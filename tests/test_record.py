"""The call record (O2): the summary row, the dispatch push and the call sheet. No network."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
from livekit.agents import llm

import paging
import receptionist
import record
import store
from receptionist import TZ, Call, file_task

WINDOWS = [{"start": "08:00", "end": "12:00"}, {"start": "12:00", "end": "16:00"}]
MONDAY_9AM = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)
BOOKING = {
    "customer_type": "residential",
    "priority": 0,
    "name": "Maria Lopez",
    "phone": "+19145550100",
    "address": "14 Maple Street, Brooklyn",
    "zip": "11225",
    "issue": "furnace won't start",
    "note": "",
}

spec = importlib.util.spec_from_file_location(
    "calls", Path(__file__).resolve().parent.parent / "scripts" / "calls.py"
)
calls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calls)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    store.init(path, WINDOWS, capacity=3, today=MONDAY_9AM.date(), days_ahead=7)
    return path


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def fake_push(variable, title, message, priority="default", tags=""):
        sent.append((variable, title, message, priority))
        return True

    async def fake_page(title, message):
        return True

    monkeypatch.setattr(receptionist, "push", fake_push)
    monkeypatch.setattr(paging, "page_on_call", fake_page)
    return sent


def history(*turns, closing_answered=False):
    ctx = llm.ChatContext()
    for role, text in turns:
        ctx.add_message(role=role, content=text)
    if closing_answered:
        ctx.add_message(role="assistant", content="Is there anything else I can help with?")
        ctx.add_message(role="user", content="No, that's all.")
    return ctx.items


async def test_a_booked_call_is_summarized_and_pushed_without_personal_details(
    db, pushes, monkeypatch
):
    monkeypatch.setenv("DISPATCH_NTFY_TOPIC", "dispatch")
    call = Call(call_id="call-a", caller_number="+19145550100", db=db)
    call.reply_latencies = [0.9, 1.4, 2.0]
    store.book(db, call_id="call-a", slot_id="2026-09-29-0800", **BOOKING)
    items = history(
        ("user", "My furnace won't start."), ("assistant", "Got it."), closing_answered=True
    )

    summary = await record.finish_call(call, items)

    assert summary["outcome"] == "booked" and summary["urgency"] == "routine"
    assert summary["booking_ref"] == 1001 and "Tuesday" in summary["window"]
    assert summary["median_reply_s"] == 1.4
    assert "Caller: My furnace won't start." in summary["transcript"]
    with store.connect(db) as conn:
        row = conn.execute("select * from call_summaries").fetchone()
    assert row["call_id"] == "call-a" and row["name"] == "Maria Lopez"
    assert json.loads(row["flags"])["hazard_backstop"] is False
    [(variable, title, message, priority)] = pushes
    assert variable == "DISPATCH_NTFY_TOPIC" and priority == "default"
    assert title == "Summit Air call: booked"
    assert "Maria" in message and "11225" in message and "booking #1001" in message
    assert "Lopez" not in message and "Maple" not in message and "9145550100" not in message
    assert "furnace" not in message


async def test_dispatch_shares_the_on_call_topic_when_it_has_none(db, pushes, monkeypatch):
    monkeypatch.delenv("DISPATCH_NTFY_TOPIC", raising=False)
    await record.finish_call(Call(call_id="call-a", db=db), history(("user", "Hello?")))
    assert pushes[0][0] == "NTFY_TOPIC"


async def outcome_of(db, call, closing_answered=False):
    call.ended_by_agent = closing_answered
    return (await record.finish_call(call, history(closing_answered=closing_answered)))["outcome"]


async def test_each_outcome(db, pushes):
    call = Call(call_id="booked", db=db)
    store.book(db, call_id="booked", slot_id="2026-09-29-0800", **BOOKING)
    assert await outcome_of(db, call) == "booked"

    call = Call(call_id="moved", db=db, moved=True)
    store.book(db, call_id="moved", slot_id="2026-09-29-1200", **BOOKING)
    assert await outcome_of(db, call) == "moved"

    call = Call(call_id="urgent", db=db)
    await file_task(call, "urgent", "no heat, mother 80", "")
    store.book(db, call_id="urgent", slot_id="2026-09-30-0800", **BOOKING)
    assert await outcome_of(db, call) == "urgent"

    call = Call(call_id="emergency", db=db)
    await file_task(call, "emergency", "gas", "")
    assert await outcome_of(db, call) == "emergency"

    call = Call(call_id="false-alarm", db=db)
    ref, _, _ = await file_task(call, "emergency", "dusty smell", "")
    store.set_task_status(db, ref, "false_alarm")
    assert await outcome_of(db, call) == "false_alarm"

    call = Call(call_id="callback", db=db)
    await file_task(call, "callback", "wants a person", "")
    assert await outcome_of(db, call) == "callback"

    assert await outcome_of(db, Call(call_id="info", db=db), closing_answered=True) == "info_only"
    assert await outcome_of(db, Call(call_id="early", db=db)) == "hung_up_early"


async def test_the_flags_say_which_backstops_fired(db, pushes):
    call = Call(call_id="call-a", db=db, warned=True, urgent_by_code=True, fallback_used=True)
    call.errors.append("llm_error: down")
    summary = await record.finish_call(call, history())
    assert json.loads(summary["flags"]) == {
        "hazard_backstop": True,
        "urgent_by_code": True,
        "promise_backstop": False,
        "fallback_model": True,
        "fabricated_confirmations": 0,
        "repeats_dropped": 0,
        "bookings_held": 0,
        "errors": ["llm_error: down"],
    }


async def test_the_call_sheet_finds_a_call_by_reference_or_room(db, pushes):
    call = Call(call_id="call-_+16505550142_abc", caller_number="+16505550142", db=db)
    store.book(db, call_id=call.call_id, slot_id="2026-09-29-0800", **BOOKING)
    await record.finish_call(call, history(("user", "My furnace won't start.")))
    with calls.connect(db) as conn:
        assert calls.find(conn, "1001")["call_id"] == call.call_id
        assert calls.find(conn, "call-_+1650")["call_id"] == call.call_id
        assert calls.find(conn, "9999") is None
        sheet = calls.sheet(conn, calls.find(conn, "1001"))
        listing = calls.listing(calls.recent(conn, 5))
    assert "Maria Lopez" in sheet and "Caller: My furnace won't start." in sheet
    assert "booked" in listing and call.call_id in listing


def test_the_page_names_no_caller_words_number_or_street():
    call = Call(call_id="call-a", caller_number="+19145550100", checked_zip="11201")
    text = receptionist.push_text(call, "no heat, mother 80", "Maria Lopez", "48 Bergen St", 2001)
    assert text == "no heat, mother 80\nMaria 11201\nDetails: scripts/calls.py 2001"


# Abandoned calls (O3)


async def test_a_caller_who_hangs_up_after_naming_the_problem_gets_a_callback(db, pushes):
    call = Call(call_id="call-a", caller_number="+19145550100", db=db)
    items = history(
        ("assistant", receptionist.GREETING),
        ("user", "Hi. My AC stopped working. It's really hot."),
        ("assistant", "Got it. What's your name?"),
    )
    summary = await record.finish_call(call, items)
    assert summary["outcome"] == "abandoned"
    [task] = store.tasks_for(db, "call-a")
    assert task["kind"] == "callback"
    assert task["reason"] == "Hung up before booking: My AC stopped working."
    assert "My AC stopped working" in task["summary"]
    assert pushes[0][1] == "Summit Air call: abandoned"


@pytest.mark.parametrize(
    ("number", "said", "closing_answered"),
    [
        (None, "My AC stopped working.", False),  # nobody to call back
        ("+19145550100", "Is this Joe's Pizza?", False),  # not heating or cooling
        ("+19145550100", "My AC stopped working.", True),  # they said they needed nothing else
        ("+19145550100", "How much is a new AC? Okay, that's all, bye.", False),  # a goodbye
    ],
)
async def test_no_abandoned_callback_without_a_number_a_problem_or_an_open_call(
    db, pushes, number, said, closing_answered
):
    call = Call(call_id="call-a", caller_number=number, db=db, ended_by_agent=closing_answered)
    items = history(("user", said), closing_answered=closing_answered)
    summary = await record.finish_call(call, items)
    assert store.tasks_for(db, "call-a") == []
    assert summary["outcome"] in ("hung_up_early", "info_only")


async def test_a_call_that_already_has_a_task_is_not_abandoned(db, pushes):
    call = Call(call_id="call-a", caller_number="+19145550100", db=db)
    await file_task(call, "callback", "wants a person", "")
    await record.finish_call(call, history(("user", "My furnace is out, get me a person.")))
    assert len(store.tasks_for(db, "call-a")) == 1
