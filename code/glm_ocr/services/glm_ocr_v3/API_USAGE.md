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
| 标题 / 段落翻译 | `llm`（`qwen3.5-9b` / `hy-mt2-18` 等，见 YAML `translation.models`）/ `google` / `baidu` |

开关：

| 参数 | 默认 | 说明 |
|---|---|---|
| `enrich_figures` | `true` | Layout 裁出的 `imgs/` 再调 Qwen → Mermaid / Caption |
| `translate` | `false` | 对段落标题与段落做中文翻译；**不译** Mermaid / 代码块 |
| `ocr_engine` | YAML `ocr.default_engine`（默认 `glm`） | `glm` = 本机 GlmOcr；`paddle` = HTTP 转发到 PaddleOCR 服务（`:18093`） |
| `translate_provider` | `llm` | `llm` / `google` / `baidu` |
| `translate_model` | YAML `default_llm` | 仅 `provider=llm`：选 `translation.models` 下的键 |
| `ocr_prompt_profile` | YAML `ocr.default_prompt_profile` | 仅 `image_mode=model_only`：OCR prompt 配置键 |
| `google_api_key` | — | Form/JSON；`provider=google` 时必填，**不落盘** |
| `baidu_app_id` | — | Form/JSON；百度 APP ID |
| `baidu_secret_key` | — | Form/JSON；百度密钥 |
| `baidu_api_key` | — | 可选，格式 `appid:secret`（与上两字段二选一） |

翻译内部默认 **合并同类段**：标题整批拼接；**小段落**（默认 ≤1200 字）也会拼批，大段单独译；
用 `<<<OCR_SEG_ID_n>>>` 拼包，译完再拆回。拆包失败的段回退逐段翻译。
批大小受 `merge_max_chars` 与 `max_tokens` 共同约束。  
配置：`merge_segments` / `merge_max_chars` / `merge_small_para_chars` / `max_tokens`。

