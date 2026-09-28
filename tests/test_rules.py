"""Deterministic checks: no model, no network, no credit. Run after every change."""

import asyncio
import contextlib
from datetime import date, datetime

import pytest
from livekit.agents import AgentSession, APIConnectionError, StopResponse, ToolError, llm
from livekit.agents.voice.agent_session import SessionConnectOptions

import paging
import receptionist
import rules
import store
from receptionist import (
    TZ,
    Call,
    GuardedEndCall,
    SummitAirAgent,
    file_task,
    flag_hazard,
    in_coverage,
    keep_promise,
    render_instructions,
    wants_dictation,
)

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
        "address": "14 Maple Street, Brooklyn",
        "zip": "11225",
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


def race_for(path, call_id):
    return store.book(path, **booking(call_id, "2026-09-29-0800")) is not None


def test_callers_racing_for_the_last_slot_get_exactly_one_booking(db):
    """Every call runs in its own process, so the capacity check has to hold across processes. It
    does because the check and the insert are one SQL statement under SQLite's write lock."""
    from concurrent.futures import ProcessPoolExecutor

    with ProcessPoolExecutor(max_workers=6) as pool:
        won = list(pool.map(race_for, [db] * 6, [f"call-{n}" for n in range(6)]))
    assert won.count(True) == 1
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from bookings").fetchone()[0] == 1


def test_a_correction_during_the_call_moves_the_same_booking(db):
    first = store.book(db, **booking("call-a", "2026-09-29-0800"))
    moved = store.book(
        db, **booking("call-a", "2026-09-29-1200", address="16 Maple Street, Brooklyn")
    )
    assert moved["ref"] == first["ref"]
    assert (moved["slot_id"], moved["address"]) == ("2026-09-29-1200", "16 Maple Street, Brooklyn")
    assert (
        store.open_slots(db, date(2026, 9, 29), "morning", MONDAY_9AM)[0]["id"] == "2026-09-29-0800"
    )


# The tools


@pytest.mark.parametrize("placeholder", ["Caller", "", "  unknown ", "N/A"])
async def test_booking_without_a_real_name_is_refused(db, placeholder):
    ctx = FakeContext(Call(call_id="call-a", db=db, offered={"2026-09-29-0800": "Tuesday"}))
    with pytest.raises(ToolError, match="No name yet"):
        await SummitAirAgent("").book_appointment(
            ctx,
            "2026-09-29-0800",
            "residential",
            placeholder,
            "+19145550100",
            "14 Maple Street, Brooklyn",
            "11225",
            "AC is broken",
        )


async def test_booking_a_window_that_was_never_offered_is_refused(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="not offered"):
        await SummitAirAgent("").book_appointment(
            ctx,
            "2026-09-29-0800",
            "residential",
            "Maria Lopez",
            "+19145550100",
            "14 Maple Street, Brooklyn",
            "11225",
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
            "10 Bay Street, Staten Island",
            "10301",
            "no heat",
        )


@pytest.mark.parametrize(
    ("zip_code", "covered"),
    [
        ("10003", True),  # Manhattan
        ("11225", True),  # Brooklyn
        ("11101", True),  # Long Island City, Queens
        ("11004", True),  # Glen Oaks, Queens, inside Nassau's 110 prefix
        ("11691", True),  # Far Rockaway, Queens
        ("10451", False),  # the Bronx
        ("10301", False),  # Staten Island
        ("11010", False),  # Franklin Square, Nassau County
        ("10601", False),  # White Plains
    ],
)
def test_the_service_area_is_manhattan_brooklyn_and_queens(zip_code, covered):
    assert in_coverage(zip_code) is covered


async def test_an_address_outside_the_area_is_caught_before_any_window(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    result = await SummitAirAgent("").check_address(ctx, "48 Bay Street", "Staten Island", "10301")
    assert "outside the service area" in result and "Don't offer times" in result
    # A tester who gives their own address has to hear where Summit Air works, or it's a dead end.
    assert "Manhattan, Brooklyn and Queens" in result
    assert ctx.userdata.checked_zip is None


async def test_an_address_without_a_borough_is_sent_back_for_the_borough(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="which borough"):
        await SummitAirAgent("").check_address(ctx, "14 Maple Street", " ", "11225")


async def test_booking_an_address_that_was_never_checked_is_refused(db):
    ctx = FakeContext(Call(call_id="call-a", db=db, offered={"2026-09-29-0800": "Tuesday"}))
    with pytest.raises(ToolError, match="hasn't been checked"):
        await SummitAirAgent("").book_appointment(
            ctx,
            "2026-09-29-0800",
            "residential",
            "Maria Lopez",
            "+19145550100",
            "14 Maple Street, Brooklyn",
            "11225",
            "no heat",
        )


async def test_an_offered_window_books_and_returns_a_reference(db):
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db))
    readback = await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    assert "14 Maple Street, Brooklyn, ZIP 11225" in readback
    offered = await agent.check_availability(ctx, "2026-09-29", "morning")
    assert "slot_id 2026-09-29-0800" in offered
    confirmation = await agent.book_appointment(
        ctx,
        "2026-09-29-0800",
        "residential",
        "Maria Lopez",
        "+19145550100",
        "14 Maple Street, Brooklyn",
        "11225",
        "no heat",
    )
    assert "Reference 1001" in confirmation and "Tuesday, September 29" in confirmation


async def test_a_booking_without_a_given_number_keeps_the_caller_id(db):
    """The same "unknown" habit as calls 8 and 9, on the booking: dispatch would have no number to
    call although caller ID has it."""
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    await agent.book_appointment(
        ctx,
        "2026-09-29-0800",
        "residential",
        "Maria Lopez",
        "unknown",
        "14 Maple Street",
        "11225",
        "no heat",
    )
    with store.connect(db) as conn:
        assert conn.execute("select phone from bookings").fetchone()[0] == "+19145550100"


async def booked_call(db, caller_number, **kwargs):
    """Check the address, offer the morning, and book it, as a call would."""
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number=caller_number))
    await agent.check_address(ctx, "200 Flatbush Avenue", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    args = {
        "slot_id": "2026-09-29-0800",
        "customer_type": "residential",
        "name": "Maria Lopez",
        "callback_number": "",
        "address": "200 Flatbush Avenue, Brooklyn",
        "zip_code": "11225",
        "issue": "no cooling",
    }
    return await agent.book_appointment(ctx, **(args | kwargs))


async def test_a_withheld_caller_id_cannot_book_without_a_number(db):
    """Simulated call 2: with caller ID withheld, a "yes" to "is this number the best one" would
    have stored an empty phone number."""
    with pytest.raises(ToolError, match="No callback number"):
        await booked_call(db, None, callback_number="yes")
    await booked_call(db, None, callback_number="914-555-0142")
    with store.connect(db) as conn:
        assert conn.execute("select phone from bookings").fetchone()[0] == "914-555-0142"


def test_a_withheld_caller_id_makes_the_prompt_ask_for_a_number():
    at = datetime(2026, 9, 29, 13, 5, tzinfo=TZ)
    assert "Caller ID is withheld" in render_instructions(at, None)
    assert "Caller ID is withheld" not in render_instructions(at, "+19145550100")


async def test_a_commercial_booking_needs_the_business_name(db):
    """Simulated call 8: four of six bookings lost the business name, which had nowhere to go."""
    with pytest.raises(ToolError, match="business's name and a site contact"):
        await booked_call(db, "+19145550100", customer_type="commercial", business_name=None)
    with pytest.raises(ToolError, match="business's name and a site contact"):
        await booked_call(
            db, "+19145550100", customer_type="commercial", business_name="Bright Smile Dental"
        )
    with pytest.raises(ToolError, match="kind of business, not its name"):
        await booked_call(
            db,
            "+19145550100",
            customer_type="commercial",
            business_name="dental office",
            site_contact="Maria",
        )
    await booked_call(
        db,
        "+19145550100",
        customer_type="commercial",
        business_name="Bright Smile Dental",
        site_contact="Maria, the office manager",
        note="Roof hatch in the back storage room.",
    )
    with store.connect(db) as conn:
        note = conn.execute("select note from bookings").fetchone()[0]
    assert note.startswith("Business: Bright Smile Dental. Site contact: Maria") and "hatch" in note


async def test_moving_to_a_full_window_says_the_first_booking_still_stands(db):
    """A full window leaves the call's booking where it was (store.book), but the refusal didn't say
    so, and a model told only "that window filled up" can tell the caller they have nothing."""
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    await agent.check_availability(ctx, "2026-09-29", "afternoon")
    args = ("residential", "Maria Lopez", "", "14 Maple Street", "11225", "no heat")
    await agent.book_appointment(ctx, "2026-09-29-0800", *args)
    store.book(db, **booking("call-b", "2026-09-29-1200"))  # capacity 1: now full
    with pytest.raises(
        ToolError, match="Tuesday, September 29, between 8 AM and noon still stands"
    ):
        ctx.userdata.turn += 1  # the caller's next turn
        await agent.book_appointment(ctx, "2026-09-29-1200", *args)


async def test_an_urgent_task_states_a_target_and_pages(db, monkeypatch):
    pages = []

    async def fake_page(title, message):
        pages.append(title)
        return True

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    result = await SummitAirAgent("").create_dispatch_task(
        ctx, "urgent", "no heat, mother is 78", "furnace out"
    )
    assert "Task 2001 created" in result and "callback target" in result
    assert "was paged" in result
    assert pages == ["Summit Air urgent #2001"]


def eastern(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize(
    ("asked", "due"),
    [
        (eastern(9, 28, 13), eastern(9, 28, 15)),  # Monday afternoon
        (eastern(9, 28, 16, 30), eastern(9, 29, 9, 30)),  # half an hour before close
        (eastern(9, 28, 23), eastern(9, 29, 10)),  # Monday night
        (eastern(9, 28, 6), eastern(9, 28, 10)),  # before opening
        (eastern(10, 2, 18), eastern(10, 5, 10)),  # Friday evening
        (eastern(10, 3, 11), eastern(10, 5, 10)),  # Saturday
        (eastern(9, 28, 8), eastern(9, 28, 10)),  # exactly at opening
        (eastern(9, 28, 17), eastern(9, 29, 10)),  # exactly at closing
        (eastern(9, 28, 16, 59), eastern(9, 29, 9, 59)),  # one office minute left
        (eastern(10, 2, 16), eastern(10, 5, 9)),  # Friday afternoon runs into Monday
        (eastern(10, 4, 23), eastern(10, 5, 10)),  # Sunday night
        (eastern(10, 31, 11), eastern(11, 2, 10)),  # Saturday before the clocks go back
    ],
)
def test_a_routine_callback_target_counts_office_time_only(asked, due):
    assert receptionist.office_minutes_from(asked, 120) == due


def test_a_target_across_the_clock_change_is_stored_in_standard_time():
    due = receptionist.office_minutes_from(eastern(10, 31, 11), 120)
    assert due.isoformat(timespec="minutes") == "2026-11-02T10:00-05:00"
    assert receptionist.speak_due(due, eastern(10, 31, 11)) == "10 AM Monday"


def test_a_callback_target_names_the_day_when_it_is_not_today():
    monday_11pm = eastern(9, 28, 23)
    assert receptionist.speak_due(eastern(9, 28, 23, 15), monday_11pm) == "11:15 PM"
    assert receptionist.speak_due(eastern(9, 29, 10), monday_11pm) == "10 AM tomorrow"
    assert receptionist.speak_due(eastern(10, 5, 10), eastern(10, 2, 18)) == "10 AM Monday"
    assert receptionist.speak_due(eastern(10, 5, 10), eastern(10, 4, 23)) == "10 AM tomorrow"


async def test_a_callback_asked_for_at_night_is_due_the_next_morning(db, monkeypatch):
    monkeypatch.setattr(receptionist, "now", lambda: eastern(9, 28, 23))
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    result = await SummitAirAgent("").create_dispatch_task(
        ctx, "callback", "wants a person", "asked to speak to someone"
    )
    assert "10 AM tomorrow" in result
    with store.connect(db) as conn:
        due_at = conn.execute("select due_at from tasks where ref = 2001").fetchone()[0]
    assert due_at.startswith("2026-09-29T10:00")


async def test_a_task_never_stores_a_placeholder_for_a_detail_nobody_gave(db):
    """Calls 8 and 9: the model filed name and address as "Unknown", and the page repeated it. A
    blank tells dispatch the detail is missing; "Unknown" reads like data."""
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    await SummitAirAgent("").create_dispatch_task(
        ctx, "callback", "wants a person", "asked for someone", "unknown", "Unknown", "unknown"
    )
    with store.connect(db) as conn:
        row = conn.execute("select name, phone, address from tasks where ref = 2001").fetchone()
    assert tuple(row) == ("", "+19145550100", "")


async def test_hanging_up_speaks_the_fixed_goodbye_and_asks_the_model_for_nothing():
    said = []

    class FakeSession:
        def say(self, text, allow_interruptions=True):
            said.append(text)

    class Event:
        ctx = type("Ctx", (), {"session": FakeSession()})()

    await receptionist.say_goodbye(Event())
    assert said == [receptionist.GOODBYE]
    end_call = next(t for t in SummitAirAgent("").tools if getattr(t, "id", "") == "end_call")
    assert end_call._end_instructions is None


class QuietLine:
    """Stands in for AgentSession: records what the silence watch says and whether it hung up."""

    def __init__(self):
        self.said = []
        self.hung_up = False

    async def generate_reply(self, instructions):
        self.said.append(instructions)

    async def say(self, text, allow_interruptions=True):
        self.said.append(text)

    async def hang_up(self):
        self.hung_up = True


async def test_a_line_that_stays_quiet_after_the_check_in_is_hung_up():
    line = QuietLine()
    watch = receptionist.SilenceWatch(line, line.hang_up, wait=0.01)
    watch.on_away()
    await receptionist.asyncio.sleep(0.05)
    assert line.said == [receptionist.CHECK_IN, receptionist.SILENT_GOODBYE]
    assert line.hung_up


async def test_speaking_after_the_check_in_keeps_the_call_open():
    line = QuietLine()
    watch = receptionist.SilenceWatch(line, line.hang_up, wait=0.05)
    watch.on_away()
    await receptionist.asyncio.sleep(0.01)
    watch.on_speaking()
    await receptionist.asyncio.sleep(0.1)
    assert line.said == [receptionist.CHECK_IN]
    assert not line.hung_up


async def test_a_reply_that_voice_detection_missed_still_keeps_the_call_open():
    """LiveKit moves a caller from away back to listening when a final transcript arrives that voice
    detection missed (agent_session.py:2244). Only "speaking" called the hang-up off, so a quiet
    "yes, I'm here" got the goodbye and the hang-up twelve seconds after the check-in."""
    line = QuietLine()
    watch = receptionist.SilenceWatch(line, line.hang_up, wait=0.05)
    watch.on_user_state(type("Event", (), {"new_state": "away"})())
    await receptionist.asyncio.sleep(0.01)
    watch.on_user_state(type("Event", (), {"new_state": "listening"})())
    await receptionist.asyncio.sleep(0.1)
    assert line.said == [receptionist.CHECK_IN]
    assert not line.hung_up


# Caller ID


class SipCaller:
    def __init__(self, identity, number=None):
        self.identity = identity
        self.attributes = {"sip.phoneNumber": number} if number else {}


@pytest.mark.parametrize(
    ("identity", "number"),
    [
        ("sip_anonymous", None),
        ("sip_+266696687", "+266696687"),  # ANONYMOUS on a keypad, Twilio's stand-in
        ("sip_restricted", "Restricted"),
        ("sip_+7378742833", "+7378742833"),  # RESTRICTED on a keypad
    ],
)
def test_a_withheld_number_is_treated_as_unknown(identity, number):
    """A withheld caller ID arrives as a word or as Twilio's stand-in digits. Stored as the number,
    it would have the agent "confirm" a number that can't be called back."""
    import agent

    assert agent.caller_number(SipCaller(identity, number)) is None


def test_a_real_caller_id_is_kept():
    import agent

    assert agent.caller_number(SipCaller("sip_+19145550100", "+19145550100")) == "+19145550100"


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
        # Propane heats homes off the gas mains, and its odorant smells of sulfur.
        "I smell propane in the basement",
        "I think there's a propane leak",
        "it smells like sulfur by the water heater",
        # Word orders the first pattern missed: the leak said after "gas", sulfur before "smell".
        "I think gas is leaking",
        "gas might be leaking from the stove",
        "the propane tank is leaking",
        "there's a sulfur smell in the basement",
        "it smells gassy in here",
        "it smells like something is burning",
        # A carbon monoxide detector gets the script on first mention, battery or not.
        "my CO detector is beeping, I think the battery is low",
        "the carbon monoxide detector has a low battery",
    ],
)
def test_hazard_phrases_trigger_the_safety_script(said):
    assert receptionist.hazard_in(said)


