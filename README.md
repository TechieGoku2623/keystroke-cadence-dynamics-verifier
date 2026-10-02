# Keystroke Cadence Dynamics Verifier

A high-throughput, low-latency asynchronous engine engineered to resolve whether a caller-supplied dwell and flight millisecond probe matches an enrolled template, using windowed dynamic time warping and a z-style distance, without reading a keyboard or storing a character.

## 🏗️ Systems Architecture & Event Topology

`KeystrokeCadenceDynamicsVerifier.run` takes a sequence of frames. A frame is a mapping with `role` (`template` or `probe`), `dwell_ms`, and `flight_ms`. Both vectors are durations in milliseconds that the caller already measured for an enrolled user. The module does not read an input device, install a hook, or store a character.

Every frame in the batch is checked before any template write. A negative duration, a non-finite duration, or a duration above 2000 ms raises `EngineKernelException` and leaves the in-process cache unchanged. Keys that would carry a PAN or a character (`pan`, `characters`, `text`, `keystroke`, and the same family) raise the same way.

A `template` frame replaces the cache only when both vectors have non-zero variance. A `probe` frame never writes the cache. Length mismatch returns `decision="reject"` and `reason="length"`. A zero-variance probe returns `decision="reject"` and `reason="degenerate"`. Otherwise dynamic time warping compares probe dwell with template dwell and probe flight with template flight. Thresholds on that cost select `accept`, `step_up`, or `reject`.

The returned dict carries `decision`, `dtw_cost`, and `z_distance`. The struct record is timings-free: decision code, binary32 cost bits, dwell length, flight length. Those bytes are appended to an in-process spool. The log line names the Kafka topic `commerce.cadence.decision`. This process does not open a broker. The template cache stays in the process.

PCI-DSS alignment here means the record has no primary account number and no keystroke characters. It is not an assessment of this process.

## 📊 Core Visual Walkthrough & Engine Pipeline Flow

```
caller-supplied dwell_ms[], flight_ms[]   (no characters)
        |
        v
 clock guard ---- negative or > 2000 ms --> EngineKernelException
                  (template cache unchanged)
        |
        +-- role=template, variance > 0 --> cache write, in process
        |
        v
 role=probe
        |
        +-- len != template -------- decision=reject reason=length
        +-- pstdev ~ 0 ------------- decision=reject reason=degenerate
        |
        v
 DTW |a-b| inside a Sakoe-Chiba band, dwell and flight
 z-distance from template fmean and pstdev
        |
        +-- cost <= accept --> accept
        +-- cost <= step ----> step_up
        +-- else ------------> reject
        v
 struct record: decision code, cost bits, lengths
 topic name: commerce.cadence.decision
```

Insert the structural terminal walkthrough recording at docs/assets/terminal-walkthrough.gif before publishing the release notes.

## ⚡ Low-Level OS Mechanics & Network Physics

`run` holds one `asyncio.Lock` around staging and the cache update. Staging yields once with `asyncio.sleep(0)` so the lock is an explicit critical section on the event loop, not a thread. There is no `/dev/input` open, no evdev handle, and no keyboard hook. The only stored features are the two millisecond tuples in the process, and the only bytes spooled are the 9-byte decision record.

`dtw_distance` is the textbook recurrence. The local cost is the absolute difference. The Sakoe-Chiba band (default 4) limits `|i - j|`. Two rolling rows of length `n + 1` are the whole matrix, so the extra memory is one probe, not an `n` by `m` table. The reported `dtw_cost` is the mean of the two channel costs after each raw path cost is divided by that channel's probe length. A constant offset whose optimal path is the diagonal therefore reports the offset in milliseconds.

`z_distance` is the mean of two channel scores. Each channel score is `statistics.fmean` of `|probe - template_mean| / template_pstdev`, using `statistics.pstdev` (population). The decision does not threshold `z_distance`. It is the second number a reviewer can compare with the warp cost. The struct record stores the warp cost as IEEE-754 binary32 bits (`>BIHH`), never the millisecond vector.

## ⚖️ Architecture Trade-offs & Pragmatic Decisions

A neural keystroke model would score free text and would also need the characters this verifier is forbidden to store. The distance is a pair of scalars against an enrollment template of millisecond numbers. `accept_cost` (default 8 ms) and `step_cost` (default 30 ms) are constructor arguments. They are the policy. They are not learned on a hidden corpus inside this process.

