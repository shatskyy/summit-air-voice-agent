"""The Summit Air receptionist: prompt rendering, the tools and the safety backstop.

src/agent.py wires the audio pipeline around this. Everything the caller experiences is decided
here or in prompt.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from livekit.agents import (
    Agent,
    RunContext,
    StopResponse,
    ToolError,
    UserStateChangedEvent,
    function_tool,
    llm,
)
from livekit.agents.beta.tools import EndCallTool

import store
from guards import (
    FABRICATED,
    PAGE_PROMISE,
    ReplyGuard,
    asked_since_caller,
    flag_fabricated_confirmation,
    guard_reply,
)
from paging import (
    HeldPage,
    address_escalation,
    confirmed,
    first_name,
    page_on_call,
    push,
    push_text,
    release_held_page,
    spawn,
    start_hold_after,
    start_page,
    zip_of,
)
from rules import (
    GENERIC_BUSINESS,
    SPANISH,
    SYSTEM_DOWN,
    address_in_one_turn,
    at_risk_in,
    caller_digits,
    clear_no,
    confirms,
    gas_suspected,
    given,
    hazard_in,
    history_of,
    is_real_name,
    note_urgency,
    repair_note,
    risk_denied_last,
    same_visit,
    spoken_numbers,
    street_key,
    street_problem,
    urgent_reason,
)
from speech import (
    CHECK_IN,
    EMERGENCY_CLOSE,
    GOODBYE,
    GREETING,
    SAFETY_SCRIPT,
    SILENT_GOODBYE,
    SPANISH_LINE,
    SPANISH_LINE_NO_NUMBER,
    TROUBLE,
    TROUBLE_NO_NUMBER,
    confirmation_line,
    spanish_due,
    speak_clock,
    speak_digits,
    speak_due,
    speak_phone,
    speak_window,
    spoken_list,
    tell_caller,
)

logger = logging.getLogger("summit-air")

# What the worker, the call record, the simulator and the tests use from this module, including
# the names now defined in rules, speech, paging and guards.
__all__ = [
    "CHECK_IN",
    "CONFIG",
    "DEFAULT_DB",
    "DICTATION_MAX_DELAY",
    "EMERGENCY_CLOSE",
    "FABRICATED",
    "GOODBYE",
    "GREETING",
    "MAX_DELAY",
    "MAX_TOOL_STEPS",
    "PAGE_PROMISE",
    "PROMPT",
    "ROOT",
    "SAFETY_SCRIPT",
    "SILENT_GOODBYE",
    "SPANISH_LINE_NO_NUMBER",
    "SYSTEM_DOWN",
    "TROUBLE_NO_NUMBER",
    "TZ",
    "Call",
    "FailureLadder",
    "GuardedEndCall",
    "ReplyGuard",
    "SilenceWatch",
    "SummitAirAgent",
    "asked_since_caller",
    "at_risk_in",
    "caller_digits",
    "clear_no",
    "file_task",
    "first_name",
    "flag_fabricated_confirmation",
    "flag_hazard",
    "guard_reply",
    "hazard_in",
    "in_coverage",
    "init_store",
    "is_real_name",
    "keep_promise",
    "note_urgency",
    "now",
    "office_minutes_from",
    "office_open",
    "page_on_call",
    "push",
    "push_text",
    "release_held_page",
    "render_instructions",
    "same_visit",
    "say_goodbye",
    "skip_reply_after_confirmation",
    "spanish_due",
    "speak_digits",
    "speak_due",
    "speak_phone",
    "speak_window",
    "spoken_numbers",
    "street_key",
    "town_covered",
    "transcript_text",
    "urgent_reason",
    "wants_dictation",
    "zip_of",
]

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config" / "business.yaml").read_text())
TZ = ZoneInfo(CONFIG["business"]["timezone"])
PROMPT = (Path(__file__).parent / "prompt.md").read_text()
DEFAULT_DB = Path(os.getenv("SUMMIT_AIR_DB", ROOT / "data" / "summit-air.db"))


# How long the emergency page waits for the answer to the safety script, counted from the end of
# the script, and the most it can wait in all, counted from when the hazard was heard.
PAGE_HOLD_SECONDS = 15.0
PAGE_HOLD_CAP_SECONDS = 45.0


Emergency = Literal["none", "asking", "standing", "closing", "false_alarm"]


@dataclass
class Call:
    """Per-call state shared by the tools. The booking tool's arguments carry the rest."""

    call_id: str
    caller_number: str | None = None
    db: Path = DEFAULT_DB
    offered: dict[str, str] = field(default_factory=dict)  # slot id -> how it was spoken
    checked_zip: str | None = None  # the in-area ZIP check_address passed on this call
    checked_street: list[str] = field(default_factory=list)  # street_key of that address
    hazard_task: int | None = None
    hazard_due: datetime | None = None  # the emergency task's callback target, for a repeat attempt
    paged: bool = False  # an urgent or emergency task was filed, so on-call has been paged
    urgent_task: int | None = None
    urgent_due: datetime | None = None
    system_down: bool = False  # the caller said the heat or cooling has failed (A1)
    at_risk: bool = False  # the caller said someone vulnerable is in the home (A1)
    heat_down: bool = False  # the caller said the heat has failed
    cold: bool = False  # the caller said it is cold, in the home or outside
    held_page: HeldPage | None = None  # the emergency page, waiting on the answer to the script
    # Where the call is in the emergency flow. "none" until a hazard is heard; then "asking" (the
    # safety script played and asked "Is that what's happening?"); the answer moves it to
    # "closing" (a yes: the closing line plays and the call ends), "false_alarm" (a clear no) or
    # "standing" (anything else: the emergency holds and the model carries on). A new hazard after
    # a false alarm reopens it. Anything but "none" means the script has been said, whether or not
    # its task was written.
    emergency: Emergency = "none"
    errors: list[str] = field(default_factory=list)  # unrecoverable provider errors (A5)
    # For the call record (record.py):
    started_at: datetime = field(default_factory=lambda: now())
    urgent_by_code: bool = False  # flag_urgent filed the urgent task, not the model
    promise_kept: bool = False  # keep_promise filed a task the agent had promised
    fallback_used: bool = False  # the fallback model answered at least once
    moved: bool = False  # the call's booking was moved to another window
    caller_turns: int = 0  # caller turns heard so far
    spanish: bool = False  # the fixed Spanish line has been said (A6)
    reply_latencies: list[float] = field(default_factory=list)  # seconds, end of speech to reply
    hang_up: Callable[[], Awaitable[object]] | None = None  # ends the call; None in simulations
    # One booking write per caller turn. LiveKit runs a turn's tool calls concurrently, and on one
    # simulated call the model sent two book_appointment calls at once, the new window then the old:
    # both returned "Moved" and the store ended where the caller wasn't told (change_window, 1 of 6).
    turn: int = 0  # caller turns completed so far
    booked_turn: int = -1  # the turn whose booking write went through
    book_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fabricated_confirmations: int = 0  # "you're booked for" said with nothing in the store
    repeats_dropped: int = 0  # sentences the reply guard kept from being said twice in one reply
    bookings_held: int = 0  # book_appointment calls held because the caller hadn't answered yet
    # book_appointment said the confirmation itself (ADR-019), so the model's reply to the tool
    # result is cancelled: one model round instead of two on the booking turn.
    confirmation_spoken: bool = False
    # The caller said nobody there is at risk, at any point on the call. It holds until they name
    # someone: on the 2:28 PM call a "No" was followed by "it needs urgent fixing" and the model
    # paged on-call for a healthy adult with a broken AC.
    risk_denied: bool = False
    # The system failed after windows were offered (an estimate turned into a repair on the 2:28
    # PM call and the Friday estimate was booked anyway). Booking waits for a fresh
    # check_availability so the caller hears the earliest window.
    reoffer_for_repair: bool = False
    ended_by_agent: bool = False  # the agent ended the call with end_call, on purpose
    # Paged tasks whose address reached on-call in a follow-up push. The urgent task is filed
    # before any details, and on the 3:51 PM call it stayed without an address or name because
    # nothing was booked and the model never updated it (ADR-021).
    address_pushed: set[int] = field(default_factory=set)
    checked_address: str = ""  # the last in-area address check_address passed, as written to tasks


