# Architecture

What the system does today. Where it differs from the first design, the reason is in
[decisions.md](decisions.md).

## Files

| File | Owns |
|---|---|
| [`src/agent.py`](../src/agent.py) | The audio pipeline: speech, the model and its fallback, turn-taking, caller ID, silence handling, the warm process, the startup key check and the health-check job |
| [`src/models.py`](../src/models.py) | Where a model runs: OpenAI models on OpenAI's API, and anything else refused |
| [`src/receptionist.py`](../src/receptionist.py) | Everything the caller experiences: the greeting, prompt rendering, the tools, the hazard, urgent and Spanish rules, the held page and the failure ladder |
| [`src/record.py`](../src/record.py) | The call record written when a call ends, the abandoned-call callback and the dispatch push |
| [`src/prompt.md`](../src/prompt.md) | The prompt, with placeholders filled from the configuration, today's date and the caller's number |
| [`src/store.py`](../src/store.py) | The SQLite schema and every query |
| [`config/business.yaml`](../config/business.yaml) | The business: service area, services, hours, windows, capacity, fees, callback targets, keyterms |
| [`scripts/calls.py`](../scripts/calls.py) | Recent calls, and one call's sheet with its transcript |
| [`scripts/watchdog.py`](../scripts/watchdog.py) | The 5-minute health check ([ADR-013](decisions.md#adr-013-hosting-launchd-and-a-watchdog)) |
| [`evals/`](../evals) | Simulated calls, their checks, pricing and the spend ledger ([ADR-012](decisions.md#adr-012-evals-fixed-clocks-database-checks-a-spend-ledger)) |

## Layers

| Layer | Owns | Does not own |
|---|---|---|
| Conversation (model) | Understanding the caller, asking for what is missing, judging softer urgency, phrasing | Availability, capacity, whether a booking exists |
| Turn rules (code) | Gas, carbon monoxide and smoke (the script, the held page, the closing line); no heat or cooling with someone at risk, or no heat in the cold whoever is home (the urgent task, [ADR-018](decisions.md#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home)); a Spanish opening (the Spanish line and a callback); the model's own reply, checked against the store after the fact, with a correction note when it confirmed a booking that isn't there ([ADR-016](decisions.md#adr-016-a-confirmation-with-no-booking-behind-it-is-caught-and-corrected)); a reply guard that drops a sentence said twice in one reply and holds a booking made before the caller answered ([ADR-017](decisions.md#adr-017-a-reply-guard-on-the-models-output)) | Urgency phrased in words the lists don't hold |
| Tools (code) | Checking the address, offering windows, validating and writing bookings, filing dispatch tasks, paging, ending the call | What the caller heard |
| End of call (code) | The call summary, a callback for a call abandoned mid-problem, the dispatch push | Whether a person reads it |
| Store (SQLite) | The authoritative record of slots, bookings, tasks, calls, call summaries and heartbeats | Whether a person acted on a task |

## Call state

There is no separate state machine. The booking tool's required arguments are the list of what must
be known (type, name, number, address, ZIP, issue), so a missing field is something the model has to
ask for before the call can book. Code keeps only what the model must not be trusted with: the
windows actually offered on this call, the ZIP code that passed the coverage check (blank when a
borough address was checked without one), whether an emergency or urgent task already exists and
its target, the `system_down`, `at_risk`, `heat_down` and `cold` flags, the checked address copied onto paged tasks, a held emergency page, the caller-turn counter
and the turn whose booking write went through, and the silence timer. The caller's number comes
from caller ID and is confirmed rather than dictated. Two tools also read the call so far: whether
the caller actually said the ZIP, or has just said nobody is at risk. Everything else code knows
comes from the model's tool arguments ([ADR-022](decisions.md#adr-022-code-owns-facts-and-actions-the-model-owns-the-conversation)).

## Tools

| Tool | Returns |
|---|---|
| `check_address` | Whether the ZIP is in the service area, and the street, town and ZIP to read back, with an instruction to say only the read-back and call `check_availability` after the caller's yes ([ADR-022](decisions.md#adr-022-code-owns-facts-and-actions-the-model-owns-the-conversation)). On a call with an urgent or emergency task it writes the checked address onto that task and pushes on-call once ([ADR-021](decisions.md#adr-021-code-puts-the-callers-details-on-a-paged-task-and-a-yes-confirms-only-a-read-back-heard-to-the-end)). Run before booking; `check_availability` can offer windows earlier if the caller asks. An out-of-area ZIP ends scheduling, a street with no name ("487 Lane") is sent back, a ZIP the caller never said is sent back, and a blank ZIP is accepted for a covered borough |
| `check_availability` | Up to two open windows from a requested date or part of day, recorded as offered on this call |
| `book_appointment` | "Booked", "Moved from X to Y" or "Updated", with the reference spelled for speech, or a refusal with the next step: window not offered, ZIP outside the area, address never checked, no name yet (a relation like "your sister" is not one), no callback number when caller ID is withheld, a second address on one call (a corrected number or street in the same ZIP is not one), the window just filled, or a second write in the same caller turn. Forced to priority when an urgent task exists. A success fills missing task contacts and updates values copied from an earlier booking; independent task contacts are preserved |
| `create_dispatch_task` | A task number and a callback target time. Emergency and urgent tasks also page the on-call phone; one of each per call, and an urgent task is refused straight after the caller said nobody is at risk |
| `end_call` | Refused only on the caller's first turn (a stray word over the greeting); after that, when to end is the model's call ([ADR-022](decisions.md#adr-022-code-owns-facts-and-actions-the-model-owns-the-conversation)). Then a fixed goodbye and the hang-up |

Every refusal comes back as text the model can act on and say out loud. The model is asked for one
tool call per turn ([ADR-014](decisions.md#adr-014-one-tool-call-per-turn)), so a turn that needs
two tools takes two model rounds, up to five.

## Tracing a call

Each call gets its own LiveKit room, named `call-…` by the dispatch rule, and that room name is the
`call_id` on every row the call writes. When the call starts, a `calls` row records the caller's
number. When it ends, the same row gets LiveKit's session report: the transcript, every tool call
and its result, and per-turn timings. So one `call_id` connects what was said, what the tools did,
and the bookings and tasks that exist because of it. The worker log carries the room name and
per-turn latency for the same call.

When the call ends, code also writes one `call_summaries` row with no model call: the outcome
(`booked`, `moved`, `urgent`, `emergency`, `false_alarm`, `callback`, `info_only`, `abandoned`,
`hung_up_early`), urgency, the booking and task references, the window, which backstops fired,
whether the fallback model answered, errors, the median reply latency and the transcript. A one-line
summary goes to dispatch by ntfy (`DISPATCH_NTFY_TOPIC`, else `NTFY_TOPIC`) with only the outcome, a
first name, the ZIP and a lookup. `uv run python scripts/calls.py <call_id or reference>` prints the
full sheet. Health-check jobs write a `heartbeats` row and nothing else.
