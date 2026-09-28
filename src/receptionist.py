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

# Known shortcut: a keyword list over-triggers by design (a chirping smoke detector matches). The
# script below is worded to be harmless when that happens, and a clear no to it cancels the page; a
# classifier is the upgrade if false alarms cost calls. A negation just before a match, in the same
# clause, cancels that match (hazard_in).
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
# A negation ending the text before a hazard match, with at most one word between: "I don't smell
# gas", "no smoke", "I don't really smell gas". "I don't know, I smell gas" still fires: the comma
# starts a new clause.
NEGATED = re.compile(
    r"\b(?:no|not|never|don[’']?t|doesn[’']?t|didn[’']?t|isn[’']?t|wasn[’']?t)(?:\W+\w+)?\W*$",
    re.IGNORECASE,
)
CLAUSE = re.compile(r"[,.;!?]|\bbut\b", re.IGNORECASE)


def hazard_in(text: str) -> bool:
    """Whether the turn names a hazard that isn't negated in its own clause."""
    return any(
        not NEGATED.search(CLAUSE.split(text[: m.start()])[-1]) for m in HAZARD.finditer(text)
    )


# The answers to the safety script's closing question. A clear no is short and only a no; anything
# else, a hesitation included, lets the page go.
# "No heat either" names a problem; "No, just dusty" and "No I said I don't" answer the question.
CLEAR_NO = re.compile(
    r"^\W*(?:no|nope|nah|not really)(?:\W*$|\s*[,.!]|\s+(?:i|i'?m|it|it'?s|nothing|not|we|there"
    r"|that'?s|just|no|sir|ma'?am)\b)",
    re.IGNORECASE,
)
NOT_ONLY_NO = re.compile(r"\b(?:but|yes|yeah|actually)\b", re.IGNORECASE)
CONFIRM = re.compile(
    r"^\W*(?:yes|yeah|yep|yup|it is|that'?s right|correct|i do|we do|uh.?huh)\b", re.IGNORECASE
)
PAGE_HOLD_SECONDS = 15.0  # how long the emergency page waits for the answer to the script


def clear_no(text: str) -> bool:
    return (
        bool(CLEAR_NO.match(text))
        and len(text.split()) <= 8
        and not NOT_ONLY_NO.search(text)
        and not hazard_in(text)
    )


def confirms(text: str) -> bool:
    return bool(CONFIRM.match(text)) or hazard_in(text)