def now() -> datetime:
    return datetime.now(TZ)


def office_open(at: datetime) -> bool:
    office = CONFIG["hours"]["office"]
    day = at.strftime("%a").lower()
    return day in office["days"] and office["start"] <= at.strftime("%H:%M") < office["end"]


def counties_spoken() -> str:
    """The service area as a caller hears it: "Manhattan, Brooklyn and Queens"."""
    return spoken_list(CONFIG["coverage"]["counties"])


def render_instructions(at: datetime, caller_number: str | None) -> str:
    office = CONFIG["hours"]["office"]
    hours = f"Office hours are Monday to Friday, {speak_clock(office['start'])} to {speak_clock(office['end'])}."
    return PROMPT.format(
        business_name=CONFIG["business"]["name"],
        counties=counties_spoken(),
        today=f"{at:%A}, {at:%B} {at.day}, {at.year}",
        time=speak_clock(at.strftime("%H:%M")),
        office_status=("The office is open. " if office_open(at) else "The office is closed. ")
        + hours,
        caller_number=speak_phone(caller_number)
        if caller_number
        else "unknown, so ask for a callback number",
        number_step=(
            "confirm the number they are calling from is the best one to reach them rather than "
            "asking them to recite it."
            if caller_number
            else "ask for the best number to reach them. Caller ID is withheld, so there is no "
            "calling number to confirm. Repeat the number back once."
        ),
        services_offered=spoken_list(CONFIG["services"]["offered"]),
        services_not_offered=spoken_list(CONFIG["services"]["not_offered"], "or"),
        diagnostic_fee=CONFIG["pricing"]["diagnostic_fee"],
        after_hours_fee=CONFIG["pricing"]["after_hours_fee"],
        urgent_minutes=CONFIG["callback_target_minutes"]["urgent"],
    )


def office_minutes_from(at: datetime, minutes: int) -> datetime:
    """When `minutes` of office time will have passed after `at`. Evenings and weekends don't count,
    so a routine callback asked for at 11 PM is due the next morning, not at 1 AM."""
    office = CONFIG["hours"]["office"]
    open_hour, open_minute = map(int, office["start"].split(":"))
    close_hour, close_minute = map(int, office["end"].split(":"))
    remaining = timedelta(minutes=minutes)
    day = at
    while True:
        opens = day.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
        closes = day.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        start = max(at, opens)
        if day.strftime("%a").lower() in office["days"] and start < closes:
            if start + remaining <= closes:
                return start + remaining
            remaining -= closes - start
        day = opens + timedelta(days=1)


def skip_reply_after_confirmation(call: Call, event) -> None:
    """After book_appointment has spoken the confirmation, cancel the model's reply to the tool
    result, which would only say it again. A refused booking (ToolError) never sets the flag, so
    the model still answers a refusal."""
    if call.confirmation_spoken:
        call.confirmation_spoken = False
        event.cancel_tool_reply()


def in_coverage(zip_code: str) -> bool:
    return (
        len(zip_code) == 5
        and zip_code.isdigit()
        and any(zip_code.startswith(prefix) for prefix in CONFIG["coverage"]["zip_prefixes"])
    )


TOWNS = {t.lower().replace(".", "") for t in CONFIG["coverage"]["towns"]}


