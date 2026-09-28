"""What code reads in the caller's words: hazards, who is at risk, whether the system is down,
and the parsing of names, streets and spoken numbers. No clock and no configuration here, so every
rule is a pure function of the text (and of the call's flags for the urgency rules)."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.agents import llm

    from receptionist import Call

# Known shortcut: a keyword list over-triggers by design. The safety script is worded to be
# harmless when that happens, and a clear no to it cancels the page; a classifier is the upgrade if
# false alarms cost calls. A negation just before a match, in the same clause, cancels that match
# (hazard_in), and so does a detector that is only asking for a battery (benign_detector).
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

# A detector asking for a battery is the most common "alarm" call and almost never a hazard. A
# smoke detector chirping or reporting a low battery is routine from the first mention. A carbon
# monoxide detector still gets the script on first mention, because a CO alarm and a CO low-battery
# chirp are easy to confuse and every guidance source says to treat doubt as an alarm; once the
# caller has heard the script, "no, it's just the battery" is taken at their word. Anything that
# sounds like a real alarm, a new battery, a symptom or another hazard keeps the match.
DETECTOR = re.compile(
    r"(?:smoke|co|carbon monoxide|monoxide)\W+(?:alarms?|detectors?)\b", re.IGNORECASE
)
LOW_BATTERY = re.compile(
    r"\blow\W+batter(?:y|ies)\b"
    r"|\bbatter(?:y|ies)(?:'s|\W+(?:is|are|was|must be|might be|seems|sounds))?\W+(?:\w+\W+)?"
    r"(?:low|dying|dead|died|going|running (?:low|out)|out)\b"
    r"|\b(?:needs?|for)\W+(?:a\W+)?(?:new\W+)?batter(?:y|ies)\b"
    r"|\b(?:just|it'?s|that'?s)\W+(?:the\W+|a\W+)?(?:\w+\W+){0,3}batter(?:y|ies)\b",
    re.IGNORECASE,
)
CHIRP = re.compile(r"\bchirp\w*", re.IGNORECASE)
ALARMING = re.compile(
    r"\bgoing off\b|\bwent off\b|\bsounding\b|\bblaring\b|\bscreaming\b|\bwon'?t stop\b|\bstill\b"
    r"|\bbatter(?:y|ies)\W+(?:is|are)\W+(?:brand\W+)?new\b|\bfresh batter"
    r"|\b(?:put|installed|changed|replaced|swapped)\W+(?:in\W+)?(?:\w+\W+){0,2}batter"
    r"|\bheadaches?\b|\bdizzy\b|\bnause|\bsick\b|\bsleepy\b|\bfaint|\bthermostat\b"
    r"|\bsmell|\bflames?\b|\bfire\b|\bsparks?\b|\bgas\b|\bpropane\b|\bsee (?:the |any )?smoke\b"
    r"|\bsmoke (?:is |coming|pouring|everywhere|in )|\bsmoky\b|\bsmoking\b|\b(?:four|4) beeps\b",
    re.IGNORECASE,
)


def benign_detector(text: str, start: int, after_script: bool) -> bool:
    """Whether the hazard match at `start` is a detector asking for a battery and nothing more."""
    detector = DETECTOR.match(text, start)
    if detector is None or ALARMING.search(text):
        return False
    smoke = detector.group().lower().startswith("smoke")
    battery = bool(LOW_BATTERY.search(text)) or (smoke and bool(CHIRP.search(text)))
    return battery and (smoke or after_script)


def hazard_in(text: str, after_script: bool = False) -> bool:
    """Whether the turn names a hazard that isn't negated in its own clause and isn't a detector
    asking for a battery. `after_script`: the caller has already heard the safety script."""
    return any(
        not NEGATED.search(CLAUSE.split(text[: m.start()])[-1])
        and not benign_detector(text, m.start(), after_script)
        for m in HAZARD.finditer(text)
    )


# Gas the caller isn't sure of: "I don't think it's gas, but there's a weird smell". It gets the
# script on first mention, like any gas smell. It is kept out of the answer checks below, so a
# hedged "no, I don't think it's gas" to the script never counts as confirming an emergency.
HEDGED_GAS = re.compile(
    r"\b(?:don'?t|do not)\W+(?:think|know|believe)\W+(?:if\W+)?(?:it'?s|it is|that'?s|there'?s)\W+"
    r"(?:\w+\W+)?(?:gas|propane)\b"
    r"|\b(?:not sure|unsure|no idea)\W+(?:if\W+|whether\W+)?(?:it'?s|it is|that'?s)\W+(?:\w+\W+)?"
    r"(?:gas|propane)\b"
    r"|\b(?:might|could|may)\W+be\W+(?:\w+\W+)?(?:gas|propane)\b|\bhope\W+it'?s\W+not\W+(?:gas|propane)\b",
    re.IGNORECASE,
)
ODOR = re.compile(r"\b(?:smell\w*|odou?r|stinks?|stench)\b", re.IGNORECASE)


def gas_suspected(text: str) -> bool:
    """A hedged gas smell, for the first mention only (see HEDGED_GAS)."""
    return bool(HEDGED_GAS.search(text) and ODOR.search(text))


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


def clear_no(text: str) -> bool:
    return (
        bool(CLEAR_NO.match(text))
        and len(text.split()) <= 8
        and not NOT_ONLY_NO.search(text)
        and not hazard_in(text, after_script=True)
    )


def confirms(text: str) -> bool:
    return bool(CONFIRM.match(text)) or hazard_in(text, after_script=True)


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
# No heat in the cold is urgent on its own, whoever is home: Rainey's brief lists "no heat in
# winter" as urgent beside "no AC with a medical condition or elderly resident" (David, 2026-09-28,
# after the 10:08 call ran routine for "20 degrees out, just me" and paged only on "as soon as
# possible"). Cooling still needs someone at risk. The heat half of SYSTEM_DOWN, with no cooling
# words and no water heater (plumbing, which Summit Air doesn't do).
HEAT_DOWN = re.compile(
    r"\bno (?:heat|heating|hot air)\b|\bno heat\b"
    r"|\b(?:heat|heating|furnace|boiler|(?<!water )heater|heat pump|radiators?)"
    + _BETWEEN
    + r"(?:out|off|not working|isn'?t working|stopped|won'?t|broke|broken|died|dead|not heating"
    r"|isn'?t heating|not coming on|gone)\b",
    re.IGNORECASE,
)
# The opposite failure: heat that won't turn off is a repair, not a failed system. Read in the
# clause the match starts, so "the heat won't turn on, it won't stop clicking" still counts.
STUCK_ON = re.compile(
    r"\b(?:won'?t|will not|can'?t|cannot|doesn'?t|does not|isn'?t|is not|not)\W+(?:\w+\W+)?"
    r"(?:turn(?:ing)?|shut(?:ting)?|switch(?:ing)?|click(?:ing)?|cut(?:ting)?|go(?:ing)?)\W+off\b"
    r"|\bwon'?t stop\b|\bstuck on\b",
    re.IGNORECASE,
)
DOWN_CLAUSE_END = re.compile(r"[,.;!?]|\b(?:and|but)\b", re.IGNORECASE)


def down_in(pattern: re.Pattern, text: str) -> bool:
    """Whether `pattern` (SYSTEM_DOWN or HEAT_DOWN) finds a failed system that isn't stuck on."""
    for match in pattern.finditer(text):
        clause = DOWN_CLAUSE_END.split(text[match.start() :], maxsplit=1)[0]
        if not STUCK_ON.search(clause):
            return True
    return False


