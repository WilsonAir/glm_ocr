# GLM-OCR v3 接口文档

GLM-OCR v3 在 [v2](../glm_ocr_v2/README.md) 队列与解析能力之上，增加：

- Layout 识别出的 `imgs/`（`image` / `chart` 区域）再调用 VL → Mermaid / Caption（`enrich_figures`，**默认开**）；
- 段落标题与段落可选翻译（`translate`，**默认关**）；后端可选 `llm` / `google` / `baidu`（凭证请求时传入）；
- 转换 / 翻译结果写回 Markdown，并分别落盘到 `figure_texts/`、`translations/`。

提供给外部调用方的接口说明见 [API_USAGE.md](API_USAGE.md)（含网关地址与 curl / Python 示例）。

## 服务地址

默认监听地址：

```text
http://127.0.0.1:18094
```

可与 v2（`18091`）、task-queue（`18092`）、paddle-ocr（`18093`）并行部署。

## 与 v2 的差异

| 项 | v2 | v3 |
|---|---|---|
| 默认端口 | `18091` | `18094`（避开 task-queue `18092`、paddle `18093`） |
| 输出目录 | `result/glm_ocr/framework` | `result/glm_ocr/framework_v3` |
| Layout 后处理 | 无 | `imgs/` → VL 文本 / Caption |
| 请求参数 | `image_mode` | 另增 `enrich_figures`（默认 `true`）、`translate`（默认 `false`）、`translate_provider`、`google_api_key` |

队列分流、文件类型、OSS 异步接口与 v2 相同。

## 处理流程

1. 按 v2 规则进入 `pdf_layout` / `image_layout` / `model_only` 队列；
2. Layout 任务：PP-DocLayoutV3 + GLM-OCR，保存 Markdown / `imgs/` / `layout_vis/`；
3. 若 `enrich_figures=true`（且为 layout 模式）：对每个 `imgs/*` 再调 VL；
4. 用转换文本替换 Markdown 中的图片引用；未引用的图追加到 `## Converted Figures`；
5. 若 `translate=true`：对段落标题 / 段落翻译（`llm`/`google`/`baidu`；跳过 Mermaid / 代码块）；
6. 返回完整 `text` 与产物列表。

`model_only` 不走 Layout，因此不会触发 figure enrichment / translation。

## 1. 健康检查

```bash
curl -s http://127.0.0.1:18094/health | python -m json.tool
```

响应中额外包含：

```json
{
  "service": "glm-ocr-queued-parse-v3",
  "figure_enrichment": {
    "enabled": true,
    "max_images": 64,
    "max_workers": 2
  },
  "translation": {
    "enabled": true,
    "default_request": false,
    "default_provider": "llm",
    "providers": ["llm", "google", "baidu"],
    "target_language": "Simplified Chinese"
  }
}
```

## 2. 统一解析接口

```http
POST /parse
Content-Type: multipart/form-data
```

| 参数 | 位置 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `file` | Form | 是 | — | PDF 或图片 |
| `image_mode` | Query | 否 | `auto` | `auto` / `layout` / `model_only` |
| `enrich_figures` | Query | 否 | `true` | Layout 任务是否对 `imgs/` 做二次 VL |
| `translate` | Query | 否 | `false` | Layout 任务是否翻译段落标题 / 段落 |
| `translate_provider` | Query | 否 | `llm` | `llm` / `google` / `baidu` |
| `google_api_key` | Form | 条件 | — | `provider=google` 时必填（不落盘） |
| `baidu_app_id` / `baidu_secret_key` | Form | 条件 | — | `provider=baidu` 时必填 |
| `baidu_api_key` | Form | 条件 | — | 可选 `appid:secret` |

```bash
curl -X POST \
  'http://127.0.0.1:18094/parse?image_mode=layout&enrich_figures=true' \
  -F 'file=@/path/to/document.pdf'
```

用 Qwen 翻译：

```bash
curl -X POST \
  'http://127.0.0.1:18094/parse?enrich_figures=true&translate=true&translate_provider=llm' \
  -F 'file=@/path/to/document.pdf'
```

用 Google 翻译（Key 即时传入）：

```bash
curl -X POST \
  'http://127.0.0.1:18094/parse?enrich_figures=true&translate=true&translate_provider=google' \
  -F 'file=@/path/to/document.pdf' \
  -F 'google_api_key=YOUR_GOOGLE_API_KEY'
```

