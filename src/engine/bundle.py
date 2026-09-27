"""Canonical JSON and hashing: the basis of reproducible, verifiable results.

A ranking run is identified by the SHA-256 of its canonical input. Floats are rounded
to 9 decimal places before hashing so the same computation on any platform yields the
same bytes. Anyone holding an exported bundle can recompute the ranking and compare.
"""

from __future__ import annotations

import hashlib
import json
import math


def _clean(o):
    if isinstance(o, float):
        if math.isnan(o):
            return "nan"
        if math.isinf(o):
            return "inf" if o > 0 else "-inf"
        r = round(o, 9)
        return 0.0 if r == 0 else r
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, set):
        return sorted(_clean(v) for v in o)
    return o


def canonical_json(obj) -> str:
    return json.dumps(_clean(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data) -> str:
    if not isinstance(data, (bytes, str)):
        data = canonical_json(data)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()
