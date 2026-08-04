# GLM-OCR v1/v2 外部接口文档

当前环境同时保留 v1 和 v2，两套接口地址和调用方式不同。原有调用方继续使用
v1，不受 v2 影响；需要异步排队、状态查询、结果查询和取消能力的新调用方使用 v2。

## 版本选择

| 对比项 | v1 同步接口 | v2 异步任务接口 |
|---|---|---|
| 外部前缀 | `/glm-ocr` | `/glm-ocr-v2` |
| 提交接口 | `POST /parse`、`POST /parse_image` | `POST /tasks` |
| 返回时机 | OCR 完成后返回结果 | 文件持久化后立即返回 `202 + job_id` |
| 查询状态 | 不支持 | `GET /tasks/{job_id}/status` |
| 获取结果 | 提交响应直接包含结果 | `GET /tasks/{job_id}/result` |
| 取消任务 | 不支持 | `DELETE /tasks/{job_id}` |
| 持久化恢复 | 不支持 | 支持 |
| 适用场景 | 兼容旧调用方、小文件或低并发调用 | 长耗时任务、批量提交和可靠排队 |
| 内部服务 | `18090` | 外层 `18092` → 内层 `18091` |

两个版本没有交叉路由：

```text
/glm-ocr/parse       是 v1
/glm-ocr-v2/tasks    是 v2

/glm-ocr/tasks       不存在
/glm-ocr-v2/parse    不存在
```

支持的文件类型：

```text
PDF：.pdf
图片：.jpg、.jpeg、.png、.bmp、.gif、.webp
```

当前网关允许的单个请求体上限为 `500 MB`。调用方不需要先把文件上传到
OSS/OBS；提交接口直接接收 `multipart/form-data` 文件内容。

## v1：同步解析接口

### 基础地址

```bash
V1_BASE_URL='http://<测试机IP>/glm-ocr'
```

v1 是原有兼容接口。客户端上传文件后，HTTP 请求会一直等待 OCR 处理完成，成功时
直接在同一个响应中返回 Markdown 文本。v1 不生成 `job_id`，也没有状态、结果或
取消接口。

### v1 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | v1 健康检查 |
| `POST` | `/parse` | 解析 PDF 或图片 |
| `POST` | `/parse_image` | 只解析图片 |
| `POST` | `/parse_bytes` | 使用原始请求体上传 PDF 或图片字节 |

### v1 解析 PDF 或图片

```http
POST /parse
Content-Type: multipart/form-data
```

| 位置 | 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| Body | `file` | File | 是 | PDF 或支持的图片文件 |
| Query | `max_pages` | Integer | 否 | v1 兼容参数；当前实现接收该参数，但未实际限制解析页数 |

接口不需要传入 `name`。

```bash
curl -sS -X POST "$V1_BASE_URL/parse" \
  -F 'file=@/path/to/document.pdf;type=application/pdf'
```

Apifox 中选择 `POST`，Body 使用 `form-data`，参数名填写 `file`，类型选择“文件”。
请求会一直等待 OCR 完成，不会先返回任务编号。

携带兼容参数的调用示例（调用方不应依赖当前 v1 实现按此参数截断页数）：

```bash
curl -sS -X POST "$V1_BASE_URL/parse?max_pages=5" \
  -F 'file=@/path/to/document.pdf;type=application/pdf'
```

成功时等待 OCR 完成后返回 HTTP `200`：

```json
{
  "filename": "document.pdf",
  "text": "# OCR 识别结果\n\n...",
  "chars": 18234,
  "elapsed_sec": 63.27,
  "content_type": "pdf"
}
```

v1 不创建持久化任务，也不上传 OSS，因此响应中没有 `job_id`、`artifacts` 或下载 URL；
需要 Markdown、图片产物和临时下载链接时，请使用 v2。

### v1 图片专用接口

```bash
curl -sS -X POST "$V1_BASE_URL/parse_image" \
  -F 'file=@/path/to/scan.png;type=image/png'
```

`/parse_image` 只接受图片。也可以把图片提交到 `/parse`。

### v1 原始字节接口

```bash
curl -sS -X POST "$V1_BASE_URL/parse_bytes" \
  -H 'Content-Type: application/pdf' \
  --data-binary '@/path/to/document.pdf'
```

