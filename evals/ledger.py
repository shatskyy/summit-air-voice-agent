"""The spend ledger: every model-calling run records what it cost, and none may start if it could
push the total past the cap.

evals/spend.json is committed, so the total survives across sessions. Both the simulator and
`pytest -m llm` go through check() before they run and record() after.
"""

import importlib.util
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "evals" / "spend.json"
TZ = ZoneInfo("America/New_York")

CAP = 3.00  # USD, simulations and `pytest -m llm` together (docs: phase 2 plan, hard rule 2)
BALANCE_FLOOR = 1.50  # below this estimated OpenAI balance, the phone line needs what is left
DEFAULT_PER_CONVERSATION = 0.03  # until a run has measured one


class Refused(Exception):
    """A run that must not start. The message says why."""


def load(path: Path = LEDGER) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"cap": CAP, "total": 0.0, "runs": []}


def total(ledger: dict) -> float:
    return round(sum(r["cost"] for r in ledger["runs"]), 6)


def per_conversation(ledger: dict) -> float:
    """Measured average cost of one simulated conversation, or the default before any."""
    runs = [r for r in ledger["runs"] if r["kind"] == "eval" and r["conversations"]]
    n = sum(r["conversations"] for r in runs)
    return sum(r["cost"] for r in runs) / n if n else DEFAULT_PER_CONVERSATION


def openai_balance() -> tuple[float | None, str]:
    """The balance estimate scripts/usage.py computes, or None and the reason it has none."""
    spec = importlib.util.spec_from_file_location("usage", ROOT / "scripts" / "usage.py")
    usage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usage)
    # usage.py loads .env.local into the environment, which would put back the ntfy topics the
    # simulator and the tests unset. Anything it adds is removed again.
    before = set(os.environ)
    try:
        usage.load_env()
        report = usage.openai_report(usage.load_state())
    finally:
        for key in set(os.environ) - before:
            del os.environ[key]
    if "error" in report:
        return None, report["error"]
    return report["balance"], "estimated by scripts/usage.py"


def budget_for(ledger: dict, budget: float | None) -> float:
    """What this run may spend: --budget if given, else what is left under the cap. Never more
    than what is left under the cap."""
    left = CAP - total(ledger)
    return min(budget, left) if budget is not None else left


def check(estimate: float, budget: float | None = None, path: Path = LEDGER) -> float:
    """Print the estimate, the ledger and the balance, and raise Refused if the run must not start.
    Returns the spend this run may reach before it stops launching conversations."""
    ledger = load(path)
    spent = total(ledger)
    allowed = budget_for(ledger, budget)
    balance, source = openai_balance()
    shown = f"${balance:.2f} ({source})" if balance is not None else f"unknown: {source}"
    print(
        f"Estimate ${estimate:.2f}. Ledger ${spent:.2f} of ${CAP:.2f}; "
        f"this run may spend ${allowed:.2f}. OpenAI balance {shown}."
    )
    if estimate > allowed:
        raise Refused(f"estimate ${estimate:.2f} is over the ${allowed:.2f} this run may spend")
    if balance is None:
        print("Balance check skipped: scripts/usage.py could not read a balance.")
    elif balance < BALANCE_FLOOR:
        raise Refused(
            f"OpenAI balance ~${balance:.2f} is under ${BALANCE_FLOOR:.2f}; the phone line needs it"
        )
    return allowed


def commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
    )
    return out.stdout.strip() or "unknown"


def record(
    kind: str,
    label: str,
    cost: float,
    conversations: int = 0,
    estimate: float = 0.0,
    results: str = "",
    path: Path = LEDGER,
) -> float:
    """Append a run and return the new total."""
    ledger = load(path)
    ledger["runs"].append(
        {
            "at": datetime.now(TZ).isoformat(timespec="seconds"),
            "commit": commit(),
            "kind": kind,
            "label": label,
            "conversations": conversations,
            "estimate": round(estimate, 4),
            "cost": round(cost, 6),
            "results": results,
        }
    )
    ledger["cap"] = CAP
    ledger["total"] = total(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2) + "\n")
    return ledger["total"]
