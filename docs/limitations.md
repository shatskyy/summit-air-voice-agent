# Known limitations

These are the current implementation limits and observations from testing. Phone-call evidence is
in [scenarios.md](scenarios.md); simulated-call results are in [the eval report](../evals/REPORT.md).


- **A booking claimed without a tool call.** The real confirmation is spoken by code from the
  stored row, after the write ([ADR-019](decisions.md#adr-019-the-booking-tool-says-the-confirmation-itself)),
  but the model can still say "you're booked" in its own words without calling the tool. A check
  reads the store after the reply and adds a correction for the next turn; it cannot unsay what
  the caller already heard.
- **One host.** The worker and the database run on one Mac. If it loses power or network, the
  number stops answering; a watchdog pages when the worker stops taking health checks.
- **One OpenAI key.** Both models and the backup voice run on it, so an outage or an empty balance
  takes out all three. The failure ladder then ends the call with a callback.
- **Keyword detectors.** Hazard, urgent and Spanish detection are word lists, tuned to over-trigger
  on danger. The gas negation is lexical ("it's not like there's no gas smell" reads as a no). A
  carbon monoxide detector chirping for a battery still gets the safety script on first mention,
  by design; a smoke detector doing the same does not. A bare age counts only after a pronoun or
  a relation ("I'm 82", "my wife is 79"), never as a word ("eighty-two"). A relative placed
  somewhere else ("she's in Florida") only counts as away when the transcript capitalizes the
  place and the caller makes the home their own, so an ambiguous case still pages.
- **The ZIP check is a heuristic.** A ZIP the caller said is found as a run of digits in what they
  said, with number words read the way ZIPs are spoken ("eleven two twenty-one"); a ZIP hidden
  inside a phone number would pass, and "one double oh two five" would be refused and asked
  again. The towns that book without a ZIP are a list in the configuration.
- **A turn said over an uninterruptible line is lost.** LiveKit drops a caller turn that completes
  while the agent can't be interrupted, so anything said over the safety script, the emergency
  closing line or the Spanish line never reaches the model or the hazard hook. The booking
  confirmation is interruptible for that reason; a barge-in during the few milliseconds of the
  store write can leave a booking the model never confirmed, which a retry finds as "unchanged".
- **Wording.** On a home replacement call the agent once asked "Who should the technician ask for?"
  as if it were a business (call `kaAJD7TK4HWb`); the prompt now forbids it, and the eval counts
  such bundles rather than failing on them.
- **Recognition.** Street names are misheard ("Bergen" as "Burger"), and nothing checks a street
  against a street list or a ZIP against a town. A house number the transcript spells out in words
  ("ninety two second Avenue") has no digits for the street check, so the agent asks for it again. A ZIP the caller never said is refused, but a
  misheard one they did say is not.
- **Pushes carry no caller details**: the outcome, a first name, the ZIP and a lookup, never the
  number, the street or the caller's words. `scripts/calls.py` has the rest. `DISPATCH_NTFY_TOPIC` falls back to `NTFY_TOPIC`, so without it dispatch summaries
  and on-call pages share one topic.
- **Latency.** On the 2026-09-28 10:0x phone calls the median reply was 1.8 to 3.0 s, from the end
  of the caller's speech to the agent's first audio. A turn with no tool call took about 1.2 to 1.6
  s. A turn with a tool call took about 2.5 to 2.9 s, because the model makes one tool call at a time
  ([ADR-014](decisions.md#adr-014-one-tool-call-per-turn)), so each tool adds a model round trip of
  roughly a second. When the turn detector thinks the caller is mid-sentence it waits up to 1.1 s,
  and up to 2.0 s after the agent asks for an address or a number (2.5 s until that morning, when a
  finished address waited the full 2.5 s). Since ADR-022 the turn after the address yes calls
  `check_availability` itself, one more model round trip, about a second.
- **The reply guard catches exact repeats only.** A sentence said twice in one reply is dropped,
  a paraphrase is not. A booking made before the caller answered the agent's question is held;
  the model has to book again on the answer ([ADR-017](decisions.md#adr-017-a-reply-guard-on-the-models-output)).
- **The cold cue is a word list.** No heat is urgent when the caller says freezing, cold in here,
  a cold snap, snow, winter or 45 degrees or below. "It's chilly" or "the house is 50" don't count,
  and the model is the backstop for them ([ADR-018](decisions.md#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home)).
- **Short acknowledgments cut the agent off.** On the phone calls "Alright." and "Okay." over an
  agent question interrupted it mid-sentence (calls `riWFX67`, `NpW9kct`). Only a phone call can
  show whether the adaptive interruption model handles a given caller's backchannels.
- **No same-day visit exists in the demo.** The schedule has two arrival windows a day and no
  same-day dispatch. For an urgent caller with no window left today, the agent can only promise
  an on-call callback about getting someone out and hold tomorrow's window (ADR-021). That rule is
  prompt text, and one simulated call skipped it.
- **Consent is guided, not enforced.** Whether the caller accepted the address and the window is
  judged by the model under the prompt and the reply guard, not by a state machine. The booked
  address must match the checked street and ZIP, but not every unit or town detail.
- **Callback tasks have no idempotency key.** One booking per call is enforced by the database;
  a model that files the same routine callback twice creates two tasks.
- **Nobody is actually on call.** Pages reach one test phone, and callback targets are targets.


### Service-need clarification (October 1)

A generic request to look at a unit previously booked without identifying the HVAC issue.
The prompt and address-tool result now require clarification before scheduling; the booking tool
rejects missing/common generic issue strings. This lexical backstop does not verify all possible
symptoms, supported service categories, or whether an issue was invented by the model. Unknown
phrasing still relies on model judgment. Phone validation of this change is pending.
