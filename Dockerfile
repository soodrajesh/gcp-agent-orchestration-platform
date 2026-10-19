# One image, four entrypoints (orchestrator, incident_analyst, mcp_ops, demo_target); Cloud Run picks via `args`.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY agentorch ./agentorch
RUN pip install . && useradd --uid 10001 --no-create-home app
USER app
ENV PORT=8080
CMD ["sh", "-c", "exec uvicorn agentorch.service:app --host 0.0.0.0 --port ${PORT}"]