# The caller saying it is cold, in the home or outside. "Freezing up" (an iced coil) and "blowing
# cold" (an AC) don't count, and COLD only matters beside HEAT_DOWN.
_COLD_NUMBER = (
    r"(?:\d|[1-3]\d|4[0-5]|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty"
    r"|(?:twenty|thirty)[\s-]\w+|forty(?:[\s-](?:one|two|three|four|five))?)"
)
COLD = re.compile(
    r"\b(?:it'?s|it is|getting|so|really|very|absolutely|pretty|way too|too)\W+(?:\w+\W+)?"
    r"(?:freezing|cold|frigid|icy)\b(?!\W+up\b)"
    r"|\bfreezing\W+(?:in here|inside|outside|out|cold)\b"
    r"|\bcold (?:in (?:here|the (?:house|apartment|home|place))|inside|outside|out there)\b"
    r"|\bcold snap\b|\bbelow (?:zero|freezing)\b|\b\d+\W*below\b|\bsnow(?:ing|storm)?\b|\bwinter\b"
    r"|\b" + _COLD_NUMBER + r"\W*(?:degrees|°)",
    re.IGNORECASE,
)
AT_RISK = re.compile(
    r"\b(?:mother|mom|mum|father|dad|parents?|grandmother|grandma|grandfather|grandpa"
    r"|grandparents?|granny|elderly|seniors?)\b"
    r"|\b(?:6[5-9]|[7-9]\d|10\d|110)\W*years?\W*old\b"
    # "I'm 82 and I live alone", "she's 84", "my wife is 79": an age said as a bare number right
    # after a person. Only a pronoun or a relation anchors it: "address is 72 Bergen", "it is 88
    # in here" and "the thermostat is 66" are not people (a fresh-context review caught the first
    # cut, which pages on-call for "address is 72 Bergen Street").
    r"|\b(?:i'?m|i am|she'?s|she is|he'?s|he is|(?:wife|husband|aunt|uncle|mother|mom|father|dad"
    r"|grandmother|grandfather|grandma|grandpa|sister|brother|neighbor|tenant|roommate|partner)"
    r"(?:'s|\W+(?:is|who'?s|who is|turned|just turned)))\W+(?:6[5-9]|[7-9]\d|10\d|110)\b"
    r"(?!\W*(?:degrees|percent|%|dollars|minutes|blocks|miles|years? ago|st\b|nd\b|rd\b|th\b"
    r"|[a-z]+ (?:street|st|avenue|ave|road|rd|place|lane|drive|boulevard|blvd)\b))"
    r"|\b(?:babies|baby|infant|newborn)\b|\b\w+\W+months?\W+old\b"
    r"|\b(?:oxygen|asthma|copd|heart condition|pregnant|dialysis|bedridden|disabled"
    r"|medical condition)\b",
    re.IGNORECASE,
)
# A denial earlier in the same clause: "no one elderly", "nobody's at risk".
# "No heat and my mom is 82" is not a denial: the "no" is about the heat, so a failed system named
# right after it, or an "and" or "so" between the denial and the person, ends the denial.
RISK_DENIED = re.compile(
    r"\b(?:no|not|nobody|no one|none|isn'?t|aren'?t)\b"
    r"(?!\W+(?:heat|heating|hot|ac|a/?c|air|cooling|power|working|coming)\b)"
    r"(?:\W+(?!(?:and|so|because|plus)\b)\w+){0,3}\W*$",
    re.IGNORECASE,
)
NOBODY = re.compile(r"\b(?:nobody|no one|just me|i'?m fine)\b", re.IGNORECASE)
PLAIN_YES = re.compile(r"^\W*(?:yes|yeah|yep|yup|she is|he is|they are)\b", re.IGNORECASE)
RISK_QUESTION = re.compile(
    r"at risk|someone older|elderly|a baby|health problem|medical", re.IGNORECASE
)


