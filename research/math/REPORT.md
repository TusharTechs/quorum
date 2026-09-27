# Judging maths: prototype report

Code is in this folder. `judging_math.py` is a pure-stdlib library.
- **Self-test:** run `python3 judging_math.py ../official/fixtures.json`.
- **Fixture analysis:** `analysis_fixture.py` writes `results_fixture.*`.
- **Simulations:** `simulate.py`, with modes normalization|flat|design|adaptive|pairwise, write `results_*.txt`.
- **Which result files to cite:**
  - `results_normalization_S5.txt` for S5 (the S5 block inside `results_normalization.txt` is a broken run);
  - `results_pairwise_persistent.txt`, not the iid variant;
  - `results_adaptive.txt`, not the iid variant.

## 0. Summary
1. **The fixture scores behave like noise.**
   - After the duplicate merge, ICC(1) = −0.006. The one-way F is 0.98 on (39, 83), with permutation p = 0.54.
   - Criterion correlations are 0.15, −0.05 and −0.08.
   - Test-retest σ ≈ 0.79, from 3 judges who scored both copies of the duplicate.
   - Every adjacent pair is a statistical tie. Even #1 vs #20 gives z = 1.66.
   - So the fixture can show what normalization *does*, not that it is *right*. Proof has to come from simulation on the fixture's exact design.
2. **Recommended method D\*:**
   - per-criterion additive judge offsets, ridge-shrunk (BLUP), with k chosen by REML;
   - a flat-judge rule;
   - unshrunk project scores;
   - an exact explanation, a standard error and a tie flag for every project.
3. **Simulation proof** (500 simulations per scenario, fixture design):
   - D\* beats raw whenever judges differ in leniency: τ +0.012 at leniency sd 0.4, +0.053 at sd 0.7.
   - It costs almost nothing when they don't: −0.005.
   - Per-judge z is worse than D\* everywhere, by 0.013–0.135.
   - Gains are modest because a sparse design caps them. Even knowing the true offsets exactly gives only +0.051 at sd 0.4.
4. **Design matters more than the estimator.** Calibration gains +0.003 on disjoint panels vs +0.018 on a connected, overlapping assignment (6×).
5. **Adaptive allocation is the biggest lever.** With the same 120 reviews, 2 each plus two targeted rounds of 20 picks the true #1 in 43.5% of runs vs 33.5% for uniform 3. Adding a pairwise tie-break among the contenders raises this to 53.5%.

## 1. Fixture facts
- **Duplicate:** prj_07 / prj_41 "Dry Harbour", same team (tm_07), same repo. The canonical copy is prj_41 (17:57Z, 3 min before close).
  - Merge policy: re-point reviews to the canonical copy. When the same judge scored both copies, average their scores per criterion into one observation.
  - Result: 40 projects, 123 observations.
  - Test-retest differences: jdg_19 +1.33, jdg_21 +1.00, jdg_26 −1.00.
- **Graph:**
  - One connected component (30 judges, 40 projects), and each track is connected.
  - Seven bridge judges link tracks: jdg_02, 03, 11, 20, 24, 26, 29.
  - Only 66 of 435 judge pairs share a project.
  - Offset information `n_j − Σ 1/n_p` ranges from 0.80 (jdg_23) to 7.05 (jdg_24); 17 of 30 judges sit below 2.2.
  - 8 projects have only 2 reviews: prj_10, 15, 18, 19, 24, 29, 39, 40.
- **Unfinished batches (inferred):** jdg_23 (1 of 8 eligible) and jdg_12 (2 of 11), with jdg_01 (1 of 5) as the alternative. Five of the eight 2-review projects fall in exactly their tracks.
- **Flat judge:** jdg_07 gave all 4s across 9 cells. P = 1.6e-5 under the pooled marginal.

