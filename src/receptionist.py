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

import aiohttp
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

logger = logging.getLogger("summit-air")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config" / "business.yaml").read_text())
TZ = ZoneInfo(CONFIG["business"]["timezone"])
PROMPT = (Path(__file__).parent / "prompt.md").read_text()
DEFAULT_DB = Path(os.getenv("SUMMIT_AIR_DB", ROOT / "data" / "summit-air.db"))
NTFY_URL = "https://ntfy.sh"

# Fixed rather than generated: it needs no model call, so it is the fastest path to the first word,
# and it discloses automation before anything else.
GREETING = "Thanks for calling Summit Air. This is the automated assistant. How can I help?"

# Known shortcut: a keyword list over-triggers by design ("I don't smell gas" matches). The script
# below is worded to be harmless when that happens; a classifier is the upgrade if false alarms cost
# calls.
# Words that can stand between the fuel and "leak": "gas is leaking", "the propane tank might be
# leaking". None names an appliance, so "my gas furnace is leaking water" stays a routine call.
LEAK_BRIDGE = r"(?:(?:is|was|tank|lines?|pipes?|might|may|could|be|seems|to|still)\W+){0,3}"
HAZARD = re.compile(
    r"smell\w*\W+(?:\w+\W+){0,3}(?:gas(?:sy)?|propane|sulfur|sulphur)\b"
    r"|\b(?:gas|propane|sulfur|sulphur)\W+(?:smell|odou?r)"
    r"|\b(?:gas|propane)\W+" + LEAK_BRIDGE + r"leak|\bleak\w*\W+(?:gas|propane)\b|rotten eggs?"
    r"|carbon monoxide|monoxide|\bco\W+(?:alarm|detector)"
    r"|\bsmoke\b|\bsmoking\b|\bon fire\b|\bflames?\b|burning smell|smell\w*\W+(?:\w+\W+){0,3}burning"
    r"|\bsparks?\b|\bsparking\b",
    re.IGNORECASE,
)
# Spoken by code when the call ends. On call 5 the model, asked to generate its own goodbye after
# end_call, repeated the opening greeting after it.
GOODBYE = "Thanks for calling Summit Air. Goodbye."

# Said when the line goes quiet: one check-in, then a goodbye if it stays quiet.
CHECK_IN = "The caller has gone quiet. Ask briefly whether they are still there."
SILENT_GOODBYE = "I'll let you go. Call us back any time."

SAFETY_SCRIPT = (
    "Just to be safe: if you smell gas, see smoke, or have a carbon monoxide alarm going off right "
    "now, please leave the house with everyone, don't touch any light switches or appliances, and "
    "call 911 from outside. Is that what's happening?"
)


# Urgent is decided in code, not left to the model: no heat or cooling, with someone at risk in the
# home. Keyword lists, like HAZARD, so they over-trigger rather than miss; a false urgent costs one
# early callback, a missed one leaves an 80-year-old in the cold. Up to three words may stand between
# the system and what went wrong ("the AC's completely out").
_BETWEEN = r"(?:\W+\w+){0,3}?\W+"
SYSTEM_DOWN = re.compile(
    r"\bno (?:heat|heating|ac|a/?c|air|air conditioning|cooling)\b"
    r"|\b(?:heat|heating|furnace|boiler|heater|heat pump|ac|a/c|air conditioner|air conditioning"
    r"|cooling)" + _BETWEEN + r"(?:out|not working|isn'?t working|stopped|won'?t|broke|broken"
    r"|died|dead|not cooling|isn'?t cooling|not heating|isn'?t heating)\b"
    r"|\bfreezing\b|\bcold in (?:here|the (?:house|apartment|home))\b|\btoo hot\b|\bsweltering\b",
    re.IGNORECASE,
)
AT_RISK = re.compile(
    r"\b(?:mother|mom|mum|father|dad|parents?|grandmother|grandma|grandfather|grandpa"
    r"|grandparents?|granny|elderly|seniors?)\b"
    r"|\b(?:6[5-9]|[7-9]\d|10\d|110)\W*years?\W*old\b"
    r"|\b(?:babies|baby|infant|newborn)\b|\b\w+\W+months?\W+old\b"
    r"|\b(?:oxygen|asthma|copd|heart condition|pregnant|dialysis|bedridden|disabled"
    r"|medical condition)\b",
    re.IGNORECASE,
)
# A denial earlier in the same clause: "no one elderly", "nobody's at risk".
RISK_DENIED = re.compile(
    r"\b(?:no|not|nobody|no one|none|isn'?t|aren'?t)\b" + r"(?:\W+\w+){0,3}\W*$", re.IGNORECASE
)
NOBODY = re.compile(r"\b(?:nobody|no one|just me|i'?m fine)\b", re.IGNORECASE)
PLAIN_YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|she is|he is|they are)\b", re.IGNORECASE)
RISK_QUESTION = re.compile(
    r"at risk|someone older|elderly|a baby|health problem|medical", re.IGNORECASE
)


