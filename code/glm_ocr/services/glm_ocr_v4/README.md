# GLM-OCR v4：OCR 直出中文

V4 在 OCR 模型请求阶段直接要求英文医学文本输出简体中文。它不调用 v3 的
段落翻译后处理，也不使用 Google 或百度翻译接口。

- 服务端口：`18095`
- 默认输出：`result/glm_ocr/framework_v4_zh`
- 配置：`config/ocr_services_v4.yaml`
- 启动：`bash deploy/glm-ocr-v4/start.sh`

示例：

```bash
curl -X POST 'http://127.0.0.1:18095/parse' \
  -F 'file=@/path/to/document.pdf'
```

V4 的直接中文效果取决于 OCR 模型是否遵循翻译提示词；如果模型仍只做英文
转写，需要改用支持指令翻译的视觉语言模型。公式、数字、单位、药品名、基因
名和医学缩写会在提示词中要求保持准确。
