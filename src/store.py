"""The booking store: one SQLite file on the host's disk.

Slots, bookings, dispatch tasks and call records live here, and this is the only file with SQL in
it. Each write is a single statement, so SQLite's own locking provides capacity, idempotency and
in-call correction without application-level locks, even though every call runs in its own process.
Callers run these functions through asyncio.to_thread.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
pragma journal_mode = wal;

create table if not exists slots (
    id text primary key,                 -- e.g. 2026-09-29-0800
    day text not null,                   -- ISO date
    start text not null,                 -- HH:MM, local time
    end text not null,
    capacity integer not null
);

create table if not exists bookings (
    ref integer primary key autoincrement,
    call_id text not null unique,        -- one booking per call; a retry or correction updates it
    slot_id text not null references slots (id),
    customer_type text not null check (customer_type in ('residential', 'commercial')),
    priority integer not null default 0,
    name text not null,
    phone text not null,
    address text not null,
    zip text not null,
    issue text not null,
    note text not null default '',
    created_at text not null default (datetime('now'))
);

create table if not exists tasks (
    ref integer primary key autoincrement,
    call_id text not null,
    kind text not null check (kind in ('emergency', 'urgent', 'callback')),
    reason text not null,
    summary text not null,
    name text not null default '',
    phone text not null default '',
    address text not null default '',
    due_at text not null,
    created_at text not null default (datetime('now'))
);

create table if not exists calls (
    call_id text primary key,
    caller_number text,
    started_at text not null default (datetime('now')),
    report_json text
);

-- Start references at speakable four-digit numbers.
insert into sqlite_sequence (name, seq)
select 'bookings', 1000 where not exists (select 1 from sqlite_sequence where name = 'bookings');
insert into sqlite_sequence (name, seq)
select 'tasks', 2000 where not exists (select 1 from sqlite_sequence where name = 'tasks');
"""


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that commits on success, rolls back on error, and always closes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init(path: Path, windows: list[dict], capacity: int, today: date, days_ahead: int) -> None:
    """Create the schema and make sure weekday slots exist from today through days_ahead."""
    days = (today + timedelta(days=n) for n in range(days_ahead + 1))
    rows = [
        (
            f"{d.isoformat()}-{w['start'].replace(':', '')}",
            d.isoformat(),
            w["start"],
            w["end"],
            capacity,
        )
        for d in days
        if d.weekday() < 5
        for w in windows
    ]
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.executemany("insert or ignore into slots values (?, ?, ?, ?, ?)", rows)


def open_slots(
    path: Path, earliest: date, part_of_day: str, now: datetime, limit: int = 2
) -> list[dict]:
    """The first open windows on or after earliest that haven't started yet."""
    sql = """
        select s.* from slots s
        where s.day >= :earliest
          and s.day || ' ' || s.start > :now
          and (:part = 'any'
               or (:part = 'morning' and s.start < '12:00')
               or (:part = 'afternoon' and s.start >= '12:00'))
          and (select count(*) from bookings b where b.slot_id = s.id) < s.capacity
        order by s.day, s.start
        limit :limit
    """
    params = {
        "earliest": earliest.isoformat(),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "part": part_of_day,
        "limit": limit,
    }
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, params)]


CALL_BOOKING = """
    select b.*, s.day, s.start, s.end from bookings b join slots s on s.id = b.slot_id
    where b.call_id = ?
"""


def book(path: Path, **booking) -> dict | None:
    """Reserve booking['slot_id'] for booking['call_id'], or move that call's booking there.

    Returns the stored booking joined with its slot, or None when the slot is full. A full slot
    leaves any booking the call already holds untouched.
    """
    sql = """
        insert into bookings
            (call_id, slot_id, customer_type, priority, name, phone, address, zip, issue, note)
        select :call_id, :slot_id, :customer_type, :priority, :name, :phone, :address, :zip, :issue, :note
        where (select count(*) from bookings where slot_id = :slot_id and call_id != :call_id)
              < (select capacity from slots where id = :slot_id)
        on conflict (call_id) do update set
            slot_id = excluded.slot_id, customer_type = excluded.customer_type,
            priority = excluded.priority, name = excluded.name, phone = excluded.phone,
            address = excluded.address, zip = excluded.zip, issue = excluded.issue,
            note = excluded.note
        returning ref
    """
    with connect(path) as conn:
        if conn.execute(sql, booking).fetchone() is None:
            return None
        return dict(conn.execute(CALL_BOOKING, (booking["call_id"],)).fetchone())


def booking_for(path: Path, call_id: str) -> dict | None:
    """The booking a call holds, joined with its slot, or None."""
    with connect(path) as conn:
        row = conn.execute(CALL_BOOKING, (call_id,)).fetchone()
        return dict(row) if row else None


def add_task(path: Path, **task) -> int:
    sql = """
        insert into tasks (call_id, kind, reason, summary, name, phone, address, due_at)
        values (:call_id, :kind, :reason, :summary, :name, :phone, :address, :due_at)
        returning ref
    """
    with connect(path) as conn:
        return conn.execute(sql, task).fetchone()["ref"]


def save_call(
    path: Path, call_id: str, caller_number: str | None = None, report_json: str | None = None
) -> None:
    sql = """
        insert into calls (call_id, caller_number, report_json) values (?, ?, ?)
        on conflict (call_id) do update set
            caller_number = coalesce(excluded.caller_number, caller_number),
            report_json = coalesce(excluded.report_json, report_json)
    """
    with connect(path) as conn:
        conn.execute(sql, (call_id, caller_number, report_json))
