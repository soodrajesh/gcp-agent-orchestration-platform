"""Token prices in USD per 1M tokens (Standard tier), from https://ai.google.dev/gemini-api/docs/pricing
as read on 2026-10-18. Output prices include thinking tokens. Re-check before relying on them: the
3.x Flash prices are scheduled to double on 2027-01-01, which is why 2.5 Flash is the default."""

from __future__ import annotations

from .models import Usage

PRICES: dict[str, tuple[float, float]] = {  # model -> (input, output)
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),  # <=200k-token prompts
    # OpenAI, for cross-vendor evals only; https://developers.openai.com/api/docs/pricing as read 2026-10-18
    "openai/gpt-5-nano": (0.05, 0.40),
    "fake": (0.0, 0.0),
}


def usage_for(model: str, input_tokens: int, output_tokens: int) -> Usage:
    if model not in PRICES:
        raise KeyError(f"no price for model {model!r}; add it to agentorch/pricing.py")
    p_in, p_out = PRICES[model]
    cost = (input_tokens * p_in + output_tokens * p_out) / 1_000_000
    return Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
