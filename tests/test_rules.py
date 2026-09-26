"""Deterministic checks: no model, no network, no credit. Run after every change."""

from datetime import date, datetime

import pytest
from livekit.agents import StopResponse, ToolError, llm

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
            "14 Maple Ave, White Plains",
            "10601",
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


async def test_an_address_outside_the_area_is_caught_before_any_window(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    result = await SummitAirAgent("").check_address(ctx, "48 Severn Lane", "Chappaqua", "10003")
    assert "outside the service area" in result and "Don't offer times" in result
    assert ctx.userdata.checked_zip is None


async def test_an_address_without_a_town_is_sent_back_for_the_town(db):
    ctx = FakeContext(Call(call_id="call-a", db=db))
    with pytest.raises(ToolError, match="which town"):
        await SummitAirAgent("").check_address(ctx, "14 Maple Ave", " ", "10601")


async def test_booking_an_address_that_was_never_checked_is_refused(db):
    ctx = FakeContext(Call(call_id="call-a", db=db, offered={"2026-09-29-0800": "Tuesday"}))
    with pytest.raises(ToolError, match="hasn't been checked"):
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


async def test_an_offered_window_books_and_returns_a_reference(db):
    agent = SummitAirAgent("")
    ctx = FakeContext(Call(call_id="call-a", db=db))
    readback = await agent.check_address(ctx, "14 Maple Ave", "White Plains", "10601")
    assert "14 Maple Ave, White Plains, ZIP 10601" in readback
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
    ],
)
def test_a_routine_callback_target_counts_office_time_only(asked, due):
    assert receptionist.office_minutes_from(asked, 120) == due


def test_a_callback_target_names_the_day_when_it_is_not_today():
    monday_11pm = eastern(9, 28, 23)
    assert receptionist.speak_due(eastern(9, 28, 23, 15), monday_11pm) == "11:15 PM"
    assert receptionist.speak_due(eastern(9, 29, 10), monday_11pm) == "10 AM tomorrow"
    assert receptionist.speak_due(eastern(10, 5, 10), eastern(10, 2, 18)) == "10 AM Monday"


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
        # Propane heats many homes in Putnam and Rockland, and its odorant smells of sulfur.
        "I smell propane in the basement",
        "I think there's a propane leak",
        "it smells like sulfur by the water heater",
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
    assert "already exists" in again
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
    assert "NTFY_TOPIC" not in sorted(receptionist.os.environ)  # keys only, never values


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