# Spoken by code when the caller confirms a hazard, then the call ends: the caller should be leaving,
# not talking to us.
EMERGENCY_CLOSE = (
    "Okay. Get everyone outside now and call 911 from there. Our on-call technician will call you "
    "at this number by {target}. Please hang up and go."
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
    # "I'm 82 and I live alone", "she's 84": an age said as a bare number after a person. Not a
    # temperature, a percentage or a street ("it's 85 degrees", "I'm on 72nd Street").
    r"|\b(?:i'?m|i am|she'?s|she is|he'?s|he is|they'?re|they are|is|am|are|turned|turning|age"
    r"|aged)\W+(?:6[5-9]|[7-9]\d|10\d|110)\b(?!\W*(?:degrees|percent|%|dollars|minutes|years? ago"
    r"|st\b|nd\b|rd\b|th\b))"
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
    held_page: HeldPage | None = None  # the emergency page, waiting on the answer to the script
    awaiting_answer: bool = False  # the safety script asked "Is that what's happening?"
    false_alarm: bool = False  # the caller answered the script with a clear no
    closing: bool = False  # the emergency closing line is playing; the call ends after it
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


def now() -> datetime:
    return datetime.now(TZ)


def office_open(at: datetime) -> bool:
    office = CONFIG["hours"]["office"]
    day = at.strftime("%a").lower()
    return day in office["days"] and office["start"] <= at.strftime("%H:%M") < office["end"]


def spoken_list(words: list[str], joiner: str = "and") -> str:
    """["a", "b", "c"] as "a, b and c"."""
    return ", ".join(words[:-1]) + f" {joiner} " + words[-1] if len(words) > 1 else "".join(words)


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


def speak_phone(number: str) -> str:
    """A US number as it is said: "+16505550142" becomes "650-555-0142". On the Gate 1 call the
    model read the caller ID back as "plus one six five oh...". Anything else is left as it came."""
    digits = re.sub(r"\D", "", number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or number.strip().startswith("+") and not number.strip().startswith("+1"):
        return number
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


DIGIT_WORDS = ["oh", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def speak_digits(ref: int) -> str:
    """A reference number digit by digit, the way dispatchers say it: 1003 is "one oh oh three".
    The voice reads the bare digits as "one thousand three"."""
    return " ".join(DIGIT_WORDS[int(d)] for d in str(ref))


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


# A street needs a name as well as a type. On a test call speech-to-text heard "48 Bergen Street" as
# "487 Lane", and the model read "487 Lane" back as an address.
STREET_TYPES = {
    "street", "st", "avenue", "ave", "lane", "ln", "road", "rd", "place", "pl", "boulevard",
    "blvd", "drive", "dr", "court", "ct", "way", "terrace", "parkway", "pkwy",
}  # fmt: skip
HOUSE_NUMBER = re.compile(r"^\s*\d+[a-z]?(?:-\d+)?\b", re.IGNORECASE)
UNIT = re.compile(r"\b(?:apt|apartment|unit|suite|ste|floor|fl)\b.*|#.*", re.IGNORECASE)


def street_problem(street: str) -> str | None:
    """What is missing from a street, as the next step for the model, or None when it has a house
    number and a name. "Broadway", "Avenue A" and "5th Avenue" are names; "Lane" alone is not."""
    street = UNIT.sub("", street.split(",")[0])
    number = HOUSE_NUMBER.match(street)
    if not number:
        return "Ask for the house number and street name, then check the address again."
    words = re.findall(r"[a-z0-9]+", street[number.end() :].lower())
    if not [w for w in words if w not in STREET_TYPES]:
        return "Ask for the street name, then check the address again."
    return None


def street_key(address: str) -> list[str]:
    """The house number and the first word of the street, which is what makes two addresses
    different: "14 Maple St. Apt 2" and "14 Maple Street, Brooklyn" are the same place."""
    street = address.split(",")[0].lower()
    return re.findall(r"\d+(?:-\d+)?[a-z]*|[a-z]+", street)[:2]


def same_visit(held: dict, address: str, zip_code: str) -> bool:
    """Whether a booking call with this address changes the visit the call holds, rather than
    adding a second one. The same place, however written, is the same visit. So is a correction
    of one part of it in the same ZIP: "it's forty, not fourteen" or "Bergen, not Burger" keeps
    the number or the street and changes the other. A different number on a different street is a
    second address, which is a callback, so the first visit is never moved to it by mistake."""
    if held["zip"] != zip_code:
        return False
    was, now = street_key(held["address"]), street_key(address)
    return was == now or (len(was) == len(now) == 2 and (was[0] == now[0] or was[1] == now[1]))


def in_coverage(zip_code: str) -> bool:
    return (
        len(zip_code) == 5
        and zip_code.isdigit()
        and any(zip_code.startswith(prefix) for prefix in CONFIG["coverage"]["zip_prefixes"])
    )


TOWNS = {t.lower().replace(".", "") for t in CONFIG["coverage"]["towns"]}


def town_covered(town: str) -> bool:
    """Whether the town alone places an address in the service area: a borough, the city, or a
    Queens post-office name (config coverage.towns). For a caller who doesn't know the ZIP."""
    return town.strip().lower().replace(".", "") in TOWNS


# Digits as speech-to-text writes them when the caller says them one at a time or in pairs:
# "one one two oh one", "eleven two oh one", "ten six zero one".
NUMBER_WORDS = {
    "oh": "0", "o": "0", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "20",
    "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70", "eighty": "80",
    "ninety": "90",
}  # fmt: skip


def caller_digits(items) -> str:
    """Every digit the caller has said so far, in order, with number words spelled out. Empty when
    there is no history to read (the offline tests' bare context)."""
    if items is None:
        return ""
    out = []
    for item in items:
        if getattr(item, "type", None) != "message" or item.role != "user":
            continue
        for word in re.findall(r"[a-z]+|\d+", (item.text_content or "").lower()):
            out.append(NUMBER_WORDS.get(word, word if word.isdigit() else ""))
    return "".join(out)


def history_of(context) -> list | None:
    session = getattr(context, "session", None)
    history = getattr(session, "history", None)
    return getattr(history, "items", None)


def risk_denied_last(items) -> bool:
    """Whether the caller's latest turn says nobody is at risk and nothing more: "no, it's just
    me", "nobody". "No, but my son is sick" is not a denial."""
    for item in reversed(items or []):
        if getattr(item, "type", None) == "message" and item.role == "user":
            text = item.text_content or ""
            if AT_RISK.search(text) or NOT_ONLY_NO.search(text):
                return False
            return bool(NOBODY.search(text)) or clear_no(text)
    return False


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
        address=address,
        due_at=due.isoformat(timespec="minutes"),
    )
    if kind == "callback":
        return ref, due, None
    call.paged = True
    if kind == "urgent":
        call.urgent_task, call.urgent_due = ref, due  # one urgent task per call, like emergencies
    title = f"Summit Air {kind} #{ref}"
    message = push_text(call, reason, name, address, ref)
    if hold:
        call.held_page = HeldPage(title, message, PAGE_HOLD_SECONDS)
        return ref, due, call.held_page.task
    return ref, due, start_page(title, message)


# ntfy topics are readable by anyone who knows the name, so a push carries no caller's words, number
# or street: a first name, the ZIP, what happened and where to look. The rest stays in the database.
ZIP_IN = re.compile(r"\b\d{5}\b")


def first_name(name: str) -> str:
    name = given(name or "").strip()
    return name.split()[0] if is_real_name(name) else ""


def zip_of(call: Call, address: str = "") -> str:
    found = ZIP_IN.findall(address or "")
    return found[-1] if found else call.checked_zip or ""


def push_text(call: Call, reason: str, name: str = "", address: str = "", ref=None) -> str:
    """A page or dispatch push: the reason, a first name and ZIP when known, and the lookup."""
    who = " ".join(p for p in (first_name(name), zip_of(call, address)) if p)
    lookup = f"Details: scripts/calls.py {ref or call.call_id}"
    return "\n".join([reason] + ([who] if who else []) + [lookup])


class HeldPage:
    """An emergency page that waits for the caller's answer to the safety script: it goes out on
    release() (any answer but a clear no, or the caller hanging up) or after `wait` seconds, and
    never after cancel(). The task's result is whether ntfy accepted it."""

    def __init__(self, title: str, message: str, wait: float) -> None:
        self._go = asyncio.Event()
        self.cancelled = False
        self.task = asyncio.create_task(self._send(title, message, wait))
        _background.add(self.task)
        self.task.add_done_callback(_background.discard)

    async def _send(self, title: str, message: str, wait: float) -> bool:
        try:
            await asyncio.wait_for(self._go.wait(), wait)
        except TimeoutError:
            pass
        self._go.set()
        if self.cancelled:
            return False
        return await page_on_call(title, message)

    def release(self) -> None:
        self._go.set()

    def cancel(self) -> bool:
        """Call off the page. False when it had already gone out."""
        if self._go.is_set():
            return False
        self.cancelled = True
        self._go.set()
        return True


async def release_held_page(call: Call) -> None:
    """Send a held page now and wait up to 5 s for it: the caller hung up without answering."""
    if call.held_page is None:
        return
    call.held_page.release()
    try:
        await asyncio.wait_for(asyncio.shield(call.held_page.task), timeout=5)
    except TimeoutError:
        pass


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
    return await push("NTFY_TOPIC", title, message, priority="urgent", tags="rotating_light")


async def push(
    variable: str, title: str, message: str, priority: str = "default", tags: str = ""
) -> bool:
    """Post to the ntfy topic named by the environment variable `variable`. True means ntfy
    accepted it; a failure is logged, never raised."""
    topic = os.getenv(variable)
    if not topic:
        logger.warning("%s is not set, so the push was skipped: %s", variable, title)
        return False
    try:
        # ntfy refuses a page with an error status (429 when rate-limited), not an exception.
        async with aiohttp.ClientSession(raise_for_status=True) as http:
            await http.post(
                f"{NTFY_URL}/{topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        logger.exception("push to %s failed: %s", variable, title)
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


# An explicit goodbye in the caller's last turn ends a call without the closing question: a wrong
# number shouldn't be asked whether there is anything else. "Stop." is not one.
GOODBYE_SAID = re.compile(
    r"\b(?:bye|goodbye|good-bye|wrong number|never ?mind|adi[oó]s)\b", re.IGNORECASE
)


def caller_said_goodbye(items) -> bool:
    """Whether the caller's latest turn says goodbye, wrong number or never mind."""
    for item in reversed(items):
        if getattr(item, "type", None) == "message" and item.role == "user":
            return bool(GOODBYE_SAID.search(item.text_content or ""))
    return False


class GuardedEndCall(EndCallTool):
    """end_call that refuses until the caller has answered whether there is anything else, or has
    just said goodbye. On a test call speech-to-text heard "Stop." over the greeting, and the model
    hung up on it (room dEwa8tRdFwLW)."""

    async def _end_call(self, ctx: RunContext):
        items = ctx.session.history.items
        if not (closing_confirmed(items) or caller_said_goodbye(items)):
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
        """Speak the safety script before the model replies whenever a hazard is mentioned, act on
        the answer to it, and file urgent work the model could miss."""
        call: Call = self.session.userdata
        call.turn += 1
        text = new_message.text_content or ""
        if call.awaiting_answer:
            call.awaiting_answer = False
            if clear_no(text):
                await self.call_off_emergency(call, turn_ctx)
            else:
                if call.held_page is not None:
                    call.held_page.release()
                if confirms(text):
                    await self.close_emergency(call, new_message)  # raises StopResponse
                    return
        note_urgency(call, turn_ctx, text)
        if await flag_hazard(call, text):
            await self.speak_over(SAFETY_SCRIPT, new_message)
            raise StopResponse()
        call.caller_turns += 1
        if call.caller_turns <= 2 and not call.spanish and SPANISH.search(text):
            await self.answer_in_spanish(call, new_message)  # raises StopResponse
        if not call.warned or call.false_alarm:  # never while an emergency stands
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
        call.closing = True
        handle = await self.speak_over(
            EMERGENCY_CLOSE.format(target=speak_due(due, now())), new_message
        )
        task = asyncio.create_task(hang_up_after(call, handle))
        _background.add(task)
        task.add_done_callback(_background.discard)
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
        call.false_alarm = True
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
        """File the urgent task in code the turn the caller has said both that the system is down
        and that someone at risk is home, then tell the model it is filed. The model still replies.
        The GPT-4.1 mini simulations filed it late or only promised it (keep_promise)."""
        text = new_message.text_content or ""
        if not (call.system_down and call.at_risk) or call.urgent_task is not None:
            return
        messages = [i for i in turn_ctx.items if i.type == "message"]
        said = [m.text_content for m in messages if m.role == "user" and m.text_content] + [text]
        ref, due, page = await file_task(
            call, "urgent", "no heat or cooling with someone at risk", " / ".join(said)
        )
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
            town: The town or city.
            zip_code: The five-digit ZIP code.
        """
        if problem := street_problem(street):
            raise ToolError(problem)
        zip_code = re.sub(r"\D", "", zip_code)
        if not town.strip():
            raise ToolError("Ask which town the address is in, then check the address again.")
        call = context.userdata
        if not zip_code:
            # No ZIP. A borough or a Queens town places the address on its own; anywhere else,
            # the ZIP decides, so ask for it once and treat "don't know" as outside the area.
            if not town_covered(town):
                return (
                    f"No ZIP, and {town} isn't {counties_spoken()}, so this is outside the "
                    "service area. Don't offer times or book. Ask for the ZIP once in case the "
                    "town was heard wrong; if they don't know it, offer a callback and ask if "
                    "there is anything else."
                )
            call.checked_zip = ""
            checked = (
                f"In the service area ({town}), no ZIP needed. Read it back once as {street}, "
                f"{town}, and wait for a yes. Book with the ZIP left blank."
            )
        else:
            if len(zip_code) != 5:
                raise ToolError("Ask for the five-digit ZIP code, then check the address again.")
            # A ZIP the caller never said is one the model made up (Gate 1 call riWFX67: "I
            # forgot", and the model checked and booked 11201 on its own).
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
            checked = (
                f"In the service area. Read it back once as {street}, {town}, ZIP {zip_code}, and "
                "wait for a yes."
            )
        # The next open windows come back with the address, so the turn after the caller's yes can
        # offer them without a second model round trip through check_availability (A7).
        slots = await asyncio.to_thread(store.open_slots, call.db, now().date(), "any", now())
        if not slots:
            return checked
        for slot in slots:
            call.offered[slot["id"]] = speak_window(slot)
        windows = "; ".join(f"{speak_window(s)} (slot_id {s['id']})" for s in slots)
        return (
            f"{checked} Don't offer times in the read-back. After the yes, unless the caller wants "
            f"a particular day or part of the day, offer these next open windows: {windows}."
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
        if zip_code and not in_coverage(zip_code):
            raise ToolError(
                f"ZIP {zip_code} is outside the service area. Don't book. Confirm the ZIP, "
                "and if it is outside the area, create a callback task."
            )
        if zip_code != call.checked_zip:  # a blank ZIP books only after a borough check
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
            call.booked_turn = turn
        window = speak_window(booking)
        call.moved = call.moved or booking["change"] == "moved"
        ref = f'{booking["ref"]} (say "{speak_digits(booking["ref"])}")'
        if booking["change"] == "moved":
            return (
                f"Moved from {speak_window(booking['previous'])} to {window}, reference {ref}, at "
                f"{address}. Tell the caller the new day and window, and that the reference is the "
                "same."
            )
        if booking["change"] == "updated":
            return (
                f"Updated. Reference {ref}: still {window} at {address}, now for: {issue}. Tell "
                "the caller what changed; the day, window and reference are the same."
            )
        return (
            f"Booked. Reference {ref}: {window} at {address}. Tell the caller the day, window, "
            "address and the reference as spelled."
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
        # "No, it's just me" is routine at any hour. On a cold-night simulation the model paged
        # on-call the turn after the caller said exactly that. The model may still escalate what
        # the keyword rules can't see ("it's dangerous for her"), just not straight over a no.
        if kind == "urgent" and not call.at_risk and risk_denied_last(history_of(context)):
            raise ToolError(
                "The caller just said nobody there is at risk, so this is routine: no urgent "
                "task, no on-call, no after-hours visit. Book the next open window."
            )
        if kind == "emergency" and call.hazard_task is not None and call.false_alarm:
            await reopen_emergency(call)
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


# Spanish on either of the caller's first two turns (A6). Only English is served, so code answers
# in Spanish rather than letting the model improvise a promise.
SPANISH = re.compile(
    r"\b(?:hola|habla|español|espanol|necesito|aire acondicionado|calefacci[oó]n|no funciona"
    r"|por favor)\b",
    re.IGNORECASE,
)
SPANISH_LINE = (
    "Lo siento, por ahora solo atendemos en inglés. Le llamaremos a este número {when}. Si "
    "prefiere, podemos seguir en inglés."
)
SPANISH_LINE_NO_NUMBER = (
    "Lo siento, por ahora solo atendemos en inglés. ¿Me da su número de teléfono para que le "
    "llamemos? Si prefiere, podemos seguir en inglés."
)
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def spanish_due(due: datetime, at: datetime) -> str:
    """A callback target in Spanish: "antes de las 2:35 de la tarde", "mañana antes de las 10 de
    la mañana", "el lunes antes de las 10 de la mañana"."""
    hour, minute = due.hour, due.minute
    if (hour, minute) == (12, 0):
        clock = "del mediodía"
    else:
        h = hour % 12 or 12
        part = "de la mañana" if hour < 12 else "de la tarde" if hour < 19 else "de la noche"
        clock = f"{'de la' if h == 1 else 'de las'} {h}{f':{minute:02d}' if minute else ''} {part}"
    days = (due.date() - at.date()).days
    if days == 0:
        return f"antes {clock}"
    if days == 1:
        return f"mañana antes {clock}"
    return f"el {DIAS[due.weekday()]} antes {clock}"


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


# Said by code when the model or speech-to-text fails for good during a call (A5). Without a number
# nobody can call back, so that caller is asked to call again instead.
TROUBLE = (
    "I'm sorry, I'm having trouble on my end. Someone from Summit Air will call you back at this "
    "number by {target}."
)
TROUBLE_NO_NUMBER = (
    "I'm sorry, I'm having trouble on my end. Please call Summit Air back in a few minutes."
)


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
        if self.task is not None or self._call.closing:
            return
        logger.error("unrecoverable %s; ending the call with a callback", kind)
        self.task = asyncio.create_task(self._end(spoken=kind != "tts_error"))
        _background.add(self.task)
        self.task.add_done_callback(_background.discard)

    async def _end(self, spoken: bool) -> None:
        call = self._call
        try:
            due = await file_dropped_call(call, transcript_text(self._session.history.items))
        except Exception:
            logger.exception("the dropped call's task was not recorded")
            due = office_minutes_from(now(), CONFIG["callback_target_minutes"]["callback"])
        if spoken:
            line = (
                TROUBLE.format(target=speak_due(due, now()))
                if call.caller_number
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
    urgent = call.system_down and call.at_risk and call.urgent_task is None
    _, due, _ = await file_task(
        call,
        "urgent" if urgent else "callback",
        "the call failed on our side"
        + (", no heat or cooling with someone at risk" if urgent else ""),
        transcript or "(nothing said yet)",
    )
    owed = [due, call.urgent_due] + ([call.hazard_due] if not call.false_alarm else [])
    return min(d for d in owed if d is not None)


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
    if call.paged or call.warned or not PAGE_PROMISE.search(text):
        return False
    logger.warning("the agent promised an on-call callback without filing it; filing it now")
    call.promise_kept = True
    try:
        await file_task(call, "urgent", "on-call callback the agent promised", text)
    except Exception:
        logger.exception("the promised urgent task was not recorded; paging on-call without it")
        start_page(
            "Summit Air urgent, not recorded",
            push_text(call, "on-call callback the agent promised; the caller's number is in calls"),
        )
    return True


def note_urgency(call: Call, turn_ctx: llm.ChatContext, text: str) -> None:
    """Update the two urgency flags from a caller turn. Every turn counts, the one that fired the
    safety script included, so a false alarm doesn't lose "the heat's out"."""
    messages = [i for i in turn_ctx.items if i.type == "message"]
    last_agent = next(
        (m.text_content or "" for m in reversed(messages) if m.role == "assistant"), ""
    )
    call.system_down = call.system_down or bool(SYSTEM_DOWN.search(text))
    call.at_risk = call.at_risk or at_risk_in(text, last_agent)


async def hang_up_after(call: Call, handle) -> None:
    """End the call once `handle` has played out."""
    await handle.wait_for_playout()
    if call.hang_up is not None:
        await call.hang_up()


async def reopen_emergency(call: Call) -> None:
    """A hazard after a false alarm: the task is open again and the page goes out now."""
    call.false_alarm = False
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
    if not hazard_in(text):
        return False
    if call.false_alarm:
        await reopen_emergency(call)
        call.awaiting_answer = True
        return True
    if call.warned or call.hazard_task is not None:
        return False
    call.warned = True  # set before the write, so a failed write can't replay the script
    call.awaiting_answer = True
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
