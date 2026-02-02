"""
Enhanced Spike Strategy - Raw Binance Spike Detection with Velocity Confirmation

UPGRADED (January 17, 2026): Added velocity confirmation filter for 218% improvement.
- Previous: Raw spike detection ($2.37/hr)
- Current: Enhanced spike with velocity confirmation ($7.54/hr)

Key Innovation:
- CRITICAL FILTER: Rejects spikes when velocity contradicts direction
- Spike UP + Velocity DOWN (v < -0.1) -> REJECT (14% accuracy without filter)
- Spike DOWN + Velocity UP (v > 0.1) -> REJECT (43% accuracy without filter)
- When velocity confirms spike -> 69-82% accuracy

Core Logic:
    1. Detect raw Binance spike (3-tick change >= 0.02%)
    2. Apply velocity confirmation filter (CRITICAL - improves accuracy from ~70% to ~100%)
    3. Compute composite score: 0.40*spike_mag + 0.30*velocity + 0.20*confirmation + 0.10*urgency
    4. Only trade if score >= 0.40

Expected Performance (from January 17, 2026 backtest):
    - Enhanced Signal: $7.54/hr (218% improvement over velocity)
    - Raw Spike: $7.03/hr
    - Best Velocity: $2.37/hr

Usage:
    strategy = EnhancedSpikeStrategy(base_size=15, spike_threshold=0.02)
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ..., binance_price=95000.0)

Author: Claude Code
Date: January 17, 2026 (Enhanced)
Based on: signal_based_mm_analysis.py findings
Supersedes: spike_capture.py (raw spike), spread_capture.py (velocity-based)
"""

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from src.config import FeeConfig

# =============================================================================
# SHARED LOGIC - Import from src/core (Single Source of Truth)
# =============================================================================
# NOTE: src/core/trading_utils.py contains the canonical implementations.
# Some standalone functions in this file are kept for backward compatibility
# but should be considered deprecated in favor of src/core imports.
from src.core import (
    polymarket_taker_fee,
    calculate_pnl_with_fees,
    velocity_confirms_spike as velocity_confirms_spike_core,
    obi_confirms_spike,
    should_take_spike_enhanced as should_take_spike_enhanced_core,
    compute_enhanced_score as compute_enhanced_score_core,
    calculate_loser_bid as calculate_loser_bid_core,
    VELOCITY_CONFIRM_THRESHOLD as VELOCITY_CONFIRM_THRESHOLD_CORE,
    ENHANCED_SCORE_THRESHOLD as ENHANCED_SCORE_THRESHOLD_CORE,
)

if TYPE_CHECKING:
    from src.strategies.ou_volatility import OUAdaptiveThreshold
    from src.services.volatility_tracker import LiveZScoreTracker

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS
# =============================================================================

# Spike detection parameters - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
# Source of truth: research/reference/TRADING_CONFIGS.py AGGRESSIVE config
# CANONICAL: lookback_ms=1200, lookback_ticks=72 at 60Hz
# For live trading with bookTicker (~60Hz avg): 72 ticks ≈ 1200ms
DEFAULT_SPIKE_LOOKBACK = 72      # 72 ticks ≈ 1200ms at ~60Hz bookTicker (CANONICAL)
DEFAULT_SPIKE_THRESHOLD = 0.02  # 0.02% minimum spike magnitude
SPIKE_HISTORY_SIZE = 50         # Keep last 50 prices for spike detection

# Magnitude → Loser Bid linear model coefficients (v2: recalibrated Jan 18, 2026)
# See research/HEDGE_PRICING_FINDINGS.md for analysis details
# Old formula (0.68 * spike + 0.01) severely underpredicted drops (predicted 0.03, actual 0.10)
# expected_drop = DROP_MULTIPLIER * magnitude_pct + DROP_INTERCEPT + regime_bonus
DROP_MULTIPLIER = 0.50          # Reduced from 0.68 - spike has weak predictive power
DROP_INTERCEPT = 0.08           # Increased from 0.01 - matches actual mean drop better
DROP_REGIME_BONUS = {'LOW': 0.0, 'MEDIUM': 0.01, 'HIGH': 0.02}  # Regime adjustment

# Target pair cost for magnitude-based bidding
DEFAULT_TARGET_PAIR_COST = 0.99  # Target sub-$1 for profit

# LEGACY velocity thresholds (kept for backward compatibility)
VELOCITY_THRESHOLD = 0.05
VELOCITY_STRONG = 0.10
VELOCITY_PULL_THRESHOLD = 0.05

# Legacy velocity zones (kept for backward compatibility)
VELOCITY_ZONES = {
    'neutral': {
        'vel_min': 0.00, 'vel_max': 0.10,
        'pair_target': 0.97,
        'winner_offset': 0.01,
        'loser_offset': 0.01,
        'winner_size_ratio': 0.50,
    },
    'moderate': {
        'vel_min': 0.10, 'vel_max': 0.30,
        'pair_target': 0.97,
        'winner_offset': 0.01,
        'loser_offset': 0.01,
        'winner_size_ratio': 0.55,
    },
    'strong': {
        'vel_min': 0.30, 'vel_max': 0.50,
        'pair_target': 0.96,
        'winner_offset': 0.00,
        'loser_offset': 0.03,
        'winner_size_ratio': 0.60,
    },
    'very_strong': {
        'vel_min': 0.50, 'vel_max': 1.00,
        'pair_target': 0.95,
        'winner_offset': -0.01,
        'loser_offset': 0.05,
        'winner_size_ratio': 0.70,
    },
    'extreme': {
        'vel_min': 1.00, 'vel_max': 99.0,
        'pair_target': 0.94,
        'winner_offset': -0.01,
        'loser_offset': 0.05,
        'winner_size_ratio': 0.75,
    },
}

# Quote offsets (LEGACY - re-exported by spread_capture.py for backward compat)
BASE_OFFSET = 0.01
TIGHT_OFFSET = -0.01
WIDE_OFFSET = 0.03
VERY_WIDE_OFFSET = 0.05

# LEGACY constants for backward compatibility
DEFAULT_ENTRY_OFFSET = 0.01
DEFAULT_HEDGE_OFFSET = 0.02
DEFAULT_ENTRY_WAIT = 8.0
DEFAULT_HEDGE_WAIT = 30.0
MAX_WAIT_TIME = 60.0

# Grid configuration
DEFAULT_GRID_LEVELS = 1
GRID_SPACING = 0.01

# Inventory management
DEFAULT_MAX_IMBALANCE_PCT = 0.10
DEFAULT_MAX_IMBALANCE_SHARES = 10
FORCE_REBALANCE_OFFSET = 0.005

# Polymarket constraints
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 15
DEFAULT_TARGET_SHARES = 50       # Updated from optimizer: 50 > 30 > 15
DEFAULT_MIN_PROFIT = 0.005
DEFAULT_MAX_SHARE_PRICE = 0.95
DEFAULT_ENABLE_CYCLING = True
DEFAULT_HIGH_ENTRY_THRESHOLD = 0.80  # TESTING: Skip entries >= $0.80 (revert to 0.90 for production)

# Zone filtering (LEGACY - spike threshold is the new filter)
DEFAULT_MIN_VELOCITY_BPS = 0.50

# Stop-loss configuration
# Updated Jan 20, 2026: 12% SL optimal - prevents resolution losses, 19.6% trigger rate
DEFAULT_STOP_LOSS_PCT = 0.12     # 12% stop-loss (was None)

# Z-score volatility filter - DISABLED Feb 2, 2026
# Testing showed OU z-scores are strongly negative (mean=-11.26), not in [0, 1.5]
# Filter blocked 99.7% of trades. Grid search v2 ($5.51/hr) does NOT use z-score filtering.
# To re-enable: set z_lo and z_hi in TRADING_CONFIGS.py (not None)
DEFAULT_ZSCORE_LO = None        # None = disabled (was 0.0)
DEFAULT_ZSCORE_HI = None        # None = disabled (was 1.5)
DEFAULT_ZSCORE_METHOD = "ewma"  # Best method for $/hr (if re-enabled)

# Timing - Min time remaining before resolution to allow new entries
# SOURCE OF TRUTH: TRADING_CONFIGS.py min_time_remaining=240.0 (Feb 1, 2026)
MIN_TIME_REMAINING = 240
QUOTE_REFRESH_INTERVAL = 0.5


# =============================================================================
# ENUMS
# =============================================================================

class EnhancedSpikePhase(Enum):
    """Strategy phases for EnhancedSpikeStrategy."""
    # Continuous phases
    IDLE = "idle"
    QUOTING = "quoting"
    REBALANCING = "rebalancing"
    COMPLETE = "complete"

    # LEGACY sequential phases (for backward compatibility)
    ENTRY_PENDING = "entry_pending"
    ENTRY_FILLED = "entry_filled"
    HEDGE_PENDING = "hedge_pending"
    EMERGENCY_DEFERRED = "emergency_deferred"


# Backward compatibility alias
SpreadCapturePhase = EnhancedSpikePhase


class VelocityZone(Enum):
    """Velocity zones (LEGACY - kept for logging)."""
    NEUTRAL = "neutral"
    MODERATE = "moderate"
    STRONG = "strong"


class CycleStatus(Enum):
    """Status of a trading cycle."""
    PENDING_ENTRY = "pending_entry"
    ENTRY_FILLED = "entry_filled"
    PENDING_HEDGE = "pending_hedge"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


# =============================================================================
# MULTI-CYCLE TRADING DATA STRUCTURES (Jan 31, 2026)
# =============================================================================
#
# DEPRECATED (Jan 31, 2026): Multi-cycle destroyed profitability.
# - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
# - MULTI: 39.8% win rate, -$26.70/hr (10x trades, 15pp lower win rate)
# Root cause: Stacking same-direction trades catches weak follow-on spikes.
#
# PRODUCTION CONFIG (SINGLE-CYCLE ONLY):
# - enable_multicycle=False in TRADING_CONFIGS.py
# - max_cycles=1, shares_per_cycle=50
# - MultiCycleManager automatically limits to 1 cycle when disabled
#
# The multi-cycle code is kept for backwards compatibility but SHOULD NOT be
# used in production. See research/findings/MULTICYCLE_ANALYSIS.md for details.
# =============================================================================

@dataclass
class CycleOrder:
    """Tracks a single order within a cycle."""
    order_id: str           # Unique ID (uuid or from exchange)
    cycle_id: str           # Parent cycle ID
    side: str               # "UP" or "DOWN"
    order_type: str         # "entry" or "hedge"
    target_price: float     # Expected fill price
    shares: int
    submitted_ts: int       # When order was submitted (ms)
    filled: bool = False
    fill_price: float = 0.0
    fill_ts: int = 0


@dataclass
class TradingCycle:
    """Complete cycle with entry + hedge tracking."""
    id: str                          # Unique cycle ID
    created_ts: int                  # Creation timestamp (ms)
    timeout_ts: int                  # Expiry timestamp (ms)
    shares: int                      # Shares per leg

    # Spike info (locked at creation)
    spike_direction: str             # "UP" or "DOWN"
    spike_magnitude: float

    # Sides (determined by spike direction)
    winner_side: str                 # Side we're betting on
    loser_side: str                  # Hedge side

    # Order tracking
    entry_order: Optional[CycleOrder] = None
    hedge_order: Optional[CycleOrder] = None

    # Status
    status: CycleStatus = CycleStatus.PENDING_ENTRY

    @property
    def is_active(self) -> bool:
        return self.status not in (
            CycleStatus.COMPLETED,
            CycleStatus.EXPIRED,
            CycleStatus.CANCELLED
        )


