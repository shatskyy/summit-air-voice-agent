# Final pass, September 28

Reviewed from `a539514` against the original employer assignment. No model, voice or architecture
change. The code and regression tests in this commit cover:

- A caller saying "yes, one more thing" after "anything else?" cannot authorize a hang-up.
  (Removed by ADR-022 on the evening of 2026-09-28: after the first turn, ending the call is the
  model's call, and the prompt carries this rule.)
- An interrupted SQLite booking keeps its lock until the write finishes. A same-turn retry cannot
  race it or move the appointment; the next caller turn can still request a correction.
- Booking contact corrections update copied dispatch details, including the caller-ID number.
  A repeated escalation tool call updates later details without a second task or page.
- An active emergency blocks booking. A rejected address correction invalidates the earlier check.
- A failed callback write does not produce a callback promise. The emergency closing line also
  omits that promise when no task was saved or caller ID is withheld.
- The provider-failure task uses the same cold-weather urgency rule as the conversation.
- Startup logs record the source commit and whether the checkout was dirty.
- The documented eval dry-run previews cost without requiring credit or calling billing APIs.

## Evidence

The baseline passed 468 offline tests. The final behavior passes 485 offline tests, with five paid
model tests excluded. Locked installation, Ruff lint and formatting passed in a fresh checkout without secrets on Python
3.11, as did all 485 tests. The local Python 3.14 run also passed all 485.
No paid simulations or phone calls were run for this pass. The September 27 result of 82/82 is
historical evidence on `c279b81`, not a result on this revision. Earlier phone evidence remains in
[scenarios.md](scenarios.md).

## Remaining limits

Spoken booking confirmation is still model-generated. Its post-hoc correction cannot retract a
false statement the caller already heard. Address/window acceptance is guided by the prompt and
question guard, not a complete consent state machine. The address key compares street words and
ZIP, not every unit or town detail. Review the actual read-back and stored address on the phone.

Urgent contact updates without a booking still depend on the model calling the update tool.
An interrupted booking can persist without its spoken confirmation, but retrying finds the same
reference. Routine callback tasks do not have a database idempotency key, so repeated tool requests
can create duplicates. These are narrower guarantees than "every retry is idempotent."

Gas negation, vulnerability and cold-weather checks remain keyword rules with the limitations in
[limitations.md](limitations.md). Gemini's slower speech startup is an intentional voice tradeoff.

## Three phone checks

1. Routine commercial repair: give the business and site contact, interrupt the address read-back,
   correct the street, accept an exact window, then change it. At "anything else?", say "yes, one
   more thing" and correct the callback number. Check one booking, the final details and reference.
2. No heat, elderly resident: ask for a callback only, then supply name, service address and a
   different number. Check one urgent task with those later details and a stated callback target.
3. Say "my furnace is out, I don't smell gas", then later "now I smell gas". Check no initial
   emergency, then safety instructions, one emergency task, no new booking and the closing line.

Use `uv run python scripts/calls.py` immediately after each call, then inspect the call ID or task
reference. Database-write failures require fault injection in offline tests; a normal phone call
cannot prove them. Worker health checks prove dispatch and database access, not PSTN audio.
