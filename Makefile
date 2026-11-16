IMAGE    ?= lectern
TAG      ?= latest
# UGREEN DXP NAS models (and most NASes) are x86_64
PLATFORM ?= linux/amd64

.PHONY: test dev build build-nas save

test:
	{ [ -d .venv ] || python3.12 -m venv .venv; } && .venv/bin/pip install -q -r requirements.txt pytest && .venv/bin/python -m pytest -q tests

## Run locally against ./media on :8000 with a random key (printed)
dev:
	@KEY=$$(openssl rand -hex 24); echo "LECTERN_API_KEY=$$KEY"; \
	LECTERN_DATA_DIR=$(CURDIR)/.data LECTERN_MEDIA_ROOTS=$(CURDIR)/media LECTERN_API_KEY=$$KEY \
	.venv/bin/uvicorn lectern.api:app --port 8000

build:
	docker build -t $(IMAGE):$(TAG) .

build-nas:
	docker buildx build --platform $(PLATFORM) --provenance=false --sbom=false -t $(IMAGE):$(TAG) --load .

## Export the NAS image to a .tar you can import in the NAS Docker app
save: build-nas
	docker save $(IMAGE):$(TAG) -o $(IMAGE)-$(TAG).tar
