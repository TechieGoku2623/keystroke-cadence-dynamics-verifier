"""Score caller-supplied dwell and flight times against an enrolled template.

Both vectors are milliseconds. Dynamic time warping uses an absolute local
cost and a Sakoe-Chiba window. A z-style distance uses the template mean
and population standard deviation. Thresholds on the DTW cost select
accept, step-up, or reject.

The template cache stays in this process. Decision bytes are timings-free
and are the payload a producer for ``commerce.cadence.decision`` would
drain. This module does not open a broker, read an input device, store a
character, or accept a primary account number. PCI-DSS alignment here means
those fields are absent, not that this process was assessed.
"""

from __future__ import annotations

import asyncio
import logging
import math
import statistics
import struct
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import wire
from .exceptions import EngineKernelException

DECISION_TOPIC = "commerce.cadence.decision"
_HUMAN_BOUND_MS = 2000.0
_VARIANCE_FLOOR = 1.0e-9
_FORBIDDEN_KEYS = frozenset(
    {
        "pan",
        "primary_account_number",
        "card",
        "character",
        "characters",
        "text",
        "keystroke",
        "keys",
    }
)
_LOGGER = logging.getLogger("keystroke_cadence_dynamics_verifier")
_LOGGER.addHandler(logging.NullHandler())


@dataclass(frozen=True, slots=True)
class _Frame:
    role: str
    dwell: tuple[float, ...]
    flight: tuple[float, ...]


def dtw_distance(left: Sequence[float], right: Sequence[float], band: int) -> float:
    """Classic DTW with an absolute local cost and a Sakoe-Chiba band.

    ``band`` is the maximum ``|i - j|`` allowed on the warping path. The
    returned value is the raw path cost, not a per-step mean.
    """
    if isinstance(band, bool) or not isinstance(band, int) or band < 0:
        raise EngineKernelException("dtw band must be a non-negative integer")
    rows = len(left)
    cols = len(right)
    if rows == 0 or cols == 0:
        raise EngineKernelException("cadence vector is empty")
    inf = math.inf
    previous = [inf] * (cols + 1)
    previous[0] = 0.0
    for i in range(1, rows + 1):
        current = [inf] * (cols + 1)
        j_lo = max(1, i - band)
        j_hi = min(cols, i + band)
        for j in range(j_lo, j_hi + 1):
            local = abs(left[i - 1] - right[j - 1])
            current[j] = local + min(previous[j], current[j - 1], previous[j - 1])
        previous = current
    total = previous[cols]
    if math.isinf(total):
        raise EngineKernelException("dtw window cannot reach the probe")
    return total


