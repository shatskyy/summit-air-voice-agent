"""Watchdog for the Summit Air phone line. launchd runs it every 5 minutes (ops/launchd/).

    uv run python scripts/watchdog.py            # one check; pages only when something changed
    uv run python scripts/watchdog.py --dry-run  # one check, print instead of paging

Each run proves the worker answers, not just that a process exists: it dispatches the summit-air
agent into a throwaway room `health-<timestamp>` with the metadata {"healthcheck": true}, waits up to
15 s for the worker to write a heartbeat row for that room into the store (src/agent.py), then
deletes the room. It also checks the Mac is on AC power, since the laptop is the host.

It pages through ntfy (WATCHDOG_NTFY_TOPIC, else NTFY_TOPIC) only when a state changes, worker up to
down or back, AC to battery or back, plus one "all good" at the first good run after 9 AM each day.
The last state lives in data/watchdog-state.json.
"""

import argparse
import asyncio
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
from livekit import api

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")

store = importlib.import_module("store")  # from src/, once it is on the path

AGENT_NAME = "summit-air"
DB = Path(os.getenv("SUMMIT_AIR_DB", ROOT / "data" / "summit-air.db"))
STATE = ROOT / "data" / "watchdog-state.json"
TZ = ZoneInfo("America/New_York")
WAIT_SECONDS = 15
DAILY_HOUR = 9


async def probe_worker() -> tuple[bool, str]:
    """Dispatch a health-check job and wait for its heartbeat. (answered, detail)."""
    room = f"health-{int(time.time())}"
    started = time.monotonic()
    async with api.LiveKitAPI() as lk:
        try:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=AGENT_NAME,
                    room=room,
                    metadata=json.dumps({"healthcheck": True}),
                )
            )
            while time.monotonic() - started < WAIT_SECONDS:
                try:
                    if await asyncio.to_thread(store.heartbeat_at, DB, room):
                        return True, f"answered in {time.monotonic() - started:.1f} s"
                except sqlite3.OperationalError:
                    pass  # no heartbeats table until the worker's first write, or a busy lock
                await asyncio.sleep(0.5)
            return False, f"no heartbeat within {WAIT_SECONDS} s"
        except (api.TwirpError, aiohttp.ClientError, TimeoutError) as exc:
            return False, f"dispatch failed: {exc}"
        finally:
            try:
                await lk.room.delete_room(api.DeleteRoomRequest(room=room))
            except (api.TwirpError, aiohttp.ClientError, TimeoutError) as exc:
                print(f"could not delete {room}: {exc}", file=sys.stderr)


def on_ac_power() -> bool | None:
    """True on AC, False on battery, None if pmset can't say."""
    try:
        out = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "AC Power" in out:
        return True
    if "Battery Power" in out:
        return False
    return None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def page(title: str, message: str, priority: str, dry_run: bool) -> None:
    topic = os.getenv("WATCHDOG_NTFY_TOPIC") or os.getenv("NTFY_TOPIC")
    if dry_run or not topic:
        print(f"[{'dry run' if dry_run else 'no ntfy topic'}] {title}: {message}")
        return
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        headers={"Title": title, "Priority": priority, "Tags": "telephone_receiver"},
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"ntfy page failed: {exc}", file=sys.stderr)


def pages_for(previous: dict, current: dict, at: datetime) -> list[tuple[str, str, str]]:
    """What to send, as (title, message, priority). Changes only, plus the morning all-good."""
    out = []
    worker, was_worker = current["worker_up"], previous.get("worker_up")
    if was_worker is not None and worker != was_worker:
        if worker:
            out.append(("Summit Air line is back", current["worker_detail"], "high"))
        else:
            out.append(("Summit Air line is DOWN", current["worker_detail"], "urgent"))
    elif was_worker is None and not worker:
        out.append(("Summit Air line is DOWN", current["worker_detail"], "urgent"))
    power, was_power = current["on_ac"], previous.get("on_ac")
    if power is False and was_power is not False:
        out.append(("Summit Air host on battery", "The laptop is not on AC power.", "high"))
    elif power is True and was_power is False:
        out.append(("Summit Air host back on AC", "The laptop is on AC power again.", "default"))
    today = at.date().isoformat()
    healthy = worker and power is not False
    if healthy and at.hour >= DAILY_HOUR and previous.get("all_good_sent") != today:
        out.append(("Summit Air all good", f"Worker {current['worker_detail']}, on AC.", "low"))
        current["all_good_sent"] = today
    else:
        current["all_good_sent"] = previous.get("all_good_sent")
    return out


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="print pages instead of sending")
    args = parser.parse_args()

    at = datetime.now(TZ)
    worker_up, detail = await probe_worker()
    current = {
        "checked_at": at.isoformat(timespec="seconds"),
        "worker_up": worker_up,
        "worker_detail": detail,
        "on_ac": on_ac_power(),
    }
    previous = load_state()
    for title, message, priority in pages_for(previous, current, at):
        page(title, message, priority, args.dry_run)
    if not args.dry_run:
        save_state(current)
    print(
        f"{at:%Y-%m-%d %H:%M} worker {'up' if worker_up else 'DOWN'} ({detail}), "
        f"power {'AC' if current['on_ac'] else 'battery' if current['on_ac'] is False else '?'}"
    )
    return 0 if worker_up else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
