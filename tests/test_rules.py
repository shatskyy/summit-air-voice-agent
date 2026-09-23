"""Deterministic checks: no model, no network, no credit. Run after every change."""

from datetime import date, datetime

import pytest
from livekit.agents import ToolError

import receptionist
import store
from receptionist import HAZARD, TZ, Call, SummitAirAgent, flag_hazard, render_instructions

WINDOWS = [{"start": "08:00", "end": "12:00"}, {"start": "12:00", "end": "16:00"}]
MONDAY_9AM = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    store.init(path, WINDOWS, capacity=1, today=MONDAY_9AM.date(), days_ahead=7)
    return path


def booking(call_id, slot_id, **overrides):
    fields = {
        "call_id": call_id,
        "slot_id": slot_id,
        "customer_type": "residential",
        "priority": 0,
        "name": "Maria Lopez",
        "phone": "+19145550100",
        "address": "14 Maple Ave, White Plains",
        "zip": "10601",
        "issue": "furnace won't start",
        "note": "",
    }
    return fields | overrides


class FakeContext:
    """Stands in for RunContext: the tools only read userdata and pause interruptions."""

    def __init__(self, call):
        self.userdata = call

    def disallow_interruptions(self):
        pass


# The store


def test_offers_at_most_two_windows_and_skips_ones_already_started(db):
    slots = store.open_slots(db, MONDAY_9AM.date(), "any", MONDAY_9AM)
    assert [s["id"] for s in slots] == ["2026-09-28-1200", "2026-09-29-0800"]


def test_no_slots_on_weekends(db):
    slots = store.open_slots(db, date(2026, 10, 3), "any", MONDAY_9AM)
    assert slots[0]["day"] == "2026-10-05"


def test_a_retry_returns_the_same_booking(db):
    first = store.book(db, **booking("call-a", "2026-09-29-0800"))
    again = store.book(db, **booking("call-a", "2026-09-29-0800"))
    assert first["ref"] == again["ref"] > 1000


def test_a_full_window_is_refused_and_the_existing_booking_is_kept(db):
    store.book(db, **booking("call-a", "2026-09-29-0800"))
    assert store.book(db, **booking("call-b", "2026-09-29-0800")) is None
    kept = store.book(db, **booking("call-b", "2026-09-29-1200"))
    assert store.book(db, **booking("call-b", "2026-09-29-0800")) is None
    assert kept["slot_id"] == "2026-09-29-1200"


def test_a_correction_during_the_call_moves_the_same_booking(db):
    first = store.book(db, **booking("call-a", "2026-09-29-0800"))
    moved = store.book(
        db, **booking("call-a", "2026-09-29-1200", address="16 Maple Ave, White Plains")
    )
    assert moved["ref"] == first["ref"]
    assert (moved["slot_id"], moved["address"]) == ("2026-09-29-1200", "16 Maple Ave, White Plains")
    assert (
        store.open_slots(db, date(2026, 9, 29), "morning", MONDAY_9AM)[0]["id"] == "2026-09-29-0800"
    )


# The tools


async def test_booking_a_window_that_was_never_offered_is_refused(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="not offered"):
        await SummitAirAgent("").book_appointment(
            ctx,
            "2026-09-29-0800",
            "residential",
            "Maria Lopez",
            "+19145550100",
            "14 Maple Ave, White Plains",
            "10601",
            "no heat",
        )


async def test_an_address_outside_the_three_counties_is_refused(db):
    ctx = FakeContext(Call(call_id="call-a", db=db, offered={"2026-09-29-0800": "Tuesday"}))
    with pytest.raises(ToolError, match="outside the service area"):
        await SummitAirAgent("").book_appointment(
            ctx,
            "2026-09-29-0800",
            "residential",
            "Maria Lopez",
            "+12125550100",
            "10 W 4th St, New York",
            "10012",
            "no heat",
        )


async def test_an_offered_window_books_and_returns_a_reference(db):
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db))
    offered = await agent.check_availability(ctx, "2026-09-29", "morning")
    assert "slot_id 2026-09-29-0800" in offered
    confirmation = await agent.book_appointment(
        ctx,
        "2026-09-29-0800",
        "residential",
        "Maria Lopez",
        "+19145550100",
        "14 Maple Ave, White Plains",
        "10601",
        "no heat",
    )
    assert "Reference 1001" in confirmation and "Tuesday, September 29" in confirmation


async def test_an_urgent_task_states_a_target_and_pages(db, monkeypatch):
    pages = []

    async def fake_page(title, message):
        pages.append(title)

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    result = await SummitAirAgent("").create_dispatch_task(
        ctx, "urgent", "no heat, mother is 78", "furnace out"
    )
    assert "Task 2001 created" in result and "callback target" in result
    await receptionist.asyncio.sleep(0)
    assert pages == ["Summit Air urgent #2001"]


# Safety backstop


@pytest.mark.parametrize(
    "said",
    [
        "I smell gas in the basement",
        "it smells kind of like gas near the furnace",
        "there's a gas leak",
        "it smells like rotten eggs",
        "the carbon monoxide alarm is going off",
        "my CO detector keeps beeping",
        "there's smoke coming out of the vent",
        "I think the unit is on fire",
        "there's a burning smell",
        # Known false positives. The script is worded to be harmless when these fire.
        "no, I don't smell gas",
        "the smoke detector battery died",
    ],
)
def test_hazard_phrases_trigger_the_safety_script(said):
    assert HAZARD.search(said)


@pytest.mark.parametrize(
    "said",
    [
        "my furnace won't fire up",
        "I have a gas furnace and it won't turn on",
        "the AC is blowing warm air",
        "the heat pump is making a grinding noise",
    ],
)
def test_ordinary_calls_do_not_trigger_it(said):
    assert not HAZARD.search(said)


async def test_a_hazard_files_one_emergency_task_per_call(db, monkeypatch):
    async def fake_page(title, message):
        pass

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db)
    assert await flag_hazard(call, "I smell gas") is True
    assert await flag_hazard(call, "yes, it's really strong gas smell") is False
    assert call.hazard_task == 2001


# The prompt


def test_the_prompt_renders_every_placeholder():
    prompt = render_instructions(datetime(2026, 9, 29, 13, 5, tzinfo=TZ), "+19145550100")
    assert "{" not in prompt and "}" not in prompt
    assert "Tuesday, September 29, 2026" in prompt and "1:05 PM" in prompt
    assert "The office is open" in prompt and "+19145550100" in prompt


def test_evenings_and_weekends_are_after_hours():
    assert "office is closed" in render_instructions(datetime(2026, 9, 29, 19, 0, tzinfo=TZ), None)
    assert "office is closed" in render_instructions(datetime(2026, 10, 3, 10, 0, tzinfo=TZ), None)
