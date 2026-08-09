# PaddleOCR-VL 服务 API（对齐 glm-ocr-v2）

默认地址：`http://127.0.0.1:18093`  
VL 后端：已部署的 vLLM `http://127.0.0.1:18081/v1`（模型名 `PaddleOCR-VL-1.6`）

版面识别按 [PaddleOCR-VL 官方文档](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)：

1. `PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="vllm-server", ...)`
2. `predict(..., use_layout_detection=True)` — PP-DocLayoutV3 做版面分析 + 阅读顺序
3. PDF 多页：`restructure_pages(merge_tables=True, relevel_titles=True, concatenate_pages=True)`
4. `save_to_json` / `save_to_markdown`；正文用官方 `concatenate_markdown_pages`

`image_mode=model_only` 对应官方元素级路径：`use_layout_detection=False, prompt_label="ocr"`。

## 启动

```bash
bash deploy/paddle-ocr/start.sh
bash deploy/paddle-ocr/start.sh --status
bash deploy/paddle-ocr/start.sh --stop
```

## GET /queue_status

```bash
curl -s 'http://127.0.0.1:18093/queue_status'
```

## POST /parse_oss（异步）

请求体与 glm-ocr-v2 相同：

| 参数 | 必填 | 说明 |
|---|---:|---|
| `job_id` | 是 | 外层任务 ID |
| `oss_path` | 是 | OSS object key |
| `image_mode` | 否 | `auto` / `layout` / `model_only` |
| `attempt` | 否 | 默认 `1` |

```bash
curl -X POST 'http://127.0.0.1:18093/parse_oss' \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "patient_001_20260807043000",
    "oss_path": "docs/medical/report.pdf",
    "image_mode": "auto",
    "attempt": 1
  }'
```

成功返回 HTTP 202，随后轮询：

```bash
curl -s 'http://127.0.0.1:18093/results/{job_id}'
```

结果上传到 OSS：`paddle_ocr_output/{job_id}/output/attempt-{n}/`

OSS 凭据从仓库根目录 `.env` 读取（与 glm-ocr-v2 共用），不要写入 `config.env`。

## 其它接口

- `GET /health`
- `POST /parse`、`POST /parse_image`（同步，语义同 glm-ocr-v2）
- `GET /results/{job_id}`、`GET /results/{job_id}/{artifact_path}`
