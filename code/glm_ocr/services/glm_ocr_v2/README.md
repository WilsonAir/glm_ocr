# GLM-OCR v2 接口文档

GLM-OCR v2 提供 PDF、整页病例图片和局部裁剪图片的 OCR 接口。服务使用
UUID 隔离每次请求的输出，并根据文件类型和 `image_mode` 将任务分配到不同
队列。

提供给外部调用方的精简说明见 [API_USAGE.md](API_USAGE.md)。

## 服务地址

默认监听地址：

```text
http://127.0.0.1:18091
```

所有示例均以该地址为准。

## 处理流程与队列

服务包含三个独立的有界队列：

| 队列 | 适用输入 | 等待容量 | Worker 数量 | 处理方式 |
|---|---|---:|---:|---|
| `pdf_layout` | PDF | 4 | 2 | PP-DocLayoutV3 + GLM-OCR |
| `image_layout` | 整页病例图片 | 8 | 1 | PP-DocLayoutV3 + GLM-OCR |
| `model_only` | 局部裁剪图、普通截图 | 8 | 4 | 直接调用 vLLM |

PDF 和整页图片分别使用独立、延迟初始化的 SDK Parser，因此大 PDF 不会在
Python Parser 层阻塞整页病例图片。两个 Parser 默认都使用 `cuda:1`，会比
共享单个 Parser 占用更多 Layout 显存。

队列容量指等待中的任务数量，不包含正在由 Worker 执行的任务。队列已满时，
服务返回 HTTP `429`，并附带：

```http
Retry-After: 10
```

## 文件分流规则

统一入口为 `POST /parse`：

| 输入 | `image_mode` | 进入队列 |
|---|---|---|
| PDF | `auto` 或 `layout` | `pdf_layout` |
| PDF | `model_only` | 拒绝，返回 400 |
| 图片 | `auto` | `image_layout` |
| 图片 | `layout` | `image_layout` |
| 图片 | `model_only` | `model_only` |

医疗场景下无法可靠地仅凭图片尺寸判断是否为整页病例，所以图片的 `auto`
模式默认执行 Layout。已经裁剪好的诊断结果、检验区域或普通截图，应由上游
明确传入 `image_mode=model_only`。

支持的文件类型：

```text
.pdf
.jpg
.jpeg
.png
.bmp
.gif
.webp
```

## 1. 健康检查

```http
GET /health
```

调用示例：

```bash
curl -s http://127.0.0.1:18091/health | python -m json.tool
```

响应示例：

```json
{
  "status": "ok",
  "model": "glm-ocr",
  "service": "glm-ocr-queued-parse-v2",
  "output_root": "/data/wilson_2/de/glm_ocr/result/glm_ocr/framework",
  "queues": {
    "pdf_layout": {
      "capacity": 4,
      "workers": 2,
      "waiting": 0
    },
    "image_layout": {
      "capacity": 8,
      "workers": 1,
      "waiting": 0
    },
    "model_only": {
      "capacity": 8,
      "workers": 4,
      "waiting": 0
    }
  }
}
```

`waiting` 只表示当前等待队列深度，不包含正在执行的任务。

## 2. 统一解析接口

```http
POST /parse
Content-Type: multipart/form-data
```

请求参数：

| 参数 | 位置 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `file` | multipart 表单 | 是 | — | PDF 或图片文件 |
| `image_mode` | Query | 否 | `auto` | `auto`、`layout` 或 `model_only` |

### 解析 PDF

```bash
curl -X POST http://127.0.0.1:18091/parse \
  -F 'file=@/path/to/document.pdf'
```

PDF 始终进入 `pdf_layout` 队列。

### 解析整页病例图片

```bash
curl -X POST \
  'http://127.0.0.1:18091/parse?image_mode=layout' \
  -F 'file=@/path/to/medical-page.jpg'
```

省略 `image_mode` 或传入 `auto` 时效果相同：

```bash
curl -X POST http://127.0.0.1:18091/parse \
  -F 'file=@/path/to/medical-page.jpg'
```

### 解析局部裁剪图片

```bash
curl -X POST \
  'http://127.0.0.1:18091/parse?image_mode=model_only' \
  -F 'file=@/path/to/diagnosis-crop.jpg'
```

该模式不执行 Layout，直接请求 vLLM，因此不会等待 PDF 或整页图片的 Layout
Parser。

### 成功响应

`/parse` 是同步接口。请求进入队列后，HTTP 连接会一直等待，直到解析和结果
保存完成。

响应示例：

```json
{
  "job_id": "b59254c0-9f42-4343-a102-4ac04a66c56b",
  "status": "completed",
  "queue": "pdf_layout",
  "filename": "document.pdf",
  "content_type": "pdf",
  "image_mode": "auto",
  "created_at": "2026-07-31T10:34:58+08:00",
  "started_at": "2026-07-31T10:34:58+08:00",
  "finished_at": "2026-07-31T10:37:00+08:00",
  "output_dir": "/data/wilson_2/de/glm_ocr/result/glm_ocr/framework/b59254c0-9f42-4343-a102-4ac04a66c56b",
  "chars": 12345,
  "elapsed_sec": 120.5,
  "text": "# Markdown 识别结果",
  "artifacts": [
    "job.json",
    "document/document.md",
    "document/document.json",
    "document/imgs/image_0.jpg",
    "document/layout_vis/document_page0.jpg"
  ],
  "result_url": "/results/b59254c0-9f42-4343-a102-4ac04a66c56b"
}
```

