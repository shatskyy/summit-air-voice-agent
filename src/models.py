"""Where each model runs: OpenAI models go straight to OpenAI on OPENAI_API_KEY, and nothing else
runs at all.

LiveKit Inference bills every model against one account-wide credit ($2.50 a month on the free
plan). The simulated calls used it up on 2026-09-27, and when it hits zero every model on it stops
at once, the fallback included. So a model that isn't OpenAI's, or a missing key, is an error at
startup rather than a quiet switch to that credit.
"""

import os

from livekit.plugins import openai


def make_llm(model: str):
    """`model` is a LiveKit-style id such as "openai/gpt-4.1-mini"."""
    provider, _, name = model.partition("/")
    if provider != "openai" or not name:
        raise ValueError(f"{model!r} is not an OpenAI model; no model runs on LiveKit Inference")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(f"OPENAI_API_KEY is not set, so {model} can't run")
    # One tool call per model turn. Every parallel pair the phone calls produced did harm: a
    # callback filed beside an address check (call KTWmzz), end_call beside a task (call 2), and
    # two bookings at once in simulation (change_window). A second tool costs one more round trip.
    return openai.LLM(model=name, parallel_tool_calls=False)
