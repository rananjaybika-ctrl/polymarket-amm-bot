"""
Backward compatibility — re-exports from spike_strategy.py.

Renamed: phoenix.py → spike_strategy.py (Feb 19, 2026)
All new code should import from src.strategies.spike_strategy directly.
"""
from src.strategies.spike_strategy import *  # noqa: F401,F403 — re-export everything
