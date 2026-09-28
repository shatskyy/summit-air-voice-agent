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
