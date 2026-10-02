"""Keystroke cadence dynamics verifier.

Callers pass dwell and flight vectors they already measured, in
milliseconds. This package does not capture keystrokes, read an input
device, or store a character.
"""

from __future__ import annotations

from .engine import KeystrokeCadenceDynamicsVerifier
from .exceptions import EngineKernelException

__all__ = [
    EngineKernelException.__name__,
    KeystrokeCadenceDynamicsVerifier.__name__,
]
__version__ = "1.0.0"
