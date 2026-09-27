"""Conversation checks against a real model. They spend LiveKit Inference credit (about a cent per
model per run), so they only run when asked for: `uv run pytest -m llm`.

Each test runs against both candidate models; the results decide which one answers the phone.
Text tests prove turn logic and tool routing, not audio, latency or barge-in. Those need a call.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from dotenv import load_dotenv
from livekit.agents import AgentSession

from models import make_llm
from receptionist import Call, SummitAirAgent, init_store, now, render_instructions

load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

pytestmark = pytest.mark.llm

MODELS = ["openai/gpt-4.1-mini", "openai/gpt-4.1"]


@pytest.fixture(params=MODELS)
def model(request):
    return request.param


@pytest.fixture
def call(tmp_path):
    db = tmp_path / "test.db"
    init_store(db)
    return Call(call_id="test-call", caller_number="+19145550100", db=db)


@pytest.fixture
async def judge():
    async with make_llm("openai/gpt-4.1-mini") as judge_llm:
        yield judge_llm


@asynccontextmanager
async def conversation(model, call):
    async with (
        make_llm(model) as agent_llm,
        AgentSession(llm=agent_llm, userdata=call) as session,
    ):
        await session.start(SummitAirAgent(render_instructions(now(), call.caller_number)))
        yield session


async def test_offers_the_windows_the_tool_returned(model, call, judge):
    """The question from test calls 1 and 2, where Gemma answered its own filler line instead of the
    result. This build has no filler (the tools are local and fast), so this checks the question and
    the prompt, not that mechanism. Only a phone call can confirm the mechanism is gone."""
    async with conversation(model, call) as session:
        result = await session.run(
            user_input="My AC stopped cooling. It's a house and it's just me. Do you have anything tomorrow morning?"
        )
        result.expect.contains_function_call(name="check_availability")
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                judge,
                intent="Offers at least one specific day and time window and asks whether it works. It does not "
                "say it will wait for the caller and does not claim anything is booked.",
            )
        )


async def test_never_claims_a_booking_it_has_not_made(model, call, judge):
    """Call 1's worst moment: "Let me get a technician scheduled", then nothing happened."""
    async with conversation(model, call) as session:
        result = await session.run(
            user_input="Hi, this is Maria Lopez. My furnace won't start. I'm at 14 Maple Street in Brooklyn, "
            "11225, it's a house, and tomorrow morning works for me."
        )
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                judge,
                intent="Does not state or imply that a visit is booked or being scheduled. Either offers specific "
                "time windows or asks one short question. Does not ask again for anything the caller already gave.",
            )
        )


async def test_elderly_person_without_heat_is_flagged_before_scheduling(model, call):
    async with conversation(model, call) as session:
        result = await session.run(
            user_input="My heat went out last night and it's freezing in here. My mother is 80 and she lives with me."
        )
        result.expect.contains_function_call(
            name="create_dispatch_task", arguments={"kind": "urgent"}
        )
        assert not any(getattr(e.item, "name", "") == "book_appointment" for e in result.events)


async def test_price_question_gets_the_diagnostic_fee_and_nothing_more(model, call, judge):
    async with conversation(model, call) as session:
        result = await session.run(
            user_input="How much is it just to have someone come look at my furnace?"
        )
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                judge,
                intent="Says the diagnostic visit is $89 and that the technician prices any repair on site. "
                "Quotes no repair or equipment price.",
            )
        )


async def test_a_caller_who_wants_a_person_gets_a_callback_without_argument(model, call, judge):
    async with conversation(model, call) as session:
        result = await session.run(
            user_input="I don't want to talk to a machine. Get me a real person."
        )
        result.expect.contains_function_call(
            name="create_dispatch_task", arguments={"kind": "callback"}
        )
        # The task itself is the agreement. The judge reads only the last message, which may follow
        # an earlier "I can have someone call you back", so it checks the target and the tone.
        await (
            result.expect[-1]
            .is_message(role="assistant")
            .judge(
                judge,
                intent="Gives a specific callback target time and does not argue or try to talk the caller out of speaking to a person.",
            )
        )
