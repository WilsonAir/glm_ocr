# GLM-OCR v3 接口调用说明

面向调用方：提交 PDF / 图片，获取 Markdown OCR 结果；版面中的流程图默认用
**Qwen3.5-9B** 转成可编辑 **Mermaid**。

更完整的服务说明见 [README.md](README.md)。流程图模型对比见
[FIGURE_VL_COMPARE.md](FIGURE_VL_COMPARE.md)。

---

## 接口地址

| 场景 | 地址 |
|---|---|
| 网关（推荐） | `http://172.19.64.72/glm-ocr-v3` |
| 部署机本机 | `http://127.0.0.1:18094` |

下文用 `{BASE_URL}` 表示上述根地址。

---

## 能力概览

| 步骤 | 模型 / 组件 |
|---|---|
| 版面检测 | PP-DocLayoutV3 |
| 正文 / 区域 OCR | `glm-ocr`（vLLM `:18080`） |
| 流程图 → Mermaid | `qwen3.5-9b`（vLLM `:18003`） |
| Prompt | `mermaid_clinical`（临床 flowchart 专用） |

默认 `enrich_figures=true`：Layout 裁出的 `imgs/` 会再调 Qwen，生成 Mermaid；
非流程图则输出 `**Image Caption:** ...`。

---

## 支持的文件类型

| 类型 | 后缀 |
|---|---|
| PDF | `.pdf` |
| 图片 | `.jpg` `.jpeg` `.png` `.bmp` `.gif` `.webp` |

---

## 1. 健康检查

```http
GET /health
```

```bash
curl -s '{BASE_URL}/health' | python -m json.tool
```

正常示例：

```json
{
  "status": "ok",
  "model": "glm-ocr",
  "service": "glm-ocr-queued-parse-v3",
  "figure_enrichment": {
    "enabled": true,
    "prompt_profile": "mermaid_clinical",
    "vllm_model": "qwen3.5-9b",
    "vllm_url": "http://127.0.0.1:18003/v1/chat/completions"
  }
}
```

---

## 2. 队列状态

```http
GET /queue_status
```

```bash
curl -s '{BASE_URL}/queue_status' | python -m json.tool
```

队列：`pdf_layout` / `image_layout` / `model_only`。满时提交返回 HTTP `429`。

---

## 3. 统一解析（同步）

```http
POST /parse
Content-Type: multipart/form-data
```

| 参数 | 位置 | 必填 | 默认 | 说明 |
|---|---|---:|---|---|
| `file` | Form | 是 | — | PDF 或图片 |
| `image_mode` | Query | 否 | `auto` | `auto` / `layout` / `model_only` |
| `enrich_figures` | Query | 否 | `true` | 是否对 `imgs/` 做 Qwen→Mermaid |

`image_mode`：

| 值 | 说明 |
|---|---|
| `auto` | 图片默认 Layout |
| `layout` | 强制 Layout |
| `model_only` | 图片直送 glm-ocr，不做 Layout，也不做 Mermaid |

PDF 不能使用 `model_only`。

### 解析 PDF（默认开 Mermaid）

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true' \
  -F 'file=@/path/to/document.pdf'
```

### 解析整页图片

```bash
curl -X POST \
  '{BASE_URL}/parse?image_mode=layout&enrich_figures=true' \
  -F 'file=@/path/to/page.jpg'
```

### 关闭图转 Mermaid（接近纯 OCR）

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=false' \
  -F 'file=@/path/to/document.pdf'
```

### 局部裁剪图（不做 Layout）

```bash
curl -X POST \
  '{BASE_URL}/parse?image_mode=model_only' \
  -F 'file=@/path/to/crop.png'
```

### 成功响应（节选）

```json
{
  "job_id": "cd33c76a-839d-4958-ab85-e16d80ecb72e",
  "status": "completed",
  "queue": "pdf_layout",
  "filename": "document.pdf",
  "chars": 120000,
  "elapsed_sec": 180.5,
  "enrich_figures": true,
  "text": "# ... Markdown，内含 ```mermaid 块 ...",
  "figure_enrichment": {
    "enabled": true,
    "image_count": 5,
    "converted": [
      {
        "path": "document_0/imgs/cropped_page13_idx0.jpg",
        "kind": "flowchart",
        "chars": 1200,
        "error": null
      }
    ],
    "errors": 0
  },
  "artifacts": [
    "job.json",
    "document_0/document_0.md",
    "document_0/imgs/cropped_page13_idx0.jpg",
    "figure_texts/cropped_page13_idx0.md"
  ],
  "result_url": "/results/cd33c76a-839d-4958-ab85-e16d80ecb72e"
}
```

`kind` 常见值：`flowchart`、`caption`、`structured`、`table`、`text`。

---

## 4. 查询结果 / 下载产物

```http
GET /results/{job_id}
GET /results/{job_id}/{artifact_path}
```

```bash
# 元数据
curl -s '{BASE_URL}/results/<job_id>' | python -m json.tool

# 下载单张图转出的 Mermaid
curl -OJ \
  '{BASE_URL}/results/<job_id>/figure_texts/cropped_page13_idx0.md'

# 下载主 Markdown
curl -OJ \
  '{BASE_URL}/results/<job_id>/document_0/document_0.md'
```

典型产物目录：

```text
<figure_job>/
├── job.json
├── figure_texts/          # Qwen 生成的 Mermaid / Caption
│   ├── cropped_*.md
│   └── summary.json
└── <文档名>/
    ├── <文档名>.md        # 图片引用已被文本替换
    ├── imgs/              # Layout 裁剪原图
    └── layout_vis/
```

---

## 5. OSS 异步提交（可选）

```http
POST /parse_oss
Content-Type: application/json
```

```bash
curl -X POST '{BASE_URL}/parse_oss' \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "patient_001_20260903120000",
    "oss_path": "docs/medical/report.pdf",
    "image_mode": "auto",
    "enrich_figures": true,
    "attempt": 1
  }'
```

返回 HTTP `202` 后，用 `GET /results/{job_id}` 轮询。OSS 凭据由服务端环境注入，调用方不传密钥。

---

## 6. Python 示例

```python
from pathlib import Path
import requests

BASE_URL = "http://172.19.64.72/glm-ocr-v3"
PDF = Path("/path/to/document.pdf")

with PDF.open("rb") as f:
    r = requests.post(
        f"{BASE_URL}/parse",
        params={"enrich_figures": "true"},
        files={"file": (PDF.name, f, "application/pdf")},
        timeout=3600,
    )
r.raise_for_status()
data = r.json()
print(data["job_id"], data["chars"], data.get("figure_enrichment"))
Path("result.md").write_text(data["text"], encoding="utf-8")
```

---

## 7. 错误码

| HTTP | 含义 |
|---:|---|
| 400 | 参数 / 文件类型错误（如 PDF + `model_only`） |
| 404 | `job_id` 或产物不存在 |
| 429 | 队列满，看 `Retry-After` |
| 500 | Layout / OCR / Qwen 图转失败 |
| 503 | 服务未就绪 |

---

## 8. 调用建议

1. 整页病例 / 指南 PDF：`enrich_figures=true`（默认）。  
2. 已裁好的小图：`image_mode=model_only`。  
3. 大 PDF 可能数分钟；客户端超时建议 ≥ 30–60 分钟。  
4. 需要原流程图裁剪时下载 `imgs/`；需要可编辑图时看 `figure_texts/` 或 `text` 里的 Mermaid。
