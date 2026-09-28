"""The call sheet: what each call did, from the call_summaries table (src/record.py).

    uv run python scripts/calls.py              # the 20 most recent calls
    uv run python scripts/calls.py -n 50        # more of them
    uv run python scripts/calls.py 1004         # one call, by booking or task reference
    uv run python scripts/calls.py call-_+1650  # one call, by room name or the start of it

Pushes to phones carry only a first name, a ZIP and a reference; this is where the rest is.
Reads SUMMIT_AIR_DB, else data/summit-air.db. Read-only.
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env.local")
DB = Path(os.getenv("SUMMIT_AIR_DB", ROOT / "data" / "summit-air.db"))


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        "select * from call_summaries order by started_at desc limit ?", (limit,)
    ).fetchall()


def find(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    """A call by booking or task reference, or by its room name or the start of it."""
    if key.isdigit():
        for table in ("bookings", "tasks"):
            row = conn.execute(f"select call_id from {table} where ref = ?", (int(key),)).fetchone()
            if row:
                key = row["call_id"]
                break
    return conn.execute(
        "select * from call_summaries where call_id = ? or call_id like ? "
        "order by started_at desc limit 1",
        (key, key + "%"),
    ).fetchone()


def listing(rows: list[sqlite3.Row]) -> str:
    lines = [f"{'started':19}  {'outcome':13}  {'urgency':9}  {'ref':>5}  {'secs':>5}  name / call"]
    for r in rows:
        ref = r["booking_ref"] or (r["task_refs"] or "").split(",")[0]
        lines.append(
            f"{r['started_at'][:19]:19}  {r['outcome']:13}  {r['urgency']:9}  {ref!s:>5}  "
            f"{r['duration_s']:>5}  {r['name'] or '-'} / {r['call_id']}"
        )
    return "\n".join(lines)


def sheet(conn: sqlite3.Connection, r: sqlite3.Row) -> str:
    flags = json.loads(r["flags"] or "{}")
    fired = [k for k, v in flags.items() if v and k != "errors"]
    tasks = conn.execute(
        "select ref, kind, status, reason, due_at from tasks where call_id = ? order by ref",
        (r["call_id"],),
    ).fetchall()
    median = f"{r['median_reply_s']} s" if r["median_reply_s"] is not None else "-"
    lines = [
        f"Call      {r['call_id']}",
        f"When      {r['started_at']} to {r['ended_at']} ({r['duration_s']} s)",
        f"Caller    {r['name'] or '-'}, {r['caller_number'] or 'number withheld'}",
        f"Address   {r['address'] or '-'}",
        f"Issue     {r['issue'] or '-'}",
        f"Outcome   {r['outcome']} ({r['urgency']})",
        f"Booking   #{r['booking_ref']}, {r['window']}" if r["booking_ref"] else "Booking   -",
        *[
            f"Task      #{t['ref']} {t['kind']} ({t['status']}), due {t['due_at']}: {t['reason']}"
            for t in tasks
        ],
        f"Flags     {', '.join(fired) or 'none'}",
        *([f"Errors    {'; '.join(flags['errors'])}"] if flags.get("errors") else []),
        f"Replies   median {median}",
        "",
        r["transcript"] or "(no transcript)",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Recent calls, or one call's sheet.")
    ap.add_argument("call", nargs="?", help="a booking or task reference, or a room name")
    ap.add_argument("-n", type=int, default=20, help="how many recent calls to list")
    args = ap.parse_args()
    if not DB.exists():
        print(f"No database at {DB}")
        return 1
    with connect(DB) as conn:
        if not conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'call_summaries'"
        ).fetchone():
            print("No call summaries yet: the first call on this version creates the table.")
            return 1
        if args.call:
            row = find(conn, args.call)
            if row is None:
                print(f"No call summary for {args.call!r}")
                return 1
            print(sheet(conn, row))
        else:
            print(listing(recent(conn, args.n)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
