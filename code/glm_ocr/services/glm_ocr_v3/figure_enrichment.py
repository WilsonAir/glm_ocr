"""Convert layout-cropped figures to editable text or image captions via VL."""

from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger("glm-ocr-service-v3")

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

DEFAULT_FIGURE_PROMPT = """\
You are converting a document region image into an editable text form.

PRIORITY: prefer Mermaid whenever the figure has nodes, branches, arrows,
decision diamonds, pathways, algorithms, or hierarchical structure.

Rules (apply in order):

1. Flowchart / process / decision tree / clinical pathway / treatment algorithm /
   risk-stratification diagram / any box-and-arrow diagram:
   ALWAYS output Mermaid. Do NOT use plain Markdown lists or ```markdown blocks.
   Use a fenced Mermaid code block only:
   ```mermaid
   flowchart TD
     A["Node text"] --> B{"Decision?"}
     B -->|Yes| C["Next step"]
     B -->|No| D["Alternative"]
   ```
   Requirements for Mermaid:
   - Prefer `flowchart TD` (or `flowchart LR` if the original is left-to-right).
   - Put full node labels in quotes: A["exact text from the figure"].
   - Preserve branch conditions on edges: -->|condition|.
   - Keep readable text, footnotes markers, and units faithfully.
   - Do not invent missing clinical steps.

2. Hierarchy / org / taxonomy / architecture schematic:
   Prefer Mermaid (`flowchart TD` or `mindmap`). Nested lists only if Mermaid
   cannot represent it cleanly.

3. Chart / plot / graph with readable labels or values:
   Markdown table of extracted data, then one short summary sentence.

4. True table-like region (grid of cells, not a pathway):
   Markdown table (or HTML table if merged cells are essential).

5. Photo, logo, stamp, decorative art, or any figure that cannot be faithfully
   turned into editable structured text:
   Output exactly:
   **Image Caption:** <concise factual caption>

Output ONLY the converted content. No preamble or closing remarks.
"""

# Clinical pathway / NCCN-style flowchart specialist (optional primary or fallback).
MERMAID_CLINICAL_PROMPT = """\
# Role: Mermaid Chart Expert & OCR Specialist

## Task
Your goal is to analyze the provided medical/clinical flowchart image and convert it into valid, executable Mermaid.js code. You must capture every detail, including text, footnotes, and logical flow.

If the image is clearly NOT a flowchart/pathway/decision diagram (e.g. photo, logo, plain table, scatter plot), output exactly:
**Image Caption:** <concise factual caption>
and stop.

## Analysis Steps
1. **Identify Structure**: Determine the main columns or phases (usually represented by top headers). These will be your `subgraph` containers.
2. **Extract Nodes**: Read every box. Preserve exact text, including superscripts/subscripts (e.g., ᵃᵃ, ᵇᵇ) and line breaks (`<br>`).
3. **Trace Edges**: Follow every arrow. Identify the source node, the target node, and any edge labels (text written on the arrow lines).
4. **Handle Complexity**: Pay attention to decision diamonds, loops, and cross-column connections.

## Output Requirements
1. **Format**: Output ONLY the Mermaid code block. Do not add conversational filler before or after.
2. **Syntax**: Use `flowchart LR` (Left to Right) unless the image is clearly Top-Down.
3. **Subgraphs**: Use `subgraph` to represent the main column headers (e.g., "RISK STRATIFICATION", "TREATMENT INDUCTION").
4. **Styling**:
   - Use `classDef` to style nodes that look like links (blue/underlined) vs. standard boxes.
   - Use `<br>` for multi-line text inside nodes.
5. **Accuracy**:
   - Do NOT summarize. Transcribe text exactly.
   - If a node connects to another across a long distance, ensure the ID linkage is correct.
   - Include all footnote markers visible in the text.

## Example Structure
```mermaid
flowchart LR
    subgraph Phase1 [HEADER TEXT]
       A[Node Text] --> B{Decision}
    end
    subgraph Phase2 [NEXT HEADER]
       B -->|Edge Label| C[Next Node]
    end
```
"""

FIGURE_PROMPT_PROFILES: dict[str, str] = {
    "default": DEFAULT_FIGURE_PROMPT,
    "mermaid_clinical": MERMAID_CLINICAL_PROMPT,
}


def resolve_figure_prompt(
    *,
    profile: str | None = None,
    prompt: str | None = None,
) -> str:
    """Resolve active prompt from custom text or a named profile."""
    if prompt and str(prompt).strip():
        return str(prompt).strip()
    key = (profile or "default").strip().lower()
    if key not in FIGURE_PROMPT_PROFILES:
        known = ", ".join(sorted(FIGURE_PROMPT_PROFILES))
        raise ValueError(f"Unknown figure prompt profile {key!r}; known: {known}")
    return FIGURE_PROMPT_PROFILES[key]