@pytest.mark.parametrize(
    "said",
    [
        "my furnace won't fire up",
        "I have a gas furnace and it won't turn on",
        # A furnace or water heater leaking water is a routine call, even when it burns gas.
        "my gas furnace is leaking water",
        "the gas water heater is leaking",
        "the AC is blowing warm air",
        "the heat pump is making a grinding noise",
    ],
)
def test_ordinary_calls_do_not_trigger_it(said):
    assert not receptionist.hazard_in(said)


@pytest.mark.parametrize(
    "said",
    [
        "my smoke detector keeps chirping",
        "the smoke detector battery died",
        "my smoke alarm is chirping for a new battery",
        "the smoke detector needs a new battery",
    ],
)
def test_a_smoke_detector_asking_for_a_battery_is_routine(said):
    assert not receptionist.hazard_in(said)


@pytest.mark.parametrize(
    "said",
    [
        "the CO alarm is going off and the battery is new",
        "the CO detector is beeping and I changed the battery",
        "I replaced the batteries and the CO alarm is still beeping",
        "the smoke detector is going off, maybe it's the battery",
        "my CO detector battery is low, and I have a headache",
        "the smoke alarm battery is low and I smell smoke",
        "the CO detector says low battery but I smell gas",
        "the CO detector does four beeps and a pause, maybe the battery",
        "the smoke detector's chirping, and the furnace is sparking",
        "it smells like something is burning, and the smoke detector battery is low",
    ],
)
def test_a_detector_with_any_sign_of_a_real_alarm_still_fires(said):
    assert receptionist.hazard_in(said, after_script=True)


@pytest.mark.parametrize(
    "said",
    [
        "No, it's just the CO detector battery",
        "No, the battery's dying",
        "No, it's just the battery",
        "no, it was just the smoke detector chirping",
    ],
)
def test_a_battery_answer_to_the_script_is_a_clear_no(said):
    assert receptionist.clear_no(said)
    assert not receptionist.confirms(said)


@pytest.mark.parametrize(
    ("said", "suspected"),
    [
        ("I don't think it's gas but there's a weird smell", True),
        ("I hope it's not gas, it smells funny", True),
        ("I don't know if it's the gas but it stinks in here", True),
        ("I don't think it's gas, the furnace just won't start", False),
        ("there's a weird smell from the vents", False),
        ("my gas furnace smells dusty", False),
    ],
)
def test_a_hedged_gas_smell_is_suspected(said, suspected):
    assert rules.gas_suspected(said) is suspected


def test_a_hedged_no_to_the_script_never_confirms_the_emergency():
    assert not receptionist.confirms("No, I don't think it's gas, it just smells weird.")


async def test_a_battery_answer_calls_off_the_page_without_reopening_it(db, monkeypatch):
    agent, line, pages, hung_up = gas_call(db, monkeypatch, hold=0.1)
    with pytest.raises(StopResponse):
        await turn(agent, "My CO detector is beeping, I think the battery is low.")
    line.handles[-1].done.set()
    await turn(agent, "No, it's just the CO detector battery.")  # the model replies
    await receptionist.asyncio.sleep(0.3)
    assert pages == [] and hung_up == [] and line.userdata.false_alarm
    assert line.said == [receptionist.SAFETY_SCRIPT]


async def test_a_hedged_gas_smell_gets_the_script(db, monkeypatch):
    agent, line, _, _ = gas_call(db, monkeypatch)
    with pytest.raises(StopResponse):
        await turn(agent, "I don't think it's gas but there's a weird smell in the kitchen.")
    assert line.said == [receptionist.SAFETY_SCRIPT]


class HazardLine:
    """Stands in for AgentSession when the backstop fires: the call, the history and what was said."""

    def __init__(self, call):
        self.userdata = call
        self.history = llm.ChatContext()
        self.said = []

    def interrupt(self, force=False):
        pass

    def say(self, text, allow_interruptions=True):
        self.said.append(text)
        return Played()


class Played:
    async def wait_for_playout(self):
        pass


async def test_the_turn_that_fires_the_backstop_is_kept_for_the_model_and_the_record(
    db, monkeypatch
):
    """Call 7: raising StopResponse makes LiveKit drop the turn, so the model never saw "smell gas
    in the kitchen" or the address, and the stored transcript lost the call's key sentence."""

    async def fake_page(title, message):
        pass

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    line = HazardLine(Call(call_id="call-a", db=db))
    monkeypatch.setattr(SummitAirAgent, "session", property(lambda self: line))
    agent = SummitAirAgent("")
    turn = llm.ChatMessage(
        role="user", content=["My address is 17 Severn. Smell gas in the kitchen."]
    )

    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(llm.ChatContext(), turn)

    assert line.said == [receptionist.SAFETY_SCRIPT]
    assert turn.id in {item.id for item in agent.chat_ctx.items}
    assert turn.id in {item.id for item in line.history.items}


async def test_the_safety_script_plays_even_when_the_emergency_task_cannot_be_written(
    db, monkeypatch
):
    """An exception out of on_user_turn_completed makes LiveKit skip the turn entirely: no script and
    no model reply. A locked database must not silence the safety script."""

    def locked(*args, **kwargs):
        raise store.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "add_task", locked)
    line = HazardLine(Call(call_id="call-a", db=db))
    monkeypatch.setattr(SummitAirAgent, "session", property(lambda self: line))
    turn = llm.ChatMessage(role="user", content=["I smell gas"])

    with pytest.raises(StopResponse):
        await SummitAirAgent("").on_user_turn_completed(llm.ChatContext(), turn)

    assert line.said == [receptionist.SAFETY_SCRIPT]
    assert line.userdata.hazard_task is None

    # Said once is enough: a yes gets the closing line, not the script again.
    with pytest.raises(StopResponse):
        await SummitAirAgent("").on_user_turn_completed(
            llm.ChatContext(),
            llm.ChatMessage(role="user", content=["yes, the gas smell is strong"]),
        )
    assert line.said[0] == receptionist.SAFETY_SCRIPT and len(line.said) == 2
    assert line.said[1].startswith("Okay. Get everyone outside now")
    assert "will call" not in line.said[1]  # no saved task or reachable number


async def test_on_call_is_paged_even_when_the_emergency_task_cannot_be_written(db, monkeypatch):
    """Once the safety script has played, the prompt tells the model the emergency task already
    exists, so it won't file one. If the backstop's write failed, the page is the only way on-call
    hears about it."""
    pages = []

    async def fake_page(title, message):
        pages.append(message)
        return True

    def locked(*args, **kwargs):
        raise store.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    monkeypatch.setattr(store, "add_task", locked)
    call = Call(call_id="call-a", caller_number="+19145550100", db=db)

    assert await flag_hazard(call, "I smell gas in the kitchen") is True
    await receptionist.asyncio.sleep(0)

    assert len(pages) == 1
    # No caller's words or number in a push (O2): the call id finds them in the database.
    assert "I smell gas" not in pages[0] and "9145550100" not in pages[0]
    assert "call-a" in pages[0]


async def test_an_emergency_the_model_filed_is_the_calls_one_emergency_task(db, monkeypatch):
    """The keyword list misses some hazards ("I smell propane"), so the model files those itself. A
    later keyword match or a second model call must not file a second task and page again."""
    pages = []

    async def fake_page(title, message):
        pages.append(title)

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db)
    ctx = FakeContext(call)
    await SummitAirAgent("").create_dispatch_task(
        ctx, "emergency", "propane smell", "in the basement"
    )
    assert await flag_hazard(call, "yes, I smell gas everywhere") is False
    again = await SummitAirAgent("").create_dispatch_task(
        ctx, "emergency", "propane", "still there"
    )
    assert "already exists" in again and "The callback target is" in again
    await receptionist.asyncio.sleep(0)
    with store.connect(db) as conn:
        assert (
            conn.execute("select count(*) from tasks where kind = 'emergency'").fetchone()[0] == 1
        )
    assert pages == ["Summit Air emergency #2001"]


async def test_a_hazard_files_one_emergency_task_per_call(db, monkeypatch):
    async def fake_page(title, message):
        pass

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db)
    assert await flag_hazard(call, "I smell gas") is True
    assert await flag_hazard(call, "yes, it's really strong gas smell") is False
    assert call.hazard_task == 2001
    # The model's repeat attempt is refused, but still hands it the target to tell the caller.
    again = await SummitAirAgent("").create_dispatch_task(
        FakeContext(call), "emergency", "gas", "strong smell"
    )
    assert "already exists" in again and "The callback target is" in again


@pytest.mark.parametrize(
    "said",
    [
        "Oh no. I am paging the on-call technician now, our target is a callback within 15 minutes.",
        "Our on-call technician will call you back tonight.",
        "Our target is a callback within 15 minutes.",
    ],
)
async def test_a_promised_page_is_filed_when_the_model_forgot(db, said, monkeypatch):
    """Simulated elderly call: GPT-4.1 mini promised the page and never called the tool."""

    async def fake_page(title, message):
        pass

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db)
    assert await keep_promise(call, said) is True
    assert await keep_promise(call, said) is False  # once is enough
    # The model filing its own urgent task afterwards gets the existing one, not a second page.
    again = await SummitAirAgent("").create_dispatch_task(
        FakeContext(call), "urgent", "no heat", "mother at home"
    )
    assert "already exists" in again and "The callback target is" in again
    with store.connect(db) as conn:
        assert [r[0] for r in conn.execute("select kind from tasks")] == ["urgent"]


async def test_ordinary_lines_and_filed_pages_file_nothing(db, monkeypatch):
    async def fake_page(title, message):
        pass

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db)
    for said in [
        "Our target is to call you back by 10 AM tomorrow.",
        "What's the address there?",
        "The diagnostic visit is $89.",
    ]:
        assert await keep_promise(call, said) is False
    await file_task(call, "urgent", "no heat, 80-year-old", "mother at home")
    assert await keep_promise(call, "Our on-call technician will call you back tonight.") is False
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("said", "dictation"),
    [
        ("Got it. What's the address there?", True),
        ("Thanks, Sam. What's the best number to reach you?", True),
        ("Which town is that in? And the ZIP?", True),
        ("Oh no. What's going on with it?", False),
        ("Is anyone there who'd be at risk in the cold?", False),
        (
            "I have 48 Severn Lane. Which window works for you?",
            False,
        ),  # the address, not a question
        ("You're booked. Your reference number is 1001.", False),  # no question at all
        # Confirmations that name a number or an address but want a yes (the 18:33 call).
        ("Is this number I'm speaking on the best to reach you?", False),
        ("Did I get the address right?", False),
        ("Can I get the address there?", True),
    ],
)
def test_the_long_wait_is_only_for_dictating_an_address_or_number(said, dictation):
    assert wants_dictation(said) is dictation


def said(role, text):
    return llm.ChatMessage(role=role, content=[text])


GREETED = said("assistant", receptionist.GREETING)


async def test_end_call_refuses_on_the_callers_first_turn():
    """The "Stop." heard over the greeting (dEwa8tRdFwLW) is the one hang-up code still refuses."""
    from types import SimpleNamespace

    call = Call(call_id="call-a", caller_turns=1)
    ctx = SimpleNamespace(session=SimpleNamespace(userdata=call))
    guard = next(t for t in SummitAirAgent("").tools if isinstance(t, GuardedEndCall))
    with pytest.raises(ToolError, match="Don't end the call yet"):
        await guard._end_call(ctx)
    assert not call.ended_by_agent


