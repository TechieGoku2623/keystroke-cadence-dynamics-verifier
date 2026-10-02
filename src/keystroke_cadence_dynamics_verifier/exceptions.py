"""Kernel faults for the cadence verifier.

A negative duration, a duration above 2000 ms, a character vector, or a
primary account number raises ``EngineKernelException`` before the template
cache is written. The verifier never reads an input device.
"""

from __future__ import annotations


class EngineKernelException(Exception):
    """A clock or type fault. The in-process template is left unchanged."""
