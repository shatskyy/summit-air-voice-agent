# Architecture

This document describes the target design. It is revised as each part is implemented and tested, so
that it describes what the system does rather than what was planned.

## Layers

| Layer | Owns | Does not own |
|---|---|---|
| Conversation (LLM) | Understanding the caller, asking for what is missing, judging softer urgency, phrasing | Availability, capacity, whether a booking exists |
| Hazard check (code) | Recognizing gas, carbon monoxide and smoke signals and interrupting with a fixed script | Nuanced urgency |
| Tools (code) | Validation, coverage, slot offers, booking writes, dispatch tasks, transfer | What the caller heard |
| Store (Postgres) | The authoritative record of slots, bookings and tasks | Whether a person acted on a task |

## Call state

The agent keeps a typed record of what the caller has said: issue, residential or commercial, name,
callback number, service address, availability, and whether a vulnerable occupant is present. Each
turn the model sees which fields are still missing, which is how it avoids asking for something it
already has. The callback number and service address are captured with a read-back and correction
step.

## Tools

| Tool | Returns |
|---|---|
| `check_availability` | Up to two eligible windows, each with an opaque slot identifier |
| `book_appointment` | A booking identifier and the confirmed details, or a reason it could not book |
| `create_dispatch_task` | A task identifier, priority, owner and due time |
| `transfer_to_dispatch` | Whether a person answered; falls back to a dispatch task if not |
| `end_call` | Nothing |

Every tool returns a result the agent can say out loud, including on failure.

## Tracing a call

*Pending.* One call identifier will connect the transcript, each tool call, and the rows written.
