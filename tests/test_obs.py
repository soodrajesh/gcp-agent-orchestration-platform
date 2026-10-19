import io
import json
import logging

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentorch import engine as engine_mod
from agentorch.engine import Engine, Step, StepResult, Workflow
from agentorch.models import Usage
from agentorch.obs import JsonFormatter
from agentorch.store import InMemoryRunStore

SECRET_ALERT = "customer-pii-4111111111111111 in the alert text"


def wf():
    async def a(ctx):
        return StepResult(
            "model said: " + SECRET_ALERT,
            Usage(input_tokens=120, output_tokens=30, cost_usd=0.00042),
        )

    return {"wf": Workflow("wf", (Step("diagnose", a),))}


def capture():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(JsonFormatter(project="p"))
    lg = logging.getLogger("agentorch.engine")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    return buf, h, lg


async def test_each_step_emits_one_cost_ledger_row_with_tokens_and_cost():
    buf, h, lg = capture()
    try:
        eng = Engine(InMemoryRunStore(), wf())
        run = await eng.advance((await eng.start("wf", {"alert": SECRET_ALERT})).id)
    finally:
        lg.removeHandler(h)
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    ledger = [r for r in rows if r.get("event") == "step_usage"]
    assert len(ledger) == 1
    assert ledger[0] | {"severity": "INFO"} == ledger[0]
    assert (ledger[0]["run_id"], ledger[0]["step"], ledger[0]["workflow"]) == (
        run.id,
        "diagnose",
        "wf",
    )
    assert (ledger[0]["input_tokens"], ledger[0]["output_tokens"], ledger[0]["cost_usd"]) == (
        120,
        30,
        0.00042,
    )


async def test_ledger_never_contains_prompt_alert_or_model_text():
    buf, h, lg = capture()
    try:
        eng = Engine(InMemoryRunStore(), wf())
        await eng.advance((await eng.start("wf", {"alert": SECRET_ALERT})).id)
    finally:
        lg.removeHandler(h)
    assert "customer-pii" not in buf.getvalue() and "model said" not in buf.getvalue()


def test_formatter_emits_json_with_severity_and_exception():
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom %s", ("now",), None)
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        rec.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(rec))
    assert (
        out["severity"] == "ERROR"
        and out["message"] == "boom now"
        and "ValueError" in out["exception"]
    )


async def test_steps_and_trace_ids_appear_as_spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(engine_mod, "tracer", provider.get_tracer("test"))
    eng = Engine(InMemoryRunStore(), wf())
    run = await eng.advance((await eng.start("wf", {})).id)
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["step diagnose"]
    assert spans[0].attributes["run.id"] == run.id and spans[0].attributes["step.key"] == "diagnose"


def test_log_lines_carry_the_trace_id_when_a_span_is_active():
    provider = TracerProvider()
    tracer = provider.get_tracer("t")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", (), None)
    with tracer.start_as_current_span("s") as span:
        out = json.loads(JsonFormatter(project="proj").format(rec))
        tid = f"{span.get_span_context().trace_id:032x}"
    assert out["logging.googleapis.com/trace"] == f"projects/proj/traces/{tid}"


def test_tracing_setup_builds_the_real_exporter_pipeline(monkeypatch):
    """Regression: the deployed images crashed at startup because ADK's Cloud Trace exporter needs a
    package we had not declared, and every other test replaced the tracer so nothing built it.
    This constructs the real pipeline (fake credentials, no network)."""
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from opentelemetry import trace

    monkeypatch.setenv("ENABLE_TRACING", "1")
    monkeypatch.setattr(
        google.auth, "default", lambda *a, **k: (AnonymousCredentials(), "test-project")
    )
    from starlette.applications import Starlette

    from agentorch import obs

    obs.setup_tracing(
        "ao-test", Starlette()
    )  # raises ModuleNotFoundError if a dependency is missing
    provider = trace.get_tracer_provider()
    assert provider is not None
    provider.shutdown()  # do not leave a global exporter (with fake credentials) running for other tests


def test_every_service_entrypoint_imports_with_tracing_dependencies():
    import importlib

    for mod in (
        "agentorch.service",
        "agentorch.mcp_ops",
        "agentorch.agents.incident_analyst",
        "agentorch.demo_target",
        "opentelemetry.exporter.cloud_trace",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "opentelemetry.instrumentation.httpx",
        "opentelemetry.instrumentation.starlette",
    ):
        importlib.import_module(mod)


def test_trace_resource_carries_project_id_and_service_name(monkeypatch):
    """Regression: spans were dropped with HTTP 400 because the resource lacked gcp.project_id."""
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from starlette.applications import Starlette

    from agentorch import obs

    captured = {}
    monkeypatch.setenv("ENABLE_TRACING", "1")
    monkeypatch.setenv("PROJECT_ID", "proj-123")
    monkeypatch.setattr(
        google.auth, "default", lambda *a, **k: (AnonymousCredentials(), "proj-123")
    )
    import google.adk.telemetry.setup as adk_setup

    monkeypatch.setattr(
        adk_setup,
        "maybe_set_otel_providers",
        lambda hooks, otel_resource=None: captured.update(r=otel_resource),
    )
    obs.setup_tracing("ao-test", Starlette())
    attrs = captured["r"].attributes
    assert attrs["gcp.project_id"] == "proj-123" and attrs["service.name"] == "ao-test"
