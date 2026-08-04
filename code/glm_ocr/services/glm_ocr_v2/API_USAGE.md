# GLM-OCR 文档解析接口调用说明

本文档面向 GLM-OCR 接口调用方，介绍 PDF、病例图片和局部裁剪图片的提交、
结果查询及文件下载方法。支持通过文件上传或 OSS 路径两种方式提交解析任务。

## 接口地址

请向服务管理员获取实际地址，并将下文的 `{BASE_URL}` 替换为该地址：

```text
{BASE_URL}
```

例如：

```text
http://example-host:18091
```

## 支持的文件类型

| 类型 | 文件后缀 |
|---|---|
| PDF | `.pdf` |
| 图片 | `.jpg`、`.jpeg`、`.png`、`.bmp`、`.gif`、`.webp` |

上传文件不能为空。文件应使用与实际内容一致的后缀。

## 1. 服务状态检查

```http
GET /health
```

调用示例：

```bash
curl -s '{BASE_URL}/health'
```

服务正常时，响应中的 `status` 为 `ok`：

```json
{
  "status": "ok",
  "model": "glm-ocr",
  "service": "glm-ocr-queued-parse-v2"
}
```

## 2. 查询队列占用情况

```http
GET /queue_status
```

调用示例：

```bash
curl -s '{BASE_URL}/queue_status'
```

响应示例：

```json
{
  "status": "ok",
  "timestamp": "2026-08-04T16:10:00+08:00",
  "total_waiting": 1,
  "total_capacity": 20,
  "queues": {
    "pdf_layout": {
      "capacity": 4,
      "workers": 2,
      "waiting": 1,
      "available_slots": 3,
      "usage_percent": 25.0,
      "is_full": false
    },
    "image_layout": {
      "capacity": 8,
      "workers": 1,
      "waiting": 0,
      "available_slots": 8,
      "usage_percent": 0.0,
      "is_full": false
    },
    "model_only": {
      "capacity": 8,
      "workers": 4,
      "waiting": 0,
      "available_slots": 8,
      "usage_percent": 0.0,
      "is_full": false
    }
  }
}
```

响应字段：

| 字段 | 说明 |
|---|---|
| `total_waiting` | 所有队列的等待任务总数 |
| `total_capacity` | 所有队列的总容量 |
| `queues.{name}.capacity` | 队列最大容量 |
| `queues.{name}.workers` | 该队列的并发 Worker 数 |
| `queues.{name}.waiting` | 当前等待中的任务数 |
| `queues.{name}.available_slots` | 剩余可用槽位 |
| `queues.{name}.usage_percent` | 队列使用率（百分比） |
| `queues.{name}.is_full` | 队列是否已满 |

当 `is_full` 为 `true` 时，提交新任务将返回 HTTP 429 错误。

## 3. 提交解析任务

统一使用：

```http
POST /parse
Content-Type: multipart/form-data
```

请求参数：

| 参数 | 位置 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `file` | multipart 表单 | 是 | — | PDF 或图片文件 |
| `image_mode` | Query | 否 | `auto` | 图片处理模式 |

`image_mode` 可选值：

| 值 | 说明 | 推荐场景 |
|---|---|---|
| `auto` | 图片默认执行版面分析 | 整页病例、扫描件 |
| `layout` | 强制执行版面分析 | 复杂排版、多栏、表格 |
| `model_only` | 图片直接 OCR，不执行版面分析 | 局部裁剪图、普通截图 |

PDF 必须使用 `auto` 或 `layout`，不支持 `model_only`。

### 解析 PDF

```bash
curl -X POST '{BASE_URL}/parse' \
  -F 'file=@/path/to/document.pdf'
```

### 解析整页病例图片

```bash
curl -X POST \
  '{BASE_URL}/parse?image_mode=layout' \
  -F 'file=@/path/to/medical-page.jpg'
```

也可以省略 `image_mode`：

```bash
curl -X POST '{BASE_URL}/parse' \
  -F 'file=@/path/to/medical-page.jpg'
```

### 解析局部裁剪图片

```bash
curl -X POST \
  '{BASE_URL}/parse?image_mode=model_only' \
  -F 'file=@/path/to/diagnosis-crop.jpg'
```

局部裁剪图使用 `model_only` 延迟更低。

## 3. 通过 OSS 路径提交解析任务

对于已存储在阿里云 OSS 中的文件，可以使用 `/parse_oss` 接口，无需上传文件，
只需提供 OSS 对象路径即可。服务会自动从 OSS 下载文件、执行解析，并将结果上传
回 OSS。

```http
POST /parse_oss
Content-Type: application/json
```

