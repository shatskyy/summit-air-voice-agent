"""Pages to the on-call phone and pushes to dispatch, through ntfy."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import TYPE_CHECKING

import aiohttp

import store
from rules import given, is_real_name

if TYPE_CHECKING:
    from receptionist import Call

logger = logging.getLogger("summit-air")

NTFY_URL = "https://ntfy.sh"

_background: set[asyncio.Task] = set()

# ntfy topics are readable by anyone who knows the name, so a push carries no caller's words, number
# or street: a first name, the ZIP, what happened and where to look. The rest stays in the database.
ZIP_IN = re.compile(r"\b\d{5}\b")


def first_name(name: str) -> str:
    name = given(name or "").strip()
    return name.split()[0] if is_real_name(name) else ""


def zip_of(call: Call, address: str = "") -> str:
    found = ZIP_IN.findall(address or "")
    return found[-1] if found else call.checked_zip or ""


def paged_tasks(call: Call) -> list[tuple[int, str]]:
    """The call's paged tasks, as (ref, kind), leaving out an emergency called off as false."""
    refs = [(call.urgent_task, "urgent")]
    if not call.false_alarm:
        refs.append((call.hazard_task, "emergency"))
    return [(ref, kind) for ref, kind in refs if ref is not None]


async def address_escalation(call: Call, address: str) -> None:
    """Write a checked in-area address onto the call's paged tasks and tell on-call it arrived, once
    per task (first name and ZIP only, like every push). A later correction is written too."""
    call.checked_address = address
    for ref, kind in paged_tasks(call):
        await asyncio.to_thread(store.update_task_contact, call.db, ref, "", "", address)
        if ref not in call.address_pushed:
            call.address_pushed.add(ref)
            start_page(
                f"Summit Air {kind} #{ref}: address added",
                push_text(call, "Service address now on the task", "", address, ref),
            )


def push_text(call: Call, reason: str, name: str = "", address: str = "", ref=None) -> str:
    """A page or dispatch push: the reason, a first name and ZIP when known, and the lookup."""
    who = " ".join(p for p in (first_name(name), zip_of(call, address)) if p)
    lookup = f"Details: scripts/calls.py {ref or call.call_id}"
    return "\n".join([reason] + ([who] if who else []) + [lookup])


class HeldPage:
    """An emergency page that waits for the caller's answer to the safety script. It goes out on
    release() (any answer but a clear no, or the caller hanging up), or `wait` seconds after
    start(), and never after cancel(). start() is called once the script has finished playing:
    the script takes about 13 seconds to say, so a countdown from the moment the hazard was heard
    ran out before any caller could answer, and every "no, it's just dusty" still paged on-call.
    `cap` bounds the whole hold from the moment the page was filed, so a playout that never
    reports finishing can't keep a real emergency from going out. The task's result is whether
    ntfy accepted the page."""

    def __init__(self, title: str, message: str, wait: float, cap: float) -> None:
        self._go = asyncio.Event()
        self._started = asyncio.Event()
        self.cancelled = False
        self.task = asyncio.create_task(self._send(title, message, wait, cap))
        _background.add(self.task)
        self.task.add_done_callback(_background.discard)

    async def _countdown(self, wait: float) -> None:
        await self._started.wait()
        await asyncio.sleep(wait)
        self._go.set()

    async def _send(self, title: str, message: str, wait: float, cap: float) -> bool:
        countdown = asyncio.create_task(self._countdown(wait))
        try:
            await asyncio.wait_for(self._go.wait(), cap)
        except TimeoutError:
            pass
        finally:
            countdown.cancel()
        self._go.set()
        if self.cancelled:
            return False
        return await page_on_call(title, message)

    def start(self) -> None:
        """Start the countdown to the answer: the script has finished playing."""
        self._started.set()

    def release(self) -> None:
        self._go.set()

    def cancel(self) -> bool:
        """Call off the page. False when it had already gone out."""
        if self._go.is_set():
            return False
        self.cancelled = True
        self._go.set()
        return True


async def start_hold_after(held: HeldPage | None, handle) -> None:
    """Start a held page's countdown once `handle`, the safety script, has played out."""
    if held is None:
        return
    try:
        if handle is not None:
            await handle.wait_for_playout()
    finally:
        held.start()


async def release_held_page(call: Call) -> None:
    """Send a held page now and wait up to 5 s for it: the caller hung up without answering."""
    if call.held_page is None:
        return
    call.held_page.release()
    try:
        await asyncio.wait_for(asyncio.shield(call.held_page.task), timeout=5)
    except TimeoutError:
        pass


def start_page(title: str, message: str) -> asyncio.Task[bool]:
    """Page the on-call phone in the background. The task's result is whether ntfy accepted it."""
    page = asyncio.create_task(page_on_call(title, message))
    _background.add(page)
    page.add_done_callback(_background.discard)
    return page


async def confirmed(page: asyncio.Task[bool]) -> bool:
    """Whether ntfy accepted the page, waiting at most 3 s. The page keeps trying after that."""
    try:
        return await asyncio.wait_for(asyncio.shield(page), timeout=3)
    except TimeoutError:
        return False


async def page_on_call(title: str, message: str) -> bool:
    """Push to the on-call phone through ntfy. True means ntfy accepted it. A failed page is logged,
    never raised into the call."""
    return await push("NTFY_TOPIC", title, message, priority="urgent", tags="rotating_light")


async def push(
    variable: str, title: str, message: str, priority: str = "default", tags: str = ""
) -> bool:
    """Post to the ntfy topic named by the environment variable `variable`. True means ntfy
    accepted it; a failure is logged, never raised."""
    topic = os.getenv(variable)
    if not topic:
        logger.warning("%s is not set, so the push was skipped: %s", variable, title)
        return False
    try:
        # ntfy refuses a page with an error status (429 when rate-limited), not an exception.
        async with aiohttp.ClientSession(raise_for_status=True) as http:
            await http.post(
                f"{NTFY_URL}/{topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        logger.exception("push to %s failed: %s", variable, title)
        return False
    return True
