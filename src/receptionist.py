"""The Summit Air receptionist: prompt rendering, the tools and the safety backstop.

src/agent.py wires the audio pipeline around this. Everything the caller experiences is decided
here or in prompt.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import aiohttp
import yaml
from livekit.agents import Agent, RunContext, StopResponse, ToolError, function_tool, llm
from livekit.agents.beta.tools import EndCallTool

import store

logger = logging.getLogger("summit-air")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config" / "business.yaml").read_text())
TZ = ZoneInfo(CONFIG["business"]["timezone"])
PROMPT = (Path(__file__).parent / "prompt.md").read_text()
DEFAULT_DB = Path(os.getenv("SUMMIT_AIR_DB", ROOT / "data" / "summit-air.db"))

# Fixed rather than generated: it needs no model call, so it is the fastest path to the first word,
# and it discloses automation before anything else.
GREETING = "Thanks for calling Summit Air. This is the automated assistant. How can I help?"

# Known shortcut: a keyword list over-triggers by design ("I don't smell gas" matches). The script
# below is worded to be harmless when that happens; a classifier is the upgrade if false alarms cost
# calls.
HAZARD = re.compile(
    r"smell\w*\W+(?:\w+\W+){0,3}gas\b|\bgas\W+(?:smell|odou?r|leak)|\bleak\w*\W+gas\b|rotten eggs?"
    r"|carbon monoxide|monoxide|\bco\W+(?:alarm|detector)"
    r"|\bsmoke\b|\bsmoking\b|\bon fire\b|\bflames?\b|burning smell|smell\w*\W+(?:\w+\W+){0,2}burning"
    r"|\bsparks?\b|\bsparking\b",
    re.IGNORECASE,
)
SAFETY_SCRIPT = (
    "Just to be safe: if you smell gas, see smoke, or have a carbon monoxide alarm going off right "
    "now, please leave the house with everyone, don't touch any light switches or appliances, and "
    "call 911 from outside. Is that what's happening?"
)


@dataclass
class Call:
    """Per-call state shared by the tools. The booking tool's arguments carry the rest."""

    call_id: str
    caller_number: str | None = None
    db: Path = DEFAULT_DB
    offered: dict[str, str] = field(default_factory=dict)  # slot id -> how it was spoken
    hazard_task: int | None = None
    away_count: int = 0


def now() -> datetime:
    return datetime.now(TZ)


def office_open(at: datetime) -> bool:
    office = CONFIG["hours"]["office"]
    day = at.strftime("%a").lower()
    return day in office["days"] and office["start"] <= at.strftime("%H:%M") < office["end"]


def render_instructions(at: datetime, caller_number: str | None) -> str:
    office = CONFIG["hours"]["office"]
    hours = f"Office hours are Monday to Friday, {speak_clock(office['start'])} to {speak_clock(office['end'])}."
    counties = CONFIG["coverage"]["counties"]
    return PROMPT.format(
        business_name=CONFIG["business"]["name"],
        counties=", ".join(counties[:-1]) + " and " + counties[-1],
        today=f"{at:%A}, {at:%B} {at.day}, {at.year}",
        time=speak_clock(at.strftime("%H:%M")),
        office_status=("The office is open. " if office_open(at) else "The office is closed. ")
        + hours,
        caller_number=caller_number or "unknown, so ask for a callback number",
        diagnostic_fee=CONFIG["pricing"]["diagnostic_fee"],
        after_hours_fee=CONFIG["pricing"]["after_hours_fee"],
        urgent_minutes=CONFIG["callback_target_minutes"]["urgent"],
        callback_minutes=CONFIG["callback_target_minutes"]["callback"],
    )


def speak_clock(hhmm: str) -> str:
    hour, minute = map(int, hhmm.split(":"))
    if (hour, minute) == (12, 0):
        return "noon"
    suffix = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    return f"{hour}:{minute:02d} {suffix}" if minute else f"{hour} {suffix}"


