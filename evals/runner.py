"""Plays one scenario against the real prompt, tools and store, on a fixed clock.

This proves turn logic, tool routing and what gets written. It does not prove speech recognition,
the voice, turn timing or the phone line: "I smell gas" heard as "I just want gas" only shows up on
a call.
"""

import asyncio
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from livekit.agents import AgentSession, StopResponse, llm

import receptionist
import store
from evals.pricing import track
from evals.scenarios import Conversation, Scenario, style
from models import make_llm
from receptionist import GREETING, Call, SummitAirAgent, init_store, keep_promise

CALLER_MODEL = "openai/gpt-4.1-mini"
HANG_UP = "<hang up>"
MAX_TURNS = 16

# The review session runs with the office open, so that is the default. Night is for the scenarios
# whose outcome depends on the hour: urgent, emergency and callback targets.
CLOCKS = {
    "demo": datetime(2026, 9, 29, 12, 35, tzinfo=receptionist.TZ),  # Tuesday, office open
    "night": datetime(2026, 9, 28, 21, 0, tzinfo=receptionist.TZ),  # Monday, office closed
}

CALLER_RULES = f"""You are a caller phoning an HVAC company. Play the caller described below and
speak one short, natural phone turn at a time: plain words, no stage directions. Answer only what
you are asked; don't volunteer details early. When the agent has confirmed the next step and asks
whether there is anything else, say no and thank them. Once the call is over (the agent said
goodbye, or you have nothing left to say), reply with exactly {HANG_UP}.

The caller:
"""


@contextmanager
def clock(name: str) -> Iterator[datetime]:
    """Run receptionist on a fixed clock that starts at CLOCKS[name] and advances in real time.
    Every internal time read goes through receptionist.now, so patching it moves the prompt's date,
    the store's slots, office hours and callback targets together. Process-wide, so one clock at a
    time."""
    start = CLOCKS[name]
    real_start = datetime.now(receptionist.TZ)
    original = receptionist.now
    receptionist.now = lambda: start + (datetime.now(receptionist.TZ) - real_start)
    try:
        yield start
    finally:
        receptionist.now = original


def rows(db: Path, table: str) -> list[dict]:
    with store.connect(db) as conn:
        cur = conn.execute(f"select * from {table} order by ref")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


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


def ended(result) -> bool:
    """Only an end_call that went through ends the run; a refused one (the guard) comes back as an
    error output and the conversation carries on."""
    return any(
        getattr(e.item, "name", "") == "end_call"
        and getattr(e.item, "type", "") == "function_call_output"
        and not e.item.is_error
        for e in result.events
    )


async def play(scenario: Scenario, clock_name: str, model: str, run: int) -> dict:
    """One conversation. Call inside `with clock(clock_name)`."""
    db = Path(tempfile.mkdtemp(prefix="eval-")) / "eval.db"
    init_store(db)
    call = Call(
        call_id=f"eval-{scenario.name}-{clock_name}-{run}",
        caller_number=scenario.caller_number,
        db=db,
    )
    error = ""
    caller_usage = None
    async with (
        make_llm(model) as agent_llm,
        make_llm(CALLER_MODEL) as caller_llm,
        AgentSession(llm=agent_llm, userdata=call) as session,
    ):
        agent_usage = track(agent_llm, model)
        if scenario.brief:
            caller_usage = track(caller_llm, CALLER_MODEL)
        agent = SummitAirAgent(
            receptionist.render_instructions(receptionist.now(), call.caller_number)
        )
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
                # The safety script, when it fires, replaces the model's reply, as on a call.
                spoke_script = await backstop(agent, session, say)
                if not spoke_script and ended(await session.run(user_input=say)):
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

    convo = Conversation(transcript, rows(db, "bookings"), rows(db, "tasks"))
    failures = scenario.check(convo) + ([f"crashed: {error}"] if error else [])
    usage = {"agent": agent_usage.as_dict()}
    if caller_usage:
        usage["caller"] = caller_usage.as_dict()
    return {
        "scenario": scenario.name,
        "title": scenario.title,
        "category": scenario.category,
        "clock": clock_name,
        "model": model,
        "run": run,
        "passed": not failures,
        "failures": failures,
        "error": error,
        "style": style(convo),
        "usage": usage,
        "cost": round(sum(u["cost"] for u in usage.values()), 6),
        "transcript": transcript,
        "bookings": convo.bookings,
        "tasks": convo.tasks,
    }