async def test_after_the_first_turn_the_model_decides_when_the_call_ends(monkeypatch):
    """ADR-022: "It's all good. K." (KSJ5YTzz9uHA) and "No. All good. Thank you." (Awb8xx294NFr)
    were refused by a word list; the model had them right."""
    from types import SimpleNamespace

    from livekit.agents.beta.tools import EndCallTool

    ended = []

    async def base_end_call(self, ctx):
        ended.append(True)

    monkeypatch.setattr(EndCallTool, "_end_call", base_end_call)
    call = Call(call_id="call-a", caller_turns=4)
    ctx = SimpleNamespace(session=SimpleNamespace(userdata=call))
    guard = next(t for t in SummitAirAgent("").tools if isinstance(t, GuardedEndCall))
    await guard._end_call(ctx)
    assert ended == [True] and call.ended_by_agent


# The prompt


def test_the_prompt_renders_every_placeholder():
    prompt = render_instructions(datetime(2026, 9, 29, 13, 5, tzinfo=TZ), "+19145550100")
    assert "{" not in prompt and "}" not in prompt
    assert "Tuesday, September 29, 2026" in prompt and "1:05 PM" in prompt
    assert "The office is open" in prompt and "914-555-0100" in prompt


def test_evenings_and_weekends_are_after_hours():
    assert "office is closed" in render_instructions(datetime(2026, 9, 29, 19, 0, tzinfo=TZ), None)
    assert "office is closed" in render_instructions(datetime(2026, 10, 3, 10, 0, tzinfo=TZ), None)


# The test harness


def test_no_test_can_page_the_real_on_call_phone():
    """tests/test_agent.py loads .env.local, which names the real ntfy topic, so on 9/25 a model test
    that filed an urgent task paged the on-call phone ("Summit Air urgent #2001" at 22:39)."""
    from evals import PAGE_TOPICS

    assert not set(PAGE_TOPICS) & set(receptionist.os.environ)  # keys only, never values


# The on-call page


@pytest.fixture
async def ntfy(monkeypatch):
    """A local stand-in for ntfy.sh that answers every page with the status the test sets."""
    from aiohttp import web

    reply = {"status": 200}

    async def publish(request):
        return web.Response(status=reply["status"])

    app = web.Application()
    app.router.add_post("/{topic}", publish)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(paging, "NTFY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    yield reply
    await runner.cleanup()


async def test_a_page_that_ntfy_refuses_is_logged_as_failed(ntfy, caplog):
    """ntfy answers a rate-limited or broken publish with an error status, not an exception, so a
    refused page used to pass as sent."""
    ntfy["status"] = 500
    assert await paging.page_on_call("Summit Air urgent #2001", "no heat") is False
    assert "push to NTFY_TOPIC failed" in caplog.text
    ntfy["status"] = 200
    assert await paging.page_on_call("Summit Air urgent #2002", "no heat") is True


async def test_the_model_is_told_paged_only_when_ntfy_accepted_the_page(db, ntfy):
    """The tool said "The on-call technician was paged" before the page was even attempted, and the
    model repeats what the tool says. The claim has to wait for the answer."""
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    ntfy["status"] = 503
    refused = await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat", "mother 78")
    assert "was paged" not in refused and "could not be confirmed" in refused
    ntfy["status"] = 200
    ctx = FakeContext(Call(call_id="call-b", db=db, caller_number="+19145550100"))
    accepted = await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat", "mother 78")
    assert "was paged" in accepted


def test_the_hosted_turn_detector_is_pinned(monkeypatch):
    """Unpinned, `start` on this Mac picks the local v1-mini and replies wait about 3.1 s (call 5)."""
    import agent

    monkeypatch.delenv("LIVEKIT_DEV_MODE", raising=False)
    monkeypatch.setenv("LIVEKIT_API_KEY", "test-key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "test-secret")
    assert agent.turn_detector().model == "turn-detector-v1"


@pytest.mark.parametrize(
    ("metadata", "health"),
    [
        ('{"healthcheck": true}', True),
        ("", False),
        (None, False),
        ('{"healthcheck": "yes"}', False),
        ("not json", False),
        ("[1]", False),
    ],
)
def test_only_the_watchdog_metadata_marks_a_health_check(metadata, health):
    import agent

    assert agent.is_healthcheck(metadata) is health


def test_a_heartbeat_is_recorded_per_room(db):
    assert store.heartbeat_at(db, "health-1") is None
    store.add_heartbeat(db, "health-1")
    assert store.heartbeat_at(db, "health-1") is not None
    assert store.heartbeat_at(db, "health-2") is None


def test_the_single_worker_never_refuses_a_call_for_cpu_load():
    """Under `start` the default threshold (0.7) marked the only worker unavailable six times on
    2026-09-27 while simulations ran on the Mac, and a call then has nowhere to go."""
    import math

    import agent

    assert math.isinf(agent.server._load_threshold)


# Urgent detected in code (A1)


@pytest.mark.parametrize(
    "said",
    [
        "My heat went out last night",
        "we have no heat",
        "the heat's out",
        "my furnace won't turn on",
        "the boiler isn't working",
        "it's freezing in here",
        "it's so cold in here",
        "the AC's out",
        "no AC and it's 95 out",
        "the air conditioner stopped working",
        "my AC isn't cooling at all",
        "it's way too hot in the apartment",
        "it's sweltering",
    ],
)
def test_a_failed_system_is_recognized(said):
    assert receptionist.SYSTEM_DOWN.search(said)


@pytest.mark.parametrize(
    "said",
    ["my AC is leaking water", "I'm due for a tune-up", "the thermostat is blank but it's warm"],
)
def test_a_working_system_is_not_called_down(said):
    assert not receptionist.SYSTEM_DOWN.search(said)


@pytest.mark.parametrize(
    "said",
    [
        "my mother is 80 and she lives with me",
        "my dad's staying with us, he's 86",
        "my grandma is here",
        "there's an elderly man upstairs",
        "she's a senior",
        "he's 72 years old",
        "an 86-year-old lives here",
        "I have a 3-month-old",
        "there's a newborn in the house",
        "my baby is here",
        "my husband is on oxygen",
        "my son has asthma",
        "she has COPD",
        "my wife is pregnant",
        "he's on dialysis",
        "she has a heart condition",
        "he's bedridden",
        "she has a medical condition",
        "I'm 82 and I live alone",
        "she's 84, and it's 55 degrees in her apartment",
        "my aunt lives here, she is 79",
        "I am 90",
        "my wife is 79",
        "my mother just turned 70",
    ],
)
def test_someone_at_risk_is_recognized(said):
    assert receptionist.at_risk_in(said, last_agent="")


@pytest.mark.parametrize(
    ("said", "last_agent"),
    [
        ("No, it's just me.", "Is anyone there who'd be at risk in the cold, like someone older?"),
        ("Nobody, I'm fine.", "Is anyone there who'd be at risk in the cold?"),
        ("No one elderly or anything.", "Anyone at risk there?"),
        ("Yes.", "What's the address there?"),  # a yes to something else
        ("I'm 35 years old.", ""),
        ("It's a 10-year-old furnace.", ""),
        ("I'm 40.", ""),
        ("it's 85 degrees in here", ""),
        ("he's 90 percent sure it's the thermostat", ""),
        ("I'm on 72nd Street", ""),
        ("Address is 72 Bergen Street.", ""),
        ("it is 88 in here", ""),
        ("the thermostat is 66", ""),
        ("we are 80 blocks away", ""),
        ("the fee is 89? that's a lot", ""),
        ("I'm 72 Bergen Street, Brooklyn", ""),
    ],
)
def test_no_one_at_risk_is_not_flagged(said, last_agent):
    assert not receptionist.at_risk_in(said, last_agent)


@pytest.mark.parametrize("said", ["Yes.", "Yeah, she is.", "yep", "He is.", "She is, yes."])
def test_a_plain_yes_to_the_risk_question_counts(said):
    asked = "Is anyone there who'd be at risk in the cold, like someone older, a baby, or someone with a health problem?"
    assert receptionist.at_risk_in(said, last_agent=asked)


class UrgentLine(HazardLine):
    """HazardLine plus the history the hook reads for the agent's last question."""


def urgent_agent(line, monkeypatch, pages):
    async def fake_page(title, message):
        pages.append((title, message))
        return True

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    monkeypatch.setattr(SummitAirAgent, "session", property(lambda self: line))
    return SummitAirAgent("")


async def test_no_heat_with_someone_at_risk_files_one_urgent_task_in_code(db, monkeypatch):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, pages)
    said = "My heat went out and my mother is 80, she lives with me."
    turn_ctx = llm.ChatContext()

    await agent.on_user_turn_completed(turn_ctx, llm.ChatMessage(role="user", content=[said]))

    call = line.userdata
    assert call.urgent_task == 2001 and call.paged
    assert [t for t, _ in pages] == ["Summit Air urgent #2001"]
    assert said not in pages[0][1] and "scripts/calls.py 2001" in pages[0][1]
    note = [i for i in turn_ctx.items if i.type == "message" and i.role == "system"]
    assert len(note) == 1
    assert "2001" in note[0].text_content and "was paged" in note[0].text_content
    assert "don't file" in note[0].text_content.lower()
    # Kept for later turns and the eval path, where turn_ctx is thrown away.
    assert any(i.role == "system" and "2001" in (i.text_content or "")
               for i in agent.chat_ctx.items if i.type == "message")  # fmt: skip
    # A later turn with the same facts files nothing new.
    await agent.on_user_turn_completed(
        llm.ChatContext(), llm.ChatMessage(role="user", content=["She's really cold, she's 80."])
    )
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1


async def test_risk_said_on_a_later_turn_still_files_urgent(db, monkeypatch):
    """risk_during_readback: "nobody" first, then "my dad's staying with us, he's 86" mid-readback."""
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, pages)
    for said in ["My furnace stopped working.", "No, it's just me."]:
        await agent.on_user_turn_completed(
            llm.ChatContext(), llm.ChatMessage(role="user", content=[said])
        )
    assert line.userdata.urgent_task is None
    await agent.on_user_turn_completed(
        llm.ChatContext(),
        llm.ChatMessage(role="user", content=["Oh, actually my dad's staying with us, he's 86."]),
    )
    assert line.userdata.urgent_task == 2001


async def test_a_yes_to_the_risk_question_files_urgent(db, monkeypatch):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, pages)
    await agent.on_user_turn_completed(
        llm.ChatContext(), llm.ChatMessage(role="user", content=["The heat's out."])
    )
    agent_ctx = agent.chat_ctx.copy()
    agent_ctx.add_message(
        role="assistant", content="Oh no. Is anyone there who'd be at risk in the cold?"
    )
    await agent.update_chat_ctx(agent_ctx)
    await agent.on_user_turn_completed(
        agent.chat_ctx.copy(), llm.ChatMessage(role="user", content=["Yes."])
    )
    assert line.userdata.urgent_task == 2001


async def test_nothing_urgent_is_filed_after_the_safety_script(db, monkeypatch):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, pages)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(
            llm.ChatContext(), llm.ChatMessage(role="user", content=["I smell gas"])
        )
    await agent.on_user_turn_completed(
        llm.ChatContext(),
        llm.ChatMessage(role="user", content=["No heat either, and my mother is 80."]),
    )
    assert line.userdata.urgent_task is None


async def test_an_urgent_call_books_with_priority_even_if_the_model_says_otherwise(db):
    call = Call(call_id="call-a", db=db, caller_number="+19145550100")
    ctx = FakeContext(call)
    await file_task(call, "urgent", "no heat, 80-year-old", "mother at home")
    agent = SummitAirAgent("")
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    await agent.book_appointment(
        ctx, "2026-09-29-0800", "residential", "Maria Lopez", "", "14 Maple Street, Brooklyn",
        "11225", "no heat", priority=False,
    )  # fmt: skip
    assert store.booking_for(db, "call-a")["priority"] == 1


# One visit per call (A2)


def test_the_store_says_whether_it_booked_moved_or_updated(db):
    first = store.book(db, **booking("call-a", "2026-09-29-0800"))
    assert first["change"] == "booked"
    same = store.book(db, **booking("call-a", "2026-09-29-0800"))
    assert same["change"] == "unchanged" and same["ref"] == first["ref"]
    updated = store.book(db, **booking("call-a", "2026-09-29-0800", issue="no heat and a leak"))
    assert updated["change"] == "updated" and updated["ref"] == first["ref"]
    moved = store.book(db, **booking("call-a", "2026-09-29-1200"))
    assert moved["change"] == "moved" and moved["ref"] == first["ref"]
    assert moved["previous"]["slot_id"] == "2026-09-29-0800"


async def checked_call(db):
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "any")
    return agent, ctx


BOOK_ARGS = ("residential", "Maria Lopez", "", "14 Maple Street, Brooklyn", "11225", "no heat")


async def test_a_new_window_on_the_same_call_is_reported_as_moved(db):
    agent, ctx = await checked_call(db)
    first = await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert first.startswith("Booked")
    ctx.userdata.turn += 1  # the caller's next turn
    moved = await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)
    assert moved.startswith("Moved from Tuesday, September 29, between 8 AM and noon to Tuesday")
    assert "1001" in moved
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from bookings").fetchone()[0] == 1


async def test_a_second_issue_at_the_same_slot_is_reported_as_updated(db):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    args = BOOK_ARGS[:-1] + ("no heat, and the AC is leaking",)
    ctx.userdata.turn += 1  # the caller's next turn
    updated = await agent.book_appointment(ctx, "2026-09-29-0800", *args)
    assert updated.startswith("Updated") and "1001" in updated


@pytest.mark.parametrize(
    ("street", "zip_code"),
    [("52 Oak Avenue, Brooklyn", "11225"), ("14 Maple Street, Queens", "11375")],
)
async def test_a_second_address_on_one_call_is_refused(db, street, zip_code):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    ctx.userdata.checked_zip = zip_code  # as if check_address had run on the new address
    ctx.userdata.checked_street = receptionist.street_key(street)
    ctx.userdata.turn += 1  # the caller's next turn
    with pytest.raises(ToolError, match="already booked #1001 at 14 Maple Street, Brooklyn"):
        await agent.book_appointment(
            ctx, "2026-09-29-1200", "residential", "Maria Lopez", "", street, zip_code, "no heat"
        )


