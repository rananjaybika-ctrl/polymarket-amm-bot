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
    strategy = SpreadCaptureStrategy(base_size=10, grid_levels=3)
    quotes = strategy.get_quotes(up_bid=0.55, up_ask=0.56, ...)

Usage (LEGACY - still works):
    strategy = SpreadCaptureStrategy(entry_size=5, target_shares=15)
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

# Velocity thresholds (basis points per second)
VELOCITY_THRESHOLD = 0.05       # Threshold for quote adjustment
VELOCITY_STRONG = 0.10          # Strong movement - more aggressive adjustment
VELOCITY_PULL_THRESHOLD = 0.05  # LEGACY: For backward compatibility with tests

# Quote offsets (from best_bid)
# Formula: our_bid = best_bid - offset
# Negative offset = bid ABOVE best_bid (very aggressive)
# Positive offset = bid BELOW best_bid (conservative)
BASE_OFFSET = 0.02              # Neutral offset: best_bid - 0.02
TIGHT_OFFSET = -0.01            # AGGRESSIVE: bid ABOVE best_bid for winner (best_bid + 0.01)
WIDE_OFFSET = 0.02              # Conservative offset when avoiding overpriced side
VERY_WIDE_OFFSET = 0.04         # CONSERVATIVE: bid far below for loser (best_bid - 0.04)

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
DEFAULT_MAX_IMBALANCE_SHARES = 50  # Absolute cap on imbalance
FORCE_REBALANCE_OFFSET = 0.005    # Tighter offset when force-buying lagging side

# Polymarket constraints
MIN_SHARES = 5
DEFAULT_BASE_SIZE = 10
DEFAULT_TARGET_SHARES = 100     # Total target per market
DEFAULT_MIN_PROFIT = 0.005
DEFAULT_MAX_SHARE_PRICE = 0.95
DEFAULT_ENABLE_CYCLING = False  # If False, stop at target; if True, keep cycling

