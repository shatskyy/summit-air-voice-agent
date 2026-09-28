# Test calls and results

Every phone test gets a row here. Each row names the assignment requirement it checks, what the
caller did, what happened, and the LiveKit room ID, which finds the call in the worker log and the
LiveKit dashboard. From call 4 on, the log's room name also keys the call's transcript, bookings and
tasks in the database. Text tests are listed separately, because they check turn logic and tool
routing and say nothing about audio, latency or interruptions.

## Phone calls

### Connection-test build (calls 1 to 3)

A four-line prompt, and one fake tool that paused 4 s behind a filler line.

| Date | Room | Model | Requirement | What happened | Result |
|---|---|---|---|---|---|
| 2026-09-23 17:23 | `RM_pExY8NcvVat5` | Gemma 4 31B | Does it work? | Connected, with first replies in 0.8 to 1.6 s. Asked "anything tomorrow?", the agent answered its own filler line ("I'll be here when you're ready") instead of the window the tool returned. Then it said "Let me get a technician scheduled" with no tool behind it, and the caller asked when the technician was coming | Connects: pass. Conversation: fail |
| 2026-09-23 17:27 | not captured | Gemma 4 31B | Does it work? | Same question, same failure: "Just let me know when you're ready" | Fail, 2 of 2 on Gemma |
| 2026-09-23 17:36 | `RM_ncqsUBRDD8Er` | GPT-4.1 mini | Does it work? | The greeting started about 4 s after the call arrived (no warm process), and the caller spoke over it; barge-in yielded. The window from the tool was read out straight away | Connects: pass. Conversation: pass |

Changed since: the fake tool and its filler are gone, since the real tools answer in milliseconds.
The prompt now forbids announcing an action without its tool, and one process is kept warm. Each
fix needs a phone retest on the new build.

### Receptionist build (call 4 on)

The prompt in `src/prompt.md`, the real tools, and Deepgram speech (Nova-3 in, Aura-2 out).

| Date | Room | Model | Requirement | What happened | Result |
|---|---|---|---|---|---|
| 2026-09-24 15:00 | `RM_ZQMnzXZZSRev` | GPT-4.1 mini | Collect name, address and availability; book | A routine booking. The address, spoken as "fourteen Maple Avenue, White Plains, ten six zero one", was read back as "14 Maple Avenue in White Plains, ZIP code 10601", confirmed, and stored exactly. The reference was spoken only after the write returned, and matches booking 1001. Two defects: the caller went from the vulnerability question straight to the address, and the agent never asked their name, so the booking was stored as "Caller". Speech-to-text heard "My AC is broken" as "My IC is broken", and the agent guessed "indoor coil" instead of asking. A final transcript that arrived late also split "Hi." from the problem into two turns. Replies took 1.3 to 3.7 s end to end, slower than on call 3 | Booking and address: pass. Name: fail. Recognition: fail |

| 2026-09-24 17:16 | `RM_V3qC4JuQ9Lbg` | GPT-4.1 mini | Retest of call 4's fixes; out-of-area address | "Hi. My AC is broken." arrived as one correct turn. The caller skipped the name again, and the agent went back for it before booking. The caller gave ZIP 10003 (Manhattan): the agent read it back, offered two windows and took a choice before `book_appointment` refused the ZIP, then filed a callback task correctly. The town the caller gave (Chappaqua) was stored with 10003 and never read back. The agent ended the call in the same turn as the task, and its goodbye ran on into the full opening greeting. Most replies took about 3.1 s end to end | Name, recognition: pass. Out-of-area: late. Hang-up: fail. Latency: fail |
| 2026-09-24 17:53 | `RM_keqYnLzfVw3g` | Gemma 4 31B | Retest of call 5's fixes; routine booking | Replies took 0.9 to 1.8 s end to end, against about 3.1 s on call 5. The caller gave ZIP 10003: `check_address` ran, and the agent read the address back and said Summit Air doesn't serve that area before offering any time. The caller corrected it to 10514. The address was checked again and read back with the town, then the name the caller skipped was asked for and the caller-ID number confirmed. "Tomorrow afternoon" got the window from the tool straight away, which is where Gemma failed on calls 1 and 2. Booking 1002 matches what was said (Friday noon to 4 PM, 48 Severn Lane, Chappaqua, 10514). After "No, that's all", the call ended with one fixed goodbye. Defects: Deepgram's text-to-speech websocket dropped (1011) partway through the booking confirmation; the caller said "Excuse me?" and the agent repeated it. One answer waited the full 2 s cap. ("AC. Not IC." was the caller misreading the test script, not a recognition or voice fault.) | Latency, address, name, model, hang-up: pass. TTS drop: recovered |

