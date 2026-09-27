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

**Host.** Closing the lid at 23:13 put the Mac into clamshell sleep within seconds, although the
worker was running under `caffeinate -s` on power. `caffeinate` holds off idle sleep, not a closed
lid, so for the review window the laptop stays open and plugged in.

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
