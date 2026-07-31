# glm_ocr

GLM-OCR 与 PaddleOCR-VL 在阿里云 PPU 上的部署、推理与对比分析（vLLM + 官方 SDK）。

## 目录结构

```
glm_ocr/
├── code/                    # 代码
│   ├── glm_ocr/             # GLM-OCR SDK 部署脚本
│   │   ├── test_sdk_parse.py   # SDK 解析入口（PDF / 图片）
│   │   ├── input_utils.py      # 输入文件类型判断
│   │   ├── config.yaml         # SDK 配置（指向本地 vLLM）
│   │   └── run_both.sh         # model_only + framework 一键跑
│   └── paddle_ocr/          # PaddleOCR-VL 部署脚本
├── result/                  # OCR 输出与对比
│   ├── glm_ocr/
│   ├── paddle_ocr/
│   └── comparison/          # report.md, metrics.json
├── sample/aml_nccn.pdf      # 测试 PDF（209 页）
└── scripts/compare_ocr.py   # 对照 PDF 文本层做对比
```

## GLM-OCR 快速开始

### 1. 环境与服务

```bash
cd code/glm_ocr
bash setup_env.sh
bash download_layout_model.sh
source activate_glm_ocr.sh
```

vLLM 推理服务（单独启动）：

```bash
# 脚本: /data/wilson_2/de/models/scripts/serve_glm_ocr.sh
# 默认: http://127.0.0.1:18080  模型名 glm-ocr
PORT=18080 bash /data/wilson_2/de/models/scripts/serve_glm_ocr.sh
```

### 2. 支持的输入类型

`test_sdk_parse.py` 会自动根据后缀判断输入类型（与官方 SDK CLI 一致）：

| 类型 | 后缀 |
|---|---|
| PDF | `.pdf` |
| 图片 | `.jpg` `.jpeg` `.png` `.bmp` `.gif` `.webp` |

### 3. SDK 解析（framework：layout + vLLM）

```bash
cd code/glm_ocr
source activate_glm_ocr.sh

# PDF（默认 sample，可用 --max-pages 限制页数）
python test_sdk_parse.py --input ../../sample/aml_nccn.pdf --max-pages 5

# 单张图片
python test_sdk_parse.py --input /path/to/page.png --output result/image_out
```

输出目录包含 Markdown、JSON 及 `summary.json`（含 `input_type` 字段）。

### 4. 两种模式一键跑

```bash
cd code/glm_ocr
bash run_both.sh

# 指定输入（PDF 或图片均可；model_only 侧目前仍走 PDF 流程）
INPUT=/path/to/file.pdf MAX_PAGES=5 bash run_both.sh
```

- **model_only**：直接调 vLLM API，无 layout SDK
- **framework**：glmocr SDK + PP-DocLayoutV3 + vLLM

## vLLM API

服务提供 OpenAI 兼容接口，可用于 model_only 推理或外部集成。

| 接口 | 地址 |
|---|---|
| 模型列表 | `GET http://127.0.0.1:18080/v1/models` |
| OCR 推理 | `POST http://127.0.0.1:18080/v1/chat/completions` |

**说明：** vLLM 接口只接受**图片**输入（`image_url`），不直接支持 PDF。处理 PDF 时需先将每页渲染为 PNG，再逐页调用 API（与 `run_both.sh` 中 model_only 流程一致）。

vLLM 需开启 `--allowed-local-media-path /` 以支持 `file://` 本地路径（启动脚本已配置）。

### 检查服务

```bash
curl -s http://127.0.0.1:18080/v1/models | python3 -m json.tool
```

### 图片 OCR

```bash
curl http://127.0.0.1:18080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-ocr",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "image_url", "image_url": {"url": "file:///path/to/image.png"}},
        {"type": "text", "text": "Text Recognition:"}
      ]
    }],
    "max_tokens": 4096,
    "temperature": 0.0
  }'
```

### PDF OCR（逐页）

```bash
PDF=/path/to/doc.pdf
OUT=/tmp/pdf_pages
mkdir -p "$OUT"

# 1. PDF 转 PNG（需 PyMuPDF / fitz）
python3 - "$PDF" "$OUT" <<'PY'
import fitz, sys
from pathlib import Path
pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
doc = fitz.open(pdf)
zoom = 150 / 72.0
mat = fitz.Matrix(zoom, zoom)
page_count = doc.page_count
for i in range(page_count):
    pix = doc.load_page(i).get_pixmap(matrix=mat, alpha=False)
    pix.save(out / f"page_{i+1:03d}.png")
doc.close()
print(f"rendered {page_count} pages")
PY

# 2. 逐页调用 OCR API
for page in "$OUT"/page_*.png; do
  echo "=== $(basename "$page") ==="
  curl -s http://127.0.0.1:18080/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "$(python3 -c "
import json; p='$page'
print(json.dumps({
  'model': 'glm-ocr',
  'messages': [{'role': 'user', 'content': [
    {'type': 'image_url', 'image_url': {'url': 'file://' + p}},
    {'type': 'text', 'text': 'Text Recognition:'}
  ]}],
  'max_tokens': 4096,
  'temperature': 0.0
}))
")" | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
done
```

