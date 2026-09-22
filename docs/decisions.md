# Architecture decisions

Each record states what was decided, why, what it costs, and what evidence would reverse it.

## ADR-001: LiveKit Agents, self-hosted worker

**Status:** Accepted

**Decision.** Build on LiveKit Agents in Python. Run the agent worker as a single always-on process
connected to LiveKit Cloud.

**Why.** Tools execute inside the agent process, so booking logic, validation and the database client
sit next to the conversation with no public HTTP endpoint that can create or change a booking. With a
managed platform such as Retell or Vapi, the same logic would live in a separately hosted webhook
service that needs its own authentication, signature verification and uptime, and the prompt would
live in a dashboard rather than in this repository. Keeping the worker always on avoids the cold start
a scale-to-zero host adds to the first call after idle.

**Cost.** More to own than a managed platform: turn-taking configuration, provider choices and process
lifecycle are this repository's responsibility.

**Would reverse it.** A real call that cannot reliably reach the agent after a focused configuration
fix. The fallback is Retell with an exported prompt and a small booking service.

## ADR-002: Cascaded speech pipeline

**Status:** Accepted

**Decision.** Speech to text, then a language model, then text to speech, rather than a
speech-to-speech model.

**Why.** The riskiest thing this agent does is capture a street address, ZIP code and callback number
correctly over telephone audio. A cascaded pipeline produces a transcript at every turn, supports
keyterm biasing for the territory's town names and HVAC vocabulary, and lets deterministic code
validate an address before anything is booked. It also guarantees ordering: the model cannot say
"you're booked" before the booking tool has returned.

**Cost.** Speech-to-speech models currently feel more natural at turn-taking. This build compensates
with a semantic turn detector and a longer end-of-turn allowance while the caller is dictating digits.

**Would reverse it.** Test calls showing a speech-to-speech model matches the cascade on exact address,
ZIP and phone capture without confirming ahead of the write.

## ADR-003: Confirm only after a durable write

**Status:** Accepted

**Decision.** The agent may confirm a booking only after the booking tool returns a stored identifier.
Bookings accept only a slot the agent actually offered, recheck capacity inside the write, and carry an
idempotency key so a retry returns the existing booking.

**Why.** An answering service fails most expensively when it tells a caller something that did not
happen. A timed-out write is treated as unknown and looked up, never retried blindly.

## ADR-004: Emergencies are detected in code

**Status:** Accepted

**Decision.** Every caller turn is checked against a fixed list of hazard signals (gas smell, rotten
egg odor, carbon monoxide alarm, smoke). A match interrupts the agent and plays a fixed safety script
immediately. The dispatch record is written in the background afterwards.

**Why.** Safety guidance must never wait on a tool call and must not depend on the model choosing to
follow an instruction. Softer urgency, such as no heat in cold weather with an elderly occupant, stays
with the model and is tested, because it depends on context a keyword list cannot judge.

**Cost.** A keyword list can over-trigger. A false alarm costs a cautious sentence; a miss costs far
more.

## ADR-005: Live transfer through a Twilio SIP trunk

**Status:** Accepted

**Decision.** Callers can be transferred to a person, over a Twilio number connected to LiveKit by SIP
trunk. If the transfer is not answered, the agent creates an owned handoff task with a summary of the
call and tells the caller exactly what happens next.

**Why.** For a home-services business, the handoff is where an answering service earns or loses trust.
A caller who asks for a person should reach one without being argued with.

**Cost.** A second provider to configure, and LiveKit's own phone numbers could not be used because
they do not support transfer.

## ADR-006: Business rules in configuration, not in the prompt

**Status:** Accepted

**Decision.** Territory, hours, windows, services and dispatch targets live in
[`config/business.yaml`](../config/business.yaml).

**Why.** Onboarding another operator should mean changing configuration, not rewriting the agent.

## ADR-007: Postgres as the booking store

**Status:** Accepted

**Decision.** Slots, bookings and dispatch tasks are stored in Postgres (Supabase).

**Why.** The agent host's filesystem is not a durable place to keep reservations. Postgres provides
transactions, uniqueness constraints for idempotency, and row-level capacity checks.