async def test_the_same_street_written_differently_is_the_same_address(db):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    ctx.userdata.turn += 1  # the caller's next turn
    moved = await agent.book_appointment(
        ctx, "2026-09-29-1200", "residential", "Maria Lopez", "", "14 Maple St. Apt 2, Brooklyn",
        "11225", "no heat",
    )  # fmt: skip
    assert moved.startswith("Moved")


def test_the_prompt_books_two_problems_as_one_visit():
    assert "one visit" in receptionist.PROMPT


# Street names (A3)


@pytest.mark.parametrize(
    "street",
    ["1600 Broadway", "22 Avenue A", "350 5th Avenue", "37-12 81st St", "48 Bergen Street, Apt 2",
     "12B West End Ave", "7 Saint Marks Place"],
)  # fmt: skip
async def test_a_real_street_passes_the_address_check(db, street):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    result = await SummitAirAgent("").check_address(ctx, street, "Brooklyn", "11201")
    assert result.startswith("In the service area")


@pytest.mark.parametrize("street", ["487 Lane", "12 Street", "9 Ave", "5 Pkwy, Apt 3"])
async def test_a_street_type_with_no_street_name_is_sent_back(db, street):
    """Speech-to-text heard "48 Bergen Street" as "487 Lane" on a test call, and the model read it
    back as an address."""
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="Ask for the street name"):
        await SummitAirAgent("").check_address(ctx, street, "Brooklyn", "11201")
    assert ctx.userdata.checked_zip is None


@pytest.mark.parametrize("street", ["Bergen Street", "Broadway", ""])
async def test_a_street_without_a_house_number_is_sent_back(db, street):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="house number"):
        await SummitAirAgent("").check_address(ctx, street, "Brooklyn", "11201")


class Speaking(FakeContext):
    """A FakeContext whose session records what the tools say to the caller."""

    def __init__(self, call):
        from types import SimpleNamespace

        super().__init__(call)
        self.said = []
        self.session = SimpleNamespace(say=self.said.append)


async def test_the_booking_tool_tells_the_caller_the_confirmation_itself(db):
    """ADR-019: on the 1:06 PM call the model took a second round to word the confirmation, and
    the booking turn took 5.9 s. Code now says it, from the row just written."""
    agent = SummitAirAgent("")
    ctx = Speaking(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    result = await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert ctx.said == [
        (
            "Maria, you're booked for Tuesday, September 29, between 8 AM and noon at 14 Maple "
            "Street, Brooklyn. Your reference number is one oh oh one. Is there anything else I "
            "can help with?"
        )
    ]
    assert ctx.userdata.confirmation_spoken
    assert "add nothing this turn" in result
    ctx.userdata.turn += 1
    await agent.check_availability(ctx, "2026-09-29", "afternoon")
    await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)
    assert ctx.said[1].startswith("Done, you're now booked for Tuesday, September 29, between noon")
    assert "stays one oh oh one" in ctx.said[1]


async def test_a_refused_booking_says_nothing(db):
    ctx = Speaking(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError):
        await SummitAirAgent("").book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert ctx.said == [] and not ctx.userdata.confirmation_spoken


def test_the_model_reply_is_cancelled_only_after_a_spoken_confirmation():
    class Executed:
        cancelled = False

        def cancel_tool_reply(self):
            self.cancelled = True

    call = Call(call_id="call-a", db="")
    refused = Executed()
    receptionist.skip_reply_after_confirmation(call, refused)
    assert not refused.cancelled
    call.confirmation_spoken = True
    booked = Executed()
    receptionist.skip_reply_after_confirmation(call, booked)
    assert booked.cancelled and not call.confirmation_spoken


# Gas: negation, a held page, a closed emergency (A4)


@pytest.mark.parametrize(
    ("said", "fires"),
    [
        ("I don't smell gas", False),
        ("no smoke", False),
        ("My furnace won't turn on. And no, I don't smell gas or anything.", False),
        ("I don't really smell gas", False),
        ("it doesn't smell like gas", False),
        ("there's no gas leak", False),
        ("I don't know, I smell gas", True),
        ("not sure but I smell gas", True),
        ("I don't smell gas but the CO alarm is going off", True),
        ("I smell gas", True),
        ("I don't know if I smell gas", True),
    ],
)
def test_a_negated_hazard_does_not_fire(said, fires):
    assert receptionist.hazard_in(said) is fires


def test_tasks_carry_a_status_and_an_old_database_gains_the_column(tmp_path):
    path = tmp_path / "old.db"
    with store.connect(path) as conn:
        conn.execute(
            "create table tasks (ref integer primary key autoincrement, call_id text not null, "
            "kind text not null, reason text not null, summary text not null, name text not null "
            "default '', phone text not null default '', address text not null default '', "
            "due_at text not null, created_at text not null default (datetime('now')))"
        )
        conn.execute(
            "insert into tasks (call_id, kind, reason, summary, due_at) "
            "values ('old', 'callback', 'r', 's', 'x')"
        )
    store.init(path, WINDOWS, capacity=1, today=MONDAY_9AM.date(), days_ahead=1)
    store.init(path, WINDOWS, capacity=1, today=MONDAY_9AM.date(), days_ahead=1)  # idempotent
    with store.connect(path) as conn:
        assert [r[0] for r in conn.execute("select status from tasks")] == ["open"]
    ref = store.add_task(path, call_id="c", kind="emergency", reason="r", summary="s", name="",
                         phone="", address="", due_at="x")  # fmt: skip
    store.set_task_status(path, ref, "false_alarm")
    with store.connect(path) as conn:
        assert conn.execute("select status from tasks where ref = ?", (ref,)).fetchone()[0] == (
            "false_alarm"
        )


class Playout:
    def __init__(self):
        self.done = receptionist.asyncio.Event()

    async def wait_for_playout(self):
        await self.done.wait()


class EmergencyLine(HazardLine):
    """HazardLine whose speech can be played out on cue, for the hang-up after the closing line."""

    def __init__(self, call):
        super().__init__(call)
        self.handles = []

    def say(self, text, allow_interruptions=True):
        super().say(text, allow_interruptions)
        self.handles.append(Playout())
        return self.handles[-1]


def gas_call(db, monkeypatch, hold=0.2, cap=5):
    pages, hung_up = [], []

    async def fake_page(title, message):
        pages.append(title)
        return True

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    monkeypatch.setattr(receptionist, "PAGE_HOLD_SECONDS", hold)
    monkeypatch.setattr(receptionist, "PAGE_HOLD_CAP_SECONDS", cap)
    call = Call(call_id="call-a", db=db, caller_number="+19145550100", hang_up=hang_up)
    line = EmergencyLine(call)
    monkeypatch.setattr(SummitAirAgent, "session", property(lambda self: line))
    return SummitAirAgent(""), line, pages, hung_up


async def turn(agent, said, ctx=None):
    await agent.on_user_turn_completed(
        ctx or llm.ChatContext(), llm.ChatMessage(role="user", content=[said])
    )


async def test_the_script_plays_at_once_but_the_page_waits_for_the_answer(db, monkeypatch):
    agent, line, pages, _ = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "I think gas is leaking from my stove.")
    assert line.said == [receptionist.SAFETY_SCRIPT]
    assert line.userdata.hazard_task == 2001  # the task is written straight away
    await receptionist.asyncio.sleep(0.05)
    assert pages == []


async def test_a_clear_no_cancels_the_page_and_marks_a_false_alarm(db, monkeypatch):
    """dusty_smell: the first heat of the year smells of burning dust."""
    agent, _, pages, hung_up = gas_call(db, monkeypatch)
    with pytest.raises(StopResponse):
        await turn(agent, "There's a dusty burning smell from the vents.")
    turn_ctx = llm.ChatContext()
    await turn(agent, "No, just dusty.", turn_ctx)  # no StopResponse: the model replies
    await receptionist.asyncio.sleep(0.3)  # past the hold
    assert pages == [] and hung_up == []
    with store.connect(db) as conn:
        assert conn.execute("select status from tasks").fetchone()[0] == "false_alarm"
    note = [i.text_content for i in turn_ctx.items if i.type == "message" and i.role == "system"]
    assert len(note) == 1 and "no hazard" in note[0] and "normal call" in note[0]


@pytest.mark.parametrize(
    "said", ["no", "Nope.", "No, it's not.", "No I said I don't smell gas", "No, just dusty."]
)
def test_short_negatives_are_clear_nos(said):
    assert receptionist.clear_no(said)


@pytest.mark.parametrize(
    "said", ["Yes.", "No, but the CO alarm is beeping", "I'm not sure", "What do you mean?",
             "No no no, I smell it everywhere, it's really strong in the kitchen",
             "No heat either, and my mother is 80."],
)  # fmt: skip
def test_anything_else_is_not_a_clear_no(said):
    assert not receptionist.clear_no(said)


async def test_a_confirmed_emergency_closes_the_call_in_code(db, monkeypatch):
    agent, line, pages, hung_up = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "I think gas is leaking from my stove.")
    with pytest.raises(StopResponse):  # no model reply on the confirming turn
        await turn(agent, "Yes.")
    await receptionist.asyncio.sleep(0.05)
    assert pages == ["Summit Air emergency #2001"]  # released at once, not after the hold
    closing = line.said[-1]
    assert closing.startswith("Okay. Get everyone outside now and call 911 from there.")
    assert "call you at this number by" in closing and closing.endswith("Please hang up and go.")
    assert hung_up == []  # not before the line has played
    line.handles[-1].done.set()
    await receptionist.asyncio.sleep(0.05)
    assert hung_up == [True]
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 1


async def test_an_unclear_answer_sends_the_page_and_lets_the_model_reply(db, monkeypatch):
    agent, _, pages, hung_up = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "I smell gas.")
    await turn(agent, "What do you mean?")
    await receptionist.asyncio.sleep(0.05)
    assert pages == ["Summit Air emergency #2001"] and hung_up == []


async def test_the_page_goes_out_when_nobody_answers(db, monkeypatch):
    agent, line, pages, _ = gas_call(db, monkeypatch, hold=0.1)
    with pytest.raises(StopResponse):
        await turn(agent, "I smell gas.")
    line.handles[-1].done.set()  # the script has finished playing
    await receptionist.asyncio.sleep(0.3)
    assert pages == ["Summit Air emergency #2001"]


async def test_the_wait_for_an_answer_starts_when_the_script_ends(db, monkeypatch):
    """The script takes about 13 seconds to say. Counted from the hazard, a 15-second hold ran out
    before any caller could answer, so a "no" never stopped a page on a real call."""
    agent, line, pages, _ = gas_call(db, monkeypatch, hold=0.1)
    with pytest.raises(StopResponse):
        await turn(agent, "There's a dusty burning smell from the vents.")
    await receptionist.asyncio.sleep(0.3)  # longer than the hold, but the script is still playing
    assert pages == []
    line.handles[-1].done.set()
    await turn(agent, "No, just dusty.")
    await receptionist.asyncio.sleep(0.3)
    assert pages == [] and line.userdata.false_alarm


async def test_a_script_that_never_finishes_still_pages_by_the_cap(db, monkeypatch):
    agent, _, pages, _ = gas_call(db, monkeypatch, hold=5, cap=0.1)
    with pytest.raises(StopResponse):
        await turn(agent, "I smell gas.")
    await receptionist.asyncio.sleep(0.3)  # playout never reported finishing
    assert pages == ["Summit Air emergency #2001"]


async def test_the_page_goes_out_when_the_caller_hangs_up(db, monkeypatch):
    agent, line, pages, _ = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "I smell gas.")
    await receptionist.release_held_page(line.userdata)
    assert pages == ["Summit Air emergency #2001"]


async def test_a_hazard_after_a_false_alarm_reopens_and_pages(db, monkeypatch):
    agent, line, pages, _ = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "There's a burning smell.")
    await turn(agent, "No.")
    with pytest.raises(StopResponse):
        await turn(agent, "Wait, now the CO alarm is going off.")
    await receptionist.asyncio.sleep(0.05)
    assert pages == ["Summit Air emergency #2001"]
    assert line.said.count(receptionist.SAFETY_SCRIPT) == 2
    with store.connect(db) as conn:
        assert [tuple(r) for r in conn.execute("select kind, status from tasks")] == [
            ("emergency", "open")
        ]


async def test_urgent_detection_resumes_after_a_false_alarm(db, monkeypatch):
    agent, line, _, _ = gas_call(db, monkeypatch, hold=5)
    with pytest.raises(StopResponse):
        await turn(agent, "The heat's out and there's a burning smell.")
    await turn(agent, "No, just dusty. But my mother is 80 and it's freezing.")
    # "But" makes it not a clear no, so the page went out and urgent stays off while it stands.
    assert line.userdata.urgent_task is None
    agent2, line2, _, _ = gas_call(db, monkeypatch, hold=5)
    line2.userdata.call_id = "call-b"
    with pytest.raises(StopResponse):
        await turn(agent2, "The heat's out and there's a burning smell.")
    await turn(agent2, "No, just dusty.")
    await turn(agent2, "My mother is 80 and she lives here.")
    assert line2.userdata.urgent_task is not None


# Speakable numbers (A8)


@pytest.mark.parametrize(
    ("number", "spoken"),
    [
        ("+16505550142", "650-555-0142"),
        ("+19145550100", "914-555-0100"),
        ("9145550100", "914-555-0100"),
        ("+442079460000", "+442079460000"),  # not a US number: left as it came
    ],
)
def test_the_caller_number_is_written_the_way_it_is_said(number, spoken):
    assert receptionist.speak_phone(number) == spoken
    assert f"The caller's phone number is {spoken}." in render_instructions(MONDAY_9AM, number)


@pytest.mark.parametrize(("ref", "spoken"), [(1003, "one oh oh three"), (1027, "one oh two seven")])
def test_a_reference_is_spelled_for_speech(ref, spoken):
    assert receptionist.speak_digits(ref) == spoken


