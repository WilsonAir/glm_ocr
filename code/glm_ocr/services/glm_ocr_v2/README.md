# GLM-OCR persistent parse API v2

The v2 service calls the official SDK's `result.save()` for every request.
Each request gets an independent UUID directory below:

```text
result/glm_ocr/framework/<uuid>/
```

Depending on the installed SDK and document contents, the UUID directory
contains JSON, Markdown, `imgs/` image crops, `layout_vis/` previews, and a
service-owned `job.json` manifest. The SDK may add a document-name subdirectory
inside the UUID directory.

Start in the background from `code/glm_ocr`:

```bash
deploy/glm-ocr-v2/start.sh
```

Manage the service:

```bash
deploy/glm-ocr-v2/start.sh --foreground
deploy/glm-ocr-v2/start.sh --status
deploy/glm-ocr-v2/start.sh --stop
```

The default address is `http://127.0.0.1:18091`. Configuration can be
overridden with `GLM_OCR_V2_CONFIG`, `GLM_OCR_V2_LAYOUT_DEVICE`,
`GLM_OCR_V2_OUTPUT_ROOT`, `GLM_OCR_V2_HOST`, and `GLM_OCR_V2_PORT`.
The launcher also keeps the PPU RTC cache under
`/data/wilson_2/cache/rtccache` and links the runtime's `~/.rtccache` path to
it. Override `PPU_RTC_CACHE_DIR` or `PPU_RTC_CACHE_LINK` when needed.

Parse and inspect a result:

```bash
curl -X POST http://127.0.0.1:18091/parse \
  -F 'file=@/path/to/document.pdf'

curl http://127.0.0.1:18091/results/<uuid>
curl -OJ http://127.0.0.1:18091/results/<uuid>/<artifact-path>
```

The synchronous parse response keeps the v1-compatible `text`, `chars`, and
`elapsed_sec` fields and adds `job_id`, `output_dir`, `artifacts`, and
`result_url`.
