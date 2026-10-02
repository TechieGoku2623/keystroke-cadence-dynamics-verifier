"""Compare caller-supplied dwell and flight times for an enrolled subject.

The caller already collected the timing tuples from an opted-in user. This
module does not read input devices, install keyboard hooks, log characters, or
accept another person's key stream. Stored features are milliseconds. Decision
bytes contain no text. The design is aligned with PCI-DSS constraints on
authentication data (no PAN, no keystroke characters) and with SOC 2
processing integrity: a rejected probe does not rewrite the template.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
import sys
from collections import deque
from collections.abc import Sequence

DECISION_TOPIC = "commerce.cadence.decision"
HUMAN_BOUND_MS = 2000.0
MIN_FEATURES = 8
CACHE_LIMIT = 256
TOKEN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"

DECISION_ACCEPT = "accept"
DECISION_STEP_UP = "step_up"
DECISION_REJECT = "reject"

_DECISION_CODE = {
    DECISION_ACCEPT: 1,
    DECISION_STEP_UP: 2,
    DECISION_REJECT: 3,
}
_REASON_CODE = {
    "match": 1,
    "marginal": 2,
    "mismatch": 3,
    "truncated_probe": 4,
    "robotic_zero_variance": 5,
    "not_enrolled": 6,
    "enrolled": 7,
}

__all__ = [
    "DECISION_TOPIC",
    "CadenceDynamicsVerifier",
    "EngineKernelException",
    "HUMAN_BOUND_MS",
    "configure_logging",
    "main",
]


class EngineKernelException(Exception):
    """A clock or type fault. The template is left untouched."""


def configure_logging() -> None:
    """Install a timestamped handler once, if the process has none yet."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def _guard_subject(subject_id: object) -> str:
    if not isinstance(subject_id, str):
        raise EngineKernelException("subject id must be an opaque token")
    if not subject_id or len(subject_id) > 64:
        raise EngineKernelException("subject id length is outside the token bound")
    for char in subject_id:
        if char not in TOKEN_ALPHABET:
            raise EngineKernelException("subject id must be an opaque token")
    return subject_id


def _one_duration(item: object, human_bound_ms: float) -> float:
    if isinstance(item, bool) or not isinstance(item, (int, float)):
        raise EngineKernelException("cadence features must be numeric milliseconds")
    value = float(item)
    if not math.isfinite(value):
        raise EngineKernelException("non-finite cadence duration")
    if value < 0.0 or value > human_bound_ms:
        raise EngineKernelException("duration outside human clock bound")
    return value


def coerce_durations(
    features: Sequence[float], human_bound_ms: float, minimum: int
) -> tuple[float, ...]:
    """Copy a caller-supplied millisecond vector. Characters are rejected."""
    if isinstance(features, (str, bytes)) or not isinstance(features, Sequence):
        raise EngineKernelException("character input is not a cadence feature")
    values = [_one_duration(item, human_bound_ms) for item in features]
    if len(values) < minimum:
        raise EngineKernelException(
            "cadence vector shorter than the minimum feature count"
        )
    return tuple(values)


def cadence_distance(
    template: Sequence[float], probe: Sequence[float]
) -> tuple[float, float]:
    """Normalized absolute deviation plus a z-style distance.

    Both statistics are taken with the population formulas. The template mean
    is the denominator of the normalized deviation so a slow typist and a fast
    typist are scored on relative error, not raw milliseconds.
    """
    deviations = [abs(probe[index] - template[index]) for index in range(len(template))]
    centered = [probe[index] - template[index] for index in range(len(template))]
    baseline = abs(statistics.mean(template))
    nad = statistics.mean(deviations) / max(baseline, 1.0e-9)
    spread = statistics.pstdev(template)
    z_distance = abs(statistics.mean(centered)) / max(spread, 1.0e-9)
    return nad, z_distance


def classify_distance(
    nad: float,
    z_distance: float,
    accept_nad: float,
    accept_z: float,
    step_nad: float,
    step_z: float,
) -> tuple[str, str]:
    if nad <= accept_nad and z_distance <= accept_z:
        return DECISION_ACCEPT, "match"
    if nad <= step_nad and z_distance <= step_z:
        return DECISION_STEP_UP, "marginal"
    return DECISION_REJECT, "mismatch"


def pack_decision(
    decision: str, reason: str, nad: float, z_distance: float, count: int
) -> bytes:
    """Pack a decision record. The layout has no character field."""
    return struct.pack(
        ">BBffHH",
        1,
        _DECISION_CODE[decision],
        nad,
        z_distance,
        count,
        _REASON_CODE[reason],
    )