# A relative the caller names who isn't in the home: "since my dad passed", "my late father's
# house", "my mom's in Florida, the AC at my place is out", "my parents are away". Only a named
# relation can be away; an age, a baby or a medical condition is taken as in the home. A place
# said after the relation only counts when the caller also makes the home their own ("my place",
# "it's just me"), so "I'm calling for my mom, she's in Queens and her heat is out" still pages.
RELATIVE = re.compile(
    r"mother|mom|mum|father|dad|parents?|grandmother|grandma|grandfather|grandpa|grandparents?"
    r"|granny",
    re.IGNORECASE,
)
GONE_AFTER = re.compile(
    r"(?:'s)?(?:\W+(?:has|had|just|recently|sadly|already|who|that))*\W+"
    r"(?:passed(?:\W+(?:away|on))?\b(?!\W+out)|died\b|is gone\b|is no longer with us\b)",
    re.IGNORECASE,
)
LATE_BEFORE = re.compile(r"\blate\W+$", re.IGNORECASE)
AWAY_AFTER = re.compile(
    r"(?:'s|'re|\W+(?:is|are))\W+(?:away|out of (?:town|state|the country)|on vacation"
    r"|travell?ing|not (?:home|here|there))\b",
    re.IGNORECASE,
)
AWAY = re.compile(
    r"\b(?:she|he|they)(?:'s|'re|\W+(?:is|are))\W+(?:away|out of (?:town|state|the country)"
    r"|on vacation|travell?ing|not (?:home|here|there))\b"
    r"|\b(?:doesn'?t|don'?t|does not|do not|no longer)\W+live\W+(?:here|there|with (?:me|us))\b",
    re.IGNORECASE,
)
ELSEWHERE = re.compile(
    r"^(?:'s|'re|\W+(?:is|are|lives?|stays?))\W+(?:down|out|over|up)?\W*in\W+(?-i:[A-Z])"
    r"|\b(?:she|he|they)(?:'s|'re|\W+(?:is|are|lives?|stays?))\W+(?:down|out|over|up)?\W*in\W+"
    r"(?-i:[A-Z])",
    re.IGNORECASE,
)
MY_HOME = re.compile(r"\bmy (?:place|house|home|apartment|condo|unit)\b", re.IGNORECASE)
THEIR_HOME = re.compile(
    r"\b(?:her|his|their) (?:place|house|home|apartment|unit|heat|heating|furnace|boiler|ac|air)\b"
    r"|\bwith (?:me|us)\b|\blives? (?:here|with)\b",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"[.;!?]|\bbut\b", re.IGNORECASE)


