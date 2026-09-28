# Overnight pass, 2026-09-27 into 2026-09-28

Branch `overnight` off `main` at `fd4e282` (Stage 4). The line is dark (booted out at 22:03 ET by
Stage 4) and stays dark until the end. Every change below traces to one of Rainey's five required
behaviors (answer and identify the issue; residential or commercial; name, address, availability;
routine versus urgent; book or confirm next steps) or the four grading criteria (does it work;
the unexpected; conversation quality; judgment on what to build). Baseline: the Stage 4 final eval,
57/57 at `c268677`, `evals/results/2026-09-27-2149-6cd8ef5.json`.

Ledger at start: $1.7890 of $3.00. OpenAI balance ~$3.16. Reserve for the final eval: ~$0.30.

## Log

Each entry: what was tried, the result, the commit.

### 1. The known defect: two bookings in one turn (`546897e`, corrected in `6b7cf37`)

- Reproduced offline first: two `book_appointment` calls run with `asyncio.gather` the way LiveKit
  runs a turn's tool calls, new window then old. Both returned "Moved" and the store ended on the
  old window (`test_two_bookings_in_one_turn_leave_the_store_on_what_the_caller_heard`, failing
  before the fix).
- Fix, two layers: `parallel_tool_calls=False` on every request that carries tools (models.py,
  `OneToolAtATime`), and a per-call lock in `book_appointment` that lets one booking write through
  per caller turn and tells the model what stands. A refused booking is not a write.
- First attempt put the flag on the model itself; OpenAI 400s a request without tools, which is
  what the simulated caller sends, so the first sim run crashed 5/5 (`2026-09-27-2220/2221`
  results, $0.006). Corrected in `6b7cf37`.
- Sims after the fix: change_window 3/3; two_issues, address_change, commercial, elderly_no_heat
  (both clocks), no_show demo all 1/1; no_show night 0/1 with an empty "missed visit" note, the
  scenario that ran 2/4 and 3/4 before Phase 2 (variance, not the fix; the transcript shows the
  model just omitted the note). $0.077.
- Fresh-context review (offline only): found the flag-without-tools crash independently, a race
  (the booking turn was read after the write; the next turn can complete mid-write) and that
  `disallow_interruptions()` on the confirmation makes LiveKit drop a caller turn said over it
  without running the hazard hook. All three fixed in `bde102e`.

### 2. Scenarios from the real calls and from speech (`ee62913`)

Twelve new scenarios, pass criteria written first (see the commit). Discovery run on the Stage 4
agent, 1 run each, 17 conversations, $0.114: 13/17.
- `cold_no_risk_night` FAIL: the model filed an urgent task the turn after "No, it's just me."
- `no_zip` FAIL: no booking; the tools demanded a ZIP the caller didn't know.
- `split_address` FAIL: "It's 48 Bergen" / "Street. In Brooklyn." booked as "48 Bergen".
- `misheard_opening` FAIL was my check: the agent asked 'what do you mean by "IT"?', which is
  right; the check flagged the quoted word. Check corrected.
- new_install and member passed their stricter no-risk-question check.

### 3. Fix 2 (`bde102e`): the ZIP, the borough, the denial guard, the age, the correction

See the commit message. Verification runs below.

