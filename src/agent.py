"""Summit Air inbound phone agent.

G0 build: answers a real call over the Twilio SIP trunk, greets the caller, and exposes one
deliberately slow tool so filler speech, interruption and latency can be observed on a live
call. Business logic arrives in later commits.
"""

import asyncio
import logging
import os

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    room_io,
)
from livekit.plugins import noise_cancellation

load_dotenv(".env.local")

logger = logging.getLogger("summit-air")

AGENT_NAME = "summit-air"

# Model IDs are configuration so a failed test call can swap one without a code change.
STT_MODEL = os.getenv("STT_MODEL", "assemblyai/universal-3-5-pro")
LLM_MODEL = os.getenv("LLM_MODEL", "google/gemma-4-31b-it")
TTS_MODEL = os.getenv("TTS_MODEL", "inworld/inworld-tts-2-flash")
TTS_VOICE = os.getenv("TTS_VOICE", "Ashley")

GREETING = "Thanks for calling Summit Air. This is the automated assistant. How can I help?"

INSTRUCTIONS = """\
You are the phone assistant for Summit Air, a heating and cooling company. This is a
connection test, so keep every reply to one or two short sentences of plain spoken English.
If the caller asks about availability, call check_availability. Never say you are testing.
"""


class SummitAirAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)

    async def on_enter(self) -> None:
        self.session.say(GREETING)

    @function_tool
    async def check_availability(self, context: RunContext, day: str) -> str:
        """Look up open service windows for a day the caller names.

        Args:
            day: The day the caller asked about, in their words.
        """
        # Deliberately slow in G0 so the filler line and interruptions can be heard on a call.
        async with context.with_filler("One moment while I check the schedule.", delay=1.0):
            await asyncio.sleep(4)
        return f"There is an opening {day} between eight and noon."


def log_turn_latency(event: ConversationItemAddedEvent) -> None:
    item = event.item
    metrics = getattr(item, "metrics", None)
    if not metrics:
        return
    fields = {k: round(v, 3) for k, v in metrics.items() if isinstance(v, float)}
    logger.info("turn latency role=%s %s", getattr(item, "role", "?"), fields)


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}

    session = AgentSession(
        stt=inference.STT(model=STT_MODEL, language="en"),
        llm=inference.LLM(model=LLM_MODEL),
        tts=inference.TTS(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            extra_kwargs={"apply_text_normalization": "ON"},
        ),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            endpointing={"min_delay": 0.5, "max_delay": 3.0},
            interruption={"mode": "adaptive"},
        ),
    )
    session.on("conversation_item_added", log_turn_latency)

    await session.start(
        agent=SummitAirAgent(),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=noise_cancellation.BVCTelephony(),
            ),
        ),
    )
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
