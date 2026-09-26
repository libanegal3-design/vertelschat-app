# Vertelschat application

Python 3.12 / FastAPI / SQLAlchemy / Jinja. One image runs as **web** (`uvicorn vertelschat.web.app:app`) and
**worker** (`python -m vertelschat.worker`). See `../documentation/` for the full guides.

```bash
pip install -r requirements-dev.txt        # plus ffmpeg on the system
make seed                                  # demo families, created through signed webhooks and the real jobs
make dev                                   # http://localhost:8000/dev  (simulator, mail, jobs, shop, time travel)
make test                                  # 40 tests: WhatsApp error cases, access, book, archive, Shopify
```

Folder map: `vertelschat/handlers.py` (WhatsApp pipeline), `whatsapp.py` (Cloud API + webhook ingestion),
`copy_nl.py` (every storyteller-facing sentence), `prompts.py` + `data/prompts.nl.json` (165 questions),
`ai.py` (transcription, editor, fabrication guard), `book.py` (print PDF), `exports.py` (archive),
`scheduler.py`, `services.py`, `shopify.py`, `web/` (routes, templates, static).