def should_retry_with_fallback(kind: str, text: str) -> bool:
    """Retry with clinical Mermaid prompt when primary missed flowchart form."""
    if kind in {"flowchart", "caption", "table"}:
        return False
    lower = text.lower()
    # Structured markdown / plain text often means a pathway was not mermaid-ized.
    if "```markdown" in lower or kind in {"structured", "text"}:
        return True
    return False


@dataclass(slots=True)
class FigureResult:
    relative_path: str
    kind: str
    text: str
    error: str | None = None


def list_layout_images(job_dir: Path) -> list[Path]:
    """Return cropped layout images under any imgs/ directory."""
    images: list[Path] = []
    for path in sorted(job_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if "imgs" not in path.relative_to(job_dir).parts:
            continue
        if "layout_vis" in path.parts:
            continue
        images.append(path)
    return images


def detect_figure_kind(text: str) -> str:
    """Best-effort classification of VL output for metadata."""
    stripped = text.strip()
    lower = stripped.lower()
    if lower.startswith("**image caption:**") or lower.startswith("image caption:"):
        return "caption"
    if (
        "```mermaid" in lower
        or re.search(r"(?m)^(flowchart|graph)\s+(td|lr|tb|rl)\b", lower)
        or ("-->" in stripped and re.search(r"\b(flowchart|graph)\b", lower))
    ):
        return "flowchart"
    if "<table" in lower or stripped.startswith("|"):
        return "table"
    if "```" in stripped:
        return "structured"
    return "text"


def normalize_mermaid_output(text: str) -> str:
    """Ensure pathway-like Mermaid bodies are wrapped in ```mermaid fences."""
    stripped = text.strip()
    if not stripped:
        return text
    lower = stripped.lower()
    if "```mermaid" in lower:
        return stripped
    if re.match(r"(?is)^(flowchart|graph)\s+(td|lr|tb|rl)\b", stripped):
        return f"```mermaid\n{stripped}\n```"
    return stripped


def format_replacement(text: str, *, kind: str, alt: str = "") -> str:
    """Wrap converted figure text for insertion into Markdown."""
    body = text.strip()
    if not body:
        caption = alt.strip() or "Converted figure was empty"
        return f"\n\n**Image Caption:** {caption}\n\n"
    if kind == "caption":
        if body.lower().startswith("image caption:"):
            body = "**Image Caption:**" + body.split(":", 1)[1]
        elif not body.lower().startswith("**image caption:**"):
            body = f"**Image Caption:** {body}"
    return f"\n\n{body}\n\n"


def call_vllm_figure(
    image_path: Path,
    *,
    vllm_url: str,
    vllm_model: str,
    prompt: str,
    timeout: int,
    max_tokens: int,
) -> str:
    """Send one cropped figure to the VL endpoint and return text content."""
    suffix = image_path.suffix.lower()
    mime = MIME_BY_SUFFIX.get(suffix, "image/png")
    data = image_path.read_bytes()
    image_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    payload = {
        "model": vllm_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    response = requests.post(vllm_url, json=payload, timeout=timeout)
    response.raise_for_status()
    raw = response.json()
    return raw["choices"][0]["message"]["content"]


def convert_one_figure(
    job_dir: Path,
    image_path: Path,
    *,
    caller: Callable[..., str],
    prompt: str,
    fallback_prompt: str | None = None,
) -> FigureResult:
    relative = image_path.relative_to(job_dir).as_posix()
    try:
        text = normalize_mermaid_output(caller(image_path, prompt=prompt).strip())
        kind = detect_figure_kind(text)
        used_fallback = False
        if (
            fallback_prompt
            and fallback_prompt.strip()
            and fallback_prompt.strip() != prompt.strip()
            and should_retry_with_fallback(kind, text)
        ):
            retry = normalize_mermaid_output(
                caller(image_path, prompt=fallback_prompt).strip()
            )
            retry_kind = detect_figure_kind(retry)
            # Prefer Mermaid flowchart; accept caption only when primary was non-caption junk.
            if retry and retry_kind == "flowchart":
                text, kind = retry, retry_kind
                used_fallback = True
            elif retry and retry_kind == "caption" and kind in {"structured", "text"}:
                # Keep primary structured text rather than losing pathway content to caption,
                # unless primary was empty-ish.
                if len(text.strip()) < 40:
                    text, kind = retry, retry_kind
                    used_fallback = True
        if not text:
            kind = "caption"
            text = f"**Image Caption:** Figure `{image_path.name}` (empty model output)"
        if used_fallback:
            logger.info("Figure %s used fallback prompt -> kind=%s", relative, kind)
        return FigureResult(relative_path=relative, kind=kind, text=text)
    except Exception as exc:
        logger.exception("Figure enrichment failed for %s", relative)
        fallback = f"**Image Caption:** Figure `{image_path.name}` (conversion failed: {exc})"
        return FigureResult(
            relative_path=relative,
            kind="caption",
            text=fallback,
            error=str(exc),
        )

def _match_figure(target: str, results: list[FigureResult]) -> FigureResult | None:
    normalized = target.strip().replace("\\", "/")
    name = Path(normalized).name
    for item in results:
        key = item.relative_path.replace("\\", "/")
        if normalized.endswith(key) or (name and name == Path(key).name):
            return item
    return None


def rewrite_markdown(markdown: str, results: list[FigureResult]) -> tuple[str, set[str]]:
    """Replace image links with converted text; return used figure keys."""
    used: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        matched = _match_figure(target, results)
        if matched is None:
            return match.group(0)
        used.add(matched.relative_path)
        return format_replacement(matched.text, kind=matched.kind, alt=alt)

    return MARKDOWN_IMAGE_RE.sub(repl, markdown), used


def append_unreferenced_figures(
    markdown: str,
    results: list[FigureResult],
    used_keys: set[str],
) -> str:
    """Append converted figures that were not referenced in Markdown."""
    leftovers = [item for item in results if item.relative_path not in used_keys]
    if not leftovers:
        return markdown if markdown.endswith("\n") else markdown + "\n"
    parts = [markdown.rstrip(), "", "## Converted Figures", ""]
    for item in leftovers:
        parts.append(f"### `{item.relative_path}`")
        parts.append("")
        parts.append(item.text.strip())
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def find_markdown_files(job_dir: Path) -> list[Path]:
    """Locate Markdown artifacts produced by the layout SDK."""
    return [
        path for path in sorted(job_dir.rglob("*.md"))
        if path.is_file() and "figure_texts" not in path.parts
    ]


def enrich_job_figures(
    job_dir: Path,
    markdown: str,
    *,
    vllm_url: str,
    vllm_model: str,
    prompt: str = DEFAULT_FIGURE_PROMPT,
    fallback_prompt: str | None = MERMAID_CLINICAL_PROMPT,
    timeout: int = 300,
    max_tokens: int = 4096,
    max_images: int = 64,
    max_workers: int = 2,
) -> tuple[str, dict[str, Any]]:
    """Convert layout imgs/ crops and rewrite Markdown to use text forms."""
    images = list_layout_images(job_dir)[: max(0, max_images)]
    summary: dict[str, Any] = {
        "enabled": True,
        "image_count": len(images),
        "converted": [],
        "errors": 0,
        "fallback_enabled": bool(fallback_prompt and fallback_prompt.strip()),
    }
    if not images:
        summary["skipped_reason"] = "no_layout_images"
        return markdown, summary

    out_dir = job_dir / "figure_texts"
    out_dir.mkdir(parents=True, exist_ok=True)

    def caller(image_path: Path, *, prompt: str) -> str:
        return call_vllm_figure(
            image_path,
            vllm_url=vllm_url,
            vllm_model=vllm_model,
            prompt=prompt,
            timeout=timeout,
            max_tokens=max_tokens,
        )

    results: list[FigureResult] = []
    workers = max(1, min(max_workers, len(images)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                convert_one_figure,
                job_dir,
                image_path,
                caller=caller,
                prompt=prompt,
                fallback_prompt=fallback_prompt,
            )
            for image_path in images
        ]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda item: item.relative_path)
    for item in results:
        stem = Path(item.relative_path).stem
        (out_dir / f"{stem}.md").write_text(item.text + "\n", encoding="utf-8")
        summary["converted"].append({
            "path": item.relative_path,
            "kind": item.kind,
            "chars": len(item.text),
            "error": item.error,
        })
        if item.error:
            summary["errors"] += 1

    md_files = find_markdown_files(job_dir)
    all_used: set[str] = set()
    if md_files:
        primary = max(md_files, key=lambda path: path.stat().st_size)
        for md_path in md_files:
            original = md_path.read_text(encoding="utf-8")
            rewritten, used = rewrite_markdown(original, results)
            all_used |= used
            if md_path == primary:
                rewritten = append_unreferenced_figures(rewritten, results, used)
            md_path.write_text(rewritten, encoding="utf-8")
        updated = primary.read_text(encoding="utf-8")
    else:
        updated, used = rewrite_markdown(markdown, results)
        all_used |= used
        updated = append_unreferenced_figures(updated, results, used)
        (job_dir / "result_enriched.md").write_text(updated, encoding="utf-8")

    summary["replaced_in_markdown"] = sorted(all_used)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return updated, summary