async def test_the_booking_result_spells_the_reference(db):
    agent, ctx = await checked_call(db)
    booked = await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert "1001" in booked and '"one oh oh one"' in booked
    ctx.userdata.turn += 1  # the caller's next turn
    moved = await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)
    assert '"one oh oh one"' in moved


class BookingLLM(llm.LLM):
    """A model that books on its first request and would say "SECOND ROUND" on any later one."""

    def __init__(self):
        super().__init__()
        self.requests = 0

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        self.requests += 1
        return BookingStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class BookingStream(llm.LLMStream):
    async def _run(self):
        import json

        if self._llm.requests == 1:
            args = dict(zip(["customer_type", "name", "callback_number", "address", "zip_code",
                             "issue"], BOOK_ARGS)) | {"slot_id": "2026-09-29-0800"}  # fmt: skip
            delta = llm.ChoiceDelta(
                role="assistant",
                tool_calls=[
                    llm.FunctionToolCall(
                        name="book_appointment", arguments=json.dumps(args), call_id="book-1"
                    )
                ],
            )
        else:
            delta = llm.ChoiceDelta(role="assistant", content="SECOND ROUND")
        self._event_ch.send_nowait(llm.ChatChunk(id=f"r{self._llm.requests}", delta=delta))


async def test_a_session_speaks_the_booking_once_with_one_model_request(db):
    """End to end in a text session: the tool's line is said, and the model is not asked again."""
    agent, ctx = await checked_call(db)
    call = ctx.userdata
    model = BookingLLM()
    async with AgentSession(llm=model, userdata=call) as session:
        await session.start(agent)
        await session.run(user_input="Morning works, and this number is fine.")
        said = [i.text_content for i in session.history.items
                if i.type == "message" and i.role == "assistant"]  # fmt: skip
    assert model.requests == 1
    assert said[-1].startswith("Maria, you're booked for Tuesday, September 29")
    assert not any("SECOND ROUND" in s for s in said)


# The failure ladder (A5)


class FailingLLM(llm.LLM):
    """A model whose every request fails the way an unreachable OpenAI does."""

    def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
        return FailingStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class FailingStream(llm.LLMStream):
    async def _run(self):
        raise APIConnectionError("OpenAI can't be reached")


async def test_a_model_that_fails_for_good_gets_the_line_a_callback_and_the_hang_up(db):
    import agent

    hung_up = []

    async def hang_up():
        hung_up.append(True)

    call = Call(call_id="call-a", db=db, caller_number="+19145550100", hang_up=hang_up)
    failing = llm.FallbackAdapter([FailingLLM(), FailingLLM()], attempt_timeout=0.5)
    async with AgentSession(
        llm=failing,
        userdata=call,
        conn_options=SessionConnectOptions(llm_conn_options=agent.LLM_CONN),
    ) as session:
        ladder = receptionist.FailureLadder(session, call)
        session.on("error", ladder.on_error)
        await session.start(SummitAirAgent(""))
        # The run fails with the model; what matters is what the ladder did about it.
        with contextlib.suppress(APIConnectionError, RuntimeError):
            await session.run(user_input="My AC stopped working.")
        for _ in range(100):
            if hung_up:
                break
            await receptionist.asyncio.sleep(0.05)
        said = [i.text_content for i in session.history.items
                if i.type == "message" and i.role == "assistant"]  # fmt: skip
    assert hung_up == [True]
    assert any(s.startswith("I'm sorry, I'm having trouble on my end.") for s in said)
    with store.connect(db) as conn:
        tasks = conn.execute("select kind, summary from tasks").fetchall()
    assert [t["kind"] for t in tasks] == ["callback"]
    assert "My AC stopped working." in tasks[0]["summary"]
    assert call.errors and call.errors[0].startswith("llm_error")


class Failed:
    def __init__(self, kind, recoverable=False):
        self.error = type("E", (), {"type": kind, "recoverable": recoverable, "error": "down"})()


async def ladder_run(db, kind, **flags):
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    call = Call(call_id="call-a", db=db, caller_number="+19145550100", hang_up=hang_up, **flags)
    line = HazardLine(call)
    ladder = receptionist.FailureLadder(line, call)
    ladder.on_error(Failed(kind))
    ladder.on_error(Failed(kind))  # a second error files nothing more
    await ladder.task
    with store.connect(db) as conn:
        kinds = [r[0] for r in conn.execute("select kind from tasks")]
    return line.said, kinds, hung_up


async def test_speech_to_text_failing_gets_the_line_too(db):
    said, kinds, hung_up = await ladder_run(db, "stt_error")
    assert len(said) == 1 and "call you back at this number by" in said[0]
    assert kinds == ["callback"] and hung_up == [True]


async def test_a_failed_voice_files_the_task_and_hangs_up_without_speaking(db):
    said, kinds, hung_up = await ladder_run(db, "tts_error")
    assert said == [] and kinds == ["callback"] and hung_up == [True]


async def test_a_dropped_call_with_someone_at_risk_is_filed_urgent(db, monkeypatch):
    async def fake_page(title, message):
        return True

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    said, kinds, _ = await ladder_run(db, "llm_error", system_down=True, at_risk=True)
    assert kinds == ["urgent"]
    # The urgent target (15 minutes), not the routine one (two office hours).
    due = receptionist.now() + receptionist.timedelta(minutes=15)
    assert said[0].endswith(f"by {receptionist.speak_due(due, receptionist.now())}.")


def test_a_recoverable_error_is_left_to_the_retry(db):
    call = Call(call_id="call-a", db=db)
    ladder = receptionist.FailureLadder(HazardLine(call), call)
    ladder.on_error(Failed("llm_error", recoverable=True))
    assert ladder.task is None and call.errors == []


def test_only_openai_models_run_and_only_on_the_key(monkeypatch):
    from models import make_llm

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert make_llm("openai/gpt-4.1-mini").model == "gpt-4.1-mini"
    with pytest.raises(ValueError):
        make_llm("google/gemma-4-31b-it")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError):
        make_llm("openai/gpt-4.1-mini")


def test_speech_never_falls_back_to_the_livekit_credit(monkeypatch):
    import agent

    assert not hasattr(agent, "inworld_voice")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        agent.speech()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        agent.backup_voice()


def test_the_worker_names_every_missing_key(monkeypatch):
    import agent

    monkeypatch.setenv("TTS_PROVIDER", "deepgram")

    for key in agent.REQUIRED_KEYS:
        monkeypatch.setenv(key, "x")
    assert agent.missing_keys() == []
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    assert agent.missing_keys() == ["DEEPGRAM_API_KEY"]


def test_each_model_gets_two_and_a_half_seconds_and_the_pair_is_not_retried(monkeypatch):
    import agent

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert agent.language_model()._attempt_timeout == 2.5
    assert agent.LLM_CONN.max_retry == 0


# Off-script calls (A6)


async def test_end_call_goes_through_on_a_wrong_number(monkeypatch):
    from types import SimpleNamespace

    from livekit.agents.beta.tools import EndCallTool

    ended = []

    async def base_end_call(self, ctx):
        ended.append(True)

    monkeypatch.setattr(EndCallTool, "_end_call", base_end_call)
    ctx = SimpleNamespace(session=SimpleNamespace(userdata=Call(call_id="call-a", caller_turns=2)))
    guard = next(t for t in SummitAirAgent("").tools if isinstance(t, GuardedEndCall))
    await guard._end_call(ctx)
    assert ended == [True]


def spanish_agent(db, monkeypatch, number="+19145550100"):
    line = HazardLine(Call(call_id="call-a", db=db, caller_number=number))
    monkeypatch.setattr(SummitAirAgent, "session", property(lambda self: line))
    return SummitAirAgent(""), line


async def test_spanish_gets_the_fixed_line_and_a_callback_filed_in_code(db, monkeypatch):
    agent, line = spanish_agent(db, monkeypatch)
    with pytest.raises(StopResponse):
        await turn(agent, "Hola, ¿habla español?")
    assert len(line.said) == 1
    assert line.said[0].startswith("Lo siento, por ahora solo atendemos en inglés. Le llamaremos")
    assert line.said[0].endswith("Si prefiere, podemos seguir en inglés.")
    with store.connect(db) as conn:
        tasks = conn.execute("select kind, summary from tasks").fetchall()
    assert [t["kind"] for t in tasks] == ["callback"] and "habla" in tasks[0]["summary"]
    notes = [i.text_content for i in agent.chat_ctx.items if i.type == "message" and i.role == "system"]  # fmt: skip
    assert len(notes) == 1 and "Never promise a Spanish speaker" in notes[0]
    assert "Don't create another callback task" in notes[0]
    # Once only.
    await turn(agent, "Me llamo Sam, por favor.")
    assert len(line.said) == 1


async def test_spanish_after_the_second_turn_is_left_to_the_model(db, monkeypatch):
    agent, line = spanish_agent(db, monkeypatch)
    await turn(agent, "Hi, my AC is leaking.")
    await turn(agent, "It's at my house.")
    await turn(agent, "My wife says hola.")
    assert line.said == []


async def test_spanish_with_a_withheld_number_asks_for_one_and_files_nothing_yet(db, monkeypatch):
    agent, line = spanish_agent(db, monkeypatch, number=None)
    with pytest.raises(StopResponse):
        await turn(agent, "Hola, mi calefacción no funciona.")
    assert line.said == [receptionist.SPANISH_LINE_NO_NUMBER]
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("due", "spoken"),
    [
        (datetime(2026, 9, 29, 14, 35, tzinfo=TZ), "antes de las 2:35 de la tarde"),
        (datetime(2026, 9, 29, 12, 0, tzinfo=TZ), "antes del mediodía"),
        (datetime(2026, 9, 29, 13, 0, tzinfo=TZ), "antes de la 1 de la tarde"),
        (datetime(2026, 9, 30, 10, 0, tzinfo=TZ), "mañana antes de las 10 de la mañana"),
        (datetime(2026, 10, 5, 10, 0, tzinfo=TZ), "el lunes antes de las 10 de la mañana"),
    ],
)
def test_the_spanish_callback_target(due, spoken):
    assert receptionist.spanish_due(due, datetime(2026, 9, 29, 12, 35, tzinfo=TZ)) == spoken


def test_the_prompt_names_the_services_and_opens_neutrally():
    text = render_instructions(MONDAY_9AM, "+19145550100")
    assert (
        "works on heating, cooling, heat pumps, boilers, ductless mini splits and maintenance"
        in text
    )
    assert "doesn't do plumbing, water heaters or appliances" in text
    assert '"What can we help you with?"' in text
    assert "understand the problem" not in text
    assert "new install" in text


# Latency (A7)


async def test_a_checked_address_offers_no_times_until_check_availability(db, monkeypatch):
    """2026-09-28 10:01: with the windows in check_address's result, the model said "is that
    right?" and offered them in the same reply. Code that then released them on a yes took the
    wrong yes (3:51 PM call). Now the model calls check_availability after the yes (ADR-022)."""
    monkeypatch.setattr(receptionist, "now", lambda: MONDAY_9AM)
    call = Call(call_id="call-a", db=db, caller_number="+19145550100")
    ctx = FakeContext(call)
    agent = SummitAirAgent("")
    result = await agent.check_address(ctx, "48 Bergen Street", "Brooklyn", "11201")
    assert "no times" in result and "check_availability" in result and "slot_id" not in result
    assert not call.offered
    with pytest.raises(ToolError, match="not offered"):
        await agent.book_appointment(
            ctx, "2026-09-28-1200", "residential", "Maria Lopez", "", "48 Bergen Street, Brooklyn",
            "11201", "furnace won't start",
        )  # fmt: skip


# One booking write per caller turn


async def test_two_bookings_in_one_turn_leave_the_store_on_what_the_caller_heard(db):
    """change_window, 1 of 6 simulated runs: the model sent two book_appointment calls in one
    turn, the new window then the old one. LiveKit runs a turn's tool calls concurrently, both came
    back "Moved", and the store ended on the old window while the caller heard the new one. Only the
    first write of a turn may go through; the second is refused and told what stands."""
    import asyncio

    agent, ctx = await checked_call(db)
    call = ctx.userdata
    call.turn = 1
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    call.turn = 2  # the caller asks for the afternoon; the model answers with two calls at once
    results = await asyncio.gather(
        agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS),
        agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS),
        return_exceptions=True,
    )
    assert results[0].startswith("Moved from Tuesday, September 29, between 8 AM and noon")
    assert isinstance(results[1], ToolError)
    assert "already" in str(results[1]) and "noon and 4 PM" in str(results[1])
    assert store.booking_for(db, "call-a")["slot_id"] == "2026-09-29-1200"


async def test_a_refused_booking_does_not_use_up_the_turn(db):
    """A booking the guards refuse (a window never offered) is not a write, so the model's corrected
    call in the same turn still goes through."""
    agent, ctx = await checked_call(db)
    ctx.userdata.turn = 1
    with pytest.raises(ToolError, match="not offered"):
        await agent.book_appointment(ctx, "2026-10-01-0800", *BOOK_ARGS)
    assert (await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)).startswith("Booked")


async def test_the_next_caller_turn_can_book_again(db):
    agent, ctx = await checked_call(db)
    ctx.userdata.turn = 1
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    ctx.userdata.turn = 2
    assert (await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)).startswith("Moved")


async def test_each_caller_turn_is_counted(db, monkeypatch):
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, [])
    for n in (1, 2):
        await turn(agent, "my furnace is making a noise")
        assert line.userdata.turn == n


def test_the_model_may_not_send_two_tool_calls_at_once(monkeypatch):
    """The other half of the guarantee: OpenAI is told not to emit parallel tool calls, so a turn
    can't carry two bookings, or a callback filed beside an address check (call KTWmzz), or end_call
    beside a task (call 2). Only when the request carries tools: OpenAI refuses the setting on a
    request without them (400), which is what the simulated caller and the test judge send."""
    from livekit.plugins import openai

    from models import make_llm

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = []
    monkeypatch.setattr(openai.LLM, "chat", lambda self, **kw: seen.append(kw))
    model = make_llm("openai/gpt-4.1-mini")
    tool = next(t for t in SummitAirAgent("").tools if getattr(t, "id", "") == "end_call")
    model.chat(chat_ctx=llm.ChatContext(), tools=[tool])
    model.chat(chat_ctx=llm.ChatContext())
    model.chat(chat_ctx=llm.ChatContext(), tools=[])
    from livekit.agents.types import NOT_GIVEN

    assert seen[0]["parallel_tool_calls"] is False
    assert seen[1]["parallel_tool_calls"] is NOT_GIVEN
    assert seen[2]["parallel_tool_calls"] is NOT_GIVEN