项目内已封装 model_only PDF 测试脚本，可直接使用：

```bash
/opt/ac2/bin/python3 /data/wilson_2/de/OCR_Test/test_ocr.py \
  --pdf sample/aml_nccn.pdf \
  --result-dir result/model_only \
  --api-base http://127.0.0.1:18080/v1 \
  --max-pages 5
```

## Python API 调用

### 方式一：官方 SDK（framework，layout + vLLM）

SDK 原生支持 PDF 与图片，内部自动完成 PDF 分页、layout 检测与 OCR，无需手动转图。

```python
from pathlib import Path
from glmocr import GlmOcr

CONFIG = Path("code/glm_ocr/config.yaml")

# PDF
with GlmOcr(config_path=str(CONFIG), mode="selfhosted") as parser:
    result = parser.parse("sample/aml_nccn.pdf")
    result.save(output_dir="result/framework")
    print(result.markdown_result)

# 图片
with GlmOcr(config_path=str(CONFIG), mode="selfhosted") as parser:
    result = parser.parse("/path/to/page.png")
    result.save(output_dir="result/image_out")

# 限制 PDF 页数
with GlmOcr(
    config_path=str(CONFIG),
    mode="selfhosted",
    _dotted={"pipeline.page_loader.pdf_max_pages": 5},
) as parser:
    result = parser.parse("sample/aml_nccn.pdf")
    result.save(output_dir="result/framework")
```

命令行等价：

```bash
cd code/glm_ocr && source activate_glm_ocr.sh
python test_sdk_parse.py --input ../../sample/aml_nccn.pdf --max-pages 5
python test_sdk_parse.py --input /path/to/page.png --output result/image_out
```

### 方式二：vLLM HTTP API（model_only，纯 OCR）

适合只需文本识别、不需要 layout 的场景。PDF 需先转 PNG，再逐页请求。

```python
from pathlib import Path

import fitz
import requests

API_BASE = "http://127.0.0.1:18080/v1"
MODEL = "glm-ocr"
PROMPT = "Text Recognition:"


def ocr_image(image_path: Path, timeout: int = 600) -> str:
    resp = requests.post(
        f"{API_BASE}/chat/completions",
        json={
            "model": MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"file://{image_path.resolve()}"}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            "max_tokens": 4096,
            "temperature": 0.0,
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def pdf_to_images(pdf_path: Path, out_dir: Path, dpi: int = 150) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    paths = []
    for i in range(doc.page_count):
        pix = doc.load_page(i).get_pixmap(matrix=matrix, alpha=False)
        path = out_dir / f"page_{i + 1:03d}.png"
        pix.save(path)
        paths.append(path)
    doc.close()
    return paths


# 单张图片
text = ocr_image(Path("/path/to/page.png"))
print(text)

# PDF 逐页
pages = pdf_to_images(Path("sample/aml_nccn.pdf"), Path("result/pages"))
for page in pages[:5]:
    print(f"--- {page.name} ---")
    print(ocr_image(page))
```

完整示例见 `/data/wilson_2/de/OCR_Test/test_ocr.py`。

### 两种方式对比

| | SDK（framework） | vLLM HTTP（model_only） |
|---|---|---|
| 输入 | PDF / 图片直接传入 | 仅图片；PDF 需自行转 PNG |
| Layout | PP-DocLayoutV3 版面分析 | 无 |
| 输出 | Markdown + JSON + 裁剪图 | 纯文本 |
| 依赖 | glmocr + layout 模型 | requests + pymupdf |
| 适用 | 结构化文档解析 | 轻量 OCR、批量文本提取 |

## PaddleOCR-VL 快速开始

```bash
cd code/paddle_ocr
bash setup_env.sh
bash install_paddle_ppu.sh
bash serve_paddleocr_vl.sh    # 端口 18081
bash run_both.sh
```

## OCR 对比分析

```bash
/opt/ac2/bin/python3 scripts/compare_ocr.py
```

以 PDF 内嵌文本为基准，输出 Token F1、序列相似度等指标，报告见 `result/comparison/report.md`。

### 对比结论（aml_nccn.pdf）

| 排名 | 方案 | 加权 Token F1 |
|---:|---|---:|
| 1 | PaddleOCR-VL model_only | 0.9519 |
| 2 | GLM-OCR framework | 0.9401 |
| 3 | GLM-OCR model_only | 0.8888 |
| 4 | PaddleOCR-VL framework | 0.8342 |

## License

SDK 代码遵循 glmocr / GLM-OCR / PaddleOCR 上游许可。