本地 LLM 翻译模型在 [`ocr_services_v3.yaml`](../../config/ocr_services_v3.yaml) 的 `translation.models` 配置；
每个模型可独立设 `vllm_url` / `vllm_model` / 采样参数 / `prompt_profile`（与 figure Qwen **解耦**）。

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
  },
  "translation": {
    "enabled": true,
    "default_request": false,
    "default_provider": "llm",
    "default_llm": "qwen3.5-9b",
    "providers": ["llm", "google", "baidu"],
    "models": {
      "qwen3.5-9b": {
        "vllm_url": "http://127.0.0.1:18003/v1/chat/completions",
        "vllm_model": "qwen3.5-9b"
      },
      "hy-mt2-18": {
        "vllm_url": "http://127.0.0.1:18004/v1/chat/completions",
        "vllm_model": "hy-mt2-18",
        "prompt_profile": "hy_mt_en2zh"
      }
    },
    "target_language": "Simplified Chinese"
  },
  "ocr": {
    "default_prompt_profile": "en",
    "prompt_profiles": ["en"]
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
| `ocr_engine` | Query | 否 | `glm` | `glm` / `paddle`；`paddle` 需本机 PaddleOCR 服务已启动 |
| `enrich_figures` | Query | 否 | `true` | 是否对 `imgs/` 做 Qwen→Mermaid |
| `translate` | Query | 否 | `false` | 是否翻译段落标题 / 段落 |
| `translate_provider` | Query | 否 | `llm` | `llm` / `google` / `baidu` |
| `translate_model` | Query | 否 | YAML `default_llm` | `llm` 时选 `translation.models` 键，如 `qwen3.5-9b` / `hy-mt2-18` |
| `ocr_prompt_profile` | Query | 否 | YAML `ocr.default_prompt_profile` | 仅 `model_only`；默认 `en`（`Text Recognition:`） |
| `google_api_key` | Form | 条件 | — | `translate_provider=google` 时必填 |
| `baidu_app_id` | Form | 条件 | — | `baidu` 时与 secret 一起必填 |
| `baidu_secret_key` | Form | 条件 | — | 百度密钥 |
| `baidu_api_key` | Form | 条件 | — | 可选 `appid:secret` 合一传入 |

`image_mode`：

| 值 | 说明 |
|---|---|
| `auto` | 图片默认 Layout |
| `layout` | 强制 Layout |
| `model_only` | 图片直送 glm-ocr，不做 Layout，也不做 Mermaid / 翻译 |

PDF 不能使用 `model_only`。

### 解析 PDF（默认开 Mermaid，关翻译）

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true' \
  -F 'file=@/path/to/document.pdf'
```

### 用 PaddleOCR 做正文/版面解析（仍走 v3 接口）

需先启动 Paddle 服务（`:18093`）。`translate` 仍可在 v3 侧执行；`enrich_figures` 依赖 `imgs/`，Paddle 路径通常无裁剪图，可能为空操作。

```bash
curl -X POST \
  '{BASE_URL}/parse?ocr_engine=paddle&enrich_figures=false&translate=true&translate_provider=llm&translate_model=hy-mt2-18' \
  -F 'file=@/path/to/document.pdf'
```

### 用 Qwen 翻译正文（默认 `translate_model=qwen3.5-9b`）

医学长 prompt + `temperature=0` + 关闭 thinking；走 `:18003`。

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true&translate=true&translate_provider=llm&translate_model=qwen3.5-9b' \
  -F 'file=@/path/to/document.pdf'
```

### 用 Hy-MT2 本地翻译（英→中短 prompt）

独立模型 `:18004`，官方风格短指令，采样 `temperature=0.7 top_p=0.6 top_k=20 repetition_penalty=1.05`。

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true&translate=true&translate_provider=llm&translate_model=hy-mt2-18' \
  -F 'file=@/path/to/document.pdf'
```

### 用 Google 翻译（Key 即时传入）

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true&translate=true&translate_provider=google' \
  -F 'file=@/path/to/document.pdf' \
  -F 'google_api_key=YOUR_GOOGLE_API_KEY'
```

官方接口为 Cloud Translation **Basic v2**（`translation.googleapis.com/language/translate/v2`），
目标语言默认 `zh-CN`。Key 只在内存中使用，**不会写入** `job.json`。

### 用百度翻译（APP ID + 密钥即时传入）

```bash
curl -X POST \
  '{BASE_URL}/parse?enrich_figures=true&translate=true&translate_provider=baidu' \
  -F 'file=@/path/to/document.pdf' \
  -F 'baidu_app_id=YOUR_APP_ID' \
  -F 'baidu_secret_key=YOUR_SECRET_KEY'
```

也可合一传入：

```bash
  -F 'baidu_api_key=YOUR_APP_ID:YOUR_SECRET_KEY'
```

接口为百度翻译开放平台通用翻译 API（`fanyi-api.baidu.com/api/trans/vip/translate`），
目标语言默认 `zh`。凭证不落盘。

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

### 局部裁剪图（不做 Layout，英文直出 OCR）

默认 `ocr_prompt_profile=en` → prompt `Text Recognition:`。

```bash
curl -X POST \
  '{BASE_URL}/parse?image_mode=model_only&ocr_prompt_profile=en' \
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
    "translate": false,
    "translate_provider": "llm",
    "google_api_key": null,
    "baidu_app_id": null,
    "baidu_secret_key": null,
    "baidu_api_key": null,
    "attempt": 1
  }'
```

`translate_provider=google` 时在 JSON 里带上 `google_api_key`；
`baidu` 时带上 `baidu_app_id` + `baidu_secret_key`（或 `baidu_api_key`），同样不落盘。

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
        params={
            "enrich_figures": "true",
            "translate": "true",
            "translate_provider": "google",
        },
        files={"file": (PDF.name, f, "application/pdf")},
        data={"google_api_key": "YOUR_GOOGLE_API_KEY"},
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
2. 需要中文正文：`translate=true`；默认 `translate_provider=llm`，或 `google` / `baidu` + 对应凭证。  
3. 已裁好的小图：`image_mode=model_only`。  
4. 大 PDF 可能数分钟；开翻译会再增加调用时间，客户端超时建议 ≥ 30–60 分钟。  
5. 需要原流程图裁剪时下载 `imgs/`；需要可编辑图时看 `figure_texts/` 或 `text` 里的 Mermaid；译文另见 `translations/`。