def town_covered(town: str) -> bool:
    """Whether the town alone places an address in the service area: a borough, the city, a
    neighborhood or a Queens post-office name (config coverage.towns), with a trailing state
    dropped ("Brooklyn, NY"). For a caller who doesn't know the ZIP."""
    cleaned = town.strip().lower().replace(".", "")
    while True:
        shorter = re.sub(
            r"[,\s]+(?:ny|new york|new york city|nyc|brooklyn|manhattan|queens)$", "", cleaned
        ).strip()
        if shorter == cleaned:
            break
        cleaned = shorter
    return cleaned in TOWNS


def init_store(db: Path) -> None:
    hours = CONFIG["hours"]
    store.init(
        db,
        hours["arrival_windows"],
        hours["capacity_per_window"],
        now().date(),
        hours["days_ahead"],
    )


async def file_task(
    call: Call,
    kind: str,
    reason: str,
    summary: str,
    name: str = "",
    phone: str = "",
    address: str = "",
    hold: bool = False,
) -> tuple[int, datetime, asyncio.Task[bool] | None]:
    """Persist a dispatch task and, for emergency or urgent work, start paging the on-call phone.
    Returns the task's reference, its due time, and the page, which is still in flight. With
    `hold`, the page waits for the answer to the safety script (HeldPage).

    On-call answers around the clock, so urgent targets run on the wall clock. A routine callback is
    handled by the office, so its target counts office time only.
    """
    targets = CONFIG["callback_target_minutes"]
    at = now()
    if kind == "callback":
        due = office_minutes_from(at, targets["callback"])
    else:
        due = at + timedelta(minutes=targets["urgent"])
    ref = await asyncio.to_thread(
        store.add_task,
        call.db,
        call_id=call.call_id,
        kind=kind,
        reason=reason,
        summary=summary,
        name=name,
        phone=phone or call.caller_number or "",
        address=address or call.checked_address,
        due_at=due.isoformat(timespec="minutes"),
    )
    if address or call.checked_address:
        call.address_pushed.add(ref)  # the first page already carries the ZIP
    if kind == "callback":
        return ref, due, None
    call.paged = True
    if kind == "urgent":
        call.urgent_task, call.urgent_due = ref, due  # one urgent task per call, like emergencies
    title = f"Summit Air {kind} #{ref}"
    message = push_text(call, reason, name, address or call.checked_address, ref)
    if hold:
        call.held_page = HeldPage(title, message, PAGE_HOLD_SECONDS, PAGE_HOLD_CAP_SECONDS)
        return ref, due, call.held_page.task
    return ref, due, start_page(title, message)


class GuardedEndCall(EndCallTool):
    """end_call that refuses on the caller's first turn only. On a test call speech-to-text heard
    "Stop." over the greeting, and the model hung up on it (room dEwa8tRdFwLW). After that, when to
    end the call is the model's call (ADR-022): a word-list check of the caller's answer refused two
    clear goodbyes and made the agent ask "anything else" twice (calls Awb8xx294NFr, KSJ5YTzz9uHA)."""

    async def _end_call(self, ctx: RunContext):
        call = ctx.session.userdata
        if call.caller_turns < 2:
            raise ToolError(
                "Don't end the call yet. If you're not sure what the caller wants, ask what they "
                "need."
            )
        call.ended_by_agent = True
        return await super()._end_call(ctx)


