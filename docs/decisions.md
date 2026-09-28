# Architecture decisions

Each record states what was decided, why, what it costs, and what evidence would reverse it. A
decision that was later reversed stays here, marked superseded or deferred, with the reason it
changed.

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001-livekit-agents-one-always-on-worker) | LiveKit Agents, one always-on worker | Accepted, host revised 2026-09-23 |
| [002](#adr-002-cascaded-speech-pipeline) | Cascaded speech pipeline | Accepted, components revised 2026-09-23 |
| [003](#adr-003-confirm-only-after-a-durable-write) | Confirm only after a durable write | Accepted, mechanism simplified 2026-09-23 |
| [004](#adr-004-emergencies-are-detected-in-code) | Emergencies are detected in code | Accepted |
| [005](#adr-005-live-transfer-deferred) | Live transfer through the Twilio trunk | Deferred 2026-09-23 |
| [006](#adr-006-business-rules-in-configuration-not-in-the-prompt) | Business rules in configuration | Accepted |
| [007](#adr-007-postgres-as-the-booking-store-superseded) | Postgres as the booking store | Superseded by 008 |
| [008](#adr-008-sqlite-on-the-workers-host) | SQLite on the worker's host | Accepted 2026-09-23 |

## ADR-001: LiveKit Agents, one always-on worker

**Status:** Accepted. Host revised 2026-09-23.

**Decision.** Build on LiveKit Agents in Python, with the agent worker running as a single always-on
process connected to LiveKit Cloud. For the review window the process runs on a home machine under
a launchd job that restarts it if it exits and keeps the machine awake while on power.

**Why.** Tools execute inside the agent process, so booking logic, validation and the database sit
next to the conversation with no public HTTP endpoint that can create or change a booking. With a
managed platform such as Retell or Vapi, the same logic would live in a separately hosted webhook
service that needs its own authentication, signature verification and uptime, and the prompt would
live in a dashboard rather than in this repository.

The worker stays up because the free cloud deployment scales to zero and takes 10 to 20 seconds to
start on the first call after idle, and a caller hearing that much silence hangs up. Keeping a cloud
instance warm needs a paid plan, and a hosted server costs money too; the home machine costs nothing
for one week. The worker also keeps one process warm, because on call 3, with none, the greeting
started about 4 s after the call arrived and the caller spoke over it.

**Cost.** More to own than a managed platform: turn-taking, provider choices and process lifecycle
are this repository's responsibility. The home machine is a single point of failure.

**Would reverse it.** A real call that cannot reliably reach the agent after a focused configuration
fix. The SIP path worked on the first call, so this has not happened. In production the worker moves
to managed hosting with a warm instance.

## ADR-002: Cascaded speech pipeline

**Status:** Accepted. Components revised 2026-09-23.

**Decision.** Speech to text, then a language model, then text to speech, rather than a
speech-to-speech model.

**Why.** The riskiest thing this agent does is capture a street address, ZIP code and callback number
correctly over telephone audio. A cascaded pipeline produces a transcript at every turn, supports
keyterm biasing for the territory's town names and HVAC vocabulary, and lets code validate the ZIP
before anything is booked. It also guarantees ordering: the model cannot say "you're booked" before
the booking tool has returned.

**Cost.** Speech-to-speech models currently feel more natural at turn-taking. This build compensates
with a semantic turn detector that waits up to 2 s when a sentence sounds unfinished.

**Revision, 2026-09-24: the hosted turn detector, not the local one.** Replies on call 5 took about
3.1 s end to end. Replaying that call's turns straight to each model put the first full sentence at
0.64 s median for GPT-4.1 mini and 0.33 s for Gemma, with or without the full prompt and the fallback
adapter, so the model was not the cause. The log showed the cause. The local `v1-mini` detector,
adopted after call 4 to spend no inference credit, scored complete short answers ("It's at a home.",
"Yes.") at 0.19 to 0.33, under its 0.36 threshold. So every such turn waited the full 3 s maximum
while the reply sat ready. The hosted v1 detector scored the same kind of turn at 0.6 to 0.99 on
call 3. Its usage counts against a separate monthly request quota, not the inference credit. It is
back, and the maximum wait drops from 3 s to 2 s.

**Components.**

| Layer | Choice | Why |
|---|---|---|
| Speech to text | Deepgram Nova-3, with keyterms | Runs on Deepgram's signup credit (see revision below). On call 4 it captured a dictated address and ZIP exactly, but heard "AC" as "IC", so "AC" is now a keyterm |
| Language model | Gemma 4 31B leads, GPT-4.1 mini as its fallback | On 2026-09-24 both passed all 5 of this repo's model tests, including the replay of the call where Gemma dropped a tool result behind the since-removed filler line. Replaying call 5's turns, Gemma's first sentence arrived at 0.33 s median against 0.64 s for GPT-4.1 mini. The next phone call on Gemma confirms it; if Gemma drops a tool result on the phone again, GPT-4.1 mini leads |
| Text to speech | Deepgram Aura-2 | Same account and credit as speech to text |

**Revision, 2026-09-23.** The first choice was AssemblyAI Universal-3.5 Pro for speech to text (the
lowest word error rate and strongest entity accuracy in independent streaming benchmarks) and Inworld
TTS-2 Flash for text to speech (the fastest time to first audio), both through LiveKit Inference.
After the first test calls, speech turned out to be the biggest cost against LiveKit Inference's free
credit, which covers only about 20 to 30 calls with everything on it. That risked the number going
dead during the review. Moving speech to Deepgram leaves the credit to the model. Both original
choices remain as the fallback when no Deepgram key is set. The original model fallback, GPT-4.1,
was replaced by the two candidates backing each other up.

**Considered: Gemini Live as a speech-to-speech alternative.** It is inexpensive and handles
turn-taking natively. It was not adopted because its tools are non-blocking by default, so it can
keep talking before a booking write returns; the caller's words reach code as a side transcript,
which turns the hazard interrupt into a race; and it offers no keyterm biasing for town names.

**Would reverse it.** Test calls showing a speech-to-speech model matches the cascade on exact address,
ZIP and phone capture without confirming ahead of the write.

## ADR-003: Confirm only after a durable write

**Status:** Accepted. Mechanism simplified 2026-09-23.

**Decision.** The agent may confirm a booking only after the booking tool returns a stored reference.
The tool accepts only a window that was offered on this call, a ZIP code inside the service area,
and a real name. Interruptions are disabled for the duration of the write.

**Why.** An answering service fails most expensively when it tells a caller something that did not
happen. On call 1, before any booking tool existed, the agent said "Let me get a technician
scheduled" with nothing behind it, and the caller asked when the technician was coming. On call 4 the
reference was spoken only after the row committed, and it matched.

**How.** One SQL statement does three jobs. The `bookings` table allows one row per call, and the
insert checks capacity inside the same statement, so a retry returns the existing booking, a caller
who changes windows mid-call moves that same booking, and a full window is refused while leaving any
existing booking untouched.

**Revision, 2026-09-23.** The first design treated a timed-out write as unknown: look the booking
up before retrying. That machinery was cut when the store became a local file (ADR-008). A local
write has no network hop, so it cannot succeed on the far side of a timeout. It comes back
committed or it raises an error. The lookup returns if the store goes remote again.

## ADR-004: Emergencies are detected in code

**Status:** Accepted, revised 2026-09-27 (negation, a held page, a closed emergency)

**Decision.** Every finished caller turn is checked against a fixed pattern list of hazard signals:
gas smell or leak, rotten eggs, carbon monoxide or a CO alarm, smoke, fire or flames, a burning smell
and sparks. A match is ignored when the words just before it, in the same clause, end in a negation
("I don't smell gas", "no smoke", "don't really smell"); "I don't know, I smell gas" still fires. On
the first match in a call, the agent writes an emergency task (a local write that takes
milliseconds), cuts off anything already being said, speaks a fixed safety script that cannot be
interrupted, and skips the model's reply for that turn. The script ends by asking "Is that what's
happening?", and code acts on the answer:

- **The page is held, not the script.** The on-call page waits for the caller's next turn, and goes
  out on the first of: an answer that is anything but a clear no, the caller hanging up, or 15
  seconds. A clear no (a short negative: "no", "nope", "no, it's not", "no, just dusty") cancels
  the page, marks the task `false_alarm` in its `status` column, and tells the model to carry on
  with the normal call. A hazard mentioned after that reopens the task and pages at once.
- **A confirmed emergency is closed in code.** A yes, or a hazard named in the answer, gets a fixed
  line in place of a model reply: "Okay. Get everyone outside now and call 911 from there. Our
  on-call technician will call you at this number by {target}. Please hang up and go." The call
  ends once it has played.

If the task can't be written, the script still plays and the page goes out at once, unheld.

**Why.** Safety guidance must never wait on a model choosing to follow an instruction, and neither
should ending the call: a caller in a house with a gas leak should be walking out, not answering
questions. Holding the page costs at most 15 seconds on a real emergency and saves a 2 AM page on
every dusty first-heat smell. Urgency without a hazard (no heat with someone at risk) is now also
filed by code (`flag_urgent` in `src/receptionist.py`).

**Cost.** A pattern list still over-triggers (a chirping smoke detector matches), and the negation
rule is lexical, so "it's not like there's no gas smell" would be read as a no. The script is
worded to be harmless when it fires, and a clear no now undoes the page. A classifier is the upgrade
if false alarms start to cost calls.

**Evidence.** Offline tests cover the patterns, the negations, the phrases that must not match (a
furnace that "won't fire up"), one emergency task per call, the held page (cancelled, released,
timed out, released on hang-up) and the closing line with the hang-up after playout. Simulated
calls `gas`, `no_gas_negation` and `dusty_smell` check the task, its status, the page and the
hang-up. On the phone the old version (page at once, model reply after the script) was proven on
calls 6 to 9; this version has its first phone test at Gate 2.

## ADR-005: Live transfer, deferred

**Status:** Deferred 2026-09-23. It was accepted on 2026-09-22.

**Original decision.** Transfer callers to a person with a SIP REFER over the Twilio trunk. A cold
transfer waits for the destination to answer, and if nobody does, the caller stays with the agent,
which files a handoff task. That was meant to make "a person picked up" something the agent knows
rather than assumes.

**Why it was deferred.** The only transfer destination this demo has is one person's cell phone, and
during the review window that person is at work or on the review call. An unanswered cell rolls to
carrier voicemail, and the phone network reports voicemail as an answered call. The REFER would
succeed, the caller would hear a personal voicemail greeting, and the agent would believe a person
had picked up. That breaks the claim this repository is built on.

**What ships instead.** A request for a person becomes a callback task with a stated target, and
urgent and emergency calls page the on-call phone. A caller who is told when they will hear back
knows what happens next, and one transferred into a phone nobody answers does not.

**Would reinstate it.** A destination that never goes to voicemail, such as a staffed queue or a
hunt group. The Twilio trunk already carries inbound calls, so REFER needs no new provider.

## ADR-006: Business rules in configuration, not in the prompt

**Status:** Accepted

**Decision.** The service area (counties and ZIP prefixes), office hours, arrival windows, capacity,
fees, callback targets and speech keyterms live in [`config/business.yaml`](../config/business.yaml).
The prompt is rendered from them, with today's date and the caller's number, at the start of every
call.

**Why.** Onboarding another operator should mean changing configuration, not rewriting the agent.

## ADR-007: Postgres as the booking store (superseded)

**Status:** Superseded by ADR-008 on 2026-09-23. It was accepted on 2026-09-22.

**Original decision.** Store slots, bookings and dispatch tasks in Supabase Postgres, because the
agent host's disk was assumed to be ephemeral, and Postgres provides transactions and uniqueness
constraints.

## ADR-008: SQLite on the worker's host

**Status:** Accepted 2026-09-23

**Decision.** Slots, bookings, dispatch tasks and call records live in one SQLite file on the
worker's host, and `src/store.py` is the only file with SQL in it.

**Why.** Postgres was chosen because the host's disk was assumed to be ephemeral. The worker now
runs on a machine with a persistent disk (ADR-001), so Postgres would add an account, credentials and
a network hop for no benefit. Every call runs in its own process, and every write is one statement,
so SQLite's file lock serializes the writes. That is what makes the capacity check in ADR-003 safe.

**Cost.** The schedule lives on one machine and cannot be shared across hosts.

**Would reverse it.** Hosting with an ephemeral disk, or more than one worker host. The booking
statement is standard `INSERT ... ON CONFLICT ... RETURNING`, which Postgres also runs. The capacity
check would need a row lock or a counter with a check constraint, because under Postgres's default
isolation two concurrent inserts can both pass the count.
