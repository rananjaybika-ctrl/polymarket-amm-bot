"""
Gabagool Neural Network - Reverse engineering passive grid market making.

This module trains neural networks to learn gabagool's passive two-sided
grid market making behavior from observer data.

Data Splits:
- Training: IS+OOS2 (Jan 16-19) + OOS5 (Jan 26)
- Validation: OOS3+OOS4 (Jan 20-24) + OOS6 (Jan 28-29)
"""

__version__ = "0.1.0"
