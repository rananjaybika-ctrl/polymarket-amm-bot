"""
Master Trading Configurations - UPDATED Jan 27, 2026

These configs are validated against 157.4 hours of data across 456 markets.
Stop-out effects ARE factored into PnL calculations.

IMPORTANT: Time-based stops work DIFFERENTLY than price-based stops:
- AGGRESSIVE: 120s time-stop + skip rule (TIME120s_SKIP config)
- BALANCED/CONSERVATIVE: 15% price-stop is BETTER (DEPRECATED)

Usage:
    from research.reference.TRADING_CONFIGS import AGGRESSIVE, BALANCED, CONSERVATIVE

    config = AGGRESSIVE
    # Use config['lookback_ticks'], config['stop_loss_pct'], etc.

# =============================================================================
# ⚠️  THIS FILE IS THE SINGLE SOURCE OF TRUTH - AUTO-WIRED TO LIVE ENGINE  ⚠️
# =============================================================================
#
# As of Jan 31, 2026, this file is DIRECTLY IMPORTED by:
#   - scripts/run_paper_bot.py (AGGRESSIVE config used as defaults)
#
# Changes to AGGRESSIVE config values HERE will automatically propagate to
# the live trading engine. No manual sync required for these parameters:
#   - lookback_ticks, time_stop_seconds, z_lo, z_hi
#   - high_entry_threshold, min_time_remaining, skip_high_entry
#   - use_cycling, threshold_method (OU adaptive)
#
# STILL NEED MANUAL UPDATES (grep to find occurrences):
# 1. src/strategies/enhanced_spike.py (DEFAULT_SPIKE_LOOKBACK for fallback)
# 2. Backtest scripts (research/backtests/*.py) - should also import from here
#
# 2. VECTORIZED SPIKE DETECTION (backtests use these for speed):
#    - research/backtests/*_backtest.py - detect_spikes_ou() functions
#    - research/optimizers/*.py - spike detection functions
#    - Ensure threshold_method matches (OU vs fixed vs EWMA)
#
# 3. HARDCODED VALUES:
#    - SPIKE_LOOKBACK_TICKS, SPIKE_THRESHOLD in any backtest
#    - TIME_STOP_SECONDS, SKIP_THRESHOLD constants
#    - DROP_MULTIPLIER, DROP_INTERCEPT for loser bid calculation
#
# 4. PRECOMPUTED REFERENCES:
#    - Any script that precomputes spikes with specific lookback
#    - Kalman state preprocessing
#
# VERIFICATION: After changes, run:
#    grep -r "lookback_ticks\|SPIKE_LOOKBACK\|spike_lookback" --include="*.py" | grep -v __pycache__
#    grep -r "time_stop\|TIME_STOP" --include="*.py" | grep -v __pycache__
#
# See CLAUDE_MISTAKES.md #30 for why this matters.
# =============================================================================
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

    # TIME120s_SKIP parameters (Jan 27, 2026)
    skip_high_entry: bool = False  # Skip entries >= high_entry_threshold
    high_entry_threshold: float = 0.90  # Turkey problem cutoff
    min_time_remaining: float = 60.0  # Minimum seconds before market end

    # OBI (Orderbook Imbalance) filter (Jan 28, 2026)
    # When OBI confirms spike: 89% accuracy vs 77% when disagrees (+4.1pp improvement)
    use_obi_filter: bool = True  # Skip entries when orderbook disagrees with spike

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
# VALIDATED CONFIGURATIONS (Jan 24, 2026)
# =============================================================================

# TIME120s_SKIP + OBI VALIDATED (Jan 28, 2026): ~$9.00/hr avg across 157.4h cross-validation
# +24% hourly rate vs TIME180s, skip rule eliminates turkey problem losses
# OBI filter adds +4.1pp accuracy when orderbook confirms spike direction
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",

    # Core settings
    threshold_method="ou",
    zscore_method="ewma",
    lookback_ticks=72,
    lookback_ms=1200,

    # STOP SETTINGS - USE 120s TIME-STOP (optimized from 180s)
    # TIME120s runs 28% more cycles than TIME300s
    stop_loss_pct=None,         # NO price-based stop
    time_stop_seconds=120.0,    # Exit after 120s if not filled AND not in profit

    # Cycling ON for more trades
    use_cycling=True,

    # Z-score filter
    z_lo=0.0,
    z_hi=1.5,

    # TIME120s_SKIP parameters
    skip_high_entry=True,        # Skip entries >= $0.90 (unhedgeable)
    high_entry_threshold=0.90,   # Turkey problem cutoff
    min_time_remaining=180.0,    # time_stop + 60s buffer (prevents resolution exits)

    # Expected performance (at 50 shares, TIME120s_SKIP cross-validation)
    expected_pnl=90.00,          # ~$9.00/hr * 10h example
    expected_hourly_rate=9.00,   # at 50 shares, cross-validated
    expected_win_rate=70.0,
    expected_trades=150,         # estimate
    premature_stop_pct=25.0,     # time-stop exits
    premature_pnl_lost=-2.50,
)


# DEPRECATED (Jan 24): OU z-score drifted, BALANCED+EWMA regime-dependent ($26.38 OOS3 -> $11.17 OOS4)
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


# DEPRECATED (Jan 24): OU z-score drifted, BALANCED+EWMA regime-dependent ($26.38 OOS3 -> $11.17 OOS4)
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
# CONTRARIAN CONFIG (Path 2) - Validated Jan 24, 2026
# OOS4: $618/hr @2500sh (42% WR, breakeven=30%), 50 trades in 24.2h
# =============================================================================

CONTRARIAN = TradingConfig(
    name="CONTRARIAN",

    # Core settings (not spike-based, uses BTC direction)
    threshold_method="none",  # No spike threshold
    zscore_method="rolling_300s",  # Rolling 300s vol for Z-score
    lookback_ticks=0,  # N/A
    lookback_ms=0,  # N/A

    # No stops - hold to resolution
    stop_loss_pct=None,
    time_stop_seconds=None,

    # No cycling (one entry per 15-min window)
    use_cycling=False,

    # Z-score filter (vol-normalized move threshold)
    z_lo=0.5,  # Z >= 0.5 required
    z_hi=None,  # No upper bound

    # Expected performance (at 2500 shares per trade)
    expected_pnl=14920.0,  # $618/hr * 24.2h (OOS4)
    expected_hourly_rate=618.0,  # $/hr at 2500 shares
    expected_win_rate=42.0,
    expected_trades=50,  # Per 24.2h
    premature_stop_pct=0.0,  # No stops
    premature_pnl_lost=0.0,
)


# =============================================================================
# ALL CONFIGS
# =============================================================================

ALL_CONFIGS = [AGGRESSIVE, BALANCED, CONSERVATIVE, CONTRARIAN]


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


# =============================================================================
# PARAMETER EXPLANATIONS (moved from run_paper_bot.py Jan 31, 2026)
# =============================================================================
#
# lookback_ticks = 72
#   - 72 ticks ≈ 1200ms at ~60Hz Binance bookTicker stream
#   - Formula: ticks = ms * freq / 1000 = 1200 * 60 / 1000 = 72
#   - This is the CANONICAL lookback validated across all datasets
#
# time_stop_seconds = 120.0
#   - Time-stop exit after 2 minutes if hedge not filled
#   - Optimized from 180.0 to 120.0 on Jan 27, 2026
#   - TIME120s runs 28% more cycles than TIME300s
#
# high_entry_threshold = 0.90
#   - Skip entries at or above this price (prevents unhedgeable trades)
#   - At $0.90 entry, min hedge = $0.10 which requires $1 min order at 10 shares
#   - Testing value: 0.80 (more conservative for small size)
#   - Production value: 0.90 (at 50 shares, $0.10 * 50 = $5 hedge is viable)
#
# min_time_remaining = 180.0
#   - Entry cutoff: don't enter if < 180 seconds remaining
#   - Formula: time_stop_seconds + 60s buffer = 120 + 60 = 180
#   - Prevents resolution exits (entering too close to market end)
#
# skip_high_entry = True
#   - Enable the high entry skip rule
#   - When winner_ask >= high_entry_threshold, skip the trade
#   - Prevents "turkey problem" where we can't hedge expensive entries
#
# threshold_method = "ou"
#   - Use OU (Ornstein-Uhlenbeck) adaptive threshold, NOT fixed 0.02%
#   - Threshold scales with volatility: low vol → lower threshold, high vol → higher
#   - Range: 0.015% to 0.10% based on EWMA volatility z-score
#   - CRITICAL: Fixed 0.02% was WRONG - always use OU adaptive
#
# z_lo = 0.0, z_hi = 1.5
#   - Z-score volatility filter bounds for entry
#   - Only trade when volatility z-score is between 0.0 and 1.5
#   - Filters out extreme low/high volatility regimes
#
# use_cycling = True
#   - Re-enter same market after hedge fills
#   - Optimal per backtest: more trades = more PnL
#   - MIN_CYCLE_GAP_MS controls minimum gap between trades
#
# =============================================================================


if __name__ == "__main__":
    print_config_summary()
