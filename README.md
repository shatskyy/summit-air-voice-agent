# Summit Air Voice Agent

An inbound AI phone agent for Summit Air, a fictional 40-technician HVAC company serving Westchester,
Rockland and Putnam counties in New York. It answers the phone, works out what is wrong, separates
routine work from urgent work from a genuine emergency, and either books a real appointment, files a
dispatch task that somebody owns, or transfers the caller to a person.

> **Status: in development.** Architecture and decisions are settled; implementation is underway.
> Sections marked *pending* are filled in from observed behavior, not written in advance.

## The design claim

**The agent only says something happened if it actually happened.**

A booking is confirmed out loud only after the reservation has committed and returned an identifier.
A callback is promised only when a dispatch task exists with an owner, a priority and a due time. A
transfer is described as successful only when a person actually picked up. When a write times out,
its outcome is treated as unknown rather than failed or succeeded: the agent checks before it retries,
and a retry returns the original booking instead of creating a second one.

The voice model runs the conversation. It does not get to decide what is true about the calendar.

## What it handles

| | |
|---|---|
| **Understanding the problem** | In the caller's own words, before asking for anything. Details volunteered out of order are kept, never asked for again. |
| **Urgency** | Emergency, then urgent, then routine, reassessed on every turn. New information can interrupt any stage, including final confirmation. |
| **Emergencies** | Gas smell, carbon monoxide alarm, smoke. Detected in code, not left to the model, and the safety script is spoken immediately without waiting on any tool. |
| **Booking** | Against a persistent schedule with real capacity. At most two arrival windows offered at a time. |
| **People** | Live transfer to dispatch when the caller asks, or when the situation calls for it, with context passed along. If nobody answers, an owned handoff task and an honest next step. |
| **Failure** | If nothing can be saved, the agent says so instead of promising a callback. |

## Stack

| Layer | Choice |
|---|---|
| Agent framework | [LiveKit Agents](https://github.com/livekit/agents), Python |
| Telephony | Twilio number on a SIP trunk into LiveKit Cloud |
| Speech to text | AssemblyAI streaming, with territory and HVAC keyterms |
| Language model | Fast non-reasoning model, pinned after test calls |
| Text to speech | Cartesia |
| Store | Supabase Postgres |

Why each of these, and what was rejected: [docs/decisions.md](docs/decisions.md).

## Architecture

```
caller ──► Twilio number ──► SIP trunk ──► LiveKit Cloud
                                              │
                                              ▼
                          agent worker (one Python process)
                            • conversation: STT → LLM → TTS
                            • hazard check on every caller turn
                            • call state: what is known, what is missing
                            • tools: availability, booking, dispatch task, transfer
                                              │
                                              ▼
                                  Postgres: slots · bookings · dispatch tasks
```

Tools run inside the agent process. There is no public HTTP endpoint that can create or change a
booking. Detail: [docs/architecture.md](docs/architecture.md).

## Deliberately not built

- **No ServiceTitan, CRM or calendar integration.** The schedule lives in Postgres with realistic
  slots, capacity and job types. Wiring a real system of record is a deployment task, and it does not
  change whether the conversation and the booking logic are right.
- **No automatic urgent dispatch.** No on-call capacity was supplied, so an urgent call gets a
  priority transfer or task, never an invented arrival time.
- **No price quotes or troubleshooting.** There is no verified pricebook, and diagnosing a gas
  appliance over the phone is a liability.
- **No multilingual support.** English only for this build. A Spanish-speaking caller is told so
  honestly and gets a callback task.
- **No speech-to-speech model.** See [ADR-002](docs/decisions.md#adr-002-cascaded-speech-pipeline).
- **No dispatcher dashboard.** Calls, bookings and tasks are inspected from the command line.

## Running it

*Pending.*

## Evaluation

*Pending.* Scenario tests cover the required behaviors plus the calls an HVAC operator would use to
test any answering vendor: a gas smell, no heat overnight with an infant in the house, a caller who
only wants a price, a caller who asks for a person, and an upset customer whose technician never came.

## Known limitations

*Pending, written from test calls.*

## Before this could take real calls

Confirm the actual territory, pricebook, capacity, membership rules and escalation staffing. Replace
the Postgres schedule with the customer's system of record. Measure acknowledgment of dispatch tasks,
not just their creation. Agree data retention. Then run a supervised pilot and review calls with the
operator, measuring booked-and-kept jobs rather than calls answered.
