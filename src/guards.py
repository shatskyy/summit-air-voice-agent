"""Checks on what the model says: the reply guard on its output stream, and the after-the-fact
checks for a promised page or a booking confirmation with nothing behind them."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from livekit.agents import llm

import store

if TYPE_CHECKING:
    from livekit.agents import Agent

    from receptionist import Call

logger = logging.getLogger("summit-air")

# What the agent says when it tells a caller on-call is coming. GPT-4.1 mini said "I am paging the
# on-call technician now" without calling the tool (decision 4; 1 of 4 simulated elderly calls).
PAGE_PROMISE = re.compile(
    r"\bpag(e|ing)\b.{0,30}on.?call|on.?call (technician|tech)\b.{0,40}(call|reach|paged)"
    r"|callback within \d+ minutes|within (15|fifteen) minutes",
    re.IGNORECASE,
)

# The booking confirmation the prompt dictates, "[first name], you're booked for [day]", and its
# close relatives. On one simulated call the model asked "Which works?" and went on, in the same
# reply, "David, you're booked for Wednesday... Your reference number is one two three four",
# with no tool call and nothing in the store. A first cut filtered the reply sentence by sentence
# before the voice; a fresh-context review showed it silencing honest lines ("You're all set. Our
# target is to call you back by 10 AM") and dropping the tool call that would have made the
# confirmation true, so this is the keep_promise shape instead: after the fact, never silencing,
# and precise about the phrase.
FABRICATED = re.compile(
    r"\b(?:you'?re|you are|you'?ve been|you have been|i'?ve got you|i have you) (?:now |all |officially )?"
    r"(?:booked|down) (?:for|in|on)\b|\breference number is\b|\bconfirmation number is\b",
    re.IGNORECASE,
)
CORRECTION_NOTE = (
    "Your last reply told the caller the visit was booked, but nothing is booked on this call and "
    "no reference exists. In your next reply say plainly that it is not booked yet, then book it "
    "with book_appointment once they accept a window, and confirm only what that tool returns."
)


async def flag_fabricated_confirmation(agent: Agent, call: Call, text: str) -> bool:
    """A spoken booking confirmation with no booking in the store: tell the model so its next
    reply corrects it. True means one was found. The store is read, not a flag, since a booking
    can land after the tool was cancelled by an interruption."""
    if not FABRICATED.search(text):
        return False
    if await asyncio.to_thread(store.booking_for, call.db, call.call_id):
        return False
    call.fabricated_confirmations += 1
    logger.warning("the agent confirmed a booking that does not exist; telling it to correct")
    try:
        kept = agent.chat_ctx.copy()
        kept.add_message(role="system", content=CORRECTION_NOTE)
        await agent.update_chat_ctx(kept)
    except Exception:
        logger.exception("the correction note was not added")
    return True


# The reply guard (ADR-017). It sits on the model's output stream, which feeds the voice, the
# transcript and the tool calls alike, so what it drops is neither said, stored nor run. It does
# two things and nothing else, each from the 2026-09-28 10:04 call:
# - A sentence identical to one already said in the same reply is dropped. The model wrote "We
#   don't need the ZIP for Brooklyn. You want an estimate to install a new AC, right?" twice in
#   one reply, and the caller heard both.
# - A book_appointment call made after the agent has asked the caller something they haven't
#   answered yet is held, never run. The model said "Which do you want?" and moved the booking
#   in the same reply, then was talked over by the caller's answer. Holding the call leaves the
#   question standing; the model books on the caller's answer.
# Last night's reverted filter (ADR-016) cut confirmation sentences and dropped a booking riding
# beside them. This one never cuts the first sentence, never drops a sentence that isn't an exact
# repeat, and never drops any other tool call. If it fails, the rest of the reply goes through as
# the model wrote it.
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")


def sentence_key(sentence: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", sentence.lower()))


def asked_since_caller(items) -> bool:
    """Whether the agent has already asked a question since the caller last spoke: a
    book_appointment call now would book before the caller answered it."""
    for item in reversed(items or []):
        if getattr(item, "type", None) != "message":
            continue
        if item.role == "user":
            return False
        if item.role == "assistant" and "?" in (item.text_content or ""):
            return True
    return False


class ReplyGuard:
    """One model reply's text, sentence by sentence. The first sentence streams straight through,
    so the first audio waits on nothing; later ones are held to their end, behind the audio of the
    first, and dropped if the reply already said them."""

    def __init__(self, asked: bool = False) -> None:
        self.asked = asked  # the agent has asked something the caller hasn't answered
        self.said: set[str] = set()
        self.first = ""  # the first sentence so far, already passed through
        self.first_done = False
        self.pending = ""  # a later sentence, held until it ends
        self.repeats = 0

    def _settle(self, sentence: str, gap: str) -> str:
        key = sentence_key(sentence)
        if key and key in self.said:
            self.repeats += 1
            return ""
        if key:
            self.said.add(key)
        if sentence.rstrip().endswith("?"):
            self.asked = True
        return sentence + gap

    def _close_first(self) -> None:
        self.first_done = True
        if key := sentence_key(self.first):
            self.said.add(key)
        if self.first.rstrip().endswith("?"):
            self.asked = True

    def feed(self, text: str) -> str:
        """The part of `text` to say now."""
        if not self.first_done:
            joined = self.first + text
            end = SENTENCE_BREAK.search(joined)
            if end is None:
                self.first = joined
                return text
            passed = joined[len(self.first) : end.end()]
            self.first = joined[: end.start()]
            self._close_first()
            return passed + self.feed(joined[end.end() :])
        self.pending += text
        out = []
        while end := SENTENCE_BREAK.search(self.pending):
            sentence, gap = self.pending[: end.start()], self.pending[end.start() : end.end()]
            self.pending = self.pending[end.end() :]
            out.append(self._settle(sentence, gap))
        return "".join(out)

    def flush(self) -> str:
        """Whatever is still held, at the end of the reply or before a tool call."""
        if not self.first_done:
            if self.first:
                self._close_first()
            return ""
        sentence, self.pending = self.pending, ""
        return self._settle(sentence, "") if sentence.strip() else sentence


async def guard_reply(chunks, call: Call, asked: bool = False):
    """The model's output with the reply guard applied (see ReplyGuard). `asked` says whether an
    earlier step of this turn already asked the caller something."""
    guard = ReplyGuard(asked)
    broken = False
    try:
        async for chunk in chunks:
            if broken:
                yield chunk
                continue
            try:
                out = list(_guard_chunk(guard, chunk, call))
            except Exception:
                logger.exception(
                    "the reply guard failed; the rest of this reply goes through as is"
                )
                broken = True
                if guard.pending:
                    yield guard.pending
                yield chunk
                continue
            for item in out:
                yield item
        if not broken and (tail := guard.flush()):
            yield tail
    finally:
        call.repeats_dropped += guard.repeats


def _guard_chunk(guard: ReplyGuard, chunk, call: Call):
    if isinstance(chunk, str):
        if text := guard.feed(chunk):
            yield text
        return
    if not isinstance(chunk, llm.ChatChunk):  # a FlushSentinel: say what is held, then pass it
        if tail := guard.flush():
            yield tail
        yield chunk
        return
    if chunk.delta is None:
        yield chunk
        return
    content = guard.feed(chunk.delta.content) if chunk.delta.content else ""
    kept = chunk.delta.tool_calls
    if kept:
        content += guard.flush()  # the words come before the tool call; judge them first
        if guard.asked:
            kept = [tool for tool in kept if tool.name != "book_appointment"]
            if held := len(chunk.delta.tool_calls) - len(kept):
                call.bookings_held += held
                logger.warning(
                    "held a booking made before the caller answered the agent's question"
                )
    delta = chunk.delta.model_copy(update={"content": content or None, "tool_calls": kept})
    yield chunk.model_copy(update={"delta": delta})
