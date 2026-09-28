"""Simulated calls: the call scripts, played in text against the real prompt, tools and store.

    uv run python -m evals                          # every scenario on its clocks, GPT-4.1 mini, 1 run
    uv run python -m evals gas no_show -n 2 --clock night
    uv run python -m evals --compare evals/results/<file>.json

See evals/cli.py for the options. Every run is priced and written to the spend ledger
(evals/spend.json) and refused up front if it would overrun it.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")

# A simulated emergency must never page a real phone. Every topic the code can page is unset here,
# before any module that reads one is imported.
PAGE_TOPICS = ("NTFY_TOPIC", "WATCHDOG_NTFY_TOPIC", "DISPATCH_NTFY_TOPIC")
for _topic in PAGE_TOPICS:
    os.environ.pop(_topic, None)