def not_home(text: str, match: re.Match) -> bool:
    """Whether the relative named at `match` is someone who isn't in the home."""
    if not RELATIVE.fullmatch(match.group()):
        return False
    if GONE_AFTER.match(text, match.end()) or AWAY_AFTER.match(text, match.end()):
        return True
    if LATE_BEFORE.search(text[: match.start()]):
        return True
    rest = SENTENCE_END.split(text[match.end() :])[0]
    if AWAY.search(rest):
        return True
    home_is_callers = MY_HOME.search(text) or NOBODY.search(text)
    return bool(ELSEWHERE.search(rest) and home_is_callers and not THEIR_HOME.search(text))


def at_risk_in(text: str, last_agent: str) -> bool:
    """Whether this caller turn says someone vulnerable is in the home: a named risk not denied in
    its own clause and not somewhere else, or a plain yes right after the agent asked who is at
    risk."""
    for match in AT_RISK.finditer(text):
        clause = re.split(r"[,.;!?]|\bbut\b", text[: match.start()])[-1]
        if not RISK_DENIED.search(clause) and not not_home(text, match):
            return True
    return bool(
        RISK_QUESTION.search(last_agent) and PLAIN_YES.match(text) and not NOBODY.search(text)
    )


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


# "Your sister", "the tenant", "my mom": who the visit is for, not a name. GPT-4.1 mini booked a
# sister's apartment under "Your sister" without asking anyone's name (relative_address).
RELATION = re.compile(
    r"^(?:your|my|his|her|their|the|a|an)\b|\b(?:sister|brother|mother|father|mom|dad|wife"
    r"|husband|son|daughter|aunt|uncle|grandmother|grandfather|tenant|landlord|owner|neighbor"
    r"|roommate|caller|customer|resident)$",
    re.IGNORECASE,
)