Fixes since call 5 (all confirmed on call 6):

- **Latency.** The local turn detector scored complete short answers under its threshold, so every
  such turn waited the full 3 s. The hosted detector is back and the cap is 2 s
  ([ADR-002](decisions.md)).
- **Address.** `check_address` refuses an out-of-area ZIP before any window is offered, and the
  readback includes the town. `book_appointment` refuses an address that was never checked.
- **Hang-up.** The goodbye is a fixed line spoken by code. The model ends the call only after the
  caller says they need nothing else.
- **Model.** Gemma leads, with GPT-4.1 mini as fallback (text tests below).

### Finish-plan build (call 7 on)

Commit `26935d8`: routine callback targets count office hours, a silent line is hung up by
`SilenceWatch`, and Inworld takes over the voice if Deepgram can't be reached. All four calls ran on
Gemma 4 31B, on a Friday night with the office closed.

| Date | Room | Requirement | What happened | Result |
|---|---|---|---|---|
| 2026-09-25 22:47 | `RM_KV2Q3N7whAyV` | Gas smell mid-address: safety before anything else | "My furnace is out", then partway into the address, "smell gas in the kitchen." Emergency task 2002 was written, the on-call page went out, and the safety script started about 0.1 s later, before the model replied. "Yes. It's strong." got the prompt's own safety line a second time, cut off when the caller hung up. Defect: the turn that fired the backstop never reached the conversation history, so the model never saw the gas mention or the address, and the stored transcript is missing the call's most important sentence (the task summary has it) | Backstop, one task, page: pass. Transcript: fail |
| 2026-09-25 22:52 | `RM_LjnLDJwFuvYt` | "No heat, my mother is 78", after hours | Urgent task 2003 filed on the first reply, before the name was asked, and the page reached the on-call phone within a minute. The caller heard the 15-minute target and the after-hours choice ($159 tonight or the $89 diagnostic in the morning). The main reply took 0.9 s end to end. Defects: the task was filed with the name and address "Unknown", which the page repeats; the one-word answer "David." waited the full 2 s cap | Urgent flow and script: pass. Placeholders: fail |
| 2026-09-25 23:06 | `RM_UJWqhsa6mgey` | A request for a person, after hours | Callback task 2004 on the first reply. The caller heard "10 AM Monday", and the stored due time is 10:00 Monday. "No" to anything else ended the call with one fixed goodbye. Name and address were filed as "unknown" again. A second room from the same number, `RM_XvXF8eKfvTnZ`, opened three seconds before this one and played only the greeting: the caller had dialed twice | Office-hours target: pass |
| 2026-09-25 23:14 | `RM_XaXDVs2oT5di` | Silence after "anything else?" | Callback target at 23:14:41, then silence. The caller went away at 23:14:59 and heard "Are you still there?"; about 12 s after that check-in, "I'll let you go. Call us back any time." at 23:15:13. The session closed at 23:15:16 with reason `ROOM_DELETED`, which is the agent hanging up, not the caller | Silence check-in and hang-up: pass |

### Bug-hunt merge (2026-09-27 on)

Commit `04655e2`: the 13 bug-hunt fixes plus the widened gas pattern and the page on a failed write.
Gemma 4 31B, Sunday afternoon with the office closed.

