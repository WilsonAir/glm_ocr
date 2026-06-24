# OCR 对比分析报告

- PDF 基准: `/data/wilson_2/de/ocr_compare/sample/aml_nccn.pdf`
- 生成时间: 2026-06-24T08:54:47.939149+00:00
- 基准说明: 使用 PDF 内嵌文本层（PyMuPDF `get_text`）作为原文对照；
  指标经归一化（去 Markdown/LaTeX、标点、大小写）后计算。

## 总体排名（按加权 Token F1，按 PDF 字符数加权）

| 排名 | 方案 | 加权 F1 | 平均 F1 | 中位 F1 | 序列相似度 | OCR/PDF 字符比 |
|---:|---|---:|---:|---:|---:|---:|
| 1 | **PaddleOCR-VL model_only** | 0.9519 | 0.9468 | 0.9770 | 0.3203 | 0.997 |
| 2 | **GLM-OCR framework** | 0.9401 | 0.9401 | 0.9401 | 0.2217 | 0.956 |
| 3 | **GLM-OCR model_only** | 0.8888 | 0.8730 | 0.9068 | 0.6632 | 0.824 |
| 4 | **PaddleOCR-VL framework** | 0.8342 | 0.7918 | 0.9085 | 0.5969 | 0.817 |

## 结论

**最接近 PDF 原文的方案是 `PaddleOCR-VL model_only`**（加权 Token F1 = 0.9519）。

### 指标解读

- **加权 Token F1**: OCR 与 PDF 文本的词级重合度，越高越贴近原文内容。
- **序列相似度**: 归一化后字符序列的编辑相似度，反映排版/顺序一致性。
- **OCR/PDF 字符比**: 接近 1.0 表示篇幅覆盖接近原文；明显偏低可能漏识别，偏高可能重复或噪声。

## 各方案详情

### PaddleOCR-VL model_only

- 文件: `/data/wilson_2/de/ocr_compare/result/paddle_ocr/model_only/aml_nccn.md`
- 页数: 209（逐页对比）
- PDF 归一化字符: 761,476
- OCR 归一化字符: 759,422
- Token Precision / Recall: 0.9550 / 0.9442

### GLM-OCR framework

- 文件: `/data/wilson_2/de/ocr_compare/result/glm_ocr/framework/aml_nccn.md`
- 页数: 1（全文对比，无分页标记）
- PDF 归一化字符: 761,684
- OCR 归一化字符: 728,337
- Token Precision / Recall: 0.9658 / 0.9157

### GLM-OCR model_only

- 文件: `/data/wilson_2/de/ocr_compare/result/glm_ocr/model_only/aml_nccn.md`
- 页数: 209（逐页对比）
- PDF 归一化字符: 761,476
- OCR 归一化字符: 627,222
- Token Precision / Recall: 0.9888 / 0.7875

### PaddleOCR-VL framework

- 文件: `/data/wilson_2/de/ocr_compare/result/paddle_ocr/framework/aml_nccn.md`
- 页数: 209（逐页对比）
- PDF 归一化字符: 761,476
- OCR 归一化字符: 621,848
- Token Precision / Recall: 0.9135 / 0.7236

## 最佳方案薄弱页（Token F1 最低 10 页）

| 页 | Token F1 | 序列相似度 | PDF 字符 | OCR 字符 |
|---:|---:|---:|---:|---:|
| 17 | 0.1487 | 0.0254 | 3426 | 1450 |
| 43 | 0.2077 | 0.0197 | 3000 | 4832 |
| 24 | 0.2611 | 0.0200 | 3205 | 4307 |
| 51 | 0.2795 | 0.1350 | 3404 | 3856 |
| 39 | 0.5142 | 0.0507 | 2801 | 1620 |
| 23 | 0.5606 | 0.3046 | 3851 | 1908 |
| 18 | 0.7184 | 0.1039 | 2543 | 1518 |
| 135 | 0.7553 | 0.7288 | 4417 | 6156 |
| 25 | 0.7672 | 0.0408 | 3649 | 2632 |
| 76 | 0.7952 | 0.0996 | 2480 | 1838 |
