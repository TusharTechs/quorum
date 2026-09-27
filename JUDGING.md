# Judging in Quorum

> **Short version.** Quorum fixes the judging method before anyone registers. It then:
> - assigns reviews so that judge harshness is measurable;
> - corrects for harshness with a transparent mixed model, and explains every score exactly;
> - reports how much the ranking can be trusted;
> - spends spare judge time only where a prize is still in doubt;
> - settles statistical ties with a pre-registered pairwise round.
>
> It does not claim to remove human judgement or to find "the true winner". It makes the method **explicit, consistent, inspectable and reproducible**.

Everything below is implemented in [`src/engine/`](src/engine), a pure-Python package with no dependencies. The web app, the worker, the CLI and the bundle verifier all call the same `engine.pipeline.compute()`. The numbers in this document come from [`docs/proof/`](docs/proof/README.md), which `python3 tools/generate_proof.py` regenerates from scratch.

---

## 1. The pipeline

```
 method locked (hash public) ──► assignment (overlap-aware, conflict-free, capacity-bounded)
        │                                   │
        │                        judges review in batches (anchored rubric, written feedback)
        │                                   │
        ▼                                   ▼
 calibration (per-criterion judge offsets, REML shrinkage, flat-judge rule)
        │
        ▼
 uncertainty (standard errors, statistical ties, P(prize), signal check, robustness)
        │
        ├──► focus round: extra reviews only where prize membership is uncertain ──┐
        │                                                                          │
        ├──► tie-break round: pairwise comparisons among tied contenders ◄─────────┘
        ▼
 lock (immutable run, signed audit head) ──► publish (results + methodology + scorecards)
```

## 2. The method is fixed before registration (pre-registration)

Hackathon Raptors' own rule is that *criteria and weights are published before registration opens and never change*. Quorum makes that rule a mechanism:

- **Locking.** Publishing an event serialises the whole method to canonical JSON (sorted keys, integer weights) and stores its SHA-256. The method covers criteria, weights in basis points, scales, calibration method, flat-judge rule, reviews per project, focus budget, tie-break policy, feedback rule and voting rule.
- **Visibility.** The hash is shown on the public event page and at `/e/<event>/methodology`.
- **Database enforcement.** A trigger refuses any change to a locked method row. Criteria that already have scores cannot change key, weight or scale.
- **Admin override.** An administrator can override, with a written reason. That creates a new locked version, adds a `METHOD_OVERRIDDEN` audit event, and puts a permanent notice on the results page.

Why it matters: without pre-registration, an organizer can try weightings after the scores are in until a preferred project wins ("method shopping"). A what-if sandbox would make that easy, so Quorum deliberately does not have one. Robustness analysis (§8) answers the legitimate question, "would a different weighting change the winner?", without letting anyone choose.

## 3. Assignment is part of the measurement

A harsh judge and a judge who happened to draw weak projects look identical, unless their batches overlap with other judges' batches. With **disjoint panels** (classic table judging) the correction below provably does nothing: every project in a panel has the same judges, so their offsets shift the whole panel equally. On the fixture's design (§12) the gain is exactly 0.000 with disjoint panels, against +0.036 Kendall τ with overlap.

`engine.assign.assign()` therefore does four jobs at once:

| Goal | How |
|---|---|
| Coverage | Every eligible project reaches `k` reviews (default 3). Slots are filled in rounds, hardest project first (fewest eligible judges). |
| Integrity | Only judges whose tracks include the project. Never a confirmed conflict of interest. The database also refuses judges who are team members. |
| Balance | Per-judge capacity (default: the batch size, 10–12 for Raptors). |
| Measurability | Each slot goes to the eligible judge maximising `3·(new co-review edges) + 2·(joins two components of the judge graph) − 2·load/capacity`. |

The dry run shows, before anything is written:

- coverage;
- load min/max;
- judge-graph components (1 = everyone is calibratable against everyone);
- judge pairs that share a project;
- conflicts violated (must be 0);
- the information behind each judge's offset, `n_j − Σ 1/n_p`.