def speak_window(slot: dict) -> str:
    day = date.fromisoformat(slot["day"])
    return f"{day:%A}, {day:%B} {day.day}, between {speak_clock(slot['start'])} and {speak_clock(slot['end'])}"


# What a model writes when it never asked. A real name is anything else with a letter in it.
PLACEHOLDER_NAMES = {"", "caller", "the caller", "customer", "unknown", "n/a", "na", "none", "sir", "ma'am"}


def is_real_name(name: str) -> bool:
    cleaned = name.strip().lower()
    return cleaned not in PLACEHOLDER_NAMES and any(c.isalpha() for c in cleaned)


def in_coverage(zip_code: str) -> bool:
    return (
        len(zip_code) == 5
        and zip_code.isdigit()
        and zip_code[:3] in CONFIG["coverage"]["zip_prefixes"]
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
) -> tuple[int, datetime]:
    """Persist a dispatch task and, for emergency or urgent work, page the on-call phone."""
    minutes = CONFIG["callback_target_minutes"]["callback" if kind == "callback" else "urgent"]
    due = now() + timedelta(minutes=minutes)
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
    if kind != "callback":
        page = asyncio.create_task(
            page_on_call(
                f"Summit Air {kind} #{ref}",
                f"{reason}\n{summary}\n{phone or call.caller_number or ''} {address}",
            )
        )
        _background.add(page)
        page.add_done_callback(_background.discard)
    return ref, due


async def page_on_call(title: str, message: str) -> None:
    """Push to the on-call phone through ntfy. A failed page is logged, never raised into the call."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC is not set, so the on-call page was skipped: %s", title)
        return
    try:
        async with aiohttp.ClientSession() as http:
            await http.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": "urgent", "Tags": "rotating_light"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        logger.exception("on-call page failed: %s", title)


class SummitAirAgent(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(
            instructions=instructions,
            tools=[EndCallTool(end_instructions="Say goodbye in one short sentence.")],
        )

    async def on_enter(self) -> None:
        self.session.say(GREETING)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        """Speak the safety script before the model replies whenever a hazard is mentioned."""
        call: Call = self.session.userdata
        if not await flag_hazard(call, new_message.text_content or ""):
            return
        try:
            self.session.interrupt(force=True)  # safety outranks anything already queued
        except RuntimeError:
            pass
        self.session.say(SAFETY_SCRIPT, allow_interruptions=False)
        raise StopResponse()

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
        """
        call = context.userdata
        if not is_real_name(name):
            raise ToolError(
                "No name yet. Ask the caller for their name, then book. Don't use a placeholder."
            )
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
        context.disallow_interruptions()
        booking = await asyncio.to_thread(
            store.book,
            call.db,
            call_id=call.call_id,
            slot_id=slot_id,
            customer_type=customer_type,
            priority=int(priority),
            name=name,
            phone=callback_number,
            address=address,
            zip=zip_code,
            issue=issue,
            note=note,
        )
        if booking is None:
            raise ToolError(
                "That window just filled up. Call check_availability again and offer another."
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
        if kind == "emergency" and call.hazard_task is not None:
            return (
                f"Emergency task {call.hazard_task} already exists and the on-call team was paged."
            )
        ref, due = await file_task(call, kind, reason, summary, name, callback_number, address)
        who = (
            "The on-call technician was paged"
            if kind != "callback"
            else "Dispatch has the callback"
        )
        return (
            f"Task {ref} created. {who}. The callback target is {speak_clock(due.strftime('%H:%M'))}. "
            "Tell the caller the target, and never promise an arrival time."
        )


async def flag_hazard(call: Call, text: str) -> bool:
    """Record an emergency task the first time a caller mentions a hazard. True means speak the script."""
    if call.hazard_task is not None or not HAZARD.search(text):
        return False
    call.hazard_task, _ = await file_task(
        call, "emergency", "possible gas, carbon monoxide or smoke", text
    )
    return True
