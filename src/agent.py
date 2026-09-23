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
    UserStateChangedEvent,
    cli,
    inference,
    llm,
    room_io,
)
from livekit.plugins import deepgram, noise_cancellation

import store
from receptionist import (
    CONFIG,
    DEFAULT_DB,
    Call,
    SummitAirAgent,
    init_store,
    now,
    render_instructions,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

logger = logging.getLogger("summit-air")

AGENT_NAME = "summit-air"

# Model IDs are configuration so a failed test call can swap one without a code change. The two
# candidates back each other up: whichever is primary, the other is the fallback.
CANDIDATE_LLMS = ("google/gemma-4-31b-it", "openai/gpt-4.1-mini")
LLM_MODEL = os.getenv("LLM_MODEL", CANDIDATE_LLMS[0])
FALLBACK_LLM_MODEL = os.getenv(
    "FALLBACK_LLM_MODEL", next(m for m in CANDIDATE_LLMS if m != LLM_MODEL)
)
TTS_VOICE = os.getenv("TTS_VOICE", "aura-2-thalia-en")


def speech():
    """Deepgram runs on its own free credit. Without a key, fall back to LiveKit Inference."""
    if os.getenv("DEEPGRAM_API_KEY"):
        return (
            deepgram.STT(model="nova-3", keyterm=CONFIG["keyterms"], smart_format=True),
            deepgram.TTS(model=TTS_VOICE),
        )
    logger.warning("DEEPGRAM_API_KEY is not set; speech runs on the LiveKit Inference credit")
    return (
        inference.STT(model="assemblyai/universal-3-5-pro", language="en"),
        inference.TTS(
            model="inworld/inworld-tts-2-flash",
            voice="Ashley",
            extra_kwargs={"apply_text_normalization": "ON"},
        ),
    )


def caller_number(participant: rtc.RemoteParticipant) -> str | None:
    """Caller ID from the SIP participant: the attribute, or the identity LiveKit gives SIP callers."""
    logger.info("SIP attribute keys: %s", sorted(participant.attributes))  # keys only, never values
    number = participant.attributes.get("sip.phoneNumber")
    if not number and participant.identity.startswith("sip_"):
        number = participant.identity.removeprefix("sip_")
    return number or None


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

    stt, tts = speech()
    session = AgentSession[Call](
        userdata=call,
        stt=stt,
        tts=tts,
        llm=llm.FallbackAdapter(
            [inference.LLM(model=LLM_MODEL), inference.LLM(model=FALLBACK_LLM_MODEL)]
        ),
        turn_handling=TurnHandlingOptions(
            # v1-mini runs locally, so turn detection spends no inference credit.
            turn_detection=inference.TurnDetector(version="v1-mini"),
            interruption={"mode": "adaptive"},
        ),
        user_away_timeout=12.0,
    )
    session.on("conversation_item_added", log_turn_latency)

    async def check_in_or_hang_up() -> None:
        call.away_count += 1
        if call.away_count == 1:
            session.generate_reply(
                instructions="The caller has gone quiet. Ask briefly whether they are still there."
            )
            return
        await session.say("I'll let you go. Call us back any time.", allow_interruptions=False)
        await ctx.delete_room()

    pending: set[asyncio.Task] = set()

    def on_user_state(event: UserStateChangedEvent) -> None:
        if event.new_state == "speaking":
            call.away_count = 0
        elif event.new_state == "away":
            task = asyncio.create_task(check_in_or_hang_up())
            pending.add(task)
            task.add_done_callback(pending.discard)

    session.on("user_state_changed", on_user_state)

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
