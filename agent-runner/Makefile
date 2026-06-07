REGISTRY    := ghcr.io
GITHUB_USER ?= nate-yolodev
IMAGE       := $(REGISTRY)/$(GITHUB_USER)/yoloscribe-agent-runner
LOCAL_IMAGE := $(IMAGE)-local
TAG         ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo latest)
PLATFORM    := linux/amd64,linux/arm64

.PHONY: lock login release release-local

## Generate/update uv.lock (required by the Dockerfile --frozen flag)
lock:
	uv lock

## Log in to ghcr.io using the GHCR_PAT environment variable
login:
	@if [ -z "$$GHCR_PAT" ]; then \
		echo "Error: GHCR_PAT is not set"; exit 1; \
	fi
	@echo "$$GHCR_PAT" | docker login $(REGISTRY) -u $(GITHUB_USER) --password-stdin

## Build for linux/amd64 and push directly to ghcr.io in one step.
## buildx cross-compiles on Apple Silicon via QEMU and pushes both tags atomically.
release: lock login
	docker buildx build \
		--platform $(PLATFORM) \
		--push \
		-t $(IMAGE):$(TAG) \
		-t $(IMAGE):latest \
		-f Dockerfile \
		..

## Build the local-mode image (adds Node.js LTS for STDIO MCP servers) and push to ghcr.io.
## Use this image in docker-compose for local development with local MCP servers.
release-local: lock login
	docker buildx build \
		--platform $(PLATFORM) \
		--push \
		-t $(LOCAL_IMAGE):$(TAG) \
		-t $(LOCAL_IMAGE):latest \
		-f Dockerfile.local \
		..
