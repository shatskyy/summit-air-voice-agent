"""What code says, word for word: the fixed lines and the formatting of numbers, clocks, windows
and callback targets the way a dispatcher would say them."""

from __future__ import annotations

import re
from datetime import date, datetime

# Fixed rather than generated: it needs no model call, so it is the fastest path to the first word,
# and it discloses automation before anything else.
GREETING = "Thanks for calling Summit Air. This is the automated assistant. How can I help?"

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


def spoken_list(words: list[str], joiner: str = "and") -> str:
    """["a", "b", "c"] as "a, b and c"."""
    return ", ".join(words[:-1]) + f" {joiner} " + words[-1] if len(words) > 1 else "".join(words)


def speak_phone(number: str) -> str:
    """A US number as it is said: "+16505550142" becomes "650-555-0142". On an early test call the
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


def confirmation_line(booking: dict, name: str, address: str) -> str:
    """What the caller hears once a booking is written, ending in the closing question."""
    window = speak_window(booking)
    digits = speak_digits(booking["ref"])
    if booking["change"] == "moved":
        return (
            f"Done, you're now booked for {window}. Your reference number stays {digits}. "
            "Is there anything else I can help with?"
        )
    if booking["change"] == "updated":
        return (
            f"I've updated your visit on {window}. Same reference, {digits}. "
            "Is there anything else I can help with?"
        )
    first = name.split()[0].strip(",.") if name.split() else ""
    opening = f"{first}, you're" if first else "You're"
    return (
        f"{opening} booked for {window} at {address}. Your reference number is {digits}. "
        "Is there anything else I can help with?"
    )


def tell_caller(context, line: str) -> None:
    """Queue a fixed line from inside a tool. It stays interruptible: LiveKit drops a caller turn
    that completes while the agent can't be interrupted, so "hold on, I smell gas" said over it
    would be lost. A context with no session (the rule tests) has no one to tell."""
    session = getattr(context, "session", None)
    if session is not None and hasattr(session, "say"):
        session.say(line)


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


# Said by code when the model or speech-to-text fails for good during a call (A5). Without a number
# nobody can call back, so that caller is asked to call again instead.
TROUBLE = (
    "I'm sorry, I'm having trouble on my end. Someone from Summit Air will call you back at this "
    "number by {target}."
)
TROUBLE_NO_NUMBER = (
    "I'm sorry, I'm having trouble on my end. Please call Summit Air back in a few minutes."
)
