"""Spend and balance monitor for the Summit Air agent's metered providers.

    uv run python scripts/usage.py                    # print the report
    uv run python scripts/usage.py --alert            # also log a row and page on low balances
    uv run python scripts/usage.py --set-openai-balance 14.81   # after a top-up

Reads keys from .env.local. Each provider is optional and reports what its key allows:

- OpenAI: spend from the organization Costs API, which needs OPENAI_ADMIN_KEY (sk-admin-...).
  No API returns the prepaid credit balance, so the balance is estimated as the last balance read
  from the dashboard minus spend since then. Record a new reading with --set-openai-balance.
- Twilio: balance from TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.
- Deepgram: credit balance from DEEPGRAM_ADMIN_KEY, a key with billing:read. The agent's own
  DEEPGRAM_API_KEY is tried as well but usually lacks that scope.
- Local call records: every call in the SQLite database stores its token and audio usage, priced
  here at list rates. This covers real calls only, never simulator or test traffic.

With --alert, a balance that falls below its floor pages once through ntfy (USAGE_NTFY_TOPIC, else
NTFY_TOPIC), and pages again only after it has recovered above the floor and fallen again.
"""

import argparse
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "usage-state.json"
HISTORY = ROOT / "logs" / "usage.tsv"
LOCAL = ZoneInfo("America/New_York")

# Page when a balance drops below these (USD).
FLOORS = {"openai": 2.00, "twilio": 5.00, "deepgram": 20.00}

# OpenAI spend is summed from this date so the balance estimate survives month boundaries.
SPEND_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)

# The dashboard read on 2026-09-27: $4.81 of credit with $0.60 spent since the epoch.
DEFAULT_OPENAI_BASELINE = {"balance": 4.81, "spend": 0.60, "read_at": "2026-09-27T22:45:00Z"}

# List prices in USD. LLM rates are per million tokens, confirmed on the LiveKit billing page
# 2026-09-27; Deepgram rates are its pay-as-you-go list prices.
LLM_RATES = {
    "gpt-4.1-mini": {"input": 0.40, "cached": 0.10, "output": 1.60},
    "openai/gpt-4.1-mini": {"input": 0.40, "cached": 0.10, "output": 1.60},
    "google/gemma-4-31b-it": {"input": 0.40, "cached": 0.20, "output": 1.20},
}
STT_PER_MINUTE = {"nova-3": 0.0077}
TTS_PER_1K_CHARS = {"aura-2-thalia-en": 0.030}