Every planned pair is re-validated on commit, because plans can also come from API clients.

**Self-healing.** A judge is *stalled* if they have pending work and no activity for 48 hours, and *behind* if their pace projects past the deadline. Rebalancing retires their pending assignments (`status=reassigned`, lineage kept in `reassigned_from`) and re-plans with the same objective, excluding stalled and unavailable judges. A recusal ("I have a conflict") records a confirmed conflict for the whole team and re-queues the project automatically.

## 4. A review

A review scores every criterion on its scale. Each scale point has an anchor text, shown in the console, to reduce variance at the source. Written feedback to the team is required, with a minimum length set by the event (default 80 characters), because *every team receives its score and written feedback, placed or not*.

The review's weighted total is `y = Σ_c w_c·x_c / Σ_c w_c`, with weights stored as integer basis points. Only complete, submitted reviews enter the ranking.

## 5. Calibration: `offset-reml/v1`

For each criterion `c` separately:

```
y_jpc = s_pc + b_jc + e,     b ~ N(0, τ²),  e ~ N(0, σ²),   k = σ²/τ²
```

- `s_pc` is project `p`'s calibrated score on the original 1–5 scale.
- `b_jc` is judge `j`'s leniency.

The fit minimises `Σ (y − s − b)² + k·Σ b²`. These are Henderson's mixed-model equations, so `s` is the GLS estimate and `b` the BLUP. It is solved by alternating least squares until changes are below 1e-12:

```
s_pc ← mean over judges of p of (y_jpc − b_jc)
b_jc ← Σ over projects of j of (y_jpc − s_pc) / (n_j + k)
```

The final score is `s_p = Σ_c w_c·s_pc / Σ_c w_c`.

- **Shrinkage `k` is chosen by REML** (projects fixed, judges random), maximising the profile `ℓ(γ) = −½[(N−P)·log σ̂² + Σ_j log(1+γ n_j) + log|X′H⁻¹X|]` with `γ = 1/k`. The Woodbury form keeps every matrix P×P. `k` is clamped to [0.5, 10⁶], with a fallback of 4 if REML cannot run.
  - If the data show no judge effect, REML picks a huge `k` and the method *becomes the raw mean*.
  - When a judge's offset rests on little evidence, it is shrunk towards 0 in proportion.
- **Why per criterion:** a missing criterion is a missing cell, never an imputed one. Because the estimator is linear in `y` for fixed `k`, "normalise then weight" equals "weight then normalise", so organizers can reason about weights directly.
- **Verified:** ALS equals a direct GLS solve to 1e-10 (`tests/engine/test_calibrate.py`). Offsets sum to zero, so the average judge is the reference. Adding a constant to one judge's scores moves every project equally and changes no ranking.

**Why not per-judge z-scores** (what most platforms, and most DOGFOOD entries, do):

- Judges see small, different, non-random sets of projects: 1–11 each in the fixture, track-confined. z-scoring assumes every judge saw a comparable random sample.
- It is undefined for a judge with one review or identical scores, and it turns every two-review judge's scores into ±0.71 however close they were.
- It punishes projects that a judge reviewed alongside strong ones. In the fixture, jdg_30 *looks* +0.52 generous but is only +0.19 against co-judges on the same projects.
- In simulation it is worse than calibration in every scenario (§12).

**Why not shrink project scores towards the mean:**

- It ranks teams partly by *how many reviews they happened to get*, so teams are penalised for judges who didn't finish.
- It collapses when there is no signal.
- Quorum fixes coverage operationally (§3) and reports the lower precision as a wider interval instead.
- Raptors' current method (weighted mean, then Bayesian shrinkage `(n·x̄ + 10·μ)/(n + 10)`) is still computed side by side as "Raptors classic", so switching is a decision rather than a leap.

## 6. The judge who marks everything a 3