# A ZIP the caller never said, and an address without one


class SpokenContext(FakeContext):
    """FakeContext plus the call so far, which check_address reads to make sure the ZIP it was
    given was actually said, and that the caller was asked for one before it is left blank. A
    line starting "AGENT: " is the agent's; the rest are the caller's."""

    def __init__(self, call, said):
        super().__init__(call)
        history = llm.ChatContext()
        for text in said:
            if text.startswith("AGENT: "):
                history.add_message(role="assistant", content=text[7:])
            else:
                history.add_message(role="user", content=text)
        self.session = type("Session", (), {"history": history})()


ZIP_ASKED = "AGENT: And what's the ZIP code there?"
NUMBER_ASKED = "AGENT: Is the number you're calling from the best one to reach you?"


@pytest.mark.parametrize(
    "said",
    [
        "48 Bergen Street, Brooklyn, 11201",
        "It's 48 Bergen Street in Brooklyn, one one two oh one",
        "eleven two oh one",
        "one twelve zero one",
        "ten thousand... no, 11201",
        "Brooklyn 11,201",
        "one-one-two-oh-one",
        "eleven-two-oh-one",
        "1 1 2 0 1",
        "11 201",
        "ZIP's 11201",
        "eleven two hundred one",
    ],
)
async def test_a_zip_the_caller_said_passes(db, said):
    ctx = SpokenContext(Call(call_id="call-a", db=db), ["My furnace is out.", said])
    result = await SummitAirAgent("").check_address(ctx, "48 Bergen Street", "Brooklyn", "11201")
    assert result.startswith("In the service area")


async def test_a_zip_the_caller_never_said_is_refused(db):
    """Call riWFX67: the caller said "I forgot" and the model checked and booked a ZIP it
    made up."""
    ctx = SpokenContext(
        Call(call_id="call-a", db=db),
        ["My AC is broken.", "48 Bergen Street in Brooklyn.", "I forgot."],
    )
    with pytest.raises(ToolError, match="never said ZIP 11201"):
        await SummitAirAgent("").check_address(ctx, "48 Bergen Street", "Brooklyn", "11201")
    assert ctx.userdata.checked_zip is None


@pytest.mark.parametrize(
    "town", ["Brooklyn", "brooklyn", "Manhattan", "New York", "NYC", "Astoria"]
)
async def test_a_covered_borough_books_without_a_zip(db, town):
    ctx = SpokenContext(
        Call(call_id="call-a", db=db),
        ["48 Bergen Street in " + town, ZIP_ASKED, "I don't know it, sorry.", NUMBER_ASKED, "Yes."],
    )
    agent = SummitAirAgent("")
    result = await agent.check_address(ctx, "48 Bergen Street", town, "")
    assert result.startswith("In the service area") and "no ZIP" in result
    assert ctx.userdata.checked_zip == ""
    await agent.check_availability(ctx, "2026-09-29", "any")
    booked = await agent.book_appointment(
        ctx, "2026-09-29-0800", "residential", "Maria Lopez", "+19145550100",
        "48 Bergen Street, " + town, "", "no heat",
    )  # fmt: skip
    assert booked.startswith("Booked.")
    assert store.booking_for(db, "call-a")["zip"] == ""


async def test_a_town_outside_the_area_without_a_zip_is_not_booked(db):
    ctx = SpokenContext(
        Call(call_id="call-a", db=db), ["14 Maple Avenue in Yonkers", ZIP_ASKED, "No idea."]
    )
    result = await SummitAirAgent("").check_address(ctx, "14 Maple Avenue", "Yonkers", "")
    assert "outside the service area" in result and "Don't offer times" in result
    assert ctx.userdata.checked_zip is None


async def test_booking_without_a_zip_needs_the_borough_check_first(db):
    """A blank ZIP at booking time is not a way around check_address."""
    agent, ctx = await checked_call(db)  # checked with ZIP 11225
    with pytest.raises(ToolError, match="hasn't been checked"):
        await agent.book_appointment(
            ctx, "2026-09-29-0800", "residential", "Maria Lopez", "", "14 Maple Street, Brooklyn",
            "", "no heat",
        )  # fmt: skip


async def test_a_zip_that_was_never_said_cannot_be_booked_either(db):
    """The booking ZIP must be the checked one, so an invented ZIP can't enter at booking."""
    ctx = SpokenContext(
        Call(call_id="call-a", db=db),
        ["48 Bergen Street in Brooklyn", ZIP_ASKED, "Don't know.", NUMBER_ASKED, "Yes."],
    )
    agent = SummitAirAgent("")
    await agent.check_address(ctx, "48 Bergen Street", "Brooklyn", "")
    await agent.check_availability(ctx, "2026-09-29", "any")
    with pytest.raises(ToolError, match="hasn't been checked"):
        await agent.book_appointment(
            ctx, "2026-09-29-0800", "residential", "Maria Lopez", "+19145550100",
            "48 Bergen Street, Brooklyn", "11201", "no heat",
        )  # fmt: skip


def test_a_context_without_a_history_skips_the_zip_check(db):
    """The offline tests' FakeContext has no session; the check needs one to run."""
    assert receptionist.caller_digits(None) == ""


# A corrected address is the same visit; a second address is not


@pytest.mark.parametrize(
    ("held", "new", "same"),
    [
        ("14 Maple Street, Brooklyn", "14 Maple St. Apt 2", True),  # the same place, rewritten
        ("14 Maple Street, Brooklyn", "40 Maple Street, Brooklyn", True),  # "forty, not fourteen"
        ("48 Burger Street, Brooklyn", "48 Bergen Street, Brooklyn", True),  # a misheard street
        ("14 Maple Street, Brooklyn", "310 Ocean Avenue, Brooklyn", False),  # a second address
        ("14 Maple Street, Brooklyn", "14 Ocean Avenue, Brooklyn", False),  # a second address
        ("48 Bergen Street, Brooklyn", "48 Dean Street, Brooklyn", False),  # a second address
        ("48 Bergen Street, Brooklyn", "48 Bergan Street, Brooklyn", True),  # a spelling
    ],
)
def test_a_correction_keeps_the_visit_and_a_second_address_does_not(held, new, same):
    assert receptionist.same_visit({"address": held, "zip": "11225"}, new, "11225") is same


def test_a_different_zip_is_always_a_second_address():
    assert not receptionist.same_visit(
        {"address": "14 Maple Street, Brooklyn", "zip": "11225"}, "14 Maple Street, Queens", "11375"
    )


async def test_a_corrected_house_number_after_booking_moves_the_same_booking(db):
    """Paul's line: "wait, did you say fourteen? It's forty." The row must say 40, not a callback
    while the technician drives to 14."""
    agent, ctx = await checked_call(db)
    ctx.userdata.turn = 1
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    ctx.userdata.turn = 2
    await agent.check_address(ctx, "40 Maple Street", "Brooklyn", "11225")  # read back again
    updated = await agent.book_appointment(
        ctx, "2026-09-29-0800", "residential", "Maria Lopez", "", "40 Maple Street, Brooklyn",
        "11225", "no heat",
    )  # fmt: skip
    assert updated.startswith("Updated")
    assert store.booking_for(db, "call-a")["address"] == "40 Maple Street, Brooklyn"


async def test_the_booking_turn_is_the_one_the_write_started_in(db, monkeypatch):
    """The next caller turn can complete while the store write is still in its thread. The write
    belongs to the turn it started in, so the caller's change in the next turn is not refused."""
    agent, ctx = await checked_call(db)
    call = ctx.userdata
    real_book = store.book

    def slow_book(path, **booking):
        call.turn += 1  # the next turn arrives mid-write
        return real_book(path, **booking)

    monkeypatch.setattr(store, "book", slow_book)
    call.turn = 1
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert call.booked_turn == 1 and call.turn == 2
    monkeypatch.setattr(store, "book", real_book)
    assert (await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)).startswith("Moved")


async def test_the_booking_confirmation_stays_interruptible(db):
    """LiveKit drops a caller turn that completes while the agent can't be interrupted, without
    running on_user_turn_completed, so an uninterruptible confirmation would lose "hold on, I
    smell gas" said over it (agent_activity: "skipping reply to user input")."""

    class Recording(FakeContext):
        def disallow_interruptions(self):
            raise AssertionError("the booking must not switch interruptions off")

    ctx = Recording(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = SummitAirAgent("")
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "any")
    assert (await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)).startswith("Booked")


def test_the_line_and_the_simulator_allow_the_same_number_of_tool_steps():
    import agent as line
    from evals import runner

    assert receptionist.MAX_TOOL_STEPS == 5
    assert "max_tool_steps=MAX_TOOL_STEPS" in (receptionist.ROOT / "src" / "agent.py").read_text()
    assert runner.MAX_TOOL_STEPS == line.MAX_TOOL_STEPS == 5


# An urgent task straight over "no, it's just me"


@pytest.mark.parametrize(
    "said",
    ["No, it's just me. I'm a healthy adult.", "Nobody, I'm fine.", "No.", "No one, just me."],
)
async def test_an_urgent_task_right_after_the_caller_denies_risk_is_refused(db, said):
    """cold_no_risk_night, discovery run: the model paged on-call the turn after the caller said
    it was just them. Since 2026-09-28 no heat in the cold is urgent whoever is home, so the
    denial holds only when the caller hasn't said it's cold."""
    ctx = SpokenContext(
        Call(call_id="call-a", db=db, caller_number="+19145550100"),
        [
            "My furnace won't kick on.",
            "AGENT: Is anyone there who'd be at risk in the cold, like someone older or a baby?",
            said,
        ],
    )
    with pytest.raises(ToolError, match="nobody there is at risk"):
        await SummitAirAgent("").create_dispatch_task(
            ctx, "urgent", "no heat, healthy adult", "no heat"
        )
    assert store.tasks_for(db, "call-a") == []


@pytest.mark.parametrize(
    "said",
    [
        "It's dangerous for her, she can't take the cold.",
        "No, but my neighbor's kid is here and he's sick.",
        "48 Bergen Street, Brooklyn.",
        "No, she just had a stroke.",
        "No one old. My wife is on chemo.",
        "No, he has a pacemaker.",
        "No, just me, I'm diabetic.",
    ],
)
async def test_softer_urgency_the_rules_cannot_see_is_still_the_models_call(db, said):
    ctx = SpokenContext(Call(call_id="call-a", db=db, caller_number="+19145550100"), [said])
    result = await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat", "no heat")
    assert result.startswith("Task 2001 created")


def test_the_prompt_says_how_to_handle_a_missing_zip_and_a_split_address():
    assert "ZIP left blank" in receptionist.PROMPT
    assert "in pieces" in receptionist.PROMPT


@pytest.mark.parametrize(
    "said",
    [
        # 2026-09-28 10:01, call eQGJXmApj2C5: the address and "I don't know the ZIP" in one turn
        [
            "Heat's out of my mother's 80.",
            "Forty eight Bergen Street, Brooklyn. I don't know the ZIP.",
        ],
        # 2026-09-28 10:04, call KwzEuKkUqMrK: the whole request in one monologue
        [
            (
                "Hi. I wanna mute eight a, uh, AC unit. There's nothing wrong with my current one. "
                "I just want an upgrade. I'm at 48 Bergen Street, Brooklyn, I don't know the ZIP. "
                "My name is Sam Rivera. I'm available tomorrow morning."
            )
        ],
        ["48 Bergen Street, Brooklyn.", "No zip code, sorry."],
    ],
)
async def test_a_caller_who_said_they_dont_know_the_zip_is_not_asked_again(db, said):
    ctx = SpokenContext(Call(call_id="call-a", db=db), said)
    result = await SummitAirAgent("").check_address(ctx, "48 Bergen Street", "Brooklyn", "")
    assert "no ZIP needed" in result
    assert ctx.userdata.checked_zip == ""


@pytest.mark.parametrize(
    "name", ["Your sister", "my mom", "the tenant", "her husband", "Sister", "caller"]
)
def test_a_relation_is_not_a_name(name):
    assert not receptionist.is_real_name(name)


@pytest.mark.parametrize("name", ["Ana", "Sam Rivera", "Maria Lopez", "S. Rivera", "Mrs. Chen"])
def test_a_real_name_still_is_one(name):
    assert receptionist.is_real_name(name)


@pytest.mark.parametrize(
    ("said", "digits"),
    [
        ("eleven two twenty-one", "11221"),
        ("ten oh twenty-five", "10025"),
        ("eleven three seventy-five", "11375"),
        ("one one two oh one", "11201"),
        ("48 Bergen, one twelve zero one", "4811201"),
    ],
)
def test_number_words_are_read_the_way_zips_are_said(said, digits):
    assert "".join(receptionist.spoken_numbers(said)) == digits


@pytest.mark.parametrize(
    ("town", "covered"),
    [
        ("Brooklyn, NY", True),
        ("Queens, New York", True),
        ("New York, NY", True),
        ("Williamsburg", True),
        ("LIC", True),
        ("Kew Gardens Hills", True),
        ("Floral Park", False),  # straddles the Nassau line: the ZIP decides
        ("Yonkers", False),
        ("Staten Island", False),
    ],
)
def test_the_towns_a_caller_may_give_without_a_zip(town, covered):
    assert receptionist.town_covered(town) is covered


