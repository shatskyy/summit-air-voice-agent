"""Summit Air inbound phone agent: the audio pipeline around the receptionist.

Run with `uv run python src/agent.py start`. The conversation itself lives in receptionist.py and
prompt.md; this file only wires speech, the model, turn-taking and the per-call record.
"""

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    APIConnectOptions,
    ConversationItemAddedEvent,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    llm,
    room_io,
    tts,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import deepgram, google, noise_cancellation, openai

import store
from models import make_llm
from receptionist import (
    CONFIG,
    DEFAULT_DB,
    DICTATION_MAX_DELAY,
    MAX_DELAY,
    MAX_TOOL_STEPS,
    Call,
    FailureLadder,
    SilenceWatch,
    SummitAirAgent,
    flag_fabricated_confirmation,
    init_store,
    keep_promise,
    now,
    release_held_page,
    render_instructions,
    wants_dictation,
)
from record import finish_call

load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

logger = logging.getLogger("summit-air")

AGENT_NAME = "summit-air"

# Model IDs are configuration so a failed test call can swap one without a code change. The two
# candidates back each other up: whichever is primary, the other is the fallback. Both run on
# OPENAI_API_KEY (models.py). Gemma led until the LiveKit Inference credit ran out on 2026-09-27;
# GPT-4.1 mini is the model the phone calls and simulations proved alongside it.
CANDIDATE_LLMS = ("openai/gpt-4.1-mini", "openai/gpt-4.1")
LLM_MODEL = os.getenv("LLM_MODEL", CANDIDATE_LLMS[0])
FALLBACK_LLM_MODEL = os.getenv(
    "FALLBACK_LLM_MODEL", next(m for m in CANDIDATE_LLMS if m != LLM_MODEL)
)
TTS_VOICE = os.getenv("TTS_VOICE", "aura-2-thalia-en")
SILENCE_SECONDS = 12.0  # quiet before the check-in, and again before hanging up


def backup_voice():
    """OpenAI's voice, on its own key: the LiveKit Inference voices bill the spent LiveKit credit."""
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set, so there is no backup voice")
    return openai.TTS(model="gpt-4o-mini-tts", voice="coral")


def voice_provider() -> str:
    provider = os.getenv("TTS_PROVIDER", "deepgram").lower()
    if provider not in {"deepgram", "gemini"}:
        raise ValueError("TTS_PROVIDER must be deepgram or gemini")
    return provider


# The Gemini voices, best first. Each Gemini TTS model has its own 10-requests-a-minute limit on
# the paid Tier 1 key, and one phone call peaked at 7 on 3.1 alone (2026-09-28), so a sentence a
# model refuses goes to the next Gemini model before it goes to Deepgram's different voice. Order
# from probes on 2026-09-28: 3.8 Flash ranks highest on independent voice arenas and started audio in
# 1.0 to 1.1 s; 3.8 Flash Lite in 0.4 to 0.6 s; 3.1 Flash, the first voice on the line, in 0.7 to
# 1.0 s. The 2.5 Flash and Pro voices took 2.8 to 4.7 s to first audio, too slow for a phone call.
GEMINI_TTS_MODELS = (
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-3.1-flash-tts-preview",
)
# The plugin sends the style prompt in front of the text ('prompt:\n"text"'). Both 3.8 models read
# that prompt aloud as if it were the text (Flash Lite 2 of 2 probes, Flash 1 of 2), and read the
# bare text exactly (6 of 6), so only 3.1 gets it.
STYLE_PROMPT = (
    "Speak as a calm, friendly HVAC receptionist, at a natural conversational "
    "pace with an American accent. Read exactly the supplied text. "
    "Do not add, omit or change words."
)
STYLE_PROMPT_MODELS = {"gemini-3.1-flash-tts-preview"}


def gemini_models() -> list[str]:
    """GEMINI_TTS_MODELS, comma-separated, or the default order above."""
    raw = os.getenv("GEMINI_TTS_MODELS", "")
    return [m.strip() for m in raw.split(",") if m.strip()] or list(GEMINI_TTS_MODELS)


def gemini_voice(model: str | None = None):
    """One Gemini voice. With no model: GEMINI_TTS_MODEL (scripts/check_voice.py), else the first
    of gemini_models()."""
    if not os.getenv("GOOGLE_API_KEY"):
        raise RuntimeError("GOOGLE_API_KEY is required when TTS_PROVIDER=gemini")
    model = model or os.getenv("GEMINI_TTS_MODEL") or gemini_models()[0]
    return google.beta.GeminiTTS(
        model=model,
        voice_name=os.getenv("GEMINI_TTS_VOICE", "Kore"),
        vertexai=False,
        instructions=STYLE_PROMPT if model in STYLE_PROMPT_MODELS else None,
    )