class CadenceDynamicsVerifier:
    """Process-local template cache and an in-process decision queue.

    Templates live in this process on purpose. A Redis cache would put
    millisecond features on the network. ``_topic`` is the buffer a producer
    for Kafka topic ``commerce.cadence.decision`` would drain; this module
    does not open a broker connection.
    """

    def __init__(
        self,
        human_bound_ms: float = HUMAN_BOUND_MS,
        accept_nad: float = 0.08,
        accept_z: float = 0.50,
        step_nad: float = 0.30,
        step_z: float = 1.50,
        minimum_features: int = MIN_FEATURES,
        cache_limit: int = CACHE_LIMIT,
    ) -> None:
        configure_logging()
        self._human_bound_ms = _require_positive("human_bound_ms", human_bound_ms)
        self._accept_nad = _require_positive("accept_nad", accept_nad)
        self._accept_z = _require_positive("accept_z", accept_z)
        self._step_nad = _require_positive("step_nad", step_nad)
        self._step_z = _require_positive("step_z", step_z)
        if self._step_nad < self._accept_nad or self._step_z < self._accept_z:
            raise EngineKernelException("step-up thresholds must sit above accept")
        if isinstance(minimum_features, bool) or not isinstance(minimum_features, int):
            raise EngineKernelException("minimum_features must be an integer")
        if minimum_features < 2:
            raise EngineKernelException("minimum_features must be at least 2")
        if isinstance(cache_limit, bool) or not isinstance(cache_limit, int):
            raise EngineKernelException("cache_limit must be an integer")
        if cache_limit < 1:
            raise EngineKernelException("cache_limit must be at least 1")
        self._minimum = minimum_features
        self._cache_limit = cache_limit
        self._templates: dict[str, tuple[float, ...]] = {}
        self._order: deque[str] = deque()
        self._spool: deque[bytes] = deque(maxlen=1024)
        self._lock = asyncio.Lock()
        self._jobs: asyncio.Queue[
            tuple[str, str, Sequence[float], asyncio.Future[dict[str, object]]]
        ] = asyncio.Queue(maxsize=256)
        self._topic: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self._worker: asyncio.Task[None] | None = None
        self._publisher: asyncio.Task[None] | None = None
        self._logger = logging.getLogger("cadence.kernel")

    async def _ensure(self) -> None:
        async with self._lock:
            if self._worker is None or self._worker.done():
                self._worker = asyncio.create_task(
                    self._consume(), name="cadence-worker"
                )
            if self._publisher is None or self._publisher.done():
                self._publisher = asyncio.create_task(
                    self._publish_loop(), name="cadence-decision-spool"
                )

    async def close(self) -> None:
        tasks = [
            task
            for task in (self._worker, self._publisher)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                continue
        self._worker = None
        self._publisher = None

    async def enroll(
        self, subject_id: str, features_ms: Sequence[float]
    ) -> dict[str, object]:
        """Store a millisecond template. A clock fault leaves the cache as-is."""
        return await self._submit("enroll", subject_id, features_ms)

    async def verify(
        self, subject_id: str, probe_ms: Sequence[float]
    ) -> dict[str, object]:
        """Score a probe. Verify never writes the template."""
        return await self._submit("verify", subject_id, probe_ms)

    async def _submit(
        self, kind: str, subject_id: str, features: Sequence[float]
    ) -> dict[str, object]:
        await self._ensure()
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        await self._jobs.put((kind, subject_id, features, future))
        return await future

    async def _consume(self) -> None:
        while True:
            job = await self._jobs.get()
            try:
                await self._settle(job)
            finally:
                self._jobs.task_done()

    async def _settle(
        self,
        job: tuple[str, str, Sequence[float], asyncio.Future[dict[str, object]]],
    ) -> None:
        kind, subject_id, features, future = job
        try:
            result, record = await self._evaluate(kind, subject_id, features)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if record is not None:
            await self._topic.put(record)
        if not future.done():
            future.set_result(result)

    async def _publish_loop(self) -> None:
        while True:
            record = await self._topic.get()
            try:
                self._spool.append(record)
            finally:
                self._topic.task_done()

    async def _evaluate(
        self, kind: str, subject_id: str, features: Sequence[float]
    ) -> tuple[dict[str, object], bytes | None]:
        if kind == "enroll":
            async with self._lock:
                return self._enroll(subject_id, features), None
        if kind == "verify":
            async with self._lock:
                result, record = self._verify(subject_id, features)
            return result, record
        raise EngineKernelException("unknown cadence job")

    def _enroll(self, subject_id: str, features: Sequence[float]) -> dict[str, object]:
        token = _guard_subject(subject_id)
        values = coerce_durations(features, self._human_bound_ms, self._minimum)
        if statistics.pstdev(values) <= 1.0e-9:
            raise EngineKernelException(
                "zero-variance cadence is not a human enrollment"
            )
        self._remember(token, values)
        record = pack_decision(DECISION_ACCEPT, "enrolled", 0.0, 0.0, len(values))
        self._logger.info("template stored features=%s", len(values))
        return {
            "status": "enrolled",
            "decision": DECISION_ACCEPT,
            "reason": "enrolled",
            "nad": 0.0,
            "z_distance": 0.0,
            "feature_count": len(values),
            "template_updated": True,
            "record_hex": record.hex(),
            "topic": None,
        }

    def _verify(
        self, subject_id: str, features: Sequence[float]
    ) -> tuple[dict[str, object], bytes]:
        token = _guard_subject(subject_id)
        probe = coerce_durations(features, self._human_bound_ms, self._minimum)
        template = self._templates.get(token)
        if template is None:
            return self._decision(DECISION_REJECT, "not_enrolled", 0.0, 0.0, len(probe))
        if len(probe) != len(template):
            return self._decision(
                DECISION_REJECT, "truncated_probe", 0.0, 0.0, len(probe)
            )
        if statistics.pstdev(probe) <= 1.0e-9:
            return self._decision(
                DECISION_REJECT, "robotic_zero_variance", 0.0, 0.0, len(probe)
            )
        nad, z_distance = cadence_distance(template, probe)
        decision, reason = classify_distance(
            nad,
            z_distance,
            self._accept_nad,
            self._accept_z,
            self._step_nad,
            self._step_z,
        )
        return self._decision(decision, reason, nad, z_distance, len(probe))

    def _decision(
        self,
        decision: str,
        reason: str,
        nad: float,
        z_distance: float,
        count: int,
    ) -> tuple[dict[str, object], bytes]:
        record = pack_decision(decision, reason, nad, z_distance, count)
        if decision == DECISION_REJECT:
            self._logger.warning(
                "cadence rejected reason=%s nad=%.4f z=%.4f",
                reason,
                nad,
                z_distance,
            )
        result = {
            "status": "decision",
            "decision": decision,
            "reason": reason,
            "nad": nad,
            "z_distance": z_distance,
            "feature_count": count,
            "template_updated": False,
            "record_hex": record.hex(),
            "topic": DECISION_TOPIC,
        }
        return result, record

    def _remember(self, subject_id: str, values: tuple[float, ...]) -> None:
        if (
            subject_id not in self._templates
            and len(self._templates) >= self._cache_limit
        ):
            oldest = self._order.popleft()
            self._templates.pop(oldest, None)
        if subject_id not in self._order:
            self._order.append(subject_id)
        self._templates[subject_id] = values


def _require_positive(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineKernelException(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise EngineKernelException(f"{name} must be a positive finite value")
    return number


def _demo_template() -> tuple[float, ...]:
    return (
        118.0,
        64.0,
        126.0,
        71.0,
        110.0,
        80.0,
        133.0,
        59.0,
        121.0,
        77.0,
        115.0,
        69.0,
        128.0,
        74.0,
        112.0,
        83.0,
    )


async def _demo() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    verifier = CadenceDynamicsVerifier()
    template = _demo_template()
    probe = tuple(value + 0.4 for value in template)
    robotic = tuple(95.0 for _ in template)
    try:
        enrolled = await verifier.enroll("acct-4401", template)
        accepted = await verifier.verify("acct-4401", probe)
        rejected = await verifier.verify("acct-4401", robotic)
        return enrolled, accepted, rejected
    finally:
        await verifier.close()


def main() -> int:
    configure_logging()
    logger = logging.getLogger("cadence.kernel")
    try:
        enrolled, accepted, rejected = asyncio.run(_demo())
    except EngineKernelException:
        logger.exception("demo cadence rejected by the clock guard")
        return 1
    logger.info(
        "demo complete enroll=%s decision=%s nad=%.4f robotic=%s",
        enrolled["status"],
        accepted["decision"],
        float(accepted["nad"]),
        rejected["reason"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
