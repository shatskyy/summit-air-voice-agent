# Summit Air Voice Agent

An inbound AI phone agent for Summit Air, a fictional 40-technician HVAC company serving Westchester,
Rockland and Putnam counties in New York. It answers the phone, works out what is wrong, separates
routine work from urgent work from a genuine emergency, and either books a real appointment or files
a dispatch task with a stated callback target.

**Read first:** the prompt, [`src/prompt.md`](src/prompt.md); the tools and the safety backstop,
[`src/receptionist.py`](src/receptionist.py); the booking store, [`src/store.py`](src/store.py); the
decisions, [`docs/decisions.md`](docs/decisions.md); every test call, [`docs/scenarios.md`](docs/scenarios.md).

> **Status.** Working and taking test calls. Booking is phone-tested (call 4 in
> [`docs/scenarios.md`](docs/scenarios.md)). The urgent and emergency paths pass the offline and
> model tests and have not had a phone call yet.

## The design claim

**The agent only says something happened if it actually happened.**

A booking is confirmed out loud only after the write has committed and returned a reference number.
A callback is promised only when a dispatch task exists with a priority and a due time. A retry, or a
caller changing their mind mid-call, moves the one booking the call holds instead of creating a
second.

The voice model runs the conversation. It does not get to decide what is true about the calendar.

## What it handles

| | |
|---|---|
| **Understanding the problem** | In the caller's own words, before asking for anything. Details volunteered out of order are kept, anything skipped is asked for before booking, and the name is also checked in code. |
| **Urgency** | Emergency, then urgent, then routine. The prompt re-decides whenever new facts arrive, including during confirmation. An urgent call is flagged before anything is scheduled. |
| **Emergencies** | Gas smell, carbon monoxide alarm, smoke. Detected in code, not left to the model, and the safety script is spoken before the model replies. |
| **Booking** | Against a persistent schedule with real capacity. At most two arrival windows offered at a time. No booking without an offered window, a covered ZIP code and the caller's name. |
| **People** | A request for a person, a reschedule, billing or a complaint becomes a callback task with a stated target, without argument. |
| **Silence** | One check-in after 12 seconds of silence. If the line stays quiet, a goodbye and a hang-up. |

## Stack