# Timing
MIN_TIME_REMAINING = 60         # Don't place new orders with <60s left
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
    """Velocity zones for auto order pulling.

    When velocity crosses zone boundaries, orders are pulled and regenerated
    with optimal offsets for the new zone.
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
    last_velocity_zone: "VelocityZone" = None  # Initialized in post_init

    # Statistics
    total_up_fills: int = 0
    total_down_fills: int = 0
    total_pairs_matched: int = 0
    total_profit: float = 0.0
    rebalance_count: int = 0

    # Tracking
    quotes_generated: int = 0
    markets_traded: int = 0

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

        # Auto-pull configuration (internal, not exposed to frontend)
        self.enable_auto_pull: bool = True   # Pull orders on velocity zone transitions
        self.pull_mode: str = "both"         # "both" or "adverse_only"

        # LEGACY attributes
        self.entry_offset = entry_offset
        self.hedge_offset = hedge_offset
        self.emergency_imbalance_threshold = max_imbalance_shares  # Alias

        self.state = SpreadCaptureState()
        self.state.last_velocity_zone = VelocityZone.NEUTRAL  # Initialize zone
        self._completed_pairs: List[Dict[str, Any]] = []

        logger.info(
            f"[SPREADCAP] Initialized: base_size={base_size}, grid_levels={grid_levels}, "
            f"max_imbalance={max_imbalance_pct:.0%}, target={target_shares}, cycling={enable_cycling}"
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
        """LEGACY: Check if entry should be pulled due to adverse velocity.

        UP entry: adverse if velocity < -VELOCITY_PULL_THRESHOLD
        DOWN entry: adverse if velocity > VELOCITY_PULL_THRESHOLD
        """
        if entry_side.upper() == "UP":
            return velocity_bps < -VELOCITY_PULL_THRESHOLD
        else:
            return velocity_bps > VELOCITY_PULL_THRESHOLD

    # =========================================================================
    # CORE: VELOCITY-BASED QUOTE ADJUSTMENT
    # =========================================================================

    def calculate_offsets(self, velocity_bps: float) -> Tuple[float, float]:
        """
        Calculate quote offsets based on velocity.

        THIS IS THE CRITICAL LOGIC - THE CORRECT WAY:

        When BTC RISING (velocity > 0):
            - UP will get more expensive (underpriced NOW) → TIGHTEN UP bid
            - DOWN will get cheaper (overpriced NOW) → WIDEN DOWN bid

        When BTC FALLING (velocity < 0):
            - UP will get cheaper (overpriced NOW) → WIDEN UP bid
            - DOWN will get more expensive (underpriced NOW) → TIGHTEN DOWN bid

        Args:
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            (up_offset, down_offset) - offsets from best_bid
        """
        abs_velocity = abs(velocity_bps)

        # Neutral zone - no directional bias
        if abs_velocity < VELOCITY_THRESHOLD:
            return (BASE_OFFSET, BASE_OFFSET)

        # Strong velocity - more aggressive adjustment
        if abs_velocity > VELOCITY_STRONG:
            if velocity_bps > 0:  # BTC rising strongly
                return (TIGHT_OFFSET, VERY_WIDE_OFFSET)
            else:  # BTC falling strongly
                return (VERY_WIDE_OFFSET, TIGHT_OFFSET)

        # Moderate velocity
        if velocity_bps > 0:  # BTC rising
            return (TIGHT_OFFSET, WIDE_OFFSET)
        else:  # BTC falling
            return (WIDE_OFFSET, TIGHT_OFFSET)

    def check_velocity_zone_transition(
        self,
        velocity_bps: float
    ) -> Tuple["VelocityZone", bool, List[str]]:
        """
        Check if velocity crossed a zone boundary.

        Used for auto-pulling orders when market conditions change significantly.
        When zone changes, stale orders at old offsets should be cancelled and
        regenerated with new optimal offsets.

        Args:
            velocity_bps: Current BTC velocity in basis points per second

        Returns:
            Tuple of (current_zone, zone_changed, sides_to_pull)

        Pull Modes:
            "both"         - Pull UP + DOWN on any zone transition (clean slate)
            "adverse_only" - Pull only the adverse side (legacy behavior)
        """
        abs_vel = abs(velocity_bps)

        # Determine current zone
        if abs_vel < VELOCITY_THRESHOLD:  # 0.05 bps
            new_zone = VelocityZone.NEUTRAL
        elif abs_vel < VELOCITY_STRONG:   # 0.10 bps
            new_zone = VelocityZone.MODERATE
        else:
            new_zone = VelocityZone.STRONG

        old_zone = self.state.last_velocity_zone
        zone_changed = (new_zone != old_zone)

        sides_to_pull: List[str] = []
        if zone_changed and self.enable_auto_pull:
            if self.pull_mode == "both":
                # Pull ALL orders for clean slate repricing
                sides_to_pull = ["UP", "DOWN"]
            elif self.pull_mode == "adverse_only":
                # Legacy: only pull the side getting overpriced
                if new_zone != VelocityZone.NEUTRAL:
                    if velocity_bps > 0:  # Rising - DOWN is adverse
                        sides_to_pull = ["DOWN"]
                    else:  # Falling - UP is adverse
                        sides_to_pull = ["UP"]

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

        # Calculate velocity-adjusted offsets
        up_offset, down_offset = self.calculate_offsets(velocity_bps)
        s.last_up_offset = up_offset
        s.last_down_offset = down_offset

        # Check if rebalancing needed
        needs_rebalance, rebalance_side = self._check_rebalance_needed()
        if needs_rebalance:
            s.phase = SpreadCapturePhase.REBALANCING

        quotes = []

        # Generate UP quotes
        up_quotes = self._generate_side_quotes(
            side="UP",
            best_bid=up_bid,
            offset=up_offset,
            current_shares=s.up_shares,
            needs_rebalance=(needs_rebalance and rebalance_side == "UP"),
        )
        quotes.extend(up_quotes)

        # Generate DOWN quotes
        down_quotes = self._generate_side_quotes(
            side="DOWN",
            best_bid=down_bid,
            offset=down_offset,
            current_shares=s.down_shares,
            needs_rebalance=(needs_rebalance and rebalance_side == "DOWN"),
        )
        quotes.extend(down_quotes)

        s.quotes_generated += len(quotes)

        if quotes:
            logger.debug(
                f"[SPREADCAP] Quotes: UP={len(up_quotes)} (off={up_offset:.3f}), "
                f"DOWN={len(down_quotes)} (off={down_offset:.3f}), vel={velocity_bps:.3f}bps"
            )

        return quotes

    def _generate_side_quotes(
        self,
        side: str,
        best_bid: float,
        offset: float,
        current_shares: int,
        needs_rebalance: bool,
    ) -> List[Dict[str, Any]]:
        """Generate quotes for one side (UP or DOWN)."""
        quotes = []

        # Check position limit
        if current_shares >= self.target_shares:
            if not needs_rebalance:
                return []

        # Size per level
        size_per_level = max(MIN_SHARES, self.base_size // self.grid_levels)

        # Rebalancing: tighter offset, full size
        if needs_rebalance:
            offset = FORCE_REBALANCE_OFFSET
            size_per_level = self.base_size
            self.state.rebalance_count += 1
            logger.info(f"[SPREADCAP] REBALANCE {side}: offset={offset:.3f}")

        # Generate grid levels
        for level in range(self.grid_levels):
            level_offset = offset + (level * GRID_SPACING)
            price = round(best_bid - level_offset, 2)

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

        # Reset matched shares
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

        self._completed_pairs = []
        logger.info(f"[SPREADCAP] Reset for market #{markets}")

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
