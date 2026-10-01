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
Nova-3 transcribes the caller, GPT-4.1 mini runs the conversation, and Deepgram Aura-2 (Arcas, a
male voice) speaks the response, with OpenAI's Onyx voice as its backup. GPT-4.1 backs up the
conversation model.

I tried Gemini 3.8 Flash TTS on September 28 and preferred how it sounded, but on a real call it
took 1.0 to 1.8 s to start each reply against about 0.5 s for Deepgram, and its limit of 10
requests a minute (one per sentence) switched the voice about a minute into the call. I kept Deepgram
and took the Gemini path out of the code.

The model gathers information in whatever order the caller gives it and uses tools to check the
address, find availability, book, or create a dispatch task. The tools validate bookings and write
them to SQLite. Code also checks caller turns for common danger and vulnerability cues, so those
cases don't depend entirely on the model remembering to escalate.

I kept the prompt, business settings and booking logic separate so I could change how the agent
speaks without changing what it can book. Every call leaves a transcript, tool results and a record
of the outcome, which makes it possible to trace a bad conversation back to what happened.

Start with the [prompt](src/prompt.md) and the [receptionist](src/receptionist.py), which holds the
call and its tools. The rules code applies to the caller's words are in [rules.py](src/rules.py),
the lines code speaks in [speech.py](src/speech.py), paging in [paging.py](src/paging.py) and the
checks on the model's output in [guards.py](src/guards.py). The [architecture](docs/architecture.md)
covers the rest, and [decisions](docs/decisions.md) explains the tradeoffs.

I built this with Claude Code as my pair programmer. The product decisions, the tradeoffs and the
review of every change are mine, and the decision log records why each one went the way it did.

## Scope and limits

I focused on the phone conversation and the handoff it produces. The calendar is local rather than
connected to a dispatch system, and requests for a person create a callback task rather than a live
transfer. Spanish callers get a short Spanish response and a callback task. There's no SMS
confirmation or customer-history lookup.

The worker runs on one Mac under launchd, with a watchdog checking it every five minutes. That is
enough for this demo, but a customer deployment needs an always-on host and a real dispatch
integration. Address recognition and interruptions still need work. The urgency rules use keyword
patterns that lean toward over-triggering. The booking confirmation is spoken by code only after
the booking is written, but the model can still claim a booking in its own words without calling
the tool; a check corrects it on the next reply, and cannot take back what the caller already heard.
[Detailed limits](docs/limitations.md) and [phone-call notes](docs/scenarios.md) are in the repo.

## Testing

The September 28 build passed 80 of 82 simulated conversations across 40 scenarios, with two runs
each on the safety and adversarial scenarios and no re-grading; the safety scenarios passed 30 of 30. In
one miss the simulated caller never mentioned the roof hatch, so a commercial booking went in
without an access note, and in the other the model filed two callback tasks for one caller. The
[final-build row in scenarios.md](docs/scenarios.md) describes both. On September 30, 18 targeted
simulated calls checked the changes made since: the end-of-call confirmation and the false-alarm
fix (ADR-024); the [eval report](evals/REPORT.md) has the last of those runs. A simulated caller is another model
reading a brief, so these runs test the turn logic and what gets written, not hearing or
turn-taking.

541 offline tests cover the rules, the tools and the store without any keys or credit, and CI runs
them on every push.

Routine booking, urgent escalation and the gas response have been tested by phone. My last two
calls, on September 30 on the build before this one, covered a false alarm, a booking corrected
after it was confirmed, and a gas report that paged on-call. They found two bugs, which this build
fixes and which so far are checked in simulation rather than by phone. The
[call notes](docs/scenarios.md) record every call, failures included.

## Run locally

You'll need Python 3.11–3.14, uv, a LiveKit Cloud project, and OpenAI and Deepgram keys. Phone calls also
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
