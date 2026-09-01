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

Decide the best representation and follow these rules strictly:

1. Flowchart / process / decision tree / pathway diagram:
   Output a Mermaid flowchart in a fenced code block:
   ```mermaid
   flowchart TD
     ...
   ```
   Preserve node text, branch conditions, and direction as faithfully as possible.

2. Chart / plot / graph with readable labels or values:
   Prefer a Markdown table of extracted data, then one short summary sentence.

3. Table-like region or structured panel:
   Output a Markdown table (or HTML table if spans/merged cells are essential).

4. Schematic / architecture / hierarchy that is still structural:
   Use Mermaid or nested Markdown lists.

5. Photo, logo, stamp, decorative art, or any figure that cannot be faithfully
   turned into editable structured text:
   Output exactly:
   **Image Caption:** <concise factual caption>

Requirements:
- Keep all readable text, numbers, units, and relationships.
- Do not invent missing values or clinical advice.
- Output ONLY the converted content. No preamble or closing remarks.
"""


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
    if "```mermaid" in lower or "flowchart td" in lower or "flowchart lr" in lower:
        return "flowchart"
    if "<table" in lower or stripped.startswith("|"):
        return "table"
    if "```" in stripped:
        return "structured"
    return "text"


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
) -> FigureResult:
    relative = image_path.relative_to(job_dir).as_posix()
    try:
        text = caller(image_path, prompt=prompt).strip()
        kind = detect_figure_kind(text)
        if not text:
            kind = "caption"
            text = f"**Image Caption:** Figure `{image_path.name}` (empty model output)"
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