具体产物名称由已安装的官方 GLM-OCR SDK 版本和文档内容决定。

`created_at`、`started_at` 和 `finished_at` 均使用 ISO 8601 格式的北京时间，
时区偏移固定显示为 `+08:00`。`elapsed_sec` 为任务实际执行耗时，不包含排队
时间。

## 3. 图片兼容接口

```http
POST /parse_image
Content-Type: multipart/form-data
```

参数与 `/parse` 相同，但只接受图片，不接受 PDF：

```bash
curl -X POST \
  'http://127.0.0.1:18091/parse_image?image_mode=model_only' \
  -F 'file=@/path/to/crop.png'
```

推荐新接入统一使用 `/parse`；`/parse_image` 主要用于兼容和显式校验。

## 4. 查询任务结果

```http
GET /results/{job_id}
```

调用示例：

```bash
curl -s \
  http://127.0.0.1:18091/results/b59254c0-9f42-4343-a102-4ac04a66c56b \
  | python -m json.tool
```

`status` 可能为：

| 状态 | 含义 |
|---|---|
| `queued` | 已进入队列，等待 Worker |
| `running` | 正在解析或保存结果 |
| `completed` | 解析和保存完成 |
| `failed` | 解析失败，`job.json` 中包含 `error` |
| `rejected` | 入队时发生竞争，队列已满 |

## 5. 下载结果文件

```http
GET /results/{job_id}/{artifact_path}
```

先通过任务查询接口取得 `artifacts`，再下载指定文件：

```bash
curl -OJ \
  http://127.0.0.1:18091/results/<job_id>/<artifact_path>
```

例如：

```bash
curl -OJ \
  http://127.0.0.1:18091/results/<job_id>/document/imgs/image_0.jpg
```

## 输出目录

每次请求使用独立 UUID 目录：

```text
/data/wilson_2/de/glm_ocr/result/glm_ocr/framework/<uuid>/
```

典型 Layout 输出：

```text
<uuid>/
├── job.json
└── <文档名>/
    ├── <文档名>.md
    ├── <文档名>.json
    ├── <文档名>_model.json
    ├── imgs/
    └── layout_vis/
```

典型 `model_only` 输出：

```text
<uuid>/
├── job.json
├── result.md
└── result.json
```

`imgs/` 保存 Layout 判断为 `image` 或 `chart` 的区域裁剪，不代表 OCR 失败图片。

## 错误码

| HTTP 状态码 | 说明 |
|---:|---|
| 400 | 文件为空、类型不支持、PDF 使用了 `model_only` 等参数错误 |
| 404 | UUID 不存在或产物路径不存在 |
| 429 | 对应队列已满，建议等待 `Retry-After` 后重试 |
| 500 | Layout、vLLM、结果格式化或保存失败 |
| 503 | 服务启动期间队列尚未初始化 |

客户端超时或断开后，已经开始的后台线程不一定会立即停止。上游应使用幂等键
或保存返回的 UUID，避免无条件重试产生重复任务。

## 启动与管理

进入项目目录：

```bash
cd /data/wilson_2/de/glm_ocr/code/glm_ocr
```

后台启动：

```bash
deploy/glm-ocr-v2/start.sh
```

前台启动、状态检查和停止：

```bash
deploy/glm-ocr-v2/start.sh --foreground
deploy/glm-ocr-v2/start.sh --status
deploy/glm-ocr-v2/start.sh --stop
```

日志文件：

```text
/data/wilson_2/de/glm_ocr/code/glm_ocr/logs/glm_ocr_v2_service.log
```

## 配置管理

可复制示例配置：

```bash
cp deploy/glm-ocr-v2/config.env.example \
   deploy/glm-ocr-v2/config.env
```

业务配置统一维护在
`code/glm_ocr/config/ocr_services_v2.yaml`，主要配置项：

| YAML 配置 | 默认值 | 说明 |
|---|---|---|
| `service.host` | `127.0.0.1` | 服务监听地址 |
| `service.port` | `18091` | 服务端口 |
| `service.layout_device` | `cuda:1` | Layout Parser 使用的设备 |
| `service.output_root` | `result/glm_ocr/framework` | UUID 输出根目录 |
| `queues.pdf_layout.capacity` | `4` | PDF 等待队列容量 |
| `queues.pdf_layout.workers` | `2` | PDF Worker 数量，每个 Worker 独立 Parser |
| `queues.image_layout.capacity` | `8` | 整页图片等待队列容量 |
| `queues.image_layout.workers` | `1` | 整页图片 Worker 数量 |
| `queues.model_only.capacity` | `8` | model-only 图片等待容量 |
| `queues.model_only.workers` | `4` | model-only Worker 数量 |
| `vllm.url` | `http://127.0.0.1:18080/v1/chat/completions` | vLLM 接口 |
| `vllm.model` | `glm-ocr` | vLLM 模型名 |
| `vllm.timeout` | `300` | vLLM 请求超时，单位秒 |
| `PPU_RTC_CACHE_DIR` | `/data/wilson_2/cache/rtccache` | PPU RTC 实际缓存目录 |

队列 Worker 数量调高后，可能增加 Layout 显存、vLLM 并发和主机内存压力，修改
前应先进行并发压测。

长期配置统一维护在：

```text
code/glm_ocr/config/ocr_services_v2.yaml
```

启动脚本会将该文件传给 v2 服务。`config.env` 只保留 Python、PPU SDK 和
RTC 缓存等启动前必须确定的基础环境配置。
