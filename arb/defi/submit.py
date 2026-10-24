"""Submission is disabled. Simulation only: nothing in arb/defi signs or sends a transaction."""
from __future__ import annotations

from typing import NoReturn


class SubmissionDisabled(RuntimeError):
    pass


def submit(*args, **kwargs) -> NoReturn:
    raise SubmissionDisabled("Simulation only: sending transactions is out of scope. "
                             "A new explicit decision by the user is required.")