## 2. Method D\*
Per criterion c:
- Model: `y_jpc = s_pc + b_jc + e`, with b ~ N(0, τ²), e ~ N(0, σ²), k = σ²/τ².
- Minimize `Σ(y − s − b)² + kΣb²` (Henderson's equations: s = GLS, b = BLUP).
- ALS updates:
  - `s ← mean_{j∈p}(y − b)`
  - `b ← Σ_{p∈j}(y − s)/(n_j + k)`
- Final score: `s_p = Σ w_c s_pc / Σ w_c`.

Properties:
- Σ b = 0.
- Linear in y, so normalize-then-weight equals weight-then-normalize.
- k → ∞ gives the raw mean.

Choosing k by REML:
- Profile `ℓ(γ) = −½[(N−P)·log σ̂² + Σ_j log(1+γ n_j) + log|X′H⁻¹X|]`, with γ = 1/k and the Woodbury form.
- Clamp to [0.5, 1e6]; fallback k = 4.
- Fixture: k = 18.8, CI (3.2, ∞), |b| ≤ 0.12.
- In simulation, REML tracks the truth. Fixed k = 3 is equally good at sd 0.4 but worse at 0.7 and at 0.

Flat rule: all criteria identical over ≥3 reviews gives weight 0. Under the scale model their slope is exactly 0, so their scores carry no information about quality.

Edge cases:
- sd = 0 with n ≤ 2 is flagged only.
- n = 1 shrinks naturally.
- Disconnected components are identified only within the component, and are prevented by `assign()`.
- A missing criterion is a missing cell.
- A project seen only by flat judges is kept and flagged.

## 3. Fixture under each method

Kendall τ vs raw, mean and max rank move, and track winners changed:

| Method | τ vs raw | Mean move | Max move | Track winners changed |
|---|---|---|---|---|
| z-score | 0.69 | 4.3 | 17 | 2 |
| robust z | 0.63 | 5.2 | 17 | 2 |
| percentile | 0.68 | 4.5 | 16 | 2 |
| D k=3 | 0.88 | 1.9 | 7 | 1 |
| D k=REML | 0.93 | 1.2 | 6 | 0 |
| **D\*** | **0.83** | **2.2** | **18** | **1 (Health)** |
| additive+scale | 0.92 | 1.3 | 9 | 0 |
| induced-pairwise BT | 0.63 | 5.3 | 17 | 3 |

- **Top 3 under D\*:** prj_34 Iron Switch 4.32, prj_11 Salt Ledger 4.32, prj_25 Dry Relay 4.14.
- **Health:** raw picks prj_19 (3.67, 2 reviews). D\* gives a three-way tie at 3.35/3.35/3.33, once jdg_07's 4s are removed.
- **z-score problems:**
  - It is undefined for jdg_07 (sd 0), and for jdg_01 and jdg_23 (n = 1).
  - Every n = 2 judge's reviews become ±0.71, however close the two scores were.
  - It penalizes the strong Data & analytics track. jdg_30 looks +0.52 generous but is +0.19 vs co-judges on the same projects; jdg_25 looks −0.09 but is −0.53.
- **Stability:** leave-one-judge-out keeps #1 in 23–29 of 30 refits. Bootstrap 90% rank intervals are 20–25 wide. The D\* top 3 have intervals [1–9], [1–7] and [1–13].

## 4. Explanation (exact)
Per criterion: `final = raw + [mean_kept − mean_all] − mean_kept(b_j)`. It checks to 1e-9 for all 40 projects.

Example: Small Relay: raw 3.67 · jdg_07 excluded (flat) −0.33 · jdg_29 −0.00 → **3.33**.

## 5. Uncertainty and ties
- SE from `σ̂²(X′H⁻¹X)⁻¹`, ranging 0.26–0.46; prj_19 is 0.64.
- Tie: `|Δ| < 1.96 SE_Δ` (full covariance). `P(A>B) = Φ(Δ/SE_Δ)`.
- Prize probabilities from 4,000 draws of N(ŝ, Cov). Do not resample reviews within a project: with 2 reviews that is degenerate.
- Fixture:
  - P(#1 > #2) = 0.50 and P(#2 > #3) = 0.65.
  - P(top 3) for ranks 1–7: 0.52, 0.49, 0.32, 0.32, 0.24, 0.19, 0.19.
  - No track reaches P(#1 > #2) ≥ 0.95.

## 6. Normalization proof
- **Setup:** 500 simulations, fixture design, jdg_07 flat at 3.
- **Truth:** `Q = θ + mean_c d`, with θ ~ N(0, .45²) and d ~ N(0, .3²).
- **Reviews:** `round(3.4 + a_j(θ+d) + b_j + e)`, e ~ N(0, .9²), clipped to 1–5. ICC ≈ 0.31–0.41.
- **Table:** Kendall τ minus raw, paired.

| Scenario | Raw τ | z-score | D k=3 | D\* | EB-shrunk | Oracle |
|---|---|---|---|---|---|---|
| S0 no leniency | .615 | −.093 | −.012 | −.005 | −.001 | +.004 |
| S1 leniency sd .4 | .559 | −.037 | +.011 | **+.012** | +.015 | +.051 |
| S2 + scale sd .3 | .564 | −.045 | +.010 | +.011 | +.013 | +.049 |
| S3 + track strength | .602 | −.125 | +.010 | +.010 | +.012 | +.046 |
| S4 leniency sd .7 | .477 | +.039 | +.037 | **+.053** | +.053 | +.124 |
| S5 fixture-like (ICC .03) | .185 | −.024 | −.004 | −.003 | −.055 | +.001 |

- S1 other metrics: P(true #1 ranked #1) is raw .30, z .23, D\* .31, oracle .36.
- The flat rule cuts top-5 rank error by 0.15–0.20.
- Additive+scale never beats D.
- EB shrinkage of project scores collapses when there is no signal (S5).

## 7. Assignment design
500 simulations; 40 projects, 30 judges, k = 3.

| Design | D\* τ gain |
|---|---|
| Disjoint panels (10 components) | +0.003 |
| Overlap-greedy (1 component, 119 shared pairs) | +0.018 |
| Random | +0.018 |

Greedy is no better than random here. Its value is the *guarantee* of connectivity and balance under track and conflict constraints.

## 8. Adaptive allocation
Setup: 400 seeds, fixture tracks (+ second tracks for 10 judges), S1 truth (ICC .31), true top 3 overall win.

| Strategy (reviews) | Exact top-3 set | True #1 is #1 | Rank error, top 5 | τ top 10 |
|---|---|---|---|---|
| U3 uniform (120) | .068 | .335 | 4.47 | .321 |
| A2+ 2 each + 2×20 targeted (120) | .092 | **.435** | 4.39 | .374 |
| A2+ 1×40 (120) | .085 | .355 | 4.27 | .355 |
| A3+ 3 each + 20 (140) | .113 | .407 | 3.85 | .383 |
| U4 uniform (160) | .062 | .383 | 3.73 | .374 |
| U3 + tie-break ≤6 | .128 | .468 | 4.16 | .363 |
| **A2+ + tie-break ≤6** | **.142** | **.535** | 4.18 | .400 |
| **A3+ + tie-break ≤6** | **.172** | **.552** | 3.60 | .419 |

- Planner objective: maximize `P(1−P)·v²/(v+σ²)` greedily, with variance updated after each planned review.
- Tie-break setup: 3 judges compare all pairs among ≤6 projects with P(top 3) > .05. Each judge holds persistent impressions. Comparisons are weighted 2/|T| and fused with the rubric prior by probit MAP.
- **Honest bottom line:** even the best pipeline picks the exact top-3 set only 17% of the time at ICC .31.

## 9. Pairwise
- **Estimator:** BT via MM, with a prior of 1 virtual win and 1 virtual loss vs a dummy of strength 1. Ties count as ½. Matches Newton to 4e-9.
- **Crowd-BT:** helps only against an adversarial judge.
- **`next_pair`:** `4p(1−p) + 1/√(1+n) + chain bonus + component-join bonus`, within track.
- **Within-track τ results:**

| Setup | Within-track τ |
|---|---|
| Rubric D\* | .494 |
| BT, 5 comparisons per judge | .491 |
| BT, 10 comparisons per judge | .546 |
| BT, 3 comparisons per judge | .422 |

- **Recommendation:** use pairwise as the tie-break stage, plus an optional per-track mode when judges can do ≥5–10 comparisons. Never fuse pairwise with rubric scores except in the tie-break.

## 10. The judge who marks everything a 3
- **Detect:** all identical over ≥3 reviews. Surface it during judging so the organizer can ask for a re-score.
- **Treat:** weight 0 for ranking. Keep the reviews in audit. Show them as their own line in the explanation.
- **Re-plan:** request replacement reviews (prj_19 on the fixture).
- **Direction:** had jdg_07 given 3s (below the co-judges), excluding them would have raised their projects.

## Caveats
- The simulations assume the additive model, give or take judge scale.
- The tie-break assumes comparison noise equals rubric noise.
- SEs ignore uncertainty in k.
- The allocation runs used augmented judge tracks.
- The unfinished-batch attribution is inferred.