def load_env() -> None:
    """Read .env.local into the environment without overriding anything already set."""
    path = ROOT / ".env.local"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def fetch_json(url: str, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def http_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = json.loads(exc.read())
            detail = body.get("error", body)
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("category") or detail
            return f"HTTP {exc.code}: {detail}"
        except (json.JSONDecodeError, ValueError, AttributeError):
            return f"HTTP {exc.code}"
    return str(exc)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"openai_baseline": DEFAULT_OPENAI_BASELINE, "below_floor": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


# --- OpenAI ------------------------------------------------------------------------------------


def openai_daily_costs(admin_key: str) -> dict[str, float]:
    """Spend per UTC day since SPEND_EPOCH, from the organization Costs API."""
    days: dict[str, float] = {}
    page = None
    while True:
        url = (
            "https://api.openai.com/v1/organization/costs"
            f"?start_time={int(SPEND_EPOCH.timestamp())}&bucket_width=1d&limit=180"
        )
        if page:
            url += f"&page={page}"
        body = fetch_json(url, {"Authorization": f"Bearer {admin_key}"})
        for bucket in body.get("data", []):
            day = datetime.fromtimestamp(bucket["start_time"], UTC).date().isoformat()
            total = sum(float(r["amount"]["value"]) for r in bucket.get("results", []))
            days[day] = days.get(day, 0.0) + total
        if not body.get("has_more"):
            return days
        page = body.get("next_page")


def openai_report(state: dict) -> dict:
    key = os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        return {"error": "no OPENAI_ADMIN_KEY in .env.local (Settings > Admin keys)"}
    try:
        days = openai_daily_costs(key)
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {"error": http_error(exc)}
    now = datetime.now(UTC)
    today = now.date().isoformat()
    month = now.strftime("%Y-%m")
    since_epoch = sum(days.values())
    baseline = state.get("openai_baseline", DEFAULT_OPENAI_BASELINE)
    return {
        "today": days.get(today, 0.0),
        "last_7_days": sum(
            v for d, v in days.items() if d >= (now - timedelta(days=6)).date().isoformat()
        ),
        "month": sum(v for d, v in days.items() if d.startswith(month)),
        "since_epoch": since_epoch,
        "balance": baseline["balance"] - (since_epoch - baseline["spend"]),
        "baseline_read_at": baseline["read_at"],
    }


# --- Twilio and Deepgram -----------------------------------------------------------------------


def twilio_report() -> dict:
    sid, token = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return {"error": "no TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN in .env.local"}
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    try:
        body = fetch_json(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Balance.json",
            {"Authorization": f"Basic {auth}"},
        )
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        return {"error": http_error(exc)}
    return {"balance": float(body["balance"])}


def deepgram_report() -> dict:
    key = os.getenv("DEEPGRAM_ADMIN_KEY") or os.getenv("DEEPGRAM_API_KEY")
    if not key:
        return {"error": "no DEEPGRAM_ADMIN_KEY in .env.local"}
    headers = {"Authorization": f"Token {key}"}
    try:
        projects = fetch_json("https://api.deepgram.com/v1/projects", headers)["projects"]
        total = 0.0
        for project in projects:
            body = fetch_json(
                f"https://api.deepgram.com/v1/projects/{project['project_id']}/balances", headers
            )
            total += sum(float(b["amount"]) for b in body.get("balances", []))
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        message = http_error(exc)
        if "INSUFFICIENT_PERMISSIONS" in message or "scope" in message or "403" in message:
            message = "key lacks billing:read; add DEEPGRAM_ADMIN_KEY (Owner or Admin role key)"
        return {"error": message}
    return {"balance": total}


# --- Local call records ------------------------------------------------------------------------


def price_usage(entries: list[dict]) -> tuple[float, set[str]]:
    """Cost of one call's usage entries, plus the models no rate covers."""
    cost, unpriced = 0.0, set()
    for u in entries:
        model = u.get("model", "")
        if "input_tokens" in u:
            rate = LLM_RATES.get(model)
            if not rate:
                unpriced.add(model)
                continue
            cached = u.get("input_cached_tokens", 0)
            uncached = max(u.get("input_tokens", 0) - cached, 0)
            cost += (
                uncached * rate["input"]
                + cached * rate["cached"]
                + u.get("output_tokens", 0) * rate["output"]
            ) / 1e6
        elif "characters_count" in u:
            if model not in TTS_PER_1K_CHARS:
                unpriced.add(model)
                continue
            cost += u["characters_count"] / 1000 * TTS_PER_1K_CHARS[model]
        elif "audio_duration" in u:
            if model not in STT_PER_MINUTE:
                unpriced.add(model)
                continue
            cost += u["audio_duration"] / 60 * STT_PER_MINUTE[model]
    return cost, unpriced


def local_report() -> dict:
    db = Path(os.getenv("SUMMIT_AIR_DB") or ROOT / "data" / "summit-air.db")
    if not db.exists():
        return {"error": f"no database at {db}"}
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    calls = calls_24h = 0
    total = total_24h = 0.0
    unpriced: set[str] = set()
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        for started_at, report in conn.execute("select started_at, report_json from calls"):
            try:
                usage = json.loads(report or "{}").get("usage") or []
            except json.JSONDecodeError:
                continue
            cost, missing = price_usage(usage)
            unpriced |= missing
            calls += 1
            total += cost
            if started_at >= cutoff:
                calls_24h += 1
                total_24h += cost
    return {
        "calls": calls,
        "cost": total,
        "per_call": total / calls if calls else 0.0,
        "calls_24h": calls_24h,
        "cost_24h": total_24h,
        "unpriced": sorted(unpriced),
    }


# --- Output and alerts -------------------------------------------------------------------------


def money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.2f}"