用百度翻译：

```bash
curl -X POST \
  'http://127.0.0.1:18094/parse?enrich_figures=true&translate=true&translate_provider=baidu' \
  -F 'file=@/path/to/document.pdf' \
  -F 'baidu_app_id=YOUR_APP_ID' \
  -F 'baidu_secret_key=YOUR_SECRET_KEY'
```

关闭二次转换（行为接近 v2）：

```bash
curl -X POST \
  'http://127.0.0.1:18094/parse?enrich_figures=false' \
  -F 'file=@/path/to/document.pdf'
```

响应在 v2 字段基础上增加：

```json
{
  "enrich_figures": true,
  "figure_enrichment": {
    "enabled": true,
    "image_count": 2,
    "converted": [
      {
        "path": "document/imgs/image_0.jpg",
        "kind": "flowchart",
        "chars": 512,
        "error": null
      }
    ],
    "errors": 0,
    "replaced_in_markdown": ["document/imgs/image_0.jpg"]
  },
  "text": "...含 Mermaid / Caption 的 Markdown..."
}
```

`kind` 可能为：`flowchart`、`table`、`structured`、`text`、`caption`。

## 输出目录

```text
result/glm_ocr/framework_v3/<uuid>/
├── job.json
├── figure_texts/
│   ├── image_0.md
│   ├── image_1.md
│   └── summary.json
└── <文档名>/
    ├── <文档名>.md          # 图片引用已被文本替换
    ├── <文档名>.json
    ├── imgs/
    └── layout_vis/
```

## VL 转换策略

Layout 裁剪出的 `imgs/` 默认交给 **Qwen3.5-9B**（`figure_enrichment.vllm_*`），
用 `mermaid_clinical` prompt 生成 Mermaid flowchart。正文 OCR / Layout 仍使用
**glm-ocr**（`vllm.*`）。

| 区域类型 | 输出形式 |
|---|---|
| 流程图 / 决策树 / 临床 pathway / 框线箭头图 | ` ```mermaid ` flowchart（优先 LR + subgraph） |
| 非流程图 | `**Image Caption:** ...`（由同一 clinical prompt 兜底） |

配置示例（`ocr_services_v3.yaml`）：

```yaml
figure_enrichment:
  prompt_profile: mermaid_clinical
  fallback_prompt_profile: none
  vllm_url: http://127.0.0.1:18003/v1
  vllm_model: qwen3.5-9b
```

## 启动与管理

```bash
cd /data/wilson_2/de/glm_ocr/code/glm_ocr

deploy/glm-ocr-v3/start.sh
deploy/glm-ocr-v3/start.sh --foreground
deploy/glm-ocr-v3/start.sh --status
deploy/glm-ocr-v3/start.sh --stop
```

日志：

```text
code/glm_ocr/logs/glm_ocr_v3_service.log
```

## 配置管理

业务配置：`code/glm_ocr/config/ocr_services_v3.yaml`

| YAML 配置 | 默认值 | 说明 |
|---|---|---|
| `service.port` | `18094` | 服务端口 |
| `service.output_root` | `result/glm_ocr/framework_v3` | UUID 输出根目录 |
| `figure_enrichment.enabled` | `true` | 全局开关 |
| `figure_enrichment.vllm_url` | `http://127.0.0.1:18003/v1` | flowchart→Mermaid 专用 VL（本机 Qwen） |
| `figure_enrichment.vllm_model` | `qwen3.5-9b` | flowchart VL 模型名 |
| `figure_enrichment.prompt_profile` | `mermaid_clinical` | 主 prompt |
| `figure_enrichment.fallback_prompt_profile` | `none` | 备选；`none` 关闭重试 |
| `figure_enrichment.max_images` | `64` | 单任务最多转换图数 |
| `figure_enrichment.max_workers` | `2` | 单任务内并行 VL 请求数 |
| `figure_enrichment.timeout` | `600` | 单图 VL 超时（秒） |
| `figure_enrichment.max_tokens` | `8192` | 单图生成上限 |
| `figure_enrichment.prompt` | （空） | 非空时覆盖 `prompt_profile` |
| `figure_enrichment.fallback_prompt` | （空） | 非空时覆盖 `fallback_prompt_profile` |
其余队列 / vLLM / Layout 配置与 v2 含义相同。

`/parse_oss` 凭据仍从仓库根目录 `.env` 注入，不要写入 `config.env`。
