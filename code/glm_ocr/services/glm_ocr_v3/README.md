# GLM-OCR v3 接口文档

GLM-OCR v3 在 [v2](../glm_ocr_v2/README.md) 队列与解析能力之上，增加 **版面裁剪图二次 VL 文本化**：

- Layout 识别出的 `imgs/`（`image` / `chart` 区域）会再调用一次 VL；
- 流程图等可结构化内容 → 可编辑文本（默认 Mermaid flowchart 等）；
- 无法可靠转成结构化文本的内容 → **Image Caption**；
- 转换结果写回 Markdown，并单独落盘到 `figure_texts/`。

提供给外部调用方的精简说明见 [API_USAGE.md](API_USAGE.md)。

## 服务地址

默认监听地址：

```text
http://127.0.0.1:18093
```

可与 v2（`18091`）和 task-queue（`18092`）并行部署，互不影响。

## 与 v2 的差异

| 项 | v2 | v3 |
|---|---|---|
| 默认端口 | `18091` | `18093`（勿占用 task-queue 的 `18092`） |
| 输出目录 | `result/glm_ocr/framework` | `result/glm_ocr/framework_v3` |
| Layout 后处理 | 无 | `imgs/` → VL 文本 / Caption |
| 请求参数 | `image_mode` | 另增 `enrich_figures`（默认 `true`） |

队列分流、文件类型、OSS 异步接口与 v2 相同。

## 处理流程

1. 按 v2 规则进入 `pdf_layout` / `image_layout` / `model_only` 队列；
2. Layout 任务：PP-DocLayoutV3 + GLM-OCR，保存 Markdown / `imgs/` / `layout_vis/`；
3. 若 `enrich_figures=true`（且为 layout 模式）：对每个 `imgs/*` 再调 VL；
4. 用转换文本替换 Markdown 中的图片引用；未引用的图追加到 `## Converted Figures`；
5. 返回完整 `text` 与产物列表。

`model_only` 不走 Layout，因此不会触发 figure enrichment。

## 1. 健康检查

```bash
curl -s http://127.0.0.1:18093/health | python -m json.tool
```

响应中额外包含：

```json
{
  "service": "glm-ocr-queued-parse-v3",
  "figure_enrichment": {
    "enabled": true,
    "max_images": 64,
    "max_workers": 2
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

```bash
curl -X POST \
  'http://127.0.0.1:18093/parse?image_mode=layout&enrich_figures=true' \
  -F 'file=@/path/to/document.pdf'
```

关闭二次转换（行为接近 v2）：

```bash
curl -X POST \
  'http://127.0.0.1:18093/parse?enrich_figures=false' \
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

## VL 转换策略（默认 prompt）

| 区域类型 | 输出形式 |
|---|---|
| 流程图 / 决策树 / 路径图 | ` ```mermaid ` flowchart |
| 可读数值的图表 | Markdown 表格 + 一句摘要 |
| 类表格区域 | Markdown / HTML 表格 |
| 结构示意图 | Mermaid 或嵌套列表 |
| 照片 / Logo / 无法结构化 | `**Image Caption:** ...` |

可在 `ocr_services_v3.yaml` 的 `figure_enrichment.prompt` 覆盖默认提示词。

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
| `service.port` | `18093` | 服务端口 |
| `service.output_root` | `result/glm_ocr/framework_v3` | UUID 输出根目录 |
| `figure_enrichment.enabled` | `true` | 全局开关 |
| `figure_enrichment.max_images` | `64` | 单任务最多转换图数 |
| `figure_enrichment.max_workers` | `2` | 单任务内并行 VL 请求数 |
| `figure_enrichment.timeout` | `300` | 单图 VL 超时（秒） |
| `figure_enrichment.max_tokens` | `4096` | 单图生成上限 |
| `figure_enrichment.prompt` | （内置） | 自定义 VL 提示词 |

其余队列 / vLLM / Layout 配置与 v2 含义相同。

`/parse_oss` 凭据仍从仓库根目录 `.env` 注入，不要写入 `config.env`。