Length mismatch is a typed reject even though DTW can align unequal lengths. The cost is still computed, with the band widened just enough for a path to exist, and the decision stays `reject` / `length`. Accepting a short probe would reward a vector that dropped the inconvenient intervals. A zero-variance probe is rejected as `degenerate` even when a warp cost would have fallen under the accept threshold, because a constant vector is not a human cadence. A zero-variance template is refused with `EngineKernelException` and is not stored.

2000 ms is inside the human bound. Anything above it is a pause, not a dwell or a flight, and it raises before a distance is computed.

## 🚀 Local Installation & Benchmarking

```bash
python3 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
python -m keystroke_cadence_dynamics_verifier
python -m keystroke_cadence_dynamics_verifier.harness
```

```python
import asyncio

from keystroke_cadence_dynamics_verifier import KeystrokeCadenceDynamicsVerifier


async def demo() -> None:
    verifier = KeystrokeCadenceDynamicsVerifier()
    dwell = [80.0, 120.0, 95.0, 110.0, 88.0, 130.0, 102.0, 115.0]
    flight = [40.0, 55.0, 35.0, 60.0, 42.0, 58.0, 37.0, 50.0]
    await verifier.run(
        [
            {"role": "template", "dwell_ms": dwell, "flight_ms": flight},
            {"role": "probe", "dwell_ms": dwell, "flight_ms": flight},
        ]
    )


asyncio.run(demo())
```

The runtime is the Python 3.12 standard library. `pip install -r requirements.txt` succeeds with no third-party pins. The vectors above are durations in milliseconds, not keys. `black==24.8.0` and `flake8==7.1.1` live in the `dev` extra.

## 🖥️ Terminal Diagnostic Output Preview

```
2026-10-02T02:58:26+0000 INFO [keystroke_cadence_dynamics_verifier] topic=commerce.cadence.decision decision=accept reason=enrolled dtw_cost=0.0000 z_distance=0.0000
2026-10-02T02:58:26+0000 INFO [keystroke_cadence_dynamics_verifier] topic=commerce.cadence.decision decision=accept reason=match dtw_cost=0.0000 z_distance=0.9239
2026-10-02T02:58:26+0000 WARNING [keystroke_cadence_dynamics_verifier] topic=commerce.cadence.decision decision=reject reason=degenerate dtw_cost=19.3125 z_distance=0.5716
2026-10-02T02:58:26+0000 INFO [keystroke_cadence_dynamics_verifier] demo complete decision=accept dtw_cost=0.0000 robotic=degenerate
```

`python -m keystroke_cadence_dynamics_verifier` exits 0. The accept path and the zero-variance rejection are both in that run. No character and no millisecond vector is logged.

## 📊 Empirical Benchmarking Performance Report

Measured by `python -m keystroke_cadence_dynamics_verifier.harness` with seed 4401, 5000 iterations after a warmup of 20, `time.perf_counter_ns` latency in microseconds, and `tracemalloc` peak. The probe is an exact copy of the seeded template, so the decision is `accept` and the DTW cost is 0.

```
status=ok seed=4401 iterations=5000 latency_us=1369.155 memory_peak_bytes=263796 benchmark_avg_us=1440.577 benchmark_p99_us=1657.253
```

| Metric | Measured |
| --- | ---: |
| Status | ok |
| Seed | 4401 |
| Iterations | 5000 |
| latency_us | 1369.155 |
| memory_peak_bytes | 263796 |
| benchmark_avg_us | 1440.577 |
| benchmark_p99_us | 1657.253 |

## 🛡️ Edge-Case Resilience & SOC2/Regulatory Compliance

A probe whose dwell or flight length does not match the template is rejected with `reason="length"` and does not replace the template. A zero-variance probe is rejected with `reason="degenerate"` and does not replace the template. A negative duration or a duration above 2000 ms raises `EngineKernelException` before a template write. A character string, and a record that carries a `pan` field, raise the same way. A duration of exactly 2000 ms is inside the bound.

The design is aligned with PCI-DSS constraints on authentication data: no PAN, no keystroke characters, no cardholder data in the decision bytes. SOC 2 processing integrity is the rule that a rejected probe does not rewrite the enrollment template. The module does not capture keystrokes. The caller supplies millisecond vectors already collected for an enrolled user. The Kafka topic `commerce.cadence.decision` is the name on the in-process spool, not a connection.