| Date | Room | Requirement | What happened | Result |
|---|---|---|---|---|
| 2026-09-27 16:06 | `RM_6WiUX8kkMmL9` | Rerun of call 7: gas mid-address | Speech-to-text heard "I smell gas in the kitchen" as "I just want gas in the kitchen", so the keyword backstop never fired and the fix under test never ran. The model caught it: it gave the prompt's safety line and filed emergency task 2006 on the next turn (about 15 s after the mention), with a 4:22 PM target. The name was filed blank rather than "Unknown". The opening "My furnace stopped working" was also heard as "My phone stopped working" | Model catch: pass. Backstop: not exercised. Recognition: fail |
| 2026-09-27 16:11 | `RM_abnXb9F2qb4b` | Rerun of call 7, said clearly | "Forty eight Severn Lane, Chapp... hold on, I smell gas in the kitchen." The backstop wrote emergency task 2007 with the caller's words and spoke the safety script. The saved call record keeps the gas sentence in order, and the model saw it (its own emergency call named the gas smell). "Yes, it's strong" got a second `create_dispatch_task`, refused with "Emergency task 2007 already exists", then the prompt's leave-the-house line, which fits a confirmed hazard. Defect: that refusal carries no callback target, so the caller never heard one. "Severn" was heard as "Southern" | Transcript fix, one task, backstop: pass. Callback target: fail |

### Before Phase 2 (2026-09-27, 18:32 to 18:37)

The build before any Phase 2 change: GPT-4.1 mini on OpenAI, the Westchester territory, the worker
in `dev` mode, Sunday evening with the office closed. Each defect here became a Phase 2 item.

| Time | Room | Requirement | What happened | Result |
|---|---|---|---|---|
| 18:32 | `RM_wD6Mg8DGZbEN` | Routine booking | "My furnace stopped working", nobody at risk. The address came through as "487 Lane Chappaqua, 10514", was read back that way, and booking 1003 was written to "487 Lane": speech-to-text dropped the street name and nothing checked for one. The name was heard as "Chatsky". Replies 1.1 to 2.3 s | Booking: pass. Address: fail (led to the street-name check) |
| 18:34 | `RM_GygbaQURKpRt` | "No heat, my mother is 80" | The first reply said "Please leave the house if you smell gas" on a call that never mentioned gas, and the caller said so. The model promised an on-call callback without filing it, so the promise backstop filed urgent task 2008; the model's own attempt later was refused as a duplicate. The target (6:50 PM) was said only at the end, after the name, number and address | Urgent task: pass, by the backstop. Target up front: fail. Gas talk: fail (led to urgent in code) |
| 18:36 | `RM_RfmJnjF4vidc` | none | Greeting only; the caller hung up | Nothing to grade |
| 18:36 | `RM_M5dTYG3RNVcr` | Gas | "I think gas is leaking from my stove": the safety script, and emergency task 2009. "Yes" got a model reply, "Please leave the house immediately", with no callback target, and the call stayed open | Script, one task: pass. Target and close: fail (led to the fixed closing line) |
| 18:36 | `RM_99KwosnzKQvD` | Stray word | Speech-to-text heard "Stop." over the greeting and the model ended the call | Fail (led to end_call waiting for "anything else?" or a goodbye) |
| 18:37 | `RM_kWhhCAm2Mu2d` | Spanish | "Hola." got a half-Spanish reply, callback task 2010, and "I have arranged for someone to call you back who can speak Spanish", a promise nobody can keep | Callback: pass. Spanish-speaker promise: fail (led to the fixed Spanish line) |

### Gate 1 (Stage 1, `7734458`, 2026-09-27 19:43)

The worker under launchd `start` with the hosted turn detector pinned.

| Time | Room | Requirement | What happened | Result |
|---|---|---|---|---|
| 19:43 | `RM_mAnQRRApSbuw` | Routine booking at 48 Bergen Street, Brooklyn, with a warm process | Greeting 1.6 s after dispatch, hosted `turn-detector-v1` confirmed in the log, median reply 1.28 s (max 2.62 s), booking 1004 matches what was said. Defects: a plain "Hello?" got "What problem are you having with your heating or cooling?"; the number was read back as "+1650..." digits; two agent questions were cut off by a short "Alright." and "You said that" | Pass (graded by David) |

### Gate 2 (Stage 2, `eea97b9`, 2026-09-27 20:39 to 20:42)