def is_real_name(name: str) -> bool:
    cleaned = name.strip().lower()
    return (
        cleaned not in PLACEHOLDERS
        and any(c.isalpha() for c in cleaned)
        and not RELATION.search(cleaned)
    )


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
    """The house number and the street's name words, without the unit or the street type, which
    is what makes two addresses different: "14 Maple St. Apt 2" and "14 Maple Street, Brooklyn"
    are the same place, "150 West 72nd" and "150 West 73rd" are not."""
    parts = [UNIT.sub("", part).strip() for part in address.lower().split(",")]
    street = next((p for p in parts if HOUSE_NUMBER.match(p)), parts[0] if parts else "")
    words = re.findall(r"\d+(?:-\d+)?[a-z]*|[a-z]+", street)
    return words[:1] + [w for w in words[1:] if w not in STREET_TYPES][:2]


def same_visit(held: dict, address: str, zip_code: str) -> bool:
    """Whether a booking call with this address changes the visit the call holds, rather than
    adding a second one. The same place, however written, is the same visit. So is a correction
    of one part of it in the same ZIP: "it's forty, not fourteen" or "Bergen, not Burger" keeps
    the number or the street and changes the other. A different number on a different street is a
    second address, which is a callback, so the first visit is never moved to it by mistake."""
    if held["zip"] != zip_code:
        return False
    was, now = street_key(held["address"]), street_key(address)
    if was == now:
        return True
    if len(was) < 2 or len(now) < 2:
        return False
    if was[1:] == now[1:]:  # the same street, a corrected house number
        return True
    # The same house number on a street that sounds alike: "Burger" for "Bergen". "48 Dean" for
    # "48 Bergen" is a different property.
    return (
        was[0] == now[0]
        and len(was) == len(now) == 2
        and SequenceMatcher(None, was[1], now[1]).ratio() >= 0.6
    )


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