class SummitAirAgent(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(
            instructions=instructions,
            tools=[
                GuardedEndCall(
                    extra_description=(
                        "Call it only after the caller has said they need nothing else, never in the "
                        "same turn as another tool. It says goodbye itself, so add no goodbye of "
                        "your own."
                    ),
                    end_instructions=None,  # no model reply after the tool; code says GOODBYE
                    on_tool_called=say_goodbye,
                )
            ],
        )

    async def on_enter(self) -> None:
        call: Call = self.session.userdata
        self.session.on(
            "function_tools_executed", lambda event: skip_reply_after_confirmation(call, event)
        )
        self.session.say(GREETING)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools, model_settings):
        """The model's reply through the reply guard (ADR-017). Only model replies pass here:
        the fixed lines code says (the greeting, the safety script, the Spanish line, goodbye)
        go straight to the voice."""
        call: Call = self.session.userdata
        stream = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        async for chunk in guard_reply(stream, call, asked_since_caller(chat_ctx.items)):
            yield chunk

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Speak the safety script before the model replies whenever a hazard is mentioned, act on
        the answer to it, and file urgent work the model could miss."""
        call: Call = self.session.userdata
        call.turn += 1
        text = new_message.text_content or ""
        if call.emergency == "asking":
            call.emergency = "standing"
            if clear_no(text):
                await self.call_off_emergency(call, turn_ctx)
            else:
                if call.held_page is not None:
                    call.held_page.release()
                if confirms(text):
                    await self.close_emergency(call, new_message)  # raises StopResponse
                    return
        was_down = call.system_down
        note_urgency(call, turn_ctx, text)
        items = [*turn_ctx.items, new_message]
        call.risk_denied = (call.risk_denied or risk_denied_last(items)) and not call.at_risk
        if note := repair_note(call, was_down):
            await self.add_note(turn_ctx, note)
        if await flag_hazard(call, text):
            handle = await self.speak_over(SAFETY_SCRIPT, new_message)
            spawn(start_hold_after(call.held_page, handle))
            raise StopResponse()
        call.caller_turns += 1
        if call.caller_turns <= 2 and not call.spanish and SPANISH.search(text):
            await self.answer_in_spanish(call, new_message)  # raises StopResponse
        if call.emergency in ("none", "false_alarm"):  # never while an emergency stands
            await self.flag_urgent(call, turn_ctx, new_message)

    async def speak_over(self, line: str, new_message: llm.ChatMessage):
        """Say a fixed line in place of the model's reply to this turn."""
        try:
            self.session.interrupt(force=True)  # safety outranks anything already queued
        except RuntimeError:
            pass
        handle = self.session.say(line, allow_interruptions=False)
        # StopResponse makes LiveKit drop this turn, so keep it by hand: the model needs what the
        # caller said, and the call record needs its most important sentence (call 7).
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.insert(new_message)
        await self.update_chat_ctx(chat_ctx)
        self.session.history.insert(new_message)
        return handle

    async def close_emergency(self, call: Call, new_message: llm.ChatMessage) -> None:
        """The caller confirmed the hazard: tell them to go, then end the call once it has played.
        No model reply on this turn."""
        due = call.hazard_due or now() + timedelta(
            minutes=CONFIG["callback_target_minutes"]["urgent"]
        )
        call.emergency = "closing"
        line = (
            EMERGENCY_CLOSE.format(target=speak_due(due, now()))
            if call.hazard_task is not None and call.caller_number
            else "Okay. Get everyone outside now and call 911 from there. Please hang up and go."
        )
        handle = await self.speak_over(line, new_message)
        spawn(hang_up_after(call, handle))
        raise StopResponse()

    async def answer_in_spanish(self, call: Call, new_message: llm.ChatMessage) -> None:
        """A caller who opens in Spanish hears a fixed Spanish line and gets a callback filed by
        code. The model on its own promised a Spanish speaker who doesn't exist."""
        call.spanish = True
        said = new_message.text_content or ""
        if call.caller_number:
            ref, due, _ = await file_task(call, "callback", "Spanish speaker, call back", said)
            line = SPANISH_LINE.format(when=spanish_due(due, now()))
            filed = (
                f"filed callback task {ref}, and told them in Spanish that someone will call them "
                f"back at this number {spanish_due(due, now())}. Don't create another callback task."
            )
        else:
            line = SPANISH_LINE_NO_NUMBER
            filed = (
                "asked in Spanish for their number, since caller ID is withheld. When they give it, "
                "repeat it back and create a callback task with it."
            )
        await self.speak_over(line, new_message)
        await self.add_note(
            llm.ChatContext(),
            "The caller spoke Spanish. Code said, in Spanish, that we only serve in English right "
            f"now, {filed} Never promise a Spanish speaker or say someone who speaks Spanish will "
            "call. If they carry on in English, help them normally. If they carry on in Spanish, "
            "answer in one short Spanish sentence, and when they say goodbye, call end_call.",
        )
        raise StopResponse()

    async def call_off_emergency(self, call: Call, turn_ctx: llm.ChatContext) -> None:
        """A clear no to the safety script: no page, the task closed as a false alarm, and the
        model told to carry on with the call."""
        sent = call.held_page is not None and not call.held_page.cancel()
        call.emergency = "false_alarm"
        if call.hazard_task is not None:
            await asyncio.to_thread(store.set_task_status, call.db, call.hazard_task, "false_alarm")
        paged = "on-call had already been paged" if sent else "on-call was not paged"
        await self.add_note(
            turn_ctx,
            "The caller confirmed there is no hazard: no gas smell, smoke or carbon monoxide alarm. "
            f"The emergency task is closed as a false alarm and {paged}. Carry on with the normal "
            "call and don't bring up gas, smoke or the safety instructions again.",
        )

    async def add_note(self, turn_ctx: llm.ChatContext, note: str) -> None:
        """A system note for this reply and the turns after it. turn_ctx is thrown away after this
        reply, and the simulator never sees it, so the note is kept in the agent's context too."""
        turn_ctx.add_message(role="system", content=note)
        kept = self.chat_ctx.copy()
        kept.add_message(role="system", content=note)
        await self.update_chat_ctx(kept)

    async def flag_urgent(
        self, call: Call, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """File the urgent task in code the turn the caller has said the system is down and either
        someone at risk is home or, for the heat, that it is cold (urgent_reason), then tell the
        model it is filed. The model still replies. The GPT-4.1 mini simulations filed it late or
        only promised it (keep_promise)."""
        text = new_message.text_content or ""
        reason = urgent_reason(call)
        if reason is None or call.urgent_task is not None:
            return
        messages = [i for i in turn_ctx.items if i.type == "message"]
        said = [m.text_content for m in messages if m.role == "user" and m.text_content] + [text]
        ref, due, page = await file_task(call, "urgent", reason, " / ".join(said))
        call.urgent_by_code = True
        paged = (
            "the on-call technician was paged"
            if page is not None and await confirmed(page)
            else "the page to the on-call phone could not be confirmed, so the task waits in the "
            "dispatch queue"
        )
        await self.add_note(
            turn_ctx,
            f"Urgent task {ref} is already filed from what the caller said, and {paged}. The "
            f"callback target is {speak_due(due, now())}. Tell the caller that target in this "
            "reply, and never promise an arrival time. Don't file an urgent task again.",
        )

    @function_tool
    async def check_address(
        self, context: RunContext[Call], street: str, town: str, zip_code: str
    ) -> str:
        """Check the service address the moment the caller gives it, before reading it back or
        offering any time.

        Args:
            street: House number and street, with any apartment or unit.
            town: The borough (Manhattan, Brooklyn or Queens), neighborhood or town.
            zip_code: The five-digit ZIP code.
        """
        call = context.userdata
        call.checked_zip = None
        call.checked_street = []
        if problem := street_problem(street):
            raise ToolError(problem)
        zip_code = re.sub(r"\D", "", zip_code)
        if not town.strip():
            raise ToolError("Ask which borough the address is in, then check the address again.")
        # Read back only an address that came in pieces; one said whole is repeated by the booking
        # confirmation at the end, which saves a turn (David, Sept 30).
        read_back = not address_in_one_turn(history_of(context), street, zip_code, town)
        if not zip_code:
            # No ZIP. A borough or a Queens town places the address on its own. Anywhere else, the
            # ZIP decides, so the model asks for it once and "don't know" is outside the area.
            # Whether to ask for the ZIP first is the model's call (ADR-022).
            if not town_covered(town):
                return (
                    f"No ZIP, and {town} isn't {counties_spoken()}, so this is outside the "
                    "service area. Don't offer times or book. Ask for the ZIP once in case the "
                    "town was heard wrong; if they don't know it, offer a callback and ask if "
                    "there is anything else."
                )
            call.checked_zip = ""
            call.checked_street = street_key(street)
            await address_escalation(call, f"{street}, {town}")
            checked = f"In the service area ({town}), no ZIP needed. Book with the ZIP left blank."
            if read_back:
                checked += f" Read it back once as {street}, {town}, and wait for a yes."
        else:
            if len(zip_code) != 5:
                raise ToolError("Ask for the five-digit ZIP code, then check the address again.")
            # A ZIP the caller never said is one the model made up (call riWFX67: "I forgot", and
            # the model checked and booked 11201 on its own).
            if (said := caller_digits(history_of(context))) and zip_code not in said:
                raise ToolError(
                    f"The caller never said ZIP {zip_code} on this call, so don't use it. Ask "
                    "them for their ZIP code. If they don't know it, check the address again "
                    "with the ZIP left blank."
                )
            if not in_coverage(zip_code):
                return (
                    f"ZIP {zip_code} is outside the service area. Don't offer times or book. Read "
                    "the ZIP back once to make sure you heard it right. If it is right, say Summit "
                    f"Air covers {counties_spoken()} in New York City, and ask whether the address "
                    "is in one of them. If it isn't, offer a callback and ask if there is anything "
                    "else."
                )
            call.checked_zip = zip_code
            call.checked_street = street_key(street)
            await address_escalation(call, f"{street}, {town} {zip_code}")
            checked = "In the service area."
            if read_back:
                checked += (
                    f" Read it back once as {street}, {town}, ZIP {zip_code}, and wait for a yes."
                )
        # Times come from check_availability after the caller's yes, not with this result: handed
        # over with the read-back, the model offered them in the same breath (the 10:01 call), and
        # code that released them on "a yes" took the wrong yes (3:51 PM call, ADR-022).
        if not read_back:
            return (
                f"{checked} The caller gave the whole address in one go, so don't read it back: "
                "the booking confirmation repeats it at the end. Call check_availability now and "
                "offer windows in this reply."
            )
        return (
            f"{checked} Say only the read-back this turn, no times. After they confirm it, call "
            "check_availability; if they correct it, check it again."
        )

    @function_tool
    async def check_availability(
        self,
        context: RunContext[Call],
        earliest_date: str,
        part_of_day: Literal["morning", "afternoon", "any"] = "any",
        latest_date: str = "",
    ) -> str:
        """Find open arrival windows. Call this before offering the caller any time.

        Args:
            earliest_date: The first date that works for the caller, as YYYY-MM-DD. Resolve words
                like "tomorrow" or "next Tuesday" from today's date in your instructions.
            part_of_day: morning (8 AM to noon), afternoon (noon to 4 PM), or any.
            latest_date: Only when the caller names several days ("Wednesday, Thursday or
                Friday"): the last of them, as YYYY-MM-DD. You get the first open window on each
                day, up to three days, to offer together in one reply.
        """
        try:
            earliest = date.fromisoformat(earliest_date)
            latest = date.fromisoformat(latest_date) if given(latest_date or "") else None
        except ValueError:
            raise ToolError("Dates must be in YYYY-MM-DD form.") from None
        if latest is not None and latest > earliest:
            slots = await asyncio.to_thread(
                store.first_slot_each_day,
                context.userdata.db,
                earliest,
                min(latest, earliest + timedelta(days=2)),
                part_of_day,
                now(),
            )
        else:
            slots = await asyncio.to_thread(
                store.open_slots, context.userdata.db, earliest, part_of_day, now()
            )
        if not slots:
            return (
                "No open windows from that date. Offer another day, or a callback task if nothing "
                "works."
            )
        context.userdata.reoffer_for_repair = False
        for slot in slots:
            context.userdata.offered[slot["id"]] = speak_window(slot)
        return "Open windows: " + "; ".join(f"{speak_window(s)} (slot_id {s['id']})" for s in slots)

    @function_tool
    async def book_appointment(
        self,
        context: RunContext[Call],
        slot_id: str,
        customer_type: Literal["residential", "commercial"],
        name: str,
        callback_number: str,
        address: str,
        zip_code: str,
        issue: str,
        priority: bool = False,
        note: str = "",
        business_name: str = "",
        site_contact: str = "",
    ) -> str:
        """Book a visit in a window you offered and the caller accepted, after reading back the address.
        A call gets one visit: calling this again moves or updates that same booking.

        Args:
            slot_id: The slot_id from check_availability for the window the caller accepted.
            customer_type: residential for a home, commercial for a business property.
            name: The caller's name.
            callback_number: The confirmed number to reach them.
            address: Street address with any unit, and the town.
            zip_code: The five-digit ZIP code.
            issue: The problem in the caller's words, or maintenance, or a replacement or
                install estimate.
            priority: True for an urgent call where someone vulnerable is without heat or cooling.
            note: What dispatch needs: access the caller mentioned for commercial, a membership the
                caller mentioned, or an earlier visit that was missed.
            business_name: For commercial, the business's name as the caller gave it. Required.
            site_contact: For commercial, who meets the technician on site. Required.
        """
        call = context.userdata
        if call.emergency != "false_alarm" and (
            call.emergency != "none" or call.hazard_task is not None
        ):
            raise ToolError(
                "An emergency is active. Do not book a visit; follow the safety instructions."
            )
        if not is_real_name(name):
            raise ToolError(
                "No name yet. Ask the caller for their name, then book. Don't use a placeholder."
            )
        phone = given(callback_number) or call.caller_number or ""
        if len(re.sub(r"\D", "", phone)) < 10:
            raise ToolError(
                "No callback number yet: caller ID is withheld. Ask for the best number to reach "
                "them, repeat it back, then book with it."
            )
        business = given(business_name or "").strip()  # models send null for unused arguments
        if business and GENERIC_BUSINESS.fullmatch(business):
            raise ToolError(
                f'"{business}" is a kind of business, not its name. Ask the caller what the '
                "business is called, then book."
            )
        contact = given(site_contact or "").strip()
        if customer_type == "commercial" and not (business and contact):
            raise ToolError(
                "A commercial booking needs the business's name and a site contact. Ask for "
                "whichever is missing, then book."
            )
        if business:
            note = f"Business: {business}. Site contact: {contact}. {note or ''}".strip()
        if call.reoffer_for_repair:
            raise ToolError(
                "Nothing is booked yet. The caller's system has failed since you offered those "
                "windows, so this is a repair: call check_availability from today, offer the "
                "earliest open window, and book the one they pick."
            )
        if slot_id not in call.offered:
            raise ToolError(
                "That slot was not offered on this call. Call check_availability and offer a window first."
            )
        zip_code = re.sub(r"\D", "", zip_code)
        if zip_code and not in_coverage(zip_code):
            raise ToolError(
                f"ZIP {zip_code} is outside the service area. Don't book. Confirm the ZIP, "
                "and if it is outside the area, create a callback task."
            )
        # The booked address is the checked one: the ZIP matches, and the house number and street
        # match, so a blank ZIP can't carry an unchecked town and a correction goes through the
        # read-back first.
        if zip_code != call.checked_zip or street_key(address) != call.checked_street:
            raise ToolError(
                "This address hasn't been checked on this call. Call check_address, read the "
                "address back and hear a yes, then book."
            )
        turn = call.turn  # read at entry: the next turn can arrive while the write is in flight
        async with call.book_lock:  # a turn's tool calls run concurrently; one write per turn
            held = await asyncio.to_thread(store.booking_for, call.db, call.call_id)
            if held and call.booked_turn == turn:
                raise ToolError(
                    f"This turn already booked #{held['ref']} for {speak_window(held)}, and that "
                    "is what stands. Tell the caller that; don't book again unless they ask for "
                    "another change."
                )
            if held and not same_visit(held, address, zip_code):
                raise ToolError(
                    f"This call already booked #{held['ref']} at {held['address']}. For a second "
                    "address, file a callback task."
                )
            # The confirmation stays interruptible. LiveKit drops a caller turn that completes
            # while the agent can't be interrupted, without running on_user_turn_completed, so an
            # uninterruptible confirmation would lose "hold on, I smell gas" said over it.
            write = asyncio.create_task(
                asyncio.to_thread(
                    store.book,
                    call.db,
                    call_id=call.call_id,
                    slot_id=slot_id,
                    customer_type=customer_type,
                    priority=int(priority or call.urgent_task is not None),
                    name=name,
                    phone=phone,
                    address=address,
                    zip=zip_code,
                    issue=issue,
                    note=note,
                )
            )
            cancelled = False
            while True:
                try:
                    booking = await asyncio.shield(write)
                    break
                except asyncio.CancelledError:
                    if write.cancelled():
                        raise  # process shutdown cancelled the write task itself
                    # Cancelling to_thread does not stop SQLite. Keep the lock until the actual
                    # write finishes, so an interrupted turn cannot race its retry.
                    cancelled = True
            if booking is not None:
                call.booked_turn = turn
                call.moved = call.moved or booking["change"] == "moved"
                # Contact corrections commit with the booking even if speech was interrupted.
                try:
                    await asyncio.to_thread(
                        store.fill_task_contact,
                        call.db,
                        call.call_id,
                        name,
                        phone,
                        address,
                        booking["previous"] or {"phone": call.caller_number or ""},
                        {"address": call.checked_address},
                    )
                except Exception:
                    logger.exception("the booking's contact was not copied onto the call's tasks")
            if cancelled:
                raise asyncio.CancelledError
            if booking is None:
                held = await asyncio.to_thread(store.booking_for, call.db, call.call_id)
                kept = f" Their booking for {speak_window(held)} still stands." if held else ""
                raise ToolError(
                    f"That window just filled up.{kept} Call check_availability again and offer "
                    "another."
                )
        window = speak_window(booking)
        ref = f'{booking["ref"]} (say "{speak_digits(booking["ref"])}")'
        # The caller hears the confirmation from code, built from the row just written (ADR-019):
        # the model's second round to word it took the 1:06 PM booking turn to 5.9 s.
        tell_caller(context, confirmation_line(booking, name, address))
        call.confirmation_spoken = True
        told = "The caller has been told this, with the reference; add nothing this turn."
        if booking["change"] == "moved":
            return (
                f"Moved from {speak_window(booking['previous'])} to {window}, reference {ref}, at "
                f"{address}. {told}"
            )
        if booking["change"] == "updated":
            return (
                f"Updated. Reference {ref}: still {window} at {address}, now for: {issue}. {told}"
            )
        return f"Booked. Reference {ref}: {window} at {address}. {told}"

    @function_tool
    async def create_dispatch_task(
        self,
        context: RunContext[Call],
        kind: Literal["emergency", "urgent", "callback"],
        reason: str,
        summary: str,
        name: str = "",
        callback_number: str = "",
        address: str = "",
    ) -> str:
        """Hand the call to a person. Missing details never block this.

        urgent: someone vulnerable is without heat or cooling. This pages the on-call technician, so
        call it the moment urgency is clear, before scheduling anything.
        emergency: gas, carbon monoxide, smoke or fire, after giving the safety instructions.
        callback: anything a person has to handle, such as a request for a person, a reschedule or
        cancellation, billing, a complaint, a commercial quote, or an address outside the area.

        Args:
            kind: emergency, urgent or callback.
            reason: One short line, such as "no heat, 78-year-old at home".
            summary: What the caller needs and anything already agreed.
            name: The caller's name, if known.
            callback_number: The number to call back, if confirmed.
            address: The service address, if known.
        """
        call = context.userdata
        if kind == "urgent" and call.urgent_task is not None:
            await asyncio.to_thread(
                store.update_task_contact,
                call.db,
                call.urgent_task,
                given(name),
                given(callback_number),
                given(address),
            )
            return (
                f"Urgent task {call.urgent_task} already exists and on-call has it. The callback "
                f"target is {speak_due(call.urgent_due, now())}. Tell the caller the target, and "
                "never promise an arrival time."
            )
        # "No, it's just me" is routine at any hour. On a cold-night simulation the model paged
        # on-call the turn after the caller said exactly that. The model may still escalate what
        # the keyword rules can't see ("it's dangerous for her"), just not straight over a no.
        # No heat in the cold is urgent whoever is home, so a "just me" doesn't make it routine.
        if (
            kind == "urgent"
            and not call.at_risk
            and not (call.heat_down and call.cold)
            and (call.risk_denied or risk_denied_last(history_of(context)))
        ):
            raise ToolError(
                "The caller said nobody there is at risk, so this is routine even if they want it "
                "fast: no urgent task, no on-call, no after-hours visit. Don't say on-call has it. "
                "Tell them plainly it's a priority repair and offer the earliest open window "
                "(check_availability from today)."
            )
        if kind == "emergency" and call.hazard_task is not None and call.emergency == "false_alarm":
            await reopen_emergency(call)
        if kind == "emergency" and call.hazard_task is not None:
            await asyncio.to_thread(
                store.update_task_contact,
                call.db,
                call.hazard_task,
                given(name),
                given(callback_number),
                given(address),
            )
            target = (
                f" The callback target is {speak_due(call.hazard_due, now())}."
                if call.hazard_due
                else ""
            )
            return (
                f"Emergency task {call.hazard_task} already exists and on-call has it.{target} "
                "Tell the caller the target, and never promise an arrival time."
            )
        ref, due, page = await file_task(
            call, kind, reason, summary, given(name), given(callback_number), given(address)
        )
        if kind == "emergency":
            call.hazard_task, call.hazard_due = ref, due  # one emergency task per call
        if page is None:
            who = "Dispatch has the callback"
        elif await confirmed(page):
            who = "The on-call technician was paged"
        else:
            who = (
                "The page to the on-call phone could not be confirmed, so the task waits in the "
                "dispatch queue"
            )
        return (
            f"Task {ref} created. {who}. The callback target is {speak_due(due, now())}. "
            "Tell the caller the target, and never promise an arrival time."
        )


class SilenceWatch:
    """Check in once when the caller goes quiet, then say goodbye and hang up if they stay quiet.

    LiveKit reports the caller as away only once per silence: it rearms its timer only while the
    caller is listening, and after the first report they are already away. So the hang-up is timed
    here instead of waiting for a second report that never comes.
    """

    def __init__(self, session, hang_up: Callable[[], Awaitable[object]], wait: float) -> None:
        self._session = session
        self._hang_up = hang_up
        self._wait = wait
        self._task: asyncio.Task | None = None
        self._closing = False

    def on_user_state(self, event: UserStateChangedEvent) -> None:
        if event.new_state == "away":
            self.on_away()
        else:
            # Speaking, or back to listening when a transcript arrives that voice detection missed.
            self.on_speaking()

    def on_away(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._check_in_then_hang_up())

    def on_speaking(self) -> None:
        if self._task is not None and not self._closing:
            self._task.cancel()
            self._task = None

    async def _check_in_then_hang_up(self) -> None:
        await self._session.generate_reply(instructions=CHECK_IN)  # returns after playout
        await asyncio.sleep(self._wait)
        self._closing = True  # the goodbye has started, so speech no longer calls it off
        await self._session.say(SILENT_GOODBYE, allow_interruptions=False)
        await self._hang_up()


def transcript_text(items) -> str:
    """The conversation so far as plain lines, for a task summary or the call record."""
    lines = []
    for item in items:
        is_turn = getattr(item, "type", None) == "message" and item.role in ("user", "assistant")
        if is_turn and (text := item.text_content):
            lines.append(f"{'Caller' if item.role == 'user' else 'Agent'}: {text}")
    return "\n".join(lines)


class FailureLadder:
    """The last rung when a provider fails for good mid-call. The fallback model and the backup voice
    come first; when an error still reaches the session as unrecoverable, LiveKit keeps the call open
    through three of them in a row while the caller hears silence. Here the first one ends the call
    in code, once: a task holding the transcript so far (urgent when the system is down and someone
    is at risk), a fixed line saying when we'll call back, and the hang-up once it has played. When
    the voice is what failed there is nothing to say it with, so the task is filed and the call
    ended."""

    def __init__(self, session, call: Call) -> None:
        self._session = session
        self._call = call
        self.task: asyncio.Task | None = None

    def on_error(self, event) -> None:
        error = event.error
        if getattr(error, "recoverable", False):
            return
        kind = getattr(error, "type", "error")
        self._call.errors.append(f"{kind}: {getattr(error, 'error', error)}")
        if self.task is not None or self._call.emergency == "closing":
            return
        logger.error("unrecoverable %s; ending the call with a callback", kind)
        self.task = spawn(self._end(spoken=kind != "tts_error"))

    async def _end(self, spoken: bool) -> None:
        call = self._call
        try:
            due = await file_dropped_call(call, transcript_text(self._session.history.items))
        except Exception:
            logger.exception("the dropped call's task was not recorded")
            due = None
        if spoken:
            line = (
                TROUBLE.format(target=speak_due(due, now()))
                if call.caller_number and due is not None
                else TROUBLE_NO_NUMBER
            )
            try:
                self._session.interrupt(force=True)
            except RuntimeError:
                pass
            handle = self._session.say(line, allow_interruptions=False)
            try:
                await asyncio.wait_for(handle.wait_for_playout(), 20)
            except TimeoutError:
                logger.warning("the trouble line did not finish playing; hanging up anyway")
        if call.hang_up is not None:
            await call.hang_up()


async def file_dropped_call(call: Call, transcript: str) -> datetime:
    """File the task for a call we are ending on our side, and return the soonest callback target
    the caller is owed: this task's, or an urgent or emergency one already filed."""
    urgent = urgent_reason(call) is not None and call.urgent_task is None
    _, due, _ = await file_task(
        call,
        "urgent" if urgent else "callback",
        "the call failed on our side" + (f", {urgent_reason(call)}" if urgent else ""),
        transcript or "(nothing said yet)",
    )
    owed = [due, call.urgent_due] + ([call.hazard_due] if call.emergency != "false_alarm" else [])
    return min(d for d in owed if d is not None)


async def say_goodbye(event: llm.Toolset.ToolCalledEvent) -> None:
    """Queue the fixed goodbye. The session drains queued speech before it shuts down."""
    event.ctx.session.say(GOODBYE, allow_interruptions=False)


# Turn timing. When the turn detector thinks a caller is mid-thought it waits max_delay before
# replying, and the hosted detector scores complete short answers low ("No." 0.31 against its
# 0.56 bar), so 9 of 21 turns on the 16:09 call waited the full 2 s. Most turns get a short cap;
# dictating an address or a number gets a long one, because callers pause between the parts.
MAX_DELAY = 1.1
# 2.5 s until 2026-09-28. On the 10:01 call "Forty eight Bergen Street, Brooklyn. I don't know the
# ZIP." was finished and still waited the full 2.5 s, the longest pause of three calls. 2.0 s
# still covers a pause between a street and its town; a longer one splits the address into two
# turns, which the prompt's "in pieces" rule and the street check handle.
DICTATION_MAX_DELAY = 2.0
# Tool calls run one at a time (models.py), so a turn that files a task, checks the address and
# books takes three steps where it took one. Past this many, LiveKit makes the model answer with
# its tools switched off, which is where it could say "booked" with nothing written. The default
# is 3; five covers the longest honest chain with one retry. The simulator uses the same value.
MAX_TOOL_STEPS = 5
DICTATION = re.compile(r"address|street|zip|number|reach you|phone", re.IGNORECASE)
# "Is this number the best one to reach you?" and "Did I get the address right?" name an address or
# a number but want a yes, and a short "Yeah." is exactly what the detector scores low, so a long cap
# there makes the most common confirmation slower than before. "Can I get your address?" still asks.
YES_NO = re.compile(
    r"^(?:is|are|was|were|do|does|did|has|have|will|would|should|shall)\b", re.IGNORECASE
)


def wants_dictation(said: str) -> bool:
    """Whether the agent's last question asks the caller to dictate an address or a number."""
    if "?" not in said:
        return False
    question = re.split(r"(?<=[.!?])\s+", said[: said.rindex("?") + 1])[-1]
    return bool(DICTATION.search(question)) and not YES_NO.match(question)


async def keep_promise(call: Call, text: str) -> bool:
    """File the urgent task when the agent has told the caller on-call is paged but never filed it.
    A false page is cheap; a promised callback that never comes is not. True means it filed one."""
    if call.paged or call.promise_kept or call.emergency != "none" or not PAGE_PROMISE.search(text):
        return False
    logger.warning("the agent promised an on-call callback without filing it; filing it now")
    call.promise_kept = True
    # The caller said nobody is at risk: keep the promise to call back, without paging on-call.
    routine = call.risk_denied and not call.at_risk and not (call.heat_down and call.cold)
    kind = "callback" if routine else "urgent"
    try:
        reason = "callback the agent promised" if routine else "on-call callback the agent promised"
        await file_task(call, kind, reason, text)
    except Exception:
        logger.exception("the promised urgent task was not recorded; paging on-call without it")
        start_page(
            "Summit Air urgent, not recorded",
            push_text(call, "on-call callback the agent promised; the caller's number is in calls"),
        )
    return True


async def hang_up_after(call: Call, handle) -> None:
    """End the call once `handle` has played out."""
    await handle.wait_for_playout()
    if call.hang_up is not None:
        await call.hang_up()


async def reopen_emergency(call: Call) -> None:
    """A hazard after a false alarm: the task is open again and the page goes out now."""
    call.emergency = "standing"
    if call.hazard_task is not None:
        await asyncio.to_thread(store.set_task_status, call.db, call.hazard_task, "open")
    start_page(
        f"Summit Air emergency #{call.hazard_task}",
        push_text(call, "reopened: a hazard after the caller said no", ref=call.hazard_task),
    )


async def flag_hazard(call: Call, text: str) -> bool:
    """Record an emergency task the first time a caller mentions a hazard. True means speak the script,
    even when the task could not be written: nothing may stand between the caller and the safety
    script. A failed write still pages on-call at once, because once the script has played the
    prompt tells the model the task exists, so the model won't file it either. A written task's page
    is held for the answer to the script (HeldPage)."""
    warned = call.emergency != "none"
    if not (hazard_in(text, after_script=warned) or (not warned and gas_suspected(text))):
        return False
    if call.emergency == "false_alarm":
        await reopen_emergency(call)
        call.emergency = "asking"
        return True
    if warned or call.hazard_task is not None:
        return False
    call.emergency = "asking"  # set before the write, so a failed write can't replay the script
    reason = "possible gas, carbon monoxide or smoke"
    try:
        call.hazard_task, call.hazard_due, _ = await file_task(
            call, "emergency", reason, text, hold=True
        )
    except Exception:
        logger.exception("the emergency task was not recorded; paging on-call without it")
        start_page(
            "Summit Air emergency, not recorded",
            push_text(call, f"{reason}; the caller's number is in calls"),
        )
    return True