### v1 健康检查

```bash
curl -sS "$V1_BASE_URL/health"
```

```json
{
  "status": "ok",
  "model": "glm-ocr",
  "service": "glm-ocr-parse"
}
```

调用 v1 时，调用方的 HTTP 超时时间必须覆盖完整 OCR 时间。v1 请求失败后不能通过
`job_id` 恢复或查询，需要调用方自行决定是否重新提交。

### v1 HTTP 状态码

| 状态码 | 说明 |
|---|---|
| `200` | OCR 已完成，响应直接包含结果 |
| `400` | 文件为空或文件类型不支持 |
| `413` | 文件超过网关允许的请求体大小 |
| `422` | 缺少必填的 `file` 字段，或查询参数格式错误 |
| `500` | OCR 解析失败 |
| `502/504` | 网关无法连接 v1 服务或同步处理超时 |

## v2：异步任务接口

### 基础地址

```bash
V2_BASE_URL='http://<测试机IP>/glm-ocr-v2'
```

v2 先上传并持久化文件，然后返回 `job_id`。OCR 在后台执行，调用方根据
`job_id` 查询状态并获取结果。

### v2 使用流程

```text
1. POST /tasks 上传文件
2. 保存响应中的 job_id
3. GET /tasks/{job_id}/status 查询状态
4. status=completed 后，GET /tasks/{job_id}/result 获取结果
5. 不再需要处理时，可 DELETE /tasks/{job_id} 取消任务
```

建议每 `2～5` 秒查询一次状态，不要高频轮询。

### v2 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查和队列概况 |
| `POST` | `/tasks` | 提交 PDF 或图片任务 |
| `GET` | `/tasks/{job_id}/status` | 查询任务状态 |
| `GET` | `/tasks/{job_id}/result` | 获取 OCR 结果 |
| `DELETE` | `/tasks/{job_id}` | 取消任务 |

### 1. 提交任务

```http
POST /tasks
Content-Type: multipart/form-data
```

#### 请求参数

| 位置 | 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| Body | `file` | File | 是 | PDF 或支持的图片文件 |
| Query | `image_mode` | String | 否 | `auto`、`layout` 或 `model_only`，默认 `auto` |

接口不需要传入 `name`。

`image_mode` 说明：

| 值 | 说明 |
|---|---|
| `auto` | 默认模式，由内层服务按文件类型处理 |
| `layout` | 执行版面分析后识别 |
| `model_only` | 图片直接交给模型，不执行版面分析；PDF 不支持此值 |

#### cURL 示例

上传 PDF：

```bash
curl -sS -X POST "$V2_BASE_URL/tasks" \
  -F 'file=@/path/to/2504.19413v1.pdf;type=application/pdf'
```

上传图片：

```bash
curl -sS -X POST "$V2_BASE_URL/tasks?image_mode=model_only" \
  -F 'file=@/path/to/crop.png;type=image/png'
```

#### Apifox 配置

1. 请求方法选择 `POST`。
2. URL 填写 `http://<测试机IP>/glm-ocr-v2/tasks`。
3. Body 选择 `form-data`。
4. 参数名填写 `file`，类型选择“文件”，然后选择本地 PDF 或图片。
5. 不要手动设置 `Content-Type`；Apifox 会自动生成 multipart boundary。

#### 成功响应

HTTP 状态码：

```text
202 Accepted
```

```json
{
  "job_id": "2504.19413v1_20260803084610",
  "name": "2504.19413v1",
  "status": "pending",
  "queue": "pdf",
  "filename": "2504.19413v1.pdf",
  "size_bytes": 1144031,
  "content_type": "pdf",
  "image_mode": "auto",
  "created_at": "2026-08-03T08:46:10+08:00",
  "status_url": "/tasks/2504.19413v1_20260803084610/status",
  "result_url": "/tasks/2504.19413v1_20260803084610/result",
  "cancel_url": "/tasks/2504.19413v1_20260803084610"
}
```

`202` 只表示任务已接收并持久化，不表示 OCR 已完成。由于后台调度是并发执行的，
响应中的 `status` 是返回时的状态快照，通常为 `pending`，也可能已经变为 `queued`
或 `running`。

#### Job ID 规则

```text
<上传文件基础名>_<北京时间 yyyyMMddHHmmss>
```

