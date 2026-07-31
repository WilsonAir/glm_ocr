# GLM-OCR 外层异步持久任务队列

本服务不修改 GLM-OCR v2 内层实现。外层监听 `18092`，Worker 通过 HTTP 调用
内层 `http://127.0.0.1:18091/parse`。

```text
调用方
  → 外层任务服务 :18092
      ├── 持久化待调度区：保存输入和 job.json，状态 pending
      ├── 后台调度器
      │     ├── PDF 模型就绪队列：4 等待位 → 2 Worker
      │     └── 图片模型就绪队列：8 等待位 → 1 Worker
      └── ResultStore
            └── 当前：本地文件
            └── 后续：OBS Adapter
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
  -F 'name=patient_001' \
  -F 'file=@/path/to/document.pdf'
```

图片可以指定处理模式：

```bash
curl -X POST \
  'http://127.0.0.1:18092/tasks?image_mode=model_only' \
  -F 'name=crop_001' \
  -F 'file=@/path/to/crop.png'
```

成功返回 HTTP `202`：

```json
{
  "job_id": "patient_001_20260731153045123456",
  "status": "pending",
  "queue": "pdf",
  "status_url": "/tasks/patient_001_20260731153045123456/status",
  "result_url": "/tasks/patient_001_20260731153045123456/result",
  "cancel_url": "/tasks/patient_001_20260731153045123456"
}
```

Job ID 格式：

```text
<用户名称>_<北京时间 yyyyMMddHHmmssffffff>
```

名称中的空格和 URL 不安全字符替换为下划线，名称部分最多保留 59 个字符。

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

## OBS 扩展点

[storage.py](storage.py) 定义了 `ResultStore` 协议：

```python
class ResultStore(Protocol):
    def save(self, *, job_dir, text, inner_response) -> dict: ...
    def read_text(self, *, job_dir, metadata) -> str: ...
```

当前 `LocalResultStore` 写本地文件。后续新增 `ObsResultStore` 并在
`build_result_store()` 中按配置创建即可，队列、任务状态和 API 不需要修改。

建议 OBS Adapter 返回类似元数据：

```json
{
  "storage": {
    "type": "obs",
    "bucket": "bucket-name",
    "object_key": "glm-ocr/<job_id>/result.md"
  }
}
```

## 约束

- 外层服务必须保持单进程；多个 Uvicorn Worker 会各自创建调度队列。
- 图片 layout 和 model-only 共用一个图片 Worker。
- 需要单独制定任务输入和本地缓存的过期清理策略。
- 外层只依赖内层 `/parse` 的 HTTP 契约，内层代码可以独立升级。