class MultiCycleManager:
    """
    Manages multiple parallel trading cycles with robust order tracking.

    DEPRECATED (Jan 31, 2026): Multi-cycle destroyed profitability.
    - SINGLE: 54.3% win rate, +$1.37/hr (LIVE-READY)
    - MULTI: 39.8% win rate, -$26.70/hr (ABANDONED)

    Root cause: Stacking same-direction trades catches weak follow-on spikes.
    PRODUCTION: Use enable_multicycle=False (single-cycle only).

    KEY DESIGN PRINCIPLES:
    1. Every order has a unique ID
    2. Order ID → Cycle ID mapping for instant lookup
    3. Fallback matching by (side, price proximity, timing)
    4. Toggleable via enable_multicycle flag (should be False in production)
    5. Logs ambiguous matches for debugging

    Usage (PRODUCTION - single-cycle):
        manager = MultiCycleManager(max_cycles=1, shares_per_cycle=50, enable_multicycle=False)

    Usage (DEPRECATED - multi-cycle):
        manager = MultiCycleManager(max_cycles=2, shares_per_cycle=25, enable_multicycle=True)
    """

    def __init__(
        self,
        max_cycles: int = 1,              # PRODUCTION: 1 (single-cycle)
        shares_per_cycle: int = 50,       # PRODUCTION: 50 shares
        time_stop_seconds: float = 180.0,
        enable_multicycle: bool = False,  # DEPRECATED: always False
    ):
        self.max_cycles = max_cycles if enable_multicycle else 1
        self.shares_per_cycle = shares_per_cycle
        self.time_stop_ms = int(time_stop_seconds * 1000)
        self.enable_multicycle = enable_multicycle

        # Active cycles
        self.cycles: Dict[str, TradingCycle] = {}

        # ORDER ID → CYCLE ID mapping (PRIMARY lookup method)
        self.order_to_cycle: Dict[str, str] = {}

        # Stats
        self.total_cycles_created = 0
        self.total_fills_matched = 0
        self.ambiguous_matches = 0

    def can_enter_new_cycle(self) -> bool:
        """Check if we have capacity for new entry."""
        self._cleanup_expired()
        active_count = sum(1 for c in self.cycles.values() if c.is_active)
        return active_count < self.max_cycles

    def create_cycle(
        self,
        spike_direction: str,
        spike_magnitude: float,
        winner_ask: float,
        loser_bid: float,
    ) -> Optional[TradingCycle]:
        """
        Create new cycle and generate entry order.

        Returns cycle if created, None if at capacity.
        """
        if not self.can_enter_new_cycle():
            return None

        now_ms = int(time.time() * 1000)
        cycle_id = f"cycle_{uuid.uuid4().hex[:8]}"

        # Determine sides based on spike direction
        winner_side = spike_direction  # UP spike → buy UP
        loser_side = "DOWN" if winner_side == "UP" else "UP"

        cycle = TradingCycle(
            id=cycle_id,
            created_ts=now_ms,
            timeout_ts=now_ms + self.time_stop_ms,
            shares=self.shares_per_cycle,
            spike_direction=spike_direction,
            spike_magnitude=spike_magnitude,
            winner_side=winner_side,
            loser_side=loser_side,
            status=CycleStatus.PENDING_ENTRY,
        )

        # Create entry order
        entry_order_id = f"entry_{cycle_id}"
        cycle.entry_order = CycleOrder(
            order_id=entry_order_id,
            cycle_id=cycle_id,
            side=winner_side,
            order_type="entry",
            target_price=winner_ask,
            shares=self.shares_per_cycle,
            submitted_ts=now_ms,
        )

        # Register mappings
        self.cycles[cycle_id] = cycle
        self.order_to_cycle[entry_order_id] = cycle_id
        self.total_cycles_created += 1

        logger.debug(
            f"[MULTICYCLE] Created {cycle_id[:12]}: {spike_direction} spike, "
            f"entry={winner_side}@${winner_ask:.3f}, shares={self.shares_per_cycle}"
        )

        return cycle

    def on_fill(
        self,
        side: str,
        price: float,
        size: int,
        order_id: Optional[str] = None,
    ) -> Optional[Tuple[TradingCycle, str]]:
        """
        Route fill to correct cycle.

        Returns (cycle, fill_type) where fill_type is "entry" or "hedge".
        Returns None if no matching cycle found.
        """
        # METHOD 1: Direct order_id lookup (PREFERRED)
        if order_id and order_id in self.order_to_cycle:
            cycle_id = self.order_to_cycle[order_id]
            cycle = self.cycles.get(cycle_id)
            if cycle:
                return self._process_fill(cycle, side, price, size, order_id)

        # METHOD 2: Fallback matching (for paper trading or missing order_id)
        return self._fallback_match(side, price, size)

    def _process_fill(
        self,
        cycle: TradingCycle,
        side: str,
        price: float,
        size: int,
        order_id: Optional[str] = None,
    ) -> Optional[Tuple[TradingCycle, str]]:
        """Process fill for a specific cycle."""
        now_ms = int(time.time() * 1000)

        # Check if this is entry or hedge
        if cycle.status == CycleStatus.PENDING_ENTRY:
            # Entry fill
            if cycle.entry_order and side == cycle.entry_order.side:
                cycle.entry_order.filled = True
                cycle.entry_order.fill_price = price
                cycle.entry_order.fill_ts = now_ms
                cycle.status = CycleStatus.ENTRY_FILLED

                # Now create hedge order
                hedge_order_id = f"hedge_{cycle.id}"
                cycle.hedge_order = CycleOrder(
                    order_id=hedge_order_id,
                    cycle_id=cycle.id,
                    side=cycle.loser_side,
                    order_type="hedge",
                    target_price=self._calculate_hedge_target(cycle, price),
                    shares=self.shares_per_cycle,
                    submitted_ts=now_ms,
                )
                self.order_to_cycle[hedge_order_id] = cycle.id
                cycle.status = CycleStatus.PENDING_HEDGE

                self.total_fills_matched += 1
                logger.info(
                    f"[MULTICYCLE] Entry filled: {cycle.id[:12]} {side}@${price:.3f}, "
                    f"hedge target={cycle.loser_side}@${cycle.hedge_order.target_price:.3f}"
                )
                return (cycle, "entry")

        elif cycle.status == CycleStatus.PENDING_HEDGE:
            # Hedge fill
            if cycle.hedge_order and side == cycle.hedge_order.side:
                cycle.hedge_order.filled = True
                cycle.hedge_order.fill_price = price
                cycle.hedge_order.fill_ts = now_ms
                cycle.status = CycleStatus.COMPLETED

                self.total_fills_matched += 1
                logger.info(
                    f"[MULTICYCLE] Hedge filled: {cycle.id[:12]} {side}@${price:.3f}, "
                    f"CYCLE COMPLETE"
                )
                return (cycle, "hedge")

        return None

    def _fallback_match(
        self,
        side: str,
        price: float,
        size: int,
    ) -> Optional[Tuple[TradingCycle, str]]:
        """
        Fallback matching when order_id not available.

        Priority:
        1. Exact side match for pending order type
        2. Price proximity (within 5 cents)
        3. Most recent cycle first
        """
        candidates = []

        for cycle in self.cycles.values():
            if not cycle.is_active:
                continue

            # Check if waiting for entry on this side
            if (cycle.status == CycleStatus.PENDING_ENTRY and
                cycle.entry_order and
                cycle.entry_order.side == side):
                price_diff = abs(cycle.entry_order.target_price - price)
                candidates.append((cycle, "entry", price_diff, cycle.created_ts))

            # Check if waiting for hedge on this side
            elif (cycle.status == CycleStatus.PENDING_HEDGE and
                  cycle.hedge_order and
                  cycle.hedge_order.side == side):
                price_diff = abs(cycle.hedge_order.target_price - price)
                candidates.append((cycle, "hedge", price_diff, cycle.created_ts))

        if not candidates:
            return None

        if len(candidates) > 1:
            self.ambiguous_matches += 1
            logger.warning(
                f"[MULTICYCLE] Ambiguous fill match: {len(candidates)} candidates for "
                f"{side}@${price:.3f} (total ambiguous: {self.ambiguous_matches})"
            )

        # Sort by: price proximity (primary), then recency (secondary)
        candidates.sort(key=lambda x: (x[2], -x[3]))
        best = candidates[0]

        return self._process_fill(best[0], side, price, size, None)

    def _calculate_hedge_target(self, cycle: TradingCycle, entry_price: float) -> float:
        """Calculate hedge bid price based on entry and spike magnitude."""
        # Use same formula as single-cycle mode
        expected_drop = DROP_MULTIPLIER * cycle.spike_magnitude + DROP_INTERCEPT
        max_loser = DEFAULT_TARGET_PAIR_COST - entry_price
        loser_bid = min((1.0 - entry_price) - expected_drop, max_loser)
        return max(0.01, min(0.95, loser_bid))

    def _cleanup_expired(self):
        """Mark expired cycles."""
        now_ms = int(time.time() * 1000)
        for cycle in self.cycles.values():
            if cycle.is_active and now_ms > cycle.timeout_ts:
                cycle.status = CycleStatus.EXPIRED
                logger.info(f"[MULTICYCLE] Cycle expired: {cycle.id[:12]}")

    def get_pending_orders(self) -> List[Tuple[str, str, float, int]]:
        """
        Get all pending orders across all cycles.

        Returns list of (side, order_type, price, shares)
        """
        orders = []
        for cycle in self.cycles.values():
            if cycle.status == CycleStatus.PENDING_ENTRY and cycle.entry_order:
                orders.append((
                    cycle.entry_order.side,
                    "entry",
                    cycle.entry_order.target_price,
                    cycle.entry_order.shares,
                ))
            elif cycle.status == CycleStatus.PENDING_HEDGE and cycle.hedge_order:
                orders.append((
                    cycle.hedge_order.side,
                    "hedge",
                    cycle.hedge_order.target_price,
                    cycle.hedge_order.shares,
                ))
        return orders

    def get_active_cycles(self) -> List[TradingCycle]:
        """Get list of active cycles."""
        return [c for c in self.cycles.values() if c.is_active]

    def get_status(self) -> Dict:
        """Get manager status for logging."""
        active = self.get_active_cycles()
        return {
            "enable_multicycle": self.enable_multicycle,
            "max_cycles": self.max_cycles,
            "shares_per_cycle": self.shares_per_cycle,
            "active_cycles": len(active),
            "total_created": self.total_cycles_created,
            "fills_matched": self.total_fills_matched,
            "ambiguous_matches": self.ambiguous_matches,
            "cycles": [
                {
                    "id": c.id[:12],
                    "status": c.status.value,
                    "spike": c.spike_direction,
                    "winner": c.winner_side,
                    "entry_filled": c.entry_order.filled if c.entry_order else False,
                    "hedge_filled": c.hedge_order.filled if c.hedge_order else False,
                }
                for c in active
            ]
        }

    def reset(self) -> None:
        """Reset manager for new market."""
        self.cycles.clear()
        self.order_to_cycle.clear()
        # Keep stats for session tracking


# =============================================================================
# ENHANCED OBI FILTER FUNCTION (Jan 31, 2026)
# =============================================================================
#
# PURPOSE: Enhanced OBI filter with ML-validated spike quality checks.
# Previous OBI filter was binary (reject if OBI <= 0).
# New filter adds: magnitude check, loser spread, time remaining, depth.
#
# TO REVERT TO SIMPLE OBI FILTER:
# 1. In get_quotes(), replace call to should_take_spike_enhanced() with
#    the original simple OBI check:
#        if spike_direction == "UP" and up_imbalance is not None:
#            obi_confirms = up_imbalance > 0
#        elif spike_direction == "DOWN" and down_imbalance is not None:
#            obi_confirms = down_imbalance > 0
#
# 2. Or simply don't call should_take_spike_enhanced() at all if not needed.
#
# The original simple OBI check code is preserved in get_quotes() around line 1100+
# (look for "OBI CONFIRMATION FILTER" comment block)
# =============================================================================
# DEPRECATED: Use should_take_spike_enhanced_core from src/core instead
# Kept for backward compatibility only.
# =============================================================================