| Time | Room | Requirement | What happened | Result |
|---|---|---|---|---|
| 20:39 | `RM_NLwxgZ2gkgHy` | "My heat went out and my mother is 80" | Code filed urgent task 2011 on the first turn; the 8:54 PM target was said before the name was asked; the number was read as a phone number | Pass |
| 20:39 | `RM_zDnXV7JaJPUD` | "My furnace won't turn on, and no, I don't smell gas" | No safety script; the at-risk question, then the name. The caller ended the call before a booking | Pass |
| 20:40 | `RM_RHT2GQirTeYY` | Gas, then "Yes" | The safety script, then the fixed closing line with the 8:55 PM target, then code hung up 0.17 s after it played. One emergency task, 2012 | Pass |
| 20:40 | `RM_YmRKBLv5DYUv` | "My AC is leaking, and I need my furnace tune-up" | The caller gave a name and address and said "Bye" before any window, so the model filed callback task 2013 naming both issues and no booking was made. "Bergen" was heard as "Burger", and "home or business?" was asked | Not tested: one visit with both issues is proven in simulation only |
| 20:41 | `RM_vCN4TiNpMid9` | "487 Lane, Brooklyn, 11201" | `check_address` sent it back, and the agent asked for the street name | Pass |

All five graded by David: passed as is.

### Stage 2 calls meant for Gate 3 (2026-09-27, 21:28 to 21:36)

These were meant as the Gate 3 calls, but Stage 3 wasn't deployed yet, so they ran on Stage 2
(`eea97b9`) and are graded as Stage 2 calls. None of them tests a Stage 3 behavior.

| Time | Room | Intended test | What happened on Stage 2 | Result |
|---|---|---|---|---|
| 21:28 | `RM_ECBeuGQNHepp` | Hang up after "my AC stopped working" | Heard as "My c stopped working"; the caller hung up. Stage 2 has no abandoned-call callback, so no task was filed | As expected for Stage 2 |
| 21:30 | `RM_7u3MoMaNngTu` | The same | Heard as "My IT stopped working"; the caller hung up. No task | As expected for Stage 2 |
| 21:30 | `RM_CfrYAV3D5TCg` | "Is this Joe's Pizza?" | "No, this is Summit Air, a heating and cooling company. How can I assist you with heating or cooling today?": a correct answer, but it kept the caller on instead of a one-line exit. Stage 3's fixed wrong-number line came after this call | Partial |
| 21:31 | `RM_CCbLcA43mpsM` | Refuse the address twice | Asked once more with the reason ("to see if we serve your area and to book the visit"), then offered a callback; task 2014, no third ask. Two agent questions were cut off by the caller starting to speak | Pass, on Stage 2's prompt |
| 21:33 | `RM_cGzMnusBBsGv` | "Hola, ¿habla español?" | "Hola. I'm", then the call ended before any reply | Nothing to grade |
| 21:33 | `RM_abBBmR7xmiA9` | "Hello?", then a new install | Opened with "I want a new HVAC" rather than "Hello?", so the neutral opening wasn't tested. A free estimate visit was booked (1005, "replacement estimate") with no fee quoted, and the reference was said as "one oh oh five". Defects: "Who should the technician ask for when they arrive? And can I have your name?", a business question bundled with a second one on a home call; the at-risk question came last, on an estimate; the caller said the number was read wrong | Estimate booking: pass. Wording: fail |

### Gate 3 (Stage 3, `2147b89`): waived

Waived by David on 2026-09-27; the six Gate 3 calls were not made. So these are proven only in
simulation and offline tests, and no phone call has exercised them: the abandoned-call callback, the
wrong-number exit, the refused address as the prompt now handles it, the fixed Spanish line, the
dispatch push, the new-install estimate from a neutral opening, the failure ladder, and the windows
offered with the address. Stage 3 was on the line from 21:38 to the end of Stage 4 with no calls.

**Host.** Closing the lid at 23:13 put the Mac into clamshell sleep within seconds, although the
worker was running under `caffeinate -s` on power. `caffeinate` holds off idle sleep, not a closed
lid, so for the review window the laptop stays open and plugged in.

### Overnight pass (2026-09-27 22:15 to 2026-09-28, branch `overnight`)

