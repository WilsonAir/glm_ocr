"""Translate OCR Markdown headings and paragraphs via the figure VL LLM."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
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
8. If the source contains markers like <<<OCR_SEG_ID_0>>>, keep every marker EXACTLY
   as-is, in the same order, on its own line. Translate only the text between markers.
   Do not add, remove, renumber, or translate the markers themselves.
9. Keep placeholders like <<<MATH_0_0>>> EXACTLY unchanged; do not translate,
   remove, or reformat them.

## Source text
"""

# Hunyuan Hy-MT style short prompt (EN→ZH). Keep separate from the medical
# Qwen template above. Marker line supports packed multi-segment batches.
HY_MT_EN2ZH_PROMPT = (
    "将以下文本准确翻译成简体中文，注意只输出翻译结果，不要额外解释。"
    "必须保持原文结构、换行和标记位置不变。"
    "若文本含 <<<OCR_SEG_ID_n>>> 或 <<<MATH_n_m>>> 标记，必须保留完全相同的标记数量、"
    "顺序和字符内容，绝对不能翻译、删除、转义、改写或在标记内部加空格；只翻译标记之外的文字。\n\n"
)

TRANSLATE_PROMPT_PROFILES: dict[str, str] = {
    "default": DEFAULT_TRANSLATE_PROMPT,
    "hy_mt_en2zh": HY_MT_EN2ZH_PROMPT,
}


def resolve_translate_prompt(
    *,
    prompt: str | None = None,
    profile: str | None = None,
    fallback: str = DEFAULT_TRANSLATE_PROMPT,
) -> str:
    """Resolve active translate prompt from custom text or a named profile."""
    if prompt and str(prompt).strip():
        return str(prompt).strip()
    if profile and str(profile).strip():
        key = str(profile).strip()
        if key not in TRANSLATE_PROMPT_PROFILES:
            known = ", ".join(sorted(TRANSLATE_PROMPT_PROFILES))
            raise ValueError(f"Unknown translate prompt profile {key!r}; known: {known}")
        return TRANSLATE_PROMPT_PROFILES[key]
    return fallback


# Unique delimiters for packed multi-segment calls. Keep ASCII-only and
# distinctive so models are less likely to rewrite them.
SEG_MARK_RE = re.compile(
    r"^[ \t]*<<<OCR_SEG_ID_(\d+)>>>[ \t]*$",
    re.MULTILINE,
)
SEG_MARK_FMT = "<<<OCR_SEG_ID_{idx}>>>"
PACK_HEADER = (
    "Keep every <<<OCR_SEG_ID_n>>> marker exactly, unchanged, on its own line, "
    "in the same order. Keep every <<<MATH_n_m>>> marker exactly unchanged, "
    "with the same count and order. Never translate, remove, escape, or rewrite "
    "any marker. Translate only visible text outside markers.\n\n"
)

GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
BAIDU_TRANSLATE_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

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

