# Known limitations

These are the current implementation limits and observations from testing. Phone-call evidence is
in [scenarios.md](scenarios.md); simulated-call results are in [the eval report](../evals/REPORT.md).


- **Premature confirmation.** The model can announce a booking before it exists. A post-hoc check adds a correction note for the next reply, but does not prevent the caller from hearing the first claim. This happened once in the final 82-conversation simulation; the next turn wrote the booking.
- **One host.** The worker and the database run on one Mac. If it loses power or network, the
  number stops answering; a watchdog pages when the worker stops taking health checks.
- **One OpenAI key.** Both models and the backup voice run on it, so an outage or an empty balance
  takes out all three. The failure ladder then ends the call with a callback.
- **Keyword detectors.** Hazard, urgent and Spanish detection are word lists. The gas negation is
  lexical ("it's not like there's no gas smell" reads as a no), a chirping smoke detector matches,
  and a bare age counts only after a pronoun or a relation ("I'm 82", "my wife is 79"), never as
  a word ("eighty-two").
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
  against a street list or a ZIP against a town. A ZIP the caller never said is refused, but a
  misheard one they did say is not.
- **Pushes carry no caller details**: the outcome, a first name, the ZIP and a lookup, never the
  number, the street or the caller's words. `scripts/calls.py` has the rest. `DISPATCH_NTFY_TOPIC` falls back to `NTFY_TOPIC`, so without it dispatch summaries
  and on-call pages share one topic.
- **Latency.** Replies ran 0.9 to 2.6 s end to end on the 2026-09-27 phone calls. When the turn
  detector thinks the caller is mid-sentence it waits up to 1.1 s, and up to 2.0 s after the agent
  asks for an address or a number. Since 2026-09-28 the model makes one tool call at a time, so a
  turn that needs two tools (file a task, then check the address) takes one more model round trip;
  not yet measured on a phone call.
- **Short acknowledgments cut the agent off.** On the phone calls "Alright." and "Okay." over an
  agent question interrupted it mid-sentence (calls `riWFX67`, `NpW9kct`). Only a phone call can
  show whether the adaptive interruption model handles a given caller's backchannels.
- **Nobody is actually on call.** Pages reach one test phone, and callback targets are targets.

