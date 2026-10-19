"""Provider-agnostic model selection. The agent code only ever receives what this returns.

  "gemini-2.5-flash-lite"      -> passed through; ADK talks to Gemini (Vertex or API key)
  "openai/gpt-5-nano"          -> ADK LiteLlm adapter (needs the `openai` extra; test/eval use only)

Deployed services use Gemini. The OpenAI path exists so evals can show whether a result is about the
architecture or about one vendor; it is never installed in the deployed image."""

from __future__ import annotations


def resolve_model(spec: str):
    if spec.startswith(("openai/", "litellm/")):
        try:
            from google.adk.models.lite_llm import LiteLlm
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "non-Gemini models need the `openai` extra: pip install '.[openai]'"
            ) from exc
        return LiteLlm(model=spec)
    return spec
