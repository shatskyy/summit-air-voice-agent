# Overnight pass, 2026-09-27 into 2026-09-28

Branch `overnight` off `main` at `15fb596` (Stage 4). The line is dark (booted out at 22:03 ET by
Stage 4) and stays dark until the end. Every change below traces to one of Rainey's five required
behaviors (answer and identify the issue; residential or commercial; name, address, availability;
routine versus urgent; book or confirm next steps) or the four grading criteria (does it work;
the unexpected; conversation quality; judgment on what to build). Baseline: the Stage 4 final eval,
57/57 at `6cd8ef5`, `evals/results/2026-09-27-2149-6cd8ef5.json`.

Ledger at start: $1.7890 of $3.00. OpenAI balance ~$3.16. Reserve for the final eval: ~$0.30.

## Log

Each entry: what was tried, the result, the commit.

### 1. The known defect: two bookings in one turn (`6c9f4e5`, corrected in `a8e8380`)

- Reproduced offline first: two `book_appointment` calls run with `asyncio.gather` the way LiveKit
  runs a turn's tool calls, new window then old. Both returned "Moved" and the store ended on the
  old window (`test_two_bookings_in_one_turn_leave_the_store_on_what_the_caller_heard`, failing
  before the fix).
- Fix, two layers: `parallel_tool_calls=False` on every request that carries tools (models.py,
  `OneToolAtATime`), and a per-call lock in `book_appointment` that lets one booking write through
  per caller turn and tells the model what stands. A refused booking is not a write.
- First attempt put the flag on the model itself; OpenAI 400s a request without tools, which is
  what the simulated caller sends, so the first sim run crashed 5/5 (`2026-09-27-2220/2221`
  results, $0.006). Corrected in `a8e8380`.
- Sims after the fix: change_window 3/3; two_issues, address_change, commercial, elderly_no_heat
  (both clocks), no_show demo all 1/1; no_show night 0/1 with an empty "missed visit" note, the
  scenario that ran 2/4 and 3/4 before Phase 2 (variance, not the fix; the transcript shows the
  model just omitted the note). $0.077.
- Fresh-context review (offline only): found the flag-without-tools crash independently, a race
  (the booking turn was read after the write; the next turn can complete mid-write) and that
  `disallow_interruptions()` on the confirmation makes LiveKit drop a caller turn said over it
  without running the hazard hook. All three fixed in `5ef7a37`.

### 2. Scenarios from the real calls and from speech (`9e92cee`)

Twelve new scenarios, pass criteria written first (see the commit). Discovery run on the Stage 4
agent, 1 run each, 17 conversations, $0.114: 13/17.
- `cold_no_risk_night` FAIL: the model filed an urgent task the turn after "No, it's just me."
- `no_zip` FAIL: no booking; the tools demanded a ZIP the caller didn't know.
- `split_address` FAIL: "It's 48 Bergen" / "Street. In Brooklyn." booked as "48 Bergen".
- `misheard_opening` FAIL was my check: the agent asked 'what do you mean by "IT"?', which is
  right; the check flagged the quoted word. Check corrected.
- new_install and member passed their stricter no-risk-question check.

### 3. Fix 2 (`5ef7a37`): the ZIP, the borough, the denial guard, the age, the correction

See the commit message. Verification runs below.

Verification of fix 2 (`5ef7a37`), 3 runs each, $0.092: cold_no_risk_night 3/3, misheard_opening
3/3, no_zip 3/3, split_address 0/3 (the model checked "48 Bergen Street, Brooklyn" with the ZIP blank
and never asked for it). Then 1 run on each scenario the ZIP check touches, $0.066: 7/8, the miss
being relative_address (the sister's number not on the booking).

### 4. Fix 2b (`7ca1456`, `875402d`, `c106fd6`)

- A blank ZIP is accepted only after an agent turn asked for the ZIP (`zip_asked`).
- The conversation-quality prompt pass: "don't ask" leads the home-or-business rule, the three
  business questions in separate turns, about 20 words a turn, the at-risk question only when
  something failed, a closing "Never" list. Measured on a 16-scenario core-heavy subset, 1 run
  each, $0.138: 16/16, with home-or-business re-asks 0.27 to 0.00 a conversation, bundled questions
  0.34 to 0.19, words a turn 16.2 to 14.6 (before = every earlier run of the same scenarios, n=163).
- "Your sister" is not a name (the model booked under it and asked nobody's name).
- Results files carry seconds in the stamp; two same-minute runs had overwritten each other.
- split_address 3/3, relative_address 2/3 ($0.045). relative_address at the Stage 4 commit
  (`e27c29b`, run from a worktree, $0.019): 2/3 with the same miss, so that is baseline variance.

### 5. Fix 3 (`345bd20`): the second fresh-context review

The review found, offline: "Address is 72 Bergen Street" set at_risk (is/am/are counted as a
person); "No, she just had a stroke" counted as a denial and blocked the model's urgent task; a
blank ZIP let a booking carry an unchecked town; a shared house number moved a visit to another
street; "eleven two twenty-one" read as 112201; Floral Park listed as covered though its ZIPs
aren't; "Brooklyn, NY" and "Williamsburg" not covered. All fixed with the review's inputs as
tests. Also: booking waits until the callback number has come up (the relative_address miss).
Sims, 1 run each on demo, $0.063 + $0.012: 10/11. The 11th is below.

### 6. Fix 4 (`b146ca5`): a fabricated confirmation, cut before the voice

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
