"""Where each model runs. OpenAI models go straight to OpenAI on OPENAI_API_KEY; anything else runs
on LiveKit Inference.

LiveKit Inference bills every model against one account-wide credit ($2.50 a month on the free
plan). The simulated calls used it up on 2026-09-27, and when it hits zero every model on it stops
at once, the fallback included. A direct key has no monthly cap.
"""

import os

from livekit.agents import inference
from livekit.plugins import openai


def make_llm(model: str):
    """`model` is a LiveKit-style id such as "openai/gpt-4.1-mini" or "google/gemma-4-31b-it"."""
    provider, _, name = model.partition("/")
    if provider == "openai" and os.getenv("OPENAI_API_KEY"):
        return openai.LLM(model=name)
    return inference.LLM(model=model)