The fixture's jdg_07 gave 4 on all nine criterion cells of three reviews. By chance alone that has probability 1.6×10⁻⁵, and jdg_07 also wrote no feedback.

- **Detect.** A judge whose every criterion score is identical over at least 3 reviews is *flat*. This shows on the ops page during judging, so the organizer can ask for a re-score while it still helps.
- **Treat.** Weight 0 in the ranking. Under a scale model `y = μ + b_j + a_j·s_p`, a flat judge's slope `a_j` is exactly 0: their scores carry no information about which project is better, and their level is only their leniency.
  - Keeping them *compresses* their projects toward their constant.
  - Offset-only correction keeps them in as "everything is average".
  - z-scoring divides by zero.
- **Keep the record.** Their reviews stay in the database and the audit log, and appear as their own line in every affected explanation ("excluded: gave 4 to everything −0.333").
- **Re-plan.** Their projects now have fewer informative reviews, so the focus planner (§9) requests replacement reviews for them (prj_19 on the fixture).
- **Direction depends on the data.** jdg_07 gave 4s, above co-judges' average, so their projects fall. A flat judge who gave 3s would see their projects rise.

A judge with fewer than 3 identical reviews is flagged but not excluded: two equal scores are not evidence of anything.

## 7. Every score explains itself

For each criterion, then combined with the weights:

```
final = raw mean + [mean over kept reviews − mean over all reviews]   (attributed to excluded flat judges)
                 − Σ over kept judges j of  b_j / n_kept                (one line per judge)
```

The parts add up **exactly**; a test enforces this to 1e-9 for every project. Organizers see it as a waterfall at `/o/<event>/results/<project>`. Teams see the same waterfall on their scorecard, with judges shown as "Judge A/B/C". Example from the fixture:

> **Small Relay**: raw 3.667 · jdg_07 (excluded: gave 4 to everything) −0.333 · jdg_29 (near-average judge) −0.001 = **3.333**

## 8. How much can the ranking be trusted?