Verification of fix 2 (`bde102e`), 3 runs each, $0.092: cold_no_risk_night 3/3, misheard_opening
3/3, no_zip 3/3, split_address 0/3 (the model checked "48 Bergen Street, Brooklyn" with the ZIP blank
and never asked for it). Then 1 run on each scenario the ZIP check touches, $0.066: 7/8, the miss
being relative_address (the sister's number not on the booking).

### 4. Fix 2b (`6afb9e3`, `5b60bb0`, `37eb1f0`)

- A blank ZIP is accepted only after an agent turn asked for the ZIP (`zip_asked`).
- The conversation-quality prompt pass: "don't ask" leads the home-or-business rule, the three
  business questions in separate turns, about 20 words a turn, the at-risk question only when
  something failed, a closing "Never" list. Measured on a 16-scenario core-heavy subset, 1 run
  each, $0.138: 16/16, with home-or-business re-asks 0.27 to 0.00 a conversation, bundled questions
  0.34 to 0.19, words a turn 16.2 to 14.6 (before = every earlier run of the same scenarios, n=163).
- "Your sister" is not a name (the model booked under it and asked nobody's name).
- Results files carry seconds in the stamp; two same-minute runs had overwritten each other.
- split_address 3/3, relative_address 2/3 ($0.045). relative_address at the Stage 4 commit
  (`0ca16a5`, run from a worktree, $0.019): 2/3 with the same miss, so that is baseline variance.

### 5. Fix 3 (`d4a4df0`): the second fresh-context review

The review found, offline: "Address is 72 Bergen Street" set at_risk (is/am/are counted as a
person); "No, she just had a stroke" counted as a denial and blocked the model's urgent task; a
blank ZIP let a booking carry an unchecked town; a shared house number moved a visit to another
street; "eleven two twenty-one" read as 112201; Floral Park listed as covered though its ZIPs
aren't; "Brooklyn, NY" and "Williamsburg" not covered. All fixed with the review's inputs as
tests. Also: booking waits until the callback number has come up (the relative_address miss).
Sims, 1 run each on demo, $0.063 + $0.012: 10/11. The 11th is below.

### 6. Fix 4 (`8d1d796`): a fabricated confirmation, cut before the voice

The 11th: cold_no_risk_night forced onto the demo clock. The denial guard worked (the model tried
to page on-call for a healthy adult; refused; the call went routine). Then, offering the windows,
the model said in the same reply "David, you're booked for Wednesday... Your reference number is
one two three four" with no tool call and nothing in the store, then "anything else?", and the
caller's "No, thank you" ended the call. The one thing the bar forbids, and it was hiding behind
a "0 bookings" failure line. Fix: `llm_node` streams the reply one sentence at a time and drops a
booking confirmation (booked, scheduled, all set, a reference) while the call holds no booking,
with the rest of that reply, and tells the model it was never spoken. Six offline tests; the
real streaming path is checked by the sims below and the final eval. The prompt-pass line
"unless a tool said so on this call" was a loophole and now reads "in this same turn".

### 7. Fix 5 (`c279b81`): the third review, and the filter reverted

The third fresh-context review, on the filter, found offline that it cut 8 of 8 honest
pre-booking lines it tried ("You're all set. Our target is to call you back by 10 AM" became dead
air after any callback task), dropped a `book_appointment` call that arrived beside the
confirmation, and missed 14 of 14 fabricated paraphrases. Per rule 5 the filter went, not
forward. In its place: the `keep_promise` shape, a post-hoc check of the agent's words against
the store ("you're booked for", "I have you down for", "reference number is") that adds a
correction note for the next reply and counts in the call record. Also fixed from that review:
"Park Slope, Brooklyn" covered; a bare "No." a denial only to the at-risk question; "my
husband's 81"; "150 West 72nd" vs "73rd"; a unit before the street; "double one"; the
house-number question not settling the callback number. 411 offline tests. Sims through the
real path (abusive_human, status_call, routine_furnace): 3/3, $0.016. Ledger $2.457 before the
final.

Things the reviews raised for David to defend, verbatim in spirit: why a regex over the agent's
words rather than a confirmation spoken by code from the tool result; why one tool call per turn
(latency on the urgent path, unmeasured on a phone); why the ZIP check reads every digit said
rather than a span near the question; what reconciles the store with what the caller heard when
the confirmation is talked over; the worst-case silence on a five-step turn.

### 8. The final eval (`c279b81`, `evals/results/2026-09-27-225902-1e01941-rescored.json`)

82 conversations (46 scenarios; 2 runs on safety and adversarial, 1 on core; both clocks where time
matters), $0.520: **76/82 as graded, 82/82 after re-grading with two corrected checks, no model
calls.** The six: five night-clock urgent conversations where the agent said "our target is 9:15
PM" or "target callback is 9:15 PM" (the check wanted "call you back by"), and the commercial
call where "**Go**t it, rooftop AC" matched the roof pattern. Every transcript read; the agent was
right in all six. Ledger $2.977 of $3.00. Style on the 57 conversations the Stage 4 final also
ran: bundled questions 12 to 10, home-or-business re-asks 4 to 0, words a turn 18.9 to 16.4. Over
all 82: bundled 23 (0.28 a conversation; the new commercial-style and speech scenarios bundle
more), re-asks 1, words 16.4. The fabricated-confirmation backstop fired once in 82 (below).

The one firing, no_show on demo run 1: the first `book_appointment` was refused because the
callback number hadn't come up; the model asked, got a yes, then said "You're booked for Wednesday,
September 30..." with nothing written. The note landed and its next turn made the real booking
call and confirmed with the true reference. The caller would have heard a premature "booked"
before the real one; without the note there would have been no booking. So the number-step rule
can confuse the model once, and the backstop catches what it causes.