async def test_a_blank_zip_cannot_carry_an_unchecked_address_into_the_booking(db):
    """After a borough check of 48 Bergen Street, a booking at 12 Elm Street, Yonkers with the ZIP
    blank is not the checked address."""
    ctx = SpokenContext(
        Call(call_id="call-a", db=db),
        ["48 Bergen Street in Brooklyn", ZIP_ASKED, "Don't know.", NUMBER_ASKED, "Yes."],
    )
    agent = SummitAirAgent("")
    await agent.check_address(ctx, "48 Bergen Street", "Brooklyn", "")
    await agent.check_availability(ctx, "2026-09-29", "any")
    with pytest.raises(ToolError, match="hasn't been checked"):
        await agent.book_appointment(
            ctx, "2026-09-29-0800", "residential", "Maria Lopez", "+19145550100",
            "12 Elm Street, Yonkers", "", "no heat",
        )  # fmt: skip


# A booking confirmation with no booking behind it


async def test_a_confirmation_with_no_booking_gets_a_correction_note(db, monkeypatch):
    """cold_no_risk_night on the demo clock, 2026-09-27 22:43: "Which works?" then, in the same
    reply, "Sam, you're booked for Wednesday... Your reference number is one two three four",
    with nothing booked. The model is told, so its next reply corrects it."""
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, [])
    said = "Which works? Sam, you're booked for Wednesday. Your reference number is one two three four."
    assert await receptionist.flag_fabricated_confirmation(agent, line.userdata, said)
    assert line.userdata.fabricated_confirmations == 1
    assert any(
        i.type == "message" and i.role == "system" and "not booked yet" in (i.text_content or "")
        for i in agent.chat_ctx.items
    )


async def test_a_real_confirmation_is_left_alone(db, monkeypatch):
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, [])
    store.book(db, **booking("call-a", "2026-09-29-0800"))
    said = "Maria, you're booked for Tuesday, September 29. Your reference number is one oh oh one."
    assert not await receptionist.flag_fabricated_confirmation(agent, line.userdata, said)
    assert line.userdata.fabricated_confirmations == 0


@pytest.mark.parametrize(
    "said",
    [
        "You're all set. Our target is to call you back by 10 AM tomorrow.",
        "Sorry, Wednesday morning is booked, but Thursday 8 AM to noon is open.",
        "Since you're scheduled for today between 8 and noon, I've asked dispatch to call you.",
        "Which window would you like booked?",
        "It's not booked yet; which window works?",
        "Just to confirm, 48 Bergen Street, Brooklyn, right?",
    ],
)
def test_honest_lines_are_not_called_fabricated(said):
    assert not receptionist.FABRICATED.search(said)


@pytest.mark.parametrize(
    "said",
    [
        "Sam, you're booked for Wednesday, September 30, between 8 AM and noon.",
        "You're now booked for Thursday morning.",
        "I have you down for Tuesday between noon and 4.",
        "Your confirmation number is one two three four.",
        "You've been booked in for Wednesday.",
    ],
)
def test_the_confirmation_phrases_are_recognized(said):
    assert receptionist.FABRICATED.search(said)


# Edge cases from review


@pytest.mark.parametrize(
    "town", ["Park Slope, Brooklyn", "Upper West Side, Manhattan", "Manhattan, New York City"]
)
def test_a_neighborhood_with_its_borough_is_covered(town):
    assert receptionist.town_covered(town)


async def test_a_bare_no_to_the_zip_question_is_not_a_denial_of_risk(db):
    ctx = SpokenContext(
        Call(call_id="call-a", db=db, caller_number="+19145550100"),
        ["My heat is out and my husband is on chemo.", "AGENT: Do you know the ZIP code?", "No."],
    )
    result = await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat, chemo", "x")
    assert result.startswith("Task 2001 created")


async def test_a_bare_no_to_the_risk_question_is_one(db):
    ctx = SpokenContext(
        Call(call_id="call-a", db=db, caller_number="+19145550100"),
        ["My heat is out.", "AGENT: Is anyone there who'd be at risk in the cold?", "No."],
    )
    with pytest.raises(ToolError, match="nobody there is at risk"):
        await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat", "x")


@pytest.mark.parametrize("said", ["My husband's 81.", "My neighbor's 90 and alone."])
def test_an_age_after_a_possessive_relation_counts(said):
    assert receptionist.at_risk_in(said, "")


@pytest.mark.parametrize(
    ("held", "new", "same"),
    [
        ("150 West 72nd Street, Manhattan", "150 West 73rd Street, Manhattan", False),
        ("12 Avenue A, Manhattan", "12 Avenue C, Manhattan", False),
        ("48 Bergen Street, Apt 2, Brooklyn", "48 Bergen St, Brooklyn", True),
    ],
)
def test_street_names_with_two_words_are_told_apart(held, new, same):
    assert receptionist.same_visit({"address": held, "zip": "10023"}, new, "10023") is same


def test_a_unit_first_address_still_keys_on_the_street():
    assert receptionist.street_key("Apartment 3B, 48 Bergen Street, Brooklyn") == ["48", "bergen"]


def test_double_one_is_two_ones():
    assert "".join(receptionist.spoken_numbers("double one two oh one")) == "11201"


async def test_the_booking_fills_in_the_urgent_task_filed_before_the_address(db):
    """2026-09-28 10:01, task 2015: code filed the urgent task on the first turn, before a name or
    an address, and nothing filled them in after the booking, so dispatch saw an urgent job with
    no address. A field the task already had keeps its value."""
    agent = SummitAirAgent("")
    call = Call(call_id="call-a", db=db, caller_number="+19145550100")
    ctx = FakeContext(call)
    await file_task(call, "urgent", "no heat or cooling with someone at risk", "Heat's out")
    await file_task(call, "callback", "manager", "tech never came", name="Maria")
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-09-29", "morning")
    await agent.book_appointment(
        ctx,
        "2026-09-29-0800",
        "residential",
        "Maria Lopez",
        "+19145550100",
        "14 Maple Street, Brooklyn",
        "11225",
        "no heat",
        priority=True,
    )
    urgent, callback = store.tasks_for(db, "call-a")
    assert (urgent["name"], urgent["address"]) == ("Maria Lopez", "14 Maple Street, Brooklyn")
    assert urgent["phone"] == "+19145550100"
    assert (callback["name"], callback["address"]) == ("Maria", "14 Maple Street, Brooklyn")


# The reply guard (ADR-017): what the 2026-09-28 10:04 call said twice or booked too early.

DOUBLED = (
    "We don't need the ZIP for Brooklyn. You want an estimate to install a new AC, right?\n"
    "We don't need the ZIP for Brooklyn. You want an estimate to install a new AC, right?"
)


def tokens(text, size=4):
    return [text[i : i + size] for i in range(0, len(text), size)]


def text_chunks(text, size=4):
    return [llm.ChatChunk(id="r", delta=llm.ChoiceDelta(content=t)) for t in tokens(text, size)]


def tool_chunk(name, content=None):
    call = llm.FunctionToolCall(name=name, arguments="{}", call_id=f"c-{name}")
    return llm.ChatChunk(id="r", delta=llm.ChoiceDelta(content=content, tool_calls=[call]))


async def guarded(chunks, call=None, asked=False):
    async def stream():
        for chunk in chunks:
            yield chunk

    call = call or Call(call_id="call-a")
    out = [chunk async for chunk in receptionist.guard_reply(stream(), call, asked)]
    text = "".join(
        c if isinstance(c, str) else (c.delta.content or "") if c.delta else "" for c in out
    )
    tools = [
        t.name for c in out if isinstance(c, llm.ChatChunk) and c.delta for t in c.delta.tool_calls
    ]
    return text, tools, call


@pytest.mark.parametrize("size", [1, 3, 7, 200])
async def test_a_reply_that_says_the_same_thing_twice_is_said_once(size):
    text, _, call = await guarded(text_chunks(DOUBLED, size))
    assert text.strip() == DOUBLED.split("\n")[0]
    assert call.repeats_dropped == 2


async def test_the_first_sentence_streams_through_before_it_ends():
    """No latency on the first audio: every piece of the first sentence goes out as it arrives."""

    async def stream():
        for piece in ["Got", " it, 48", " Bergen"]:
            yield piece

    guard = receptionist.guard_reply(stream(), Call(call_id="call-a"))
    assert [await anext(guard) for _ in range(3)] == ["Got", " it, 48", " Bergen"]


@pytest.mark.parametrize(
    "reply",
    [
        "Oh no, in this cold? What's the address there?",
        (
            "Sam, you're booked for Monday, September 28, between noon and 4 PM at 48 Bergen "
            "Street, Brooklyn. Your reference number is one oh oh six. Is there anything else I "
            "can do?"
        ),
        "Okay. Okay. Got it.",  # a repeated one-word sentence is dropped like any other repeat
        "Thanks, Sam. Is 650-555-0142 the best number to reach you?",
    ],
)
async def test_an_ordinary_reply_passes_whole(reply):
    text, _, _ = await guarded(text_chunks(reply))
    expected = reply if reply != "Okay. Okay. Got it." else "Okay. Got it."
    assert text == expected


async def test_a_booking_made_in_the_same_reply_as_the_question_is_held():
    """10:04: "I see Tuesday afternoon 12 to 4 PM or Wednesday afternoon 12 to 4 PM open. Which do
    you want?" and book_appointment in the same reply; the caller's answer talked over "Moved"."""
    question = "I see Tuesday afternoon 12 to 4 PM or Wednesday afternoon 12 to 4 PM open. Which do you want?"
    text, tools, call = await guarded([*text_chunks(question), tool_chunk("book_appointment")])
    assert text == question
    assert tools == []
    assert call.bookings_held == 1


async def test_a_booking_after_an_earlier_step_asked_the_caller_is_held():
    _, tools, call = await guarded([tool_chunk("book_appointment")], asked=True)
    assert tools == [] and call.bookings_held == 1


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # a statement, then the booking: nothing was asked, so it books
        (
            [*text_chunks("Got it, Tuesday afternoon."), tool_chunk("book_appointment")],
            ["book_appointment"],
        ),
        # no words at all, the usual shape on the caller's answer
        ([tool_chunk("book_appointment")], ["book_appointment"]),
        # every other tool runs after a question: "What's the address?" and the urgent task
        (
            [*text_chunks("What's the address there?"), tool_chunk("create_dispatch_task")],
            ["create_dispatch_task"],
        ),
        ([*text_chunks("Which works?"), tool_chunk("check_availability")], ["check_availability"]),
    ],
)
async def test_other_tool_calls_and_answered_bookings_go_through(chunks, expected):
    _, tools, call = await guarded(chunks)
    assert tools == expected and call.bookings_held == 0


def test_a_question_counts_as_unanswered_only_until_the_caller_speaks():
    history = llm.ChatContext()
    history.add_message(role="assistant", content="Which works, Monday or Tuesday?")
    assert receptionist.asked_since_caller(history.items)
    history.add_message(role="user", content="Tuesday, please.")
    assert not receptionist.asked_since_caller(history.items)
    history.add_message(role="system", content="A note from code.")
    assert not receptionist.asked_since_caller(history.items)
    history.add_message(role="assistant", content="Got it.")
    assert not receptionist.asked_since_caller(history.items)


async def test_usage_only_chunks_and_plain_strings_pass():
    usage = llm.ChatChunk(
        id="r", usage=llm.CompletionUsage(completion_tokens=1, prompt_tokens=1, total_tokens=2)
    )
    text, _, _ = await guarded(["Got it. ", "Got it. ", "Tuesday?", usage])
    assert text == "Got it. Tuesday?"


async def test_a_guard_that_fails_lets_the_rest_of_the_reply_through(monkeypatch):
    calls = {"n": 0}
    real = receptionist.ReplyGuard.feed

    def flaky(self, text):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return real(self, text)

    monkeypatch.setattr(receptionist.ReplyGuard, "feed", flaky)
    text, tools, _ = await guarded(
        [*text_chunks("Got it. Tuesday works."), tool_chunk("book_appointment")]
    )
    assert "Tuesday works." in text
    assert tools == ["book_appointment"]


async def test_the_agent_sends_every_model_reply_through_the_guard(monkeypatch):
    call = Call(call_id="call-a")

    async def model(agent, chat_ctx, tools, model_settings):
        for chunk in [*text_chunks(DOUBLED), tool_chunk("book_appointment")]:
            yield chunk

    monkeypatch.setattr(receptionist.Agent.default, "llm_node", model)
    monkeypatch.setattr(
        SummitAirAgent, "session", property(lambda self: type("S", (), {"userdata": call})())
    )
    history = llm.ChatContext()
    history.add_message(role="user", content="Yeah. What's the ZIP code?")
    out = [c async for c in SummitAirAgent("").llm_node(history, [], None)]
    said = "".join(c.delta.content or "" for c in out if c.delta)
    assert said.strip() == DOUBLED.split("\n")[0]
    assert call.repeats_dropped == 2 and call.bookings_held == 1


# The windows arrive on the caller's yes to the address (2026-09-28 10:01)


async def test_an_urgent_task_filed_before_details_gets_the_address(db, monkeypatch):
    """The 3:51 PM call: the urgent page went out on the second turn, nothing was booked, the model
    never updated the task, and on-call had no address (ADR-021)."""
    pages = []

    async def fake_page(title, message):
        pages.append((title, message))
        return True

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db, caller_number="+16505550142"))
    await agent.create_dispatch_task(ctx, "urgent", "no heat, 80-year-old at home", "furnace out")
    await agent.check_address(ctx, "150 West 72nd Street Apartment 3", "Manhattan", "10023")
    await asyncio.sleep(0)  # let the background push run
    task = store.tasks_for(db, "call-a")[0]
    assert task["address"] == "150 West 72nd Street Apartment 3, Manhattan 10023"
    assert task["phone"] == "+16505550142"
    assert [t for t, _ in pages] == [
        "Summit Air urgent #2001",
        "Summit Air urgent #2001: address added",
    ]
    assert "10023" in pages[1][1] and "72nd" not in pages[1][1]  # ZIP only, never the street
    await agent.check_address(
        ctx, "152 West 72nd Street Apartment 3", "Manhattan", "10023"
    )  # a correction
    await asyncio.sleep(0)
    assert store.tasks_for(db, "call-a")[0]["address"].startswith("152 West 72nd")
    assert len(pages) == 2  # on-call is told once


