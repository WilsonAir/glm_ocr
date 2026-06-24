# glm_ocr

GLM-OCR official SDK deployment for Alibaba Cloud PPU, using a remote vLLM inference service.

## Features

- Official `glmocr` SDK with PP-DocLayoutV3 layout detection
- Self-hosted mode: OCR inference via existing vLLM OpenAI API
- PPU-compatible Python env (reuses `/opt/ac2` PyTorch stack)

## Quick start

```bash
# 1. Setup environment (pip venv on ac2, not conda)
bash setup_env.sh
bash download_layout_model.sh   # ModelScope: PP-DocLayoutV3

# 2. Start vLLM GLM-OCR (separate process, e.g. port 18080)
# See /data/wilson_2/de/models/scripts/serve_glm_ocr.sh

# 3. Activate & parse
source activate_glm_ocr.sh
python test_sdk_parse.py --max-pages 5
```

## Configuration

Edit `config.yaml`:

- `pipeline.ocr_api.api_host` / `api_port` — vLLM endpoint (default `127.0.0.1:18080`)
- `pipeline.layout.device` — layout GPU (default `cuda:0`)
- `pipeline.page_loader.max_tokens` — keep below vLLM `max_model_len`

## PPU pip

Use Alibaba PPU pip index when installing extra packages:

```bash
unset PIP_INDEX_URL
pip install <package> -c pip.conf.ppu
```

## License

SDK code follows glmocr / GLM-OCR upstream licenses.
