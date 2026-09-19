"""A benchmark for adapting to real API drift.

The point of this package is to make a claim about the agent falsifiable. It
mines drift events out of a provider's real version history, labels them without
human annotation, and scores any solver against them — including deliberately
naive baselines, so a number means something.
"""

__all__ = ["cases", "mine", "solvers", "score"]
