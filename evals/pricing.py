"""What a conversation cost, from the tokens OpenAI reported for it.

USD per million tokens, standard tier. Checked 2026-09-27 against OpenAI's pricing page,
https://developers.openai.com/api/docs/pricing ("Standard pricing data", Flagship models).
"""

from dataclasses import dataclass

RATES = {
    "gpt-4.1-mini": {"input": 0.40, "cached": 0.10, "output": 1.60},
    "gpt-4.1": {"input": 2.00, "cached": 0.50, "output": 8.00},
}


@dataclass
class Usage:
    """Tokens one LLM instance used. input includes the cached tokens, as OpenAI reports it."""

    model: str
    input: int = 0
    cached: int = 0
    output: int = 0
    requests: int = 0

    def add(self, metrics) -> None:
        """Fold in one LLMMetrics event (the LLM's "metrics_collected")."""
        self.input += metrics.prompt_tokens
        self.cached += metrics.prompt_cached_tokens
        self.output += metrics.completion_tokens
        self.requests += 1

    @property
    def cost(self) -> float:
        return cost(self.model, self.input, self.cached, self.output)

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "input": self.input,
            "cached": self.cached,
            "output": self.output,
            "requests": self.requests,
            "cost": round(self.cost, 6),
        }


def rate(model: str) -> dict:
    """Rates for "gpt-4.1-mini" or a LiveKit-style "openai/gpt-4.1-mini". Unknown models raise,
    so nothing runs unpriced."""
    name = model.split("/")[-1]
    if name not in RATES:
        raise KeyError(f"no price for {model!r} in evals/pricing.py")
    return RATES[name]


def cost(model: str, input_tokens: int, cached: int, output: int) -> float:
    r = rate(model)
    uncached = max(input_tokens - cached, 0)
    return (uncached * r["input"] + cached * r["cached"] + output * r["output"]) / 1e6


def track(llm_instance, model: str) -> Usage:
    """Count every request `llm_instance` makes from now on."""
    usage = Usage(model)
    llm_instance.on("metrics_collected", usage.add)
    return usage
