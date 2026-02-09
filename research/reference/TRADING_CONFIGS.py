"""
Master Trading Configurations - UPDATED Feb 9, 2026

These configs are validated against 157.4 hours of data across 456 markets.
Stop-out effects ARE factored into PnL calculations.

IMPORTANT: Time-based stops work DIFFERENTLY than price-based stops:
- AGGRESSIVE: 30s time-stop + 10s breakeven exit + skip rule
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

    # =========================================================================
    # FIELDS WITH DEFAULTS (must come after non-default fields)
    # =========================================================================

    # Spike detection method (Feb 3, 2026)
    # "FIXED" = fixed lookback window, "EWMA_1000" = EWMA with 1000ms half-life
    spike_method: str = "FIXED"  # "FIXED", "EWMA_500", "EWMA_1000", "EWMA_1200"

    # Cycling gap (Feb 3, 2026)
    min_cycle_gap_ms: int = 50  # Minimum gap between cycles (ms)

    # TIME120s_SKIP parameters (Jan 27, 2026)
    skip_high_entry: bool = True  # Skip entries >= high_entry_threshold
    high_entry_threshold: float = 0.80  # Skip entries >= $0.80 *revert to >=0.90 if test validated
    min_time_remaining: float = 60.0  # Minimum seconds before market end

    # OBI (Orderbook Imbalance) filter (Jan 28, 2026)
    # When OBI confirms spike: 89% accuracy vs 77% when disagrees (+4.1pp improvement)
    use_obi_filter: bool = True  # Skip entries when orderbook disagrees with spike

    # Multi-cycle trading - DEPRECATED Jan 31, 2026
    # ABANDONED: Multi-cycle destroyed profitability (39.8% win rate vs 54.3% single)
    # Even with direction consistency fix, stacking same-direction trades dilutes edge.
    # See: research/findings/MULTICYCLE_ANALYSIS.md
    enable_multicycle: bool = False  # DEPRECATED - always use single-cycle
    max_cycles: int = 1              # DEPRECATED - always 1 (single-cycle)
    shares_per_cycle: int = 50       # PRODUCTION: 50 shares per trade

    # Session loss limit (Feb 1, 2026)
    # Circuit breaker: stop trading if cumulative session loss exceeds this amount
    # Analysis showed $50 never triggered in OOS7/OOS8 but protects against catastrophic sessions
    max_session_loss: float = 50.0   # $50 hard stop on session losses

    # Breakeven exit (Feb 3, 2026)
    # Real-time monitoring: exit when winner_bid <= entry_price AFTER min hold time
    # Prevents expensive time-stop exits ($1.04) by catching breakeven moment (~$1.00)
    # TESTED: 0ms=DISASTER (98% taker), 2s=worse, 5s=good, 10s=BEST (+13% $/hr, +41% Sharpe)
    # See: research/findings/BREAKEVEN_SWEEP_FINDINGS.md
    breakeven_min_hold_ms: int = 10000  # 10s hold before BE check (5s is close second)

    # Position imbalance limit (Feb 3, 2026)
    # HARD STOP: Block ALL new entries when abs(up_shares - down_shares) >= this
    # Calculated as 1.1x shares_per_cycle (allows small buffer, blocks runaway)
    # NOTE: Set to None to auto-calculate as int(shares_per_cycle * 1.1)
    hard_max_imbalance: Optional[int] = None  # Auto-calculated from shares_per_cycle

    # Hour-of-day filter (Feb 9, 2026)
    # Skip new entries during UTC hours where FADE accuracy drops significantly
    # UTC 14=London close (76.1%), 20=US evening dead zone (23.3%),
    # 8=London open stop-hunt (83.3%), 3-4=pre-Tokyo thin liquidity (0%)
    # See: research/findings/data/loser_analysis_filters.csv (worst_5_hours_skip)
    skip_utc_hours: Optional[list] = None  # e.g. [14, 20, 8, 4, 3]

    # Per-market entry cap (Feb 9, 2026 - CAP3 winner)
    # Limits filled entries per market to prevent cycling into losing markets.
    # CAP3: max 3 entries × 15 shares = $42 max exposure per market.
    # 6/6 datasets profitable, $1.69/hr, 340.40 total PnL across 201.9 hours.
    # Prevents 78% of losses caused by repeat re-entry (cycling) into bad markets.
    # None = unlimited entries (baseline behavior).
    # See: research/findings/data/aggressive_m_v2_session_stops.csv
    max_entries_per_market: Optional[int] = None

    # Event-driven spike detection (Feb 4, 2026)
    # Reduces response latency from ~5000ms (polling) to ~500ms (event-driven)
    # BinanceClient fires EWMA spike callbacks at ~60Hz, SpikeEventHandler validates
    # and queues signals for the 0.5s trading loop to execute.
    # Key latency improvement: worst-case from ~5700ms to ~1350ms
    event_driven_mode: bool = True       # Feature flag for event-driven spike detection
    event_loop_interval_ms: int = 500    # Trading loop interval (was 5000ms with polling)
    event_signal_max_age_ms: int = 1000  # Drop signals older than this (stale protection)

    def __post_init__(self):
        """Calculate derived fields after initialization."""
        if self.hard_max_imbalance is None:
            self.hard_max_imbalance = int(self.shares_per_cycle * 1.1)

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

# EWMA_1000 + TS30 + BE10s + OLD HEDGE VALIDATED (Feb 3, 2026): +$15.35/hr on OOS7-9
# EWMA reduces redundant signals: one price move → one spike (not 14)
# Breakeven exit (10s hold) adds +13% $/hr, +41% Sharpe vs time-stop only
# See: research/findings/AGGRESSIVE_EWMA_FINDINGS.md
# See: research/findings/BREAKEVEN_SWEEP_FINDINGS.md
#
# MULTI-CYCLE ABANDONED (Jan 31, 2026):
# - Multi-cycle destroyed profitability: 39.8% win rate vs 54.3% single-cycle
# - Even with direction consistency fix, stacking same-direction trades dilutes edge
# - SINGLE-CYCLE ONLY: enable_multicycle=False, max_cycles=1, shares_per_cycle=50
AGGRESSIVE = TradingConfig(
    name="AGGRESSIVE",

    # Core settings
    threshold_method="ou",
    zscore_method="ewma",
    lookback_ticks=72,
    lookback_ms=1200,

    # SPIKE DETECTION - EWMA_1000 (Feb 3, 2026 winner)
    # EWMA with 1000ms half-life reduces redundant signals from same price move
    spike_method="EWMA_1000",    # Winner config (was "FIXED")

    # STOP SETTINGS - USE 30s TIME-STOP (Feb 3, 2026 EWMA winner)
    # See: research/findings/AGGRESSIVE_EWMA_FINDINGS.md
    stop_loss_pct=None,         # NO price-based stop
    time_stop_seconds=30.0,     # Exit after 30s if not filled (E1000_TS30 winner)

    # Cycling ON for more trades
    use_cycling=True,
    min_cycle_gap_ms=50,        # Faster cycling (was 200)

    # Z-score filter - DISABLED (Feb 2, 2026)
    # Testing showed filter blocked 99.7% of trades due to OU z-score mismatch
    # OU z-scores are strongly negative (mean=-11.26), not in [0, 1.5] range
    # Grid search v2 (canonical $5.51/hr) does NOT use z-score filtering
    z_lo=None,
    z_hi=None,

    # TS30 parameters (Feb 3, 2026 - updated for EWMA winner)
    skip_high_entry=True,        # Skip entries >= threshold (unhedgeable)
    high_entry_threshold=0.80,   # Skip >= $0.80 *revert to >=0.90 if test validated
    min_time_remaining=90.0,     # time_stop + 60s buffer = 30 + 60 = 90

    # SINGLE-CYCLE ONLY (Jan 31, 2026 - multi-cycle abandoned)
    # Multi-cycle destroyed profitability: 39.8% win rate vs 54.3% single
    enable_multicycle=False,     # DEPRECATED - always False
    max_cycles=1,                # DEPRECATED - always 1
    shares_per_cycle=15,         # CAP3 production: 15 shares × 3 entries = $42 max/market

    # Session loss limit (Feb 1, 2026) - circuit breaker
    max_session_loss=50.0,       # Safety circuit breaker (not part of validated CAP3, but sensible guard)

    # hard_max_imbalance: Auto-calculated as int(shares_per_cycle * 1.1) = 11

    # HOUR-OF-DAY FILTER - OFF (not validated with CAP3)
    # Loser analysis showed potential benefit but CAP3 backtest ran WITHOUT hour filter.
    # To enable: skip_utc_hours=[14, 20, 8, 4, 3]
    skip_utc_hours=None,

    # PER-MARKET ENTRY CAP (Feb 9, 2026 - CAP3 winner)
    # Max 3 entries per 15-min market. Prevents cycling into losing markets.
    # CAP3: $1.69/hr, 6/6 profitable, $42 max exposure/market, 88.2% accuracy
    max_entries_per_market=3,

    # BREAKEVEN EXIT - 10s minimum hold before checking winner_bid <= entry_price
    # Prevents instant exit from spread (0ms=98% taker DISASTER)
    # BE_10000ms: +$15.35/hr, Sharpe 1.03 on OOS7-9 (vs $13.61/hr, 0.73 baseline)
    # BE_5000ms is close second: +$14.24/hr, Sharpe 1.01 (higher taker 71% vs 66%)
    breakeven_min_hold_ms=10000,  # 10 seconds (WINNER - use 5000 for more trades)

    # Expected performance (at 50 shares, EWMA_1000 + TS30 + BE10s on OOS7-9)
    expected_pnl=686.94,         # Combined OOS7+OOS8+OOS9.1 (44.81h) with BE10s
    expected_hourly_rate=15.35,  # at 50 shares, 60Hz datasets, BE_10000ms
    expected_win_rate=46.1,      # Lower win% but higher avg win
    expected_trades=1188,        # Combined across 44.81h (OOS7-9 only)
    premature_stop_pct=66.3,     # taker exits (breakeven + time-stop)
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
# z_lo = None, z_hi = None (DISABLED Feb 2, 2026)
#   - Z-score volatility filter is DISABLED
#   - Testing showed OU z-scores are strongly negative (mean=-11.26)
#   - Filter [0.0, 1.5] blocked 99.7% of trades (wrong bounds for OU params)
#   - Grid search v2 (canonical $5.51/hr) does NOT use z-score filtering
#   - To re-enable: set z_lo and z_hi to actual float values
#
# use_cycling = True
#   - Re-enter same market after hedge fills
#   - Optimal per backtest: more trades = more PnL
#   - MIN_CYCLE_GAP_MS controls minimum gap between trades
#
# =============================================================================


if __name__ == "__main__":
    print_config_summary()