No phone calls: the line was dark from 22:03 until submission. Everything here is simulation and
offline tests, so phone calls are still needed to prove it. The pass mined the 28 stored phone calls above and the worker log,
turned the failures into scenarios, and fixed what the scenarios and two fresh-context reviews
found. Twelve scenarios were added (`misheard_opening`, `no_zip`, `split_address`,
`rambling_elderly`, `angry_kid_asthma`, `fillers_self_correction`, `answers_different_question`,
`phone_in_pieces`, `buried_cue_no_smoke`, `defrost_steam`, `cold_no_risk_night`,
`mom_other_address`), each with its pass criterion written before it ran.

| Run | Commit | What | Result |
|---|---|---|---|
| Discovery, 1 run each | `ee62913` (Stage 4 behavior) | The twelve new scenarios plus new_install and member with a stricter check | 13/17: the model filed urgent the turn after "no, it's just me"; no booking without a ZIP; "48 Bergen" booked without "Street"; one bad check |
| Fix 1 | `6b7cf37` | change_window x3, and the booking-path scenarios | 3/3, then 6/7 (no_show night, the baseline-variance scenario, omitted its note) |
| Fix 2 | `bde102e` | The four discovery failures x3, and every scenario the ZIP check touches | cold_no_risk_night 3/3, misheard_opening 3/3, no_zip 3/3, split_address 0/3 (the model skipped the ZIP once it had the borough); 7/8 on the rest, relative_address missing the sister's number |
| Fix 2b | `37eb1f0` | split_address and relative_address x3, then a 16-scenario style subset after the prompt pass | 3/3 and 2/3; 16/16, with home-or-business re-asks 0.27 to 0 a conversation, bundled questions 0.34 to 0.19, words a turn 16.2 to 14.6 |
| Comparison | `0ca16a5` (Stage 4, worktree) | relative_address x3 | 2/3 with the same miss, so the miss is baseline variance |
| Fix 3 | `d4a4df0` | The scenarios the review's fixes touch, 1 run each | 10/11; the 11th was the model confirming a booking it never made, with a made-up reference |
| Fix 4, reverted in fix 5 | `8d1d796`, `c279b81` | A sentence filter on the reply, then, after the third review showed it silencing honest lines, a post-hoc correction note instead | 4/4 and 3/3 on the real streaming path |
| Final | `c279b81` | The full suite: 46 scenarios, 2 runs on safety and adversarial, 1 on core, both clocks where time matters, against the Stage 4 final | 82/82 after re-grading six conversations the old checks had misread (the agent said "our target is 9:15 PM" without "by"; "**Go**t it, rooftop AC" matched the roof pattern); 76/82 before the re-grade, $0.52 |

### Morning calls (2026-09-28 10:01 to 10:08, `00232dd`)

David's three calls on the overnight build, reviewed from `scripts/calls.py` and the worker log.
All three booked correctly; the defects are what the morning commits fix.

| Time | Room | What the caller said | What happened | Defects |
|---|---|---|---|---|
| 10:01 | `call-..._eQGJXmApj2C5` | "Heat's out, my mother's 80", 48 Bergen Street, Brooklyn, no ZIP | Code filed urgent task 2015 on the first turn and the target came in the first reply; booking 1006 today noon to 4 PM with priority | Asked for the ZIP after "I don't know the ZIP"; read-back and windows in one reply; refused for the name, then again for the number; a 3.5 s pause after the address; the task had no address |
| 10:04 | `call-..._KwzEuKkUqMrK` | A new AC install estimate in one 14 s turn, then a move to Tuesday afternoon | Booking 1007 Tuesday morning, moved to Tuesday noon to 4 PM, reference unchanged; no at-risk question on an estimate | Asked for the ZIP again; said a two-sentence reply twice; asked "Which do you want?" and moved the booking in the same reply, then was talked over |
| 10:08 | `call-..._F2VQyNc5ZTBV` | "Furnace won't kick on, 20 degrees out, just me", then "as soon as possible" | Offered routine windows, then the model paged on-call (task 2016) and booked 1009 today with priority | Routine, then urgent on the same facts; asked the borough before the street; "An on-call tech has been paged for urgency" |

Proven on these calls, from the overnight list: one tool call per turn (about a second a tool
turn), the borough without a ZIP, the ZIP the caller never said, the number step, and the
interruptible confirmation (the store held on the 10:04 move). The booking lock and the correction
note didn't fire.

