"""OTEL telemetry setup for the YoloScribe backend.

Reads standard OTEL env vars (OTEL_EXPORTER_OTLP_ENDPOINT,
OTEL_EXPORTER_OTLP_HEADERS) automatically — no additional config needed
beyond setting those vars. Wraps the OTLP exporter in a sanitizer that
redacts API token values before they reach the trace backend.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r'\bas_[0-9a-f]{64}\b')
_SANITIZED_ATTRS = ("gen_ai.choice",)


class _SanitizingExporter:
    """Wraps an OTLP exporter and redacts AS_ API tokens from span attributes."""

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped

    def export(self, spans):
        for span in spans:
            attrs = getattr(span, "_attributes", None)
            if not attrs:
                continue
            for key in _SANITIZED_ATTRS:
                val = attrs.get(key)
                if val and isinstance(val, str) and "as_" in val:
                    try:
                        attrs[key] = _TOKEN_RE.sub("[REDACTED]", val)
                    except Exception:
                        pass
        try:
            return self._wrapped.export(spans)
        except Exception:
            log.debug("OTEL span export failed", exc_info=True)
            from opentelemetry.sdk.trace.export import SpanExportResult
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._wrapped.force_flush(timeout_millis)


def setup_telemetry() -> None:
    """Initialize OTEL tracing if OTEL_EXPORTER_OTLP_ENDPOINT is set.

    No-ops silently when the env var is absent or the otel extra is not installed.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(_SanitizingExporter(OTLPSpanExporter()))
        )
        trace.set_tracer_provider(provider)
        log.info("OTEL tracing enabled → %s", endpoint)
    except ImportError:
        log.warning("strands-agents[otel] not installed — tracing disabled")
    except Exception:
        log.warning("Failed to initialize OTEL telemetry", exc_info=True)
