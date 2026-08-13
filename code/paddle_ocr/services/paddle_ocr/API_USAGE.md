# PaddleOCR-VL 服务 API（对齐 glm-ocr-v2）

| 入口 | 地址 |
|---|---|
| 本机服务 | `http://127.0.0.1:18093` |
| Nginx 网关 | `http://<host>:18780/paddle-ocr`（转发到 `:18093`） |

VL 后端：vLLM `http://127.0.0.1:18081/v1`（served-model-name / `/v1/models` id：`PaddleOCR-VL-1.6`）

Conda 环境：`paddle_ocr`（`deploy/paddle-ocr/config.env`）。若 env 不可写，额外依赖装在 `code/paddle_ocr/.deps`，由 `start.sh` 注入 `PYTHONPATH`。

版面识别按 [PaddleOCR-VL 官方文档](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)：

1. `PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="vllm-server", vl_rec_api_model_name="PaddleOCR-VL-1.6", ...)`
2. `predict(..., use_layout_detection=True)` — PP-DocLayoutV3 做版面分析 + 阅读顺序
3. PDF 多页：`restructure_pages(merge_tables=True, relevel_titles=True, concatenate_pages=True)`
4. `save_to_json` / `save_to_markdown`；正文用官方 `concatenate_markdown_pages`

`image_mode=model_only` 对应官方元素级路径：`use_layout_detection=False, prompt_label="ocr"`。

## 启动

```bash
cd /data/wilson_2/de/glm_ocr/code/paddle_ocr
bash deploy/paddle-ocr/start.sh
bash deploy/paddle-ocr/start.sh --status
bash deploy/paddle-ocr/start.sh --stop
```

依赖：vLLM 已在 `18081` 就绪。

## GET /health

```bash
curl -s 'http://127.0.0.1:18093/health'
curl -s 'http://127.0.0.1:18780/paddle-ocr/health'
```

## GET /queue_status

```bash
curl -s 'http://127.0.0.1:18093/queue_status'
curl -s 'http://127.0.0.1:18780/paddle-ocr/queue_status'
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
curl -X POST 'http://127.0.0.1:18780/paddle-ocr/parse_oss' \
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
curl -s 'http://127.0.0.1:18780/paddle-ocr/results/{job_id}'
```

结果上传到 OSS：`paddle_ocr_output/{job_id}/output/attempt-{n}/`

OSS 凭据从仓库根目录 `.env` 读取（与 glm-ocr-v2 共用），不要写入 `config.env`。

## 其它接口

- `POST /parse`、`POST /parse_image`（同步，语义同 glm-ocr-v2）
- `GET /results/{job_id}`、`GET /results/{job_id}/{artifact_path}`
