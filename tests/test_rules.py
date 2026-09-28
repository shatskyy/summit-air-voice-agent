"""Deterministic checks: no model, no network, no credit. Run after every change."""

from datetime import date, datetime

import pytest
from livekit.agents import StopResponse, ToolError, llm

import receptionist
import store
from receptionist import (
    TZ,
    Call,
    GuardedEndCall,
    SummitAirAgent,
    closing_confirmed,
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


async def test_an_address_without_a_town_is_sent_back_for_the_town(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="which town"):
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
        await agent.book_appointment(ctx, "2026-09-29-1200", *args)


async def test_an_urgent_task_states_a_target_and_pages(db, monkeypatch):
    pages = []

    async def fake_page(title, message):
        pages.append(title)
        return True

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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
        # A known false positive. The script is worded to be harmless when it fires.
        "the smoke detector battery died",
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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
    monkeypatch.setattr(store, "add_task", locked)
    call = Call(call_id="call-a", caller_number="+19145550100", db=db)

    assert await flag_hazard(call, "I smell gas in the kitchen") is True
    await receptionist.asyncio.sleep(0)

    assert len(pages) == 1
    assert "I smell gas in the kitchen" in pages[0]
    assert "+19145550100" in pages[0]


async def test_an_emergency_the_model_filed_is_the_calls_one_emergency_task(db, monkeypatch):
    """The keyword list misses some hazards ("I smell propane"), so the model files those itself. A
    later keyword match or a second model call must not file a second task and page again."""
    pages = []

    async def fake_page(title, message):
        pages.append(title)

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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
        ("Thanks, David. What's the best number to reach you?", True),
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


@pytest.mark.parametrize(
    ("history", "ends"),
    [
        ([GREETED, said("user", "Stop.")], False),  # the stray word on the dEwa8tRdFwLW call
        ([GREETED, said("user", "No heat."), said("assistant", "Is there anything else?")], False),
        (
            [
                GREETED,
                said("assistant", "Is there anything else I can help with?"),
                said("user", "No."),
            ],
            True,
        ),
    ],
)
def test_the_call_ends_only_after_anything_else_is_answered(history, ends):
    assert closing_confirmed(history) is ends


async def test_end_call_refuses_before_the_closing_question():
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        session=SimpleNamespace(history=SimpleNamespace(items=[GREETED, said("user", "Stop.")]))
    )

    agent = SummitAirAgent("")
    guard = next(t for t in agent.tools if isinstance(t, GuardedEndCall))
    with pytest.raises(ToolError, match="Don't end the call yet"):
        await guard._end_call(ctx)


# The prompt


def test_the_prompt_renders_every_placeholder():
    prompt = render_instructions(datetime(2026, 9, 29, 13, 5, tzinfo=TZ), "+19145550100")
    assert "{" not in prompt and "}" not in prompt
    assert "Tuesday, September 29, 2026" in prompt and "1:05 PM" in prompt
    assert "The office is open" in prompt and "+19145550100" in prompt


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
    monkeypatch.setattr(receptionist, "NTFY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    yield reply
    await runner.cleanup()


async def test_a_page_that_ntfy_refuses_is_logged_as_failed(ntfy, caplog):
    """ntfy answers a rate-limited or broken publish with an error status, not an exception, so a
    refused page used to pass as sent."""
    ntfy["status"] = 500
    assert await receptionist.page_on_call("Summit Air urgent #2001", "no heat") is False
    assert "on-call page failed" in caplog.text
    ntfy["status"] = 200
    assert await receptionist.page_on_call("Summit Air urgent #2002", "no heat") is True


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

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
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
    assert said in pages[0][1]
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
    moved = await agent.book_appointment(ctx, "2026-09-29-1200", *BOOK_ARGS)
    assert moved.startswith("Moved from Tuesday, September 29, between 8 AM and noon to Tuesday")
    assert "1001" in moved
    with store.connect(db) as conn:
        assert conn.execute("select count(*) from bookings").fetchone()[0] == 1


async def test_a_second_issue_at_the_same_slot_is_reported_as_updated(db):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    args = BOOK_ARGS[:-1] + ("no heat, and the AC is leaking",)
    updated = await agent.book_appointment(ctx, "2026-09-29-0800", *args)
    assert updated.startswith("Updated") and "1001" in updated


@pytest.mark.parametrize(
    ("street", "zip_code"),
    [("52 Oak Avenue, Brooklyn", "11225"), ("14 Maple Street, Queens", "11375")],
)
async def test_a_second_address_on_one_call_is_refused(db, street, zip_code):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
    ctx.userdata.checked_zip = zip_code
    with pytest.raises(ToolError, match="already booked #1001 at 14 Maple Street, Brooklyn"):
        await agent.book_appointment(
            ctx, "2026-09-29-1200", "residential", "Maria Lopez", "", street, zip_code, "no heat"
        )


async def test_the_same_street_written_differently_is_the_same_address(db):
    agent, ctx = await checked_call(db)
    await agent.book_appointment(ctx, "2026-09-29-0800", *BOOK_ARGS)
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


def test_the_booking_confirmation_uses_the_callers_first_name():
    assert (
        "[first name]" in receptionist.PROMPT.split("Only after book_appointment succeeds")[1][:80]
    )


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


def gas_call(db, monkeypatch, hold=0.2):
    pages, hung_up = [], []

    async def fake_page(title, message):
        pages.append(title)
        return True

    async def hang_up():
        hung_up.append(True)

    monkeypatch.setattr(receptionist, "page_on_call", fake_page)
    monkeypatch.setattr(receptionist, "PAGE_HOLD_SECONDS", hold)
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
    agent, _, pages, _ = gas_call(db, monkeypatch, hold=0.1)
    with pytest.raises(StopResponse):
        await turn(agent, "I smell gas.")
    await receptionist.asyncio.sleep(0.3)
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