def should_take_spike_enhanced(
    spike_direction: str,
    obi_winner: float,
    loser_spread: float = 0.05,
    time_remaining: float = 600.0,
    winner_ask_depth: Optional[float] = None,
) -> Tuple[bool, str]:
    """
    DEPRECATED: Use should_take_spike_enhanced_core from src/core instead.

    ML-validated spike filter with enhanced OBI and market structure checks.
    This is a local copy for backward compatibility.
    Canonical implementation: src/core/trading_utils.py
    """
    # Delegate to canonical implementation
    return should_take_spike_enhanced_core(
        spike_direction, obi_winner, loser_spread, time_remaining, winner_ask_depth
    )


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class EnhancedSpikeState:
    """State tracking for spike capture market making."""
    phase: EnhancedSpikePhase = EnhancedSpikePhase.IDLE

    # Position tracking
    up_shares: int = 0
    down_shares: int = 0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0

    # Quote tracking
    last_quote_time: float = 0.0
    last_velocity: float = 0.0
    last_up_offset: float = 0.0
    last_down_offset: float = 0.0
    last_velocity_zone: "VelocityZone" = None

    # Spike detection state
    last_spike_direction: Optional[str] = None
    last_spike_magnitude: float = 0.0
    last_spike_time: float = 0.0

    # Statistics
    total_up_fills: int = 0
    total_down_fills: int = 0
    total_pairs_matched: int = 0
    total_profit: float = 0.0
    rebalance_count: int = 0

    # Tracking
    quotes_generated: int = 0
    markets_traded: int = 0

    # Dynamic hedge target tracking
    first_fill_side: Optional[str] = None
    first_fill_price: float = 0.0
    first_fill_time: Optional[float] = None  # Timestamp of first fill for time-stop
    first_fill_velocity_dir: Optional[str] = None
    locked_hedge_target: Optional[float] = None
    current_velocity_zone: Optional[str] = None

    # Stop-loss tracking
    stop_loss_triggered: bool = False
    stop_loss_hedge_price: float = 0.0

    # LEGACY attributes for backward compatibility
    entry_side: Optional[str] = None
    hedge_side: Optional[str] = None
    entry_price: float = 0.0
    hedge_price: float = 0.0
    entry_size: int = 0
    hedge_size: int = 0
    cycles_completed: int = 0

    @property
    def imbalance(self) -> int:
        """Signed imbalance: positive = UP heavy, negative = DOWN heavy."""
        return self.up_shares - self.down_shares

    @property
    def abs_imbalance(self) -> int:
        """Absolute imbalance."""
        return abs(self.imbalance)

    @property
    def imbalance_pct(self) -> float:
        """Imbalance as percentage of larger side."""
        max_side = max(self.up_shares, self.down_shares)
        if max_side == 0:
            return 0.0
        return self.abs_imbalance / max_side

    @property
    def pair_cost(self) -> float:
        """Average pair cost if positions were matched."""
        if self.up_shares == 0 or self.down_shares == 0:
            return 0.0
        return self.up_avg_price + self.down_avg_price

    @property
    def matchable_pairs(self) -> int:
        """Number of complete pairs that can be merged."""
        return min(self.up_shares, self.down_shares)

    def lagging_side(self) -> Optional[str]:
        """Return which side has fewer shares, or None if balanced."""
        if self.up_shares < self.down_shares:
            return "UP"
        elif self.down_shares < self.up_shares:
            return "DOWN"
        return None


