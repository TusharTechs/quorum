"""Quorum judging engine: pure-Python, dependency-free, deterministic.

Modules
-------
prepare      duplicate merging, weighted totals, flat-judge detection
calibrate    additive judge offsets with REML-chosen shrinkage + exact explanations
uncertainty  standard errors, ties, prize probabilities, signal check, robustness
allocate     focus rounds (adaptive review allocation)
pairwise     Bradley-Terry, pair selection, tie-break fusion
assign       overlap-maximising assignment and design diagnostics
compare      raw / Raptors-classic / z-score baselines, rank agreement
pipeline     compute(): the single entry point used by the app, CLI and verifier
bundle       canonical JSON and hashing
"""

ENGINE_VERSION = "quorum-engine/1.0"
