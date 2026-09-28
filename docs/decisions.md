# Architecture decisions

Each record states what was decided, why, what it costs, and what evidence would reverse it. A
decision that was later reversed stays here, marked superseded or deferred, with the reason it
changed.

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001-livekit-agents-one-always-on-worker) | LiveKit Agents, one always-on worker | Accepted, host revised 2026-09-23 |
| [002](#adr-002-cascaded-speech-pipeline) | Cascaded speech pipeline | Accepted; components revised 2026-09-23, models 2026-09-27 |
| [003](#adr-003-confirm-only-after-a-durable-write) | Confirm only after a durable write | Accepted; mechanism simplified 2026-09-23, revised 2026-09-28 |
| [004](#adr-004-emergencies-are-detected-in-code) | Emergencies are detected in code | Accepted; revised 2026-09-27 and 2026-09-28 |
| [005](#adr-005-live-transfer-deferred) | Live transfer through the Twilio trunk | Deferred 2026-09-23 |
| [006](#adr-006-business-rules-in-configuration-not-in-the-prompt) | Business rules in configuration | Accepted |
| [007](#adr-007-postgres-as-the-booking-store-superseded) | Postgres as the booking store | Superseded by 008 on 2026-09-23 |
| [008](#adr-008-sqlite-on-the-workers-host) | SQLite on the worker's host | Accepted 2026-09-23 |
| [009](#adr-009-urgent-is-detected-in-code) | Urgent is detected in code | Accepted 2026-09-27; revised 2026-09-28, in part superseded by 018 |
| [010](#adr-010-one-visit-per-call) | One visit per call | Accepted 2026-09-27, revised 2026-09-28 |
| [011](#adr-011-the-failure-ladder-and-the-shared-key) | The failure ladder, and the shared key | Accepted 2026-09-27 |
| [012](#adr-012-evals-fixed-clocks-database-checks-a-spend-ledger) | Evals: fixed clocks, database checks, a spend ledger | Accepted 2026-09-27 |
| [013](#adr-013-hosting-launchd-and-a-watchdog) | Hosting: launchd and a watchdog | Accepted 2026-09-27 |
| [014](#adr-014-one-tool-call-per-turn) | One tool call per turn | Accepted 2026-09-28 |
| [015](#adr-015-a-zip-the-caller-never-said) | A ZIP the caller never said | Accepted 2026-09-28; its ask-for-the-ZIP-first rule and number step removed by 022 |
| [016](#adr-016-a-confirmation-with-no-booking-behind-it-is-caught-and-corrected) | A confirmation with no booking behind it is caught and corrected | Accepted 2026-09-28; its first cut, an output filter, reverted and superseded by the after-the-fact check |
| [017](#adr-017-a-reply-guard-on-the-models-output) | A reply guard on the model's output | Accepted 2026-09-28 |
| [018](#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home) | No heat in the cold is urgent whoever is home | Accepted 2026-09-28 |
| [019](#adr-019-the-booking-tool-says-the-confirmation-itself) | The booking tool says the confirmation itself | Accepted 2026-09-28 |
| [020](#adr-020-a-failure-mid-call-makes-it-a-repair-and-a-no-to-risk-holds-for-the-call) | A failure mid-call makes it a repair; a "no" to risk holds | Accepted 2026-09-28 |
| [021](#adr-021-code-puts-the-callers-details-on-a-paged-task-and-a-yes-confirms-only-a-read-back-heard-to-the-end) | Code puts the caller's details on a paged task | Accepted 2026-09-28; parts 2 and 3 superseded by 022 |
| [022](#adr-022-code-owns-facts-and-actions-the-model-owns-the-conversation) | Code owns facts and actions; the model owns the conversation | Accepted 2026-09-28 |
| [023](#adr-023-split-the-receptionist-module) | Split the receptionist module | Accepted 2026-09-28 |

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

**Status:** Accepted. Components revised 2026-09-23; models revised 2026-09-27.

**Decision.** Speech to text, then a language model, then text to speech, rather than a
speech-to-speech model. Today: Deepgram Nova-3 listens, GPT-4.1 mini answers on OpenAI's API with
GPT-4.1 as its fallback, and Deepgram Aura-2 speaks with OpenAI's gpt-4o-mini-tts as the backup
voice. Nothing runs on LiveKit Inference except the hosted turn detector.

**Why.** The riskiest thing this agent does is capture a street address, ZIP code and callback number
correctly over telephone audio. A cascaded pipeline produces a transcript at every turn, supports
keyterm biasing for the territory's town names and HVAC vocabulary, and lets code validate the ZIP
before anything is booked. It also guarantees ordering: the model cannot say "you're booked" before
the booking tool has returned.

**Cost.** Speech-to-speech models currently feel more natural at turn-taking. This build compensates
with a semantic turn detector that waits when a sentence sounds unfinished: up to 1.1 s, or 2.0 s
after the agent asks for an address or a number.

**Revision, 2026-09-24: the hosted turn detector, not the local one.** Replies on call 5 took about
3.1 s end to end. Replaying that call's turns straight to each model put the first full sentence at
0.64 s median for GPT-4.1 mini and 0.33 s for Gemma, with or without the full prompt and the fallback
adapter, so the model was not the cause. The log showed the cause. The local `v1-mini` detector,
adopted after call 4 to spend no inference credit, scored complete short answers ("It's at a home.",
"Yes.") at 0.19 to 0.33, under its 0.36 threshold. So every such turn waited the full 3 s maximum
while the reply sat ready. The hosted v1 detector scored the same kind of turn at 0.6 to 0.99 on
call 3. Its usage counts against a separate monthly request quota, not the inference credit. It is
back, and the maximum wait drops from 3 s to 2 s.

**Components, as of 2026-09-28.**

| Layer | Choice | Why |
|---|---|---|
| Speech to text | Deepgram Nova-3, with keyterms; no backup | Runs on Deepgram's signup credit (see revision below). On call 4 it captured a dictated address and ZIP exactly, but heard "AC" as "IC", so "AC" is now a keyterm. A missing Deepgram key stops the worker at start (`REQUIRED_KEYS`), and a failure mid-call goes to the failure ladder (ADR-011) |
| Language model | GPT-4.1 mini leads, GPT-4.1 as its fallback, both on OpenAI's API | See the 2026-09-27 revision below. Gemma 4 31B led from 2026-09-24, when replaying call 5's turns put its first sentence at 0.33 s median against 0.64 s for GPT-4.1 mini, until LiveKit Inference's credit ran out |
| Text to speech | Deepgram Aura-2 (`aura-2-arcas-en`), OpenAI gpt-4o-mini-tts (`onyx`) as backup | Same account and credit as speech to text. The backup is also a male voice, so a fallback doesn't switch voices mid-call. A Gemini voice can be put in front (`TTS_PROVIDER=gemini`) but is off: on the 13:06 call on 2026-09-28 it was slow to start and hit Gemini's rate limit, and the voice changed mid-call |

**Revision, 2026-09-23.** The first choice was AssemblyAI Universal-3.5 Pro for speech to text (the
lowest word error rate and strongest entity accuracy in independent streaming benchmarks) and Inworld
TTS-2 Flash for text to speech (the fastest time to first audio), both through LiveKit Inference.
After the first test calls, speech turned out to be the biggest cost against LiveKit Inference's free
credit, which covers only about 20 to 30 calls with everything on it. That risked the number going
dead during the review. Moving speech to Deepgram leaves the credit to the model. Both original
choices stayed as the fallback when no Deepgram key was set, until the 2026-09-27 revision: a
missing Deepgram key now stops the worker at start. The original model fallback, GPT-4.1, was
replaced that day by the two candidates backing each other up, and came back as the fallback in
the 2026-09-27 revision below.

**Revision, 2026-09-27: OpenAI models on OpenAI's API, GPT-4.1 as the fallback.** Gemma 4 31B and
GPT-4.1 mini both ran through LiveKit Inference, which bills every model against one account-wide
credit. The simulated calls spent it on 2026-09-27, and at zero every model on it stops at once,
the fallback included. The models moved to OpenAI directly, on my own API key. Gemma isn't served
there, so GPT-4.1 mini leads: it passed the same model tests, and it is the model the simulated calls
and every phone call from 18:32 on 2026-09-27 ran on. GPT-4.1 is the fallback because it is the stronger model on
the same key and API, with the same tool-calling behavior, so a slow or failed attempt can be
retried without a new provider. An attempt gets 2.5 s before the fallback takes over (ADR-011).
`make_llm` refuses any model that isn't OpenAI's, so nothing can drift back onto the credit. The
backup voice moved from Inworld (on the same credit) to OpenAI's gpt-4o-mini-tts for the same
reason. The cost is ADR-011's shared key: one outage takes out both models and the backup voice.

**Considered: Gemini Live as a speech-to-speech alternative.** It is inexpensive and handles
turn-taking natively. It was not adopted because its tools are non-blocking by default, so it can
keep talking before a booking write returns; the caller's words reach code as a side transcript,
which turns the hazard interrupt into a race; and it offers no keyterm biasing for town names.

**Would reverse it.** Test calls showing a speech-to-speech model matches the cascade on exact address,
ZIP and phone capture without confirming ahead of the write.

## ADR-003: Confirm only after a durable write

**Status:** Accepted. Mechanism simplified 2026-09-23.

**Decision.** The agent may confirm a booking only after the booking tool returns a stored reference.
The tool accepts only a window that was offered on this call, a ZIP code inside the service area
that the caller actually said (or none, for an address in a covered borough), and a real name,
which is not a relation like "your sister". The borough case first required that the caller had
been asked for the ZIP; ADR-022 removed that condition.

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

**Revision, 2026-09-28.** The write used to switch interruptions off for the confirmation. LiveKit
drops a caller turn that completes while the agent can't be interrupted, without adding it to the
context or running `on_user_turn_completed`, which is the hazard backstop: "hold on, I smell gas"
said over the confirmation would have been lost. The confirmation is interruptible again; a caller
who talks over it can ask for the reference and the model restates it. The write itself needs no
protection, since it has already returned before anything is spoken.

## ADR-004: Emergencies are detected in code

**Status:** Accepted. Revised 2026-09-27 (negation, a held page, a closed emergency) and
2026-09-28 (the hold starts when the script ends; detector batteries and hedged gas).

**Decision.** Every finished caller turn is checked against a fixed pattern list of hazard signals:
gas smell or leak, rotten eggs, carbon monoxide or a CO alarm, smoke, fire or flames, a burning smell
and sparks (`hazard_in` in `src/rules.py`). A match is ignored when the words just before it, in the
same clause, end in a negation ("I don't smell gas", "no smoke", "don't really smell"); "I don't
know, I smell gas" still fires. Two cases are read more closely:

- **A detector asking for a battery.** A smoke detector chirping or reporting a low battery, with
  nothing that sounds like a real alarm (going off, won't stop, a new battery already in, a symptom,
  a smell, another hazard), is a routine call from the first mention and gets no script. A carbon
  monoxide detector still gets the script on first mention, because a CO alarm and a CO
  low-battery chirp are easy to confuse and guidance says to treat doubt as an alarm. Once the
  caller has heard the script, "no, it's just the battery" is a clear no and doesn't reopen the
  emergency (`benign_detector`).
- **Gas the caller isn't sure of.** "I don't think it's gas, but there's a weird smell" gets the
  script on first mention, like any gas smell (`gas_suspected`). It is kept out of the check on the
  answer, so a hedged "no, I don't think it's gas" to the script never counts as a yes and never
  gets the 911 close.

On the first match in a call, the agent writes an emergency task (a local write that takes
milliseconds), cuts off anything already being said, speaks a fixed safety script that cannot be
interrupted, and skips the model's reply for that turn. The script ends by asking "Is that what's
happening?", and code acts on the answer:

- **The page is held, not the script.** The on-call page is created with the task but waits for
  the caller's answer (`HeldPage` in `src/paging.py`). Its 15-second countdown starts when the
  script has finished playing (`start_hold_after`), and the whole hold is capped at 45 seconds
  from when the task was filed, so a playout that never reports finishing can't keep a real
  emergency from paging. The page goes out on the first of: an answer that is anything but a
  clear no, the caller hanging up, the countdown, or the cap. A clear no (a short negative: "no",
  "nope", "no, it's not", "no, just dusty") cancels the page, marks the task `false_alarm` in its
  `status` column, and tells the model to carry on with the normal call. A hazard mentioned after
  that reopens the task and pages at once.
- **A confirmed emergency is closed in code.** A yes, or a hazard named in the answer, gets a fixed
  line in place of a model reply: "Okay. Get everyone outside now and call 911 from there. Our
  on-call technician will call you at this number by {target}. Please hang up and go." The call
  ends once it has played.

If the task can't be written, the script still plays and the page goes out at once, unheld.

**Why.** Safety guidance must never wait on a model choosing to follow an instruction, and neither
should ending the call: a caller in a house with a gas leak should be walking out, not answering
questions. On a real emergency the held page waits for the script and the caller's yes, and never
more than 45 seconds; a caller who hangs up releases it at once. In exchange, a dusty first-heat
smell or a detector battery doesn't wake on-call at 2 AM. Urgency without a hazard (no heat with
someone at risk) is also filed by code (`flag_urgent` in `src/receptionist.py`, ADR-009).

**Revision, 2026-09-28 evening: the hold starts when the script ends.** Until this change the
15-second countdown started when the hazard was heard. The script is 42 words and took about
13.4 s to play on the 18:36 call on 2026-09-27 (`RM_M5dTYG3RNVcr`). That left about a second and
a half for an answer, so the hold ran out before any caller could give one, and every "no, it's just dusty" on a real call would still have paged
on-call. The false-alarm cancel only ever worked in simulation. The same change took smoke
detector batteries out of the pattern's reach, let a CO battery answer count as a clear no, and
gave hedged gas the script.

**Cost.** A pattern list still over-triggers, and the negation rule is lexical, so "it's not like
there's no gas smell" would be read as a no. The battery rule is a word list too: a smoke detector
described in words it doesn't know ("it keeps beeping") still gets the script. The script is
worded to be harmless when it fires, and a clear no undoes the page. A classifier is the upgrade
if false alarms start to cost calls.

**Evidence.** Offline tests cover the patterns, the negations, the phrases that must not match (a
furnace that "won't fire up"), the detector-battery and hedged-gas cases, one emergency task per
call, the held page (cancelled, released, timed out, released on hang-up, started only once the
script has played, and sent by the cap when playout never finishes) and the closing line with the
hang-up after playout. Simulated calls `gas`, `no_gas_negation` and `dusty_smell` check the task,
its status, the page and the hang-up. On the phone the old version (page at once, model reply
after the script) was proven on calls 6 to 9. The held page and the closing line were proven on
the calls of 2026-09-27 between 20:39 and 20:42: "I think gas is leaking from my stove", then
"Yes", got the script, the closing line and a hang-up 0.17 s after it played, with one emergency
task (2012); "no, I don't smell gas" got no script. The `false_alarm` path (a clear no after the
script) is proven in simulation and offline tests only, and the 2026-09-28 evening changes have
not been simulated or heard on a phone call.

## ADR-005: Live transfer, deferred

**Status:** Deferred 2026-09-23. It was accepted on 2026-09-22.

**Original decision.** Transfer callers to a person with a SIP REFER over the Twilio trunk. A cold
transfer waits for the destination to answer, and if nobody does, the caller stays with the agent,
which files a handoff task. That was meant to make "a person picked up" something the agent knows
rather than assumes.

**Why it was deferred.** The only transfer destination this demo has is my own cell phone, and
during the review window I am at work or on the review call. An unanswered cell rolls to
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

## ADR-009: Urgent is detected in code

**Status:** Accepted 2026-09-27. Revised 2026-09-28 (below).

**Decision.** Every caller turn updates two flags on the call: `system_down` (no heat, heat out, a
furnace or boiler not working, freezing, no AC, too hot) and `at_risk` (a parent or grandparent,
"elderly" or "senior", an age from 65 up, a baby or infant, oxygen, asthma, COPD, a heart condition, pregnancy, dialysis, and a
plain yes to the agent's at-risk question, and from 2026-09-28 a bare age right after a pronoun or
a relation, "I'm 82", "she's 84", "my wife is 79", never after "is" alone, since "address is 72
Bergen Street" is not a person; "nobody" or "just me" doesn't count). When both are set,
code files the urgent task on that turn, pages on-call, and tells the model the task number and the
callback target to say. Any later booking is forced to priority. A keyword check on what the agent
says (`keep_promise`) stays as the last layer: if the agent tells a caller on-call is coming and no
task exists, code files one.

**Why.** An 80-year-old without heat is the case where a missed step costs the most, and the model
missed it: GPT-4.1 mini asked for a name first, or said "I am paging the on-call technician now"
without calling the tool, in 1 of 4 simulated elderly calls. Filing on the turn the facts arrive
also catches a risk named late, during the address readback.

**Cost.** A word list. It misses phrasing it doesn't know ("she's frail") and the model is still the
backstop for those. A false urgent is cheap next to a missed one.

**Revision, 2026-09-28: the other direction.** On a simulated cold night with nobody at risk, the
model filed an urgent task the turn after the caller said "No, it's just me", and told them on-call
would call. `create_dispatch_task` now refuses an urgent task when the caller's latest turn is a
plain denial of risk, made of nothing but denial words ("no, it's just me, I'm fine"), and the
flags are clear. "No, she just had a stroke" and "no, but my son is sick" are not denials, so the
model keeps its say over what the lists can't see; a review caught a first cut that took any
short "No, ..." as one. `cold_no_risk_night` 3 of 3 after the change, 0 of 1 before.

**Revision, 2026-09-28 morning: no heat in the cold is urgent on its own.** Superseded in part by
[ADR-018](#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home): the denial above still holds
for a cooling failure, or no heat when the caller hasn't said it's cold, but not for no heat in the
cold.

**Revision, 2026-09-28 evening: a yes to the at-risk question means the system is down.** On the
16:40 call (`7gjANeDhy3Md`) the caller said "the AC. And now it's broken", too spread out for
`SYSTEM_DOWN`, then "Yes." to the at-risk question. `at_risk` was set but `system_down` wasn't, so
the backstop never fired, and the model paged 22 s later, after the name, the number and
"Goodbye". The prompt has the agent ask who is at risk only once heating or cooling has failed, so
a yes to that question (`RISK_QUESTION` in the agent's last turn) now also sets `system_down`
(changed at 16:48 that day). A needless page is cheap. Offline tests only.

**Revision, 2026-09-28 evening: who is home, and what counts as down.** Three misreads of the word
lists, fixed together with the ADR-004 revision of the same evening:

- A relative who isn't in the home no longer counts as at risk: one who has passed away, died or
  is "late" ("my late father's house"), one who is away, out of town, on vacation or doesn't live
  here, and one said to be somewhere else ("my mom's in Florida") when the caller also makes the
  home their own ("the AC at my place", "it's just me"). Only a named relation can be away; an
  age, a baby or a medical condition is taken as in the home, and "I'm calling for my mom, she's
  in Queens and her heat is out" still pages (`not_home` in `src/rules.py`).
- "No heat and my mom is 82" was read as a denial of risk, because a "no" shortly before the
  person counted as one. A "no" about the heat, cooling or power, or an "and" or "so" between the
  "no" and the person, now ends the denial.
- Heat that won't turn off, won't shut off, won't stop or is stuck on (`STUCK_ON`) no longer
  counts as the system being down, for `system_down` or for the cold rule in ADR-018. "The heat
  won't turn on, it won't stop clicking" still counts, because the check reads only the clause
  the failure is named in.

Offline tests only; not simulated and not heard on a phone call.

**Evidence.** Simulated `elderly_no_heat`, `infant_no_heat`, `ac_oxygen` and `risk_during_readback`,
on both clocks, 16 of 16 in the final run of the night of 2026-09-27. On the phone, the 20:39
call that evening: "My heat went out and my mother is 80" filed urgent task 2011 on the first
turn, and the target was said before the name.

**Would reverse it.** False urgents that cost on-call real sleep, which would argue for a classifier.

## ADR-010: One visit per call

**Status:** Accepted 2026-09-27

**Decision.** A call holds at most one booking. `book_appointment` returns "Booked", "Moved from X to
Y" or "Updated", so the agent knows which happened. A second booking at a different street or ZIP is
refused ("For a second address, file a callback task"), and two problems at one address are one
visit with both issues listed.

**Why.** A retry or a changed mind should move a visit, not create a second one, and a technician
should arrive knowing about both the leak and the tune-up.

**Cost.** A caller with two properties gets a callback for the second.

**Revision, 2026-09-28.** Two things found in simulation. First, the model once sent two
`book_appointment` calls in one turn, the new window then the old, and both went through, so the
store ended on the old window while the caller heard the new one (`change_window`, 1 of 6 runs in
the simulations of the evening of 2026-09-27). Fixed by ADR-014. Second, "a different street or ZIP" also refused a correction: "it's
forty, not fourteen" after the booking was treated as a second address and sent to a callback while
the technician kept the wrong number. A change in the same ZIP that keeps the house number or the
street is now a correction of this visit (`same_visit`); a different number on a different street
is still a second address.

## ADR-011: The failure ladder, and the shared key

**Status:** Accepted 2026-09-27

**Decision.** Three rungs. Each model attempt gets 2.5 s before GPT-4.1 takes over, with no session
retries on top. Deepgram's voice falls over to OpenAI's. When an error still reaches the session as
unrecoverable, code acts on the first one, once: a fixed line ("I'm sorry, I'm having trouble on my
end. Someone from Summit Air will call you back at this number by {target}."), a callback task with
the transcript so far (urgent if the system is down and someone is at risk), and the hang-up once the
line has played. If the voice is what failed, the task is filed and the call ended. At startup the
worker refuses to run without the OpenAI or Deepgram key, so it can never fall back onto the LiveKit
credit; launchd restarts it and the watchdog pages.

**Why.** LiveKit's defaults retry three times, 2 s apart, and then keep the call open through three
unrecoverable errors, which leaves a caller in silence for up to about 24 s. Silence reads as a dead
line; a stated callback doesn't.

**Cost.** Both models and the backup voice run on one OpenAI key, so an outage or an empty balance
takes all three out at once, and the ladder's line and callback are the only rescue. Speech to text
has no backup: wrapping Deepgram in a fallback adapter drops its word-aligned transcripts on every
normal call, so the backup was left out.

**Evidence.** Offline tests with a model that raises check the line, the task and the hang-up. No
phone call has exercised it.

**Would reverse it.** A second provider's key for the fallback model, which removes the shared-key
limit at the price of a second prompt-behavior profile to test.

## ADR-012: Evals: fixed clocks, database checks, a spend ledger

**Status:** Accepted 2026-09-27

**Decision.** `uv run python -m evals` plays each scenario against the real prompt, tools and store,
with the caller played by GPT-4.1 mini from a brief or by scripted lines. The clock is fixed:
`demo` is Tuesday 2026-09-29 12:35 PM ET with the office open, `night` is Monday 2026-09-28 9 PM
with it closed, and time-sensitive scenarios run on both. Checks are Python functions over the
bookings, the tasks and the transcript. There is no model judge. Every run is priced from one table
and appended to `evals/spend.json`, and a run is refused if it could take the ledger past $3.00 or
the OpenAI balance under $1.50.

**Why.** Callback targets, the after-hours offer and the urgent path all depend on the clock, so an
unpinned run tests whatever time it happens to be. A database check can't be talked into a pass: the
urgent task exists before the booking or it doesn't. The ledger exists because the live phone line
spends from the same key, and a simulation run that emptied it would take the line down.

**Cost.** Text simulations say nothing about audio, recognition, latency or barge-in; only phone
calls do. Scripted callers never drift, but model-played ones vary run to run, so one pass is weak
evidence and single misses get rechecked at both the new and the old commit before anything is
reverted.

## ADR-013: Hosting: launchd and a watchdog

**Status:** Accepted 2026-09-27

**Decision.** The worker runs as `start` under a launchd job that restarts it if it exits, with one
process kept warm and a load threshold high enough that it never refuses a call for CPU. A second
launchd job runs `scripts/watchdog.py` every 5 minutes: it dispatches the agent into a throwaway
`health-...` room, waits up to 15 s for the worker to write a heartbeat row, checks the Mac is on AC
power, and pages only when a state changes, plus one "all good" each morning.

**Why.** `dev` mode reloads on every file save and drops live calls. A process check proves a
process exists; a health-check dispatch proves the worker takes a job and can write to the store.
The default load threshold (0.7) marked the only worker unavailable six times on 2026-09-27 while
simulations ran on the same Mac, and with one worker a refused call is silence.

**Cost.** One home machine is the host: power, network or a closed lid takes the line down, and the
watchdog runs on the same machine, so it can't page if the Mac itself is gone.

**Evidence.** Drills in [scenarios.md](scenarios.md#operations): a killed worker restarted and
registered in 4 s; the watchdog reported down with the worker stopped and up once it was back.

## ADR-014: One tool call per turn

**Status:** Accepted 2026-09-28

**Decision.** Every request that carries tools asks OpenAI for one tool call at a time
(`parallel_tool_calls=False`, set per request in `models.py` because OpenAI rejects it on a request
without tools). On top of that, `book_appointment` lets one write through per caller turn, under a
per-call lock, and tells the model what stands if it tries again. The turn is counted in
`on_user_turn_completed`, so the simulator and the phone count the same way. `max_tool_steps` goes
from LiveKit's default 3 to 5, since a turn that files a task, checks the address, finds windows
and books is now four steps, and past the limit LiveKit has the model answer with its tools off.

**Why.** Every parallel pair the calls produced did harm: on call `KTWmzz` a callback was filed
beside an address check when the caller said "bye" mid-address; on call 2 `end_call` ran beside the
task so the goodbye ran into the greeting; and in simulation two bookings landed at once with the
store on the window the caller wasn't told. Confirming only what persisted (ADR-003) assumes the
model saw one result before it spoke.

**Cost.** A second tool costs one more model round trip, about a second, on the few turns that
need two. The lock refuses a second booking after the model has already seen the first result,
which is the case where the store and the speech could not disagree; it is defence in depth.

**Evidence.** The double write reproduced offline with `asyncio.gather` the way LiveKit runs a
turn's tools, failing before the fix. `change_window` 3 of 3 after it, and every booking-path
scenario (`two_issues`, `address_change`, `commercial`, `elderly_no_heat` on both clocks,
`no_show`) unchanged.

## ADR-015: A ZIP the caller never said

**Status:** Accepted 2026-09-28. The rule to ask for the ZIP first and the number step below were
superseded and removed by
[ADR-022](#adr-022-code-owns-facts-and-actions-the-model-owns-the-conversation) the same day; the
prompt still asks for both.

**Decision.** `check_address` refuses a ZIP that appears nowhere in the caller's turns, with number
words read as digits ("one one two oh one", "eleven two oh one", "ten six zero one"). If the caller
doesn't know the ZIP, and only after the agent has asked for it, an address whose town is a
borough, the city, or a Queens post-office name (`coverage.towns` in the configuration) books with
the ZIP left blank; anywhere else without a ZIP is outside the area. The booking ZIP must be the
checked one, so an invented ZIP can't enter at booking time either.

**Why.** On the 19:43 call on 2026-09-27 the caller said "I forgot" and the model checked and booked 11201 on
its own, then read it back as if the caller had said it. The number happened to be right. The
alternative, refusing to book without a ZIP, sends a Brooklyn caller who doesn't know their ZIP to a
callback, which is the form-reading-robot outcome the rubric grades against.

**Cost.** The number-word reading is a heuristic: pairs and hundreds combine ("eleven two
twenty-one" is 11221), but "one double oh two five" is refused and asked again, and a ZIP hidden
inside a phone number passes. A false refusal costs one more question; a false pass is the old
behavior. The towns list is configuration: the boroughs, the Queens post-office names inside the
ZIP prefixes (not Floral Park or Bellerose, which straddle the Nassau line), and the neighborhoods
a caller gives as the town, with a trailing ", NY" dropped. The booked house number and street
must be the checked ones, so a blank ZIP can't carry a town the check never saw.

**Same date, the number step.** `book_appointment` also refuses until the callback number has
come up on the call, asked by the agent or volunteered by the caller, after the model booked a
sister's apartment on the caller's own number without asking which number reaches someone there
(`relative_address`, 2 of 3 both on the build from before that night's changes and on the changed
build). Removed by ADR-022: booking now needs only a real number, from the caller or caller ID.

**Evidence.** `no_zip` 3 of 3 (booked at 48 Bergen Street, Brooklyn, ZIP blank, no ZIP spoken by the
agent), `split_address` after the rule to ask for the ZIP first, and `address_change`, `asr_street`, `blocked_id`,
`commercial`, `routine_furnace` unchanged with the check in place.

## ADR-016: A confirmation with no booking behind it is caught and corrected

**Status:** Accepted 2026-09-28. The first cut, a filter on the reply before it reached the
voice, was reverted the same night and is superseded by the after-the-fact check below.

**Decision.** After each reply, code checks the agent's own words against the store: if it said
"you're booked for", "I have you down for", "your reference number is" or "your confirmation
number is" and the call holds no booking, the model gets a system note saying nothing is booked,
to say so plainly in its next reply and to book with the tool. The call record counts these. It
is the `keep_promise` shape (ADR-009): after the fact, precise about the phrase, never silencing.

**Why.** On a simulated cold-night call the model asked "Which works?" and, in the same reply,
said "Sam, you're booked for Wednesday... Your reference number is one two three four", with no
tool call and nothing in the store. It then asked "anything else?", so the end-call guard let the
call end. Confirming only what persisted (ADR-003) had a tool-side half, the write before the
reference; this is the speech-side half. The prompt already forbade it and the model did it
anyway, once in about 450 simulated conversations.

**What was tried and reverted the same night.** A first cut filtered the reply sentence by
sentence in `llm_node` before it reached the voice. A review showed, offline, that
it silenced honest lines ("You're all set. Our target is to call you back by 10 AM", after any
callback task, became dead air), dropped a `book_appointment` call that arrived with the
confirmation, and still missed most paraphrases. Silence and a lost booking are worse than one
false sentence followed by a correction, so the filter went and the note stayed.

**Cost.** The caller hears the false sentence once, then the correction. A fabricated "moved" is
not caught, since the store does hold a booking. The phrase list is short on purpose: "you're
scheduled for" is an honest line on a status call, so it is not in it.

**Evidence.** Offline tests with the honest lines that must pass and the phrases that must be
caught; the final eval, where every conversation runs the check.

**Would reverse it.** A model that never does this, or a confirmation spoken by code from the
tool result, the way the greeting and the emergency closing line already are.

## ADR-017: A reply guard on the model's output

**Status:** Accepted 2026-09-28. Not yet run in a simulation, so its only live runs are the phone
calls made after it was deployed.

**Decision.** Every model reply passes through `guard_reply` in `llm_node`, before the voice, the
transcript and the tool calls see it. It does two things. A sentence identical to one already said
in the same reply is dropped. A `book_appointment` call made after the agent asked the caller
something they haven't answered yet, earlier in the same reply or in an earlier step of the same
turn, is held and never runs. The first sentence always streams straight through, no sentence that
isn't an exact repeat is dropped, no other tool call is touched, and if the guard throws, the rest
of the reply goes through as the model wrote it. The call record counts `repeats_dropped` and
`bookings_held`.

**Why.** Two model mistakes on the 10:04 phone call. The agent said "We don't need the ZIP for
Brooklyn. You want an estimate to install a new AC, right?" twice in one reply. Then it asked
"Which do you want?" and moved the booking in the same reply, so the caller's answer talked over
"Moved to Tuesday," and the confirmation had to start again. The prompt already forbade both.

**How it differs from the filter reverted in ADR-016.** That filter cut sentences by meaning (a
confirmation with nothing booked), which silenced honest lines, and it dropped the booking call
that arrived beside them. This one cuts only exact repeats, which no honest reply contains, and
holds only a booking the prompt forbids, made before the caller answered.

**Cost.** Sentences after the first are held to their end, behind the first sentence's audio, so
nothing is added to the first audio. A paraphrased repeat is not caught. A held booking relies on
the model booking again on the caller's answer, which it does whenever the caller picks a window.

**Evidence.** 19 offline tests from the 10:04 transcript, including the lines that must pass whole
("Oh no, in this cold? What's the address there?", the booking confirmation).

**Would reverse it.** A phone call where a held booking leaves a caller who has already picked a
window without one.

## ADR-018: No heat in the cold is urgent whoever is home

**Status:** Accepted 2026-09-28. Not yet run in a simulation.

**Decision.** Code files the urgent task and pages on-call the turn the caller has said the heat is
down (`HEAT_DOWN`: no heat, a furnace, boiler, heater or heat pump out, off, dead or won't come on,
never a water heater) and that it is cold (`COLD`: freezing, cold in here, a cold snap, snow,
winter, 45 degrees or below), with reason "no heat in cold weather". Cooling still needs someone at
risk. The denial guard in ADR-009 no longer turns this case routine, and the prompt says the same.

**Why.** The assignment brief lists "no heat in winter" as urgent on its own, beside "no AC with a
medical condition or elderly resident". On the 10:08 call, "My furnace won't kick on. It's 20 degrees out.
It's just me." ran routine, then the model paged on-call when the caller said "as soon as
possible", so the agent did both on one call. I chose to follow the brief and accept the extra
pages.

**Cost.** More pages: every no-heat call in the cold goes to on-call, a healthy adult alone
included. The cold cue is a word list, so "it's chilly" or "the house is 50" don't count.

**Evidence.** Offline tests: the 10:08 opener files urgent on the first turn; an iced coil, an AC
blowing cold, a water heater and no heat at 65 degrees stay routine. The `cold_no_risk_night`
scenario now expects one urgent task.

**Would reverse it.** On-call load from healthy callers that Summit Air says it doesn't want.

## ADR-019: The booking tool says the confirmation itself

**Status:** Accepted 2026-09-28. Heard on the 14:28 phone call (`GKp6z86JAVAG`), which booked 1013
with the confirmation said by code.

**Decision.** When `book_appointment` writes a booking, code builds the confirmation from the
stored row (`confirmation_line`: first name, day, window, address, the reference digit by digit,
then "Is there anything else I can help with?") and says it with `session.say`. The tool result
tells the model the caller has already heard it, and a `function_tools_executed` handler cancels
the model's reply to that result. A refused booking (`ToolError`) sets nothing, so the model still
answers a refusal. The line stays interruptible, for the reason in ADR-003: LiveKit
drops a caller turn said over an uninterruptible line.

**Why.** Latency and truth. On the 1:06 PM call on September 28 the booking turn took 5.9 s, against a
median of 3.0 s, because the model made one request to call the tool and a second to word the
confirmation. Cancelling the second request removes about 1.5 to 2 s from that turn. It also
means "you're booked" can only be said after the write returns, which ADR-016 could only catch
after the fact.

**Cost.** The confirmation wording is fixed rather than varied, and it can't fold in a
caller-specific remark (a parking note, a second issue) in the same breath; the model can still
add that on the caller's next turn. ADR-016's check stays as the backstop for a model that says
"you're booked" without calling the tool.

**Evidence.** A text session with a scripted model that books on its first request and would say
"SECOND ROUND" on a later one: the model gets one request, and the last thing said is the
confirmation. Rule tests cover the booked, moved and updated lines, a refused booking saying
nothing, and "No, that's all" after the line closing the call.

**Would reverse it.** A caller talking over the confirmation and the model then repeating it, or
callers finding the fixed wording stiff.

## ADR-020: A failure mid-call makes it a repair, and a "no" to risk holds for the call

**Status:** Accepted 2026-09-28. Offline tests only; not yet heard on a phone call.

**Decision.** Three changes from the 2:28 PM call, where a caller booked a free AC install
estimate for Friday, then said "my AC just completely broke", answered "No" to the at-risk
question, and pushed with "it needs urgent fixing":

1. If the caller says the system has failed after windows were offered, code tells the model the
   visit is now a repair and holds `book_appointment` until `check_availability` runs again, so
   the caller hears the earliest window before anything is booked.
2. A denial of risk now holds for the rest of the call (`Call.risk_denied`), not only on the turn
   right after it. The urgent-task guard refuses on it until the caller names someone at risk, or
   says the heat is out in the cold (ADR-018). If the model still tells the caller on-call has
   it, the promise backstop files an office callback instead of paging.
3. `check_availability` takes an optional `latest_date`. When the caller names several days, it
   returns the first open window on each, up to three days, offered in one reply. On the call,
   "Wednesday, Thursday, or Friday" took three turns.

**Why.** The call ended with a Friday estimate at routine priority and an urgent on-call page
for the same healthy adult, two outcomes that contradict each other and the brief, where a broken
AC with nobody at risk is routine.

**Cost.** A caller who says "no" and then describes risk in words the at-risk patterns miss stays
routine; the model can no longer override that with its own judgment.

**Would reverse it.** A missed at-risk caller after an early "no".

## ADR-021: Code puts the caller's details on a paged task, and a yes confirms only a read-back heard to the end

**Status:** Accepted 2026-09-28. Parts 2 and 3, and the name taken from speech in part 1, were
superseded and removed the same day by ADR-022. Offline tests only; not yet heard on a phone
call. Part 4 is a prompt change and has not been simulated.

**Decision.** Four changes from the 3:51 PM call (`KSJ5YTzz9uHA`), an 80-year-old without heat:

1. When `check_address` passes an in-area address and the call has a paged task (urgent, or an
   emergency not called off), code writes the address onto the task and pushes on-call once,
   "#N: address added", with the first name and ZIP only. A name the caller gives for themselves
   ("my name is", "this is") is written onto the task the same turn. Both are kept on the call,
   so a task filed later starts with them. A booking still replaces these code-made copies.
2. The windows found with the address reach the model only on a yes whose previous agent message
   was the read-back, said to the end (not `interrupted`). When the caller talked over the
   read-back, a note tells the model to answer them and read the address back again, alone.
3. The closing check also accepts any short answer made only of closing words ("It's all good.
   K.", "Okay, thank you so much") with at least one marker (no, nothing, good, set, fine,
   thanks, bye). "Yes", "but", "actually", "also" or a question keeps the call open.
4. Urgent with the office open and no window today: the prompt has the agent say so, say on-call
   will call by the target about getting someone out today, name the after-hours fee, and hold
   the earliest window as a backup, without ever saying a technician is coming today.

**Why.** The page went out on the second turn by design (ADR-009), before any details, and the
prompt's instruction to update the task later was not followed, so on-call had a reason and a
caller ID and nothing else while the agent told the caller "I have your details". The caller's
"Yes." answered the phone question, but any yes released the windows. The close check was a list
of whole phrases, and this was its third miss. The caller asked for today and was offered Tuesday
twice, then nothing.

**Cost.** A second push on urgent calls. A name heard wrong by speech-to-text lands on the task
until a booking or the model corrects it. A read-back the model phrases without the house number
and street words doesn't count, so the model finds windows itself with `check_availability`, one
extra tool turn.

**Would reverse it.** A wrong name or address on a task that the caller never gave, or a real
request after "anything else" taken as a goodbye.

## ADR-022: Code owns facts and actions; the model owns the conversation

**Status:** Accepted 2026-09-28. Offline tests plus two simulated calls. It was live on the
16:40 and 16:45 phone calls, but neither reached the address read-back, a booking or `end_call`.

**Decision.** I decided that code reads the caller's words only for safety: the gas and carbon
monoxide script and the urgent page backstop (ADR-004, ADR-009, ADR-018). Everything else code
knows comes from the arguments the model passes to its tools, which code checks against the store. Removed:

1. The closing check on `end_call`. It now refuses only on the caller's first turn (the "Stop."
   over the greeting); after that, when to hang up is the model's call.
2. The windows found with the address and released on a yes (ADR-021 part 2).
   `check_address` tells the model to read back only, then call `check_availability` after the
   yes.
3. The rule that the callback number had to come up before booking. Booking still needs a real
   number, from the caller or caller ID.
4. The rule that the ZIP had to be asked before a borough-only address was checked.
5. The name taken from the caller's words (ADR-021 part 1). The checked address still goes onto
   a paged task, since it comes from a tool's arguments.

Kept, because each is a fact, an action or a business rule rather than a conversational choice:
the safety script, the urgent backstop and the "no" to risk holding for the call (ADR-020), every
booking check (offered window, checked address, one visit, a ZIP the caller never said), the
confirmation spoken from the stored row, the checks on "you're booked" and "on-call has it" with
nothing behind them, and the reply guard.

**Why.** On the 3:51 PM call two of four defects were code overriding a model that was right:
the closing word list refused "It's all good. K." and the agent asked "anything else" twice, and a
note released on the wrong yes told the model the address was confirmed when it wasn't. Each
fix added another word list, which fails on the next phrasing. A model slip on these is
awkward; a check that misfires on them is wrong.

**Cost.** Failures the removed checks had caught can come back: the model may offer times with
the read-back, skip the number step, or ask for a ZIP a borough didn't need. The prompt still
says each. The turn after the address yes takes one more tool round, about a second.

**Evidence.** 466 offline tests. Simulated `elderly_no_heat` and `relative_address` (demo clock,
$0.0149): both passed, with the read-back alone, `check_availability` after the yes, the number
asked for a relative's home, and `end_call` on the first "No, that's all". On the urgent call the
model offered Wednesday without the same-day wording ADR-021 part 4 asks for.

**Would reverse it.** Repeated phone calls where the model bundles times with the read-back, books
without a number, or hangs up on a caller who still needed something.

## ADR-023: Split the receptionist module

**Status:** Accepted 2026-09-28

**Decision.** `src/receptionist.py` had grown to 1,960 lines. I split it move-only, with no change
in behavior, into four modules by what the code does:

| Module | What it holds |
|---|---|
| `src/rules.py` | What code reads in the caller's words: hazards, who is at risk, whether the system is down, and the parsing of names, streets and spoken numbers |
| `src/speech.py` | What code says: the fixed lines (the greeting, the safety script, the closing lines) and how numbers, clock times, windows and callback targets are said |
| `src/paging.py` | Pages to the on-call phone and pushes to dispatch through ntfy, including the held emergency page |
| `src/guards.py` | The checks on the model's output: the reply guard (ADR-017) and the after-the-fact checks (ADR-009, ADR-016) |

`receptionist.py` keeps the call state, the code that depends on the clock, and the agent with its
tools, about 1,200 lines. It still exposes the names it used to define, so imports from elsewhere
keep working.

**Why.** One 1,960-line file mixed pure text rules with the agent's lifecycle, which made both
harder to review and to test. The dividing line is the clock and the configuration: everything
that calls `now()` or reads the business configuration stayed in `receptionist.py`, so the
simulator's fixed clocks (ADR-012) and the tests' clock patches still apply without change, and
`rules.py` is a set of pure functions of the text and the call's flags.

**Cost.** A reader has five files to open instead of one, and a test that patches a function must
patch it where it now lives.

**Evidence.** The tests are unchanged except their patch targets for paging
(`monkeypatch.setattr(paging, "page_on_call", ...)` in place of `receptionist`).
