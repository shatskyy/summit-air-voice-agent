"""Where each model runs: OpenAI models go straight to OpenAI on OPENAI_API_KEY, and nothing else
runs at all.

LiveKit Inference bills every model against one account-wide credit ($2.50 a month on the free
plan). The simulated calls used it up on 2026-09-27, and when it hits zero every model on it stops
at once, the fallback included. So a model that isn't OpenAI's, or a missing key, is an error at
startup rather than a quiet switch to that credit.
"""

import os

from livekit.agents import llm
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.plugins import openai


class OneToolAtATime(openai.LLM):
    """OpenAI's model, told to send one tool call per turn whenever it has tools. Every parallel
    pair the calls produced did harm: a callback filed beside an address check (call KTWmzz),
    end_call beside a task (call 2), and two bookings at once in simulation (change_window). A
    second tool costs one more round trip. A request with no tools (the simulated caller, the test
    judge) is left alone, because OpenAI rejects the setting without tools."""

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        **kwargs,
    ) -> llm.LLMStream:
        if tools:
            parallel_tool_calls = False
        return super().chat(
            chat_ctx=chat_ctx, tools=tools, parallel_tool_calls=parallel_tool_calls, **kwargs
        )


def make_llm(model: str):
    """`model` is a LiveKit-style id such as "openai/gpt-4.1-mini"."""
    provider, _, name = model.partition("/")
    if provider != "openai" or not name:
        raise ValueError(f"{model!r} is not an OpenAI model; no model runs on LiveKit Inference")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(f"OPENAI_API_KEY is not set, so {model} can't run")
    return OneToolAtATime(model=name)
