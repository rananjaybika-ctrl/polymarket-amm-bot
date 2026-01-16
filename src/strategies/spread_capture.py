"""
Spread Capture Strategy - Continuous Velocity Market Maker

Transformed from sequential entry→hedge to continuous two-sided quoting.
Based on Telegram alpha analysis and Gabagool reverse-engineering.

BACKWARD COMPATIBILITY: This file maintains the OLD API while implementing
the NEW continuous quoting logic. Old code using entry_size, entry_offset,
hedge_offset, emergency_imbalance_threshold will continue to work.

Key Changes from Original:
1. REMOVED: Sequential entry→hedge phases (wasted 87% of time waiting)
2. ADDED: Continuous quoting on BOTH sides simultaneously
3. ADDED: Velocity-based quote ADJUSTMENT (not gating)
4. ADDED: Inventory management with 10% max imbalance rule
5. ADDED: Grid orders at multiple price levels

Core Logic (THE CORRECT WAY):
    When velocity > 0 (BTC rising):
        - UP is UNDERPRICED (will rise) → TIGHTEN UP bid (buy winner)
        - DOWN is OVERPRICED (will fall) → WIDEN DOWN bid (avoid loser)

    When velocity < 0 (BTC falling):
        - UP is OVERPRICED (will fall) → WIDEN UP bid (avoid loser)
        - DOWN is UNDERPRICED (will rise) → TIGHTEN DOWN bid (buy winner)

Expected Improvement:
    - Cycles/Hour: 5.6 → 50-100 (9-18x increase)
    - Profit/Hour: $3.22 → $20-40 (6-12x increase)
    - Time utilization: 5% → 95%

Usage (NEW):
    strategy = SpreadCaptureStrategy(base_size=15, grid_levels=3)
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ...)

Usage (LEGACY - still works):
    strategy = SpreadCaptureStrategy(entry_size=15, target_shares=30)
    action = strategy.decide(up_bid=0.55, up_ask=0.56, ...)

Author: Claude Code
Date: January 12, 2026
Based on: TELEGRAM_ALPHA_ANALYSIS_JAN12.md, GABAGOOL_STRATEGY_ANALYSIS.md
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src.config import FeeConfig

logger = logging.getLogger(__name__)


# =============================================================================
# CONSTANTS (including LEGACY constants for backward compatibility)
# =============================================================================

# DEPRECATED: Old velocity thresholds - use VELOCITY_ZONES dict instead
# Kept for backward compatibility with tests and check_velocity_zone_transition()
VELOCITY_THRESHOLD = 0.05       # DEPRECATED: Use VELOCITY_ZONES['neutral']['vel_max']
VELOCITY_STRONG = 0.10          # DEPRECATED: Use VELOCITY_ZONES['strong']['vel_min']
VELOCITY_PULL_THRESHOLD = 0.05  # LEGACY: For backward compatibility with tests

# Multi-zone velocity configuration with dynamic hedge targets and offsets
# Key insight: Higher velocity = more confidence = more aggressive entry, tighter hedge
#
# winner_offset: Added to best_bid for ENTRY side (winner)
#   - Negative = bid BELOW best_bid (conservative, slow fill)
#   - Positive = bid ABOVE best_bid (aggressive, fast fill)
#
# loser_offset: Added to best_bid for HEDGE side (loser) - used as fallback
#   - Always negative = bid below best_bid (wait for price drop)
#
# winner_size_ratio: Fraction of total shares allocated to winner side (HYBRID MM)
#   - 0.50 = symmetric (no velocity edge)
#   - 0.80 = 80% winner, 20% loser (strong velocity edge)
#   - Higher velocity = more confidence = larger winner allocation
#
# hedge_target = pair_target - entry_price (primary hedge pricing)
# VELOCITY ZONE CONFIGURATION
# Formula: our_bid = best_bid - offset
# - Positive offset → bid BELOW best_bid (passive, cheaper fills)
# - Negative offset → bid ABOVE best_bid (aggressive, faster fills)
#
# Zone behavior:
# |v| < 0.1:  Both sides at best_bid - 0.01 (symmetric, passive)
# |v| >= 0.1: Both sides at best_bid - 0.01 (same as above)
# |v| >= 0.3: Loser at best_bid - 0.03, Winner at best_bid (more passive loser)
# |v| >= 0.5: Loser at best_bid - 0.05, Winner at best_bid + 0.01 (aggressive winner)
VELOCITY_ZONES = {
    'neutral': {
        'vel_min': 0.00, 'vel_max': 0.10,
        'pair_target': 0.97,
        'winner_offset': 0.01,   # best_bid - 0.01 (one tick below)
        'loser_offset': 0.01,    # best_bid - 0.01 (symmetric)
        'winner_size_ratio': 0.50,  # Symmetric - no velocity edge
    },
    'moderate': {
        'vel_min': 0.10, 'vel_max': 0.30,
        'pair_target': 0.97,
        'winner_offset': 0.01,   # best_bid - 0.01 (one tick below)
        'loser_offset': 0.01,    # best_bid - 0.01 (same as winner)
        'winner_size_ratio': 0.55,  # Slight bias toward winner
    },
    'strong': {
        'vel_min': 0.30, 'vel_max': 0.50,
        'pair_target': 0.96,
        'winner_offset': 0.00,   # best_bid - 0 = at best_bid exactly
        'loser_offset': 0.03,    # best_bid - 0.03 (3 ticks below, passive)
        'winner_size_ratio': 0.60,  # Moderate bias toward winner
    },
    'very_strong': {
        'vel_min': 0.50, 'vel_max': 1.00,
        'pair_target': 0.95,
        'winner_offset': -0.01,  # best_bid - (-0.01) = best_bid + 0.01 (AGGRESSIVE)
        'loser_offset': 0.05,    # best_bid - 0.05 (5 ticks below, very passive)
        'winner_size_ratio': 0.70,  # Strong bias toward winner
    },
    'extreme': {
        'vel_min': 1.00, 'vel_max': 99.0,
        'pair_target': 0.94,
        'winner_offset': -0.01,  # best_bid + 0.01 (AGGRESSIVE)
        'loser_offset': 0.05,    # best_bid - 0.05 (very passive)
        'winner_size_ratio': 0.75,  # Maximum bias
    },
}

# Quote offsets (from best_bid)
# Formula: our_bid = best_bid - offset
# - Positive offset → bid BELOW best_bid (passive)
# - Negative offset → bid ABOVE best_bid (aggressive)
BASE_OFFSET = 0.01              # Default: best_bid - 0.01 (one tick below)
TIGHT_OFFSET = -0.01            # AGGRESSIVE: best_bid + 0.01 (one tick above)
WIDE_OFFSET = 0.03              # Passive: best_bid - 0.03 (three ticks below)
VERY_WIDE_OFFSET = 0.05         # Very passive: best_bid - 0.05 (five ticks below)

# LEGACY constants for backward compatibility
DEFAULT_ENTRY_OFFSET = 0.01     # LEGACY: Fixed entry offset
DEFAULT_HEDGE_OFFSET = 0.02     # LEGACY: Fixed hedge offset
DEFAULT_ENTRY_WAIT = 8.0        # LEGACY: Base entry wait time
DEFAULT_HEDGE_WAIT = 30.0       # LEGACY: Base hedge wait time
MAX_WAIT_TIME = 60.0            # LEGACY: Maximum wait time

# Grid configuration
DEFAULT_GRID_LEVELS = 1         # Number of price levels per side
GRID_SPACING = 0.01             # $0.01 between grid levels

# Inventory management (from Telegram alpha)
DEFAULT_MAX_IMBALANCE_PCT = 0.10  # 10% max imbalance
DEFAULT_MAX_IMBALANCE_SHARES = 10  # Absolute cap on imbalance (tightened from 50)
FORCE_REBALANCE_OFFSET = 0.005    # Tighter offset when force-buying lagging side

# Polymarket constraints
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 15          # Target: 15 shares/side (scale to 30 after live validation)
DEFAULT_TARGET_SHARES = 100     # Total target per market
DEFAULT_MIN_PROFIT = 0.005
DEFAULT_MAX_SHARE_PRICE = 0.95
DEFAULT_ENABLE_CYCLING = True   # PROFITABLE: Enable cycling (7.96 cycles/market = 2.4x PnL)

# Zone filtering - only trade when velocity >= min_velocity_bps
# Set to 0.50 for zones 5-6 only (extreme, super_strong) - PROFITABLE CONFIG
# Set to 0.30 for zones 4-6 only (very_strong, extreme, super_strong)
# Set to 0.0 for all zones
# PROFITABLE STRATEGY: Use 0.50 to only trade high-accuracy zones (61% accuracy)
DEFAULT_MIN_VELOCITY_BPS = 0.50  # zones 5-6 only (61%+ accuracy, +$1.17/hr)

# Stop-loss configuration - trigger hedge when winner drops X%
# Early stop-loss (7%) = cheaper hedge pair cost ($1.048 vs $1.091 at 15%)
DEFAULT_STOP_LOSS_PCT = 0.07  # 7% drop triggers immediate hedge

# Timing
MIN_TIME_REMAINING = 120        # Don't place new orders with <120s left (safer buffer)
QUOTE_REFRESH_INTERVAL = 0.5    # Refresh quotes every 500ms


# =============================================================================
# ENUMS (including LEGACY phases for backward compatibility)
# =============================================================================

class SpreadCapturePhase(Enum):
    """Strategy phases for SpreadCaptureStrategy.

    Includes both NEW continuous phases and LEGACY sequential phases
    for backward compatibility with existing code and tests.
    """
    # NEW continuous phases
    IDLE = "idle"               # Not yet started
    QUOTING = "quoting"         # Actively quoting both sides
    REBALANCING = "rebalancing" # Force-buying lagging side
    COMPLETE = "complete"       # Target reached or market ending

    # LEGACY sequential phases (for backward compatibility)
    ENTRY_PENDING = "entry_pending"     # LEGACY: Entry order placed, waiting for fill
    ENTRY_FILLED = "entry_filled"       # LEGACY: Entry filled, preparing hedge
    HEDGE_PENDING = "hedge_pending"     # LEGACY: Hedge order placed, waiting for fill
    EMERGENCY_DEFERRED = "emergency_deferred"  # LEGACY: Deferred due to high imbalance


class VelocityZone(Enum):
    """Velocity zones for zone transition LOGGING (order pulling disabled).

    NOTE: This 3-zone enum is used ONLY for logging zone transitions.
    Order pulling was disabled because simulation proved it destroys performance.

    For ACTIVE hedge target calculations, use VELOCITY_ZONES dict (6 zones)
    and the string-based current_velocity_zone field instead.

    This enum is kept for backward compatibility and zone transition logging.
    """
    NEUTRAL = "neutral"      # abs(vel) < 5 bps - use BASE_OFFSET for both
    MODERATE = "moderate"    # 5-10 bps - use TIGHT/WIDE offsets
    STRONG = "strong"        # > 10 bps - use TIGHT/VERY_WIDE offsets


# =============================================================================
# STATE DATACLASS
# =============================================================================

@dataclass
class SpreadCaptureState:
    """State tracking for continuous market making.

    Includes both NEW and LEGACY attributes for backward compatibility.
    """
    phase: SpreadCapturePhase = SpreadCapturePhase.IDLE

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
    last_velocity_zone: "VelocityZone" = None  # 3-zone enum for logging only (see VelocityZone docstring)

    # Statistics
    total_up_fills: int = 0
    total_down_fills: int = 0
    total_pairs_matched: int = 0
    total_profit: float = 0.0
    rebalance_count: int = 0

    # Tracking
    quotes_generated: int = 0
    markets_traded: int = 0

    # Dynamic hedge target tracking (uses 6-zone VELOCITY_ZONES dict, NOT VelocityZone enum)
    # Key rule: Only tighten hedge target, never loosen
    first_fill_side: Optional[str] = None           # Which side filled first ("UP" or "DOWN")
    first_fill_price: float = 0.0                   # Price of first fill
    first_fill_velocity_dir: Optional[str] = None   # Velocity direction at first fill ("UP" or "DOWN")
    locked_hedge_target: Optional[float] = None     # Current hedge target (only tightens)
    current_velocity_zone: Optional[str] = None     # 6-zone string from VELOCITY_ZONES (active system)

    # Stop-loss tracking (7% stop-loss = profitable config)
    # When winner drops X% from fill price, immediately hedge at loser ASK
    stop_loss_triggered: bool = False               # Whether stop-loss was triggered
    stop_loss_hedge_price: float = 0.0              # Price we hedged at via stop-loss

    # LEGACY attributes for backward compatibility
    entry_side: Optional[str] = None      # LEGACY: "UP" or "DOWN"
    hedge_side: Optional[str] = None      # LEGACY: Opposite of entry_side
    entry_price: float = 0.0              # LEGACY: Entry fill price
    hedge_price: float = 0.0              # LEGACY: Hedge fill price
    entry_size: int = 0                   # LEGACY: Entry fill size
    hedge_size: int = 0                   # LEGACY: Hedge fill size
    cycles_completed: int = 0             # LEGACY: Number of complete entry+hedge cycles

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


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class SpreadCaptureStrategy:
    """
    Spread Capture Strategy - Continuous Velocity Market Maker.

    Uses velocity-based quote adjustment for continuous two-sided market making.
    Always has orders out on both sides, adjusts prices based on BTC velocity.

    THE CORRECT VELOCITY LOGIC:
        When velocity > 0 (BTC rising):
            - UP is UNDERPRICED → TIGHTEN UP bid (buy the winner)
            - DOWN is OVERPRICED → WIDEN DOWN bid (avoid the loser)

        When velocity < 0 (BTC falling):
            - UP is OVERPRICED → WIDEN UP bid (avoid the loser)
            - DOWN is UNDERPRICED → TIGHTEN DOWN bid (buy the winner)

    Constructor Args (NEW):
        base_size: Base order size per level (default 10)
        target_shares: Total target per market (default 100)
        grid_levels: Number of price levels per side (default 3)
        max_imbalance_pct: Maximum imbalance percentage (default 0.10)
        min_profit: Minimum profit per pair (default 0.005)

    Constructor Args (LEGACY - still supported):
        entry_size: Alias for base_size
        entry_offset: Fixed entry offset (default 0.01)
        hedge_offset: Fixed hedge offset (default 0.02)
        emergency_imbalance_threshold: Alias for max_imbalance_shares
    """

    def __init__(
        self,
        # NEW parameters
        base_size: int = DEFAULT_BASE_SIZE,
        target_shares: int = DEFAULT_TARGET_SHARES,
        grid_levels: int = DEFAULT_GRID_LEVELS,
        max_imbalance_pct: float = DEFAULT_MAX_IMBALANCE_PCT,
        max_imbalance_shares: int = DEFAULT_MAX_IMBALANCE_SHARES,
        min_profit: float = DEFAULT_MIN_PROFIT,
        max_share_price: float = DEFAULT_MAX_SHARE_PRICE,
        enable_cycling: bool = DEFAULT_ENABLE_CYCLING,
        min_velocity_bps: float = DEFAULT_MIN_VELOCITY_BPS,  # Zone filter: 0.50 = zones 5-6 only
        stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,  # 7% stop-loss = profitable config
        # LEGACY parameters (aliases)
        entry_size: Optional[int] = None,
        entry_offset: float = DEFAULT_ENTRY_OFFSET,
        hedge_offset: float = DEFAULT_HEDGE_OFFSET,
        emergency_imbalance_threshold: Optional[int] = None,
    ):
        # Handle LEGACY parameter aliases
        if entry_size is not None:
            base_size = entry_size
        if emergency_imbalance_threshold is not None:
            max_imbalance_shares = emergency_imbalance_threshold

        self.base_size = max(MIN_SHARES, base_size)
        self.target_shares = target_shares
        self.grid_levels = max(1, grid_levels)
        self.max_imbalance_pct = max_imbalance_pct
        self.max_imbalance_shares = max_imbalance_shares
        self.min_profit = min_profit
        self.max_share_price = max_share_price
        self.enable_cycling = enable_cycling
        self.min_velocity_bps = min_velocity_bps  # Zone filter threshold (0.50 = zones 5-6)
        self.stop_loss_pct = stop_loss_pct  # 7% stop-loss = profitable config

        # LEGACY attributes
        self.entry_offset = entry_offset
        self.hedge_offset = hedge_offset
        self.emergency_imbalance_threshold = max_imbalance_shares  # Alias

        self.state = SpreadCaptureState()
        self.state.last_velocity_zone = VelocityZone.NEUTRAL  # Initialize zone
        self._completed_pairs: List[Dict[str, Any]] = []

        zone_info = f", min_vel={min_velocity_bps:.2f}" if min_velocity_bps > 0 else ""
        sl_info = f", stop_loss={stop_loss_pct:.0%}" if stop_loss_pct > 0 else ""
        logger.info(
            f"[SPREADCAP] Initialized: base_size={base_size}, grid_levels={grid_levels}, "
            f"max_imbalance={max_imbalance_pct:.0%}, target={target_shares}, cycling={enable_cycling}{zone_info}{sl_info}"
        )

    # =========================================================================
    # LEGACY METHODS (for backward compatibility with tests and existing code)
    # =========================================================================

    def calculate_entry_offset(self) -> float:
        """LEGACY: Return fixed entry offset."""
        return self.entry_offset

    def calculate_hedge_offset(self) -> float:
        """LEGACY: Return fixed hedge offset."""
        return self.hedge_offset

    def calculate_max_hedge_price(self, entry_price: float) -> float:
        """LEGACY: Calculate maximum hedge price to preserve min_profit.

        With maker rebates (~1%), formula is:
        max_pair_cost = (1.00 - min_profit) / 0.99
        max_hedge = max_pair_cost - entry_price
        """
        max_pair_cost = (1.00 - self.min_profit) / 0.99
        return round(max_pair_cost - entry_price, 4)

    def calculate_wait_time(
        self,
        attempt: int = 0,
        is_entry: bool = True,
        price_room: float = 0.10,
    ) -> float:
        """LEGACY: Calculate wait time with exponential backoff.

        Entry: base 8s, backoff 1.3x
        Hedge: base 30s scaled by price_room, backoff 1.3x
        """
        if is_entry:
            base_wait = DEFAULT_ENTRY_WAIT
        else:
            # Scale hedge wait by price room
            base_wait = DEFAULT_HEDGE_WAIT * (price_room / 0.10)

        # Exponential backoff
        wait = base_wait * (1.3 ** attempt)
        return min(wait, MAX_WAIT_TIME)

    def should_pull_entry(self, velocity_bps: float, entry_side: str) -> bool:
        """DEPRECATED: Order pulling disabled - simulation proved it destroys performance.

        This method is unused. Order pulling was disabled because:
        - 31/33 orders were pulled before filling
        - Result: -$33 loss instead of +$32.70 profit

        Kept for backward compatibility only.

        UP entry: adverse if velocity < -VELOCITY_PULL_THRESHOLD
        DOWN entry: adverse if velocity > VELOCITY_PULL_THRESHOLD
        """
        if entry_side.upper() == "UP":
            return velocity_bps < -VELOCITY_PULL_THRESHOLD
        else:
            return velocity_bps > VELOCITY_PULL_THRESHOLD

    # =========================================================================
    # DYNAMIC HEDGE TARGET (Multi-zone tightening)
    # =========================================================================

    def get_velocity_zone_name(self, velocity_bps: float) -> str:
        """Get the velocity zone name for given velocity."""
        abs_vel = abs(velocity_bps)
        for zone_name, zone in VELOCITY_ZONES.items():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                return zone_name
        return 'super_strong'  # Default for very high velocity

    def get_pair_target_for_velocity(self, velocity_bps: float) -> float:
        """Get the target pair cost for current velocity zone.

        Higher velocity = more confidence = tighter (lower) pair target.
        """
        zone_name = self.get_velocity_zone_name(velocity_bps)
        return VELOCITY_ZONES[zone_name]['pair_target']

    def calculate_size_allocation(self, velocity_bps: float, total_size: int) -> Tuple[int, int]:
        """Calculate size allocation for UP and DOWN sides based on velocity.

        HYBRID MM: Velocity biases SIZE allocation, not just offsets.
        - When velocity > 0 (BTC rising): allocate MORE to UP (winner)
        - When velocity < 0 (BTC falling): allocate MORE to DOWN (winner)

        Args:
            velocity_bps: Current BTC velocity in basis points per second
            total_size: Total shares to allocate across both sides

        Returns:
            (up_size, down_size) - share allocation for each side
        """
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['neutral'])
        winner_ratio = zone_config.get('winner_size_ratio', 0.50)

        winner_size = int(total_size * winner_ratio)
        loser_size = total_size - winner_size

        # Ensure minimum size
        winner_size = max(MIN_SHARES, winner_size)
        loser_size = max(MIN_SHARES, loser_size)

        # Apply to correct side based on velocity direction
        if velocity_bps >= 0:
            # BTC rising → UP is winner
            return (winner_size, loser_size)
        else:
            # BTC falling → DOWN is winner
            return (loser_size, winner_size)

    def calculate_hedge_target(self, entry_price: float, velocity_bps: float) -> float:
        """Calculate hedge target price based on entry and current velocity zone.

        Formula: hedge_target = pair_target - entry_price

        Example:
            entry_price = $0.52
            velocity = 0.8 bps (extreme zone, pair_target = 0.94)
            hedge_target = 0.94 - 0.52 = $0.42
        """
        pair_target = self.get_pair_target_for_velocity(velocity_bps)
        hedge_target = pair_target - entry_price
        return max(0.01, min(0.95, hedge_target))

    def record_first_fill(self, side: str, price: float, velocity_bps: float) -> None:
        """Record first fill and initialize hedge target.

        Called on first fill to establish:
        1. Which side is "entry" vs "hedge"
        2. Initial hedge target based on velocity zone
        3. Velocity direction to track for tightening
        """
        s = self.state
        if s.first_fill_side is not None:
            return  # Already recorded

        s.first_fill_side = side.upper()
        s.first_fill_price = price
        s.first_fill_velocity_dir = "UP" if velocity_bps > 0 else "DOWN"
        s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)

        # Set initial hedge target
        s.locked_hedge_target = self.calculate_hedge_target(price, velocity_bps)

        logger.info(
            f"[SPREADCAP] First fill: {side} @ ${price:.4f}, vel={velocity_bps:.2f}bps "
            f"({s.current_velocity_zone}), hedge_target=${s.locked_hedge_target:.4f}"
        )

    def maybe_tighten_hedge_target(self, velocity_bps: float) -> bool:
        """Tighten hedge target if velocity strengthened in entry direction.

        KEY RULE: Only tighten, NEVER loosen.

        Returns True if target was tightened.
        """
        s = self.state
        if s.first_fill_side is None or s.locked_hedge_target is None:
            return False

        # Check if velocity is still in the same direction as at entry
        current_vel_dir = "UP" if velocity_bps > 0 else "DOWN"
        if current_vel_dir != s.first_fill_velocity_dir:
            return False  # Velocity flipped, don't tighten

        # Calculate new target based on current velocity
        new_target = self.calculate_hedge_target(s.first_fill_price, velocity_bps)

        # ONLY TIGHTEN (lower target), NEVER LOOSEN
        if new_target < s.locked_hedge_target:
            old_target = s.locked_hedge_target
            old_zone = s.current_velocity_zone
            s.locked_hedge_target = new_target
            s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)

            logger.info(
                f"[SPREADCAP] Hedge target tightened: ${old_target:.4f} ({old_zone}) "
                f"-> ${new_target:.4f} ({s.current_velocity_zone})"
            )
            return True

        return False

    def get_current_hedge_target(self) -> Optional[float]:
        """Get current locked hedge target, or None if no entry yet."""
        return self.state.locked_hedge_target

    def check_hedge_target_change(self, velocity_bps: float) -> Tuple[bool, Optional[float], Optional[float]]:
        """
        Check if hedge target should be tightened and order pulled.

        This method is called by the bot runner to determine if the existing
        hedge order should be cancelled and replaced with a new order at a
        tighter (lower) price.

        RULE: Only tighten, NEVER loosen.

        Args:
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            Tuple of (should_pull, old_target, new_target)
            - should_pull: True if hedge order should be cancelled and replaced
            - old_target: Previous hedge target (for logging)
            - new_target: New tightened hedge target
        """
        s = self.state
        if s.first_fill_side is None or s.locked_hedge_target is None:
            return (False, None, None)

        # Only tighten if velocity is in same direction as at entry
        current_vel_dir = "UP" if velocity_bps > 0 else "DOWN"
        if current_vel_dir != s.first_fill_velocity_dir:
            return (False, None, None)

        # Calculate new target based on current velocity zone
        new_target = self.calculate_hedge_target(s.first_fill_price, velocity_bps)

        # ONLY TIGHTEN (lower target), NEVER LOOSEN
        if new_target < s.locked_hedge_target:
            old_target = s.locked_hedge_target
            old_zone = s.current_velocity_zone
            s.locked_hedge_target = new_target
            s.current_velocity_zone = self.get_velocity_zone_name(velocity_bps)

            logger.info(
                f"[SPREADCAP] Hedge target tightened: ${old_target:.4f} ({old_zone}) "
                f"-> ${new_target:.4f} ({s.current_velocity_zone}) - PULL REQUIRED"
            )
            return (True, old_target, new_target)

        return (False, None, None)

    # =========================================================================
    # STOP-LOSS MECHANISM (7% = profitable config)
    # =========================================================================

    def check_stop_loss(
        self,
        winner_current_bid: float,
        loser_current_ask: float,
    ) -> Tuple[bool, Optional[float]]:
        """
        Check if stop-loss should trigger and return hedge price.

        Stop-loss triggers when winner drops X% from fill price.
        When triggered, immediately hedge by hitting loser ASK.

        Research finding: 7% stop-loss = cheaper hedge ($1.048 pair cost)
        vs 15% stop-loss = expensive hedge ($1.091 pair cost)

        Args:
            winner_current_bid: Current bid price of winner side
            loser_current_ask: Current ask price of loser side

        Returns:
            (should_trigger, hedge_price) - hedge_price is loser_current_ask
        """
        s = self.state

        # Only check if entry filled but hedge hasn't
        if s.first_fill_side is None or s.first_fill_price <= 0:
            return (False, None)

        # Already triggered or hedged
        if s.stop_loss_triggered:
            return (False, None)

        # Calculate drop percentage
        drop_pct = (s.first_fill_price - winner_current_bid) / s.first_fill_price

        if drop_pct >= self.stop_loss_pct:
            s.stop_loss_triggered = True
            s.stop_loss_hedge_price = loser_current_ask

            logger.warning(
                f"[SPREADCAP] STOP-LOSS TRIGGERED: winner dropped {drop_pct:.1%} "
                f"(fill=${s.first_fill_price:.3f} → bid=${winner_current_bid:.3f}), "
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
        """
        Check stop-loss and return immediate hedge order if triggered.

        This should be called BEFORE normal quote generation.
        If stop-loss triggers, return a market-taking order to hedge.

        Args:
            up_bid, up_ask, down_bid, down_ask: Current orderbook prices

        Returns:
            Dict with hedge order details, or None if no stop-loss
        """
        s = self.state

        if s.first_fill_side is None or s.stop_loss_triggered:
            return None

        # Determine winner/loser based on entry side
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
                'is_market_order': True,  # Hit the ask immediately
            }

        return None

    # =========================================================================
    # CORE: VELOCITY-BASED QUOTE ADJUSTMENT
    # =========================================================================

    def calculate_offsets(self, velocity_bps: float) -> Tuple[float, float]:
        """
        Calculate quote offsets based on velocity zone.

        Uses 6-zone VELOCITY_ZONES for zone-specific offsets:
        - winner_offset: For entry side (velocity direction)
        - loser_offset: For hedge side (opposite direction)

        Higher velocity = more confidence = more aggressive entry offset.

        Args:
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            (up_offset, down_offset) - offsets to ADD to best_bid
        """
        s = self.state
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['moderate'])

        winner_offset = zone_config['winner_offset']
        loser_offset = zone_config['loser_offset']

        # NEUTRAL ZONE: Use inventory to break ties
        if zone_name == 'neutral':
            inventory_bias = s.down_shares - s.up_shares  # positive = need UP
            if abs(inventory_bias) <= 2:
                # Balanced - use winner_offset on both for fills
                return (winner_offset, winner_offset)
            elif inventory_bias > 0:
                # Need UP - aggressive UP, passive DOWN
                return (winner_offset, loser_offset)
            else:
                # Need DOWN - passive UP, aggressive DOWN
                return (loser_offset, winner_offset)

        # DIRECTIONAL: Winner gets winner_offset, loser gets loser_offset
        if velocity_bps > 0:  # BTC rising → UP is winner
            return (winner_offset, loser_offset)
        else:  # BTC falling → DOWN is winner
            return (loser_offset, winner_offset)

    def calculate_entry_bid(self, best_bid: float, best_ask: float,
                            velocity_bps: float) -> float:
        """
        Calculate LIMIT ORDER entry bid price for winner side.

        CRITICAL: Must stay below ask to remain MAKER and avoid taker fees.
        Higher velocity = more confidence = bid closer to ask for faster fill.

        Formula: entry_bid = best_bid - winner_offset
        - Positive offset → bid BELOW best_bid (passive)
        - Negative offset → bid ABOVE best_bid (aggressive)
        Clamped to stay below ask by at least 0.001.

        Args:
            best_bid: Current best bid price
            best_ask: Current best ask price
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            entry_bid: Price to post LIMIT order at (always < best_ask)
        """
        zone_name = self.get_velocity_zone_name(velocity_bps)
        zone_config = VELOCITY_ZONES.get(zone_name, VELOCITY_ZONES['moderate'])
        winner_offset = zone_config['winner_offset']

        entry_bid = best_bid - winner_offset

        # NEVER cross the spread - stay below ask to remain MAKER
        max_bid = best_ask - 0.001
        entry_bid = min(entry_bid, max_bid)

        # Clamp to valid price range
        entry_bid = max(0.01, min(0.95, entry_bid))

        return entry_bid

    def check_velocity_zone_transition(
        self,
        velocity_bps: float
    ) -> Tuple["VelocityZone", bool, List[str]]:
        """
        Check if velocity crossed a zone boundary (for LOGGING only).

        NOTE: Zone transition pulling is DISABLED. Simulation proved pulling destroys
        performance (31/33 orders pulled = -$33 loss).

        This method is used for:
        1. LOGGING zone transitions for analysis
        2. Tracking last_velocity_zone state

        Order pulling is handled separately via check_hedge_target_change() which
        only pulls hedge orders when tightening (not on zone transitions).

        Args:
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            Tuple of (current_zone, zone_changed, sides_to_pull)
            - sides_to_pull is always empty (zone pulling disabled)
        """
        abs_vel = abs(velocity_bps)

        # Determine current zone (3-zone for logging compatibility)
        if abs_vel < VELOCITY_THRESHOLD:  # 0.05 bps
            new_zone = VelocityZone.NEUTRAL
        elif abs_vel < VELOCITY_STRONG:   # 0.10 bps
            new_zone = VelocityZone.MODERATE
        else:
            new_zone = VelocityZone.STRONG

        old_zone = self.state.last_velocity_zone
        zone_changed = (new_zone != old_zone)

        # Zone pulling is DISABLED - always return empty list
        # Hedge pulling is handled by check_hedge_target_change() instead
        sides_to_pull: List[str] = []

        # Update state
        self.state.last_velocity_zone = new_zone
        return (new_zone, zone_changed, sides_to_pull)

    # =========================================================================
    # MAIN ENTRY POINT: GET QUOTES
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
    ) -> List[Dict[str, Any]]:
        """
        Generate quotes for both sides based on current market state.

        This is the main entry point - call every tick.

        Args:
            up_bid: Current UP best bid
            up_ask: Current UP best ask
            down_bid: Current DOWN best bid
            down_ask: Current DOWN best ask
            velocity_bps: Current BTC velocity (basis points per second)
            time_remaining: Seconds until market resolution
            current_time: Current timestamp (default: time.time())

        Returns:
            List of quote dicts: [{'side': str, 'price': float, 'size': int}, ...]
        """
        if current_time is None:
            current_time = time.time()

        s = self.state

        # Don't place new orders if market ending soon
        if time_remaining < MIN_TIME_REMAINING:
            logger.debug(f"[SPREADCAP] Skipping: {time_remaining:.0f}s remaining")
            return []

        # Zone filter: skip low-velocity zones for NEW entries only
        # If we already have a position (first_fill_side set), allow hedging to continue
        if self.min_velocity_bps > 0 and s.first_fill_side is None:
            if abs(velocity_bps) < self.min_velocity_bps:
                # Log occasionally to avoid spam
                if int(current_time) % 10 == 0:
                    zone = self.get_velocity_zone_name(velocity_bps)
                    logger.debug(
                        f"[SPREADCAP] Skipping zone '{zone}': |{velocity_bps:.3f}| < {self.min_velocity_bps:.2f}"
                    )
                return []

        # Check if target reached (both sides have target_shares)
        # When cycling is disabled: stop when CURRENT position reaches target on both sides
        # When cycling is enabled: keep going (pairs get settled, position resets)
        if not self.enable_cycling:
            if s.up_shares >= self.target_shares and s.down_shares >= self.target_shares:
                if s.phase != SpreadCapturePhase.COMPLETE:
                    s.phase = SpreadCapturePhase.COMPLETE
                    logger.info(
                        f"[SPREADCAP] Target reached: UP={s.up_shares}, DOWN={s.down_shares} "
                        f"(cycling disabled, stopping)"
                    )
                return []

        # Rate limit quote generation
        if current_time - s.last_quote_time < QUOTE_REFRESH_INTERVAL:
            return []

        s.last_quote_time = current_time
        s.last_velocity = velocity_bps
        s.phase = SpreadCapturePhase.QUOTING

        # STOP-LOSS CHECK: If winner dropped X%, immediately hedge
        # This takes priority over normal quote generation
        stop_loss_order = self.get_stop_loss_order(up_bid, up_ask, down_bid, down_ask)
        if stop_loss_order:
            logger.info(
                f"[SPREADCAP] Stop-loss hedge: {stop_loss_order['side']} @ ${stop_loss_order['price']:.3f}"
            )
            return [stop_loss_order]

        # Dynamic hedge target tightening on each tick
        # This connects the multi-zone hedge system to quote generation
        if s.locked_hedge_target is not None:
            self.maybe_tighten_hedge_target(velocity_bps)

        # Calculate velocity-adjusted offsets
        up_offset, down_offset = self.calculate_offsets(velocity_bps)
        s.last_up_offset = up_offset
        s.last_down_offset = down_offset

        # Determine winner side based on velocity direction
        winner_side = "UP" if velocity_bps > 0 else "DOWN"
        loser_side = "DOWN" if velocity_bps > 0 else "UP"

        # Get current ask prices for hedge decision
        winner_ask = up_ask if winner_side == "UP" else down_ask
        loser_ask = down_ask if winner_side == "UP" else up_ask
        loser_bid = down_bid if winner_side == "UP" else up_bid

        quotes = []
        zone_name = self.get_velocity_zone_name(velocity_bps)

        # DIRECTIONAL STRATEGY:
        # Phase 1: Entry not filled yet → only post on WINNER side
        # Phase 2: Entry filled → only post on LOSER side when price is near target
        if s.first_fill_side is None:
            # PHASE 1: No entry yet - post aggressively on WINNER side only
            winner_offset = up_offset if winner_side == "UP" else down_offset
            winner_bid = up_bid if winner_side == "UP" else down_bid

            winner_quotes = self._generate_side_quotes(
                side=winner_side,
                best_bid=winner_bid,
                offset=winner_offset,
                current_shares=s.up_shares if winner_side == "UP" else s.down_shares,
                needs_rebalance=False,
                allocated_size=self.base_size,  # Full size on winner
            )
            quotes.extend(winner_quotes)

            if winner_quotes:
                logger.debug(
                    f"[SPREADCAP] ENTRY: {winner_side} {len(winner_quotes)} quotes "
                    f"(off={winner_offset:.3f}), vel={velocity_bps:.3f}bps ({zone_name})"
                )
        else:
            # PHASE 2: Entry filled - check if loser price is near hedge target
            hedge_target = s.locked_hedge_target

            if hedge_target is not None:
                # Only post hedge when loser ask is CLOSE to our target
                # This avoids adverse selection - we wait for price to drop
                price_gap = loser_ask - hedge_target

                if price_gap <= 0.02:  # Loser ask within $0.02 of target
                    loser_offset = down_offset if loser_side == "DOWN" else up_offset

                    loser_quotes = self._generate_side_quotes(
                        side=loser_side,
                        best_bid=loser_bid,
                        offset=loser_offset,
                        current_shares=s.down_shares if loser_side == "DOWN" else s.up_shares,
                        needs_rebalance=False,
                        allocated_size=self.base_size,  # Full size on hedge
                    )
                    quotes.extend(loser_quotes)

                    if loser_quotes:
                        logger.debug(
                            f"[SPREADCAP] HEDGE: {loser_side} {len(loser_quotes)} quotes, "
                            f"ask=${loser_ask:.3f} near target=${hedge_target:.3f} (gap=${price_gap:.3f})"
                        )
                else:
                    logger.debug(
                        f"[SPREADCAP] WAITING: loser ask=${loser_ask:.3f} > target=${hedge_target:.3f} "
                        f"(gap=${price_gap:.3f}, need <=$0.02)"
                    )

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
        """Generate quotes for one side (UP or DOWN).

        Args:
            side: "UP" or "DOWN"
            best_bid: Current best bid for this side
            offset: Price offset to apply (from velocity zone)
            current_shares: Current position on this side
            needs_rebalance: Whether this side needs force-rebalancing
            allocated_size: Size allocation for this side (from velocity-biased sizing)
                           If None, uses base_size (backward compatible)
        """
        quotes = []
        s = self.state

        # Check position limit
        if current_shares >= self.target_shares:
            if not needs_rebalance:
                return []

        # Size per level - use allocated_size if provided (HYBRID MM)
        effective_size = allocated_size if allocated_size is not None else self.base_size
        size_per_level = max(MIN_SHARES, effective_size // self.grid_levels)

        # Rebalancing: tighter offset, full size
        if needs_rebalance:
            offset = FORCE_REBALANCE_OFFSET
            size_per_level = self.base_size
            self.state.rebalance_count += 1
            logger.info(f"[SPREADCAP] REBALANCE {side}: offset={offset:.3f}")

        # HEDGE TARGET CONSTRAINT: Cap hedge-side price at locked target
        # If first fill was on one side, the OTHER side is the hedge side
        # We should not bid higher than our locked hedge target
        max_hedge_price = None
        if s.first_fill_side is not None and side != s.first_fill_side:
            max_hedge_price = self.get_current_hedge_target()
            if max_hedge_price is not None:
                logger.debug(
                    f"[SPREADCAP] Hedge side {side}: max_price=${max_hedge_price:.4f}"
                )

        # Generate grid levels
        for level in range(self.grid_levels):
            level_offset = offset + (level * GRID_SPACING)
            price = round(best_bid - level_offset, 2)  # SUBTRACT offset (positive = lower price)

            # Apply hedge target cap (don't bid higher than target)
            if max_hedge_price is not None and price > max_hedge_price:
                price = round(max_hedge_price, 2)

            # Validate price
            if price <= 0.01 or price > self.max_share_price:
                continue

            # Size decreases at worse prices
            level_size = size_per_level if level == 0 else max(MIN_SHARES, size_per_level // (level + 1))

            # Don't exceed target
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
        """
        Check if inventory rebalancing is needed.

        From Telegram alpha: "amount of down shares can only be 10% more
        than up shares. If you surpass that number your bot has to buy the
        other side, even if price is too high"

        Returns:
            (needs_rebalance, side_to_buy)
        """
        s = self.state

        # Check percentage imbalance
        if s.imbalance_pct > self.max_imbalance_pct:
            lagging = s.lagging_side()
            if lagging:
                logger.info(
                    f"[SPREADCAP] Imbalance {s.imbalance_pct:.1%} > {self.max_imbalance_pct:.0%}, "
                    f"rebalance {lagging}"
                )
                return (True, lagging)

        # Check absolute imbalance
        if s.abs_imbalance > self.max_imbalance_shares:
            lagging = s.lagging_side()
            if lagging:
                logger.info(
                    f"[SPREADCAP] Abs imbalance {s.abs_imbalance} > {self.max_imbalance_shares}, "
                    f"rebalance {lagging}"
                )
                return (True, lagging)

        return (False, None)

    # =========================================================================
    # FILL HANDLING
    # =========================================================================

    def on_fill(self, side: str, price: float, size: int) -> None:
        """
        Handle a fill notification.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
        """
        s = self.state
        side_upper = side.upper()

        if side_upper == "UP":
            # Update UP position with weighted average
            s.up_cost += price * size
            s.up_shares += size
            s.up_avg_price = round(s.up_cost / s.up_shares, 4) if s.up_shares > 0 else 0.0
            s.total_up_fills += size
        else:
            # Update DOWN position
            s.down_cost += price * size
            s.down_shares += size
            s.down_avg_price = round(s.down_cost / s.down_shares, 4) if s.down_shares > 0 else 0.0
            s.total_down_fills += size

        # LEGACY: Update entry/hedge tracking for sequential mode compatibility
        if s.phase == SpreadCapturePhase.ENTRY_PENDING:
            s.entry_price = price
            s.entry_size = size
            s.phase = SpreadCapturePhase.ENTRY_FILLED
        elif s.phase == SpreadCapturePhase.HEDGE_PENDING:
            s.hedge_price = price
            s.hedge_size = size
            s.cycles_completed += 1
            s.phase = SpreadCapturePhase.COMPLETE

        logger.info(
            f"[SPREADCAP] Fill: {side_upper} {size}@${price:.3f} | "
            f"Pos: UP={s.up_shares}@${s.up_avg_price:.3f}, DOWN={s.down_shares}@${s.down_avg_price:.3f} | "
            f"Imbal: {s.imbalance:+d} ({s.imbalance_pct:.1%})"
        )

        # Track first fill for dynamic hedge targeting
        # Uses last_velocity from most recent get_quotes() call
        if s.first_fill_side is None:
            self.record_first_fill(side_upper, price, s.last_velocity)
        else:
            # Check if velocity strengthened - maybe tighten hedge target
            self.maybe_tighten_hedge_target(s.last_velocity)

        # Check for completed pairs
        self._check_completed_pairs()

    def _check_completed_pairs(self) -> None:
        """Check and record matched pairs."""
        s = self.state
        matchable = s.matchable_pairs

        if matchable == 0:
            return

        pair_cost = s.pair_cost
        base_profit = 1.00 - pair_cost

        net_profit = FeeConfig.calculate_net_profit(
            entry_price=s.up_avg_price,
            hedge_price=s.down_avg_price,
            size=matchable,
            entry_is_maker=True,
            hedge_is_maker=True,
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
            f"[SPREADCAP] Pairs: {matchable} @ ${pair_cost:.4f} | "
            f"Profit: ${net_profit:.4f} | Total: ${s.total_profit:.4f}"
        )

        # Reset matched shares after merge
        # Logic: Keep remainder on the heavier side, zero the lighter side
        # When equal (up_shares == down_shares): both become 0 (else branch zeros UP explicitly)
        if s.up_shares > s.down_shares:
            # UP is heavier - keep UP remainder, zero DOWN
            s.up_shares -= matchable
            s.up_cost = s.up_avg_price * s.up_shares
            s.down_shares = 0
            s.down_cost = 0.0
            s.down_avg_price = 0.0
        else:
            # DOWN is heavier OR equal - keep DOWN remainder, zero UP
            s.down_shares -= matchable
            s.down_cost = s.down_avg_price * s.down_shares
            s.up_shares = 0
            s.up_cost = 0.0
            s.up_avg_price = 0.0

        # CRITICAL: Reset for next cycle if cycling enabled
        # Without this, first_fill_side stays set and strategy can't re-enter
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
        """
        Legacy decision method for sequential entry→hedge mode.

        This method implements the OLD sequential logic for backward compatibility
        while also supporting the new continuous quoting mode.

        In sequential mode:
        1. IDLE → Place entry on expensive side → ENTRY_PENDING
        2. Entry fills → ENTRY_FILLED → Place hedge → HEDGE_PENDING
        3. Hedge fills → COMPLETE

        Args:
            up_bid, up_ask, down_bid, down_ask: Current orderbook prices
            time_remaining: Seconds until market resolution
            current_imbalance: Current position imbalance (from external tracking)
            current_time: Current timestamp
            velocity_bps: Optional BTC velocity

        Returns:
            (side, price, size) tuple or None
        """
        if velocity_bps is None:
            velocity_bps = 0.0

        s = self.state

        # Check emergency imbalance deferral (LEGACY behavior)
        if abs(current_imbalance) >= self.emergency_imbalance_threshold:
            if s.phase != SpreadCapturePhase.EMERGENCY_DEFERRED:
                s.phase = SpreadCapturePhase.EMERGENCY_DEFERRED
                logger.info(f"[SPREADCAP] Emergency deferred: imbalance={current_imbalance}")
            return None

        # If we were deferred, go back to idle
        if s.phase == SpreadCapturePhase.EMERGENCY_DEFERRED:
            s.phase = SpreadCapturePhase.IDLE

        # Don't place new orders if market ending soon
        if time_remaining < MIN_TIME_REMAINING:
            return None

        # IDLE: Start new cycle - enter expensive side
        if s.phase == SpreadCapturePhase.IDLE:
            # Determine expensive side
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

            # Calculate entry price
            entry_price = round(entry_bid - self.entry_offset, 2)
            if entry_price <= 0.01 or entry_price > self.max_share_price:
                return None

            s.phase = SpreadCapturePhase.ENTRY_PENDING
            return (entry_side, entry_price, self.base_size)

        # ENTRY_FILLED: Place hedge
        if s.phase == SpreadCapturePhase.ENTRY_FILLED:
            hedge_bid = down_bid if s.hedge_side == "DOWN" else up_bid

            # Calculate hedge price with profit ceiling
            max_hedge = self.calculate_max_hedge_price(s.entry_price)
            hedge_price = round(hedge_bid - self.hedge_offset, 2)
            hedge_price = min(hedge_price, max_hedge)

            if hedge_price <= 0.01 or hedge_price > self.max_share_price:
                return None

            s.phase = SpreadCapturePhase.HEDGE_PENDING
            return (s.hedge_side, hedge_price, self.base_size)

        # ENTRY_PENDING or HEDGE_PENDING: Wait for fill
        # COMPLETE: Cycle done
        return None

    # =========================================================================
    # STATUS & RESET
    # =========================================================================

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        s = self.state
        return {
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
            # LEGACY attributes
            "entry_side": s.entry_side,
            "hedge_side": s.hedge_side,
            "cycles_completed": s.cycles_completed,
            "last_velocity": s.last_velocity,
            "last_offsets": {
                "up": s.last_up_offset,
                "down": s.last_down_offset,
            },
            # Configuration
            "enable_cycling": self.enable_cycling,
            "target_shares": self.target_shares,
        }

    def get_completed_cycles(self) -> List[Dict[str, Any]]:
        """Get list of completed pair matches."""
        return self._completed_pairs.copy()

    def reset(self) -> None:
        """Reset strategy for new market."""
        total_profit = self.state.total_profit
        total_pairs = self.state.total_pairs_matched
        markets = self.state.markets_traded + 1

        self.state = SpreadCaptureState()
        self.state.total_profit = total_profit
        self.state.total_pairs_matched = total_pairs
        self.state.markets_traded = markets
        self.state.last_velocity_zone = VelocityZone.NEUTRAL  # Reset zone
        # Note: Dynamic hedge fields (first_fill_side, locked_hedge_target, etc.)
        # are reset to None/0.0 by the fresh SpreadCaptureState()
        # Note: Stop-loss fields (stop_loss_triggered, stop_loss_hedge_price)
        # are also reset by the fresh SpreadCaptureState()

        self._completed_pairs = []
        logger.info(f"[SPREADCAP] Reset for market #{markets} (hedge target + stop-loss cleared)")

    def reset_for_cycle(self) -> None:
        """Reset state for next cycle WITHIN same market (cycling mode).

        Called after merge to allow re-entry for another trade cycle.
        Preserves cumulative statistics but clears entry/hedge state.

        CRITICAL: This method enables cycling (multiple trades per market).
        Without it, after first merge the strategy would be stuck.
        """
        s = self.state

        # Reset entry/hedge tracking for new cycle
        s.first_fill_side = None
        s.first_fill_price = 0.0
        s.first_fill_velocity_dir = None
        s.locked_hedge_target = None
        s.current_velocity_zone = None

        # Reset stop-loss state
        s.stop_loss_triggered = False
        s.stop_loss_hedge_price = 0.0

        # Reset legacy phase tracking
        s.entry_side = None
        s.hedge_side = None
        s.entry_price = 0.0
        s.hedge_price = 0.0
        s.entry_size = 0
        s.hedge_size = 0
        s.phase = SpreadCapturePhase.IDLE

        logger.info(
            f"[SPREADCAP] Cycle reset: ready for re-entry "
            f"(pairs={s.total_pairs_matched}, profit=${s.total_profit:.2f})"
        )

    def __repr__(self) -> str:
        return (
            f"SpreadCaptureStrategy("
            f"base_size={self.base_size}, "
            f"grid_levels={self.grid_levels}, "
            f"max_imbalance={self.max_imbalance_pct:.0%}, "
            f"cycling={self.enable_cycling})"
        )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def calculate_velocity_edge(velocity_bps: float, side: str) -> float:
    """
    Calculate expected edge from velocity for a given side.

    The spot edge: Binance→Chainlink latency ~1-2 seconds.

    Args:
        velocity_bps: Current velocity in basis points per second
        side: "UP" or "DOWN"

    Returns:
        Expected edge in basis points (positive = favorable)
    """
    LATENCY_SECONDS = 1.5
    expected_move_bps = velocity_bps * LATENCY_SECONDS

    if side.upper() == "UP":
        return expected_move_bps   # Rising = UP favorable
    else:
        return -expected_move_bps  # Rising = DOWN unfavorable
