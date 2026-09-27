"""Simulated test calls: the call scripts, played in text against the real prompt, tools and store.

    uv run python tests/sim_calls.py                   # every scenario, both models, 3 runs each
    uv run python tests/sim_calls.py member no_show -n 1 -m google/gemma-4-31b-it

A caller either reads fixed lines or is played by a model from a short brief. Each run gets its own
database, and the checks read what was actually booked and filed there. Transcripts land in
logs/sim/ so the judgment calls (tone, what was said aloud) can be read afterwards.

This proves turn logic, tool routing and what gets written. It does not prove speech recognition,
the voice, turn timing or the phone line: "I smell gas" heard as "I just want gas" only shows up on
a call. Spends LiveKit Inference credit, about a cent per conversation.
"""

import argparse
import asyncio
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env.local")
os.environ.pop("NTFY_TOPIC", None)  # a simulated emergency must never page the real on-call phone

from livekit.agents import AgentSession, StopResponse, llm

import store
from models import make_llm
from receptionist import (
    GREETING,
    SAFETY_SCRIPT,
    Call,
    SummitAirAgent,
    init_store,
    keep_promise,
    now,
    render_instructions,
)

MODELS = ["openai/gpt-4.1-mini", "openai/gpt-4.1"]
CALLER_MODEL = "openai/gpt-4.1-mini"
HANG_UP = "<hang up>"
MAX_TURNS = 16

HOME = "48 Severn Lane, Chappaqua, 10514"


@dataclass
class Scenario:
    name: str
    title: str
    opening: str
    brief: str = (
        ""  # for a model-played caller; empty means `lines` are read in order, then hang up
    )
    lines: list[str] = field(default_factory=list)
    caller_number: str | None = "+19145550100"


SCENARIOS = [
    Scenario(
        "blocked_id",
        "2. Blocked caller ID",
        "Hi, my AC is making a rattling noise. Can someone come look at it this week?",
        brief=f"Your name is David Shatsky. Your address is {HOME}. It's a house, nobody at risk. "
        "Take the first window offered. If asked for a phone number, it's 914-555-0142.",
        caller_number=None,
    ),
    Scenario(
        "gas",
        "3. The new gas wording",
        "Hi, I think gas is leaking from my stove.",
        lines=["Yes."],
    ),
    Scenario(
        "price_robot",
        "4. Price shopper, then 'is this a robot?'",
        "How much do you charge to fix a furnace?",
        lines=[
            "What about a whole new AC system, ballpark?",
            "Wait, am I talking to a robot?",
            "I'm just shopping around, thanks.",
        ],
    ),
    Scenario(
        "member",
        "5. Member with a covered tune-up",
        "I'm on your maintenance plan and I'm due for my fall tune-up.",
        brief=f"Your name is David Shatsky. Your address is {HOME}. The number you're calling from "
        "is fine. Take the first window offered. If any price or fee comes up, say exactly: "
        '"Isn\'t that covered by my plan?"',
    ),
    Scenario(
        "no_show",
        "6. Angry no-show",
        "Your technician was supposed to come Friday morning and nobody showed up. I took the "
        "morning off work. This is ridiculous.",
        brief=f"You are annoyed but not abusive. Your second thing to say, whatever the agent "
        f'says first, is "I want to talk to a manager." Your name is David Shatsky, your address '
        f"is {HOME}, the AC isn't cooling. The number you're calling from is fine. Accept the "
        "first window offered.",
    ),
    Scenario(
        "spanish",
        "7. Spanish (model side only)",
        "Hola, mi aire acondicionado no funciona. ¿Pueden venir a revisarlo?",
        brief='You speak only Spanish. If asked your name and number, say: "Me llamo David, mi '
        'número es nueve uno cuatro, cinco cinco cinco, cero uno cuatro dos."',
    ),
    Scenario(
        "commercial",
        "8. Commercial rooftop",
        "I manage a dental office in White Plains. Our rooftop AC stopped working and it's "
        "getting really hot in here.",
        brief="Your name is David Shatsky. The business is Bright Smile Dental. The site contact "
        "is Maria, the office manager. Access: the roof hatch is in the back storage room. The "
        "address is 200 Main Street, White Plains, 10601. Nobody is medically at risk. The number "
        "you're calling from is fine. Take the first window offered. Only give each detail when "
        "asked for it.",
    ),
    Scenario(
        "address_change",
        "9. Address change during the read-back",
        "My AC isn't cooling well, can someone come check it?",
        brief=f"Your name is David Shatsky. Give the address as {HOME}. Nobody is at risk. The "
        "number you're calling from is fine. The first time the agent reads the address back to "
        'you, say exactly: "Actually, sorry, it\'s 52 Severn Lane, not 48." After that, confirm '
        "52 and accept the first window offered.",
    ),
    Scenario(
        "routine_furnace",
        "10. Routine furnace (naturalness)",
        "Hi. My furnace stopped working.",
        brief=f"It's your house. Nobody at risk: just you, healthy adult. Your name is David "
        f"Shatsky, your address is {HOME}. The number you're calling from is fine. Take the "
        "first window offered.",
    ),
    Scenario(
        "elderly_no_heat",
        "11. Elderly, no heat",
        "My heat went out last night and it's freezing in here. My mother is 80 and she lives "
        "with me.",
        brief=f"It's your house. Your name is David Shatsky, your address is {HOME}. The number "
        "you're calling from is fine. If offered a choice, take the first morning window. If "
        "told on-call will call back, accept that too.",
    ),
]