| Layer | Choice |
|---|---|
| Agent framework | [LiveKit Agents](https://github.com/livekit/agents) 1.8, Python |
| Telephony | Twilio number on an Elastic SIP trunk into LiveKit Cloud |
| Speech to text | Deepgram Nova-3, with territory and HVAC keyterms |
| Language model | Gemma 4 31B and GPT-4.1 mini through LiveKit Inference, each the other's fallback. Which one leads is still being decided on phone calls |
| Text to speech | Deepgram Aura-2 |
| Turn-taking | LiveKit's hosted turn detector (v1), adaptive interruption, telephony noise cancellation |
| Store | SQLite |
| On-call page | [ntfy](https://ntfy.sh) push |

Without a Deepgram key, speech falls back to AssemblyAI Universal-3.5 Pro and Inworld TTS-2 Flash
through LiveKit Inference. Why each choice, and what was rejected or reversed:
[docs/decisions.md](docs/decisions.md).

## Architecture

```
caller ──► Twilio number ──► SIP trunk ──► LiveKit Cloud
                                              │
                                              ▼
                     agent worker (one Python process, one call kept warm)
                       • speech to text → language model → text to speech
                       • hazard check on every caller turn
                       • tools: availability, booking, dispatch task, end call
                                              │
                        ┌─────────────────────┴───────────────────┐
                        ▼                                         ▼
     SQLite: slots · bookings · tasks · calls        ntfy push to the on-call phone
```

Tools run inside the agent process. There is no public HTTP endpoint that can create or change a
booking. Detail: [docs/architecture.md](docs/architecture.md).

## Deliberately not built

- **No ServiceTitan, CRM or calendar integration.** The schedule lives in SQLite with realistic
  windows and capacity. Wiring a real system of record is a deployment task, and it does not change
  whether the conversation and the booking logic are right.
- **No live transfer.** The only transfer destination this demo has is one person's cell phone. An
  unanswered cell rolls to voicemail, which the phone network reports as answered, so the agent could
  not tell a person from a greeting. A callback task with a stated target is the honest version.
  See [ADR-005](docs/decisions.md#adr-005-live-transfer-deferred).
- **No arrival times for urgent calls.** No on-call capacity was supplied, so an urgent call gets a
  page and a callback target, never an invented arrival time.
- **No repair quotes or troubleshooting.** The agent quotes the diagnostic fee and nothing else, and
  diagnosing a gas appliance over the phone is a liability.
- **No text-message confirmation.** Sending SMS from a US number needs A2P 10DLC registration.
- **English only.** A Spanish-speaking caller is told so in Spanish and gets a callback task.
- **No speech-to-speech model.** See [ADR-002](docs/decisions.md#adr-002-cascaded-speech-pipeline).
- **No dispatcher dashboard.** Calls, bookings and tasks are rows in SQLite (queries below).

## Running it

Needs Python 3.11 or later, [uv](https://docs.astral.sh/uv/), a LiveKit Cloud project, and for
phone calls a Twilio number on an Elastic SIP trunk pointed at the LiveKit project, with a LiveKit
inbound trunk for that number.

```sh
uv sync
cp .env.example .env.local        # lk app env --write --destination .env.local fills the LiveKit values
uv run python src/agent.py download-files
uv run python src/agent.py start
```

Route inbound calls to the agent with `lk sip dispatch create telephony/dispatch-rule.json`. Each
call gets its own room named `call-…`, and that room name keys the call's rows in the database.

For the review window the worker runs as one process on a home machine, kept alive by a launchd job
and kept awake while on power. See [ADR-001](docs/decisions.md#adr-001-livekit-agents-one-always-on-worker).

Inspecting what calls left behind:

```sh
sqlite3 data/summit-air.db "select ref, name, address, zip, slot_id from bookings order by ref desc limit 5"
sqlite3 data/summit-air.db "select ref, kind, reason, due_at from tasks order by ref desc limit 5"
```

## Evaluation

- **`uv run pytest`**: 31 offline checks, with no credentials and no cost. They cover capacity,
  retries and mid-call corrections in the store; the booking guards (a window never offered, a ZIP
  outside the area, a missing name); the hazard patterns, including phrases that must not trigger
  them; and prompt rendering.
- **`uv run pytest -m llm`**: 5 behavior tests, each run against both candidate models through
  LiveKit Inference with an LLM judge. They cover offering the windows a tool returned, never
  claiming an unmade booking, flagging an elderly caller without heat before scheduling, a price
  question, and a caller who wants a person. They spend a little credit.
- **Phone calls**: every one is a row in [`docs/scenarios.md`](docs/scenarios.md), with the
  requirement it checks, what happened and the room ID.

## Known limitations

- **Nobody is actually on call.** The urgent page reaches one test phone, and the 15-minute urgent
  target is stated as a target, not a guarantee.
- **Callback targets run on the wall clock.** After hours, a routine callback is still promised
  within two hours, which can mean the middle of the night.
- **The hazard check is a keyword list.** It over-triggers by design ("I don't smell gas" and a
  chirping smoke detector both match), and the safety script is worded to be harmless when it does.
- **One machine.** The worker and the database share one host. If it loses power or network, the
  number stops answering.
- **Latency is still being measured.** Replies took 0.8 to 1.6 s on the first calls and 1.3 to 3.7 s
  on call 4, the first on Deepgram speech.
- **Gemma leads on text evidence, pending a phone call.** Both candidates pass all five model tests;
  Gemma is faster to its first sentence. See [ADR-002](docs/decisions.md).

## Before this could take real calls

Confirm the actual territory, pricebook, capacity, membership rules and on-call staffing. Replace
the SQLite schedule with the customer's system of record, and move the worker to managed hosting
with a warm instance. Measure whether dispatch tasks get acknowledged, not just created. Agree data
retention. Then run a supervised pilot and review calls with the operator, measuring booked-and-kept
jobs rather than calls answered.
