"""Score one matching probe and one zero-variance probe."""

from __future__ import annotations

import asyncio
import logging
import sys

from .engine import KeystrokeCadenceDynamicsVerifier
from .exceptions import EngineKernelException

_DWELL = (100.0, 140.0, 80.0, 160.0, 90.0, 150.0, 110.0, 130.0)
_FLIGHT = (40.0, 55.0, 35.0, 60.0, 42.0, 58.0, 37.0, 50.0)


async def _demo() -> tuple[dict[str, object], dict[str, object]]:
    engine = KeystrokeCadenceDynamicsVerifier()
    accepted = await engine.run(
        [
            {"role": "template", "dwell_ms": _DWELL, "flight_ms": _FLIGHT},
            {"role": "probe", "dwell_ms": _DWELL, "flight_ms": _FLIGHT},
        ]
    )
    rejected = await engine.run(
        [
            {
                "role": "probe",
                "dwell_ms": (95.0,) * len(_DWELL),
                "flight_ms": (45.0,) * len(_FLIGHT),
            }
        ]
    )
    return accepted, rejected


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    logger = logging.getLogger("keystroke_cadence_dynamics_verifier")
    try:
        accepted, rejected = asyncio.run(_demo())
    except EngineKernelException:
        logger.exception("demo cadence rejected by the clock guard")
        return 1
    if accepted["decision"] != "accept" or rejected["reason"] != "degenerate":
        logger.error("demo cadence did not accept the template and reject the robot")
        return 1
    logger.info(
        "demo complete decision=%s dtw_cost=%.4f robotic=%s",
        accepted["decision"],
        float(accepted["dtw_cost"]),
        rejected["reason"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