def speech():
    """Deepgram transcribes; speech uses the selected provider and direct-provider backups."""
    if not os.getenv("DEEPGRAM_API_KEY"):
        raise RuntimeError("DEEPGRAM_API_KEY is not set")
    provider = voice_provider()
    voices = [deepgram.TTS(model=TTS_VOICE), backup_voice()]
    if provider == "gemini":
        voices = [gemini_voice(model) for model in gemini_models()] + voices
    # On the Gemini path, try the next voice after one failed attempt, without retry delays: a
    # rate-limited model answers at once, and the next Gemini model takes the sentence.
    speaking = tts.FallbackAdapter(voices, max_retry_per_tts=0 if provider == "gemini" else 2)
    return (
        deepgram.STT(model="nova-3", keyterm=CONFIG["keyterms"], smart_format=True),
        speaking,
    )


# Each model attempt gets 2.5 s before the other model takes over, and the session doesn't retry
# the pair: its default (3 retries, 2 s apart) left a caller in silence for up to about 24 s before
# the error reached FailureLadder. Both models run on one OpenAI key, so an outage of the key takes
# out both, and the ladder's line and callback are the only rescue.
LLM_ATTEMPT_TIMEOUT = 2.5
LLM_CONN = APIConnectOptions(max_retry=0)


def language_model() -> llm.FallbackAdapter:
    return llm.FallbackAdapter(
        [make_llm(LLM_MODEL), make_llm(FALLBACK_LLM_MODEL)], attempt_timeout=LLM_ATTEMPT_TIMEOUT
    )


# Without these the worker would run on nothing or on the LiveKit Inference credit, so it refuses to
# start: launchd restarts it every 10 s and the watchdog pages that the line is down.
REQUIRED_KEYS = (
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "OPENAI_API_KEY",
    "DEEPGRAM_API_KEY",
)


def missing_keys() -> list[str]:
    required = (*REQUIRED_KEYS, "GOOGLE_API_KEY") if voice_provider() == "gemini" else REQUIRED_KEYS
    return [key for key in required if not os.getenv(key)]


def turn_detector() -> inference.TurnDetector:
    """The hosted v1 detector, pinned. Unpinned, the plugin picks v1 only under `dev` or on LiveKit
    Cloud hosting, so `start` on this Mac silently ran the local v1-mini. On call 5, v1-mini scored
    complete short answers ("It's at a home.", "Yes.") below its threshold, so each reply waited the
    full max_delay: about 3.1 s end to end. v1 scored the same kind of turn 0.6 to 0.99 on call 3,
    and it bills against a separate monthly request quota, not the credit. If the hosted model
    can't be reached it falls back to v1-mini by itself (local_fallback)."""
    return inference.TurnDetector(version="v1", local_fallback=True)


# What Twilio sends in place of a withheld number: the words spelled out on a phone keypad
# (ANONYMOUS, UNAVAILABLE, UNKNOWN, BLOCKED, RESTRICTED).
WITHHELD = {"+266696687", "+86282452253", "+8656696", "+2562533", "+7378742833"}


def caller_number(participant: rtc.RemoteParticipant) -> str | None:
    """Caller ID from the SIP participant: the attribute, or the identity LiveKit gives SIP callers.
    None when the number was withheld, so the prompt asks for one instead of confirming it."""
    logger.info("SIP attribute keys: %s", sorted(participant.attributes))  # keys only, never values
    number = participant.attributes.get("sip.phoneNumber")
    if not number and participant.identity.startswith("sip_"):
        number = participant.identity.removeprefix("sip_")
    if not number or number in WITHHELD or not any(c.isdigit() for c in number):
        return None
    return number


def log_turn_latency(call: Call, event: ConversationItemAddedEvent) -> None:
    item = event.item
    metrics = getattr(item, "metrics", None)
    if not metrics:
        return
    fields = {k: round(v, 3) for k, v in metrics.items() if isinstance(v, float)}
    logger.info("turn latency role=%s %s", getattr(item, "role", "?"), fields)
    if getattr(item, "role", None) == "assistant" and "e2e_latency" in fields:
        call.reply_latencies.append(fields["e2e_latency"])


def is_healthcheck(metadata: str | None) -> bool:
    """Whether this job is the watchdog's health check (scripts/watchdog.py), not a call."""
    try:
        return json.loads(metadata or "{}").get("healthcheck") is True
    except (json.JSONDecodeError, AttributeError):
        return False


async def save_call_record(ctx: JobContext) -> None:
    """Keep the transcript, tool calls and timings next to the bookings they produced, then write
    the call's summary and push it to dispatch (record.py)."""
    if is_healthcheck(ctx.job.metadata):
        return
    try:
        report = ctx.make_session_report()
        session = ctx.primary_session
    except RuntimeError:
        return
    record = json.dumps(report.to_dict(), default=str)
    await asyncio.to_thread(store.save_call, DEFAULT_DB, ctx.room.name, None, record)
    try:
        await finish_call(session.userdata, session.history.items)
    except Exception:
        logger.exception("the call summary was not written")


