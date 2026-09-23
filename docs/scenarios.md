# Test calls and results

Every phone test gets a row here. Each row names the assignment requirement it checks, what the
caller did, what happened, and the room ID that finds the transcript and any booking or task in the
database. Text tests are listed separately, because they check turn logic and tool routing and say
nothing about audio, latency or interruptions.

## Phone calls

All three ran on the connection-test build (a four-line prompt, and one fake tool that paused 4 s
behind a filler line).

| Date | Room | Model | Requirement | What happened | Result |
|---|---|---|---|---|---|
| 2026-09-23 17:23 | `RM_pExY8NcvVat5` | Gemma 4 31B | Does it work? | Connected, with first replies in 0.8 to 1.6 s. Asked "anything tomorrow?", the agent answered its own filler line ("I'll be here when you're ready") instead of the window the tool returned. Then it said "Let me get a technician scheduled" with no tool behind it, and the caller asked when the technician was coming | Connects: pass. Conversation: fail |
| 2026-09-23 17:27 | not captured | Gemma 4 31B | Does it work? | Same question, same failure: "Just let me know when you're ready" | Fail, 2 of 2 on Gemma |
| 2026-09-23 17:36 | `RM_ncqsUBRDD8Er` | GPT-4.1 mini | Does it work? | The greeting started about 4 s after the call arrived (no warm process), and the caller spoke over it; barge-in yielded. The window from the tool was read out straight away | Connects: pass. Conversation: pass |

Changed since: the fake tool and its filler are gone, since the real tools answer in milliseconds.
The prompt now forbids announcing an action without its tool, and one process is kept warm. Each
fix needs a phone retest on the new build.

## Text tests (`uv run pytest -m llm`)

Run on 2026-09-23 against the new prompt, with no filler line. Each test ran against both candidates.

| Test | Gemma 4 31B | GPT-4.1 mini |
|---|---|---|
| Offers the windows the tool returned (call 1 replay) | Pass after the prompt fix | Fails the judge: offers windows but doesn't ask whether they work |
| Never claims a booking it hasn't made | Pass | Pass |
| An elderly person without heat is flagged before scheduling | Pass | Pass after the prompt fix. Before it, asked for the caller's name first |
| A price question gets the diagnostic fee and nothing more | Pass | Pass |
| A caller who wants a person gets a callback without argument | Pass | Pass |

**The model choice is open.** On the phone, with the old filler, Gemma dropped the tool result twice
and GPT-4.1 mini read it once. In text, on the new build, Gemma passed all five, and GPT-4.1 mini four
of five. Gemma is also faster to its first token (0.24 to 0.37 s against 0.58 s on these calls). The
tiebreaker is one phone call per model on the new build. Whichever wins answers the phone, and the
other is its fallback.
