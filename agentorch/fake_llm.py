"""Scripted ADK model for tests: deterministic, free, and records what it was asked."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class FakeLlm(BaseLlm):
    model: str = "fake"
    replies: list[str | types.Part] = Field(default_factory=list)
    seen: list[str] = Field(default_factory=list)
    tool_results: list[str] = Field(default_factory=list)  # function responses the model was shown

    @staticmethod
    def transfer_to(agent_name: str) -> types.Part:
        """Scripted reply that makes an ADK agent hand the conversation to `agent_name`."""
        return types.Part(
            function_call=types.FunctionCall(
                name="transfer_to_agent", args={"agent_name": agent_name}
            )
        )

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["fake"]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1] if llm_request.contents else None
        for c in llm_request.contents:
            for p in c.parts or []:
                if p.function_response is not None:
                    self.tool_results.append(str(p.function_response.response))
        self.seen.append(" ".join(p.text or "" for p in (last.parts if last else [])))
        reply = self.replies.pop(0) if self.replies else "(no scripted reply)"
        part = reply if isinstance(reply, types.Part) else types.Part(text=reply)
        text = part.text or ""
        yield LlmResponse(
            content=types.Content(role="model", parts=[part]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=len(self.seen[-1]) // 4 + 1,
                candidates_token_count=len(text) // 4 + 1,
                total_token_count=0,
            ),
        )