def print_report(openai: dict, twilio: dict, deepgram: dict, local: dict) -> None:
    print(f"Summit Air usage, {datetime.now(LOCAL).strftime('%Y-%m-%d %H:%M')}")
    print()
    if "error" in openai:
        print(f"OpenAI     {openai['error']}")
    else:
        flag = "  LOW" if openai["balance"] < FLOORS["openai"] else ""
        print(
            f"OpenAI     balance ~{money(openai['balance'])}{flag}   today {money(openai['today'])}"
            f"   7 days {money(openai['last_7_days'])}   month {money(openai['month'])}"
        )
        print(
            f"           (estimate: dashboard reading of {openai['baseline_read_at']} minus spend since)"
        )
    if "error" in twilio:
        print(f"Twilio     {twilio['error']}")
    else:
        flag = "  LOW" if twilio["balance"] < FLOORS["twilio"] else ""
        print(f"Twilio     balance {money(twilio['balance'])}{flag}")
    if "error" in deepgram:
        print(f"Deepgram   {deepgram['error']}")
    else:
        flag = "  LOW" if deepgram["balance"] < FLOORS["deepgram"] else ""
        print(f"Deepgram   credit {money(deepgram['balance'])}{flag}")
    if "error" in local:
        print(f"Calls      {local['error']}")
    else:
        print(
            f"Calls      {local['calls_24h']} in 24 h ({money(local['cost_24h'])}),"
            f" {local['calls']} stored ({money(local['cost'])}, ~${local['per_call']:.4f} a call)"
        )
        if local["unpriced"]:
            print(f"           not priced: {', '.join(local['unpriced'])}")


def page(title: str, message: str) -> None:
    topic = os.getenv("USAGE_NTFY_TOPIC") or os.getenv("NTFY_TOPIC")
    if not topic:
        print(f"no ntfy topic, page skipped: {title}", file=sys.stderr)
        return
    request = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        headers={"Title": title, "Priority": "high", "Tags": "moneybag"},
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"ntfy page failed: {http_error(exc)}", file=sys.stderr)


def check_floors(state: dict, balances: dict[str, float | None]) -> None:
    below = state.setdefault("below_floor", {})
    for name, balance in balances.items():
        if balance is None:
            continue
        low = balance < FLOORS[name]
        if low and not below.get(name):
            page(
                f"{name.title()} balance low: {money(balance)}",
                f"{name.title()} is at {money(balance)}, under the {money(FLOORS[name])} floor. "
                "At $0 the Summit Air agent loses this provider.",
            )
        below[name] = low


def append_history(openai: dict, twilio: dict, deepgram: dict, local: dict) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    new = not HISTORY.exists()
    with HISTORY.open("a") as f:
        if new:
            f.write(
                "time\topenai_today\topenai_month\topenai_balance\ttwilio_balance"
                "\tdeepgram_balance\tcalls_24h\tcalls_cost_24h\n"
            )
        row = [
            datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            openai.get("today"),
            openai.get("month"),
            openai.get("balance"),
            twilio.get("balance"),
            deepgram.get("balance"),
            local.get("calls_24h"),
            local.get("cost_24h"),
        ]
        f.write(
            "\t".join(
                "" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v)) for v in row
            )
            + "\n"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--alert", action="store_true", help="log a history row and page on low balances"
    )
    parser.add_argument(
        "--set-openai-balance",
        type=float,
        metavar="USD",
        help="record the credit balance shown on the OpenAI billing page now",
    )
    args = parser.parse_args()
    load_env()
    state = load_state()

    if args.set_openai_balance is not None:
        key = os.getenv("OPENAI_ADMIN_KEY")
        if not key:
            sys.exit(
                "--set-openai-balance needs OPENAI_ADMIN_KEY, to record spend at the same moment"
            )
        spend = sum(openai_daily_costs(key).values())
        state["openai_baseline"] = {
            "balance": args.set_openai_balance,
            "spend": spend,
            "read_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        state.setdefault("below_floor", {})["openai"] = False
        save_state(state)
        print(
            f"OpenAI baseline set: {money(args.set_openai_balance)} at {money(spend)} spent since "
            f"{SPEND_EPOCH.date()}"
        )
        return

    openai, twilio, deepgram, local = (
        openai_report(state),
        twilio_report(),
        deepgram_report(),
        local_report(),
    )
    print_report(openai, twilio, deepgram, local)
    if args.alert:
        append_history(openai, twilio, deepgram, local)
        check_floors(
            state,
            {
                "openai": openai.get("balance"),
                "twilio": twilio.get("balance"),
                "deepgram": deepgram.get("balance"),
            },
        )
        save_state(state)


if __name__ == "__main__":
    main()
