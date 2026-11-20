# GLM-OCR v5：公式安全翻译

V5 基于 V3 的本地 LLM 翻译流程，在调用翻译模型前保护 LaTeX / 数学公式，
翻译完成后恢复原始公式。若模型改写或删除公式占位符，该段回退原文，避免
产生乱码。

- 端口：`18096`
- 启动：`bash deploy/glm-ocr-v5/start.sh`
- 默认翻译模型：本地 `qwen3.5-9b`（`127.0.0.1:18003`）

```bash
curl -X POST 'http://127.0.0.1:18096/parse?translate=true' \
  -F 'file=@/path/to/document.pdf'
```
