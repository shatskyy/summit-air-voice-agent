# Architecture decisions

Each record states what was decided, why, what it costs, and what evidence would reverse it. A
decision that was later reversed stays here, marked superseded or deferred, with the reason it
changed.

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001-livekit-agents-one-always-on-worker) | LiveKit Agents, one always-on worker | Accepted, host revised 2026-09-23 |
| [002](#adr-002-cascaded-speech-pipeline) | Cascaded speech pipeline | Accepted, models revised 2026-09-27 |
| [003](#adr-003-confirm-only-after-a-durable-write) | Confirm only after a durable write | Accepted, mechanism simplified 2026-09-23 |
| [004](#adr-004-emergencies-are-detected-in-code) | Emergencies are detected in code | Accepted, revised 2026-09-27 |
| [005](#adr-005-live-transfer-deferred) | Live transfer through the Twilio trunk | Deferred 2026-09-23 |
| [006](#adr-006-business-rules-in-configuration-not-in-the-prompt) | Business rules in configuration | Accepted |
| [007](#adr-007-postgres-as-the-booking-store-superseded) | Postgres as the booking store | Superseded by 008 |
| [008](#adr-008-sqlite-on-the-workers-host) | SQLite on the worker's host | Accepted 2026-09-23 |
| [009](#adr-009-urgent-is-detected-in-code) | Urgent is detected in code | Accepted 2026-09-27 |
| [010](#adr-010-one-visit-per-call) | One visit per call | Accepted 2026-09-27 |
| [011](#adr-011-the-failure-ladder-and-the-shared-key) | The failure ladder, and the shared key | Accepted 2026-09-27 |
| [012](#adr-012-evals-fixed-clocks-database-checks-a-spend-ledger) | Evals: fixed clocks, database checks, a spend ledger | Accepted 2026-09-27 |
| [013](#adr-013-hosting-launchd-and-a-watchdog) | Hosting: launchd and a watchdog | Accepted 2026-09-27 |

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

**Revision, 2026-09-27: OpenAI models on OpenAI's API, GPT-4.1 as the fallback.** Gemma 4 31B and
GPT-4.1 mini both ran through LiveKit Inference, which bills every model against one account-wide
credit. The simulated calls spent it on 2026-09-27, and at zero every model on it stops at once,
the fallback included. The models moved to OpenAI directly on the builder's key. Gemma isn't served
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
that the caller actually said (or none, for an address in a covered borough, once the caller has
been asked for one), and a real name, which is not a relation like "your sister".

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
calls 6 to 9. This version was proven on the Gate 2 calls of 2026-09-27: "I think gas is leaking
from my stove", then "Yes", got the script, the closing line and a hang-up 0.17 s after it played,
with one emergency task (2012); "no, I don't smell gas" got no script. The `false_alarm` path (a
clear no after the script) is proven in simulation only.

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

## ADR-009: Urgent is detected in code

**Status:** Accepted 2026-09-27

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
model keeps its say over what the lists can't see; a fresh-context review caught a first cut that
took any short "No, ..." as one. `cold_no_risk_night` 3 of 3 after the change, 0 of 1 before.

**Revision, 2026-09-28 morning: no heat in the cold is urgent on its own.** Superseded in part by
[ADR-018](#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home): the denial above still holds
for a cooling failure, or no heat when the caller hasn't said it's cold, but not for no heat in the
cold.

**Evidence.** Simulated `elderly_no_heat`, `infant_no_heat`, `ac_oxygen` and `risk_during_readback`,
on both clocks, 16 of 16 in the final run. On the phone, Gate 2 call 1: "My heat went out and my
mother is 80" filed urgent task 2011 on the first turn, and the target was said before the name.

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
store ended on the old window while the caller heard the new one (`change_window`, 1 of 6 runs at
Stage 3). Fixed by ADR-014. Second, "a different street or ZIP" also refused a correction: "it's
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

**Status:** Accepted 2026-09-28

**Decision.** `check_address` refuses a ZIP that appears nowhere in the caller's turns, with number
words read as digits ("one one two oh one", "eleven two oh one", "ten six zero one"). If the caller
doesn't know the ZIP, and only after the agent has asked for it, an address whose town is a
borough, the city, or a Queens post-office name (`coverage.towns` in the configuration) books with
the ZIP left blank; anywhere else without a ZIP is outside the area. The booking ZIP must be the
checked one, so an invented ZIP can't enter at booking time either.

**Why.** On the Gate 1 call the caller said "I forgot" and the model checked and booked 11201 on
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
(`relative_address`, 2 of 3 at the Stage 4 commit and at the overnight commits alike).

**Evidence.** `no_zip` 3 of 3 (booked at 48 Bergen Street, Brooklyn, ZIP blank, no ZIP spoken by the
agent), `split_address` after the ask-first rule, and `address_change`, `asr_street`, `blocked_id`,
`commercial`, `routine_furnace` unchanged with the check in place.

## ADR-016: A confirmation with no booking behind it is caught and corrected

**Status:** Accepted 2026-09-28

**Decision.** After each reply, code checks the agent's own words against the store: if it said
"you're booked for", "I have you down for", "your reference number is" or "your confirmation
number is" and the call holds no booking, the model gets a system note saying nothing is booked,
to say so plainly in its next reply and to book with the tool. The call record counts these. It
is the `keep_promise` shape (ADR-009): after the fact, precise about the phrase, never silencing.

**Why.** On a simulated cold-night call the model asked "Which works?" and, in the same reply,
said "David, you're booked for Wednesday... Your reference number is one two three four", with no
tool call and nothing in the store. It then asked "anything else?", so the end-call guard let the
call end. Confirming only what persisted (ADR-003) had a tool-side half, the write before the
reference; this is the speech-side half. The prompt already forbade it and the model did it
anyway, once in about 450 simulated conversations.

**What was tried and reverted the same night.** A first cut filtered the reply sentence by
sentence in `llm_node` before it reached the voice. A fresh-context review showed, offline, that
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

**Status:** Accepted 2026-09-28. Not yet run in a simulation (rule 9); the first live runs are the
phone calls after the deploy.

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

**Status:** Accepted 2026-09-28 (David's call). Not yet run in a simulation (rule 9).

**Decision.** Code files the urgent task and pages on-call the turn the caller has said the heat is
down (`HEAT_DOWN`: no heat, a furnace, boiler, heater or heat pump out, off, dead or won't come on,
never a water heater) and that it is cold (`COLD`: freezing, cold in here, a cold snap, snow,
winter, 45 degrees or below), with reason "no heat in cold weather". Cooling still needs someone at
risk. The denial guard in ADR-009 no longer turns this case routine, and the prompt says the same.

**Why.** Rainey's brief lists "no heat in winter" as urgent on its own, beside "no AC with a medical
condition or elderly resident". On the 10:08 call, "My furnace won't kick on. It's 20 degrees out.
It's just me." ran routine, then the model paged on-call when the caller said "as soon as
possible", so the agent did both on one call.

**Cost.** More pages: every no-heat call in the cold goes to on-call, a healthy adult alone
included. The cold cue is a word list, so "it's chilly" or "the house is 50" don't count.

**Evidence.** Offline tests: the 10:08 opener files urgent on the first turn; an iced coil, an AC
blowing cold, a water heater and no heat at 65 degrees stay routine. The `cold_no_risk_night`
scenario now expects one urgent task.

**Would reverse it.** On-call load from healthy callers that Summit Air says it doesn't want.

## ADR-019: The booking tool says the confirmation itself

**Status:** Accepted 2026-09-28. Offline tests only; not yet heard on a phone call.

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
