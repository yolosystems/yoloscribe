# S0.1 — Runtime Conformance Harness (R1-R7). See
# projects/yoloscribe/ideas/runtime-conformance-contract and
# projects/yoloscribe/feature-backlog/agent-runtime-rearchitecture in the wiki.

.PHONY: conformance conformance-live

# Fast, in-process checks — no docker required. R1 and R3b are skipped (they
# need live MinIO/dynamodb-local) and reported as such.
conformance:
	cd agent-runner && uv run pytest tests/conformance -m conformance -v -s

# Full R1-R7 session against live MinIO/ElasticMQ/dynamodb-local. Regenerates
# agent-runner/tests/conformance/BASELINE.md on completion.
conformance-live:
	docker compose up -d minio minio-init dynamodb-local dynamodb-init elasticmq
	cd agent-runner && uv run pytest tests/conformance -m "conformance or conformance_live" -v -s
