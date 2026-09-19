"""Sensors against real published API contracts, not a simulation.

`sell/environment.py` fabricates drift so the loop can be exercised end to end.
This package points the same detection layer at contracts a real provider
actually shipped, so the drift is whatever really happened between two releases.
"""

__all__ = ["openapi", "diff", "sources", "scan"]
