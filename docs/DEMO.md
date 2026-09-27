# Five-minute demo script

Spine: *every portal can print a podium from the fixture. Quorum tells you which parts of
that podium you can trust, and then helps you decide the rest, fairly and on the record.*

| Time | Screen | Say |
|---|---|---|
| 0:00 | Terminal: `docker compose up` → banner (41 projects, duplicate merged, 4 headers). Then `docker compose -f … offline.yml run offline-check`: "no route to the internet", 7/7, 20/20, 0 leaks | "One command. And here it is with the network physically removed: the official checker, our T3/T4 checker and 332 isolation probes, all from inside a sealed network." |
| 0:30 | `curl` as judge B for judge A's scores → 403; same for a judge that does not exist → the same 403; audit timeline | "Isolation lives in the backend, is tested for every one of 92 API operations × 6 roles, and leaves a trail." |
| 0:50 | Organizer overview: **Decisions that need you** | "Not a dashboard of numbers: the five decisions a human has to make. Everything else is running." |
| 1:10 | Judging ops: burn-down, two stalled judges → **Plan rebalance** → dry run (40/40 at target, 1 component, 0 conflicts) → commit → Mailpit shows the batch e-mails | "The fixture's two unfinished batches. Quorum reassigns them to judges in the right tracks, keeps every judge connected so leniency stays measurable, and e-mails them, offline." |
| 1:50 | Judge console: keyboard scoring with anchors, feedback meter, autosave | "Built for 1–2 hours of reviewing, which is what Raptors asks of judges." |
| 2:10 | Results: the **signal check** banner (ICC −0.006) | "These judges agree no better than chance. Everyone else will print a winner from this. We won't." |
| 2:30 | Click a project → **Why #N** waterfall: jdg_07 excluded ("gave 4 to everything") | "Every score explains itself exactly. This is what we did about the judge who marks everything the same: weight zero, visible, and replaced." |
| 3:00 | **Plan focus round** → table of P(prize) and SE before/after → commit | "Spare judge time goes only where a prize is still in doubt: 43.5% vs 33.5% chance of crowning the true winner on the same budget." |
| 3:20 | **Open tie-break** → judge compares A vs B (keys A/B) → organizer sees P(order) | "Pairwise, where it actually works: separating a handful of tied finalists. Raptors' own Code Olympics podium was 0.071 apart: a tie." |
| 3:50 | **Lock → Publish** → public results (method hash, run hash, audit head) → team scorecard → judge's signed protocol → `/verify` "valid, verified in your browser" | "Every team gets its scorecard. Every judge gets the signed, numbered protocol Raptors issues by hand today." |
| 4:30 | Data → bundle → `python3 -m engine recompute evt_01-bundle.json` → MATCH | "Don't trust us: re-run it with nothing but Python." |
| 4:50 | README acceptance block | "Every project judged. Every tie decided. Every team answered." |

Before recording: `make reset && make up`, sign in as the organizer in one browser profile
and a judge in another, open http://localhost:8025 in a tab.
