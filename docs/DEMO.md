# Five-minute demo script: one full event lifecycle

The brief asks the video to walk "one full event lifecycle: create, submit, judge, publish".

**Spine:** *every portal can print a podium from the fixture. Quorum tells you which parts of
that podium you can trust, helps you decide the rest fairly and on the record, and answers
every team.*

**Before recording**
- Run `make reset && make up`.
- Open http://localhost:8080 in one browser profile and http://localhost:8025 (Mailpit) in a tab.
- Keep a terminal ready.
- Use the home page's **Try it as…** buttons to switch roles.

| Time | Screen | Say |
|---|---|---|
| 0:00 | Terminal: `docker compose up` → the banner. Then `docker compose -f docker-compose.yml -f docker-compose.offline.yml run --rm offline-check`: "no route to the internet", 7/7, 21/21, 0 leaks | "One command, seeded with the official fixture. And here it is with the network physically removed: the official checker, our T3/T4 checker and 344 isolation probes, from inside a sealed network. The local AI runs in there too." |
| 0:25 | Home page: the logo, the certainty illustration, **Try it as…** | "Quorum: every project judged, every tie decided, every team answered." |
| **CREATE** 0:35 | **Continue as organizer** → Organize → **New event**, "Hackathon Raptors style" → Setup: tracks, prizes, weighted rubric → **Publish & lock method** → the method hash on the public methodology page | "Weights are fixed and fingerprinted before anyone registers. Nobody can change the rules after seeing the scores without a visible, audited override." |
| **SUBMIT** 1:05 | **Continue as participant** → Quorum Live Demo, which is open → create a team → the progress stepper → **Start the submission** → type a title and see the live card preview and the rubric beside the form → Submit | "A normal person always knows the next step, sees how they'll be judged, and drafts autosave." |
| 1:30 | Terminal: `curl` a late submission to the fixture event → `403 deadline_passed`; `curl` judge B for judge A's scores → the same 403 as for a judge who doesn't exist | "The deadline and isolation live in the backend and the database, refused for the right reason." |
| **JUDGE** 1:45 | **Continue as organizer** → Sample Hack 2026 overview: decisions that need you and the checklist → **⌘K** "who hasn't started?" → the answer and "how I know" | "Ask Quorum answers only from your data, by running named queries. It can't invent anything, and a judge asking 'who is winning' gets nothing." |
| 2:05 | Judging ops: two stalled judges → **Plan rebalance** → dry run (40/40 at target, 0 conflicts) → commit → Mailpit shows the batch e-mails | "Unfinished batches are reassigned in one click, keeping judges connected so their leniency stays measurable." |
| 2:25 | **Continue as judge** → a review: keys 1–5, then type feedback and watch the **coach** tick criteria and suggest a next step | "Thirty reviews in an afternoon, and feedback a team can act on. The coach advises; it never scores." |
| **DECIDE** 2:50 | Organizer → Results: the **signal check** (ICC −0.006) and the **certainty chart**, where the whole top 12 is one tie band → **Why #N** waterfall with the flat judge excluded | "These judges agree no better than chance. Everyone else will print a winner. We show what the data supports, and exactly why each score is what it is." |
| 3:20 | **Plan focus round** (P(prize), P(track), SE before/after) → **Open tie-break** → as the judge, compare A vs B with keys A/B | "Spare judge time goes only where a prize is still in doubt. Ties at a prize go to a pre-registered head-to-head round." |
| **PUBLISH** 3:45 | **Lock → Publish** → public results (method hash, run hash, audit head) → participant scorecard with **themes quoted verbatim** and next steps → judge's signed protocol → `/verify`: "valid, verified in your browser" | "Every team gets its scorecard. Every judge gets the signed, numbered protocol Raptors issues by hand today." |
| 4:25 | Data → bundle → `python3 -m engine recompute …` → MATCH. Flash docs/proof/load-test.md: 300 voters, 0 lost votes, 3 replicas | "Recompute it with nothing but Python. And under load, nothing is lost or counted twice." |
| 4:50 | README acceptance block | "Every project judged. Every tie decided. Every team answered." |

If a take runs short, one 20-second insert: **comparative judging**.
- Go to Results → Comparative judging per track → switch on a track.
- The judge picks the stronger of two projects they reviewed.
- The organizer sees a Bradley–Terry order beside the rubric order, with τ.
