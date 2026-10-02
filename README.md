# Keystroke Cadence Dynamics Verifier

A high-throughput, low-latency asynchronous engine engineered to resolve enrollment mismatch on caller-supplied dwell and flight millisecond vectors by scoring normalized deviation against a template, without reading a keyboard or storing a character.

## 🏗️ Systems Architecture & Event Topology

`CadenceDynamicsVerifier` stores timing tuples the caller already collected from an opted-in subject. `enroll` writes a template. `verify` scores a probe and returns a decision. `coerce_durations` checks each millisecond value. `cadence_distance` returns the normalized absolute deviation and a z-style distance. `classify_distance` maps those two numbers onto accept, step-up, or reject. `pack_decision` emits the decision bytes. The topic name on those bytes is `commerce.cadence.decision`.

The module does not read input devices, install a hook, log characters, or accept another person's key stream. Stored features are milliseconds. An `asyncio.Lock` covers the template cache. `configure_logging` calls `logging.basicConfig` with timestamps. A negative duration, a duration above 2000 ms, a missing subject, or a non-finite value raises `EngineKernelException`. A rejected probe does not rewrite the template.

## 📊 Core Visual Walkthrough & Engine Pipeline Flow

```
opted-in dwell/flight vector (milliseconds, no characters)
    |
    v
coerce_durations
    |
    +-- negative or > 2000 ms --> EngineKernelException
    +-- length != template -------> truncated / mismatch decision
    +-- zero variance ------------> robotic_zero_variance, template unchanged
    |
    v
cadence_distance(template, probe) --> nad, z
    |
    v
classify_distance --> accept | step-up | reject
    |
    v
pack_decision on commerce.cadence.decision
```

Insert the structural terminal walkthrough recording at docs/assets/terminal-walkthrough.gif before publishing the release notes.

## ⚡ Low-Level OS Mechanics & Network Physics

Normalized absolute deviation divides the mean absolute residual by the template's own scale, so a subject who is uniformly slower is not punished as hard as a subject whose rhythm changed shape. The z-style term uses `statistics.pstdev` of the template. A zero-variance probe has a spread of zero and is classified as robotic before that division is allowed to blow up. `math` guards the finite checks.

The human bound is 2000 ms. Anything slower is not a dwell or a flight; it is a pause, and it raises. The cache is a bounded `deque` of template slots (`CACHE_LIMIT` 256) so a long-lived process does not retain every subject forever. Decision records carry the reason code and the two distances. They do not carry the millisecond vector again, and they never carry a character.

## ⚖️ Architecture Trade-offs & Pragmatic Decisions

A neural keystroke model would score free text and would also need the characters this verifier is forbidden to store. The distance is a pair of scalars against an enrollment template of millisecond numbers. The accept and step-up thresholds (`accept_nad` 0.08, `accept_z` 0.50, `step_nad` 0.30, `step_z` 1.50) are constructor arguments with those defaults. They are the policy. They are not learned on a hidden corpus inside this process.

Length mismatch is a reject, not a sliding alignment. Alignment would reward a probe that dropped the inconvenient intervals. The caller who has a partial vector must re-enroll or send a full one. Zero variance is rejected even when the constant value equals the template mean, because a constant vector is not a human cadence.

## 🚀 Local Installation & Benchmarking

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python src/main.py
python src/test_harness.py
```

```python
import asyncio

from src.main import CadenceDynamicsVerifier


async def demo() -> None:
    verifier = CadenceDynamicsVerifier()
    features = [80.0, 40.0, 90.0, 35.0, 70.0, 45.0, 85.0, 38.0]
    try:
        await verifier.enroll("subject-opt-in", features)
        await verifier.verify("subject-opt-in", features)
    finally:
        await verifier.close()


asyncio.run(demo())
```

The runtime is the Python 3.12 standard library. `pip install -r requirements.txt` succeeds with no third-party packages. The vectors above are durations in milliseconds, not keys.

## 🖥️ Terminal Diagnostic Output Preview

```
INFO [cadence.kernel] template stored features=16
WARNING [cadence.kernel] cadence rejected reason=robotic_zero_variance nad=0.0000 z=0.0000
INFO [cadence.kernel] demo complete enroll=enrolled decision=accept nad=0.0042 robotic=robotic_zero_variance
```

`python src/main.py` exits 0. The accept path and the zero-variance rejection are both in that run. No character is logged.

## 📊 Empirical Benchmarking Performance Report

Measured by `python src/test_harness.py` with seed 4401, 5000 iterations, `time.perf_counter_ns` latency in microseconds, and `tracemalloc` peak.

| Metric | Measured |
| --- | ---: |
| Status | PASS |
| Iterations | 5000 |
| Average latency | 565.814 µs |
| Empirical P99 | 689.472 µs |
| tracemalloc peak | 223893 bytes |

## 🛡️ Edge-Case Resilience & SOC2/Regulatory Compliance

A probe whose length does not match the template is rejected as truncated or mismatched and does not replace the template. A zero-variance probe is rejected as `robotic_zero_variance`. A negative duration or a duration above 2000 ms raises `EngineKernelException` inside `coerce_durations` before a distance is computed.

The design is aligned with PCI-DSS constraints on authentication data: no PAN, no keystroke characters, no cardholder data in the decision bytes. SOC 2 processing integrity is the rule that a rejected probe does not rewrite the enrollment template. The module does not capture keystrokes. The caller supplies millisecond vectors from an opted-in subject.
