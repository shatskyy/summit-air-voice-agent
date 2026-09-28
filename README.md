# Summit Air voice agent

I built this for Summit Air, a fictional HVAC company with 40 technicians across three counties.
It answers inbound calls, works out what the caller needs, and books a visit or records a callback
for dispatch. It handles residential and commercial calls, with priority escalation when someone
vulnerable is without heating or cooling and a separate safety response for gas, smoke or carbon
monoxide.

**Call +1 914-537-8567.** The demo serves Manhattan, Brooklyn and Queens. You can use
14 Maple Street, Brooklyn, 11225 as a test address. Call as you would an HVAC company; there's no
script you need to follow.

Bookings persist in a local SQLite calendar. Summit Air's hours, fees and capacity are demo
assumptions in [business.yaml](config/business.yaml), and notifications go to my phone. No real
technician is dispatched.

## How I built it

Twilio carries the call into LiveKit, which handles the audio session and turn-taking. Deepgram
Nova-3 transcribes the caller, GPT-4.1 mini runs the conversation, and Deepgram Aura-2 speaks the
response. GPT-4.1 and an OpenAI voice provide model and speech fallbacks.

The model gathers information in whatever order the caller gives it and uses tools to check the
address, find availability, book, or create a dispatch task. The tools validate bookings and write
them to SQLite. Code also checks caller turns for common danger and vulnerability cues, so those
cases don't depend entirely on the model remembering to escalate.

I kept the prompt, business settings and booking logic separate so I could change how the agent
speaks without changing what it can book. Every call leaves a transcript, tool results and a record
of the outcome, which makes it possible to trace a bad conversation back to what happened.

Start with the [prompt](src/prompt.md) and [receptionist](src/receptionist.py).
The [architecture](docs/architecture.md) covers the rest, and [decisions](docs/decisions.md)
explains the tradeoffs.

## Scope and limits

I focused on the phone conversation and the handoff it produces. The calendar is local rather than
connected to a dispatch system, and requests for a person create a callback task rather than a live
transfer. Spanish callers get a short Spanish response and a callback task. There's no SMS
confirmation or customer-history lookup.

The worker runs on one Mac under launchd, with a watchdog checking it every five minutes. That is
enough for this demo, but a customer deployment needs an always-on host and a real dispatch
integration. Address recognition and interruptions still need work. The urgency rules use keyword
patterns, and the model can still say something is booked before writing it; a check adds a
correction for its next reply, but cannot take back what the caller already heard.
[Detailed limits](docs/limitations.md) and [phone-call notes](docs/scenarios.md) are in the repo.

## Testing

The latest full text simulation passed 82 of 82 conversations across 46 scenarios, including
changed addresses, vulnerable residents, refused information and off-script requests. Six results
were regraded after correcting the checks. The [report](evals/REPORT.md) links the evidence.
These tests exercise the prompt, tools and database; they don't test hearing or turn-taking.

Routine booking, urgent escalation and the gas response have been tested by phone. The latest
changes still need a fresh phone pass. The [call notes](docs/scenarios.md) also include failures
from earlier model tests, rather than treating the simulation score as proof that every call works.

## Run locally

You'll need Python 3.11–3.14, uv, a LiveKit Cloud project, OpenAI and Deepgram keys. Phone calls also
need a Twilio number routed through an Elastic SIP trunk into LiveKit.

```sh
uv sync --locked
uv run pytest                       # offline tests, no keys needed
cp .env.example .env.local           # fill in the provider keys
uv run python src/agent.py download-files
uv run python src/agent.py start
```

With the inbound trunk configured, create the dispatch rule using
`lk sip dispatch create telephony/dispatch-rule.json`.
[Host setup](ops/launchd/README.md) covers launchd and the watchdog.

```sh
uv run python scripts/calls.py       # recent calls; add a call ID for its transcript and outcome
uv run python -m evals --dry-run     # preview simulated calls and cost without spending
```
