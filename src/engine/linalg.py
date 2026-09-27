"""Small dense linear algebra, enough for the P x P systems a hackathon produces.

Pure standard library on purpose: the judging engine must run anywhere Python runs,
with nothing to install, so anyone can recompute published results.
"""

from __future__ import annotations

import math


def cholesky(a: list[list[float]]) -> list[list[float]]:
    """Lower-triangular L with L L^T = a (a symmetric positive definite)."""
    n = len(a)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        Li = L[i]
        for j in range(i + 1):
            Lj = L[j]
            s = a[i][j]
            for m in range(j):
                s -= Li[m] * Lj[m]
            if i == j:
                if s <= 0.0:
                    raise ValueError("matrix not positive definite")
                Li[i] = math.sqrt(s)
            else:
                Li[j] = s / Lj[j]
    return L


def chol_solve(L: list[list[float]], b: list[float]) -> list[float]:
    n = len(L)
    z = [0.0] * n
    for i in range(n):
        s = b[i]
        Li = L[i]
        for m in range(i):
            s -= Li[m] * z[m]
        z[i] = s / Li[i]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = z[i]
        for m in range(i + 1, n):
            s -= L[m][i] * x[m]
        x[i] = s / L[i][i]
    return x


def chol_inverse(L: list[list[float]]) -> list[list[float]]:
    n = len(L)
    cols = []
    for i in range(n):
        e = [0.0] * n
        e[i] = 1.0
        cols.append(chol_solve(L, e))
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def chol_logdet(L: list[list[float]]) -> float:
    return 2.0 * sum(math.log(L[i][i]) for i in range(len(L)))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