def _require_positive(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EngineKernelException(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise EngineKernelException(f"{name} must be a positive finite value")
    return number


def _channel_z(template: Sequence[float], probe: Sequence[float]) -> float:
    center = statistics.fmean(template)
    spread = statistics.pstdev(template)
    if spread <= 1.0e-12:
        return 0.0
    return statistics.fmean(abs((value - center) / spread) for value in probe)


class KeystrokeCadenceDynamicsVerifier:
    """Process-local template and a timings-free decision record.

    ``run`` accepts a sequence of frames. A ``template`` frame replaces the
    cache only after every frame in the batch has passed the clock guard.
    A ``probe`` frame never writes the cache. Length mismatch and a
    zero-variance probe are typed rejects, not exceptions.
    """

    def __init__(
        self,
        accept_cost: float = 8.0,
        step_cost: float = 30.0,
        band: int = 4,
        human_bound_ms: float = _HUMAN_BOUND_MS,
    ) -> None:
        self._accept_cost = _require_positive("accept_cost", accept_cost)
        self._step_cost = _require_positive("step_cost", step_cost)
        if self._step_cost < self._accept_cost:
            raise EngineKernelException("step-up cost must sit at or above accept")
        if isinstance(band, bool) or not isinstance(band, int) or band < 0:
            raise EngineKernelException("band must be a non-negative integer")
        self._band = band
        self._human_bound_ms = _require_positive("human_bound_ms", human_bound_ms)
        self._dwell: tuple[float, ...] | None = None
        self._flight: tuple[float, ...] | None = None
        self._lock = asyncio.Lock()
        self._spool: deque[bytes] = deque(maxlen=64)
        self._logger = _LOGGER

    async def run(self, records: Sequence[object]) -> dict[str, object]:
        """Enroll or score frames. The returned dict is the last frame."""
        async with self._lock:
            frames = await self._stage(records)
            result: dict[str, object] | None = None
            for frame in frames:
                if frame.role == "template":
                    self._store(frame)
                    result = self._emit("accept", "enrolled", 0.0, 0.0, frame)
                else:
                    result = self._verify(frame)
            if result is None:
                raise EngineKernelException("cadence batch is empty")
            return result

    async def _stage(self, records: Sequence[object]) -> tuple[_Frame, ...]:
        await asyncio.sleep(0)
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise EngineKernelException("cadence records must be a sequence of frames")
        if not records:
            raise EngineKernelException("cadence batch is empty")
        return tuple(self._coerce(record) for record in records)

    def _coerce(self, record: object) -> _Frame:
        if not isinstance(record, Mapping):
            raise EngineKernelException("cadence record must be a mapping")
        forbidden = _FORBIDDEN_KEYS.intersection(record)
        if forbidden:
            raise EngineKernelException(
                "cadence record cannot carry characters or a PAN"
            )
        role = record.get("role")
        if role not in {"template", "probe"}:
            raise EngineKernelException("role must be template or probe")
        dwell = self._vector(record.get("dwell_ms"), "dwell_ms")
        flight = self._vector(record.get("flight_ms"), "flight_ms")
        return _Frame(str(role), dwell, flight)

    def _vector(self, values: object, name: str) -> tuple[float, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise EngineKernelException(f"{name} must be a millisecond vector")
        parsed: list[float] = []
        for item in values:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise EngineKernelException(f"{name} must be numeric milliseconds")
            value = float(item)
            if not math.isfinite(value):
                raise EngineKernelException(f"{name} must be finite")
            if value < 0.0 or value > self._human_bound_ms:
                raise EngineKernelException("duration outside 0..2000 ms")
            parsed.append(value)
        if not parsed:
            raise EngineKernelException(f"{name} is empty")
        return tuple(parsed)

    def _store(self, frame: _Frame) -> None:
        if (
            statistics.pstdev(frame.dwell) <= _VARIANCE_FLOOR
            or statistics.pstdev(frame.flight) <= _VARIANCE_FLOOR
        ):
            raise EngineKernelException("zero-variance template is not enrolled")
        self._dwell = frame.dwell
        self._flight = frame.flight

    def _verify(self, frame: _Frame) -> dict[str, object]:
        if self._dwell is None or self._flight is None:
            return self._emit("reject", "unenrolled", 0.0, 0.0, frame)
        if len(frame.dwell) != len(self._dwell) or len(frame.flight) != len(
            self._flight
        ):
            cost = self._dtw_cost(self._dwell, self._flight, frame)
            distance = self._z_distance(self._dwell, self._flight, frame)
            return self._emit("reject", "length", cost, distance, frame)
        if (
            statistics.pstdev(frame.dwell) <= _VARIANCE_FLOOR
            or statistics.pstdev(frame.flight) <= _VARIANCE_FLOOR
        ):
            cost = self._dtw_cost(self._dwell, self._flight, frame)
            distance = self._z_distance(self._dwell, self._flight, frame)
            return self._emit("reject", "degenerate", cost, distance, frame)
        cost = self._dtw_cost(self._dwell, self._flight, frame)
        distance = self._z_distance(self._dwell, self._flight, frame)
        decision, reason = self._classify(cost)
        return self._emit(decision, reason, cost, distance, frame)

    def _dtw_cost(
        self,
        template_dwell: Sequence[float],
        template_flight: Sequence[float],
        frame: _Frame,
    ) -> float:
        dwell_band = max(self._band, abs(len(template_dwell) - len(frame.dwell)))
        flight_band = max(self._band, abs(len(template_flight) - len(frame.flight)))
        dwell_cost = dtw_distance(template_dwell, frame.dwell, dwell_band) / len(
            frame.dwell
        )
        flight_cost = dtw_distance(template_flight, frame.flight, flight_band) / len(
            frame.flight
        )
        return statistics.fmean((dwell_cost, flight_cost))

    def _z_distance(
        self,
        template_dwell: Sequence[float],
        template_flight: Sequence[float],
        frame: _Frame,
    ) -> float:
        return statistics.fmean(
            (
                _channel_z(template_dwell, frame.dwell),
                _channel_z(template_flight, frame.flight),
            )
        )

    def _classify(self, dtw_cost: float) -> tuple[str, str]:
        if dtw_cost <= self._accept_cost:
            return "accept", "match"
        if dtw_cost <= self._step_cost:
            return "step_up", "step_up"
        return "reject", "mismatch"

    def _emit(
        self,
        decision: str,
        reason: str,
        dtw_cost: float,
        z_distance: float,
        frame: _Frame,
    ) -> dict[str, object]:
        record = wire.pack_decision(
            decision, dtw_cost, len(frame.dwell), len(frame.flight)
        )
        code, cost_bits, dwell_len, flight_len = wire.unpack_decision(record)
        restored = struct.unpack(">f", struct.pack(">I", cost_bits))[0]
        if code != wire.DECISION_CODE[decision]:
            raise EngineKernelException("decision record failed struct roundtrip")
        if dwell_len != len(frame.dwell) or flight_len != len(frame.flight):
            raise EngineKernelException("decision record failed struct roundtrip")
        if not math.isclose(restored, float(dtw_cost), rel_tol=1.0e-5, abs_tol=1.0e-5):
            raise EngineKernelException("decision cost bits failed struct roundtrip")
        self._spool.append(record)
        if decision == "reject":
            self._logger.warning(
                "topic=%s decision=%s reason=%s dtw_cost=%.4f z_distance=%.4f",
                DECISION_TOPIC,
                decision,
                reason,
                dtw_cost,
                z_distance,
            )
        else:
            self._logger.info(
                "topic=%s decision=%s reason=%s dtw_cost=%.4f z_distance=%.4f",
                DECISION_TOPIC,
                decision,
                reason,
                dtw_cost,
                z_distance,
            )
        return {
            "decision": decision,
            "dtw_cost": float(dtw_cost),
            "z_distance": float(z_distance),
            "reason": reason,
            "record": record,
            "topic": DECISION_TOPIC,
        }
