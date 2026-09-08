"""Translate OCR Markdown headings and paragraphs via the figure VL LLM."""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger("glm-ocr-service-v3")

DEFAULT_TRANSLATE_PROMPT = """\
# Role: Medical Document Translator

## Task
Translate the given medical/clinical document fragment into <<TARGET_LANGUAGE>>.
The source is OCR Markdown from oncology guidelines, case notes, or similar.

## Rules
1. Output ONLY the translated text. No reasoning, thinking process, preamble, notes, or quotes.
2. Translate the exact fragment given — do not substitute a different disease, drug, or section title.
3. Preserve Markdown structure exactly:
   - Keep heading markers (# ## ### …)
   - Keep emphasis (*italic*, **bold**), lists, links, and line breaks
   - Keep LaTeX / math spans ($...$, $^{...}$) unchanged in place
4. Do NOT translate or alter:
   - Fenced code blocks (``` … ```), including Mermaid
   - HTML tags and tables that are structural markup
   - Drug names / regimen abbreviations when commonly kept in English
     (e.g. ATRA, TKI, HCT, MRD, CR, ALL, AML) — keep the English token and
     may add a brief Chinese gloss in parentheses only when clarity requires it
   - Citation markers, footnote superscripts (a,b,1,2,aa), and page references
     like (ALL-4), (APL-B 1 of 6)
5. Translate paragraph titles and body prose fluently into natural <<TARGET_LANGUAGE>>.
6. Keep numbers, units, dosages, and laboratory values unchanged.
7. Do not invent clinical content. If a token is illegible OCR garbage, keep it as-is.

## Source text
"""

GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"

# Human-readable labels → Google BCP-47 codes.
TARGET_LANGUAGE_CODES: dict[str, str] = {
    "simplified chinese": "zh-CN",
    "chinese": "zh-CN",
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh_cn": "zh-CN",
    "zh-hans": "zh-CN",
    "traditional chinese": "zh-TW",
    "zh-tw": "zh-TW",
    "zh_tw": "zh-TW",
    "english": "en",
    "en": "en",
}


def resolve_google_language_code(language: str) -> str:
    key = language.strip().lower()
    if key in TARGET_LANGUAGE_CODES:
        return TARGET_LANGUAGE_CODES[key]
    # Already looks like a language code (e.g. ja, fr, zh-CN).
    if re.fullmatch(r"[a-z]{2}(-[A-Za-z]{2})?", language.strip()):
        return language.strip()
    return language.strip()


# Blocks that must not be sent to the translator as free prose.
FENCE_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
HEADING_OR_PARA_RE = re.compile(
    r"(?P<head>^#{1,6}[ \t]+.+$)|"
    r"(?P<para>^(?!#{1,6}\s|!\[[^\]]*\]\([^)]+\)|\s*\|)(?!\s*$).+(?:\n(?!#{1,6}\s|```|\s*\|).+)*)",
    re.MULTILINE,
)


@dataclass(slots=True)
class TranslateSegment:
    index: int
    kind: str  # heading | paragraph | fence | other
    source: str
    translated: str | None = None
    error: str | None = None


def build_translate_prompt(
    text: str,
    *,
    target_language: str = "Simplified Chinese",
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT,
) -> str:
    header = prompt_template.replace("<<TARGET_LANGUAGE>>", target_language)
    return f"{header}{text.strip()}\n"


