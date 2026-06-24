# glm_ocr

GLM-OCR 与 PaddleOCR-VL 在阿里云 PPU 上的部署、推理与对比分析（vLLM + 官方 SDK）。

## 目录结构

```
glm_ocr/
├── code/                    # 代码
│   ├── glm_ocr/             # GLM-OCR SDK 部署脚本
│   └── paddle_ocr/          # PaddleOCR-VL 部署脚本
├── result/                  # OCR 输出与对比
│   ├── glm_ocr/
│   ├── paddle_ocr/
│   └── comparison/          # report.md, metrics.json
├── sample/aml_nccn.pdf      # 测试 PDF（209 页）
└── scripts/compare_ocr.py   # 对照 PDF 文本层做对比
```

## GLM-OCR 快速开始

```bash
cd code/glm_ocr
bash setup_env.sh
bash download_layout_model.sh
source activate_glm_ocr.sh
python test_sdk_parse.py --max-pages 5
```

vLLM 服务见 `/data/wilson_2/de/models/scripts/serve_glm_ocr.sh`（默认端口 18080）。

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