def at_risk_in(text: str, last_agent: str) -> bool:
    """Whether this caller turn says someone vulnerable is in the home: a named risk not denied in
    its own clause, or a plain yes right after the agent asked who is at risk."""
    for match in AT_RISK.finditer(text):
        clause = re.split(r"[,.;!?]|\bbut\b", text[: match.start()])[-1]
        if not RISK_DENIED.search(clause):
            return True
    return bool(
        RISK_QUESTION.search(last_agent) and PLAIN_YES.match(text) and not NOBODY.search(text)
    )


@dataclass
class Call:
    """Per-call state shared by the tools. The booking tool's arguments carry the rest."""

    call_id: str
    caller_number: str | None = None
    db: Path = DEFAULT_DB
    offered: dict[str, str] = field(default_factory=dict)  # slot id -> how it was spoken
    checked_zip: str | None = None  # the in-area ZIP check_address passed on this call
    hazard_task: int | None = None
    hazard_due: datetime | None = None  # the emergency task's callback target, for a repeat attempt
    paged: bool = False  # an urgent or emergency task was filed, so on-call has been paged
    urgent_task: int | None = None
    urgent_due: datetime | None = None
    warned: bool = False  # the safety script has been given, whether or not its task was written
    system_down: bool = False  # the caller said the heat or cooling has failed (A1)
    at_risk: bool = False  # the caller said someone vulnerable is in the home (A1)


def now() -> datetime:
    return datetime.now(TZ)


def office_open(at: datetime) -> bool:
    office = CONFIG["hours"]["office"]
    day = at.strftime("%a").lower()
    return day in office["days"] and office["start"] <= at.strftime("%H:%M") < office["end"]


def counties_spoken() -> str:
    """The service area as a caller hears it: "Manhattan, Brooklyn and Queens"."""
    counties = CONFIG["coverage"]["counties"]
    return ", ".join(counties[:-1]) + " and " + counties[-1]


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
        caller_number=caller_number or "unknown, so ask for a callback number",
        number_step=(
            "confirm the number they are calling from is the best one to reach them rather than "
            "asking them to recite it."
            if caller_number
            else "ask for the best number to reach them. Caller ID is withheld, so there is no "
            "calling number to confirm. Repeat the number back once."
        ),
        diagnostic_fee=CONFIG["pricing"]["diagnostic_fee"],
        after_hours_fee=CONFIG["pricing"]["after_hours_fee"],
        urgent_minutes=CONFIG["callback_target_minutes"]["urgent"],
    )


def speak_clock(hhmm: str) -> str:
    hour, minute = map(int, hhmm.split(":"))
    if (hour, minute) == (12, 0):
        return "noon"
    suffix = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    return f"{hour}:{minute:02d} {suffix}" if minute else f"{hour} {suffix}"


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


def speak_due(due: datetime, at: datetime) -> str:
    clock = speak_clock(due.strftime("%H:%M"))
    days = (due.date() - at.date()).days
    if days == 0:
        return clock
    if days == 1:
        return f"{clock} tomorrow"
    return f"{clock} {due:%A}"


def speak_window(slot: dict) -> str:
    day = date.fromisoformat(slot["day"])
    return f"{day:%A}, {day:%B} {day.day}, between {speak_clock(slot['start'])} and {speak_clock(slot['end'])}"


# What a model writes when it never asked. A real name is anything else with a letter in it.
PLACEHOLDERS = {
    "",
    "caller",
    "the caller",
    "customer",
    "unknown",
    "n/a",
    "na",
    "none",
    "sir",
    "ma'am",
}


def is_real_name(name: str) -> bool:
    cleaned = name.strip().lower()
    return cleaned not in PLACEHOLDERS and any(c.isalpha() for c in cleaned)