例如：

```text
2504.19413v1.pdf
→ 2504.19413v1_20260803084610
```

文件扩展名不进入 Job ID。空格和不安全字符会替换为下划线，文件名部分最多保留
59 个字符。同名文件在同一秒内重复提交时，后续任务追加 `_0001`、`_0002` 等短序号。

### 2. 查询任务状态

```http
GET /tasks/{job_id}/status
```

```bash
curl -sS "$V2_BASE_URL/tasks/<job_id>/status"
```

成功返回 HTTP `200`。主要字段示例：

```json
{
  "job_id": "2504.19413v1_20260803084610",
  "status": "running",
  "queue": "pdf",
  "filename": "2504.19413v1.pdf",
  "created_at": "2026-08-03T08:46:10+08:00",
  "queued_at": "2026-08-03T08:46:10+08:00",
  "started_at": "2026-08-03T08:46:11+08:00",
  "artifacts": [
    "input/2504.19413v1.pdf",
    "job.json"
  ]
}
```

#### 状态说明

| 状态 | 是否结束 | 说明 | 调用方处理建议 |
|---|---|---|---|
| `pending` | 否 | 文件已持久化，正在等待模型就绪队列空位 | 继续轮询 |
| `queued` | 否 | 已进入对应的模型就绪队列 | 继续轮询 |
| `running` | 否 | Worker 已调用内层 OCR 服务处理 | 继续轮询 |
| `cancel_requested` | 否 | 运行中任务已请求取消，等待内层调用返回后收尾 | 继续轮询至 `canceled` |
| `completed` | 是 | OCR 成功完成，结果可以获取 | 调用结果接口 |
| `failed` | 是 | 处理失败，响应中通常包含 `error` | 记录错误，按业务决定是否重试 |
| `canceled` | 是 | 任务已取消，不会产生可用结果 | 停止轮询 |

正常状态流转：

```text
pending → queued → running → completed
                           ↘ failed
```

取消状态流转：

```text
pending/queued → canceled
running → cancel_requested → canceled
```

服务重启恢复时，未完成的 `pending`、`queued`、`running` 任务可能重新显示为
`pending`，随后由调度器重新投递。

### 3. 获取结果

```http
GET /tasks/{job_id}/result
```

```bash
curl -sS "$V2_BASE_URL/tasks/<job_id>/result"
```

任务为 `completed` 时返回 HTTP `200`：

```json
{
  "job_id": "2504.19413v1_20260803084610",
  "status": "completed",
  "filename": "2504.19413v1.pdf",
 "chars": 18234,
 "elapsed_sec": 63.27,
  "text": "# OCR 识别结果\n\n...",
  "storage": {
    "type": "oss",
    "bucket": "repilot",
    "prefix": "glm_ocr_output/2504.19413v1_20260803084610",
    "result_object_key": "glm_ocr_output/2504.19413v1_20260803084610/result.md"
  },
  "artifacts": [
    {
      "name": "result.md",
      "path": "result.md",
      "type": "markdown",
      "object_key": "glm_ocr_output/2504.19413v1_20260803084610/result.md",
      "url": "https://...签名参数...",
      "url_expires_in_seconds": 3600
    },
    {
      "name": "image_0.jpg",
      "path": "artifacts/imgs/image_0.jpg",
      "type": "image",
      "object_key": "glm_ocr_output/2504.19413v1_20260803084610/artifacts/imgs/image_0.jpg",
      "url": "https://...签名参数...",
      "url_expires_in_seconds": 3600
    }
  ]
}
```

`text` 是 OCR Markdown 文本。`storage` 描述该任务在 OSS 中的保存位置；其中
`object_key` 是 OSS 对象键，**不是**可直接访问的 URL。

`artifacts` 是 OSS 中的结果文件清单：

| 字段 | 说明 |
|---|---|
| `name` / `path` | 文件名及任务内相对路径 |
| `type` | `markdown`、`json`、`image` 或 `file` |
| `object_key` | OSS 对象键，例如 `glm_ocr_output/<job_id>/result.md` |
| `url` | 可直接 `GET` 的临时签名下载链接 |
| `url_expires_in_seconds` | URL 签发时配置的有效期（秒），默认 `3600` |

