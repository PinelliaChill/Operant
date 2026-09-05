#!/usr/bin/env python3
"""Bounded microbenchmark for the hot, local OAuth session lookup path."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time

from operant.auth import OAuthConfig, OAuthControl, _Session


async def benchmark(iterations: int, p95_budget_ms: float) -> dict[str, float | int | bool]:
    config = OAuthConfig(
        issuer="https://issuer.example",
        client_id="operant-local",
        subject="local-user",
        redirect_uri="https://operant.example/internal/auth/callback",
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        jwks_uri="https://issuer.example/jwks",
    )
    control = OAuthControl(config)
    control._sessions["benchmark"] = _Session("subject", {}, time.time() + 3600)
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        if not await control.authenticated("benchmark"):
            raise RuntimeError("benchmark session unexpectedly expired")
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    samples.sort()
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    return {
        "iterations": iterations,
        "median_ms": round(statistics.median(samples), 6),
        "p95_ms": round(p95, 6),
        "budget_p95_ms": p95_budget_ms,
        "passed": p95 <= p95_budget_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10_000)
    parser.add_argument("--p95-budget-ms", type=float, default=0.25)
    args = parser.parse_args()
    if not 100 <= args.iterations <= 1_000_000 or args.p95_budget_ms <= 0:
        parser.error("iterations must be 100..1000000 and budget must be positive")
    result = asyncio.run(benchmark(args.iterations, args.p95_budget_ms))
    print(json.dumps(result, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
