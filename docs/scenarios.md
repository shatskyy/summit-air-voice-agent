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
5's turns. GPT-4.1 mini is its fallback. The next phone call on Gemma confirms the choice.