# One warm process answers the next call immediately. dev mode keeps none, which delayed the
# greeting by about 2.7 seconds on the first test calls.
# Never refuse a call for CPU load. Under `start` the server marks itself unavailable once machine
# CPU passes load_threshold (default 0.7), which is right for a fleet where another worker takes the
# job, but this line has one worker on one Mac: a call that arrives then has nowhere to go and the
# caller hears silence. The log shows it six times on 2026-09-27 while simulations ran here. Load is
# CPU as a fraction capped at 1.0, and the check is load >= threshold with reserved jobs added on, so
# any finite value can still refuse; infinity is the value `dev` uses and the one the server treats
# as always available (livekit-agents 1.8, worker.py `_is_available`).
server = AgentServer(num_idle_processes=1, load_threshold=math.inf)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=save_call_record)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    if is_healthcheck(ctx.job.metadata):
        # The watchdog's probe: prove this worker takes jobs and can write the store, then leave.
        await asyncio.to_thread(init_store, DEFAULT_DB)
        await asyncio.to_thread(store.add_heartbeat, DEFAULT_DB, ctx.room.name)
        logger.info("health check answered")
        ctx.shutdown(reason="health check")
        return

    caller = await ctx.wait_for_participant()
    call = Call(call_id=ctx.room.name, caller_number=caller_number(caller), hang_up=ctx.delete_room)
    # A caller who hangs up after the safety script, without answering it, gets the page at once.
    ctx.add_shutdown_callback(lambda: release_held_page(call))
    await asyncio.to_thread(init_store, call.db)
    await asyncio.to_thread(store.save_call, call.db, call.call_id, call.caller_number)

    listening, speaking = speech()
    model = language_model()

    def note_fallback(event) -> None:
        if not event.available:
            call.fallback_used = True
            logger.warning("%s is unavailable; the fallback model answers", event.llm.model)

    model.on("llm_availability_changed", note_fallback)
    session = AgentSession[Call](
        userdata=call,
        stt=listening,
        tts=speaking,
        llm=model,
        turn_handling=TurnHandlingOptions(
            turn_detection=turn_detector(),
            # Deepgram's final transcript can land after a 0.5 s wait, splitting one sentence into
            # two turns (call 4). 0.7 s gives it room. max_delay caps the wait when the detector
            # thinks the caller is mid-thought; set_patience stretches it for dictation.
            endpointing={"min_delay": 0.7, "max_delay": MAX_DELAY},
            interruption={"mode": "adaptive"},
        ),
        user_away_timeout=SILENCE_SECONDS,
        conn_options=SessionConnectOptions(llm_conn_options=LLM_CONN),
        max_tool_steps=MAX_TOOL_STEPS,
    )
    # A provider that fails for good ends the call in code: a line, a callback task, the hang-up.
    session.on("error", FailureLadder(session, call).on_error)
    session.on("conversation_item_added", lambda event: log_turn_latency(call, event))

    def check_promise(event: ConversationItemAddedEvent) -> None:
        """What the agent just said, checked against the store: a page it promised without filing,
        a booking it confirmed without writing."""
        item = event.item
        if getattr(item, "role", None) == "assistant" and item.text_content:
            for check in (
                keep_promise(call, item.text_content),
                flag_fabricated_confirmation(agent, call, item.text_content),
            ):
                task = asyncio.create_task(check)
                background.add(task)
                task.add_done_callback(background.discard)

    agent = SummitAirAgent(render_instructions(now(), call.caller_number))
    background: set[asyncio.Task] = set()
    session.on("conversation_item_added", check_promise)

    patience = {"max_delay": MAX_DELAY}

    def set_patience(event: ConversationItemAddedEvent) -> None:
        """Wait longer for the caller's next turn when the agent just asked for an address or a
        number, and go back to the short wait after any other question."""
        item = event.item
        if getattr(item, "role", None) != "assistant" or not item.text_content:
            return
        wanted = DICTATION_MAX_DELAY if wants_dictation(item.text_content) else MAX_DELAY
        if wanted != patience["max_delay"]:
            patience["max_delay"] = wanted
            session.update_options(endpointing_opts={"max_delay": wanted})
            logger.info("endpointing max_delay -> %s", wanted)

    session.on("conversation_item_added", set_patience)

    silence = SilenceWatch(session, ctx.delete_room, wait=SILENCE_SECONDS)
    session.on("user_state_changed", silence.on_user_state)

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            ),
        ),
    )


if __name__ == "__main__":
    if missing := missing_keys():
        logging.basicConfig()
        logger.critical("not starting: %s missing from .env.local", ", ".join(missing))
        sys.exit(1)
    # Record the deployed source revision once at startup, without logging environment values.
    logging.basicConfig(level=logging.INFO)
    try:
        root = Path(__file__).resolve().parent.parent
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=5
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=root, text=True, timeout=5
            ).strip()
        )
        logger.info("worker source revision=%s dirty=%s", revision, dirty)
    except (OSError, subprocess.SubprocessError):
        logger.warning("worker source revision unavailable")
    cli.run_app(server)