## Text tests (`uv run pytest -m llm`)

Run on 2026-09-23 against the new prompt, with no filler line. Each test ran against both candidates.

| Test | Gemma 4 31B | GPT-4.1 mini |
|---|---|---|
| Offers the windows the tool returned (call 1 replay) | Pass after the prompt fix | Fails the judge: offers windows but doesn't ask whether they work |
| Never claims a booking it hasn't made | Pass | Pass |
| An elderly person without heat is flagged before scheduling | Pass | Pass after the prompt fix. Before it, asked for the caller's name first |
| A price question gets the diagnostic fee and nothing more | Pass | Pass |
| A caller who wants a person gets a callback without argument | Pass | Pass |

**Rerun 2026-09-24, after the prompt and tool fixes:** both models pass all five, including the
call-1 replay. Gemma leads, since its first sentence arrives at 0.33 s median against 0.64 s on call
5's turns. GPT-4.1 mini is its fallback. Call 6 confirmed the choice on the phone.

**Reruns 2026-09-25:** Gemma passes all five on every run. GPT-4.1 mini went 3 of 5, then 5 of 5
in the morning, and 3 of 5 twice that night on `26935d8`. Its two night failures: it didn't call
`check_availability` when asked for tomorrow morning, and it didn't file the urgent task for an
80-year-old without heat. It only answers if Gemma fails mid-call.

**Final run 2026-09-27 22:00, on `c268677` (the Stage 3 behavior), both models, real clock
(Sunday night, office closed):** 6 of 10. GPT-4.1 passed 4 of 5; GPT-4.1 mini 2 of 5. Both models
now run on OpenAI directly, GPT-4.1 mini leading. The failures, reported and not fixed under the
behavior freeze:

- GPT-4.1 mini, "never claims a booking": for "My furnace won't start", with nobody at risk
  mentioned, it said the on-call technician was notified with a 10:07 PM target. On a closed office
  that is over-escalation, and a real finding.
- GPT-4.1 mini, "a caller who wants a person": the callback task and target were right, then it
  asked "What can we help you with in the meantime?". A real wording slip.
- GPT-4.1 mini, "offers the windows": it called `check_availability` and offered windows, then asked
  for the address rather than whether they work. The judge's wording predates checking the address
  before booking.
- GPT-4.1, "elderly without heat is flagged": code filed the urgent task on that turn (ADR-009), so
  the model didn't call the tool the test looks for. A stale expectation.

The simulated calls (`uv run python -m evals`, [ADR-012](decisions.md#adr-012-evals-fixed-clocks-database-checks-a-spend-ledger))
replace these as the main check; their results are in [`evals/REPORT.md`](../evals/REPORT.md).

## Operations

The worker runs under launchd `start` from 2026-09-27 19:22 (`ops/launchd/`), and a watchdog checks
it every 5 minutes by dispatching a health-check job and waiting for its heartbeat row. Drills run
2026-09-27 on `main`:

| Time | Drill | What happened | Result |
|---|---|---|---|
| 19:22 | Switch from `dev` to launchd `start` | No `call-*` room open and no worker process running (the `dev` worker had already exited). `launchctl bootstrap` started it; "registered worker" 3 s later | Pass |
| 19:26 | First health check | Worker wrote the heartbeat 0.9 s after the dispatch; room deleted, no `calls` row written | Pass |
| 19:27 | Kill the worker process | `kill` on the worker's `uv` process at 19:27:03. launchd started a new one and it registered 4 s later. One worker tree afterwards, no orphans | Pass |
| 19:27 | Watchdog, worker stopped | `launchctl bootout`, then the watchdog by hand (`--dry-run`): "line is DOWN: no heartbeat within 15 s", exit 1 | Pass |
| 19:27 | Watchdog, worker started | `launchctl bootstrap`, then the watchdog by hand: "answered in 2.8 s" (the process was still warming), exit 0 | Pass |
| 19:28 | Watchdog under launchd | First scheduled run: up in 0.9 s, on AC, and the day's "all good" sent to the ntfy topic | Pass |

Each health check uses the warm idle process; the worker starts a replacement in about 2 s, so a
real call arriving in that gap waits for it.