# A business_name made only of these words describes the business instead of naming it: "dental
# office", "the restaurant". GPT-4.1 mini booked "dental office" in the simulated calls.
GENERIC_BUSINESS = re.compile(
    r"(?:(?:the|a|an|our|my|dental|dentist|doctor'?s?|medical|law|office|offices|clinic|store|"
    r"shop|restaurant|salon|building|company|business|practice|firm|school|church|warehouse|"
    r"gym|bakery|cafe|bar|hotel|apartment|apartments|plaza|center)\s*)+",
    re.IGNORECASE,
)


def given(detail: str) -> str:
    """The detail as the model passed it, or blank when it is a placeholder like "Unknown"."""
    return "" if detail.strip().lower() in PLACEHOLDERS else detail


def in_coverage(zip_code: str) -> bool:
    return (
        len(zip_code) == 5
        and zip_code.isdigit()
        and any(zip_code.startswith(prefix) for prefix in CONFIG["coverage"]["zip_prefixes"])
    )


def init_store(db: Path) -> None:
    hours = CONFIG["hours"]
    store.init(
        db,
        hours["arrival_windows"],
        hours["capacity_per_window"],
        now().date(),
        hours["days_ahead"],
    )


_background: set[asyncio.Task] = set()


async def file_task(
    call: Call,
    kind: str,
    reason: str,
    summary: str,
    name: str = "",
    phone: str = "",
    address: str = "",
) -> tuple[int, datetime, asyncio.Task[bool] | None]:
    """Persist a dispatch task and, for emergency or urgent work, start paging the on-call phone.
    Returns the task's reference, its due time, and the page, which is still in flight.

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
        address=address,
        due_at=due.isoformat(timespec="minutes"),
    )
    if kind == "callback":
        return ref, due, None
    call.paged = True
    if kind == "urgent":
        call.urgent_task, call.urgent_due = ref, due  # one urgent task per call, like emergencies
    page = start_page(
        f"Summit Air {kind} #{ref}",
        f"{reason}\n{summary}\n{phone or call.caller_number or ''} {address}",
    )
    return ref, due, page


def start_page(title: str, message: str) -> asyncio.Task[bool]:
    """Page the on-call phone in the background. The task's result is whether ntfy accepted it."""
    page = asyncio.create_task(page_on_call(title, message))
    _background.add(page)
    page.add_done_callback(_background.discard)
    return page


async def confirmed(page: asyncio.Task[bool]) -> bool:
    """Whether ntfy accepted the page, waiting at most 3 s. The page keeps trying after that."""
    try:
        return await asyncio.wait_for(asyncio.shield(page), timeout=3)
    except TimeoutError:
        return False