请求参数（JSON Body）：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---|---|
| `task_id` | string | 是 | — | 业务侧自定义的任务标识 |
| `oss_path` | string | 是 | — | OSS 对象路径（bucket 内的 object key） |
| `image_mode` | string | 否 | `auto` | 图片处理模式，可选值同 `/parse` |

请求示例：

```bash
curl -X POST '{BASE_URL}/parse_oss' \
  -H 'Content-Type: application/json' \
  -d '{
    "task_id": "order-12345",
    "oss_path": "docs/medical/report.pdf",
    "image_mode": "auto"
  }'
```

成功响应示例：

```json
{
  "job_id": "b59254c0-9f42-4343-a102-4ac04a66c56b",
  "task_id": "order-12345",
  "status": "completed",
  "queue": "pdf_layout",
  "filename": "report.pdf",
  "content_type": "pdf",
  "image_mode": "auto",
  "oss_source": "docs/medical/report.pdf",
  "created_at": "2026-08-04T15:30:00+08:00",
  "started_at": "2026-08-04T15:30:05+08:00",
  "finished_at": "2026-08-04T15:32:10+08:00",
  "chars": 12345,
  "elapsed_sec": 125.0,
  "text": "# Markdown 识别结果",
  "artifacts": [
    "job.json",
    "result.md",
    "result.json"
  ],
  "oss_artifacts": [
    {
      "path": "result.md",
      "oss_key": "glm_ocr_output/b59254c0-9f42-4343-a102-4ac04a66c56b/result.md",
      "url": "https://oss-cn-xxx.aliyuncs.com/glm_ocr_output/b59254c0-...?sign=..."
    },
    {
      "path": "result.json",
      "oss_key": "glm_ocr_output/b59254c0-9f42-4343-a102-4ac04a66c56b/result.json",
      "url": "https://oss-cn-xxx.aliyuncs.com/glm_ocr_output/b59254c0-...?sign=..."
    }
  ],
  "result_url": "/results/b59254c0-9f42-4343-a102-4ac04a66c56b"
}
```

OSS 任务响应在 `/parse` 响应基础上额外包含：

| 字段 | 说明 |
|---|---|
| `task_id` | 请求中传入的业务任务标识 |
| `oss_source` | 原始文件的 OSS 路径 |
| `oss_artifacts` | 已上传到 OSS 的结果文件列表，每项包含 `path`（相对路径）、`oss_key`（OSS 对象键）和 `url`（签名下载链接） |

`oss_artifacts` 中的 `url` 为临时签名链接，有效期由服务端配置（默认 1 小时）。
调用方可直接使用该链接下载结果文件，无需再通过 `/results/{job_id}` 接口获取。

如果服务端未配置 OSS 凭据，该接口将返回 HTTP 500 错误。

## 4. 成功响应

解析接口为同步接口。提交后，连接会保持到排队、解析和结果保存全部完成。大
PDF 可能需要较长时间，请将客户端读取超时设置为至少 10 分钟。

成功响应示例：

```json
{
  "job_id": "b59254c0-9f42-4343-a102-4ac04a66c56b",
  "status": "completed",
  "queue": "pdf_layout",
  "filename": "document.pdf",
  "content_type": "pdf",
  "image_mode": "auto",
  "created_at": "2026-07-31T13:30:00+08:00",
  "started_at": "2026-07-31T13:30:05+08:00",
  "finished_at": "2026-07-31T13:32:10+08:00",
  "chars": 12345,
  "elapsed_sec": 125.0,
  "text": "# Markdown 识别结果",
  "artifacts": [
    "job.json",
    "result/result.md",
    "result/result.json"
  ],
  "result_url": "/results/b59254c0-9f42-4343-a102-4ac04a66c56b"
}
```

主要字段：

| 字段 | 说明 |
|---|---|
| `job_id` | 本次请求的唯一 UUID |
| `status` | 任务状态 |
| `queue` | 实际进入的处理队列 |
| `text` | Markdown 或纯文本识别结果 |
| `chars` | 识别结果字符数 |
| `elapsed_sec` | 实际执行耗时，不包含排队时间 |
| `created_at` | 任务创建时间，北京时间 |
| `started_at` | 开始执行时间，北京时间 |
| `finished_at` | 完成时间，北京时间 |
| `artifacts` | 可下载的结果文件相对路径 |
| `result_url` | 任务查询接口路径 |

调用方应保存 `job_id`，用于后续查询和下载结果。

## 5. 查询任务

```http
GET /results/{job_id}
```

调用示例：

```bash
curl -s \
  '{BASE_URL}/results/b59254c0-9f42-4343-a102-4ac04a66c56b'
```

任务状态：

