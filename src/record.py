"""The call record: what one call did, written by code when it ends, and the push that tells
dispatch.

No model call. Everything comes from the call's state, its rows in the store and the history, so
the record says what happened rather than what a summary thinks happened. src/agent.py runs
finish_call when a call ends; the simulator runs it after each conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics

import receptionist
import store
from receptionist import (
    Call,
    caller_said_goodbye,
    closing_confirmed,
    file_task,
    first_name,
    speak_window,
    transcript_text,
    zip_of,
)

logger = logging.getLogger("summit-air")

OUTCOMES = (
    "booked",
    "moved",
    "urgent",
    "emergency",
    "false_alarm",
    "callback",
    "info_only",
    "abandoned",
    "hung_up_early",
)


# A caller who names their heating or cooling and hangs up before anything was booked or filed is a
# lost job, not a wrong number: the office calls them back (O3).
HVAC = re.compile(
    r"\b(?:heat\w*|furnace|boiler|a/?c|air ?condition\w*|cool\w*|heat pump|thermostat"
    r"|mini.?split|ductless|hvac|radiator|condenser|vents?|freezing|too hot)\b",
    re.IGNORECASE,
)
ABANDONED = "Hung up before booking: "


def caller_turns(items) -> list[str]:
    return [
        i.text_content
        for i in items
        if getattr(i, "type", None) == "message" and i.role == "user" and i.text_content
    ]


def ended_on_purpose(items) -> bool:
    """The caller answered the closing question, or said goodbye (GuardedEndCall's two exits)."""
    return closing_confirmed(items) or caller_said_goodbye(items)


def abandoned(call: Call, items, booking: dict | None, tasks: list[dict]) -> bool:
    """The caller didn't end the call on purpose, mentioned heating or cooling, nothing was booked
    or filed, and there is a number to call back."""
    return (
        not ended_on_purpose(items)
        and booking is None
        and not tasks
        and bool(call.caller_number)
        and any(HVAC.search(t) for t in caller_turns(items))
    )


def problem_sentence(turns: list[str]) -> str:
    """The first sentence the caller said about their heating or cooling: "Hi." tells nobody why."""
    sentences = [x for t in turns for x in re.split(r"(?<=[.!?])\s+", t.strip())]
    return next((x for x in sentences if HVAC.search(x)), sentences[0] if sentences else "")


def outcome(booking: dict | None, tasks: list[dict], call: Call, answered_close: bool) -> str:
    """The one word dispatch reads first. Safety outranks a booking, and a booking outranks a
    callback, since the booking is what a technician drives to."""
    kinds = {t["kind"] for t in tasks if t["status"] == "open"}
    if "emergency" in kinds:
        return "emergency"
    if "urgent" in kinds:
        return "urgent"
    if booking:
        return "moved" if call.moved else "booked"
    if any(t["kind"] == "emergency" and t["status"] == "false_alarm" for t in tasks):
        return "false_alarm"
    if any(t["reason"].startswith(ABANDONED) for t in tasks):
        return "abandoned"
    if kinds:
        return "callback"
    return "info_only" if answered_close else "hung_up_early"


def summarize(call: Call, items, booking: dict | None, tasks: list[dict]) -> dict:
    """The call_summaries row for a call that has just ended."""
    ended = receptionist.now()
    open_kinds = {t["kind"] for t in tasks if t["status"] == "open"}
    named = next((t for t in tasks if t["name"]), None)
    placed = next((t for t in tasks if t["address"]), None)
    latencies = call.reply_latencies
    return {
        "call_id": call.call_id,
        "started_at": call.started_at.isoformat(timespec="seconds"),
        "ended_at": ended.isoformat(timespec="seconds"),
        "duration_s": max(round((ended - call.started_at).total_seconds()), 0),
        "caller_number": call.caller_number,
        "name": booking["name"] if booking else named["name"] if named else "",
        "address": f"{booking['address']} {booking['zip']}"
        if booking
        else placed["address"]
        if placed
        else "",
        "issue": booking["issue"] if booking else tasks[0]["reason"] if tasks else "",
        "outcome": outcome(booking, tasks, call, ended_on_purpose(items)),
        "urgency": "emergency"
        if "emergency" in open_kinds
        else "urgent"
        if "urgent" in open_kinds
        else "routine",
        "booking_ref": booking["ref"] if booking else None,
        "window": speak_window(booking) if booking else "",
        "task_refs": ",".join(str(t["ref"]) for t in tasks),
        "flags": json.dumps(
            {
                "hazard_backstop": call.warned,
                "urgent_by_code": call.urgent_by_code,
                "promise_backstop": call.promise_kept,
                "fallback_model": call.fallback_used,
                "errors": call.errors,
            }
        ),
        "median_reply_s": round(statistics.median(latencies), 2) if latencies else None,
        "transcript": transcript_text(items),
    }


def dispatch_text(summary: dict, zip_code: str) -> tuple[str, str]:
    """The push for one call: first name, ZIP, outcome, window, reference, and where the rest is."""
    ref = summary["booking_ref"] or summary["task_refs"].split(",")[0] or summary["call_id"]
    title = f"Summit Air call: {summary['outcome'].replace('_', ' ')}"
    parts = [first_name(summary["name"]), zip_code, summary["window"]]
    if summary["booking_ref"]:
        parts.append(f"booking #{summary['booking_ref']}")
    if summary["task_refs"]:
        parts.append(f"task #{summary['task_refs'].replace(',', ', #')}")
    lines = [" / ".join(p for p in parts if p), f"Details: scripts/calls.py {ref}"]
    return title, "\n".join(line for line in lines if line)


async def finish_call(call: Call, items) -> dict:
    """Write the call's summary and push it to dispatch. Returns the summary."""
    booking = await asyncio.to_thread(store.booking_for, call.db, call.call_id)
    tasks = await asyncio.to_thread(store.tasks_for, call.db, call.call_id)
    if abandoned(call, items, booking, tasks):
        said = caller_turns(items)
        await file_task(
            call, "callback", ABANDONED + problem_sentence(said), transcript_text(items)
        )
        tasks = await asyncio.to_thread(store.tasks_for, call.db, call.call_id)
    summary = summarize(call, items, booking, tasks)
    await asyncio.to_thread(store.save_summary, call.db, **summary)
    title, message = dispatch_text(summary, zip_of(call, summary["address"]))
    # One phone for the demo: without a topic of its own, dispatch shares the on-call topic.
    topic = "DISPATCH_NTFY_TOPIC" if os.getenv("DISPATCH_NTFY_TOPIC") else "NTFY_TOPIC"
    await receptionist.push(topic, title, message)
    return summary
