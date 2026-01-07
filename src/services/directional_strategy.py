"""
Directional Trading Strategy for Polymarket BTC Up/Down markets.

Implements:
1. Bias-based trading (BULLISH → buy UP, BEARISH → buy DOWN)
2. Time-decay adjusted flip detection using Binance price feed
3. Priority hedging to achieve pair cost < $0.95
4. Emergency hedging when <5 mins remaining
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.api.binance_client import BinanceClient

logger = logging.getLogger(__name__)


class Bias(Enum):
    """Trading bias direction."""
    BULLISH = "BULLISH"  # Expects price UP → buy UP shares
    BEARISH = "BEARISH"  # Expects price DOWN → buy DOWN shares


class DirectionalPhase(Enum):
    """Current trading phase."""
    ACCUMULATE = "accumulate"        # Buy bias side at attractive prices
    REBALANCE = "rebalance"          # Priority: buy opposite side to hedge
    AVERAGE_DOWN = "average_down"    # After balanced, improve pair cost on bias side
    EMERGENCY_HEDGE = "emergency"    # <5 mins left, immediate full hedge
    BALANCED = "balanced"            # Fully hedged, wait for resolution


class HedgeMode(Enum):
    """Hedge execution mode."""
    GRADUAL = "gradual"      # Normal: small increments over time
    EMERGENCY = "emergency"  # <5 mins: immediate full hedge


@dataclass
class DirectionalConfig:
    """Configuration for directional trading mode."""

    # Flip detection
    sigma_threshold: float = 2.0          # 2 sigma for impulsive move detection
    sustained_seconds: float = 45.0       # Seconds to confirm flip (increased from 30 for more confirmation)
    window_seconds: int = 60              # Rolling window for stats
    flip_cooldown_seconds: float = 180.0  # Min time between flips (increased from 120s)
    time_conviction_threshold: float = 10.0  # Time conviction needed to flip (increased from 8.0)
    max_flips_per_market: int = 2         # Hard cap on flips per market (reduced from 5)
    min_flip_time_remaining_secs: int = 420  # Don't flip with <7 minutes remaining (new parameter)

    # Position sizing (% of starting balance)
    max_position_pct: float = 0.17        # 17% of balance per side (final safety limit)
    target_shares: int = 15               # Target shares per side (first line of defense)
    # Example: $100 balance → max 15 UP + 15 DOWN
    trade_size_pct: float = 0.3333        # Each trade = 33.33% of max shares (e.g., 5 of 15)

    # Trading - Time-decayed attractive price
    # Early in market: more selective (lower threshold)
    # Late in market: more aggressive (higher threshold)
    attractive_price_early: float = 0.75  # Max price early in market (>10 mins left)
    attractive_price_late: float = 0.90   # Max price late in market (<2 mins left)
    # Formula: threshold = early + (late - early) * (1 - time_factor)

    dip_threshold_pct: float = 0.10       # 10% dip to average down
    trade_size: int = 5                   # Shares per trade
    hedge_increment: int = 5              # Shares per hedge cycle
    max_share_price: float = 0.95         # Never buy above this

    # Hedging
    pair_cost_target: float = 0.95        # Target pair cost
    emergency_threshold_secs: int = 300   # 5 minutes
    emergency_max_price: float = 0.65     # Max price for emergency hedges (stricter than normal)


@dataclass
class FlipSignal:
    """Signal indicating bias should flip."""
    should_flip: bool
    new_bias: Bias
    reason: str
    confidence: float  # 0.0 to 1.0
    price_vs_strike_pct: float
    z_score: float
    time_conviction: float
    sustained_seconds: float


@dataclass
class TradeDecision:
    """Decision on what trade to execute."""
    side: str  # "UP" or "DOWN"
    price: float
    size: int
    reason: str
    phase: DirectionalPhase
    is_hedge: bool = False


@dataclass
class DirectionalState:
    """Complete state for directional trading."""
    bias: Bias
    phase: DirectionalPhase
    flip_count: int = 0
    last_flip_time: Optional[datetime] = None
    last_flip_reason: Optional[str] = None

    # Position tracking (managed externally, updated here for phase logic)
    up_shares: int = 0
    down_shares: int = 0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0

    # Price references
    strike_price: float = 0.0
    last_buy_price: float = 0.0
    last_buy_side: Optional[str] = None

    # Flip detection state
    cross_timestamp: Optional[datetime] = None


class FlipDetector:
    """
    Detects when bias should flip based on price action and time decay.

    Uses Binance price feed to detect sustained moves against current bias,
    with time-adjusted thresholds (easier to flip late in market).
    """

    def __init__(
        self,
        binance_client: "BinanceClient",
        config: DirectionalConfig,
    ):
        self.client = binance_client
        self.config = config
        self._cross_timestamp: Optional[datetime] = None
        self._last_flip_time: Optional[datetime] = None

    def check_for_flip(
        self,
        current_bias: Bias,
        time_remaining_secs: int,
        current_flip_count: int = 0,
    ) -> FlipSignal:
        """
        Check if current price action warrants a bias flip.

        Algorithm (with time decay):
        1. Calculate price change vs strike as percentage
        2. Get std dev of recent price changes
        3. Calculate z-score (how many std devs from mean)
        4. Apply time decay: lower threshold late in market
        5. Check if move has been sustained for required duration

        Args:
            current_bias: Current trading bias
            time_remaining_secs: Seconds until market resolution
            current_flip_count: Number of flips already made this market

        Returns:
            FlipSignal with recommendation
        """
        now = datetime.now(timezone.utc)

        # Check max flips per market
        if self.config.max_flips_per_market > 0 and current_flip_count >= self.config.max_flips_per_market:
            return FlipSignal(
                should_flip=False,
                new_bias=current_bias,
                reason=f"Max flips reached ({current_flip_count}/{self.config.max_flips_per_market})",
                confidence=0.0,
                price_vs_strike_pct=self.client.price_vs_strike_pct,
                z_score=0.0,
                time_conviction=0.0,
                sustained_seconds=0.0,
            )

        # Check time-based flip lockout (don't flip too close to resolution)
        if time_remaining_secs < self.config.min_flip_time_remaining_secs:
            return FlipSignal(
                should_flip=False,
                new_bias=current_bias,
                reason=f"Too late to flip ({time_remaining_secs}s < {self.config.min_flip_time_remaining_secs}s)",
                confidence=0.0,
                price_vs_strike_pct=self.client.price_vs_strike_pct,
                z_score=0.0,
                time_conviction=0.0,
                sustained_seconds=0.0,
            )

        # Check cooldown
        if self._last_flip_time:
            elapsed = (now - self._last_flip_time).total_seconds()
            if elapsed < self.config.flip_cooldown_seconds:
                return FlipSignal(
                    should_flip=False,
                    new_bias=current_bias,
                    reason=f"Cooldown ({elapsed:.0f}s / {self.config.flip_cooldown_seconds:.0f}s)",
                    confidence=0.0,
                    price_vs_strike_pct=self.client.price_vs_strike_pct,
                    z_score=0.0,
                    time_conviction=0.0,
                    sustained_seconds=0.0,
                )

        # Get price statistics
        price_vs_strike_pct = self.client.price_vs_strike_pct
        z_score = self.client.calculate_z_score(self.config.window_seconds)

        # Time decay: adjust threshold based on time remaining
        # 15 min market = 900 seconds
        time_factor = min(1.0, time_remaining_secs / 900)

        # Use configured sigma_threshold as BASE, with slight time decay
        # Early market: full sigma_threshold (e.g., 2.5σ)
        # Late market: sigma_threshold - 0.5 (e.g., 2.0σ)
        # This is much less aggressive than before (was 1.5 to 2.5)
        adjusted_sigma = self.config.sigma_threshold - (0.5 * (1 - time_factor))

        # Time conviction: how much does time + price position favor flip?
        # Higher conviction late in market if price is against us
        # Made less aggressive by increasing the threshold needed
        time_conviction = (1 - time_factor) * abs(price_vs_strike_pct) * 10

        # Determine if price is signaling opposite to current bias
        is_bearish_signal = price_vs_strike_pct < -0.05  # Below strike by 0.05%
        is_bullish_signal = price_vs_strike_pct > 0.05   # Above strike by 0.05%

        # Check if this is an opposing signal
        opposing_signal = (
            (current_bias == Bias.BULLISH and is_bearish_signal) or
            (current_bias == Bias.BEARISH and is_bullish_signal)
        )

        if not opposing_signal:
            self._cross_timestamp = None  # Reset sustained timer
            return FlipSignal(
                should_flip=False,
                new_bias=current_bias,
                reason="Price aligned with bias",
                confidence=0.0,
                price_vs_strike_pct=price_vs_strike_pct,
                z_score=z_score,
                time_conviction=time_conviction,
                sustained_seconds=0.0,
            )

        # Track sustained duration
        if self._cross_timestamp is None:
            self._cross_timestamp = now

        sustained_seconds = (now - self._cross_timestamp).total_seconds()

        # Check all flip conditions
        momentum_trigger = z_score >= adjusted_sigma
        time_trigger = time_conviction > self.config.time_conviction_threshold
        sustained_trigger = sustained_seconds >= self.config.sustained_seconds

        # Flip if: (momentum OR time conviction) AND sustained
        should_flip = (momentum_trigger or time_trigger) and sustained_trigger

        # Calculate confidence
        confidence = min(1.0, (
            (z_score / adjusted_sigma) * 0.3 +
            (sustained_seconds / self.config.sustained_seconds) * 0.3 +
            (time_conviction / 10.0) * 0.4
        ))

        new_bias = Bias.BEARISH if is_bearish_signal else Bias.BULLISH

        if should_flip:
            self._last_flip_time = now
            self._cross_timestamp = None
            reason = (
                f"FLIP: z={z_score:.1f}σ (threshold={adjusted_sigma:.1f}), "
                f"sustained={sustained_seconds:.0f}s, "
                f"time_conv={time_conviction:.1f}, "
                f"price={price_vs_strike_pct:+.3f}%"
            )
        else:
            missing = []
            if not momentum_trigger and not time_trigger:
                missing.append(f"signal (z={z_score:.1f}<{adjusted_sigma:.1f}σ, tc={time_conviction:.1f}<{self.config.time_conviction_threshold:.0f})")
            if not sustained_trigger:
                missing.append(f"sustain ({sustained_seconds:.0f}s<{self.config.sustained_seconds:.0f}s)")
            reason = f"Waiting: {', '.join(missing)}"

        return FlipSignal(
            should_flip=should_flip,
            new_bias=new_bias,
            reason=reason,
            confidence=confidence,
            price_vs_strike_pct=price_vs_strike_pct,
            z_score=z_score,
            time_conviction=time_conviction,
            sustained_seconds=sustained_seconds,
        )

    def reset(self) -> None:
        """Reset flip detection state (e.g., on new market)."""
        self._cross_timestamp = None
        # Don't reset _last_flip_time - that's for cooldown across markets


class DirectionalTradingStrategy:
    """
    Implements directional trading logic with phases.

    Phases:
    1. ACCUMULATE: Buy bias side at attractive prices
    2. REBALANCE: Priority hedge to achieve pair cost < $0.95
    3. AVERAGE_DOWN: After balanced, buy more bias side at dips
    4. EMERGENCY_HEDGE: <5 mins left, immediate full hedge
    5. BALANCED: Fully hedged, wait for resolution
    """

    def __init__(
        self,
        binance_client: "BinanceClient",
        initial_bias: Bias,
        config: DirectionalConfig,
        starting_balance: float = 100.0,
    ):
        self.binance = binance_client
        self.config = config
        self.flip_detector = FlipDetector(binance_client, config)

        # Calculate max shares per side
        self.max_shares_per_side = int(starting_balance * config.max_position_pct)

        # Calculate trade size from percentage of max shares
        # trade_size_pct=0.3333 means each trade is 33.33% of max (e.g., 5 of 15)
        calculated_trade_size = max(1, int(self.max_shares_per_side * config.trade_size_pct))
        # Use calculated size, fallback to config.trade_size if trade_size_pct not set
        self.trade_size = calculated_trade_size if hasattr(config, 'trade_size_pct') else config.trade_size

        self.state = DirectionalState(
            bias=initial_bias,
            phase=DirectionalPhase.ACCUMULATE,
        )

        # Track time remaining for time-decayed calculations
        self._time_remaining_secs: int = 900  # Default 15 mins

        logger.info(
            f"DirectionalStrategy initialized: bias={initial_bias.value}, "
            f"max_shares={self.max_shares_per_side}/side, trade_size={self.trade_size}, "
            f"attractive_price={config.attractive_price_early:.2f}-{config.attractive_price_late:.2f}"
        )

    def update_position(
        self,
        up_shares: int,
        down_shares: int,
        up_avg_price: float,
        down_avg_price: float,
    ) -> None:
        """Update position tracking from external source."""
        self.state.up_shares = up_shares
        self.state.down_shares = down_shares
        self.state.up_avg_price = up_avg_price
        self.state.down_avg_price = down_avg_price

    def record_trade(self, side: str, price: float) -> None:
        """Record a trade for average-down logic."""
        self.state.last_buy_price = price
        self.state.last_buy_side = side

    def _get_attractive_price(self) -> float:
        """
        Calculate time-decayed attractive price threshold.

        Early in market (>10 mins): lower threshold (more selective)
        Late in market (<2 mins): higher threshold (more aggressive)

        Formula:
            time_factor = time_remaining / 900 (0.0 to 1.0)
            attractive = early + (late - early) * (1 - time_factor)

        Examples for 15-min market with early=$0.55, late=$0.90:
            15 mins left: $0.55 (most selective)
            10 mins left: $0.62
            5 mins left:  $0.73
            2 mins left:  $0.85
            0 mins left:  $0.90 (least selective)
        """
        # time_factor: 1.0 = full time remaining, 0.0 = no time left
        time_factor = min(1.0, max(0.0, self._time_remaining_secs / 900))

        early = self.config.attractive_price_early
        late = self.config.attractive_price_late

        # Interpolate: as time_factor decreases, price threshold increases
        attractive = early + (late - early) * (1 - time_factor)

        return attractive

    def get_volume_weighted_size(self, price: float, base_size: int) -> int:
        """
        Volume-weighted trade size for bias accumulation.

        Gabagool-style: buy MORE shares when cheap, FEWER when expensive.
        This maximizes position at lower average cost.

        Price tiers:
            < $0.35: 3x base size (load up on cheap bias!)
            $0.35-0.50: 2x base size (still good value)
            $0.50+: 1x base size (fair value or hedging)

        Args:
            price: Current ask price for the side
            base_size: Base trade size from config

        Returns:
            Adjusted trade size (capped by max_shares_per_side)
        """
        if price < 0.35:
            multiplier = 3
        elif price < 0.50:
            multiplier = 2
        else:
            multiplier = 1

        return base_size * multiplier

    def reset_for_new_market(self) -> None:
        """Reset state for a new market cycle."""
        self.state.phase = DirectionalPhase.ACCUMULATE
        self.state.up_shares = 0
        self.state.down_shares = 0
        self.state.up_avg_price = 0.0
        self.state.down_avg_price = 0.0
        self.state.last_buy_price = 0.0
        self.state.last_buy_side = None
        self.state.cross_timestamp = None
        self.flip_detector.reset()

        # Read strike price from Binance (already set by caller from previous candle close)
        self.state.strike_price = self.binance.strike_price
        logger.info(f"New market: Strike=${self.state.strike_price:,.2f}, Bias={self.state.bias.value}")

    def _calculate_pair_cost(self) -> float:
        """Calculate current pair cost."""
        if self.state.up_shares == 0 or self.state.down_shares == 0:
            return 0.0
        return self.state.up_avg_price + self.state.down_avg_price

    def _calculate_imbalance(self) -> int:
        """Calculate share imbalance (positive = more UP)."""
        return self.state.up_shares - self.state.down_shares

    def _get_hedge_side(self) -> str:
        """Get the side that needs hedging."""
        if self.state.up_shares > self.state.down_shares:
            return "DOWN"
        return "UP"

    def _get_bias_side(self) -> str:
        """Get the side matching current bias."""
        return "UP" if self.state.bias == Bias.BULLISH else "DOWN"

    def _determine_phase(self, time_remaining_secs: int) -> DirectionalPhase:
        """
        Determine current trading phase based on position and time.

        Priority:
        1. Emergency hedge if <5 mins and unbalanced
        2. Rebalance if imbalance exists
        3. Balanced if fully hedged
        4. Average down if balanced but can improve
        5. Accumulate otherwise
        """
        imbalance = abs(self._calculate_imbalance())
        is_balanced = imbalance <= 2  # Allow small imbalance

        # Emergency hedge check
        if time_remaining_secs < self.config.emergency_threshold_secs and imbalance > 0:
            return DirectionalPhase.EMERGENCY_HEDGE

        # Rebalance if imbalanced
        if imbalance > 5:  # More than 5 share difference
            return DirectionalPhase.REBALANCE

        # Check if fully hedged
        if is_balanced:
            # Can we average down on bias side?
            bias_side = self._get_bias_side()
            if bias_side == "UP" and self.state.up_shares < self.max_shares_per_side:
                return DirectionalPhase.AVERAGE_DOWN
            elif bias_side == "DOWN" and self.state.down_shares < self.max_shares_per_side:
                return DirectionalPhase.AVERAGE_DOWN
            return DirectionalPhase.BALANCED

        # Default to accumulate
        return DirectionalPhase.ACCUMULATE

    def evaluate_trade(
        self,
        up_ask: float,
        down_ask: float,
        time_remaining_secs: int,
    ) -> Optional[TradeDecision]:
        """
        Evaluate what trade to make given current market prices.

        Args:
            up_ask: Current UP contract ask price
            down_ask: Current DOWN contract ask price
            time_remaining_secs: Seconds until market resolution

        Returns:
            TradeDecision or None if no trade should be made
        """
        # Store time remaining for time-decay calculations
        self._time_remaining_secs = time_remaining_secs

        # 1. Check for flip signal
        flip_signal = self.flip_detector.check_for_flip(
            self.state.bias,
            time_remaining_secs,
            self.state.flip_count,
        )

        if flip_signal.should_flip:
            old_bias = self.state.bias
            self.state.bias = flip_signal.new_bias
            self.state.flip_count += 1
            self.state.last_flip_time = datetime.now(timezone.utc)
            self.state.last_flip_reason = flip_signal.reason

            logger.warning(
                f"BIAS FLIP: {old_bias.value} → {flip_signal.new_bias.value} | "
                f"{flip_signal.reason}"
            )

        # 2. Determine current phase
        self.state.phase = self._determine_phase(time_remaining_secs)

        # 3. Execute trade based on phase
        if self.state.phase == DirectionalPhase.ACCUMULATE:
            return self._evaluate_accumulate(up_ask, down_ask)

        elif self.state.phase == DirectionalPhase.REBALANCE:
            return self._evaluate_rebalance(up_ask, down_ask)

        elif self.state.phase == DirectionalPhase.AVERAGE_DOWN:
            return self._evaluate_average_down(up_ask, down_ask)

        elif self.state.phase == DirectionalPhase.EMERGENCY_HEDGE:
            return self._evaluate_emergency_hedge(up_ask, down_ask)

        # BALANCED - no trade
        return None

    def _evaluate_accumulate(
        self,
        up_ask: float,
        down_ask: float,
    ) -> Optional[TradeDecision]:
        """Phase 1: Buy bias side at attractive prices with volume weighting."""
        bias_side = self._get_bias_side()
        price = up_ask if bias_side == "UP" else down_ask
        current_shares = self.state.up_shares if bias_side == "UP" else self.state.down_shares

        # Check max position limit
        if current_shares >= self.max_shares_per_side:
            return None

        # Check max share price (hard cap)
        if price >= self.config.max_share_price:
            return None

        # Check time-decayed attractive price
        attractive = self._get_attractive_price()
        if price > attractive:
            return None

        # Volume-weighted size: buy more when cheap, less when expensive
        weighted_size = self.get_volume_weighted_size(price, self.trade_size)
        trade_size = min(weighted_size, self.max_shares_per_side - current_shares)

        return TradeDecision(
            side=bias_side,
            price=price,
            size=trade_size,
            reason=f"Accumulate {bias_side} ({self.state.bias.value} bias) [thresh=${attractive:.2f}, size={trade_size}]",
            phase=DirectionalPhase.ACCUMULATE,
            is_hedge=False,
        )

    def _evaluate_rebalance(
        self,
        up_ask: float,
        down_ask: float,
    ) -> Optional[TradeDecision]:
        """Phase 2: Priority hedge to balance position."""
        hedge_side = self._get_hedge_side()
        price = up_ask if hedge_side == "UP" else down_ask
        imbalance = abs(self._calculate_imbalance())

        # Check max share price (even for hedge)
        if price >= self.config.max_share_price:
            logger.info(f"REBALANCE BLOCKED: {hedge_side} ask ${price:.2f} >= ${self.config.max_share_price:.2f}")
            return None

        # Calculate prospective pair cost
        current_pair_cost = self._calculate_pair_cost()
        if current_pair_cost > 0:
            # Would adding this hedge exceed pair cost target?
            # This is approximate - real calculation happens in paper_trading.py
            if price > self.config.pair_cost_target:
                logger.debug(f"REBALANCE: {hedge_side} price ${price:.2f} > target ${self.config.pair_cost_target:.2f}")

        return TradeDecision(
            side=hedge_side,
            price=price,
            size=min(self.config.hedge_increment, imbalance),
            reason=f"Rebalance: buy {hedge_side} (imbal={imbalance})",
            phase=DirectionalPhase.REBALANCE,
            is_hedge=True,
        )

    def _evaluate_average_down(
        self,
        up_ask: float,
        down_ask: float,
    ) -> Optional[TradeDecision]:
        """Phase 3: Average down on bias side after balanced (with volume weighting)."""
        bias_side = self._get_bias_side()
        price = up_ask if bias_side == "UP" else down_ask
        current_shares = self.state.up_shares if bias_side == "UP" else self.state.down_shares

        # Check max position limit
        if current_shares >= self.max_shares_per_side:
            return None

        # Check max share price
        if price >= self.config.max_share_price:
            return None

        # Only average down if price is lower than last buy
        if self.state.last_buy_side == bias_side and self.state.last_buy_price > 0:
            threshold = self.state.last_buy_price * (1 - self.config.dip_threshold_pct)
            if price >= threshold:
                return None  # Not enough of a dip

        # Check time-decayed attractive price
        attractive = self._get_attractive_price()
        if price > attractive:
            return None

        # Volume-weighted size: buy more when cheap (dips are great opportunities!)
        weighted_size = self.get_volume_weighted_size(price, self.trade_size)
        trade_size = min(weighted_size, self.max_shares_per_side - current_shares)

        return TradeDecision(
            side=bias_side,
            price=price,
            size=trade_size,
            reason=f"Avg down {bias_side} (was ${self.state.last_buy_price:.2f}) [thresh=${attractive:.2f}, size={trade_size}]",
            phase=DirectionalPhase.AVERAGE_DOWN,
            is_hedge=False,
        )

    def _evaluate_emergency_hedge(
        self,
        up_ask: float,
        down_ask: float,
    ) -> Optional[TradeDecision]:
        """Phase 4: Emergency hedge with <5 mins remaining."""
        hedge_side = self._get_hedge_side()
        price = up_ask if hedge_side == "UP" else down_ask
        imbalance = abs(self._calculate_imbalance())

        if imbalance == 0:
            return None

        # CRITICAL: Only execute at price < emergency_max_price (stricter than normal)
        if price >= self.config.emergency_max_price:
            logger.warning(
                f"EMERGENCY HEDGE BLOCKED: {hedge_side} ask ${price:.2f} >= "
                f"${self.config.emergency_max_price:.2f} - accepting unhedged position"
            )
            return None

        # Buy aggressively - larger chunks
        chunk_size = min(10, imbalance)

        return TradeDecision(
            side=hedge_side,
            price=price,
            size=chunk_size,
            reason=f"EMERGENCY: {hedge_side} ({imbalance} to hedge, <5min)",
            phase=DirectionalPhase.EMERGENCY_HEDGE,
            is_hedge=True,
        )

    def get_status_dict(self) -> dict:
        """Get current status as dictionary for logging."""
        return {
            "bias": self.state.bias.value,
            "phase": self.state.phase.value,
            "flip_count": self.state.flip_count,
            "up_shares": self.state.up_shares,
            "down_shares": self.state.down_shares,
            "imbalance": self._calculate_imbalance(),
            "pair_cost": self._calculate_pair_cost(),
            "strike_price": self.state.strike_price,
            "btc_price": self.binance.current_price,
            "price_vs_strike_pct": self.binance.price_vs_strike_pct,
            "z_score": self.binance.calculate_z_score(self.config.window_seconds),
        }
