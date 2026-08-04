# GLM-OCR 外层异步持久任务队列

本服务不修改 GLM-OCR v2 内层实现。外层监听 `18092`，Worker 通过 HTTP 调用
内层 `http://127.0.0.1:18091/parse`。

面向外部调用方的 v1/v2 完整接口说明见 [API_USAGE.md](API_USAGE.md)。

```text
调用方
  → 外层任务服务 :18092
      ├── 持久化待调度区：保存输入和 job.json，状态 pending
      ├── 后台调度器
      │     ├── PDF 模型就绪队列：4 等待位 → 2 Worker
      │     └── 图片模型就绪队列：8 等待位 → 1 Worker
      └── ResultStore
            ├── 本地任务目录（用于恢复与缓存）
            └── 阿里云 OSS（对外结果与图片产物）
  → 内层 GLM-OCR v2 :18091
```

接口接收任务和模型处理能力相互解耦。提交接口不会因为模型就绪队列已满而返回
“队列已满”；任务会先持久化并立即返回 `jobId`。后台调度器只在对应模型就绪队列
有空位时，才把 `pending` 任务转为 `queued`。

这里的异步队列提升的是接口承载能力、削峰能力和任务可靠性，不会增加模型本身的
物理吞吐量。持久化待调度区没有配置业务数量上限，但仍受磁盘容量约束；落盘失败时
提交接口会返回服务器错误。

## 接口

### 提交任务

```http
POST /tasks
Content-Type: multipart/form-data
```

```bash
curl -X POST 'http://127.0.0.1:18092/tasks' \
  -F 'file=@/path/to/patient_001.pdf'
```

图片可以指定处理模式：

```bash
curl -X POST \
  'http://127.0.0.1:18092/tasks?image_mode=model_only' \
  -F 'file=@/path/to/crop.png'
```

成功返回 HTTP `202`：

```json
{
  "job_id": "patient_001_20260731153045",
  "status": "pending",
  "queue": "pdf",
  "status_url": "/tasks/patient_001_20260731153045/status",
  "result_url": "/tasks/patient_001_20260731153045/result",
  "cancel_url": "/tasks/patient_001_20260731153045"
}
```

Job ID 格式：

```text
<上传文件基础名>_<北京时间 yyyyMMddHHmmss>
```

接口不再要求单独传入 `name`。服务使用上传文件名去掉扩展名后的基础名生成
Job ID；其中的空格和 URL 不安全字符替换为下划线，名称部分最多保留 59 个字符。
同名文件在同一秒内重复提交时，后续任务追加 `_0001`、`_0002` 等短序号以保证唯一。

### 查询状态

```bash
curl -s \
  'http://127.0.0.1:18092/tasks/<job_id>/status'
```

状态：

```text
pending
queued
running
cancel_requested
completed
failed
canceled
```

- `pending`：任务已接收并持久化，等待模型就绪队列空位。
- `queued`：任务已进入有界模型就绪队列。
- `running`：Worker 已把任务交给内层 `/parse` 处理。

### 获取结果

```bash
curl -s \
  'http://127.0.0.1:18092/tasks/<job_id>/result'
```

只有 `completed` 状态返回结果；其他状态返回 HTTP `409`。

### 取消任务

```bash
curl -X DELETE \
  'http://127.0.0.1:18092/tasks/<job_id>'
```

- `pending` 或 `queued` 任务立即进入 `canceled`。
- 运行中任务进入 `cancel_requested`。
- 内层 `/parse` 是同步调用，运行中的线程不能被安全强杀；内层调用返回后，外层
  不再发布结果并将任务收尾为 `canceled`。

### 健康检查

```bash
curl -s http://127.0.0.1:18092/health
```

## 持久化和恢复

任务提交时先保存：

```text
result/glm_ocr/task_queue/<job_id>/
├── input/
│   └── <原始文件>
└── job.json
```

执行成功后增加：

```text
├── result.md
└── inner_response.json
└── artifacts/
    ├── imgs/             # 内层生成时存在
    └── layout_vis/       # 内层生成时存在
```

`job.json` 使用临时文件和 `os.replace()` 原子更新。服务启动时：

- `pending`、`queued`、`running` 任务统一恢复为 `pending`，由调度器重新投递；
- `cancel_requested` 任务收尾为 `canceled`；
- 已完成、失败或取消的任务不重复执行。

## 启动

先启动原有内层 v2：

```bash
cd code/glm_ocr
deploy/glm-ocr-v2/start.sh
```

再启动外层：

```bash
deploy/glm-ocr-task-queue/start.sh
```

管理命令：

```bash
deploy/glm-ocr-task-queue/start.sh --status
deploy/glm-ocr-task-queue/start.sh --stop
deploy/glm-ocr-task-queue/start.sh --foreground
```

配置文件：

```text
code/glm_ocr/config/task_queue_v2.yaml
```

## 阿里云 OSS 结果存储

外层不改动内层 OCR 服务。内层任务完成后，外层根据其 `artifacts` 清单，通过内层
`/results/{inner_job_id}/{artifact_path}` 下载图片、布局可视化等产物到本地任务目录，
再统一上传 OSS。

OSS 对象键结构：

```text
${OSS_PREFIX}/<job_id>/
├── result.md
├── inner_response.json
└── artifacts/
    ├── imgs/...
    └── layout_vis/...
```

将仓库中的 [.env.example](../../../../.env.example) 复制为仓库根目录 `.env` 并填写真实
凭据。`.env` 已在 `.gitignore` 中，禁止提交到 GitLab：

```dotenv
OSS_ENDPOINT=https://oss-cn-<region>.aliyuncs.com
OSS_ACCESS_KEY_ID=...
OSS_ACCESS_KEY_SECRET=...
OSS_BUCKET_NAME=...
OSS_PREFIX=glm_ocr_output
OSS_SIGNED_URL_EXPIRES_SECONDS=3600
```

启动前安装 OSS SDK：

```bash
pip install -r code/glm_ocr/requirements.txt
```

任务完成后，`GET /tasks/{job_id}/result` 的 `artifacts` 包含每个 OSS 对象键和临时
签名下载链接；默认有效期为 3600 秒，可在 `.env` 或
`task_queue_v2.yaml` 中调整。`object_key` 只是对象路径，只有 `artifacts[].url` 可供
浏览器或 `curl -L` 直接下载。对象桶建议保持私有。

OSS 上传或内层产物下载失败时，任务标记为 `failed`，从而避免返回一个缺少图片的
“完成”结果。

## 约束

- 外层服务必须保持单进程；多个 Uvicorn Worker 会各自创建调度队列。
- 图片 layout 和 model-only 共用一个图片 Worker。
- 需要单独制定任务输入和本地缓存的过期清理策略。
- 外层只依赖内层 `/parse` 的 HTTP 契约，内层代码可以独立升级。