# Baidu Fanyi uses its own codes (zh / cht / en …).
BAIDU_LANGUAGE_CODES: dict[str, str] = {
    "simplified chinese": "zh",
    "chinese": "zh",
    "zh": "zh",
    "zh-cn": "zh",
    "zh_cn": "zh",
    "zh-hans": "zh",
    "zh-CN": "zh",
    "traditional chinese": "cht",
    "zh-tw": "cht",
    "zh_tw": "cht",
    "zh-TW": "cht",
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


def resolve_baidu_language_code(language: str) -> str:
    raw = language.strip()
    key = raw.lower()
    if key in BAIDU_LANGUAGE_CODES:
        return BAIDU_LANGUAGE_CODES[key]
    if raw in BAIDU_LANGUAGE_CODES:
        return BAIDU_LANGUAGE_CODES[raw]
    # Pass through known short codes (zh, en, jp, kor, cht, …).
    if re.fullmatch(r"[a-z]{2,3}", key):
        return key
    google = resolve_google_language_code(language)
    return BAIDU_LANGUAGE_CODES.get(google.lower(), google)


# Blocks that must not be sent to the translator as free prose.
FENCE_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
# Math is protected with ASCII placeholders before any LLM/API call. Prompt-only
# protection is insufficient because translation models may rewrite LaTeX tokens.
MATH_SPAN_RE = re.compile(
    r"(?:"
    r"\$\$[\s\S]*?\$\$|"
    r"\\\[[\s\S]*?\\\]|"
    r"\\\([\s\S]*?\\\)|"
    r"\\begin\{([A-Za-z*]+)\}[\s\S]*?\\end\{\1\}|"
    r"(?<!\\)(?<!\$)\$(?!\$)[^\n$]+?(?<!\\)\$(?!\$)"
    r")"
)
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


def _protect_math_spans(text: str, *, prefix: str) -> tuple[str, dict[str, str]]:
    """Replace LaTeX/math spans with stable ASCII placeholders."""
    formulas: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        token = f"<<<MATH_{prefix}_{len(formulas)}>>>"
        formulas[token] = match.group(0)
        return token

    return MATH_SPAN_RE.sub(replace, text), formulas


def _restore_math_spans(text: str, formulas: dict[str, str]) -> str:
    """Restore formulas and reject model output that damaged placeholders."""
    if not formulas:
        return text
    missing = [token for token in formulas if token not in text]
    if missing:
        raise ValueError(
            "Translation model altered or removed math placeholders: "
            + ", ".join(missing[:5])
        )
    restored = text
    for token, formula in formulas.items():
        restored = restored.replace(token, formula)
    return restored


def _translate_with_immutable_math(text: str, caller: Callable[[str], str]) -> str:
    """Translate prose fragments while never sending math spans to the model."""
    matches = list(MATH_SPAN_RE.finditer(text))
    if not matches:
        return caller(text)

    pieces: list[str] = []
    cursor = 0
    for match in matches:
        prose = text[cursor:match.start()]
        if prose.strip():
            leading = prose[: len(prose) - len(prose.lstrip())]
            trailing = prose[len(prose.rstrip()):]
            core = prose[len(leading): len(prose) - len(trailing) if trailing else None]
            pieces.append(leading + caller(core).strip() + trailing)
        else:
            pieces.append(prose)
        # The original formula is never included in the translation request.
        pieces.append(match.group(0))
        cursor = match.end()
    tail = text[cursor:]
    if tail.strip():
        leading = tail[: len(tail) - len(tail.lstrip())]
        trailing = tail[len(tail.rstrip()):]
        core = tail[len(leading): len(tail) - len(trailing) if trailing else None]
        pieces.append(leading + caller(core).strip() + trailing)
    else:
        pieces.append(tail)
    return "".join(pieces)


def call_vllm_translate(
    text: str,
    *,
    vllm_url: str,
    vllm_model: str,
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT,
    target_language: str = "Simplified Chinese",
    timeout: int = 300,
    max_tokens: int = 4096,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    repetition_penalty: float | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> str:
    """Call OpenAI-compatible chat completions for one text segment.

    Sampling / chat_template_kwargs are only sent when provided so dedicated
    MT models (e.g. Hy-MT) are not forced into Qwen thinking defaults.
    """
    prompt = build_translate_prompt(
        text,
        target_language=target_language,
        prompt_template=prompt_template,
    )
    payload: dict[str, Any] = {
        "model": vllm_model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if top_k is not None:
        payload["top_k"] = top_k
    if repetition_penalty is not None:
        payload["repetition_penalty"] = repetition_penalty
    if chat_template_kwargs:
        payload["chat_template_kwargs"] = chat_template_kwargs
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


def parse_baidu_credentials(
    *,
    app_id: str | None = None,
    secret_key: str | None = None,
    api_key: str | None = None,
) -> tuple[str, str]:
    """Resolve Baidu appid/secret from separate fields or `appid:secret` combo."""
    app = (app_id or "").strip()
    secret = (secret_key or "").strip()
    combo = (api_key or "").strip()
    if combo and (not app or not secret):
        # Accept appid:secret or appid|secret or appid/secret.
        for sep in (":", "|", "/", ";"):
            if sep in combo:
                left, right = combo.split(sep, 1)
                app = app or left.strip()
                secret = secret or right.strip()
                break
        else:
            # Single token: treat as secret only when app_id already provided.
            if app and not secret:
                secret = combo
            elif secret and not app:
                app = combo
    if not app or not secret:
        raise ValueError(
            "baidu credentials required: pass baidu_app_id + baidu_secret_key "
            "(or baidu_api_key as 'appid:secret')"
        )
    return app, secret


def call_baidu_translate(
    text: str,
    *,
    app_id: str,
    secret_key: str,
    target_language: str = "Simplified Chinese",
    source_language: str = "en",
    timeout: int = 60,
    max_retries: int = 4,
    retry_backoff_sec: float = 1.2,
) -> str:
    """Translate one fragment via Baidu Fanyi open platform (vip/translate)."""
    app, secret = parse_baidu_credentials(app_id=app_id, secret_key=secret_key)
    q = text if text is not None else ""
    if not q.strip():
        return q
    target = resolve_baidu_language_code(target_language)
    source = resolve_baidu_language_code(source_language) if source_language else "auto"
    last_error: Exception | None = None
    for attempt in range(max(1, max_retries)):
        salt = str(random.randint(32768, 65536))
        sign = hashlib.md5(f"{app}{q}{salt}{secret}".encode("utf-8")).hexdigest()
        response = requests.post(
            BAIDU_TRANSLATE_URL,
            data={
                "q": q,
                "from": source or "auto",
                "to": target,
                "appid": app,
                "salt": salt,
                "sign": sign,
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            last_error = RuntimeError(
                f"Baidu Translate HTTP {response.status_code}: {response.text[:500]}"
            )
        else:
            raw = response.json()
            err = str(raw.get("error_code") or "")
            if err:
                # 54003 = access/QPS limit on free tier; retry with backoff.
                if err == "54003" and attempt + 1 < max_retries:
                    time.sleep(retry_backoff_sec * (attempt + 1))
                    continue
                last_error = RuntimeError(
                    f"Baidu Translate error {raw.get('error_code')}: {raw.get('error_msg')}"
                )
            else:
                results = raw.get("trans_result") or []
                if not results:
                    last_error = RuntimeError("Baidu Translate returned empty trans_result")
                else:
                    # Keep paragraph breaks when API splits into multiple sentences.
                    return "\n".join(str(item.get("dst") or "") for item in results)
        if attempt + 1 < max_retries:
            time.sleep(retry_backoff_sec * (attempt + 1))
    assert last_error is not None
    raise last_error


def translate_segments_baidu(
    segments: list[TranslateSegment],
    *,
    app_id: str,
    secret_key: str,
    target_language: str,
    source_language: str = "en",
    timeout: int = 60,
    max_workers: int = 1,
    min_interval_sec: float = 1.05,
    merge_segments: bool = True,
    merge_max_chars: int = 2800,
    merge_small_para_chars: int = 1200,
) -> list[TranslateSegment]:
    """Translate via Baidu; merge segments with markers; pace free-tier QPS."""
    last_call = {"t": 0.0}

    def caller(text: str) -> str:
        if min_interval_sec > 0 and last_call["t"]:
            wait = min_interval_sec - (time.perf_counter() - last_call["t"])
            if wait > 0:
                time.sleep(wait)
        out = call_baidu_translate(
            text,
            app_id=app_id,
            secret_key=secret_key,
            target_language=target_language,
            source_language=source_language,
            timeout=timeout,
        )
        last_call["t"] = time.perf_counter()
        return out

    return translate_segments(
        segments,
        caller=caller,
        max_workers=1 if merge_segments else max_workers,
        merge_segments=merge_segments,
        merge_max_chars=merge_max_chars,
        max_tokens=None,  # Baidu is char-capped below, not LLM max_tokens.
        merge_small_para_chars=merge_small_para_chars,
    )


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


def pack_segments_with_markers(
    segments: list[TranslateSegment],
    *,
    source_overrides: dict[int, str] | None = None,
) -> str:
    """Join segments with stable delimiters for a single translator call."""
    parts: list[str] = [PACK_HEADER.rstrip(), ""]
    for seg in segments:
        parts.append(SEG_MARK_FMT.format(idx=seg.index))
        source = (source_overrides or {}).get(seg.index, seg.source)
        parts.append(source.rstrip("\n"))
        parts.append("")  # blank line between blocks
    return "\n".join(parts).rstrip() + "\n"


def unpack_segments_with_markers(
    packed_translation: str,
    segments: list[TranslateSegment],
) -> dict[int, str] | None:
    """Split packed translation back to {segment_index: text}.

    Returns None when no usable markers are found. Partial hits are returned
    so the caller can fall back only for missing segments.
    """
    text = packed_translation.strip()
    if not text:
        return None
    matches = list(SEG_MARK_RE.finditer(text))
    if not matches:
        # Some models put markers inline; try a looser search.
        loose = list(re.finditer(r"<<<OCR_SEG_ID_(\d+)>>>", text))
        if not loose:
            return None
        matches = loose  # type: ignore[assignment]
        out: dict[int, str] = {}
        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end]
            body = re.sub(r"^\s*\n", "", body).strip()
            out[int(match.group(1))] = body
        expected = {seg.index for seg in segments}
        if not expected.intersection(out):
            return None
        return out

    out = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end]
        body = re.sub(r"^\s*\n", "", body).strip()
        out[int(match.group(1))] = body
    expected = {seg.index for seg in segments}
    if not expected.intersection(out):
        return None
    missing = expected - set(out)
    if missing:
        logger.warning(
            "Packed translate missing markers for segments %s (got %s)",
            sorted(missing),
            sorted(out),
        )
    return out


def resolve_merge_max_chars(
    *,
    merge_max_chars: int,
    max_tokens: int,
    output_chars_per_token: float = 1.2,
    expansion_ratio: float = 1.3,
    safety: float = 0.80,
) -> int:
    """Cap packed-batch size so expected translation fits in max_tokens.

    Conservative for EN→ZH: treat ~1.2 output chars/token and allow translation
    to expand vs source, with spare room for markers / model verbosity.
    """
    tokens = max(64, int(max_tokens))
    # Hard ceiling on completion characters.
    max_out_chars = int(tokens * max(0.5, output_chars_per_token) * max(0.5, safety))
    # Source pack (content + markers) should stay below this after expansion.
    capped = int(max_out_chars / max(1.0, expansion_ratio))
    capped -= len(PACK_HEADER) + 128
    capped = max(200, capped)
    if merge_max_chars and merge_max_chars > 0:
        capped = min(int(merge_max_chars), capped)
    return capped


def _chunk_segments_by_chars(
    segments: list[TranslateSegment],
    *,
    max_chars: int,
) -> list[list[TranslateSegment]]:
    """Pack segments into batches that stay under max_chars (incl. markers)."""
    if max_chars <= 0:
        return [[seg] for seg in segments]
    batches: list[list[TranslateSegment]] = []
    current: list[TranslateSegment] = []
    current_len = len(PACK_HEADER) + 2
    for seg in segments:
        marker = SEG_MARK_FMT.format(idx=seg.index)
        piece = len(marker) + 1 + len(seg.source) + 2
        if current and current_len + piece > max_chars:
            batches.append(current)
            current = []
            current_len = len(PACK_HEADER) + 2
        # Single oversized segment: still alone in its batch.
        if not current and piece > max_chars:
            batches.append([seg])
            continue
        current.append(seg)
        current_len += piece
    if current:
        batches.append(current)
    return batches


def _chunk_paragraphs_for_merge(
    paragraphs: list[TranslateSegment],
    *,
    max_chars: int,
    small_para_chars: int,
) -> list[list[TranslateSegment]]:
    """Pack small paragraphs together; translate large ones alone.

    Walks in document order: accumulate small paras into packed batches (under
    max_chars / max_tokens budget). When a large paragraph appears, flush the
    small buffer first, then emit the large paragraph as its own batch(es).
    """
    if not paragraphs:
        return []
    small_limit = max(64, int(small_para_chars))
    batches: list[list[TranslateSegment]] = []
    small_buf: list[TranslateSegment] = []

    def flush_small() -> None:
        nonlocal small_buf
        if not small_buf:
            return
        batches.extend(_chunk_segments_by_chars(small_buf, max_chars=max_chars))
        small_buf = []

    for seg in paragraphs:
        if len(seg.source.strip()) <= small_limit:
            small_buf.append(seg)
            continue
        flush_small()
        batches.extend(_chunk_segments_by_chars([seg], max_chars=max_chars))
    flush_small()
    return batches


def translate_segments(
    segments: list[TranslateSegment],
    *,
    caller: Callable[[str], str],
    max_workers: int = 2,
    merge_segments: bool = True,
    merge_max_chars: int = 3500,
    max_tokens: int | None = None,
    merge_small_para_chars: int = 1200,
) -> list[TranslateSegment]:
    """Translate heading/paragraph segments; optionally merge with markers."""
    todo = [seg for seg in segments if _should_translate(seg)]
    if not todo:
        return segments

    effective_merge_chars = merge_max_chars
    if max_tokens is not None:
        effective_merge_chars = resolve_merge_max_chars(
            merge_max_chars=merge_max_chars,
            max_tokens=max_tokens,
        )

    def run_one(seg: TranslateSegment) -> TranslateSegment:
        try:
            protected, formulas = _protect_math_spans(
                seg.source, prefix=f"{seg.index}_single"
            )
            translated = _fix_heading_marker(
                seg.source,
                _restore_math_spans(caller(protected), formulas),
                kind=seg.kind,
            )
            seg.translated = translated
        except Exception as exc:  # noqa: BLE001
            logger.exception("Translate failed for segment %s", seg.index)
            seg.error = str(exc)
            seg.translated = seg.source
        return seg

    def run_batch(batch: list[TranslateSegment]) -> None:
        if len(batch) == 1:
            run_one(batch[0])
            return
        protected_sources: dict[int, str] = {}
        formula_maps: dict[int, dict[str, str]] = {}
        for seg in batch:
            protected_sources[seg.index], formula_maps[seg.index] = _protect_math_spans(
                seg.source, prefix=f"{seg.index}_batch"
            )
        packed = pack_segments_with_markers(batch, source_overrides=protected_sources)
        try:
            raw = caller(packed)
            mapping = unpack_segments_with_markers(raw, batch)
            if mapping is None:
                logger.warning(
                    "Packed translate unpack failed (%d segs); falling back "
                    "to per-segment",
                    len(batch),
                )
                for seg in batch:
                    run_one(seg)
                return
            missing: list[TranslateSegment] = []
            for seg in batch:
                if seg.index in mapping and mapping[seg.index].strip():
                    seg.translated = _fix_heading_marker(
                        seg.source,
                        _restore_math_spans(mapping[seg.index], formula_maps[seg.index]),
                        kind=seg.kind,
                    )
                else:
                    missing.append(seg)
            for seg in missing:
                run_one(seg)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Packed translate failed for %d segments", len(batch))
            for seg in batch:
                if seg.translated is None:
                    seg.error = str(exc)
                    seg.translated = seg.source

    if not merge_segments:
        workers = max(1, min(max_workers, len(todo)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run_one, seg) for seg in todo]
            for future in as_completed(futures):
                future.result()
        return segments

    # Headings are usually short → pack together under the budget.
    # Paragraphs: pack small ones together; keep large blocks solo.
    headings = [s for s in todo if s.kind == "heading"]
    paragraphs = [s for s in todo if s.kind == "paragraph"]
    others = [s for s in todo if s.kind not in {"heading", "paragraph"}]
    batches = (
        _chunk_segments_by_chars(headings, max_chars=effective_merge_chars)
        + _chunk_paragraphs_for_merge(
            paragraphs,
            max_chars=effective_merge_chars,
            small_para_chars=merge_small_para_chars,
        )
        + _chunk_segments_by_chars(others, max_chars=effective_merge_chars)
    )
    logger.info(
        "Translate merge: batches=%d packed_batches=%d packed_segments=%d "
        "solo=%d budget_chars=%d small_para<=%d",
        len(batches),
        sum(1 for batch in batches if len(batch) > 1),
        sum(len(batch) for batch in batches if len(batch) > 1),
        sum(1 for batch in batches if len(batch) == 1),
        effective_merge_chars,
        merge_small_para_chars,
    )
    workers = max(1, min(max_workers, len(batches)))
    if workers == 1:
        for batch in batches:
            run_batch(batch)
        return segments
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_batch, batch) for batch in batches]
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
    translate_model: str | None = None,
    google_api_key: str | None = None,
    baidu_app_id: str | None = None,
    baidu_secret_key: str | None = None,
    baidu_api_key: str | None = None,
    target_language: str = "Simplified Chinese",
    source_language: str = "en",
    prompt_template: str = DEFAULT_TRANSLATE_PROMPT,
    timeout: int = 300,
    max_tokens: int = 4096,
    max_workers: int = 2,
    max_segments: int = 500,
    google_batch_size: int = 40,
    merge_segments: bool = True,
    merge_max_chars: int = 3500,
    merge_small_para_chars: int = 1200,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    repetition_penalty: float | None = None,
    chat_template_kwargs: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Translate headings/paragraphs in Markdown; leave Mermaid/fences alone."""
    provider_key = (provider or "llm").strip().lower()
    if provider_key not in {"llm", "google", "baidu"}:
        raise ValueError(f"Unsupported translate provider: {provider}")
    if provider_key == "google" and not (google_api_key and google_api_key.strip()):
        raise ValueError("google_api_key is required when translate_provider=google")
    baidu_app = ""
    baidu_secret = ""
    if provider_key == "baidu":
        baidu_app, baidu_secret = parse_baidu_credentials(
            app_id=baidu_app_id,
            secret_key=baidu_secret_key,
            api_key=baidu_api_key,
        )

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
        "merge_segments": merge_segments,
        "merge_max_chars": merge_max_chars,
        "merge_small_para_chars": merge_small_para_chars,
        "merge_max_chars_effective": None,
    }
    if provider_key == "llm":
        summary["model"] = vllm_model
        summary["vllm_url"] = vllm_url
        summary["translate_model"] = translate_model
        summary["max_tokens"] = max_tokens
        summary["merge_max_chars_effective"] = resolve_merge_max_chars(
            merge_max_chars=merge_max_chars,
            max_tokens=max_tokens,
        )
        if temperature is not None:
            summary["temperature"] = temperature
        if top_p is not None:
            summary["top_p"] = top_p
        if top_k is not None:
            summary["top_k"] = top_k
        if repetition_penalty is not None:
            summary["repetition_penalty"] = repetition_penalty
    elif provider_key == "google":
        summary["google_target"] = resolve_google_language_code(target_language)
        summary["google_source"] = resolve_google_language_code(source_language)
    else:
        summary["baidu_target"] = resolve_baidu_language_code(target_language)
        summary["baidu_source"] = resolve_baidu_language_code(source_language)
        summary["baidu_app_id"] = baidu_app
        summary["merge_max_chars_effective"] = min(merge_max_chars, 2800)

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
    elif provider_key == "baidu":
        translate_segments_baidu(
            segments,
            app_id=baidu_app,
            secret_key=baidu_secret,
            target_language=target_language,
            source_language=source_language,
            timeout=min(timeout, 60),
            max_workers=1,
            merge_segments=merge_segments,
            # Stay under Baidu ~6000 char/request limit (UTF-8).
            merge_max_chars=min(merge_max_chars, 2800),
            merge_small_para_chars=merge_small_para_chars,
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
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                repetition_penalty=repetition_penalty,
                chat_template_kwargs=chat_template_kwargs,
            )

        translate_segments(
            segments,
            caller=caller,
            max_workers=max_workers,
            merge_segments=merge_segments,
            merge_max_chars=merge_max_chars,
            max_tokens=max_tokens,
            merge_small_para_chars=merge_small_para_chars,
        )
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