TENS = {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"}
UNITS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"}


def spoken_numbers(text: str) -> list[str]:
    """The numbers in a caller's words, as digit strings: "48 Bergen, one one two oh one" gives
    ["48", "1", "1", "2", "0", "1"]. "twenty one" is 21 and "two hundred one" is 201, since a ZIP
    is said in pairs and hundreds as often as digit by digit ("eleven two twenty-one")."""
    words = re.findall(r"[a-z]+|\d+", text.lower())
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w.isdigit():
            out.append(w)
        elif w in ("double", "triple") and i + 1 < len(words) and words[i + 1] in NUMBER_WORDS:
            out += [NUMBER_WORDS[words[i + 1]]] * (2 if w == "double" else 3)
            i += 1
        elif w in TENS and i + 1 < len(words) and words[i + 1] in UNITS:
            out.append(str(int(NUMBER_WORDS[w]) + int(NUMBER_WORDS[words[i + 1]])))
            i += 1
        elif w == "hundred" and out and i + 1 < len(words) and words[i + 1] in NUMBER_WORDS:
            n = int(out.pop()) * 100 + int(NUMBER_WORDS[words[i + 1]])
            out.append(str(n))
            i += 1
        elif w == "hundred" and out:
            out.append(str(int(out.pop()) * 100))
        elif w in NUMBER_WORDS:
            out.append(NUMBER_WORDS[w])
        i += 1
    return out


def caller_digits(items) -> str:
    """Every digit the caller has said so far, in order, with number words spelled out. Empty when
    there is no history to read (the offline tests' bare context). A ZIP is checked as a run of
    this string, so a ZIP hidden inside a phone number passes; that is the old behavior, not a
    new hole."""
    if items is None:
        return ""
    out = []
    for item in items:
        if getattr(item, "type", None) != "message" or item.role != "user":
            continue
        out += spoken_numbers(item.text_content or "")
    return "".join(out)


def history_of(context) -> list | None:
    session = getattr(context, "session", None)
    history = getattr(session, "history", None)
    return getattr(history, "items", None)


# What a plain denial of risk is made of, and nothing else: "No, it's just me, I'm fine." A turn
# with any other word in it ("No, she just had a stroke") is the model's to judge.
DENIAL_WORDS = {
    "no", "nope", "nah", "nobody", "none", "one", "not", "really", "just", "me", "us", "myself",
    "it", "its", "is", "im", "i", "am", "a", "an", "fine", "healthy", "adult", "adults", "here",
    "home", "at", "risk", "there", "theres", "thats", "that", "all", "only", "ok", "okay", "thanks",
    "thank", "you", "nothing", "like", "of", "the", "sort", "kind", "we", "were", "are", "both",
    "good", "young", "and", "my", "wife", "husband", "dog", "cat",
}  # fmt: skip


def risk_denied_last(items) -> bool:
    """Whether the caller's latest turn is a plain denial of risk and nothing more: "no, it's just
    me", "nobody, I'm fine". "No, she just had a stroke" or "no, but my son is sick" is not, and a
    bare "No." counts only as the answer to the agent's at-risk question."""
    messages = [i for i in items or [] if getattr(i, "type", None) == "message"]
    last_agent = next(
        (m.text_content or "" for m in reversed(messages) if m.role == "assistant"), ""
    )
    for item in reversed(messages):
        if item.role == "user":
            text = item.text_content or ""
            words = re.findall(r"[a-z]+", text.lower().replace("'", ""))
            if not words or at_risk_in(text, ""):
                return False
            if not NOBODY.search(text) and not RISK_QUESTION.search(last_agent):
                return False  # a plain no to some other question
            return words[0] in {"no", "nope", "nah", "nobody", "none", "just"} and all(
                w in DENIAL_WORDS for w in words
            )
    return False


def repair_note(call: Call, was_down: bool) -> str | None:
    """The note for the model when the caller says the system has failed after windows were
    offered: the visit is now a repair, so the earliest window comes first. Sets the flag that holds
    booking until check_availability runs again. None otherwise, and never for a call already
    urgent, where the urgent task decides what happens next."""
    if was_down or not call.system_down or not call.offered or urgent_reason(call):
        return None
    call.reoffer_for_repair = True
    held = " and offer to move their booking to it" if call.booked_turn >= 0 else ""
    return (
        "The caller just said their system has failed, so this visit is now a repair, not an "
        "estimate or maintenance. If you haven't asked whether anyone there is at risk, ask that "
        "first. Then call check_availability from today and offer the earliest open window"
        f"{held}, and book with the issue in their words."
    )


# Spanish on either of the caller's first two turns (A6). Only English is served, so code answers
# in Spanish rather than letting the model improvise a promise.
SPANISH = re.compile(
    r"\b(?:hola|habla|español|espanol|necesito|aire acondicionado|calefacci[oó]n|no funciona"
    r"|por favor)\b",
    re.IGNORECASE,
)


def note_urgency(call: Call, turn_ctx: llm.ChatContext, text: str) -> None:
    """Update the two urgency flags from a caller turn. Every turn counts, the one that fired the
    safety script included, so a false alarm doesn't lose "the heat's out"."""
    messages = [i for i in turn_ctx.items if i.type == "message"]
    last_agent = next(
        (m.text_content or "" for m in reversed(messages) if m.role == "assistant"), ""
    )
    call.system_down = call.system_down or down_in(SYSTEM_DOWN, text)
    at_risk = at_risk_in(text, last_agent)
    # The prompt has the agent ask who is at risk only once heating or cooling has failed, so a yes
    # to that question means the model judged the system down, even when the caller's words were
    # too far apart for SYSTEM_DOWN: "the AC. And now it's broken" paged on-call 22 s late, after
    # the name, the number and "Goodbye" (call 7gjANeDhy3Md). A needless page is cheap.
    if at_risk and RISK_QUESTION.search(last_agent):
        call.system_down = True
    call.at_risk = call.at_risk or at_risk
    call.heat_down = call.heat_down or down_in(HEAT_DOWN, text)
    call.cold = call.cold or bool(COLD.search(text))


def urgent_reason(call: Call) -> str | None:
    """Why this call is urgent by the rules code enforces, or None: no heat or cooling with
    someone at risk, or no heat in the cold whoever is home."""
    if call.system_down and call.at_risk:
        return "no heat or cooling with someone at risk"
    if call.heat_down and call.cold:
        return "no heat in cold weather"
    return None
