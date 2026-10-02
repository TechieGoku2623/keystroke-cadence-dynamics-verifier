"""Deterministic checks and a latency sample for the cadence verifier."""

from __future__ import annotations

import asyncio
import importlib
import json
import math
import random
import statistics
import struct
import sys
import time
import tracemalloc
from pathlib import Path

SEED = 4401
ITERATIONS = 5000
WARMUP = 20
FEATURES = 16


def load_module():
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("src.main")


def expect(condition: bool, detail: object) -> None:
    if not condition:
        raise AssertionError(detail)


def empirical_p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.99 * len(ordered))
    index = min(len(ordered), max(1, rank)) - 1
    return ordered[index]


def build_template(rng: random.Random) -> list[float]:
    values = []
    for index in range(FEATURES):
        base = 120.0 if index % 2 == 0 else 70.0
        values.append(base + rng.uniform(-6.0, 6.0))
    return values


def shift(template: list[float], delta: float) -> list[float]:
    return [value + delta for value in template]


async def check_decisions(mod, template: list[float]) -> None:
    verifier = mod.CadenceDynamicsVerifier()
    try:
        enrolled = await verifier.enroll("acct-4401", template)
        accepted = await verifier.verify("acct-4401", shift(template, 0.4))
        stepped = await verifier.verify("acct-4401", shift(template, 15.0))
        rejected = await verifier.verify("acct-4401", shift(template, 70.0))
        missing = await verifier.verify("acct-9999", shift(template, 0.4))
    finally:
        await verifier.close()
    expect(enrolled["status"] == "enrolled", enrolled)
    expect(enrolled["template_updated"] is True, enrolled)
    expect(enrolled["topic"] is None, enrolled)
    expect(accepted["decision"] == "accept", accepted)
    expect(accepted["reason"] == "match", accepted)
    expect(accepted["template_updated"] is False, accepted)
    expect(accepted["topic"] == mod.DECISION_TOPIC, accepted)
    expect(stepped["decision"] == "step_up", stepped)
    expect(stepped["reason"] == "marginal", stepped)
    expect(stepped["template_updated"] is False, stepped)
    expect(rejected["decision"] == "reject", rejected)
    expect(rejected["reason"] == "mismatch", rejected)
    expect(missing["reason"] == "not_enrolled", missing)
    blob = bytes.fromhex(accepted["record_hex"])
    version, code, nad, z_distance, count, reason = struct.unpack(">BBffHH", blob)
    expect(version == 1, version)
    expect(code == 1, code)
    expect(count == FEATURES, count)
    expect(reason == 1, reason)
    expect(math.isclose(nad, accepted["nad"], rel_tol=1e-6, abs_tol=1e-6), nad)
    expect(len(blob) == 14, len(blob))
    expect(z_distance >= 0.0, z_distance)


async def check_truncated_and_robotic(mod, template: list[float]) -> None:
    verifier = mod.CadenceDynamicsVerifier()
    truncated = template[: len(template) // 2]
    robotic = [90.0] * len(template)
    try:
        await verifier.enroll("acct-4401", template)
        short = await verifier.verify("acct-4401", truncated)
        flat = await verifier.verify("acct-4401", robotic)
        still = await verifier.verify("acct-4401", shift(template, 0.4))
    finally:
        await verifier.close()
    expect(short["decision"] == "reject", short)
    expect(short["reason"] == "truncated_probe", short)
    expect(short["template_updated"] is False, short)
    expect(len(truncated) != len(template), len(truncated))
    expect(flat["decision"] == "reject", flat)
    expect(flat["reason"] == "robotic_zero_variance", flat)
    expect(flat["template_updated"] is False, flat)
    expect(statistics.pstdev(robotic) == 0.0, robotic)
    expect(still["decision"] == "accept", still)


async def check_clock_skew_does_not_update(mod, template: list[float]) -> None:
    verifier = mod.CadenceDynamicsVerifier()
    negative = [-1.0] * len(template)
    too_slow = [2000.1] * len(template)
    characters_rejected = False
    try:
        await verifier.enroll("acct-4401", template)
        enroll_negative = False
        try:
            await verifier.enroll("acct-4401", negative)
        except mod.EngineKernelException:
            enroll_negative = True
        enroll_slow = False
        try:
            await verifier.enroll("acct-4401", too_slow)
        except mod.EngineKernelException:
            enroll_slow = True
        verify_negative = False
        try:
            await verifier.verify("acct-4401", negative)
        except mod.EngineKernelException:
            verify_negative = True
        try:
            await verifier.verify("acct-4401", "dwell-flight-text")
        except mod.EngineKernelException:
            characters_rejected = True
        still = await verifier.verify("acct-4401", shift(template, 0.4))
    finally:
        await verifier.close()
    expect(enroll_negative, "negative enroll was stored")
    expect(enroll_slow, "over-bound enroll was stored")
    expect(verify_negative, "negative probe was scored")
    expect(characters_rejected, "character probe was accepted")
    expect(still["decision"] == "accept", still)
    expect(still["template_updated"] is False, still)


async def run_benchmark(mod, template: list[float]) -> dict[str, float | int]:
    verifier = mod.CadenceDynamicsVerifier()
    probe = shift(template, 0.4)
    latencies: list[float] = []
    try:
        await verifier.enroll("acct-4401", template)
        for _ in range(WARMUP):
            await verifier.verify("acct-4401", probe)
        tracemalloc.start()
        last = None
        for _ in range(ITERATIONS):
            started = time.perf_counter_ns()
            last = await verifier.verify("acct-4401", probe)
            latencies.append((time.perf_counter_ns() - started) / 1000.0)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        await verifier.close()
    expect(last is not None and last["decision"] == "accept", last)
    return {
        "n": ITERATIONS,
        "avg_us": round(statistics.fmean(latencies), 3),
        "p99_us": round(empirical_p99(latencies), 3),
        "peak_bytes": peak,
    }


async def execute(name: str, func) -> dict[str, object]:
    try:
        await func()
    except Exception as exc:
        return {
            "name": name,
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {"name": name, "passed": True}


async def amain() -> dict[str, object]:
    mod = load_module()
    rng = random.Random(SEED)
    template = build_template(rng)
    checks = [
        ("accept_step_up_reject", lambda: check_decisions(mod, template)),
        ("truncated_and_robotic", lambda: check_truncated_and_robotic(mod, template)),
        ("clock_skew_guard", lambda: check_clock_skew_does_not_update(mod, template)),
    ]
    results = []
    for name, func in checks:
        results.append(await execute(name, func))
    benchmark = await run_benchmark(mod, template)
    status = "PASS" if all(item["passed"] for item in results) else "FAIL"
    return {
        "status": status,
        "seed": SEED,
        "checks": results,
        "benchmark": benchmark,
    }


def main() -> int:
    summary = asyncio.run(amain())
    benchmark = summary["benchmark"]
    print(
        "benchmark "
        f"n={benchmark['n']} avg_us={benchmark['avg_us']:.3f} "
        f"p99_us={benchmark['p99_us']:.3f} peak_bytes={benchmark['peak_bytes']}"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
