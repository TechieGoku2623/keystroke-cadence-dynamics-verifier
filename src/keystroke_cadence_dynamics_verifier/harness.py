"""Fixed-seed latency sample for the cadence verifier."""

from __future__ import annotations

import asyncio
import math
import random
import statistics
import sys
import time
import tracemalloc

from .engine import KeystrokeCadenceDynamicsVerifier

SEED = 4401
ITERATIONS = 5000
WARMUP = 20
_FEATURES = 16


def _empirical_p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.99 * len(ordered))
    index = min(len(ordered), max(1, rank)) - 1
    return ordered[index]


def _vectors(rng: random.Random) -> tuple[list[float], list[float]]:
    dwell = [100.0 + rng.uniform(-8.0, 8.0) for _ in range(_FEATURES)]
    flight = [45.0 + rng.uniform(-6.0, 6.0) for _ in range(_FEATURES)]
    return dwell, flight


async def _execute() -> dict[str, object]:
    rng = random.Random(SEED)
    dwell, flight = _vectors(rng)
    engine = KeystrokeCadenceDynamicsVerifier()
    await engine.run([{"role": "template", "dwell_ms": dwell, "flight_ms": flight}])
    probe = [{"role": "probe", "dwell_ms": dwell, "flight_ms": flight}]
    for _ in range(WARMUP):
        await engine.run(probe)
    latencies: list[float] = []
    tracemalloc.start()
    try:
        last = None
        for _ in range(ITERATIONS):
            started = time.perf_counter_ns()
            last = await engine.run(probe)
            latencies.append((time.perf_counter_ns() - started) / 1000.0)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    if last is None or last["decision"] != "accept":
        raise RuntimeError("benchmark probe was not accepted")
    return {
        "status": "ok",
        "seed": SEED,
        "iterations": ITERATIONS,
        "latency_us": round(latencies[-1], 3),
        "memory_peak_bytes": int(peak),
        "benchmark_avg_us": round(statistics.fmean(latencies), 3),
        "benchmark_p99_us": round(_empirical_p99(latencies), 3),
    }


def main() -> int:
    try:
        report = asyncio.run(_execute())
    except Exception as exc:
        print(f"status=error detail={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "status={status} seed={seed} iterations={iterations} "
        "latency_us={latency_us:.3f} memory_peak_bytes={memory_peak_bytes} "
        "benchmark_avg_us={benchmark_avg_us:.3f} "
        "benchmark_p99_us={benchmark_p99_us:.3f}".format(**report)
    )
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