- **Standard errors.** From `Cov(ŝ) = σ̂²(X′H⁻¹X)⁻¹`, the same estimator with the same `k`. We also report the *effective* number of reviews after the flat rule.
- **Statistical ties.** Adjacent projects are tied when `|Δ| < 1.96·SE_Δ`, using the full covariance. The ranking shows `P(A above B) = Φ(Δ/SE_Δ)`.
- **Prize probabilities and plausible ranks.** 4,000 seeded draws from `N(ŝ, Cov)` give P(top N), P(#1 overall), P(#1 in track) and the 90% rank interval.
  - We do not bootstrap reviews within a project: with two reviews that resample has zero spread half the time.
- **Signal check.** ICC(1) of review totals, with a 2,000-shuffle permutation test of the one-way F. If `p > 0.05` the results page says, in words, that the scores cannot separate the projects.
- **Leave-one-judge-out.** Refit without each judge (k fixed). *Pivotal judges* are those whose removal changes a prize.
- **Weight sensitivity.** Each weight ×0.9 and ×1.1, renormalised. Does the podium change?
- **Method comparison.** Kendall τ of raw, Raptors-classic, z-score and ordinal (pairwise-from-rubric) rankings against the calibrated one.

**Raptors' own data.** At Code Olympics 2026 the podium was 4.312, 4.241 and 4.177, on 9 reviews each. With a per-review spread of 0.5–0.8, the standard error of a 9-review mean is 0.17–0.27 before shrinkage, so 1st and 2nd (0.071 apart) were almost certainly a statistical tie. Quorum would have marked it and opened a tie-break round.

## 9. Focus rounds: spend judge time where it changes the outcome

After every project has its baseline reviews, extra reviews on a project that is certainly in, or certainly out of, the prize set change nothing. `engine.allocate.plan_next_round()` sends each extra review where it buys the most prize certainty:

```
gain_p = P_p (1 − P_p) · v_p² / (v_p + σ²)      P_p = P(project wins a prize),  v_p = current score variance
```

After each planned review, `v_p ← 1/(1/v_p + 1/σ²)`, which gives diminishing returns. Each slot goes to an eligible, non-flat judge with capacity, preferring the judge whose own offset rests on the most evidence.

- **Guardrails, all pre-registered:**
  - every team keeps its full baseline;
  - focus uses spare capacity only, within a budget (default 15% of baseline reviews);
  - at most 2 rounds;
  - scorecards show each project's review count and standard error.
- **Evidence** (400 simulations, ICC ≈ 0.31, top 3 win; `research/math/results_adaptive.txt`):

| Strategy (reviews) | True #1 ranked #1 | Exact top-3 set |
|---|---:|---:|
| Uniform, 3 each (120) | 33.5% | 6.8% |
| 2 each + two targeted rounds of 20 (120) | **43.5%** | 9.2% |
| Uniform, 4 each (160) | 38.3% | 6.2% |
| 3 each + 20 targeted (140) | 40.7% | 11.3% |
| 2 each + targeted + **tie-break** (120) | **53.5%** | 14.2% |
| 3 each + 20 targeted + **tie-break** (140) | **55.2%** | 17.2% |

The honest cost is that overall τ across all 40 projects dips slightly (0.558 vs 0.585). Focus trades mid-table precision for podium precision, which is the part that carries prize money. The honest bottom line: even the best pipeline picks the exact top 3 only 17% of the time at this level of judge agreement. That is why ties are reported rather than hidden.

## 10. Tie-breaks: pairwise where it earns its keep

When a tie group crosses a paid prize position, three conflict-free judges compare the tied contenders head to head (at most 6 projects).

- **Panel choice.** Prefer judges whose calibration rests on the most evidence, never flat scorers, and break ties by current load.
- **Pair order** (`engine.pairwise.next_pair`). Hard rules: never repeat a pair for a judge, and never a conflict. The soft score is `4p(1−p)` (uncertain outcome) + coverage + a *chain bonus* for pairs that include the project the judge just saw, so they read one new project per comparison (the Gavel trick) + a component-join bonus.
- **Fusion.** Maximum a posteriori latent scores. The rubric result is the prior `N(ŝ, SE²)`, and the likelihood is Thurstone–Mosteller `P(a beats b) = Φ((x_a − x_b)/(√2·σ))`. Ties count as half a win each way, and each judge's comparisons are weighted `2/|T|`. Solved by Newton's method on a concave objective.
  - Judge leniency cancels inside a comparison, so no offsets are needed. That is exactly why pairwise is the right tool here.
- **Resolution rule** (pre-registered):
  - Resolved when every adjacent pair has `P ≥ 0.80` under the fused posterior.
  - Otherwise the fallback applies: an organizer decision recorded with a written reason, or a shared prize. Either way it is audited and published as such.
- **Rubric and pairwise are never averaged.** They are on different scales. Pairwise enters only as evidence conditioned on the rubric posterior, at a prize boundary, which is the one place the fusion is principled.

**Full pairwise mode.** The Bradley–Terry estimator (Hunter's MM algorithm, with one virtual win and one virtual loss against a dummy of strength 1, so the fit exists for all-win projects and disconnected graphs) is also exposed for events that want Gavel-style comparative judging per track.

- In simulation it matches the rubric's within-track accuracy at 5 comparisons per judge and beats it at 10.
- It produces no per-criterion scores or written feedback, which Raptors promises, so it is an option rather than the default.
- For tracks of 3 projects, ranking the track directly is better.
- Crowd-BT judge-reliability parameters are not used: they are not identifiable at about 10 comparisons per judge.

## 11. Reproducibility

- **Immutable runs.** Every ranking is an immutable `RankingRun` (a database trigger forbids UPDATE/DELETE). It is keyed by the SHA-256 of its exact engine input and stores the SHA-256 of its canonical output. Floats are rounded to 9 decimals before hashing, so any platform gives the same bytes.
- **Locking.** Locking results freezes an official run, applies resolved tie-breaks, and signs the audit head (Ed25519). The run hash and audit head are printed on the public results page.
- **Recompute.** The event bundle contains every run's input and output hash. `python -m engine recompute bundle.json` reruns them with the standard library and prints `MATCH`/`MISMATCH`. The API offers the same at `POST /api/v1/bundles/verify`.

## 12. The DOGFOOD fixture, honestly

- **Duplicate.** prj_07 and prj_41 are the same team and repository.
  - The canonical copy is the latest on-time one, prj_41 (submitted 17:57Z, three minutes before close).
  - Reviews are pooled. A judge who scored both copies is averaged into one observation, so no judge counts twice.
  - Three judges did score both copies, and disagreed *with themselves* by 1.0–1.33 points: a free measurement of how noisy one review is.
- **Unfinished batches.** The fixture has no assignment list, so we reconstruct its two unfinished batches rather than pretend they didn't exist. Each missing review of an under-covered project is attributed to the least-complete judge (under 30%) in its track. This gives jdg_23 and jdg_12, with five pending reviews. The inference is recorded in the audit log.
- **Flat judge.** jdg_07 (§6).
- **No signal.** ICC(1) = −0.006, permutation p ≈ 0.5, and the three criteria are nearly uncorrelated.
  - REML chooses k ≈ 18.8 because judge offsets are barely identifiable.
  - Every adjacent position is a statistical tie, and the top project's plausible ranks span 1–15.
  - **No method can recover a ranking from these scores.** A platform that prints a confident podium from this data is overstating it. Quorum says so on the results page, then shows how a focus round and a tie-break would settle the prize positions.

## 13. The proof

On the fixture's exact 123 judge–project pairs, with known truth (full tables and chart in [`docs/proof/`](docs/proof/README.md)). Kendall τ of each method minus the raw mean, 300 paired simulations per row:

| Judge leniency spread (sd) | Per-judge z-score | **Quorum calibration** |
|---|---:|---:|
| 0.0 | −0.092 | **+0.003** |
| 0.4 | −0.036 | **+0.018** |
| 0.8 | +0.066 | **+0.070** |

- When judges differ, calibration recovers the truth better, and more so as they differ more.
- When they do not, it costs nothing.
- z-scoring is worse than calibration everywhere.
- The ceiling on gains is set by the design (§3), which is why assignment and calibration are one system.

## 14. Known limits

- **Linear leniency only.** A judge who compresses only the top of the scale is not corrected. Judge slopes are not estimable at 1–11 reviews each; we tested a scale model and it never beat offsets.
- **Standard errors treat `k` as known.**
- **Consistent collusion is invisible to statistics.** Undisclosed friendships or coordinated judges are not detectable when their pattern is consistent. Leave-one-judge-out shows how much any single judge matters.
- **The tie-break model assumes comparison noise ≈ rubric noise.**
- **Focus rounds assume the prize is "top N overall".** Per-track prizes use the per-track P(#1), which is reported but not yet planned against.
- **Offline means no external checks.** Quorum cannot verify that a repository wasn't changed after the deadline. It stores the team's declared commit SHA for judges to check.

## 15. References

- Henderson (1975), best linear unbiased estimation and prediction under a selection model.
- Patterson & Thompson (1971), recovery of inter-block information (REML).
- MacKay, Kenna, Low & Parker (2017), *Calibration with confidence: a principled method for panel assessment*, Royal Society Open Science.
- Roos, Rothe & Scheuermann (2011), *How to calibrate the scores of biased reviewers by quadratic programming*.
- Wang & Shah (2019), *Your 2 is my 1, your 3 is my 9: handling arbitrary miscalibrations in ratings*.
- Hunter (2004), *MM algorithms for generalized Bradley–Terry models*.
- Chen et al. (2013), *Pairwise ranking aggregation in a crowdsourced setting* (Crowd-BT, used by Gavel).