成功任务始终包含 `result.md` 和 `inner_response.json`；内层 OCR 实际生成时，还会包含
`imgs/`、`layout_vis/` 等图片产物。`url` 可以在浏览器打开，也可用 `curl` 下载：

```bash
# 下载 OCR Markdown
curl -L '<result.md 对应的 artifacts[].url>' -o result.md

# 下载 PDF 内层生成的图片
curl -L '<image 对应的 artifacts[].url>' -o image_0.jpg
```

对象桶保持私有，签名 URL 到期后将无法访问；重新调用本接口即可获得新的 URL。

任务尚未完成、失败或已取消时，返回 HTTP `409`：

```json
{
  "job_id": "2504.19413v1_20260803084610",
  "status": "running",
 "message": "Task result is not available"
}
```

### 4. 取消任务

```http
DELETE /tasks/{job_id}
```

```bash
curl -sS -X DELETE "$V2_BASE_URL/tasks/<job_id>"
```

- `pending` 或 `queued`：立即取消，返回 HTTP `200` 和 `status=canceled`。
- `running`：返回 HTTP `202` 和 `status=cancel_requested`。内层 OCR 是同步调用，
  当前处理不能被安全强杀；调用结束后外层不发布结果，并将状态收尾为 `canceled`。
- `completed` 或 `failed`：任务已经结束，返回 HTTP `409`。
- 重复取消 `canceled` 任务：返回 HTTP `200`。

### 5. 健康检查

```http
GET /health
```

```bash
curl -sS "$V2_BASE_URL/health"
```

响应示例：

```json
{
  "status": "ok",
  "service": "glm-ocr-task-queue",
  "queues": {
    "pdf": {
      "capacity": 4,
      "workers": 2,
      "waiting": 0,
      "running": 0,
      "backlog": 0
    },
    "image": {
      "capacity": 8,
      "workers": 1,
      "waiting": 0,
      "running": 0,
      "backlog": 0
    }
  }
}
```

字段说明：

- `waiting`：已进入模型就绪队列、等待 Worker 的数量。
- `running`：正在调用内层 OCR 的数量。
- `backlog`：已持久化但尚未进入模型就绪队列的 `pending` 数量。

健康检查返回 `200` 只能证明网关和外层任务服务可访问。完整 OCR 能力应通过
“提交任务 → 查询至 completed → 获取结果”进行验证。

### v2 HTTP 状态码

| 状态码 | 说明 |
|---|---|
| `200` | 查询成功、结果获取成功或任务已取消 |
| `202` | 任务已接收，或者运行中任务已接受取消请求 |
| `400` | 空文件、不支持的文件类型、PDF 使用了 `model_only` 等参数错误 |
| `404` | `job_id` 不存在或格式不正确 |
| `409` | 结果尚不可用，或者当前任务状态不允许取消 |
| `413` | 文件超过网关允许的请求体大小 |
| `422` | 缺少必填的 `file` 字段，或 `image_mode` 取值非法 |
| `500` | 文件持久化或结果读取等服务端错误 |
| `502/504` | 网关无法连接后端服务或等待后端超时 |

### v2 完整调用示例

提交：

```bash
curl -sS -X POST "$V2_BASE_URL/tasks" \
  -F 'file=@/path/to/2504.19413v1.pdf;type=application/pdf' \
  | tee /tmp/glm_ocr_submit.json
```

读取 Job ID（需要 `jq`）：

```bash
JOB_ID=$(jq -r '.job_id' /tmp/glm_ocr_submit.json)
```

查询状态：

```bash
curl -sS "$V2_BASE_URL/tasks/$JOB_ID/status" | jq
```

任务完成后保存 Markdown：

```bash
curl -sS "$V2_BASE_URL/tasks/$JOB_ID/result" \
  | jq -r '.text' \
  > glm_ocr_result.md
```

### v2 路径前缀说明

接口响应中的 `status_url`、`result_url`、`cancel_url` 是服务内部相对路径，
不包含网关前缀 `/glm-ocr-v2`。外部调用时应使用：

```text
V2_BASE_URL + status_url
```

例如：

```text
V2_BASE_URL = http://<测试机IP>/glm-ocr-v2
status_url  = /tasks/<job_id>/status

最终地址：
http://<测试机IP>/glm-ocr-v2/tasks/<job_id>/status
```