| 状态 | 说明 |
|---|---|
| `queued` | 正在排队 |
| `running` | 正在处理 |
| `completed` | 已完成 |
| `failed` | 处理失败 |
| `rejected` | 队列已满，任务被拒绝 |

失败时，响应或 `job.json` 中包含 `error` 字段。

## 6. 下载结果文件

先从解析响应或任务查询响应的 `artifacts` 字段取得文件路径，然后调用：

```http
GET /results/{job_id}/{artifact_path}
```

下载示例：

```bash
curl -OJ \
  '{BASE_URL}/results/<job_id>/<artifact_path>'
```

例如：

```bash
curl -OJ \
  '{BASE_URL}/results/<job_id>/result/result.md'
```

不同文件和处理模式生成的产物可能不同，应以响应中的 `artifacts` 为准，不要
自行拼接或猜测文件名。

## 7. Python 调用示例

```python
from pathlib import Path

import requests

BASE_URL = "http://example-host:18091"
FILE_PATH = Path("/path/to/document.pdf")

with FILE_PATH.open("rb") as file_obj:
    response = requests.post(
        f"{BASE_URL}/parse",
        files={
            "file": (
                FILE_PATH.name,
                file_obj,
                "application/pdf",
            )
        },
        timeout=600,
    )

if response.status_code == 429:
    retry_after = response.headers.get("Retry-After", "10")
    raise RuntimeError(f"服务繁忙，请在 {retry_after} 秒后重试")

response.raise_for_status()
result = response.json()

print("任务 ID：", result["job_id"])
print("执行耗时：", result["elapsed_sec"])
print(result["text"])
```

局部图片使用 `model_only`：

```python
from pathlib import Path

import requests

BASE_URL = "http://example-host:18091"
IMAGE_PATH = Path("/path/to/crop.png")

with IMAGE_PATH.open("rb") as file_obj:
    response = requests.post(
        f"{BASE_URL}/parse",
        params={"image_mode": "model_only"},
        files={
            "file": (
                IMAGE_PATH.name,
                file_obj,
                "image/png",
            )
        },
        timeout=600,
    )

response.raise_for_status()
print(response.json()["text"])
```

通过 OSS 路径解析：

```python
import requests

BASE_URL = "http://example-host:18091"

response = requests.post(
    f"{BASE_URL}/parse_oss",
    json={
        "task_id": "order-12345",
        "oss_path": "docs/medical/report.pdf",
        "image_mode": "auto",
    },
    timeout=600,
)

if response.status_code == 429:
    retry_after = response.headers.get("Retry-After", "10")
    raise RuntimeError(f"服务繁忙，请在 {retry_after} 秒后重试")

response.raise_for_status()
result = response.json()

print("任务 ID：", result["task_id"])
print("解析 ID：", result["job_id"])
print("执行耗时：", result["elapsed_sec"])
print(result["text"])

for artifact in result.get("oss_artifacts", []):
    print(f"  {artifact['path']} -> {artifact['url']}")
```

## 8. 限流与重试

对应处理队列已满时，服务返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 10
```

响应示例：

```json
{
  "detail": {
    "message": "pdf_layout queue is full",
    "retry_after_sec": 10
  }
}
```

建议调用方：

1. 优先读取 `Retry-After` 响应头。
2. 等待指定秒数后重试。
3. 增加少量随机延迟，避免多个客户端同时重试。
4. 限制重试次数，例如最多重试 3 次。
5. 不要对 HTTP `400` 错误自动重试。

## 9. 错误码

| HTTP 状态码 | 说明 | 建议 |
|---:|---|---|
| 200 | 解析成功 | 读取并保存 `job_id` |
| 400 | 文件或参数错误 | 检查文件类型和 `image_mode` |
| 404 | UUID 或结果文件不存在 | 检查任务 ID 和 `artifacts` |
| 429 | 服务队列已满 | 按 `Retry-After` 延迟重试 |
| 500 | OCR 或结果保存失败 | 保存 `job_id` 并联系服务管理员 |
| 503 | 服务尚未准备好 | 稍后重试并检查 `/health` |

## 10. 调用注意事项

- 病例整页图片建议使用 `auto` 或 `layout`。
- 已裁剪的局部图片建议使用 `model_only`。
- 大 PDF 的处理时间与页数、版面区域数量有关。
- 请求超时不代表服务端任务一定停止，请避免立即无条件重复提交。
- 不要将同一文件高频重复提交。
- 下载产物时必须使用响应中的 `artifacts` 路径。
- `imgs/` 中的文件是版面分析识别出的图片或图表区域，不代表 OCR 失败。
- 病例文件可能包含敏感信息，请仅通过服务管理员提供的受控网络地址调用。
