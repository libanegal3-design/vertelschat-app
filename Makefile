.PHONY: dev seed test worker voices
dev:        ## run the app with the inline worker and dev tools
	VT_INLINE_WORKER=1 uvicorn vertelschat.web.app:app --reload --port 8000
seed:       ## wipe local data and build the demo families through the real pipeline
	python -m vertelschat.seed --reset
worker:
	python -m vertelschat.worker
test:
	python -m pytest
voices:     ## regenerate the demo voice notes and fixtures
	python scripts/make_voice_notes.py && python scripts/make_demo_photos.py
