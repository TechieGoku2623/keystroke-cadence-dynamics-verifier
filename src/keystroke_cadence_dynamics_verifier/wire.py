"""Timings-free decision record.

Layout, 9 bytes: uint8 decision code, uint32 IEEE-754 binary32 cost bits,
uint16 dwell length, uint16 flight length. The record has no millisecond
vector and no character field.
"""

from __future__ import annotations

import struct

from .exceptions import EngineKernelException

DECISION_FORMAT = ">BIHH"
DECISION_CODE = {
    "accept": 1,
    "step_up": 2,
    "reject": 3,
}
CODE_DECISION = {code: name for name, code in DECISION_CODE.items()}


def pack_decision(
    decision: str, dtw_cost: float, dwell_len: int, flight_len: int
) -> bytes:
    """Pack a decision. ``dtw_cost`` is stored as binary32 bits, not as text."""
    if decision not in DECISION_CODE:
        raise EngineKernelException("unknown cadence decision")
    if dwell_len < 0 or flight_len < 0 or dwell_len > 65535 or flight_len > 65535:
        raise EngineKernelException("cadence length does not fit in uint16")
    cost_bits = struct.unpack(">I", struct.pack(">f", float(dtw_cost)))[0]
    return struct.pack(
        DECISION_FORMAT,
        DECISION_CODE[decision],
        cost_bits,
        int(dwell_len),
        int(flight_len),
    )


def unpack_decision(payload: bytes) -> tuple[int, int, int, int]:
    """Inverse of :func:`pack_decision`. Returns code, cost bits, and lengths."""
    expected = struct.calcsize(DECISION_FORMAT)
    if len(payload) != expected:
        raise EngineKernelException("cadence decision record length is not 9 bytes")
    code, cost_bits, dwell_len, flight_len = struct.unpack(DECISION_FORMAT, payload)
    return int(code), int(cost_bits), int(dwell_len), int(flight_len)