async def page_on_call(title: str, message: str) -> bool:
    """Push to the on-call phone through ntfy. True means ntfy accepted it. A failed page is logged,
    never raised into the call."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC is not set, so the on-call page was skipped: %s", title)
        return False
    try:
        # ntfy refuses a page with an error status (429 when rate-limited), not an exception.
        async with aiohttp.ClientSession(raise_for_status=True) as http:
            await http.post(
                f"{NTFY_URL}/{topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        logger.exception("on-call page failed: %s", title)
        return False
    return True


CLOSING_QUESTION = re.compile(r"anything else", re.IGNORECASE)


def closing_confirmed(items) -> bool:
    """Whether the caller has spoken since the agent last asked if there is anything else. The
    history is the record, so this needs no state of its own."""
    caller_spoke = False
    for item in reversed(items):
        if getattr(item, "type", None) != "message":
            continue
        if item.role == "user":
            caller_spoke = True
        elif item.role == "assistant" and CLOSING_QUESTION.search(item.text_content or ""):
            return caller_spoke
    return False


class GuardedEndCall(EndCallTool):
    """end_call that refuses until the caller has been asked whether there is anything else and has
    answered. On a test call speech-to-text heard "Stop." over the greeting, and the model hung up
    on it (room dEwa8tRdFwLW)."""

    async def _end_call(self, ctx: RunContext):
        if not closing_confirmed(ctx.session.history.items):
            raise ToolError(
                "Don't end the call yet. If you're not sure what the caller wants, ask what they "
                "need. Before ending, ask whether there is anything else and hear their answer."
            )
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
        self.session.say(GREETING)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Speak the safety script before the model replies whenever a hazard is mentioned."""
        call: Call = self.session.userdata
        if not await flag_hazard(call, new_message.text_content or ""):
            if not call.warned:
                await self.flag_urgent(call, turn_ctx, new_message)
            return
        try:
            self.session.interrupt(force=True)  # safety outranks anything already queued
        except RuntimeError:
            pass
        self.session.say(SAFETY_SCRIPT, allow_interruptions=False)
        # StopResponse makes LiveKit drop this turn, so keep it by hand: the model needs what the
        # caller said, and the call record needs its most important sentence (call 7).
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.insert(new_message)
        await self.update_chat_ctx(chat_ctx)
        self.session.history.insert(new_message)
        raise StopResponse()

    async def flag_urgent(
        self, call: Call, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """File the urgent task in code the turn the caller has said both that the system is down
        and that someone at risk is home, then tell the model it is filed. The model still replies.
        The GPT-4.1 mini simulations filed it late or only promised it (keep_promise)."""
        text = new_message.text_content or ""
        messages = [i for i in turn_ctx.items if i.type == "message"]
        last_agent = next(
            (m.text_content or "" for m in reversed(messages) if m.role == "assistant"), ""
        )
        call.system_down = call.system_down or bool(SYSTEM_DOWN.search(text))
        call.at_risk = call.at_risk or at_risk_in(text, last_agent)
        if not (call.system_down and call.at_risk) or call.urgent_task is not None:
            return
        said = [m.text_content for m in messages if m.role == "user" and m.text_content] + [text]
        ref, due, page = await file_task(
            call, "urgent", "no heat or cooling with someone at risk", " / ".join(said)
        )
        paged = (
            "the on-call technician was paged"
            if page is not None and await confirmed(page)
            else "the page to the on-call phone could not be confirmed, so the task waits in the "
            "dispatch queue"
        )
        note = (
            f"Urgent task {ref} is already filed from what the caller said, and {paged}. The "
            f"callback target is {speak_due(due, now())}. Tell the caller that target in this "
            "reply, and never promise an arrival time. Don't file an urgent task again."
        )
        turn_ctx.add_message(role="system", content=note)
        # turn_ctx is thrown away after this reply, so keep the note for the turns after it.
        kept = self.chat_ctx.copy()
        kept.add_message(role="system", content=note)
        await self.update_chat_ctx(kept)

    @function_tool
    async def check_address(
        self, context: RunContext[Call], street: str, town: str, zip_code: str
    ) -> str:
        """Check the service address the moment the caller gives it, before reading it back or
        offering any time.

        Args:
            street: House number and street, with any apartment or unit.
            town: The town or city.
            zip_code: The five-digit ZIP code.
        """
        zip_code = re.sub(r"\D", "", zip_code)
        if len(zip_code) != 5:
            raise ToolError("Ask for the five-digit ZIP code, then check the address again.")
        if not town.strip():
            raise ToolError("Ask which town the address is in, then check the address again.")
        if not in_coverage(zip_code):
            return (
                f"ZIP {zip_code} is outside the service area. Don't offer times or book. Read the "
                "ZIP back once to make sure you heard it right. If it is right, say Summit Air "
                f"covers {counties_spoken()} in New York City, and ask whether the address is in "
                "one of them. If it isn't, offer a callback and ask if there is anything else."
            )
        context.userdata.checked_zip = zip_code
        return (
            f"In the service area. Read it back once as {street}, {town}, ZIP {zip_code}, and wait "
            "for a yes."
        )

    @function_tool
    async def check_availability(
        self,
        context: RunContext[Call],
        earliest_date: str,
        part_of_day: Literal["morning", "afternoon", "any"] = "any",
    ) -> str:
        """Find open arrival windows. Call this before offering the caller any time.

        Args:
            earliest_date: The first date that works for the caller, as YYYY-MM-DD. Resolve words
                like "tomorrow" or "next Tuesday" from today's date in your instructions.
            part_of_day: morning (8 AM to noon), afternoon (noon to 4 PM), or any.
        """
        try:
            earliest = date.fromisoformat(earliest_date)
        except ValueError:
            raise ToolError("earliest_date must be a date in YYYY-MM-DD form.") from None
        slots = await asyncio.to_thread(
            store.open_slots, context.userdata.db, earliest, part_of_day, now()
        )
        if not slots:
            return (
                "No open windows from that date. Offer another day, or a callback task if nothing "
                "works."
            )
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

        Args:
            slot_id: The slot_id from check_availability for the window the caller accepted.
            customer_type: residential for a home, commercial for a business property.
            name: The caller's name.
            callback_number: The confirmed number to reach them.
            address: Street address with any unit, and the town.
            zip_code: The five-digit ZIP code.
            issue: The problem in the caller's words, or maintenance, or replacement estimate.
            priority: True for an urgent call where someone vulnerable is without heat or cooling.
            note: What dispatch needs: a site contact and access for commercial, a membership the
                caller mentioned, or an earlier visit that was missed.
            business_name: For commercial, the business's name as the caller gave it. Required.
            site_contact: For commercial, who meets the technician on site. Required.
        """
        call = context.userdata
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
                "whichever is missing, and any access instructions, then book."
            )
        if business:
            note = f"Business: {business}. Site contact: {contact}. {note or ''}".strip()
        if slot_id not in call.offered:
            raise ToolError(
                "That slot was not offered on this call. Call check_availability and offer a window first."
            )
        zip_code = re.sub(r"\D", "", zip_code)
        if not in_coverage(zip_code):
            raise ToolError(
                f"ZIP {zip_code or 'missing'} is outside the service area. Don't book. Confirm the ZIP, "
                "and if it is outside the area, create a callback task."
            )
        if zip_code != call.checked_zip:
            raise ToolError(
                "This address hasn't been checked on this call. Call check_address, read the "
                "address back and hear a yes, then book."
            )
        context.disallow_interruptions()
        booking = await asyncio.to_thread(
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
        if booking is None:
            held = await asyncio.to_thread(store.booking_for, call.db, call.call_id)
            kept = f" Their booking for {speak_window(held)} still stands." if held else ""
            raise ToolError(
                f"That window just filled up.{kept} Call check_availability again and offer "
                "another."
            )
        return (
            f"Booked. Reference {booking['ref']}: {speak_window(booking)} at {address}. Tell the "
            "caller the day, window, address and reference number."
        )

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
            return (
                f"Urgent task {call.urgent_task} already exists and on-call has it. The callback "
                f"target is {speak_due(call.urgent_due, now())}. Tell the caller the target, and "
                "never promise an arrival time."
            )
        if kind == "emergency" and call.hazard_task is not None:
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


async def say_goodbye(event: llm.Toolset.ToolCalledEvent) -> None:
    """Queue the fixed goodbye. The session drains queued speech before it shuts down."""
    event.ctx.session.say(GOODBYE, allow_interruptions=False)


# What the agent says when it tells a caller on-call is coming. GPT-4.1 mini said "I am paging the
# on-call technician now" without calling the tool (decision 4; 1 of 4 simulated elderly calls).
PAGE_PROMISE = re.compile(
    r"\bpag(e|ing)\b.{0,30}on.?call|on.?call (technician|tech)\b.{0,40}(call|reach|paged)"
    r"|callback within \d+ minutes|within (15|fifteen) minutes",
    re.IGNORECASE,
)


# Turn timing. When the turn detector thinks a caller is mid-thought it waits max_delay before
# replying, and the hosted detector scores complete short answers low ("No." 0.31 against its
# 0.56 bar), so 9 of 21 turns on the 16:09 call waited the full 2 s. Most turns get a short cap;
# dictating an address or a number gets a long one, because callers pause between the parts.
MAX_DELAY = 1.1
DICTATION_MAX_DELAY = 2.5
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
    if call.paged or call.warned or not PAGE_PROMISE.search(text):
        return False
    logger.warning("the agent promised an on-call callback without filing it; filing it now")
    try:
        await file_task(call, "urgent", "on-call callback the agent promised", text)
    except Exception:
        logger.exception("the promised urgent task was not recorded; paging on-call without it")
        start_page("Summit Air urgent, not recorded", f"{text}\n{call.caller_number or ''}")
    return True


async def flag_hazard(call: Call, text: str) -> bool:
    """Record an emergency task the first time a caller mentions a hazard. True means speak the script,
    even when the task could not be written: nothing may stand between the caller and the safety
    script. A failed write still pages on-call, because once the script has played the prompt tells
    the model the task exists, so the model won't file it either."""
    if call.warned or call.hazard_task is not None or not HAZARD.search(text):
        return False
    call.warned = True  # set before the write, so a failed write can't replay the script
    reason = "possible gas, carbon monoxide or smoke"
    try:
        call.hazard_task, call.hazard_due, _ = await file_task(call, "emergency", reason, text)
    except Exception:
        logger.exception("the emergency task was not recorded; paging on-call without it")
        start_page(
            "Summit Air emergency, not recorded", f"{reason}\n{text}\n{call.caller_number or ''}"
        )
    return True
