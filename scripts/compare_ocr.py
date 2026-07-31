#!/usr/bin/env python3
"""Compare OCR markdown outputs against PDF embedded text baseline."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import fitz  # PyMuPDF


PAGE_HEADER_RE = re.compile(r"^##\s+Page\s+(\d+)\s*$", re.MULTILINE)


def normalize_text(text: str) -> str:
    """Normalize for fuzzy comparison: strip markdown/LaTeX noise, lowercase, collapse space."""
    text = re.sub(r"\$\s*\^\{[^}]*\}\s*\$", "", text)
    text = re.sub(r"\$[^$]*\$", "", text)
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#+\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]+", "", text)
    text = re.sub(r"[®©™°•●○◯✗✓]", "", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def tokenize(text: str) -> list[str]:
    return [t for t in normalize_text(text).split() if t]


def parse_markdown_pages(md_path: Path) -> tuple[dict[int, str], bool]:
    content = md_path.read_text(encoding="utf-8", errors="replace")
    matches = list(PAGE_HEADER_RE.finditer(content))
    pages: dict[int, str] = {}
    if not matches:
        # Skip YAML-like header block if present
        body = content
        if body.startswith("# "):
            body = re.sub(r"^#.*?\n\n", "", body, count=1, flags=re.DOTALL)
        pages[0] = body.strip()  # 0 = full document
        return pages, False

    for i, match in enumerate(matches):
        page_num = int(match.group(1))
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        pages[page_num] = content[start:end].strip()
    return pages, True


def extract_pdf_pages(pdf_path: Path) -> dict[int, str]:
    doc = fitz.open(pdf_path)
    pages = {i + 1: doc[i].get_text("text") for i in range(len(doc))}
    doc.close()
    return pages


def token_f1(ref_tokens: list[str], hyp_tokens: list[str]) -> dict[str, float]:
    ref = Counter(ref_tokens)
    hyp = Counter(hyp_tokens)
    overlap = sum((ref & hyp).values())
    prec = overlap / max(sum(hyp.values()), 1)
    rec = overlap / max(sum(ref.values()), 1)
    f1 = 2 * prec * rec / max(prec + rec, 1e-9)
    return {"precision": prec, "recall": rec, "f1": f1}


@dataclass
class PageMetrics:
    page: int
    pdf_chars: int
    ocr_chars: int
    char_ratio: float
    seq_similarity: float
    token_precision: float
    token_recall: float
    token_f1: float


@dataclass
class RunMetrics:
    name: str
    md_path: str
    pages: int
    page_aligned: bool
    pdf_total_chars: int
    ocr_total_chars: int
    avg_char_ratio: float
    avg_seq_similarity: float
    avg_token_precision: float
    avg_token_recall: float
    avg_token_f1: float
    median_token_f1: float
    weighted_token_f1: float
    per_page: list[PageMetrics]


def evaluate_run(name: str, md_path: Path, pdf_pages: dict[int, str]) -> RunMetrics:
    ocr_pages, page_aligned = parse_markdown_pages(md_path)
    if page_aligned:
        all_page_nums = sorted(set(pdf_pages) | set(ocr_pages))
    else:
        full_pdf = "\n".join(pdf_pages[p] for p in sorted(pdf_pages))
        pdf_pages = {0: full_pdf}
        all_page_nums = [0]

    per_page: list[PageMetrics] = []

    pdf_chars_total = 0
    ocr_chars_total = 0
    weighted_f1_num = 0.0
    weighted_f1_den = 0.0

    for page in all_page_nums:
        pdf_text = pdf_pages.get(page, "")
        ocr_text = ocr_pages.get(page, "")
        pdf_norm = normalize_text(pdf_text)
        ocr_norm = normalize_text(ocr_text)

        pdf_chars = len(pdf_norm)
        ocr_chars = len(ocr_norm)
        pdf_chars_total += pdf_chars
        ocr_chars_total += ocr_chars

        seq_sim = SequenceMatcher(None, pdf_norm, ocr_norm).ratio() if pdf_norm or ocr_norm else 1.0
        tf1 = token_f1(tokenize(pdf_text), tokenize(ocr_text))
        char_ratio = ocr_chars / max(pdf_chars, 1)

        pm = PageMetrics(
            page=page,
            pdf_chars=pdf_chars,
            ocr_chars=ocr_chars,
            char_ratio=char_ratio,
            seq_similarity=seq_sim,
            token_precision=tf1["precision"],
            token_recall=tf1["recall"],
            token_f1=tf1["f1"],
        )
        per_page.append(pm)
        weight = max(pdf_chars, 1)
        weighted_f1_num += tf1["f1"] * weight
        weighted_f1_den += weight

    f1s = [p.token_f1 for p in per_page]
    return RunMetrics(
        name=name,
        md_path=str(md_path),
        pages=len(all_page_nums),
        page_aligned=page_aligned,
        pdf_total_chars=pdf_chars_total,
        ocr_total_chars=ocr_chars_total,
        avg_char_ratio=statistics.mean([p.char_ratio for p in per_page]),
        avg_seq_similarity=statistics.mean([p.seq_similarity for p in per_page]),
        avg_token_precision=statistics.mean([p.token_precision for p in per_page]),
        avg_token_recall=statistics.mean([p.token_recall for p in per_page]),
        avg_token_f1=statistics.mean(f1s),
        median_token_f1=statistics.median(f1s),
        weighted_token_f1=weighted_f1_num / max(weighted_f1_den, 1e-9),
        per_page=per_page,
    )


def render_report(pdf_path: Path, runs: list[RunMetrics]) -> str:
    ranked = sorted(runs, key=lambda r: r.weighted_token_f1, reverse=True)
    lines = [
        "# OCR 对比分析报告",
        "",
        f"- PDF 基准: `{pdf_path}`",
        f"- 生成时间: {datetime.now(timezone.utc).isoformat()}",
        "- 基准说明: 使用 PDF 内嵌文本层（PyMuPDF `get_text`）作为原文对照；",
        "  指标经归一化（去 Markdown/LaTeX、标点、大小写）后计算。",
        "",
        "## 总体排名（按加权 Token F1，按 PDF 字符数加权）",
        "",
        "| 排名 | 方案 | 加权 F1 | 平均 F1 | 中位 F1 | 序列相似度 | OCR/PDF 字符比 |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for rank, run in enumerate(ranked, 1):
        lines.append(
            f"| {rank} | **{run.name}** | {run.weighted_token_f1:.4f} | "
            f"{run.avg_token_f1:.4f} | {run.median_token_f1:.4f} | "
            f"{run.avg_seq_similarity:.4f} | {run.ocr_total_chars / max(run.pdf_total_chars, 1):.3f} |"
        )

    best = ranked[0]
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"**最接近 PDF 原文的方案是 `{best.name}`**（加权 Token F1 = {best.weighted_token_f1:.4f}）。",
            "",
            "### 指标解读",
            "",
            "- **加权 Token F1**: OCR 与 PDF 文本的词级重合度，越高越贴近原文内容。",
            "- **序列相似度**: 归一化后字符序列的编辑相似度，反映排版/顺序一致性。",
            "- **OCR/PDF 字符比**: 接近 1.0 表示篇幅覆盖接近原文；明显偏低可能漏识别，偏高可能重复或噪声。",
            "",
            "## 各方案详情",
            "",
        ]
    )

    for run in ranked:
        lines.extend(
            [
                f"### {run.name}",
                "",
                f"- 文件: `{run.md_path}`",
                f"- 页数: {run.pages}" + ("（全文对比，无分页标记）" if not run.page_aligned else "（逐页对比）"),
                f"- PDF 归一化字符: {run.pdf_total_chars:,}",
                f"- OCR 归一化字符: {run.ocr_total_chars:,}",
                f"- Token Precision / Recall: {run.avg_token_precision:.4f} / {run.avg_token_recall:.4f}",
                "",
            ]
        )

    # weakest pages for best run (only when per-page alignment exists)
    if best.page_aligned:
        weak = sorted(best.per_page, key=lambda p: p.token_f1)[:10]
        lines.extend(["## 最佳方案薄弱页（Token F1 最低 10 页）", ""])
        lines.append("| 页 | Token F1 | 序列相似度 | PDF 字符 | OCR 字符 |")
        lines.append("|---:|---:|---:|---:|---:|")
        for p in weak:
            lines.append(
                f"| {p.page} | {p.token_f1:.4f} | {p.seq_similarity:.4f} | {p.pdf_chars} | {p.ocr_chars} |"
            )
        lines.append("")
    return "\n".join(lines)


REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare OCR outputs vs PDF text")
    parser.add_argument(
        "--pdf",
        default=REPO_ROOT / "sample/aml_nccn.pdf",
        type=Path,
    )
    parser.add_argument(
        "--result-root",
        default=REPO_ROOT / "result",
        type=Path,
    )
    parser.add_argument(
        "--out-dir",
        default=REPO_ROOT / "result/comparison",
        type=Path,
    )
    args = parser.parse_args()

    candidates = [
        ("GLM-OCR model_only", args.result_root / "glm_ocr/model_only/aml_nccn.md"),
        ("GLM-OCR framework", args.result_root / "glm_ocr/framework/aml_nccn.md"),
        ("PaddleOCR-VL model_only", args.result_root / "paddle_ocr/model_only/aml_nccn.md"),
        ("PaddleOCR-VL framework", args.result_root / "paddle_ocr/framework/aml_nccn.md"),
    ]

    pdf_pages = extract_pdf_pages(args.pdf)
    runs: list[RunMetrics] = []
    for name, md_path in candidates:
        if not md_path.exists():
            raise FileNotFoundError(md_path)
        runs.append(evaluate_run(name, md_path, pdf_pages))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = args.out_dir / "metrics.json"
    report_path = args.out_dir / "report.md"

    payload = {
        "pdf": str(args.pdf),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs": [
            {k: v for k, v in asdict(r).items() if k != "per_page"}
            | {"per_page": [asdict(p) for p in r.per_page]}
            for r in runs
        ],
    }
    metrics_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    report_path.write_text(render_report(args.pdf, runs), encoding="utf-8")

    ranked = sorted(runs, key=lambda r: r.weighted_token_f1, reverse=True)
    print("Ranking by weighted token F1:")
    for i, r in enumerate(ranked, 1):
        print(f"  {i}. {r.name}: {r.weighted_token_f1:.4f}")
    print(f"\nWrote {metrics_path}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
