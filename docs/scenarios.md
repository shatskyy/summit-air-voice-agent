# Test calls and results

Every phone test gets a row here. Each row names the assignment requirement it checks, what the
caller did, what happened, and the room ID that finds the transcript and any booking or task in the
database. Text tests are listed separately, because they check turn logic and tool routing and say
nothing about audio, latency or interruptions.

## Phone calls

| Date | Room | Requirement | What the caller did | What happened | Result |
|---|---|---|---|---|---|
| 2026-09-23 | `RM_pExY8NcvVat5` | Does it work? | Asked "Do you have anything tomorrow?" on the connection-test build | The call connected, with first reply latency of 0.8 to 1.6 s. Two defects: the agent ignored the availability result ("I'll be here when you're ready"), then said "Let me get a technician scheduled" with no tool behind it. The caller asked when the technician was coming | Connects: pass. Conversation: fail. Both defects are addressed in the prompt and pass in `tests/test_agent.py`; phone retest pending |
| 2026-09-23 | `RM_ncqsUBRDD8Er` | Does it work? | Spoke over the greeting | The greeting started about 4 s after the call arrived, because no worker process was warm. Barge-in cut the greeting correctly, and availability was offered from the tool result | Connects: pass. Greeting delay: one warm process is now configured; phone retest pending |

## Text tests (`uv run pytest -m llm`)

Run on 2026-09-23 to choose the model. Each test ran against both candidates.

| Test | Gemma 4 31B | GPT-4.1 mini |
|---|---|---|
| Offers the windows the tool returned (call 1 replay) | Pass after the prompt fix | Fails the judge: offers windows but doesn't ask whether they work |
| Never claims a booking it hasn't made | Pass | Pass |
| An elderly person without heat is flagged before scheduling | Pass | Pass after the prompt fix. Before it, asked for the caller's name first |
| A price question gets the diagnostic fee and nothing more | Pass | Pass |
| A caller who wants a person gets a callback without argument | Pass | Pass |

Gemma answers the phone. GPT-4.1 mini is the fallback if Gemma errors.
