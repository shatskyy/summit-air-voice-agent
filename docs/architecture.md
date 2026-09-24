# Architecture

What the system does today. Where it differs from the first design, the reason is in
[decisions.md](decisions.md).

## Files

| File | Owns |
|---|---|
| [`src/agent.py`](../src/agent.py) | The audio pipeline: speech, the model and its fallback, turn-taking, caller ID, silence handling, the warm process and the per-call record |
| [`src/receptionist.py`](../src/receptionist.py) | Everything the caller experiences: the greeting, prompt rendering, the tools and the hazard backstop |
| [`src/prompt.md`](../src/prompt.md) | The prompt, with placeholders filled from the configuration, today's date and the caller's number |
| [`src/store.py`](../src/store.py) | The SQLite schema and every query |
| [`config/business.yaml`](../config/business.yaml) | The business: service area, hours, windows, capacity, fees, callback targets, keyterms |

## Layers

| Layer | Owns | Does not own |
|---|---|---|
| Conversation (model) | Understanding the caller, asking for what is missing, judging softer urgency, phrasing | Availability, capacity, whether a booking exists |
| Hazard check (code) | Recognizing gas, carbon monoxide and smoke signals and interrupting with a fixed script | Nuanced urgency |
| Tools (code) | Offering windows, validating and writing bookings, filing dispatch tasks, paging, ending the call | What the caller heard |
| Store (SQLite) | The authoritative record of slots, bookings, tasks and calls | Whether a person acted on a task |

## Call state

There is no separate state machine. The booking tool's required arguments are the list of what must
be known (type, name, number, address, ZIP, issue), so a missing field is something the model has to
ask for before the call can book. Code keeps only what the model must not be trusted with: the
windows actually offered on this call, whether an emergency task already exists, and the silence
count. The caller's number comes from caller ID and is confirmed rather than dictated.

## Tools

| Tool | Returns |
|---|---|
| `check_address` | Whether the ZIP is in the service area, and the street, town and ZIP to read back. Run before any window is offered; an out-of-area ZIP ends scheduling |
| `check_availability` | Up to two open windows from the requested date, recorded as offered on this call |
| `book_appointment` | A reference number and the confirmed day, window and address, or a refusal with the next step: window not offered, ZIP outside the area, address never checked, no name yet, or the window just filled |
| `create_dispatch_task` | A task number and a callback target time. Emergency and urgent tasks also page the on-call phone |
| `end_call` | Nothing; says goodbye and hangs up |

Every refusal comes back as text the model can act on and say out loud.

## Tracing a call

Each call gets its own LiveKit room, named `call-…` by the dispatch rule, and that room name is the
`call_id` on every row the call writes. When the call starts, a `calls` row records the caller's
number. When it ends, the same row gets LiveKit's session report: the transcript, every tool call
and its result, and per-turn timings. So one `call_id` connects what was said, what the tools did,
and the bookings and tasks that exist because of it. The worker log carries the room name and
per-turn latency for the same call.