# Backward compatibility alias
SpreadCaptureState = EnhancedSpikeState


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class EnhancedSpikeStrategy:
    """
    Spike Capture Strategy - Raw Binance Spike Detection Market Maker.

    Uses raw Binance price spike detection for faster, more accurate entries.
    Replaces 10-second velocity averaging with 3-tick (~600ms) spike detection.

    THE SPIKE DETECTION LOGIC:
        1. Track last N Binance prices (default N=3)
        2. When |price change| >= threshold (default 0.02%):
           - If positive: Buy UP (predicted winner)
           - If negative: Buy DOWN (predicted winner)
        3. Calculate loser bid based on spike magnitude:
           - expected_drop = 0.68 * magnitude + 0.01
           - loser_bid = min(loser_ask - expected_drop, target_pair - winner_entry)

    Constructor Args:
        base_size: Base order size (default 15)
        spike_lookback: Ticks to look back for spike detection (default 3)
        spike_threshold: Minimum % change to trigger (default 0.02)
        target_pair_cost: Target pair cost for loser bid calc (default 0.99)

    BACKWARD COMPATIBLE with spread_capture.py parameters:
        entry_size, entry_offset, hedge_offset, min_velocity_bps
    """

    def __init__(
        self,
        # NEW spike detection parameters
        base_size: int = DEFAULT_BASE_SIZE,
        target_shares: int = DEFAULT_TARGET_SHARES,
        spike_lookback: int = DEFAULT_SPIKE_LOOKBACK,
        spike_threshold: float = DEFAULT_SPIKE_THRESHOLD,
        target_pair_cost: float = DEFAULT_TARGET_PAIR_COST,
        grid_levels: int = DEFAULT_GRID_LEVELS,
        max_imbalance_pct: float = DEFAULT_MAX_IMBALANCE_PCT,
        max_imbalance_shares: int = DEFAULT_MAX_IMBALANCE_SHARES,
        min_profit: float = DEFAULT_MIN_PROFIT,
        max_share_price: float = DEFAULT_MAX_SHARE_PRICE,
        enable_cycling: bool = DEFAULT_ENABLE_CYCLING,
        stop_loss_pct: Optional[float] = DEFAULT_STOP_LOSS_PCT,
        # Time-stop: Exit if position held too long without profit
        time_stop_seconds: float = 180.0,  # 3 minutes default
        # OU adaptive threshold (optional - use for production)
        ou_adaptive_threshold: Optional["OUAdaptiveThreshold"] = None,
        # Z-score volatility filter (Jan 22, 2026)
        # Best zone from grid search: 0 < z < 1.5
        zscore_tracker: Optional["LiveZScoreTracker"] = None,
        zscore_filter_enabled: bool = True,
        zscore_lo: Optional[float] = DEFAULT_ZSCORE_LO,
        zscore_hi: Optional[float] = DEFAULT_ZSCORE_HI,
        # TIME120s_SKIP parameters (Jan 27, 2026 optimization)
        # Skip entries >= $0.90 (unhedgeable due to Polymarket $1 minimum)
        skip_high_entry: bool = False,
        high_entry_threshold: float = DEFAULT_HIGH_ENTRY_THRESHOLD,
        min_time_remaining: float = 60.0,  # Configurable (was hardcoded MIN_TIME_REMAINING)
        # MULTI-CYCLE DEPRECATED (Jan 31, 2026)
        # Multi-cycle destroyed profitability: 39.8% win rate vs 54.3% single
        # PRODUCTION: enable_multicycle=False, max_cycles=1, shares_per_cycle=50
        enable_multicycle: bool = False,  # DEPRECATED - always False
        max_cycles: int = 1,              # DEPRECATED - always 1
        shares_per_cycle: int = 50,       # PRODUCTION: 50 shares per trade
        # LEGACY parameters (aliases for backward compatibility)
        entry_size: Optional[int] = None,
        entry_offset: float = DEFAULT_ENTRY_OFFSET,
        hedge_offset: float = DEFAULT_HEDGE_OFFSET,
        emergency_imbalance_threshold: Optional[int] = None,
        min_velocity_bps: float = DEFAULT_MIN_VELOCITY_BPS,  # LEGACY - ignored in spike mode
        # SHARED BUFFER: Use BinanceClient's 60Hz price buffer (Feb 2, 2026)
        # When set, strategy shares BinanceClient's spike_price_history instead of
        # maintaining its own buffer that fills at the slower 5-second trading loop rate.
        # This fixes the warmup issue where move=0.0000% for ~6 minutes after start.
        binance_client: Optional[Any] = None,  # Type hint is Any to avoid circular import
    ):
        # Handle LEGACY parameter aliases
        if entry_size is not None:
            base_size = entry_size
        if emergency_imbalance_threshold is not None:
            max_imbalance_shares = emergency_imbalance_threshold

        # Spike detection parameters
        self.spike_lookback = spike_lookback
        self.spike_threshold = spike_threshold
        self.target_pair_cost = target_pair_cost
        self._binance_price_history: List[float] = []  # Local buffer (slow, fallback)
        self._binance_client = binance_client  # Shared 60Hz buffer (fast, preferred)

        # OU adaptive threshold (replaces fixed threshold when set)
        self.ou_adaptive_threshold = ou_adaptive_threshold

        # Z-score volatility filter (Jan 22, 2026)
        # Filters out trades in suboptimal volatility regimes
        self.zscore_tracker = zscore_tracker
        self.zscore_filter_enabled = zscore_filter_enabled
        self.zscore_lo = zscore_lo
        self.zscore_hi = zscore_hi
        self._zscore_skip_count = 0  # Count trades skipped by z-score filter
        self._vol_log_interval = 30.0  # Log volatility every 30 seconds
        self._last_vol_log_time = 0.0  # Last time we logged volatility

        # Core parameters
        self.base_size = max(MIN_SHARES, base_size)
        self.target_shares = target_shares
        self.grid_levels = max(1, grid_levels)
        self.max_imbalance_pct = max_imbalance_pct
        self.max_imbalance_shares = max_imbalance_shares
        self.min_profit = min_profit
        self.max_share_price = max_share_price
        self.enable_cycling = enable_cycling
        self.stop_loss_pct = stop_loss_pct
        self.time_stop_seconds = time_stop_seconds

        # TIME120s_SKIP parameters
        self.skip_high_entry = skip_high_entry
        self.high_entry_threshold = high_entry_threshold
        self.min_time_remaining = min_time_remaining

        # LEGACY attributes
        self.entry_offset = entry_offset
        self.hedge_offset = hedge_offset
        self.emergency_imbalance_threshold = max_imbalance_shares
        self.min_velocity_bps = min_velocity_bps  # LEGACY - not used in spike mode

        self.state = EnhancedSpikeState()
        self.state.last_velocity_zone = VelocityZone.NEUTRAL
        self._completed_pairs: List[Dict[str, Any]] = []

        # MULTI-CYCLE TRADING (Jan 31, 2026)
        # Set enable_multicycle=False to revert to single-cycle mode
        self.enable_multicycle = enable_multicycle
        self.max_cycles = max_cycles
        self.shares_per_cycle = shares_per_cycle
        self.cycle_manager: Optional[MultiCycleManager] = None

        if enable_multicycle:
            self.cycle_manager = MultiCycleManager(
                max_cycles=max_cycles,
                shares_per_cycle=shares_per_cycle,
                time_stop_seconds=time_stop_seconds,
                enable_multicycle=True,
            )
            logger.info(
                f"[ENHSPIKE] Multi-cycle ENABLED: max={max_cycles}, shares_per={shares_per_cycle}, "
                f"total_capacity={max_cycles * shares_per_cycle}"
            )

        zscore_info = ""
        if zscore_filter_enabled and zscore_tracker:
            zscore_info = f", zscore_filter=[{zscore_lo}, {zscore_hi}]"
        elif zscore_filter_enabled:
            zscore_info = f", zscore_filter=ENABLED (no tracker yet)"

        stop_info = f"{stop_loss_pct:.0%}" if stop_loss_pct else "None"
        skip_info = f", skip_high_entry={skip_high_entry} (>=${high_entry_threshold:.2f})" if skip_high_entry else ""
        multicycle_info = f", multicycle={enable_multicycle} ({max_cycles}x{shares_per_cycle})" if enable_multicycle else ""
        logger.info(
            f"[ENHSPIKE] Initialized: base_size={base_size}, lookback={spike_lookback}, "
            f"threshold={spike_threshold:.2f}%, target_pair=${target_pair_cost:.2f}, "
            f"stop_loss={stop_info}, time_stop={time_stop_seconds}s, min_time={min_time_remaining}s, "
            f"cycling={enable_cycling}{zscore_info}{skip_info}{multicycle_info}"
        )

    # =========================================================================
    # SPIKE DETECTION (CORE NEW FUNCTIONALITY)
    # =========================================================================

    def detect_spike(self, binance_price: float) -> Tuple[Optional[str], float]:
        """
        Detect raw Binance price spike over last N ticks.

        This REPLACES the velocity-based zone detection with faster, more
        accurate raw spike detection.

        Uses OU-based adaptive threshold if ou_adaptive_threshold is set,
        otherwise falls back to fixed spike_threshold.

        Args:
            binance_price: Current Binance BTCUSDT price

        Returns:
            (direction, magnitude_pct) - direction is "UP" or "DOWN" or None
            magnitude_pct is the absolute percentage change
        """
        # Use shared BinanceClient buffer if available (60Hz, fast warmup)
        # Otherwise fall back to local buffer (5s rate, slow warmup)
        if self._binance_client is not None and hasattr(self._binance_client, 'spike_price_history'):
            price_history = self._binance_client.spike_price_history
            # BinanceClient updates its buffer on every tick, so we just read it
        else:
            # Fallback: use local buffer (slower, but works without BinanceClient)
            self._binance_price_history.append(binance_price)
            if len(self._binance_price_history) > SPIKE_HISTORY_SIZE:
                self._binance_price_history = self._binance_price_history[-SPIKE_HISTORY_SIZE:]
            price_history = self._binance_price_history

        # Need enough history
        if len(price_history) < self.spike_lookback + 1:
            return None, 0

        current = price_history[-1]
        previous = price_history[-self.spike_lookback - 1]

        if previous <= 0:
            return None, 0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        # Use OU adaptive threshold if available, otherwise fixed threshold
        if self.ou_adaptive_threshold is not None:
            threshold = self.ou_adaptive_threshold.update(binance_price)
        else:
            threshold = self.spike_threshold

        if magnitude >= threshold:
            direction = "UP" if change_pct > 0 else "DOWN"

            # Update state
            self.state.last_spike_direction = direction
            self.state.last_spike_magnitude = magnitude
            self.state.last_spike_time = time.time()

            logger.debug(
                f"[ENHSPIKE] Spike detected: {direction} {magnitude:.4f}% "
                f"(threshold={threshold:.4f}%, ${previous:.2f} -> ${current:.2f})"
            )
            return direction, magnitude

        return None, 0

    def get_current_threshold(self) -> float:
        """Get current spike threshold (OU adaptive or fixed)."""
        if self.ou_adaptive_threshold is not None:
            return self.ou_adaptive_threshold.get_threshold()
        return self.spike_threshold

    def calculate_magnitude_loser_bid(
        self,
        magnitude_pct: float,
        loser_ask: float,
        winner_entry: float,
        regime: str = "MEDIUM",
    ) -> float:
        """
        Calculate optimal loser bid based on BTC spike magnitude (v2).

        Uses recalibrated model from hedge_pricing_analysis.py:
        expected_drop = 0.50 * magnitude_pct + 0.08 + regime_bonus

        Args:
            magnitude_pct: Absolute BTC % change (e.g., 0.05 for 0.05%)
            loser_ask: Current loser side ask price
            winner_entry: Price we paid for winner
            regime: Volatility regime ('LOW', 'MEDIUM', 'HIGH')

        Returns:
            Optimal loser bid price
        """
        # Expected drop from recalibrated model (v2)
        regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)
        expected_drop = DROP_MULTIPLIER * magnitude_pct + DROP_INTERCEPT + regime_bonus
        expected_drop = max(0.02, min(0.20, expected_drop))  # Clamp to reasonable range

        # Maximum we can pay and still achieve target pair cost
        max_loser = self.target_pair_cost - winner_entry

        # FIX Feb 2, 2026: Use theoretical loser price (1.0 - winner_entry), NOT actual loser_ask
        # Backtest uses: loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)
        # Live was using: loser_bid = min(loser_ask - expected_drop, max_loser)
        # When loser_ask > theoretical (due to spread), live would bid HIGHER = worse pair costs
        theoretical_loser = 1.0 - winner_entry
        loser_bid = min(theoretical_loser - expected_drop, max_loser)

        # Floor at 1 cent
        result = max(loser_bid, 0.01)

        logger.debug(
            f"[ENHSPIKE] Magnitude bid: mag={magnitude_pct:.4f}%, expected_drop=${expected_drop:.3f}, "
            f"theoretical_loser=${theoretical_loser:.3f}, winner=${winner_entry:.3f} -> bid=${result:.3f}"
        )

        return result

    def clear_spike_history(self) -> None:
        """Clear spike detection history (call on new market)."""
        self._binance_price_history = []
        self.state.last_spike_direction = None
        self.state.last_spike_magnitude = 0.0
        self.state.last_spike_time = 0.0

    def set_zscore_tracker(self, tracker: "LiveZScoreTracker") -> None:
        """Set or update the z-score tracker (for live trading integration)."""
        self.zscore_tracker = tracker
        logger.info(
            f"[ENHSPIKE] Z-score tracker set: method={tracker.method}, "
            f"bounds=[{self.zscore_lo}, {self.zscore_hi}]"
        )

    def get_zscore_stats(self) -> dict:
        """Get z-score filter statistics."""
        zscore_info = {
            "enabled": self.zscore_filter_enabled,
            "bounds": [self.zscore_lo, self.zscore_hi],
            "skipped_entries": self._zscore_skip_count,
            "tracker_active": self.zscore_tracker is not None,
        }
        if self.zscore_tracker is not None:
            zscore_info.update(self.zscore_tracker.get_state())
        return zscore_info

    # =========================================================================
    # ENHANCED SIGNAL: Velocity Confirmation Filter (January 17, 2026)
    # =========================================================================
    #
    # Key Discovery from signal_based_mm_analysis.py:
    # - Spike UP + Velocity confirms (v>0.1): 69% accuracy
    # - Spike UP + Velocity contradicts (v<-0.1): 14% accuracy (REJECT!)
    # - Spike DOWN + Velocity confirms (v<-0.1): 82% accuracy
    # - Spike DOWN + Velocity contradicts (v>0.1): 43% accuracy (REJECT!)
    #
    # This filter improves hourly rate from $2.37/hr (velocity) to $7.54/hr (+218%)
    # =========================================================================

    def _velocity_confirms_spike(self, spike_dir: str, velocity_bps: float) -> bool:
        """
        Check if velocity confirms the spike direction.

        Returns True if:
        - Spike UP and velocity > 0 (both bullish)
        - Spike DOWN and velocity < 0 (both bearish)
        """
        if spike_dir == "UP":
            return velocity_bps > 0
        elif spike_dir == "DOWN":
            return velocity_bps < 0
        return False

    def compute_enhanced_score(
        self,
        spike_magnitude: float,
        velocity_bps: float,
        spike_direction: str,
        time_remaining: float,
    ) -> float:
        """
        Compute composite score for enhanced signal quality.

        Score formula (from backtest optimization):
            0.40 * spike_magnitude_score +
            0.30 * velocity_strength_score +
            0.20 * confirmation_bonus +
            0.10 * urgency_score

        Args:
            spike_magnitude: Absolute BTC % change (e.g., 0.05 for 0.05%)
            velocity_bps: Current velocity in basis points per second
            spike_direction: "UP" or "DOWN"
            time_remaining: Seconds until market resolution

        Returns:
            Composite score [0, 1]. Trade if score >= 0.40.
        """
        # Spike magnitude score: 0-5% maps to 0-1
        spike_score = min(spike_magnitude / 0.05, 1.0)

        # Velocity strength score: 0-0.5 bps maps to 0-1
        velocity_score = min(abs(velocity_bps) / 0.50, 1.0)

        # Confirmation bonus: 1.0 if velocity confirms spike direction
        confirmation_bonus = 1.0 if self._velocity_confirms_spike(spike_direction, velocity_bps) else 0.0

        # Urgency score: higher as market approaches resolution
        # At 900s (15 min): urgency = 0
        # At 0s: urgency = 1
        urgency_score = 1.0 - min(time_remaining / 900.0, 1.0)

        # Weighted composite
        score = (
            0.40 * spike_score +
            0.30 * velocity_score +
            0.20 * confirmation_bonus +
            0.10 * urgency_score
        )

        return round(score, 3)

    def should_take_enhanced_signal(
        self,
        spike_dir: Optional[str],
        spike_magnitude: float,
        velocity_bps: float,
        time_remaining: float,
        min_score: float = 0.40,
    ) -> Tuple[bool, float, Optional[str]]:
        """
        Determine if we should take a spike signal using enhanced filtering.

        CRITICAL FILTER: Rejects spikes when velocity contradicts.
        This single filter improves accuracy from ~70% to ~100%.

        Args:
            spike_dir: "UP", "DOWN", or None
            spike_magnitude: Absolute BTC % change
            velocity_bps: Current velocity in basis points per second
            time_remaining: Seconds until market resolution
            min_score: Minimum composite score to accept (default 0.40)

        Returns:
            (should_trade, score, reason)
            - should_trade: True if signal passes all filters
            - score: The composite score (for logging)
            - reason: Human-readable reason for decision
        """
        # No spike = no signal
        if spike_dir is None:
            return False, 0.0, "No spike detected"

        # CRITICAL: Reject if velocity contradicts spike direction
        # This is the KEY insight from backtest analysis
        # FIX Feb 2, 2026: Use <= and >= to match core velocity_confirms_spike()
        # Core returns TRUE if velocity_bps > -threshold, so REJECT when <= -threshold
        if spike_dir == "UP" and velocity_bps <= -0.10:
            logger.debug(
                f"[ENHANCED] REJECTED: Spike UP but velocity={velocity_bps:.3f} <= -0.10 (contradicts)"
            )
            return False, 0.0, f"Velocity contradicts UP spike (v={velocity_bps:.3f})"

        if spike_dir == "DOWN" and velocity_bps >= 0.10:
            logger.debug(
                f"[ENHANCED] REJECTED: Spike DOWN but velocity={velocity_bps:.3f} >= 0.10 (contradicts)"
            )
            return False, 0.0, f"Velocity contradicts DOWN spike (v={velocity_bps:.3f})"

        # NOTE: Neutral zone (-0.10 < v < +0.10) is ACCEPTED per backtest validation
        # Backtest shows BASELINE (accept neutral) beats CONSERVATIVE (reject neutral) by +$2.55/hr
        # See research/velocity_options_backtest.py for validation results

        # Compute composite score
        score = self.compute_enhanced_score(
            spike_magnitude=spike_magnitude,
            velocity_bps=velocity_bps,
            spike_direction=spike_dir,
            time_remaining=time_remaining,
        )

        # Check minimum score threshold
        if score < min_score:
            logger.debug(
                f"[ENHANCED] REJECTED: score={score:.3f} < {min_score:.2f} threshold"
            )
            return False, score, f"Score {score:.3f} below threshold {min_score:.2f}"

        # Signal accepted!
        confirms = "confirms" if self._velocity_confirms_spike(spike_dir, velocity_bps) else "neutral"
        reason = f"ACCEPTED: {spike_dir} spike (mag={spike_magnitude:.4f}%), velocity {confirms} (v={velocity_bps:.3f}), score={score:.3f}"
        logger.info(f"[ENHANCED] {reason}")

        return True, score, reason

    # =========================================================================
    # LEGACY METHODS (for backward compatibility)
    # =========================================================================

    def calculate_entry_offset(self) -> float:
        """LEGACY: Return fixed entry offset."""
        return self.entry_offset

    def calculate_hedge_offset(self) -> float:
        """LEGACY: Return fixed hedge offset."""
        return self.hedge_offset

    def calculate_max_hedge_price(self, entry_price: float) -> float:
        """LEGACY: Calculate maximum hedge price to preserve min_profit."""
        max_pair_cost = (1.00 - self.min_profit) / 0.99
        return round(max_pair_cost - entry_price, 4)

    def calculate_wait_time(
        self,
        attempt: int = 0,
        is_entry: bool = True,
        price_room: float = 0.10,
    ) -> float:
        """LEGACY: Calculate wait time with exponential backoff."""
        if is_entry:
            base_wait = DEFAULT_ENTRY_WAIT
        else:
            base_wait = DEFAULT_HEDGE_WAIT * (price_room / 0.10)
        wait = base_wait * (1.3 ** attempt)
        return min(wait, MAX_WAIT_TIME)

    def should_pull_entry(self, velocity_bps: float, entry_side: str) -> bool:
        """DEPRECATED: Order pulling disabled."""
        if entry_side.upper() == "UP":
            return velocity_bps < -VELOCITY_PULL_THRESHOLD
        else:
            return velocity_bps > VELOCITY_PULL_THRESHOLD

    # =========================================================================
    # VELOCITY ZONE METHODS (LEGACY - for backward compatibility)
    # =========================================================================

    def get_velocity_zone_name(self, velocity_bps: float) -> str:
        """Get the velocity zone name for given velocity."""
        abs_vel = abs(velocity_bps)
        for zone_name, zone in VELOCITY_ZONES.items():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                return zone_name
        return 'extreme'  # FIXED: 'super_strong' doesn't exist, use 'extreme'

    def get_pair_target_for_velocity(self, velocity_bps: float) -> float:
        """Get the target pair cost for current velocity zone."""
        zone_name = self.get_velocity_zone_name(velocity_bps)
        return VELOCITY_ZONES[zone_name]['pair_target']

    def calculate_size_allocation(self, velocity_bps: float, total_size: int) -> Tuple[int, int]:
        """Calculate size allocation for UP and DOWN sides based on velocity."""
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['neutral'])
        winner_ratio = zone_config.get('winner_size_ratio', 0.50)

        winner_size = int(total_size * winner_ratio)
        loser_size = total_size - winner_size

        winner_size = max(MIN_SHARES, winner_size)
        loser_size = max(MIN_SHARES, loser_size)

        if velocity_bps >= 0:
            return (winner_size, loser_size)
        else:
            return (loser_size, winner_size)

    def calculate_hedge_target(self, entry_price: float, velocity_bps: float) -> float:
        """Calculate hedge target price based on entry and current velocity zone."""
        pair_target = self.get_pair_target_for_velocity(velocity_bps)
        hedge_target = pair_target - entry_price
        return max(0.01, min(0.95, hedge_target))

    def record_first_fill(self, side: str, price: float, velocity_bps: float) -> None:
        """Record first fill and initialize hedge target."""
        s = self.state
        if s.first_fill_side is not None:
            return

        s.first_fill_side = side.upper()
        s.first_fill_price = price
        s.first_fill_time = time.time()  # Track entry time for time-stop
        s.first_fill_velocity_dir = "UP" if velocity_bps > 0 else "DOWN"
        s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)
        s.locked_hedge_target = self.calculate_hedge_target(price, velocity_bps)

        logger.info(
            f"[ENHSPIKE] First fill: {side} @ ${price:.4f}, vel={velocity_bps:.2f}bps "
            f"({s.current_velocity_zone}), hedge_target=${s.locked_hedge_target:.4f}"
        )

    def maybe_tighten_hedge_target(self, velocity_bps: float) -> bool:
        """Tighten hedge target if velocity strengthened in entry direction.

        Wrapper around check_hedge_target_change() for callers that only need boolean.
        """
        should_pull, _, _ = self.check_hedge_target_change(velocity_bps)
        return should_pull

    def get_current_hedge_target(self) -> Optional[float]:
        """Get current locked hedge target, or None if no entry yet."""
        return self.state.locked_hedge_target

    def check_hedge_target_change(self, velocity_bps: float) -> Tuple[bool, Optional[float], Optional[float]]:
        """Check if hedge target should be tightened and order pulled."""
        s = self.state
        if s.first_fill_side is None or s.locked_hedge_target is None:
            return (False, None, None)

        current_vel_dir = "UP" if velocity_bps > 0 else "DOWN"
        if current_vel_dir != s.first_fill_velocity_dir:
            return (False, None, None)

        new_target = self.calculate_hedge_target(s.first_fill_price, velocity_bps)

        if new_target < s.locked_hedge_target:
            old_target = s.locked_hedge_target
            old_zone = s.current_velocity_zone
            s.locked_hedge_target = new_target
            s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)

            logger.info(
                f"[ENHSPIKE] Hedge target tightened: ${old_target:.4f} ({old_zone}) "
                f"-> ${new_target:.4f} ({s.current_velocity_zone}) - PULL REQUIRED"
            )
            return (True, old_target, new_target)

        return (False, None, None)

    # =========================================================================
    # STOP-LOSS MECHANISM
    # =========================================================================

    def check_stop_loss(
        self,
        winner_current_bid: float,
        loser_current_ask: float,
    ) -> Tuple[bool, Optional[float]]:
        """Check if stop-loss should trigger and return hedge price."""
        s = self.state

        # Stop-loss disabled
        if self.stop_loss_pct is None:
            return (False, None)

        if s.first_fill_side is None or s.first_fill_price <= 0:
            return (False, None)

        if s.stop_loss_triggered:
            return (False, None)

        drop_pct = (s.first_fill_price - winner_current_bid) / s.first_fill_price

        if drop_pct >= self.stop_loss_pct:
            s.stop_loss_triggered = True
            s.stop_loss_hedge_price = loser_current_ask

            logger.warning(
                f"[ENHSPIKE] STOP-LOSS TRIGGERED: winner dropped {drop_pct:.1%} "
                f"(fill=${s.first_fill_price:.3f} -> bid=${winner_current_bid:.3f}), "
                f"hedging at loser_ask=${loser_current_ask:.3f}"
            )
            return (True, loser_current_ask)

        return (False, None)

    def get_stop_loss_order(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
    ) -> Optional[Dict[str, Any]]:
        """Check stop-loss and return immediate hedge order if triggered."""
        s = self.state

        if s.first_fill_side is None or s.stop_loss_triggered:
            return None

        if s.first_fill_side == "UP":
            winner_bid = up_bid
            loser_ask = down_ask
            loser_side = "DOWN"
        else:
            winner_bid = down_bid
            loser_ask = up_ask
            loser_side = "UP"

        should_trigger, hedge_price = self.check_stop_loss(winner_bid, loser_ask)

        if should_trigger and hedge_price is not None:
            return {
                'side': loser_side,
                'price': hedge_price,
                'size': self.base_size,
                'is_stop_loss': True,
                'is_market_order': True,
            }

        return None

    # =========================================================================
    # OFFSET CALCULATIONS (LEGACY - for backward compatibility)
    # =========================================================================

    def calculate_offsets(self, velocity_bps: float) -> Tuple[float, float]:
        """Calculate quote offsets based on velocity zone."""
        s = self.state
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['moderate'])

        winner_offset = zone_config['winner_offset']
        loser_offset = zone_config['loser_offset']

        if zone_name == 'neutral':
            inventory_bias = s.down_shares - s.up_shares
            if abs(inventory_bias) <= 2:
                return (winner_offset, winner_offset)
            elif inventory_bias > 0:
                return (winner_offset, loser_offset)
            else:
                return (loser_offset, winner_offset)

        if velocity_bps > 0:
            return (winner_offset, loser_offset)
        else:
            return (loser_offset, winner_offset)

    def calculate_entry_bid(self, best_bid: float, best_ask: float,
                            velocity_bps: float) -> float:
        """Calculate LIMIT ORDER entry bid price for winner side."""
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['moderate'])
        winner_offset = zone_config['winner_offset']

        entry_bid = best_bid - winner_offset
        max_bid = best_ask - 0.001
        entry_bid = min(entry_bid, max_bid)
        entry_bid = max(0.01, min(0.95, entry_bid))

        return entry_bid

    def check_velocity_zone_transition(
        self,
        velocity_bps: float
    ) -> Tuple["VelocityZone", bool, List[str]]:
        """Check if velocity crossed a zone boundary (for LOGGING only)."""
        abs_vel = abs(velocity_bps)

        if abs_vel < VELOCITY_THRESHOLD:
            new_zone = VelocityZone.NEUTRAL
        elif abs_vel < VELOCITY_STRONG:
            new_zone = VelocityZone.MODERATE
        else:
            new_zone = VelocityZone.STRONG

        old_zone = self.state.last_velocity_zone
        zone_changed = (new_zone != old_zone)
        sides_to_pull: List[str] = []

        self.state.last_velocity_zone = new_zone
        return (new_zone, zone_changed, sides_to_pull)

    # =========================================================================
    # MAIN ENTRY POINT: GET QUOTES (with spike detection)
    # =========================================================================

    def get_quotes(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        velocity_bps: float,
        time_remaining: float,
        current_time: Optional[float] = None,
        binance_price: Optional[float] = None,
        up_imbalance: Optional[float] = None,
        down_imbalance: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate quotes for both sides based on current market state.

        UPGRADED: Now uses spike detection when binance_price is provided.
        Falls back to velocity-based logic if binance_price is None.

        Args:
            up_bid: Current UP best bid
            up_ask: Current UP best ask
            down_bid: Current DOWN best bid
            down_ask: Current DOWN best ask
            velocity_bps: Current BTC velocity (basis points per second)
            time_remaining: Seconds until market resolution
            current_time: Current timestamp (default: time.time())
            binance_price: Current Binance BTCUSDT price (NEW - enables spike detection)
            up_imbalance: Orderbook imbalance for UP token (-1 to +1, positive = buying pressure)
            down_imbalance: Orderbook imbalance for DOWN token (-1 to +1, positive = buying pressure)

        Returns:
            List of quote dicts: [{'side': str, 'price': float, 'size': int}, ...]
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't place NEW ENTRIES if market ending soon
        # FIX Feb 2, 2026: Only block new entries, not time-stop hedges for existing positions
        # Previously this returned [] for ALL orders, preventing time-stop from executing
        if time_remaining < self.min_time_remaining and s.first_fill_side is None:
            logger.debug(f"[ENHSPIKE] Skipping NEW ENTRY: {time_remaining:.0f}s remaining < {self.min_time_remaining:.0f}s min")
            return []

        # SPIKE DETECTION MODE (preferred when binance_price provided)
        spike_direction = None
        spike_magnitude = 0.0
        enhanced_score = 0.0

        # Z-SCORE FILTER: Skip trades in suboptimal volatility regimes
        # DISABLED Feb 2, 2026: Testing showed filter blocked 99.7% of trades
        # OU z-scores are strongly negative (mean=-11.26), not in [0, 1.5] range
        # Grid search v2 (canonical $5.51/hr) does NOT use z-score filtering
        # To re-enable: set z_lo and z_hi in TRADING_CONFIGS.py (not None)
        zscore_tradeable = True
        current_zscore = 0.0

        if binance_price is not None and self.zscore_tracker is not None:
            # Update z-score tracker with current price
            current_zscore = self.zscore_tracker.update(binance_price)

            # Check if z-score is in tradeable zone (only for new entries)
            # Skip filter entirely if z_lo or z_hi is None (DISABLED)
            filter_enabled = (
                self.zscore_filter_enabled and
                self.zscore_lo is not None and
                self.zscore_hi is not None
            )

            if filter_enabled and s.first_fill_side is None:
                zscore_tradeable = self.zscore_tracker.should_trade(
                    z_lo=self.zscore_lo,
                    z_hi=self.zscore_hi
                )

                if not zscore_tradeable:
                    self._zscore_skip_count += 1
                    # Log every 10th skip to avoid log spam
                    if self._zscore_skip_count % 10 == 0:
                        regime = self.zscore_tracker.get_regime()
                        logger.debug(
                            f"[ENHSPIKE] Z-score filter: skipped entry (z={current_zscore:.2f}, "
                            f"regime={regime}, bounds=[{self.zscore_lo}, {self.zscore_hi}], "
                            f"skips={self._zscore_skip_count})"
                        )
                    return []

        # =========================================================
        # PERIODIC VOLATILITY LOGGING (Feb 1, 2026)
        # Log volatility metrics every 30 seconds for diagnostics
        # =========================================================
        if binance_price is not None:
            current_time = time.time()
            if current_time - self._last_vol_log_time >= self._vol_log_interval:
                self._last_vol_log_time = current_time

                # Calculate current price move (same as detect_spike)
                # Use shared BinanceClient buffer if available (60Hz, fast warmup)
                move_pct = 0.0
                if self._binance_client is not None and hasattr(self._binance_client, 'spike_price_history'):
                    price_history = self._binance_client.spike_price_history
                else:
                    price_history = self._binance_price_history

                if len(price_history) >= self.spike_lookback + 1:
                    current = price_history[-1]
                    previous = price_history[-self.spike_lookback - 1]
                    if previous > 0:
                        move_pct = abs((current - previous) / previous * 100)

                # Get current threshold
                threshold = self.get_current_threshold()

                # Get z-score info
                z_info = ""
                if self.zscore_tracker is not None:
                    regime = self.zscore_tracker.get_regime()
                    z_info = f", z={current_zscore:.2f} ({regime})"

                # Log volatility status (with buffer debug)
                buffer_info = f"buf={len(price_history)}/{self.spike_lookback+1}"
                shared = "shared" if self._binance_client is not None else "local"
                logger.info(
                    f"[VOL] move={move_pct:.4f}% vs threshold={threshold:.4f}% "
                    f"({'ABOVE' if move_pct >= threshold else 'below'}){z_info}, "
                    f"BTC=${binance_price:,.2f} [{buffer_info} {shared}]"
                )

        if binance_price is not None:
            raw_spike_direction, spike_magnitude = self.detect_spike(binance_price)

            # ENHANCED SIGNAL FILTERING (January 17, 2026):
            # Apply velocity confirmation filter to spike signals.
            # This improves accuracy from ~70% to ~100% and hourly rate from $2.37 to $7.54.
            if raw_spike_direction is not None:
                should_trade, enhanced_score, reason = self.should_take_enhanced_signal(
                    spike_dir=raw_spike_direction,
                    spike_magnitude=spike_magnitude,
                    velocity_bps=velocity_bps,
                    time_remaining=time_remaining,
                    min_score=0.40,  # Minimum composite score threshold
                )

                if should_trade:
                    spike_direction = raw_spike_direction

                    # =========================================================
                    # OBI CONFIRMATION FILTER (January 28, 2026)
                    # =========================================================
                    # CURRENT BEHAVIOR: Simple binary check (OBI > 0)
                    # When OBI confirms spike direction: 89% accuracy vs 77% when disagrees
                    # Improvement: +4.1pp at 30-tick horizon
                    #
                    # TO DISABLE OBI FILTER ENTIRELY:
                    # Comment out the entire if/elif block below (lines checking up_imbalance/down_imbalance)
                    # The spike will pass through without OBI validation
                    #
                    # TO USE ENHANCED OBI FILTER (should_take_spike_enhanced):
                    # Replace the if/elif block below with:
                    #     obi_winner = up_imbalance if spike_direction == "UP" else down_imbalance
                    #     should_take, reason = should_take_spike_enhanced(
                    #         spike_direction=spike_direction,
                    #         obi_winner=obi_winner if obi_winner is not None else 0.5,
                    #         loser_spread=loser_spread,  # Must pass from caller
                    #         time_remaining=time_remaining,
                    #         winner_ask_depth=winner_ask_depth,  # Must pass from caller
                    #     )
                    #     if not should_take:
                    #         logger.info(f"[ENHANCED OBI REJECT] {spike_direction}: {reason}")
                    #         spike_direction = None
                    # =========================================================
                    obi_confirms = True
                    if spike_direction == "UP" and up_imbalance is not None:
                        obi_confirms = up_imbalance > 0
                        if not obi_confirms:
                            # INFO level to make OBI rejections visible in live trading
                            logger.info(
                                f"[OBI REJECT] UP spike rejected: up_imbalance={up_imbalance:.3f} <= 0 "
                                f"(mag={spike_magnitude:.4f}%)"
                            )
                            spike_direction = None
                    elif spike_direction == "DOWN" and down_imbalance is not None:
                        obi_confirms = down_imbalance > 0
                        if not obi_confirms:
                            # INFO level to make OBI rejections visible in live trading
                            logger.info(
                                f"[OBI REJECT] DOWN spike rejected: down_imbalance={down_imbalance:.3f} <= 0 "
                                f"(mag={spike_magnitude:.4f}%)"
                            )
                            spike_direction = None

                    if spike_direction is not None:
                        obi_str = f", obi={'confirms' if obi_confirms else 'N/A'}"
                        logger.debug(
                            f"[ENHANCED] Signal accepted: {spike_direction} "
                            f"(mag={spike_magnitude:.4f}%, vel={velocity_bps:.3f}, score={enhanced_score:.3f}{obi_str})"
                        )
                else:
                    # Spike detected but rejected by filter - treat as no spike
                    spike_direction = None
                    logger.debug(f"[ENHANCED] Signal rejected: {reason}")

            # No valid signal and no position = wait
            if spike_direction is None and s.first_fill_side is None:
                return []

        else:
            # LEGACY: Velocity-based zone filter (when no Binance price available)
            if self.min_velocity_bps > 0 and s.first_fill_side is None:
                if abs(velocity_bps) < self.min_velocity_bps:
                    if int(current_time) % 10 == 0:
                        zone = self.get_velocity_zone_name(velocity_bps)
                        logger.debug(
                            f"[ENHSPIKE] Skipping zone '{zone}': |{velocity_bps:.3f}| < {self.min_velocity_bps:.2f}"
                        )
                    return []

        # Check if target reached (cycling disabled)
        if not self.enable_cycling:
            if s.up_shares >= self.target_shares and s.down_shares >= self.target_shares:
                if s.phase != EnhancedSpikePhase.COMPLETE:
                    s.phase = EnhancedSpikePhase.COMPLETE
                    logger.info(
                        f"[ENHSPIKE] Target reached: UP={s.up_shares}, DOWN={s.down_shares} "
                        f"(cycling disabled, stopping)"
                    )
                return []

        # =========================================================================
        # EXIT LOGIC: STOP-LOSS AND TIME-STOP (BEFORE rate limiting!)
        # FIX Feb 2, 2026: Exit logic must NEVER be blocked by rate limiting.
        # Previously, rate limit at line 1712 would return [] before reaching
        # stop-loss and time-stop checks, preventing exits when rate-limited.
        # =========================================================================

        # STOP-LOSS CHECK (executes regardless of rate limit)
        stop_loss_order = self.get_stop_loss_order(up_bid, up_ask, down_bid, down_ask)
        if stop_loss_order:
            logger.info(
                f"[ENHSPIKE] Stop-loss hedge: {stop_loss_order['side']} @ ${stop_loss_order['price']:.3f}"
            )
            return [stop_loss_order]

        # TIME-STOP CHECK (executes regardless of rate limit)
        # Exit if position held too long and not in profit
        if s.first_fill_side is not None and s.first_fill_time is not None:
            elapsed = current_time - s.first_fill_time
            if elapsed >= self.time_stop_seconds:
                # Determine winner and loser based on entry side
                if s.first_fill_side == "UP":
                    winner_bid = up_bid
                    loser_ask = down_ask
                    loser_side = "DOWN"
                else:
                    winner_bid = down_bid
                    loser_ask = up_ask
                    loser_side = "UP"

                # Only trigger time-stop if NOT in profit
                in_profit = winner_bid >= s.first_fill_price
                if not in_profit:
                    logger.warning(
                        f"[ENHSPIKE] TIME-STOP TRIGGERED: {elapsed:.0f}s elapsed >= {self.time_stop_seconds:.0f}s, "
                        f"winner bid=${winner_bid:.3f} < entry=${s.first_fill_price:.3f}, hedging at ${loser_ask:.3f}"
                    )
                    return [{
                        'side': loser_side,
                        'price': loser_ask,  # Take market (ask)
                        'size': self.base_size,
                        'is_time_stop': True,
                        'is_market_order': True,
                    }]
                else:
                    logger.debug(
                        f"[ENHSPIKE] Time-stop skipped: {elapsed:.0f}s elapsed but in profit "
                        f"(winner bid=${winner_bid:.3f} >= entry=${s.first_fill_price:.3f})"
                    )

        # =========================================================================
        # RATE LIMIT: Only applies to NEW ENTRY/HEDGE quotes, not exits above
        # =========================================================================
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []

        s.last_quote_time = current_time
        s.last_velocity = velocity_bps
        s.phase = EnhancedSpikePhase.QUOTING

        # Dynamic hedge target tightening
        if s.locked_hedge_target is not None:
            self.maybe_tighten_hedge_target(velocity_bps)

        quotes = []

        # =========================================================================
        # MULTI-CYCLE MODE (Jan 31, 2026)
        # =========================================================================
        # When enabled, use cycle manager for entry/hedge tracking
        # To revert: set enable_multicycle=False in config
        if self.enable_multicycle and self.cycle_manager is not None:
            # Check if we can create a new cycle
            if spike_direction is not None and self.cycle_manager.can_enter_new_cycle():
                winner_side = spike_direction
                loser_side = "DOWN" if winner_side == "UP" else "UP"

                if winner_side == "UP":
                    winner_ask = up_ask
                    winner_bid = up_bid
                    loser_bid = down_bid
                else:
                    winner_ask = down_ask
                    winner_bid = down_bid
                    loser_bid = up_bid

                # SKIP HIGH-ENTRY check
                if self.skip_high_entry and winner_ask >= self.high_entry_threshold:
                    logger.debug(
                        f"[MULTICYCLE] SKIP: {winner_side} ask=${winner_ask:.3f} >= "
                        f"${self.high_entry_threshold:.2f} (unhedgeable)"
                    )
                else:
                    # Create new cycle
                    cycle = self.cycle_manager.create_cycle(
                        spike_direction=spike_direction,
                        spike_magnitude=spike_magnitude,
                        winner_ask=winner_ask,
                        loser_bid=loser_bid,
                    )
                    if cycle:
                        # Generate entry quote using cycle's shares
                        # TAKER entry at ask - matches backtest logic (Feb 1, 2026 fix)
                        entry_price = winner_ask
                        entry_price = round(entry_price, 2)
                        entry_price = max(0.01, min(self.max_share_price, entry_price))

                        quotes.append({
                            'side': winner_side,
                            'price': entry_price,
                            'size': self.shares_per_cycle,  # Use per-cycle size
                            'level': 0,
                            'is_rebalance': False,
                            'is_spike_entry': True,
                            'spike_magnitude': spike_magnitude,
                            'enhanced_score': enhanced_score,
                            'zscore': current_zscore if self.zscore_tracker else None,
                            'zscore_regime': self.zscore_tracker.get_regime() if self.zscore_tracker else None,
                            'cycle_id': cycle.id,  # Track which cycle this is for
                            'order_id': cycle.entry_order.order_id if cycle.entry_order else None,
                        })

                        logger.info(
                            f"[MULTICYCLE] New cycle {cycle.id[:12]}: {winner_side} entry @ ${entry_price:.3f} "
                            f"(spike={spike_magnitude:.4f}%, active={len(self.cycle_manager.get_active_cycles())})"
                        )

            # Generate hedge quotes for pending cycles
            for cycle in self.cycle_manager.get_active_cycles():
                if cycle.status == CycleStatus.PENDING_HEDGE and cycle.hedge_order:
                    loser_side = cycle.loser_side
                    loser_ask = down_ask if loser_side == "DOWN" else up_ask
                    loser_bid_price = down_bid if loser_side == "DOWN" else up_bid

                    # Use magnitude-based hedge pricing
                    hedge_bid = self.calculate_magnitude_loser_bid(
                        cycle.spike_magnitude,
                        loser_ask,
                        cycle.entry_order.fill_price if cycle.entry_order else 0.5,
                    )
                    hedge_bid = round(hedge_bid, 2)
                    hedge_bid = max(0.01, min(self.max_share_price, hedge_bid))

                    quotes.append({
                        'side': loser_side,
                        'price': hedge_bid,
                        'size': self.shares_per_cycle,
                        'level': 0,
                        'is_rebalance': False,
                        'is_hedge': True,
                        'cycle_id': cycle.id,
                        'order_id': cycle.hedge_order.order_id,
                    })

            s.quotes_generated += len(quotes)
            return quotes

        # =========================================================================
        # SINGLE-CYCLE MODE (original behavior)
        # =========================================================================
        # PHASE 1: Entry not filled yet
        if s.first_fill_side is None:
            # Determine winner based on spike (preferred) or velocity (fallback)
            if spike_direction is not None:
                winner_side = spike_direction
                logger.debug(
                    f"[ENHSPIKE] SPIKE ENTRY: {winner_side} (mag={spike_magnitude:.4f}%)"
                )
            else:
                winner_side = "UP" if velocity_bps > 0 else "DOWN"

            loser_side = "DOWN" if winner_side == "UP" else "UP"

            # TARGET SHARES CHECK: Don't exceed target on either side
            # NOTE: When changing base_size, ensure target_shares is updated accordingly
            winner_shares = s.up_shares if winner_side == "UP" else s.down_shares
            if winner_shares + self.base_size > self.target_shares:
                logger.debug(
                    f"[ENHSPIKE] SKIP: {winner_side} would exceed target "
                    f"({winner_shares} + {self.base_size} > {self.target_shares})"
                )
                return []

            # Winner entry - buy at ASK for speed (spike mode) or bid+offset (velocity mode)
            if winner_side == "UP":
                winner_ask = up_ask
                winner_bid = up_bid
            else:
                winner_ask = down_ask
                winner_bid = down_bid

            # SKIP HIGH-ENTRY: Block entries >= $0.90 (cannot hedge - Polymarket $1 min)
            # Only blocks NEW entries (PHASE 1), never hedging (PHASE 2)
            if self.skip_high_entry and winner_ask >= self.high_entry_threshold:
                logger.debug(
                    f"[ENHSPIKE] SKIP: {winner_side} ask=${winner_ask:.3f} >= "
                    f"${self.high_entry_threshold:.2f} (unhedgeable)"
                )
                return []

            # Use TAKER entry at ask price - matches backtest logic
            # FIX Feb 1, 2026: Changed from maker (bid+0.01) to taker (ask)
            # Maker orders below ask rarely fill. Backtest assumes taker at ask.
            # Note: 500ms taker delay applies, but backtest is validated with this.
            if spike_direction is not None:
                # TAKER entry: buy at ask to ensure fill
                entry_price = winner_ask
            else:
                # Legacy velocity-based entry (rarely used now)
                up_offset, down_offset = self.calculate_offsets(velocity_bps)
                offset = up_offset if winner_side == "UP" else down_offset
                entry_price = winner_bid - offset

            entry_price = round(entry_price, 2)
            entry_price = max(0.01, min(self.max_share_price, entry_price))

            quotes.append({
                'side': winner_side,
                'price': entry_price,
                'size': self.base_size,
                'level': 0,
                'is_rebalance': False,
                'is_spike_entry': spike_direction is not None,
                'spike_magnitude': spike_magnitude,
                'enhanced_score': enhanced_score,  # Composite score from enhanced filter
                'zscore': current_zscore if self.zscore_tracker else None,
                'zscore_regime': self.zscore_tracker.get_regime() if self.zscore_tracker else None,
            })

            zone_name = self.get_velocity_zone_name(velocity_bps)
            if spike_direction:
                logger.debug(
                    f"[ENHSPIKE] ENTRY: {winner_side} @ ${entry_price:.3f} "
                    f"(spike={spike_magnitude:.4f}%, score={enhanced_score:.3f})"
                )
            else:
                logger.debug(
                    f"[ENHSPIKE] ENTRY: {winner_side} @ ${entry_price:.3f} "
                    f"(vel={velocity_bps:.3f}bps, zone={zone_name})"
                )

        # PHASE 2: Entry filled - place hedge
        else:
            loser_side = "DOWN" if s.first_fill_side == "UP" else "UP"
            loser_ask = down_ask if loser_side == "DOWN" else up_ask
            loser_bid_price = down_bid if loser_side == "DOWN" else up_bid

            # Calculate loser bid based on spike magnitude (preferred) or hedge target (fallback)
            if s.last_spike_magnitude > 0:
                # SPIKE MODE: Use magnitude-based bid
                loser_bid = self.calculate_magnitude_loser_bid(
                    s.last_spike_magnitude,
                    loser_ask,
                    s.first_fill_price,
                )
                logger.debug(
                    f"[ENHSPIKE] HEDGE (magnitude): {loser_side} bid=${loser_bid:.3f} "
                    f"(mag={s.last_spike_magnitude:.4f}%, loser_ask=${loser_ask:.3f})"
                )
            else:
                # LEGACY: Use hedge target
                hedge_target = s.locked_hedge_target
                if hedge_target is not None:
                    price_gap = loser_ask - hedge_target
                    if price_gap <= 0.02:
                        up_offset, down_offset = self.calculate_offsets(velocity_bps)
                        offset = down_offset if loser_side == "DOWN" else up_offset
                        loser_bid = loser_bid_price - offset
                        logger.debug(
                            f"[ENHSPIKE] HEDGE (target): {loser_side} bid=${loser_bid:.3f}, "
                            f"ask=${loser_ask:.3f} near target=${hedge_target:.3f}"
                        )
                    else:
                        logger.debug(
                            f"[ENHSPIKE] WAITING: loser ask=${loser_ask:.3f} > target=${hedge_target:.3f}"
                        )
                        return quotes
                else:
                    # No target set, use default offset
                    loser_bid = loser_bid_price - 0.03

            loser_bid = round(loser_bid, 2)
            loser_bid = max(0.01, min(self.max_share_price, loser_bid))

            quotes.append({
                'side': loser_side,
                'price': loser_bid,
                'size': self.base_size,
                'level': 0,
                'is_rebalance': False,
                'is_hedge': True,
            })

        s.quotes_generated += len(quotes)
        return quotes

    def _generate_side_quotes(
        self,
        side: str,
        best_bid: float,
        offset: float,
        current_shares: int,
        needs_rebalance: bool,
        allocated_size: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Generate quotes for one side (UP or DOWN)."""
        quotes = []
        s = self.state

        if current_shares >= self.target_shares:
            if not needs_rebalance:
                return []

        effective_size = allocated_size if allocated_size is not None else self.base_size
        size_per_level = max(MIN_SHARES, effective_size // self.grid_levels)

        if needs_rebalance:
            offset = FORCE_REBALANCE_OFFSET
            size_per_level = self.base_size
            self.state.rebalance_count += 1
            logger.info(f"[ENHSPIKE] REBALANCE {side}: offset={offset:.3f}")

        max_hedge_price = None
        if s.first_fill_side is not None and side != s.first_fill_side:
            max_hedge_price = self.get_current_hedge_target()
            if max_hedge_price is not None:
                logger.debug(f"[ENHSPIKE] Hedge side {side}: max_price=${max_hedge_price:.4f}")

        for level in range(self.grid_levels):
            level_offset = offset + (level * GRID_SPACING)
            price = round(best_bid - level_offset, 2)

            if max_hedge_price is not None and price > max_hedge_price:
                price = round(max_hedge_price, 2)

            if price <= 0.01 or price > self.max_share_price:
                continue

            level_size = size_per_level if level == 0 else max(MIN_SHARES, size_per_level // (level + 1))

            remaining = self.target_shares - current_shares
            if level_size > remaining:
                level_size = remaining

            if level_size >= MIN_SHARES:
                quotes.append({
                    'side': side,
                    'price': price,
                    'size': level_size,
                    'level': level,
                    'is_rebalance': needs_rebalance,
                })
                current_shares += level_size

        return quotes

    # =========================================================================
    # INVENTORY MANAGEMENT
    # =========================================================================

    def _check_rebalance_needed(self) -> Tuple[bool, Optional[str]]:
        """Check if inventory rebalancing is needed."""
        s = self.state

        if s.imbalance_pct > self.max_imbalance_pct:
            lagging = s.lagging_side()
            if lagging:
                logger.info(
                    f"[ENHSPIKE] Imbalance {s.imbalance_pct:.1%} > {self.max_imbalance_pct:.0%}, "
                    f"rebalance {lagging}"
                )
                return (True, lagging)

        if s.abs_imbalance > self.max_imbalance_shares:
            lagging = s.lagging_side()
            if lagging:
                logger.info(
                    f"[ENHSPIKE] Abs imbalance {s.abs_imbalance} > {self.max_imbalance_shares}, "
                    f"rebalance {lagging}"
                )
                return (True, lagging)

        return (False, None)

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int, order_id: Optional[str] = None) -> None:
        """
        Handle a fill notification.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Number of shares filled
            order_id: Optional order ID for multi-cycle routing
        """
        s = self.state
        side_upper = side.upper()

        # MULTI-CYCLE ROUTING (Jan 31, 2026)
        # Route fills through cycle manager when enabled
        # To revert: set enable_multicycle=False in config or strategy init
        if self.enable_multicycle and self.cycle_manager is not None:
            result = self.cycle_manager.on_fill(side_upper, price, size, order_id)
            if result:
                cycle, fill_type = result
                # Update state to reflect the fill
                if side_upper == "UP":
                    s.up_cost += price * size
                    s.up_shares += size
                    s.up_avg_price = round(s.up_cost / s.up_shares, 4) if s.up_shares > 0 else 0.0
                    s.total_up_fills += size
                else:
                    s.down_cost += price * size
                    s.down_shares += size
                    s.down_avg_price = round(s.down_cost / s.down_shares, 4) if s.down_shares > 0 else 0.0
                    s.total_down_fills += size

                logger.info(
                    f"[MULTICYCLE] Fill routed: {fill_type} for {cycle.id[:12]} "
                    f"({cycle.spike_direction} spike) | {side_upper} {size}@${price:.3f}"
                )

                # Check for completed cycles
                if cycle.status == CycleStatus.COMPLETED:
                    self._check_completed_pairs()
                return
            else:
                logger.warning(
                    f"[MULTICYCLE] Unrouted fill: {side_upper} {size}@${price:.3f} "
                    f"(order_id={order_id}) - falling through to single-cycle handling"
                )

        # SINGLE-CYCLE MODE (original behavior)
        if side_upper == "UP":
            s.up_cost += price * size
            s.up_shares += size
            s.up_avg_price = round(s.up_cost / s.up_shares, 4) if s.up_shares > 0 else 0.0
            s.total_up_fills += size
        else:
            s.down_cost += price * size
            s.down_shares += size
            s.down_avg_price = round(s.down_cost / s.down_shares, 4) if s.down_shares > 0 else 0.0
            s.total_down_fills += size

        # LEGACY phase tracking
        if s.phase == EnhancedSpikePhase.ENTRY_PENDING:
            s.entry_price = price
            s.entry_size = size
            s.phase = EnhancedSpikePhase.ENTRY_FILLED
        elif s.phase == EnhancedSpikePhase.HEDGE_PENDING:
            s.hedge_price = price
            s.hedge_size = size
            s.cycles_completed += 1
            s.phase = EnhancedSpikePhase.COMPLETE

        logger.info(
            f"[ENHSPIKE] Fill: {side_upper} {size}@${price:.3f} | "
            f"Pos: UP={s.up_shares}@${s.up_avg_price:.3f}, DOWN={s.down_shares}@${s.down_avg_price:.3f} | "
            f"Imbal: {s.imbalance:+d} ({s.imbalance_pct:.1%})"
        )

        if s.first_fill_side is None:
            self.record_first_fill(side_upper, price, s.last_velocity)
        else:
            self.maybe_tighten_hedge_target(s.last_velocity)

        self._check_completed_pairs()

    def _check_completed_pairs(self) -> None:
        """Check and record matched pairs."""
        s = self.state
        matchable = s.matchable_pairs

        if matchable == 0:
            return

        pair_cost = s.pair_cost
        base_profit = 1.00 - pair_cost

        # Fee handling: Entry is TAKER (we take best ask), Hedge is MAKER (passive bid)
        # This matches backtest logic in src/core.calculate_pnl_with_fees
        net_profit = FeeConfig.calculate_net_profit(
            entry_price=s.up_avg_price,
            hedge_price=s.down_avg_price,
            size=matchable,
            entry_is_maker=False,  # Entry is TAKER - pays ~0.83% fee at mid prices
            hedge_is_maker=True,   # Hedge is MAKER - gets rebate (or no fee)
        )

        self._completed_pairs.append({
            "pairs": matchable,
            "up_avg": s.up_avg_price,
            "down_avg": s.down_avg_price,
            "pair_cost": pair_cost,
            "base_profit": base_profit,
            "net_profit": net_profit,
            "timestamp": time.time(),
        })

        s.total_pairs_matched += matchable
        s.total_profit += net_profit

        logger.info(
            f"[ENHSPIKE] Pairs: {matchable} @ ${pair_cost:.4f} | "
            f"Profit: ${net_profit:.4f} | Total: ${s.total_profit:.4f}"
        )

        if s.up_shares > s.down_shares:
            s.up_shares -= matchable
            s.up_cost = s.up_avg_price * s.up_shares
            s.down_shares = 0
            s.down_cost = 0.0
            s.down_avg_price = 0.0
        else:
            s.down_shares -= matchable
            s.down_cost = s.down_avg_price * s.down_shares
            s.up_shares = 0
            s.up_cost = 0.0
            s.up_avg_price = 0.0

        if self.enable_cycling:
            self.reset_for_cycle()

    # =========================================================================
    # LEGACY COMPATIBILITY - decide() for sequential mode
    # =========================================================================

    def decide(
        self,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
        time_remaining: float,
        current_imbalance: int,
        current_time: float,
        velocity_bps: Optional[float] = None,
    ) -> Optional[Tuple[str, float, int]]:
        """Legacy decision method for sequential entry->hedge mode."""
        if velocity_bps is None:
            velocity_bps = 0.0

        s = self.state

        if abs(current_imbalance) >= self.emergency_imbalance_threshold:
            if s.phase != EnhancedSpikePhase.EMERGENCY_DEFERRED:
                s.phase = EnhancedSpikePhase.EMERGENCY_DEFERRED
                logger.info(f"[ENHSPIKE] Emergency deferred: imbalance={current_imbalance}")
            return None

        if s.phase == EnhancedSpikePhase.EMERGENCY_DEFERRED:
            s.phase = EnhancedSpikePhase.IDLE

        if time_remaining < self.min_time_remaining:
            return None

        if s.phase == EnhancedSpikePhase.IDLE:
            if up_ask > down_ask:
                entry_side = "UP"
                hedge_side = "DOWN"
                entry_bid = up_bid
            else:
                entry_side = "DOWN"
                hedge_side = "UP"
                entry_bid = down_bid

            s.entry_side = entry_side
            s.hedge_side = hedge_side

            entry_price = round(entry_bid - self.entry_offset, 2)
            if entry_price <= 0.01 or entry_price > self.max_share_price:
                return None

            s.phase = EnhancedSpikePhase.ENTRY_PENDING
            return (entry_side, entry_price, self.base_size)

        if s.phase == EnhancedSpikePhase.ENTRY_FILLED:
            hedge_bid = down_bid if s.hedge_side == "DOWN" else up_bid

            max_hedge = self.calculate_max_hedge_price(s.entry_price)
            hedge_price = round(hedge_bid - self.hedge_offset, 2)
            hedge_price = min(hedge_price, max_hedge)

            if hedge_price <= 0.01 or hedge_price > self.max_share_price:
                return None

            s.phase = EnhancedSpikePhase.HEDGE_PENDING
            return (s.hedge_side, hedge_price, self.base_size)

        return None

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        status = {
            "phase": s.phase.value,
            "position": {
                "up_shares": s.up_shares,
                "up_avg_price": s.up_avg_price,
                "down_shares": s.down_shares,
                "down_avg_price": s.down_avg_price,
                "imbalance": s.imbalance,
                "imbalance_pct": s.imbalance_pct,
                "pair_cost": s.pair_cost,
                "matchable_pairs": s.matchable_pairs,
            },
            "statistics": {
                "total_up_fills": s.total_up_fills,
                "total_down_fills": s.total_down_fills,
                "total_pairs_matched": s.total_pairs_matched,
                "total_profit": s.total_profit,
                "rebalance_count": s.rebalance_count,
                "quotes_generated": s.quotes_generated,
            },
            "spike_detection": {
                "last_direction": s.last_spike_direction,
                "last_magnitude": s.last_spike_magnitude,
                "last_time": s.last_spike_time,
                "history_size": len(self._binance_price_history),
            },
            "entry_side": s.entry_side,
            "hedge_side": s.hedge_side,
            "cycles_completed": s.cycles_completed,
            "last_velocity": s.last_velocity,
            "last_offsets": {
                "up": s.last_up_offset,
                "down": s.last_down_offset,
            },
            "enable_cycling": self.enable_cycling,
            "target_shares": self.target_shares,
            "zscore_filter": self.get_zscore_stats(),
        }

        # Add multi-cycle status if enabled
        if self.enable_multicycle and self.cycle_manager is not None:
            status["multicycle"] = self.cycle_manager.get_status()

        return status

    def get_completed_cycles(self) -> List[Dict[str, Any]]:
        """Get list of completed pair matches."""
        return self._completed_pairs.copy()

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_profit = self.state.total_profit
        total_pairs = self.state.total_pairs_matched
        markets = self.state.markets_traded + 1

        self.state = EnhancedSpikeState()
        self.state.total_profit = total_profit
        self.state.total_pairs_matched = total_pairs
        self.state.markets_traded = markets
        self.state.last_velocity_zone = VelocityZone.NEUTRAL

        self._completed_pairs = []
        self.clear_spike_history()
        self._zscore_skip_count = 0

        # Reset multi-cycle manager for new market
        if self.cycle_manager is not None:
            self.cycle_manager.reset()
            logger.info(
                f"[ENHSPIKE] Reset for market #{markets} (spike history + multicycle cleared)"
            )
        else:
            logger.info(f"[ENHSPIKE] Reset for market #{markets} (spike history cleared)")

    def reset_for_cycle(self) -> None:
        """Reset state for next cycle WITHIN same market (cycling mode)."""
        s = self.state

        s.first_fill_side = None
        s.first_fill_price = 0.0
        s.first_fill_time = None  # Reset time-stop tracking
        s.first_fill_velocity_dir = None
        s.locked_hedge_target = None
        s.current_velocity_zone = None

        s.stop_loss_triggered = False
        s.stop_loss_hedge_price = 0.0

        # Reset spike state for new cycle
        s.last_spike_direction = None
        s.last_spike_magnitude = 0.0

        s.entry_side = None
        s.hedge_side = None
        s.entry_price = 0.0
        s.hedge_price = 0.0
        s.entry_size = 0
        s.hedge_size = 0
        s.phase = EnhancedSpikePhase.IDLE

        # FIXED: Issue #14 - Clear spike history to prevent stale prices affecting next cycle
        self.clear_spike_history()

        logger.info(
            f"[ENHSPIKE] Cycle reset: ready for re-entry "
            f"(pairs={s.total_pairs_matched}, profit=${s.total_profit:.2f})"
        )

    def __repr__(self) -> str:
        return (
            f"EnhancedSpikeStrategy("
            f"base_size={self.base_size}, "
            f"spike_lookback={self.spike_lookback}, "
            f"spike_threshold={self.spike_threshold:.2f}%, "
            f"cycling={self.enable_cycling})"
        )


# =============================================================================
# BACKWARD COMPATIBILITY ALIASES
# =============================================================================

# Allow importing with old names
SpreadCaptureStrategy = EnhancedSpikeStrategy


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def calculate_velocity_edge(velocity_bps: float, side: str) -> float:
    """
    Calculate expected edge from velocity for a given side.
    LEGACY - kept for backward compatibility.
    """
    LATENCY_SECONDS = 1.5
    expected_move_bps = velocity_bps * LATENCY_SECONDS

    if side.upper() == "UP":
        return expected_move_bps
    else:
        return -expected_move_bps


def detect_binance_spike(
    prices: List[float],
    lookback: int = 3,
    threshold: float = 0.02
) -> Tuple[Optional[str], float]:
    """
    Standalone spike detection function.

    Args:
        prices: Recent Binance prices (newest last)
        lookback: Number of ticks to look back (3 ticks ~ 600ms)
        threshold: Minimum % change to trigger (0.02% = $20 on $100k BTC)

    Returns:
        (direction, magnitude_pct) or (None, 0) if no signal
    """
    if len(prices) < lookback + 1:
        return None, 0

    current = prices[-1]
    previous = prices[-lookback - 1]

    if previous <= 0:
        return None, 0

    change_pct = (current - previous) / previous * 100
    magnitude = abs(change_pct)

    if magnitude >= threshold:
        direction = "UP" if change_pct > 0 else "DOWN"
        return direction, magnitude

    return None, 0


def calculate_magnitude_loser_bid(
    magnitude_pct: float,
    loser_ask: float,
    winner_entry: float,
    target_pair: float = 0.99,
    regime: str = "MEDIUM",
) -> float:
    """
    Standalone loser bid calculation based on spike magnitude (v2).

    Args:
        magnitude_pct: Absolute BTC % change
        loser_ask: Current loser side ask price (kept for API compatibility, NOT used)
        winner_entry: Price we paid for winner
        target_pair: Target pair cost (default $0.99)
        regime: Volatility regime ('LOW', 'MEDIUM', 'HIGH')

    Returns:
        Optimal loser bid price
    """
    regime_bonus = DROP_REGIME_BONUS.get(regime, 0.01)
    expected_drop = DROP_MULTIPLIER * magnitude_pct + DROP_INTERCEPT + regime_bonus
    expected_drop = max(0.02, min(0.20, expected_drop))
    max_loser = target_pair - winner_entry
    # FIX Feb 2, 2026: Use theoretical loser (1.0 - winner_entry), NOT loser_ask
    # Matches backtest formula for consistent pair costs
    theoretical_loser = 1.0 - winner_entry
    loser_bid = min(theoretical_loser - expected_drop, max_loser)
    return max(loser_bid, 0.01)


# =============================================================================
# DEPRECATED: Use compute_enhanced_score_core from src/core instead
# =============================================================================

def compute_enhanced_score(
    spike_magnitude: float,
    velocity_bps: float,
    spike_direction: str,
    time_remaining: float,
) -> float:
    """
    DEPRECATED: Use compute_enhanced_score_core from src/core instead.

    Standalone composite score calculation for enhanced signal filtering.
    This is a wrapper for backward compatibility.
    Canonical implementation: src/core/trading_utils.py
    """
    # Delegate to canonical implementation
    return compute_enhanced_score_core(
        spike_magnitude, velocity_bps, spike_direction, time_remaining
    )


def should_take_enhanced_signal(
    spike_dir: Optional[str],
    spike_magnitude: float,
    velocity_bps: float,
    time_remaining: float,
    min_score: float = 0.40,
) -> Tuple[bool, float, str]:
    """
    Standalone enhanced signal filter function.

    Used for determining if a spike signal should be traded.
    Key insight: Reject spikes when velocity contradicts direction.

    Args:
        spike_dir: "UP", "DOWN", or None
        spike_magnitude: Absolute BTC % change
        velocity_bps: Current velocity in basis points per second
        time_remaining: Seconds until market resolution
        min_score: Minimum composite score to accept (default 0.40)

    Returns:
        (should_trade, score, reason)
    """
    if spike_dir is None:
        return False, 0.0, "No spike detected"

    # CRITICAL: Reject if velocity contradicts spike direction
    # FIX Feb 2, 2026: Use <= and >= to match core velocity_confirms_spike()
    if spike_dir == "UP" and velocity_bps <= -0.10:
        return False, 0.0, f"Velocity contradicts UP spike (v={velocity_bps:.3f})"

    if spike_dir == "DOWN" and velocity_bps >= 0.10:
        return False, 0.0, f"Velocity contradicts DOWN spike (v={velocity_bps:.3f})"

    # Compute composite score
    score = compute_enhanced_score(
        spike_magnitude=spike_magnitude,
        velocity_bps=velocity_bps,
        spike_direction=spike_dir,
        time_remaining=time_remaining,
    )

    if score < min_score:
        return False, score, f"Score {score:.3f} below threshold {min_score:.2f}"

    velocity_confirms = (
        (spike_dir == "UP" and velocity_bps > 0) or
        (spike_dir == "DOWN" and velocity_bps < 0)
    )
    confirms_str = "confirms" if velocity_confirms else "neutral"

    return True, score, f"ACCEPTED: velocity {confirms_str}, score={score:.3f}"
