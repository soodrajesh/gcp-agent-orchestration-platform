"""Observability: structured JSON logs (Cloud Logging -> BigQuery cost record) and Cloud Trace.

Logs: one JSON object per line on stdout. Cloud Run turns `severity` into the log severity and every
other key into `jsonPayload`, so `event="step_usage"` rows can be routed to BigQuery by a log sink
and queried as a per-step cost ledger. Nothing here logs prompts, tool results or alert text.

Traces: spans for each advance() and each step, plus ADK's own spans, exported to Cloud Trace.
httpx and Starlette are instrumented so one trace follows a run across orchestrator -> analyst -> MCP.
"""

from __future__ import annotations

import json
import logging
import os
import sys

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, project: str | None = None) -> None:
        super().__init__()
        self._project = project

    def format(self, record: logging.LogRecord) -> str:
        out = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        out.update({k: v for k, v in record.__dict__.items() if k not in _STD})
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        if self._project:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx.is_valid:  # lets Cloud Logging link the log line to its trace
                out["logging.googleapis.com/trace"] = (
                    f"projects/{self._project}/traces/{ctx.trace_id:032x}"
                )
                out["logging.googleapis.com/spanId"] = f"{ctx.span_id:016x}"
        return json.dumps(out, default=str)


def setup_logging(project: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(project))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def setup_tracing(service_name: str, app=None) -> None:
    """Export spans to Cloud Trace when ENABLE_TRACING=1. Safe to call once per process."""
    if os.environ.get("ENABLE_TRACING") != "1":
        return
    from google.adk.telemetry.google_cloud import get_gcp_exporters, get_gcp_resource
    from google.adk.telemetry.setup import maybe_set_otel_providers
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource

    project = os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    # Extend ADK's GCP resource instead of replacing it: the Telemetry API rejects spans (HTTP 400)
    # whose resource lacks `gcp.project_id` (found live when spans were silently dropped).
    resource = get_gcp_resource(project).merge(Resource.create({"service.name": service_name}))
    maybe_set_otel_providers([get_gcp_exporters(enable_cloud_tracing=True)], otel_resource=resource)
    HTTPXClientInstrumentor().instrument()
    if app is not None:
        from opentelemetry.instrumentation.starlette import StarletteInstrumentor

        StarletteInstrumentor.instrument_app(app)


def init(service_name: str, app=None) -> None:
    """One call at service start: JSON logs always, traces if enabled."""
    setup_logging(os.environ.get("PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    setup_tracing(service_name, app)
