"""Roundtrip, accept/step-up, length reject, and clock/degenerate guards."""

from __future__ import annotations

import asyncio
import statistics
import struct
import unittest

from keystroke_cadence_dynamics_verifier import (
    EngineKernelException,
    KeystrokeCadenceDynamicsVerifier,
)
from keystroke_cadence_dynamics_verifier.wire import pack_decision, unpack_decision

_DWELL = (50.0, 250.0, 450.0, 650.0, 850.0, 1050.0, 1250.0, 1450.0)
_FLIGHT = (40.0, 240.0, 440.0, 640.0, 840.0, 1040.0, 1240.0, 1440.0)


def _shift(values: tuple[float, ...], delta: float) -> list[float]:
    return [value + delta for value in values]


def _channel_z(template: tuple[float, ...], probe: tuple[float, ...]) -> float:
    center = statistics.fmean(template)
    spread = statistics.pstdev(template)
    return statistics.fmean(abs((value - center) / spread) for value in probe)


class CadenceEngineTest(unittest.TestCase):
    def test_decision_roundtrip(self) -> None:
        payload = pack_decision("step_up", 15.0, 8, 8)
        code, cost_bits, dwell_len, flight_len = unpack_decision(payload)
        restored = struct.unpack(">f", struct.pack(">I", cost_bits))[0]
        self.assertEqual(len(payload), 9)
        self.assertEqual(code, 2)
        self.assertEqual((dwell_len, flight_len), (8, 8))
        self.assertAlmostEqual(restored, 15.0, places=6)
        self.assertNotIn(struct.pack(">d", _DWELL[0]), payload)
        reject = pack_decision("reject", 0.0, 3, 4)
        self.assertEqual(unpack_decision(reject)[0], 3)

    def test_happy_path_maps_dtw_cost(self) -> None:
        engine = KeystrokeCadenceDynamicsVerifier()
        matched = asyncio.run(
            engine.run(
                [
                    {"role": "template", "dwell_ms": _DWELL, "flight_ms": _FLIGHT},
                    {"role": "probe", "dwell_ms": _DWELL, "flight_ms": _FLIGHT},
                ]
            )
        )
        stepped = asyncio.run(
            engine.run(
                [
                    {
                        "role": "probe",
                        "dwell_ms": _shift(_DWELL, 15.0),
                        "flight_ms": _shift(_FLIGHT, 15.0),
                    }
                ]
            )
        )
        mismatched = asyncio.run(
            engine.run(
                [
                    {
                        "role": "probe",
                        "dwell_ms": _shift(_DWELL, 80.0),
                        "flight_ms": _shift(_FLIGHT, 80.0),
                    }
                ]
            )
        )
        expected_z = statistics.fmean(
            (_channel_z(_DWELL, _DWELL), _channel_z(_FLIGHT, _FLIGHT))
        )
        self.assertEqual(matched["decision"], "accept")
        self.assertEqual(matched["reason"], "match")
        self.assertAlmostEqual(matched["dtw_cost"], 0.0, places=9)
        self.assertAlmostEqual(matched["z_distance"], expected_z, places=9)
        self.assertEqual(stepped["decision"], "step_up")
        self.assertAlmostEqual(stepped["dtw_cost"], 15.0, places=9)
        self.assertEqual(mismatched["decision"], "reject")
        self.assertEqual(mismatched["reason"], "mismatch")
        self.assertGreater(mismatched["dtw_cost"], 30.0)
        code, _bits, dwell_len, flight_len = unpack_decision(matched["record"])
        self.assertEqual(code, 1)
        self.assertEqual((dwell_len, flight_len), (len(_DWELL), len(_FLIGHT)))
        self.assertNotIn("dwell_ms", matched)
        self.assertEqual(matched["topic"], "commerce.cadence.decision")

    def test_length_mismatch_rejects_without_replacing_template(self) -> None:
        engine = KeystrokeCadenceDynamicsVerifier()
        short = asyncio.run(
            engine.run(
                [
                    {"role": "template", "dwell_ms": _DWELL, "flight_ms": _FLIGHT},
                    {
                        "role": "probe",
                        "dwell_ms": _DWELL[:-1],
                        "flight_ms": _FLIGHT,
                    },
                ]
            )
        )
        self.assertEqual(short["decision"], "reject")
        self.assertEqual(short["reason"], "length")
        self.assertGreaterEqual(short["dtw_cost"], 0.0)
        again = asyncio.run(
            engine.run([{"role": "probe", "dwell_ms": _DWELL, "flight_ms": _FLIGHT}])
        )
        self.assertEqual(again["decision"], "accept")
        self.assertAlmostEqual(again["dtw_cost"], 0.0, places=9)

    def test_degenerate_probe_and_clock_fault_leave_template(self) -> None:
        engine = KeystrokeCadenceDynamicsVerifier()
        bounded = list(_DWELL)
        bounded[0] = 2000.0
        at_bound = asyncio.run(
            engine.run(
                [
                    {"role": "template", "dwell_ms": bounded, "flight_ms": _FLIGHT},
                    {"role": "probe", "dwell_ms": bounded, "flight_ms": _FLIGHT},
                ]
            )
        )
        self.assertEqual(at_bound["decision"], "accept")
        asyncio.run(
            engine.run([{"role": "template", "dwell_ms": _DWELL, "flight_ms": _FLIGHT}])
        )
        negative = [-1.0, *_DWELL[1:]]
        with self.assertRaises(EngineKernelException):
            asyncio.run(
                engine.run(
                    [{"role": "template", "dwell_ms": negative, "flight_ms": _FLIGHT}]
                )
            )
        with self.assertRaises(EngineKernelException):
            asyncio.run(
                engine.run(
                    [
                        {
                            "role": "probe",
                            "dwell_ms": [2000.1] * len(_DWELL),
                            "flight_ms": _FLIGHT,
                        }
                    ]
                )
            )
        with self.assertRaises(EngineKernelException):
            asyncio.run(
                engine.run(
                    [
                        {
                            "role": "probe",
                            "dwell_ms": "abcdefgh",
                            "flight_ms": _FLIGHT,
                        }
                    ]
                )
            )
        with self.assertRaises(EngineKernelException):
            asyncio.run(
                engine.run(
                    [
                        {
                            "role": "template",
                            "dwell_ms": _DWELL,
                            "flight_ms": _FLIGHT,
                            "pan": "4111111111111111",
                        }
                    ]
                )
            )
        still = asyncio.run(
            engine.run([{"role": "probe", "dwell_ms": _DWELL, "flight_ms": _FLIGHT}])
        )
        self.assertEqual(still["decision"], "accept")
        flat = asyncio.run(
            engine.run(
                [
                    {
                        "role": "probe",
                        "dwell_ms": [95.0] * len(_DWELL),
                        "flight_ms": [45.0] * len(_FLIGHT),
                    }
                ]
            )
        )
        self.assertEqual(flat["decision"], "reject")
        self.assertEqual(flat["reason"], "degenerate")
        unchanged = asyncio.run(
            engine.run([{"role": "probe", "dwell_ms": _DWELL, "flight_ms": _FLIGHT}])
        )
        self.assertEqual(unchanged["decision"], "accept")
        self.assertAlmostEqual(unchanged["dtw_cost"], 0.0, places=9)


if __name__ == "__main__":
    unittest.main()
