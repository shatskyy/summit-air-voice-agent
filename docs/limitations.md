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
- **Latency.** On the 2026-09-28 10:0x phone calls the median reply was 1.8 to 3.0 s, from the end
  of the caller's speech to the agent's first audio. A turn with no tool call took about 1.2 to 1.6
  s. A turn with a tool call took about 2.5 to 2.9 s, because the model makes one tool call at a time
  ([ADR-014](decisions.md#adr-014-one-tool-call-per-turn)), so each tool adds a model round trip of
  roughly a second. When the turn detector thinks the caller is mid-sentence it waits up to 1.1 s,
  and up to 2.0 s after the agent asks for an address or a number (2.5 s until that morning, when a
  finished address waited the full 2.5 s). The windows note on the address yes throws away that
  turn's preemptive reply, a few hundred milliseconds.
- **The reply guard catches exact repeats only.** A sentence said twice in one reply is dropped,
  a paraphrase is not. A booking made before the caller answered the agent's question is held;
  the model has to book again on the answer ([ADR-017](decisions.md#adr-017-a-reply-guard-on-the-models-output)).
- **The cold cue is a word list.** No heat is urgent when the caller says freezing, cold in here,
  a cold snap, snow, winter or 45 degrees or below. "It's chilly" or "the house is 50" don't count,
  and the model is the backstop for them ([ADR-018](decisions.md#adr-018-no-heat-in-the-cold-is-urgent-whoever-is-home)).
- **Short acknowledgments cut the agent off.** On the phone calls "Alright." and "Okay." over an
  agent question interrupted it mid-sentence (calls `riWFX67`, `NpW9kct`). Only a phone call can
  show whether the adaptive interruption model handles a given caller's backchannels.
- **Nobody is actually on call.** Pages reach one test phone, and callback targets are targets.

## Known bugs, not yet fixed

Found in a read-through on September 28 and confirmed by calling the functions directly. Each fix
changes who gets paged or what the agent says, so none went in without a simulated and a phone pass.

- **A failed system can hide an at-risk person from the urgent check.** `RISK_DENIED` treats a
  negation near the risk match as denying the person, and the "no" in "no heat" or the "isn't" in
  "isn't working" counts. `at_risk_in("No heat and my mom is 82.")` returns false; with a comma
  after "heat" it returns true, so the transcript's punctuation decides it. The prompt still tells
  the model to escalate, so the call falls back to the model rather than to routine outright. The
  fix is to scope the negation to the clause that holds the person.
- **An equipment age reads as an infant.** The months-old pattern matches "it's only 3 months
  old" about the AC, which files an urgent task and pages on-call.
- **Smoke as a habit triggers the safety script.** "I smoke" matches the hazard list the same as
  "I smell smoke".
- **A ZIP given after a ZIP-less booking is refused.** A borough-only booking stores an empty ZIP.
  If the caller then adds the ZIP for the same street, `same_visit` compares the ZIPs first, sees
  them differ, and treats it as a second address.

