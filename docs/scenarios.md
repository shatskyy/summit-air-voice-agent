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

Fixes since call 4, each pending a phone retest:

- **Recognition.** "AC" and "air conditioner" are keyterms, and the prompt asks what an unfamiliar
  word meant instead of guessing. The end-of-turn wait rose from 0.5 to 0.7 s so a late final
  transcript joins its sentence, at up to 0.2 s on each reply.
- **Name.** `book_appointment` refuses an empty or placeholder name and tells the model to ask for
  it, and the prompt says to go back for anything a caller skipped by answering out of order.

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
