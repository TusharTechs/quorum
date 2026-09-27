"""Command line for the judging engine.

  python -m engine fixture fixtures.json          # full report for a DOGFOOD fixture
  python -m engine recompute bundle.json          # re-run an exported event, compare hashes
  python -m engine explain fixtures.json prj_19   # "why is this project where it is?"
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .fixtures import from_dogfood_fixture
from .pipeline import compute


def _fmt(x, nd=2):
    return "-" if x is None else f"{x:.{nd}f}"


def cmd_fixture(path: str) -> int:
    fx = json.load(open(path))
    inp, log = from_dogfood_fixture(fx)
    t0 = time.time()
    res = compute(inp)
    dt = time.time() - t0
    titles = {p["id"]: p.get("title") for p in fx["projects"]}
    sig = res["signal"]
    print(f"engine {res['engine']}  input {res['input_hash'][:16]}  output {res['output_hash'][:16]}  ({dt:.1f}s)")
    for d in log["duplicates"]:
        print(f"duplicate merged: {', '.join(d['superseded'])} -> {d['canonical']} ({d['reason']})")
    print(f"k = {res['k']:.2f} ({res['k_source']})   flat judges: {', '.join(res['flat_judges']) or 'none'}")
    print(f"signal check: ICC(1) = {_fmt(sig['icc1'], 3)}, permutation p = {_fmt(sig['p_value'], 3)} -> {sig['verdict']}")
    print()
    print(f"{'rank':>4} {'project':<24} {'raw':>5} {'calib':>6} {'±se':>5} {'P(prize)':>8} {'interval':>9}  tie")
    for e in res["entries"][:12]:
        print(f"{e['rank']:>4} {titles.get(e['project'], e['project'])[:24]:<24} {_fmt(e['raw']):>5} "
              f"{_fmt(e['calibrated']):>6} {_fmt(e['se']):>5} {_fmt(e['p_prize']):>8} "
              f"{str(e['rank_lo']) + '-' + str(e['rank_hi']):>9}  {'tied with next' if e['tied_with_next'] else ''}")
    return 0


def cmd_explain(path: str, project: str) -> int:
    fx = json.load(open(path))
    inp, _ = from_dogfood_fixture(fx)
    res = compute(inp, heavy=False)
    e = res["explanations"][project]
    print(f"{project}: raw {e['raw_mean']:.2f}")
    for line in e["lines"]:
        print(f"  {line['judge']:<8} {line['label']:<34} {line['contribution']:+.3f}")
    print(f"  = {e['final']:.3f}   (exact: {e['exact']})")
    return 0


def cmd_recompute(path: str) -> int:
    bundle = json.load(open(path))
    runs = bundle.get("ranking_runs") or []
    if not runs:
        print("bundle contains no ranking runs")
        return 2
    ok = True
    for run in runs:
        res = compute(run["input"], heavy=run.get("heavy", True))
        match = res["output_hash"] == run["output_hash"]
        ok &= match
        print(f"{'MATCH' if match else 'MISMATCH'} run {run.get('id', '?')} ({run.get('kind')}) "
              f"output {res['output_hash']} expected {run['output_hash']}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m engine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("fixture")
    a.add_argument("path")
    b = sub.add_parser("recompute")
    b.add_argument("path")
    c = sub.add_parser("explain")
    c.add_argument("path")
    c.add_argument("project")
    args = ap.parse_args(argv)
    if args.cmd == "fixture":
        return cmd_fixture(args.path)
    if args.cmd == "recompute":
        return cmd_recompute(args.path)
    return cmd_explain(args.path, args.project)


if __name__ == "__main__":
    sys.exit(main())