def call_vllm_translate(
    text: str,
    *,
    vllm_url: str,
    vllm_model: str,
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT,
    target_language: str = "Simplified Chinese",
    timeout: int = 300,
    max_tokens: int = 4096,
) -> str:
    """Call OpenAI-compatible chat completions for one text segment."""
    prompt = build_translate_prompt(
        text,
        target_language=target_language,
        prompt_template=prompt_template,
    )
    payload = {
        "model": vllm_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        # Qwen3.5 defaults to hybrid thinking; disable for clean translations.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    response = requests.post(vllm_url, json=payload, timeout=timeout)
    response.raise_for_status()
    raw = response.json()
    message = raw["choices"][0]["message"]
    content = message.get("content") or ""
    # Some servers put the answer in content after stripping reasoning.
    if not str(content).strip() and message.get("reasoning_content"):
        content = message["reasoning_content"]
    return _strip_model_wrapper(str(content))


def call_google_translate(
    text: str,
    *,
    api_key: str,
    target_language: str = "Simplified Chinese",
    source_language: str = "en",
    timeout: int = 60,
) -> str:
    """Translate one fragment via Google Cloud Translation Basic (v2) REST API."""
    results = call_google_translate_batch(
        [text],
        api_key=api_key,
        target_language=target_language,
        source_language=source_language,
        timeout=timeout,
    )
    return results[0] if results else text


def call_google_translate_batch(
    texts: list[str],
    *,
    api_key: str,
    target_language: str = "Simplified Chinese",
    source_language: str = "en",
    timeout: int = 60,
) -> list[str]:
    """Batch-translate via Google Translate v2 (`key` query param)."""
    if not api_key or not api_key.strip():
        raise ValueError("google_api_key is required for provider=google")
    if not texts:
        return []
    target = resolve_google_language_code(target_language)
    source = resolve_google_language_code(source_language) if source_language else ""
    payload: dict[str, Any] = {
        "q": texts,
        "target": target,
        "format": "text",
    }
    if source:
        payload["source"] = source
    response = requests.post(
        GOOGLE_TRANSLATE_URL,
        params={"key": api_key.strip()},
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(
            f"Google Translate HTTP {response.status_code}: {detail}"
        )
    raw = response.json()
    translations = raw.get("data", {}).get("translations") or []
    if len(translations) != len(texts):
        raise RuntimeError(
            f"Google Translate returned {len(translations)} results "
            f"for {len(texts)} inputs"
        )
    return [str(item.get("translatedText") or "") for item in translations]


def _strip_model_wrapper(text: str) -> str:
    """Remove common CoT / fence wrappers from model output."""
    stripped = text.strip()
    if not stripped:
        return stripped
    # Drop explicit <think>...</think> blocks when present.
    stripped = re.sub(
        r"<think>[\s\S]*?</think>",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    # Prefer last fenced block if the model wrapped the answer.
    fences = re.findall(r"```(?:markdown|md|text)?\s*\n([\s\S]*?)```", stripped)
    if fences and "```mermaid" not in stripped.lower():
        # Only unwrap when it looks like the whole answer was wrapped.
        candidate = fences[-1].strip()
        if candidate and len(candidate) >= max(20, int(len(stripped) * 0.3)):
            stripped = candidate
    # If the model dumped a long "Thinking Process:" essay, keep the last
    # short non-bullet line that looks like the final translation.
    if re.search(r"(?i)^\s*(?:thinking process|分析|推理)\s*[:：]", stripped) or (
        stripped.count("\n") > 8 and "*   " in stripped and len(stripped) > 400
    ):
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        candidates = [
            ln for ln in lines
            if not ln.startswith(("*", "-", "#", "1.", "2.", "3.", "4.", "5.", "6."))
            and not re.match(r"(?i)^(thinking|wait|okay|final|source|translation|draft|let'?s|i will|actually)\b", ln)
            and "->" not in ln
            and len(ln) < 500
        ]
        if candidates:
            stripped = candidates[-1]
        else:
            # Fall back: last markdown heading line if any.
            heads = [ln for ln in lines if re.match(r"^#{1,6}\s+\S", ln)]
            if heads:
                stripped = heads[-1]
    # Drop a leading "Translation:" style label.
    stripped = re.sub(
        r"^(?:翻译结果|译文|Translation)\s*[:：]\s*",
        "",
        stripped,
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped.strip()


def split_markdown_for_translation(markdown: str) -> list[TranslateSegment]:
    """Split Markdown into heading/paragraph segments; keep fences intact."""
    segments: list[TranslateSegment] = []
    parts = FENCE_RE.split(markdown)
    index = 0
    for part in parts:
        if not part:
            continue
        if part.startswith("```"):
            segments.append(TranslateSegment(index=index, kind="fence", source=part))
            index += 1
            continue
        pos = 0
        for match in HEADING_OR_PARA_RE.finditer(part):
            start, end = match.span()
            if start > pos:
                gap = part[pos:start]
                if gap.strip():
                    segments.append(
                        TranslateSegment(index=index, kind="other", source=gap)
                    )
                    index += 1
                elif gap:
                    segments.append(
                        TranslateSegment(index=index, kind="other", source=gap)
                    )
                    index += 1
            if match.group("head"):
                segments.append(
                    TranslateSegment(
                        index=index,
                        kind="heading",
                        source=match.group("head"),
                    )
                )
            else:
                segments.append(
                    TranslateSegment(
                        index=index,
                        kind="paragraph",
                        source=match.group("para"),
                    )
                )
            index += 1
            pos = end
        if pos < len(part):
            tail = part[pos:]
            segments.append(TranslateSegment(index=index, kind="other", source=tail))
            index += 1
    return segments


def _should_translate(segment: TranslateSegment) -> bool:
    if segment.kind not in {"heading", "paragraph"}:
        return False
    text = segment.source.strip()
    if not text:
        return False
    # Skip image-only or pure markdown noise lines.
    if text.startswith("![") and "](" in text:
        return False
    return True


def _fix_heading_marker(source: str, translated: str, *, kind: str) -> str:
    text = (translated or "").strip() or source
    if kind != "heading":
        return text
    match = re.match(r"^#+", source.strip())
    if not match:
        return text
    src_level = len(match.group(0))
    if not re.match(r"^#{1,6}\s", text):
        body = re.sub(r"^#{1,6}\s*", "", text).strip()
        return f"{'#' * src_level} {body}"
    return text


def translate_segments(
    segments: list[TranslateSegment],
    *,
    caller: Callable[[str], str],
    max_workers: int = 2,
) -> list[TranslateSegment]:
    """Translate heading/paragraph segments in parallel."""
    todo = [seg for seg in segments if _should_translate(seg)]
    if not todo:
        return segments

    def run_one(seg: TranslateSegment) -> TranslateSegment:
        try:
            translated = _fix_heading_marker(
                seg.source, caller(seg.source), kind=seg.kind
            )
            seg.translated = translated
        except Exception as exc:  # noqa: BLE001
            logger.exception("Translate failed for segment %s", seg.index)
            seg.error = str(exc)
            seg.translated = seg.source
        return seg

    workers = max(1, min(max_workers, len(todo)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, seg) for seg in todo]
        for future in as_completed(futures):
            future.result()
    return segments


def translate_segments_google(
    segments: list[TranslateSegment],
    *,
    api_key: str,
    target_language: str,
    source_language: str = "en",
    timeout: int = 60,
    batch_size: int = 40,
) -> list[TranslateSegment]:
    """Translate via Google in batches (fewer HTTP round-trips)."""
    todo = [seg for seg in segments if _should_translate(seg)]
    if not todo:
        return segments
    size = max(1, batch_size)
    for start in range(0, len(todo), size):
        chunk = todo[start : start + size]
        try:
            outs = call_google_translate_batch(
                [seg.source for seg in chunk],
                api_key=api_key,
                target_language=target_language,
                source_language=source_language,
                timeout=timeout,
            )
            for seg, out in zip(chunk, outs, strict=True):
                seg.translated = _fix_heading_marker(
                    seg.source, out, kind=seg.kind
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Google Translate batch failed (%d-%d)",
                start,
                start + len(chunk) - 1,
            )
            for seg in chunk:
                seg.error = str(exc)
                seg.translated = seg.source
    return segments


def join_translated_segments(segments: list[TranslateSegment]) -> str:
    parts: list[str] = []
    for seg in segments:
        if seg.kind in {"heading", "paragraph"} and seg.translated is not None:
            parts.append(seg.translated)
        else:
            parts.append(seg.source)
    text = "".join(parts)
    if not text.endswith("\n"):
        text += "\n"
    return text


def find_markdown_files(job_dir: Path) -> list[Path]:
    return [
        path for path in sorted(job_dir.rglob("*.md"))
        if path.is_file()
        and "figure_texts" not in path.parts
        and "translations" not in path.parts
    ]


def translate_job_markdown(
    job_dir: Path,
    markdown: str,
    *,
    provider: str = "llm",
    vllm_url: str = "",
    vllm_model: str = "",
    google_api_key: str | None = None,
    target_language: str = "Simplified Chinese",
    source_language: str = "en",
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT,
    timeout: int = 300,
    max_tokens: int = 4096,
    max_workers: int = 2,
    max_segments: int = 500,
    google_batch_size: int = 40,
) -> tuple[str, dict[str, Any]]:
    """Translate headings/paragraphs in Markdown; leave Mermaid/fences alone."""
    provider_key = (provider or "llm").strip().lower()
    if provider_key not in {"llm", "google"}:
        raise ValueError(f"Unsupported translate provider: {provider}")
    if provider_key == "google" and not (google_api_key and google_api_key.strip()):
        raise ValueError("google_api_key is required when translate_provider=google")

    segments = split_markdown_for_translation(markdown)
    translatable = [s for s in segments if _should_translate(s)]
    if len(translatable) > max_segments:
        # Keep document order; only translate the first N eligible segments.
        keep = {id(s) for s in translatable[:max_segments]}
        for seg in translatable[max_segments:]:
            seg.kind = "other"  # skip remaining
        translatable = [s for s in segments if id(s) in keep]

    summary: dict[str, Any] = {
        "enabled": True,
        "provider": provider_key,
        "target_language": target_language,
        "segment_count": len(segments),
        "translated_count": 0,
        "errors": 0,
    }
    if provider_key == "llm":
        summary["model"] = vllm_model
        summary["vllm_url"] = vllm_url
    else:
        summary["google_target"] = resolve_google_language_code(target_language)
        summary["google_source"] = resolve_google_language_code(source_language)

    if not translatable:
        summary["skipped_reason"] = "no_heading_or_paragraph"
        return markdown, summary

    if provider_key == "google":
        translate_segments_google(
            segments,
            api_key=google_api_key or "",
            target_language=target_language,
            source_language=source_language,
            timeout=min(timeout, 120),
            batch_size=google_batch_size,
        )
    else:
        def caller(text: str) -> str:
            return call_vllm_translate(
                text,
                vllm_url=vllm_url,
                vllm_model=vllm_model,
                prompt_template=prompt_template,
                target_language=target_language,
                timeout=timeout,
                max_tokens=max_tokens,
            )

        translate_segments(segments, caller=caller, max_workers=max_workers)
    updated = join_translated_segments(segments)

    out_dir = job_dir / "translations"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "translated.md").write_text(updated, encoding="utf-8")

    details = []
    for seg in segments:
        if seg.kind not in {"heading", "paragraph"}:
            continue
        if seg.translated is None and not seg.error:
            continue
        details.append({
            "index": seg.index,
            "kind": seg.kind,
            "source_chars": len(seg.source),
            "translated_chars": len(seg.translated or ""),
            "error": seg.error,
        })
        if seg.error:
            summary["errors"] += 1
        else:
            summary["translated_count"] += 1
    summary["segments"] = details

    for md_path in find_markdown_files(job_dir):
        original = md_path.read_text(encoding="utf-8")
        local_segments = split_markdown_for_translation(original)
        # Reuse translations by exact source match when possible.
        by_source = {
            s.source: s.translated
            for s in segments
            if s.translated is not None and s.kind in {"heading", "paragraph"}
        }
        for seg in local_segments:
            if seg.kind in {"heading", "paragraph"} and seg.source in by_source:
                seg.translated = by_source[seg.source]
        rewritten = join_translated_segments(local_segments)
        md_path.write_text(rewritten, encoding="utf-8")

    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Prefer rewritten primary markdown if present.
    md_files = find_markdown_files(job_dir)
    if md_files:
        primary = max(md_files, key=lambda path: path.stat().st_size)
        return primary.read_text(encoding="utf-8"), summary
    return updated, summary