def test_a_booking_replaces_the_address_code_copied_onto_the_task(db):
    ref = store.add_task(
        db, call_id="call-a", kind="urgent", reason="no heat", summary="", name="",
        phone="", address="150 West 72nd Street, Manhattan 10023", due_at="2026-09-28T16:06-04:00",
    )  # fmt: skip
    store.fill_task_contact(
        db, "call-a", "Sam Rivera", "+16505550142", "150 West 72nd Street, Apt 3", {},
        {"address": "150 West 72nd Street, Manhattan 10023"},
    )  # fmt: skip
    task = store.tasks_for(db, "call-a")[0]
    assert (task["name"], task["address"]) == ("Sam Rivera", "150 West 72nd Street, Apt 3")
    store.update_task_contact(db, ref, "", "", "")  # empty details never blank a field
    assert store.tasks_for(db, "call-a")[0]["name"] == "Sam Rivera"


# No heat in the cold is urgent whoever is home (after the 10:08 call)


@pytest.mark.parametrize(
    "said",
    [
        "My furnace won't kick on. It's 20 degrees out. It's just me.",  # 10:08, verbatim
        "My furnace won't kick on and it's like 20 degrees outside.",  # cold_no_risk_night
        "The heat's out and it's freezing in here.",
        "No heat, and it's so cold in the apartment.",
        "The boiler died, it's thirty two degrees out.",
        "Heat stopped working during this cold snap.",
        "My heat pump isn't heating and it's getting cold.",
    ],
)
def test_no_heat_in_the_cold_is_urgent_whoever_is_home(said):
    call = Call(call_id="call-a")
    receptionist.note_urgency(call, llm.ChatContext(), said)
    assert receptionist.urgent_reason(call) == "no heat in cold weather"


@pytest.mark.parametrize(
    "said",
    [
        "My furnace stopped working.",  # no cold said
        "My AC coil is freezing up.",  # an iced coil, not a cold home
        "The AC isn't blowing cold anymore.",
        "My water heater stopped, and it's freezing outside.",  # plumbing
        "The heat's out, but it's 65 degrees in here, it's fine.",
        "It's so cold in here, the AC is stuck on high.",  # cold, but the heat hasn't failed
        "No AC and it's 95 out.",
        # Heat that won't turn off is the opposite failure.
        "My heating won't turn off even though it's winter.",
        "My furnace won't shut off and it's freezing outside.",
        "The heat won't stop running, and it's snowing.",
    ],
)
def test_the_cold_rule_needs_both_a_failed_heat_and_the_cold(said):
    call = Call(call_id="call-a")
    receptionist.note_urgency(call, llm.ChatContext(), said)
    assert receptionist.urgent_reason(call) is None


@pytest.mark.parametrize(
    "said",
    [
        "my furnace won't turn on",
        "the heat's off and it's freezing",
        "my heat won't turn off, and now the furnace died",
        "the heat won't turn on, it won't stop clicking",
    ],
)
def test_a_failed_heat_still_counts_beside_stuck_on_words(said):
    assert rules.down_in(rules.HEAT_DOWN, said)


@pytest.mark.parametrize(
    "said",
    [
        "I'm calling for my mom, she's in Florida, the AC at my place is out",
        "the heat's been off since my dad passed and I'm selling the place",
        "No, my mom's in Florida, it's just me.",
        "it was my late father's house and the boiler is broken",
        "my dad died last year and the house is empty",
        "my mother passed away and the boiler is broken",
        "my parents are away, it's just me",
        "my mom is out of town and the AC died",
        "my mom doesn't live here anymore",
    ],
)
def test_a_relative_who_isnt_in_the_home_is_not_at_risk(said):
    assert not receptionist.at_risk_in(said, "")


@pytest.mark.parametrize(
    "said",
    [
        "No heat and my mom is 82.",
        "my mom passed out from the heat",
        "I'm calling for my mom, she's in Queens and her heat is out",
        "my mom is in Florida but my dad is here with me",
        "my mom lives with me, my dad passed last year",
        "my mom's furnace died and she's 84",
        "my mom's AC died",
    ],
)
def test_a_relative_in_the_home_is_still_at_risk(said):
    assert receptionist.at_risk_in(said, "")


async def test_the_1008_opener_files_urgent_in_code_on_the_first_turn(db, monkeypatch):
    """10:08: "It's 20 degrees out. It's just me." ran routine, and paged only when the caller
    said "as soon as possible" two turns later."""
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, pages)
    await turn(agent, "My furnace won't kick on. It's 20 degrees out. It's just me.")
    (task,) = store.tasks_for(db, "call-a")
    assert (task["kind"], task["reason"]) == ("urgent", "no heat in cold weather")
    assert line.userdata.urgent_by_code and len(pages) == 1


async def test_just_me_does_not_turn_no_heat_in_the_cold_routine(db):
    call = Call(call_id="call-a", db=db, caller_number="+19145550100", heat_down=True, cold=True)
    ctx = SpokenContext(
        call,
        [
            "My furnace won't kick on and it's like 20 degrees outside.",
            "AGENT: Is anyone there who'd be at risk in the cold, like someone older or a baby?",
            "No, it's just me.",
        ],
    )
    result = await SummitAirAgent("").create_dispatch_task(ctx, "urgent", "no heat", "no heat")
    assert result.startswith("Task 2001 created")


def test_the_prompt_makes_no_heat_in_the_cold_urgent_whoever_is_home():
    assert "whoever is home, even a healthy adult alone" in receptionist.PROMPT
    assert "No heat in the cold stays urgent" in receptionist.PROMPT


def test_the_urgent_line_the_prompt_dictates_is_one_keep_promise_recognizes():
    """If the model says the line without the task filed, keep_promise files it."""
    line = (
        "I've flagged this as urgent for our on-call technician. Our target is to call you back "
        "by 10:24 AM."
    )
    assert "flagged this as urgent for our\non-call technician" in receptionist.PROMPT
    assert receptionist.PAGE_PROMISE.search(line)


def test_the_prompt_asks_for_the_street_before_the_borough():
    assert "Ask for the town or borough before the street" in receptionist.PROMPT


async def test_failed_task_write_never_promises_a_callback(db, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(store, "add_task", fail)
    lines, kinds, hung_up = await ladder_run(db, "llm_error")
    assert lines == [receptionist.TROUBLE_NO_NUMBER]
    assert kinds == [] and hung_up == [True]


async def test_dropped_cold_weather_call_is_urgent_without_vulnerable_resident(db):
    _, kinds, _ = await ladder_run(db, "llm_error", heat_down=True, cold=True)
    assert kinds == ["urgent"]


async def test_later_contact_details_update_urgent_task_without_a_booking(db):
    agent = SummitAirAgent("")
    call = Call(call_id="call-a", db=db, caller_number="+19145550100")
    ref, due, _ = await file_task(call, "urgent", "no heat", "cold")
    await agent.create_dispatch_task(
        FakeContext(call),
        "urgent",
        "no heat",
        "cold",
        name="Maria",
        callback_number="+19145550101",
        address="14 Maple Street, Brooklyn",
    )
    tasks = store.tasks_for(db, call.call_id)
    assert len(tasks) == 1
    assert (tasks[0]["ref"], tasks[0]["due_at"]) == (ref, due.isoformat(timespec="minutes"))
    assert (tasks[0]["name"], tasks[0]["phone"], tasks[0]["address"]) == (
        "Maria",
        "+19145550101",
        "14 Maple Street, Brooklyn",
    )


async def test_booking_corrections_update_copied_task_contact(db):
    agent, ctx = await checked_call(db)
    call = ctx.userdata
    call.caller_number = "+19145550100"
    await file_task(call, "urgent", "no heat", "cold")
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    call.turn += 1
    await agent.check_address(ctx, "40 Maple Street", "Brooklyn", "11225")
    await agent.book_appointment(
        ctx,
        "2026-09-29-0800",
        "residential",
        "Maria Lopez",
        "+19145550101",
        "40 Maple Street, Brooklyn",
        "11225",
        "no heat",
    )
    task = store.tasks_for(db, call.call_id)[0]
    assert task["address"] == "40 Maple Street, Brooklyn"
    assert task["phone"] == "+19145550101"


async def test_emergency_cannot_be_booked_even_if_model_requests_it(db):
    agent, ctx = await checked_call(db)
    ctx.userdata.warned = True
    with pytest.raises(ToolError, match="emergency is active"):
        await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    assert store.booking_for(db, ctx.userdata.call_id) is None


async def test_failed_address_correction_invalidates_previous_check(db):
    agent, ctx = await checked_call(db)
    assert ctx.userdata.checked_zip == "11225"
    await agent.check_address(ctx, "14 Maple Street", "White Plains", "10601")
    with pytest.raises(ToolError, match="hasn't been checked"):
        await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)


async def test_interrupted_write_keeps_lock_until_database_finishes(db, monkeypatch):
    import asyncio
    import threading

    agent, ctx = await checked_call(db)
    entered, release = threading.Event(), threading.Event()
    real_book = store.book

    def slow_book(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return real_book(*args, **kwargs)

    monkeypatch.setattr(store, "book", slow_book)
    first = asyncio.create_task(agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        first.cancel()
        await asyncio.sleep(0)
        retry = asyncio.create_task(agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS))
        await asyncio.sleep(0.02)
        assert not first.done() and ctx.userdata.book_lock.locked()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(ToolError, match="already booked"):
        await retry
    assert store.booking_for(db, ctx.userdata.call_id)["slot_id"] == "2026-09-29-0800"


# The 2:28 PM call: an estimate that turned into a broken AC, and a "No" that didn't hold


async def test_a_system_that_fails_after_windows_were_offered_is_reoffered_as_a_repair(
    db, monkeypatch
):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db, caller_number="+19145550100"))
    agent = urgent_agent(line, monkeypatch, pages)
    call = line.userdata
    ctx = FakeContext(call)
    await agent.check_address(ctx, "14 Maple Street", "Brooklyn", "11225")
    await agent.check_availability(ctx, "2026-10-02", "afternoon")
    turn_ctx = llm.ChatContext()
    said = "Oh, hold on. My AC just completely broke, and it's really hot in here."
    await agent.on_user_turn_completed(turn_ctx, llm.ChatMessage(role="user", content=[said]))
    notes = [i.text_content for i in turn_ctx.items if i.type == "message" and i.role == "system"]
    assert any("now a repair" in n for n in notes)
    assert call.reoffer_for_repair and call.urgent_task is None
    with pytest.raises(ToolError, match="earliest open window"):
        await agent.book_appointment(ctx, "2026-10-02-1200", *BOOK_ARGS)
    await agent.check_availability(ctx, "2026-09-29", "any")
    assert not call.reoffer_for_repair
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)


async def test_a_system_down_from_the_start_needs_no_reoffer(db, monkeypatch):
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, [])
    await agent.on_user_turn_completed(
        llm.ChatContext(), llm.ChatMessage(role="user", content=["My AC broke."])
    )
    assert not line.userdata.reoffer_for_repair


async def test_a_denial_of_risk_holds_when_the_caller_later_pushes_for_urgent(db, monkeypatch):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, pages)
    for said in ["My AC broke.", "No, it's just me.", "It needs urgent fixing."]:
        await agent.on_user_turn_completed(
            llm.ChatContext(), llm.ChatMessage(role="user", content=[said])
        )
    call = line.userdata
    assert call.risk_denied
    with pytest.raises(ToolError, match="routine even if they want it fast"):
        await agent.create_dispatch_task(FakeContext(call), "urgent", "AC broke", "hot inside")
    assert pages == [] and call.urgent_task is None


async def test_naming_someone_at_risk_after_a_denial_still_pages(db, monkeypatch):
    pages = []
    line = UrgentLine(Call(call_id="call-a", db=db))
    agent = urgent_agent(line, monkeypatch, pages)
    for said in ["My AC broke.", "No, it's just me.", "Actually my grandmother is here, she's 90."]:
        await agent.on_user_turn_completed(
            llm.ChatContext(), llm.ChatMessage(role="user", content=[said])
        )
    assert not line.userdata.risk_denied and line.userdata.urgent_task == 2001


async def test_a_promise_after_a_denial_files_an_office_callback_not_a_page(db, monkeypatch):
    pages = []

    async def fake_page(title, message):
        pages.append(title)

    monkeypatch.setattr(paging, "page_on_call", fake_page)
    call = Call(call_id="call-a", db=db, risk_denied=True)
    said = "I've flagged this as urgent for our on-call technician. Our target is to call you back."
    assert await keep_promise(call, said) is True
    assert await keep_promise(call, said) is False  # once per call
    with store.connect(db) as conn:
        kinds = [r[0] for r in conn.execute("select kind from tasks")]
    assert kinds == ["callback"] and pages == [] and not call.paged


async def test_several_named_days_get_one_window_each(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    offered = await SummitAirAgent("").check_availability(
        ctx, "2026-09-30", "any", latest_date="2026-10-02"
    )
    assert [d in offered for d in ("September 30", "October 1", "October 2")] == [True] * 3
    assert offered.count("slot_id") == 3


async def test_a_day_range_is_capped_at_three_days(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    offered = await SummitAirAgent("").check_availability(
        ctx, "2026-09-29", "morning", latest_date="2026-10-09"
    )
    assert offered.count("slot_id") == 3 and "October 2" not in offered


def test_a_yes_to_the_risk_question_counts_the_system_as_down():
    """Call 7gjANeDhy3Md: "My dog's chew through the AC. And now it's broken." was too spread out
    for SYSTEM_DOWN, so code never paged and the model paged 22 s late, after "Goodbye"."""
    call = Call(call_id="call-a")
    history = llm.ChatContext()
    history.add_message(
        role="user", content="Hi. My dog's chew through the AC. And now it's broken."
    )
    history.add_message(
        role="assistant",
        content="Oh no, the AC is broken. Is anyone there who'd be at risk without cooling, like "
        "an infant or someone with a health problem?",
    )
    receptionist.note_urgency(call, history, "Yes.")
    assert receptionist.urgent_reason(call) == "no heat or cooling with someone at risk"
    fine = Call(call_id="call-b")
    receptionist.note_urgency(fine, history, "No, it's just me.")
    assert receptionist.urgent_reason(fine) is None
