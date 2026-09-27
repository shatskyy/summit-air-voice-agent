"""Summit Air inbound phone agent: the audio pipeline around the receptionist.

Run with `uv run python src/agent.py start`. The conversation itself lives in receptionist.py and
prompt.md; this file only wires speech, the model, turn-taking and the per-call record.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    llm,
    room_io,
    tts,
)
from livekit.plugins import deepgram, noise_cancellation, openai

import store
from models import make_llm
from receptionist import (
    CONFIG,
    DEFAULT_DB,
    DICTATION_MAX_DELAY,
    MAX_DELAY,
    Call,
    SilenceWatch,
    SummitAirAgent,
    init_store,
    keep_promise,
    now,
    render_instructions,
    wants_dictation,
)

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
    """OpenAI's voice when the key is set, since Inworld runs on the spent LiveKit credit."""
    if os.getenv("OPENAI_API_KEY"):
        return openai.TTS(model="gpt-4o-mini-tts", voice="coral")
    return inworld_voice()


def inworld_voice():
    return inference.TTS(
        model="inworld/inworld-tts-2-flash",
        voice="Ashley",
        extra_kwargs={"apply_text_normalization": "ON"},
    )


def speech():
    """Deepgram runs on its own free credit. Without a key, speech runs on LiveKit Inference."""
    if os.getenv("DEEPGRAM_API_KEY"):
        return (
            deepgram.STT(model="nova-3", keyterm=CONFIG["keyterms"], smart_format=True),
            # The backup voice takes over if Deepgram can't be reached. It can't rescue a sentence
            # that drops partway (call 6): the adapter never replays audio already heard.
            tts.FallbackAdapter([deepgram.TTS(model=TTS_VOICE), backup_voice()]),
        )
    logger.warning("DEEPGRAM_API_KEY is not set; speech runs on the LiveKit Inference credit")
    return inference.STT(model="assemblyai/universal-3-5-pro", language="en"), inworld_voice()


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


def log_turn_latency(event: ConversationItemAddedEvent) -> None:
    item = event.item
    metrics = getattr(item, "metrics", None)
    if not metrics:
        return
    fields = {k: round(v, 3) for k, v in metrics.items() if isinstance(v, float)}
    logger.info("turn latency role=%s %s", getattr(item, "role", "?"), fields)


async def save_call_record(ctx: JobContext) -> None:
    """Keep the transcript, tool calls and timings next to the bookings they produced."""
    try:
        report = ctx.make_session_report()
    except RuntimeError:
        return
    record = json.dumps(report.to_dict(), default=str)
    await asyncio.to_thread(store.save_call, DEFAULT_DB, ctx.room.name, None, record)


# One warm process answers the next call immediately. dev mode keeps none, which delayed the
# greeting by about 2.7 seconds on the first test calls.
server = AgentServer(num_idle_processes=1)


@server.rtc_session(agent_name=AGENT_NAME, on_session_end=save_call_record)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    caller = await ctx.wait_for_participant()
    call = Call(call_id=ctx.room.name, caller_number=caller_number(caller))
    await asyncio.to_thread(init_store, call.db)
    await asyncio.to_thread(store.save_call, call.db, call.call_id, call.caller_number)

    listening, speaking = speech()
    session = AgentSession[Call](
        userdata=call,
        stt=listening,
        tts=speaking,
        llm=llm.FallbackAdapter([make_llm(LLM_MODEL), make_llm(FALLBACK_LLM_MODEL)]),
        turn_handling=TurnHandlingOptions(
            turn_detection=turn_detector(),
            # Deepgram's final transcript can land after a 0.5 s wait, splitting one sentence into
            # two turns (call 4). 0.7 s gives it room. max_delay caps the wait when the detector
            # thinks the caller is mid-thought; set_patience stretches it for dictation.
            endpointing={"min_delay": 0.7, "max_delay": MAX_DELAY},
            interruption={"mode": "adaptive"},
        ),
        user_away_timeout=SILENCE_SECONDS,
    )
    session.on("conversation_item_added", log_turn_latency)

    def check_promise(event: ConversationItemAddedEvent) -> None:
        item = event.item
        if getattr(item, "role", None) == "assistant" and item.text_content:
            task = asyncio.create_task(keep_promise(call, item.text_content))
            background.add(task)
            task.add_done_callback(background.discard)

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
        agent=SummitAirAgent(render_instructions(now(), call.caller_number)),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony()
            ),
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
