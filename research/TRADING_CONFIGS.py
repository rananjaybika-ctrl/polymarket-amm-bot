"""
Master Trading Configurations - VALIDATED Jan 22, 2026

These configs are validated against 81.71 hours of data across 254 markets.
Stop-out effects ARE factored into PnL calculations.

IMPORTANT: Time-based stops work DIFFERENTLY than price-based stops:
- AGGRESSIVE: 180s time-stop is BETTER (+33% PnL)
- BALANCED/CONSERVATIVE: 15% price-stop is BETTER

Usage:
    from research.TRADING_CONFIGS import AGGRESSIVE, BALANCED, CONSERVATIVE

    config = AGGRESSIVE
    # Use config['lookback_ticks'], config['stop_loss_pct'], etc.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class TradingConfig:
    """Trading configuration with all parameters."""
    name: str

    # Threshold method
    threshold_method: str  # "ou" or "ewma"

    # Z-score method
    zscore_method: str  # "ou", "ewma", "ewma_ratio", "percentile"

    # Lookback
    lookback_ticks: int  # At 60Hz
    lookback_ms: int     # Milliseconds

    # Stop settings
    stop_loss_pct: Optional[float]  # None = no price stop
    time_stop_seconds: Optional[float]  # None = no time stop

    # Cycling
    use_cycling: bool

    # Z-score filter bounds
    z_lo: Optional[float]  # None = no lower bound
    z_hi: Optional[float]  # None = no upper bound

    # Expected performance (at 5 shares, scale x10 for 50 shares)
    expected_pnl: float
    expected_hourly_rate: float
    expected_win_rate: float
    expected_trades: int
    premature_stop_pct: float
    premature_pnl_lost: float

    @property
    def z_zone_label(self) -> str:
        if self.z_lo is None and self.z_hi is None:
            return "no_limit"
        elif self.z_lo is None:
            return f"z<{self.z_hi}"
        elif self.z_hi is None:
            return f"z>{self.z_lo}"
        else:
            return f"{self.z_lo}<z<{self.z_hi}"


# =============================================================================
# VALIDATED CONFIGURATIONS (Jan 22, 2026)
# =============================================================================

AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",

    # Core settings
    threshold_method="ou",
    zscore_method="ewma",
    lookback_ticks=72,
    lookback_ms=1200,

    # STOP SETTINGS - USE 180s TIME-STOP (NOT price-stop!)
    # Time-stop gives +33% PnL improvement over 15% price-stop
    stop_loss_pct=None,         # NO price-based stop
    time_stop_seconds=180.0,    # Exit after 180s if not filled AND not in profit

    # Cycling ON for more trades
    use_cycling=True,

    # Z-score filter
    z_lo=0.0,
    z_hi=1.5,

    # Expected performance (at 5 shares)
    expected_pnl=28.95,          # With 180s time-stop
    expected_hourly_rate=0.953,  # $/hr with time-stop
    expected_win_rate=66.7,
    expected_trades=111,
    premature_stop_pct=34.5,     # Much lower than 43.9% with price-stop
    premature_pnl_lost=-5.32,    # Much better than -$15.44 with price-stop
)


BALANCED = TradingConfig(
    name="BALANCED",

    # Core settings
    threshold_method="ou",
    zscore_method="ou",
    lookback_ticks=84,
    lookback_ms=1400,

    # STOP SETTINGS - USE 15% PRICE-STOP (time-stop is worse here)
    stop_loss_pct=0.15,
    time_stop_seconds=None,  # NO time-based stop

    # Cycling ON
    use_cycling=True,

    # Z-score filter (wider lower bound)
    z_lo=-0.5,
    z_hi=1.5,

    # Expected performance (at 5 shares)
    expected_pnl=27.12,
    expected_hourly_rate=0.615,
    expected_win_rate=70.7,
    expected_trades=99,
    premature_stop_pct=37.0,
    premature_pnl_lost=-7.80,
)


CONSERVATIVE = TradingConfig(
    name="CONSERVATIVE",

    # Core settings
    threshold_method="ou",
    zscore_method="ou",
    lookback_ticks=84,
    lookback_ms=1400,

    # STOP SETTINGS - USE 15% PRICE-STOP (time-stop is worse here)
    stop_loss_pct=0.15,
    time_stop_seconds=None,  # NO time-based stop

    # Cycling OFF for fewer but higher quality trades
    use_cycling=False,

    # Z-score filter
    z_lo=0.0,
    z_hi=1.5,

    # Expected performance (at 5 shares)
    expected_pnl=20.98,
    expected_hourly_rate=0.619,
    expected_win_rate=75.0,
    expected_trades=52,
    premature_stop_pct=23.1,    # Lowest premature stop rate
    premature_pnl_lost=-1.73,   # Lowest PnL lost to premature stops
)


# =============================================================================
# ALL CONFIGS
# =============================================================================

ALL_CONFIGS = [AGGRESSIVE, BALANCED, CONSERVATIVE]


def get_config(name: str) -> TradingConfig:
    """Get config by name."""
    name_upper = name.upper()
    for config in ALL_CONFIGS:
        if config.name == name_upper:
            return config
    raise ValueError(f"Unknown config: {name}. Available: {[c.name for c in ALL_CONFIGS]}")


def print_config_summary():
    """Print summary of all configs."""
    print("=" * 100)
    print("VALIDATED TRADING CONFIGURATIONS (Jan 22, 2026)")
    print("=" * 100)

    for cfg in ALL_CONFIGS:
        print(f"\n{cfg.name}:")
        print(f"  Threshold: {cfg.threshold_method}, Z-Score: {cfg.zscore_method}")
        print(f"  Lookback: {cfg.lookback_ms}ms ({cfg.lookback_ticks} ticks)")
        print(f"  Stop: {'180s TIME' if cfg.time_stop_seconds else f'{int(cfg.stop_loss_pct*100)}% PRICE'}")
        print(f"  Cycling: {'ON' if cfg.use_cycling else 'OFF'}")
        print(f"  Z-Zone: {cfg.z_zone_label}")
        print(f"  Expected @5sh: ${cfg.expected_pnl:.2f} PnL, {cfg.expected_win_rate:.1f}% win rate")
        print(f"  Expected @50sh: ${cfg.expected_pnl * 10:.2f} PnL")
        print(f"  Premature Stop: {cfg.premature_stop_pct:.1f}% (${cfg.premature_pnl_lost:.2f} lost)")


if __name__ == "__main__":
    print_config_summary()