CALLER_RULES = f"""You are a caller phoning an HVAC company. Play the caller described below and
speak one short, natural phone turn at a time: plain words, no stage directions. Answer only what
you are asked; don't volunteer details early. When the agent has confirmed the next step and asks
whether there is anything else, say no and thank them. Once the call is over (the agent said
goodbye, or you have nothing left to say), reply with exactly {HANG_UP}.

The caller:
"""


@dataclass
class Result:
    scenario: str
    model: str
    run: int
    transcript: list[str]
    bookings: list[dict]
    tasks: list[dict]
    failures: list[str]
    error: str = ""
    style: dict = field(default_factory=dict)


def rows(db: Path, table: str) -> list[dict]:
    with store.connect(db) as conn:
        cur = conn.execute(f"select * from {table} order by ref")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def history_lines(session: AgentSession) -> list[str]:
    out = []
    for item in session.history.items:
        if item.type == "message":
            text = item.text_content or ""
            if text:
                out.append(f"{'CALLER' if item.role == 'user' else 'AGENT '}: {text}")
        elif item.type == "function_call":
            out.append(f"  tool  {item.name}({item.arguments})")
        elif item.type == "function_call_output":
            out.append(f"  ->    {item.output}")
    return out


async def caller_turn(caller_llm, scenario: Scenario, session: AgentSession) -> str:
    ctx = llm.ChatContext()
    ctx.add_message(role="system", content=CALLER_RULES + scenario.brief)
    ctx.add_message(role="user", content=GREETING)
    for item in session.history.items:
        if item.type == "message" and item.text_content:
            # Roles flip: the agent is the caller model's "user".
            ctx.add_message(
                role="assistant" if item.role == "user" else "user", content=item.text_content
            )
    text = ""
    async with caller_llm.chat(chat_ctx=ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                text += chunk.delta.content
    return text.strip()


async def settle(session: AgentSession) -> None:
    """Wait for whatever the agent is saying to finish."""
    for _ in range(20):  # a say() is queued, not yet current, for a moment after it's called
        if session.current_speech is not None:
            break
        await asyncio.sleep(0.05)
    for _ in range(50):
        speech = session.current_speech
        if speech is None or speech.done():
            return
        await asyncio.wait_for(asyncio.shield(speech.wait_for_playout()), 20)


async def backstop(agent: SummitAirAgent, session: AgentSession, text: str) -> bool:
    """Run the hazard hook the way a call does. LiveKit calls on_user_turn_completed only on the
    audio path, so session.run() alone would skip the keyword backstop. True means it spoke the
    safety script and the model's reply was dropped."""
    message = llm.ChatMessage(role="user", content=[text])
    try:
        await agent.on_user_turn_completed(agent.chat_ctx.copy(), message)
    except StopResponse:
        await settle(session)
        return True
    return False


async def play(scenario: Scenario, model: str, run: int) -> Result:
    tmp = Path(tempfile.mkdtemp(prefix="sim-"))
    db = tmp / "sim.db"
    init_store(db)
    call = Call(call_id=f"sim-{scenario.name}-{run}", caller_number=scenario.caller_number, db=db)
    error = ""
    async with (
        make_llm(model) as agent_llm,
        make_llm(CALLER_MODEL) as caller_llm,
        AgentSession(llm=agent_llm, userdata=call) as session,
    ):
        agent = SummitAirAgent(render_instructions(now(), call.caller_number))
        await session.start(agent)
        promises: list[asyncio.Task] = []
        session.on(
            "conversation_item_added",
            lambda e: (
                getattr(e.item, "role", None) == "assistant"
                and e.item.text_content
                and promises.append(asyncio.create_task(keep_promise(call, e.item.text_content)))
            ),
        )
        await settle(session)  # the greeting, before the caller speaks
        say, lines = scenario.opening, list(scenario.lines)
        try:
            for _ in range(MAX_TURNS):
                if await backstop(agent, session, say):
                    pass  # the safety script replaced the model's reply, as on a call
                else:
                    result = await session.run(user_input=say)
                    if any(getattr(e.item, "name", "") == "end_call" for e in result.events):
                        break
                if scenario.brief:
                    say = await caller_turn(caller_llm, scenario, session)
                else:
                    say = lines.pop(0) if lines else HANG_UP
                if not say or HANG_UP in say:
                    break
        except Exception as e:  # noqa: BLE001 - a crash is a finding; keep the transcript
            error = f"{type(e).__name__}: {e}"
        kept_promise = any(await asyncio.gather(*promises))
        transcript = history_lines(session)
        if kept_promise:
            transcript.append("  [backstop] filed the urgent task the agent promised")
    res = Result(scenario.name, model, run, transcript, rows(db, "bookings"), rows(db, "tasks"), [])
    res.error = error
    res.style = style(res)
    res.failures = CHECKS[scenario.name](res) + ([f"crashed: {error}"] if error else [])
    return res


# Hard checks: only what the store and the transcript can settle mechanically. Tone and wording are
# read from the transcripts.


def agent_text(r: Result) -> str:
    return "\n".join(line for line in r.transcript if line.startswith("AGENT"))


def digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def check_blocked_id(r):
    f = []
    if "calling from" in agent_text(r).lower():
        f.append("asked to confirm the calling number, but there is none")
    if len(r.bookings) != 1:
        f.append(f"{len(r.bookings)} bookings, expected 1")
    elif not digits(r.bookings[0]["phone"]).endswith("9145550142"):
        f.append(f"booking phone is {r.bookings[0]['phone']!r}, expected 914-555-0142")
    return f


def check_gas(r):
    f = []
    agent = [line for line in r.transcript if line.startswith("AGENT") and GREETING not in line]
    if not agent or SAFETY_SCRIPT[:20] not in agent[0]:
        f.append("the first agent line was not the backstop's 'Just to be safe'")
    if sum(t["kind"] == "emergency" for t in r.tasks) != 1:
        f.append(f"{sum(t['kind'] == 'emergency' for t in r.tasks)} emergency tasks, expected 1")
    if r.bookings:
        f.append("booked an appointment on an emergency")
    return f


def check_price_robot(r):
    f = []
    text = agent_text(r)
    if "89" not in text:
        f.append("never quoted the $89 diagnostic")
    prices = set(re.findall(r"\$\s?\d+(?:,\d{3})*", text)) - {"$89", "$159"}
    if prices:
        f.append(f"quoted other prices: {sorted(prices)}")
    if "automated" not in text.lower() and "ai" not in text.lower().split():
        f.append("never said it is automated")
    if r.bookings:
        f.append("booked a price shopper")
    return f


def check_member(r):
    f = []
    if "89" in agent_text(r):
        f.append("quoted the $89 diagnostic to a member")
    if len(r.bookings) != 1:
        f.append(f"{len(r.bookings)} bookings, expected 1")
    elif not re.search(
        r"member|plan", r.bookings[0]["note"] + r.bookings[0]["issue"], re.IGNORECASE
    ):
        f.append(f"membership not in the booking: note {r.bookings[0]['note']!r}")
    return f


def check_no_show(r):
    f = []
    if len(r.bookings) != 1:
        f.append(f"{len(r.bookings)} bookings, expected 1")
    elif not re.search(
        r"miss|no.?show|didn.?t show|never (came|showed)", r.bookings[0]["note"], re.IGNORECASE
    ):
        f.append(f"booking note doesn't say the visit was missed: {r.bookings[0]['note']!r}")
    if not any(t["kind"] == "callback" for t in r.tasks):
        f.append("no manager callback task")
    if "call you back by" not in agent_text(r).lower() and "target" not in agent_text(r).lower():
        f.append("never gave a callback target")
    return f


def check_spanish(r):
    f = []
    if "solo puedo atender en inglés" not in agent_text(r):
        f.append("never said the fixed Spanish line")
    if not any(t["kind"] == "callback" for t in r.tasks):
        f.append("no callback task")
    if r.bookings:
        f.append("booked instead of handing off")
    return f


def check_commercial(r):
    f = []
    if "roof" in agent_text(r).lower() and re.search(
        r"(go|climb|check).{0,20}roof", agent_text(r), re.IGNORECASE
    ):
        f.append("may have asked the caller onto the roof (read the transcript)")
    if len(r.bookings) != 1:
        f.append(f"{len(r.bookings)} bookings, expected 1")
        return f
    b = r.bookings[0]
    if b["customer_type"] != "commercial":
        f.append(f"booked as {b['customer_type']}")
    everything = " ".join(str(v) for v in b.values()).lower()
    for want in ("bright smile", "maria", "hatch"):
        if want not in everything:
            f.append(f"{want!r} not in the booking")
    return f


def check_address_change(r):
    f = []
    if len(r.bookings) != 1:
        return [f"{len(r.bookings)} bookings, expected 1"]
    if not r.bookings[0]["address"].strip().startswith("52"):
        f.append(f"booked address {r.bookings[0]['address']!r}, expected 52")
    # Did an agent line read 52 back before the booking call?
    read_back = False
    for line in r.transcript:
        if line.startswith("AGENT") and re.search(r"\b52\b|fifty.two", line, re.IGNORECASE):
            read_back = True
        if "book_appointment(" in line:
            break
    if not read_back:
        f.append("booked 52 without reading 52 back first")
    if not any("check_address(" in line and "52" in line for line in r.transcript):
        f.append("52 was never checked with check_address (decision 2)")
    return f


def check_routine_furnace(r):
    if len(r.bookings) != 1:
        return [f"{len(r.bookings)} bookings, expected 1"]
    if r.bookings[0]["customer_type"] != "residential":
        return [f"booked as {r.bookings[0]['customer_type']}"]
    return []


def check_elderly_no_heat(r):
    f = []
    urgent = [t for t in r.tasks if t["kind"] == "urgent"]
    if len(urgent) != 1:
        f.append(f"{len(urgent)} urgent tasks, expected 1")
    elif (
        r.bookings
        and r.bookings[0]["ref"]
        and r.bookings[0]["created_at"] < urgent[0]["created_at"]
    ):
        f.append("booked before filing the urgent task")
    if not re.search(r"15 minutes|fifteen minutes|call you back by", agent_text(r), re.IGNORECASE):
        f.append("never said the callback target")
    return f


CLOSE = re.compile(r"anything else", re.IGNORECASE)
HOME_WORDS = re.compile(r"\b(house|home|my (furnace|ac|heat|air))\b", re.IGNORECASE)
TYPE_Q = re.compile(r"residential|commercial|home or (a )?business", re.IGNORECASE)
SPOKEN_LIST = re.compile(
    r"elderly, an infant|\w+, \w+,? (and|or) (a |an )?(infant|zip|medical)", re.IGNORECASE
)


def style(r: Result) -> dict:
    """Soft naturalness counts: reported beside pass/fail, never failures themselves."""
    said = [x[8:] for x in r.transcript if x.startswith("AGENT") and GREETING not in x]
    bundled = sum(t.count("?") >= 2 and not CLOSE.search(t) for t in said)
    reasked = 0
    heard_home = False
    for x in r.transcript:
        if x.startswith("CALLER") and HOME_WORDS.search(x):
            heard_home = True
        if x.startswith("AGENT") and heard_home and TYPE_Q.search(x) and "?" in x:
            reasked += 1
    lists = sum(bool(SPOKEN_LIST.search(t)) for t in said)
    words = sum(len(t.split()) for t in said) / max(len(said), 1)
    return {"bundled": bundled, "reasked_type": reasked, "lists": lists, "words": round(words, 1)}


CHECKS = {
    "blocked_id": check_blocked_id,
    "gas": check_gas,
    "price_robot": check_price_robot,
    "member": check_member,
    "no_show": check_no_show,
    "spanish": check_spanish,
    "commercial": check_commercial,
    "address_change": check_address_change,
    "routine_furnace": check_routine_furnace,
    "elderly_no_heat": check_elderly_no_heat,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenarios", nargs="*", help=f"any of {', '.join(CHECKS)}")
    ap.add_argument("-n", "--runs", type=int, default=3)
    ap.add_argument("-m", "--model", action="append", help="default: both candidate models")
    ap.add_argument("-j", "--jobs", type=int, default=2, help="conversations at once")
    args = ap.parse_args()

    chosen = [s for s in SCENARIOS if not args.scenarios or s.name in args.scenarios]
    models = args.model or MODELS
    gate = asyncio.Semaphore(args.jobs)

    async def one(s, m, i):
        # The free tier allows 110 model requests a minute across the project. A run that hits it
        # proves nothing about the agent, so wait out the minute and replay it.
        for _ in range(5):
            async with gate:
                res = await play(s, m, i)
            if "429" not in res.error:
                return res
            await asyncio.sleep(60)
        return res

    started = time.monotonic()
    results = await asyncio.gather(
        *(one(s, m, i) for s in chosen for m in models for i in range(1, args.runs + 1))
    )

    out = ROOT / "logs" / "sim" / now().strftime("%Y-%m-%d-%H%M")
    out.mkdir(parents=True, exist_ok=True)
    for r in results:
        short = r.model.split("/")[-1]
        body = [
            f"# {r.scenario} / {r.model} / run {r.run}",
            "PASS" if not r.failures else "FAIL: " + "; ".join(r.failures),
            f"style: {r.style}",
            "",
            *r.transcript,
            "",
            f"bookings: {r.bookings}",
            f"tasks: {r.tasks}",
        ]
        (out / f"{r.scenario}-{short}-{r.run}.txt").write_text("\n".join(body) + "\n")

    print(f"\n{len(results)} conversations in {time.monotonic() - started:.0f}s -> {out}\n")
    for s in chosen:
        print(s.title)
        for m in models:
            mine = [r for r in results if r.scenario == s.name and r.model == m]
            passed = sum(not r.failures for r in mine)
            print(f"  {m:24} {passed}/{len(mine)}")
            for r in mine:
                for why in r.failures:
                    print(f"      run {r.run}: {why}")

    # Naturalness, summed over every conversation a model had. Lower is better except words.
    summary = ["", "Naturalness (per model, all scenarios):"]
    for m in models:
        mine = [r for r in results if r.model == m]
        turns = sum(sum(x.startswith("AGENT") for x in r.transcript) for r in mine)
        total = {k: sum(r.style[k] for r in mine) for k in ("bundled", "reasked_type", "lists")}
        words = sum(r.style["words"] for r in mine) / max(len(mine), 1)
        summary.append(
            f"  {m:24} bundled {total['bundled']}, re-asked home/business "
            f"{total['reasked_type']}, spoken lists {total['lists']}, "
            f"{words:.1f} words a turn, over {turns} agent turns"
        )
    print("\n".join(summary))
    (out / "SUMMARY.txt").write_text("\n".join(summary) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
