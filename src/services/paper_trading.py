"""
Paper Trading Engine for dry run simulation.

Simulates order execution without real API calls, allowing
strategy validation before risking real capital.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from src.services.trend_detector import TrendDetector

from src.models.market import BTCMarket
from src.models.position import Position, Fill
from src.services.pair_analyzer import PairOpportunity
from src.services.order_executor import (
    OrderInfo,
    PairExecutionResult,
    ExecutionStatus,
    OrderSide,
)


logger = logging.getLogger(__name__)


# Polymarket order constraints
MIN_ORDER_SHARES = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0     # Minimum $1 order value


class FillType(Enum):
    """Type of simulated fill."""
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


@dataclass
class SimulationConfig:
    """
    Configuration for paper trading simulation.

    Attributes:
        fill_probability: Base probability of order filling (0.0 - 1.0)
        partial_fill_rate: Probability of partial fill when filling (0.0 - 1.0)
        partial_fill_min: Minimum % filled on partial (0.0 - 1.0)
        partial_fill_max: Maximum % filled on partial (0.0 - 1.0)
        slippage_bps: Maximum slippage in basis points
        execution_delay_ms: Simulated execution delay
        price_improvement_chance: Chance of getting better price
        random_seed: Optional seed for reproducible simulations
        dynamic_fill_enabled: Enable dynamic fill probability based on price movement
        price_volatility: Expected price movement as fraction (e.g., 0.02 = 2%)
        competition_factor: Probability of being "sniped" by competing bots (0.0 - 1.0)
        min_fill_delay_ms: Minimum time before order can fill (simulates queue)
    """
    fill_probability: float = 0.90  # 90% fill rate
    partial_fill_rate: float = 0.10  # 10% partial fills
    partial_fill_min: float = 0.3   # Min 30% filled
    partial_fill_max: float = 0.9   # Max 90% filled
    slippage_bps: float = 5.0       # 5 bps max slippage
    execution_delay_ms: float = 100.0
    price_improvement_chance: float = 0.05  # 5% chance of price improvement
    random_seed: Optional[int] = None
    dynamic_fill_enabled: bool = True  # Enable dynamic fill probability
    price_volatility: float = 0.03  # 3% expected price swing during order lifetime
    competition_factor: float = 0.25  # 25% chance of being sniped by competition
    min_fill_delay_ms: float = 500.0  # 500ms minimum before fill (queue simulation)

    def __post_init__(self):
        if self.random_seed is not None:
            random.seed(self.random_seed)


@dataclass
class PaperTrade:
    """Record of a paper trade."""
    trade_id: str
    market_slug: str
    side: str  # "UP" or "DOWN"
    price: float
    size: float
    cost: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_pair: bool = False
    pair_trade_id: Optional[str] = None


# PaperPosition has been consolidated into the unified Position class
# in src/models/position.py. This eliminates ~300 lines of duplicate code
# and ensures paper and live trading use identical position calculations.


@dataclass
class SimulationStats:
    """Statistics from paper trading session."""
    total_trades: int = 0
    successful_pairs: int = 0
    partial_fills: int = 0
    failed_fills: int = 0
    total_cost: float = 0.0
    total_profit: float = 0.0
    realized_pnl: float = 0.0
    markets_traded: int = 0
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: Optional[datetime] = None

    @property
    def win_rate(self) -> float:
        """Percentage of successful pair trades."""
        total = self.successful_pairs + self.partial_fills + self.failed_fills
        if total == 0:
            return 0.0
        return self.successful_pairs / total

    @property
    def avg_profit_per_pair(self) -> float:
        """Average profit per successful pair."""
        if self.successful_pairs == 0:
            return 0.0
        return self.total_profit / self.successful_pairs

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_trades": self.total_trades,
            "successful_pairs": self.successful_pairs,
            "partial_fills": self.partial_fills,
            "failed_fills": self.failed_fills,
            "total_cost": self.total_cost,
            "total_profit": self.total_profit,
            "realized_pnl": self.realized_pnl,
            "markets_traded": self.markets_traded,
            "win_rate": f"{self.win_rate:.1%}",
            "avg_profit_per_pair": self.avg_profit_per_pair,
        }


class PaperTradingEngine:
    """
    Paper trading simulation engine.

    Simulates order execution with realistic conditions:
    - Configurable fill rates
    - Partial fills
    - Slippage
    - Queue position uncertainty

    Example:
        engine = PaperTradingEngine()

        # Execute paper trade
        result = await engine.execute_paper_trade(opportunity, size=10)

        if result.success:
            print(f"Paper trade executed: {result.actual_cost}")

        # Check P&L
        print(f"Total P&L: ${engine.get_total_pnl():.4f}")
    """

    def __init__(
        self,
        config: Optional[SimulationConfig] = None,
        initial_balance: float = 1000.0,
    ):
        """
        Initialize paper trading engine.

        Args:
            config: Simulation configuration
            initial_balance: Starting paper balance
        """
        self.config = config or SimulationConfig()
        self.initial_balance = initial_balance
        self._balance = initial_balance

        # State
        self._positions: Dict[str, Position] = {}
        self._trades: List[PaperTrade] = []
        self._stats = SimulationStats()
        self._trade_counter = 0

        # Pending orders for quote pulling simulation
        # Key: "{market_slug}_{side}" e.g. "btc-up-down-15min_UP"
        # Value: {"order_id": str, "price": float, "size": float, "placed_at": float,
        #         "market": BTCMarket, "side": str, "best_ask": float}
        self._pending_orders: Dict[str, Dict[str, Any]] = {}

    @property
    def balance(self) -> float:
        """Current paper balance."""
        return self._balance

    @property
    def positions(self) -> List[Position]:
        """All paper positions."""
        return list(self._positions.values())

    @property
    def stats(self) -> SimulationStats:
        """Simulation statistics."""
        return self._stats

    def _generate_trade_id(self) -> str:
        """Generate unique trade ID."""
        self._trade_counter += 1
        return f"paper_{self._trade_counter:06d}"

    def _simulate_fill(self) -> FillType:
        """
        Simulate whether an order fills in a competitive orderbook.

        Simulates:
        1. Competition factor - other bots may snipe the fill
        2. Base fill probability - not all orders fill
        3. Partial fills - sometimes only part of order fills

        Returns:
            FillType indicating fill result
        """
        # Competition check: other bots may snipe the fill first
        if random.random() < self.config.competition_factor:
            logger.debug("Order sniped by competition")
            return FillType.NONE

        # Base fill probability check
        roll = random.random()
        if roll > self.config.fill_probability:
            logger.debug(f"Fill probability miss: {roll:.2f} > {self.config.fill_probability}")
            return FillType.NONE

        # Check for partial fill
        if random.random() < self.config.partial_fill_rate:
            return FillType.PARTIAL

        return FillType.FULL

    def _calculate_dynamic_fill_probability(
        self,
        limit_price: float,
        best_ask: float,
        best_bid: Optional[float] = None,
    ) -> float:
        """
        Calculate fill probability considering price movement toward limit order.

        In real markets, a limit order below best ask has INCREASING fill probability
        as price moves toward it. This simulates that dynamic behavior.

        The model considers:
        1. Static distance penalty (original behavior)
        2. Probability that price will move toward our limit during order lifetime
        3. Combined probability of fill

        Args:
            limit_price: Our limit order price
            best_ask: Current best ask
            best_bid: Current best bid (optional, for spread calculation)

        Returns:
            Fill probability (0.0 to 1.0)
        """
        if limit_price >= best_ask:
            # At or above ask - should fill at base probability
            return self.config.fill_probability

        price_diff = best_ask - limit_price
        price_diff_pct = price_diff / best_ask if best_ask > 0 else 0

        # If dynamic fill is disabled, use base fill probability without distance penalty
        # This is useful for maker strategies where patient orders are expected to fill
        if not self.config.dynamic_fill_enabled:
            return self.config.fill_probability

        # STATIC COMPONENT: Original distance-based penalty
        # Linear decay: 100% at ask → 40% at 3+ cents below
        static_prob = max(0.40, 1.0 - (price_diff * 20))

        # DYNAMIC COMPONENT: Consider price movement toward our limit
        # How likely is price to reach our limit during order lifetime?
        volatility = self.config.price_volatility

        # Calculate how many "volatility units" away our limit is
        # volatility_units = distance / expected_movement
        volatility_units = price_diff_pct / volatility if volatility > 0 else float('inf')

        # Probability price touches our level (simplified normal distribution)
        # Within 0.5 sigma: ~95% chance
        # Within 1.0 sigma: ~68% chance
        # Within 1.5 sigma: ~50% chance
        # Within 2.0 sigma: ~32% chance
        # Beyond 2.0 sigma: ~15% chance
        if volatility_units <= 0.25:
            price_approach_prob = 0.98  # Almost certain
        elif volatility_units <= 0.5:
            price_approach_prob = 0.90  # Very likely
        elif volatility_units <= 1.0:
            price_approach_prob = 0.68  # Likely (1 sigma)
        elif volatility_units <= 1.5:
            price_approach_prob = 0.50  # Coin flip
        elif volatility_units <= 2.0:
            price_approach_prob = 0.32  # Less likely (2 sigma)
        elif volatility_units <= 3.0:
            price_approach_prob = 0.15  # Unlikely
        else:
            price_approach_prob = 0.05  # Very unlikely

        # When price approaches our limit, high chance of fill
        fill_when_approached = 0.92

        # Combined probability using inclusion-exclusion:
        # P(fill) = P(fills immediately) + P(doesn't fill immediately) * P(price approaches) * P(fills when approached)
        immediate_fill_prob = static_prob * self.config.fill_probability
        deferred_fill_prob = (1 - immediate_fill_prob) * price_approach_prob * fill_when_approached

        combined_prob = immediate_fill_prob + deferred_fill_prob

        logger.debug(
            f"Dynamic fill calc: limit=${limit_price:.4f}, ask=${best_ask:.4f}, "
            f"diff={price_diff:.4f} ({price_diff_pct:.2%}), "
            f"vol_units={volatility_units:.2f}, approach_prob={price_approach_prob:.0%}, "
            f"static={static_prob:.0%}, combined={combined_prob:.0%}"
        )

        return min(1.0, combined_prob)

    def _simulate_fill_size(self, requested_size: float, fill_type: FillType) -> float:
        """
        Simulate filled size.

        Args:
            requested_size: Requested order size
            fill_type: Type of fill

        Returns:
            Simulated filled size
        """
        if fill_type == FillType.NONE:
            return 0.0
        elif fill_type == FillType.FULL:
            return requested_size
        else:
            # Partial fill
            fill_pct = random.uniform(
                self.config.partial_fill_min,
                self.config.partial_fill_max,
            )
            return int(requested_size * fill_pct)

    def _simulate_price(self, base_price: float, is_buy: bool) -> float:
        """
        Simulate execution price for LIMIT orders.

        For limit orders, fills happen at the limit price or BETTER:
        - Buy limit: fills at limit or LOWER (price improvement only)
        - Sell limit: fills at limit or HIGHER (price improvement only)

        Negative slippage (worse price) is NOT possible for limit orders.

        Args:
            base_price: Limit order price
            is_buy: Whether this is a buy order

        Returns:
            Simulated execution price (always at limit or better)
        """
        # Limit orders fill at limit price or better - never worse
        # Check for price improvement (random chance to get better price)
        if random.random() < self.config.price_improvement_chance:
            improvement = random.uniform(0, self.config.slippage_bps) / 10000
            if is_buy:
                return base_price * (1 - improvement)  # Lower = better for buy
            else:
                return base_price * (1 + improvement)  # Higher = better for sell

        # No improvement - fill at exactly the limit price
        return base_price

    async def execute_paper_trade(
        self,
        opportunity: PairOpportunity,
        size: int,
    ) -> PairExecutionResult:
        """
        Execute a simulated pair trade.

        Args:
            opportunity: PairOpportunity to trade
            size: Number of pairs to trade

        Returns:
            PairExecutionResult with simulation results
        """
        market = opportunity.market
        pair_trade_id = self._generate_trade_id()

        # Enforce Polymarket order constraints: min 5 shares AND min $1 value per side
        up_value = size * opportunity.up_ask
        down_value = size * opportunity.down_ask
        if size < MIN_ORDER_SHARES:
            logger.warning(f"[PAPER] Pair trade rejected: {size} shares < min {MIN_ORDER_SHARES}")
            return PairExecutionResult(
                market=market,
                up_order=OrderInfo(token_id=market.up_token_id, side=OrderSide.BUY.value,
                                   price=opportunity.up_ask, size=size, order_id=pair_trade_id+"_up"),
                down_order=OrderInfo(token_id=market.down_token_id, side=OrderSide.BUY.value,
                                     price=opportunity.down_ask, size=size, order_id=pair_trade_id+"_down"),
                expected_cost=size * opportunity.pair_cost,
                actual_cost=0,
                success=False,
                error=f"Size {size} < min {MIN_ORDER_SHARES} shares",
            )
        if up_value < MIN_ORDER_VALUE or down_value < MIN_ORDER_VALUE:
            min_val = min(up_value, down_value)
            logger.warning(f"[PAPER] Pair trade rejected: ${min_val:.2f} value < min ${MIN_ORDER_VALUE:.2f}")
            return PairExecutionResult(
                market=market,
                up_order=OrderInfo(token_id=market.up_token_id, side=OrderSide.BUY.value,
                                   price=opportunity.up_ask, size=size, order_id=pair_trade_id+"_up"),
                down_order=OrderInfo(token_id=market.down_token_id, side=OrderSide.BUY.value,
                                     price=opportunity.down_ask, size=size, order_id=pair_trade_id+"_down"),
                expected_cost=size * opportunity.pair_cost,
                actual_cost=0,
                success=False,
                error=f"Value ${min_val:.2f} < min ${MIN_ORDER_VALUE:.2f}",
            )

        logger.info(f"Paper trade: {size} pairs on {market.slug}")

        # Simulate Up order
        up_fill_type = self._simulate_fill()
        up_filled_size = self._simulate_fill_size(size, up_fill_type)
        up_price = self._simulate_price(opportunity.up_ask, is_buy=True)

        up_order = OrderInfo(
            token_id=market.up_token_id,
            side=OrderSide.BUY.value,
            price=opportunity.up_ask,
            size=size,
            order_id=f"{pair_trade_id}_up",
            filled_size=up_filled_size,
            filled_price=up_price if up_filled_size > 0 else 0.0,
        )

        # Simulate Down order
        down_fill_type = self._simulate_fill()
        down_filled_size = self._simulate_fill_size(size, down_fill_type)
        down_price = self._simulate_price(opportunity.down_ask, is_buy=True)

        down_order = OrderInfo(
            token_id=market.down_token_id,
            side=OrderSide.BUY.value,
            price=opportunity.down_ask,
            size=size,
            order_id=f"{pair_trade_id}_down",
            filled_size=down_filled_size,
            filled_price=down_price if down_filled_size > 0 else 0.0,
        )

        # Determine fill status
        if up_filled_size == size:
            up_order.status = ExecutionStatus.FILLED
        elif up_filled_size > 0:
            up_order.status = ExecutionStatus.PARTIAL
        else:
            up_order.status = ExecutionStatus.CANCELLED

        if down_filled_size == size:
            down_order.status = ExecutionStatus.FILLED
        elif down_filled_size > 0:
            down_order.status = ExecutionStatus.PARTIAL
        else:
            down_order.status = ExecutionStatus.CANCELLED

        # Calculate costs
        up_cost = up_filled_size * up_price
        down_cost = down_filled_size * down_price
        actual_cost = up_cost + down_cost
        expected_cost = size * opportunity.pair_cost

        # Determine success
        both_filled = up_order.is_filled and down_order.is_filled
        any_filled = up_filled_size > 0 or down_filled_size > 0

        # Update balance
        if any_filled:
            self._balance -= actual_cost

        # Record trades
        if up_filled_size > 0:
            up_trade = PaperTrade(
                trade_id=f"{pair_trade_id}_up",
                market_slug=market.slug,
                side="UP",
                price=up_price,
                size=up_filled_size,
                cost=up_cost,
                is_pair=True,
                pair_trade_id=pair_trade_id,
            )
            self._trades.append(up_trade)

        if down_filled_size > 0:
            down_trade = PaperTrade(
                trade_id=f"{pair_trade_id}_down",
                market_slug=market.slug,
                side="DOWN",
                price=down_price,
                size=down_filled_size,
                cost=down_cost,
                is_pair=True,
                pair_trade_id=pair_trade_id,
            )
            self._trades.append(down_trade)

        # Update position
        if any_filled:
            self._update_position(
                market=market,
                up_size=up_filled_size,
                down_size=down_filled_size,
                up_cost=up_cost,
                down_cost=down_cost,
            )

        # Update stats
        self._stats.total_trades += 1
        if both_filled:
            self._stats.successful_pairs += 1
            pair_count = min(up_filled_size, down_filled_size)
            profit = pair_count * (1.0 - (up_price + down_price))
            self._stats.total_profit += profit
        elif any_filled:
            self._stats.partial_fills += 1
        else:
            self._stats.failed_fills += 1

        self._stats.total_cost += actual_cost

        # Create result
        result = PairExecutionResult(
            market=market,
            up_order=up_order,
            down_order=down_order,
            expected_cost=expected_cost,
            actual_cost=actual_cost,
            success=both_filled,
            error=None if both_filled else "Partial or no fill",
        )

        logger.info(
            f"Paper trade result: {'SUCCESS' if both_filled else 'PARTIAL/FAIL'}, "
            f"Up: {up_filled_size}/{size}, Down: {down_filled_size}/{size}, "
            f"Cost: ${actual_cost:.4f}"
        )

        return result

    async def execute_single_side_trade(
        self,
        market: BTCMarket,
        side: str,
        price: float,
        size: float,
        best_ask: Optional[float] = None,
        use_pending_orders: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute a single-side paper trade (UP or DOWN only).

        This is the core of gabagool's asymmetric strategy - buying one side
        at a time when it becomes cheap, rather than waiting for both sides
        to be cheap simultaneously.

        Args:
            market: The BTCMarket to trade
            side: "UP" or "DOWN"
            price: The bid price to buy at (may be below ask for patient orders)
            size: Number of shares to buy
            best_ask: Current best ask price (for fill probability simulation)
            use_pending_orders: If True, add to pending orders for tick-based fills
                               instead of instant fill determination

        Returns:
            Dict with trade result:
                - success: bool (or "pending" status if use_pending_orders)
                - filled_size: float
                - filled_price: float
                - cost: float
                - trade_id: str
                - status: "pending" | "filled" | "failed"
        """
        trade_id = self._generate_trade_id()
        side_upper = side.upper()

        # DISABLED: Pending order mode was buggy - force instant fills
        use_pending_orders = False

        # Enforce Polymarket order constraints: min 5 shares AND min $1 value
        order_value = size * price
        if size < MIN_ORDER_SHARES:
            logger.warning(f"[PAPER] Order rejected: {size} shares < min {MIN_ORDER_SHARES}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "side": side_upper,
                "status": "rejected",
                "error": f"Size {size} < min {MIN_ORDER_SHARES} shares",
            }
        if order_value < MIN_ORDER_VALUE:
            logger.warning(f"[PAPER] Order rejected: ${order_value:.2f} value < min ${MIN_ORDER_VALUE:.2f}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "side": side_upper,
                "status": "rejected",
                "error": f"Value ${order_value:.2f} < min ${MIN_ORDER_VALUE:.2f}",
            }

        # PENDING ORDER MODE: Add to pending orders for tick-based fills
        if use_pending_orders:
            key = f"{market.slug}_{side_upper}"

            # Check if there's already a pending order for this side
            if key in self._pending_orders:
                existing = self._pending_orders[key]
                logger.debug(
                    f"[PAPER] Replacing existing {side_upper} order: "
                    f"${existing['price']:.4f} → ${price:.4f}"
                )

            # Add to pending orders
            self._pending_orders[key] = {
                "order_id": trade_id,
                "price": price,
                "size": size,
                "placed_at": time.time(),
                "market": market,
                "market_slug": market.slug,
                "side": side_upper,
                "best_ask": best_ask or price,
            }

            logger.info(
                f"[PAPER] Placed {side_upper} order: {size} @ ${price:.4f} "
                f"(pending, will check fills each tick)"
            )

            return {
                "success": True,  # Order placed successfully (not filled yet)
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "side": side_upper,
                "status": "pending",
                "action": "placed",
            }

        # INSTANT FILL MODE (original behavior for backwards compatibility)
        logger.debug(f"Single-side trade: {size} {side_upper} @ ${price:.4f} on {market.slug}")

        # DYNAMIC FILL PROBABILITY SIMULATION
        # Uses the new model that considers price movement toward limit orders
        # If our limit is $0.03 below ask but price is likely to move toward us,
        # fill probability is higher than the static model would suggest
        if best_ask and price < best_ask:
            # Calculate dynamic fill probability considering price movement
            dynamic_fill_prob = self._calculate_dynamic_fill_probability(
                limit_price=price,
                best_ask=best_ask,
            )
            logger.debug(
                f"Patient order: ${price:.4f} vs ask ${best_ask:.4f}, "
                f"dynamic fill prob: {dynamic_fill_prob:.0%}"
            )

            # Roll against dynamic probability
            if random.random() > dynamic_fill_prob:
                logger.debug(f"Patient order unfilled: roll failed at {dynamic_fill_prob:.0%} probability")
                fill_type = FillType.NONE
            else:
                # Passed the fill check - determine full vs partial
                if random.random() < self.config.partial_fill_rate:
                    fill_type = FillType.PARTIAL
                else:
                    fill_type = FillType.FULL
        else:
            # At or above ask - use standard fill simulation
            fill_type = self._simulate_fill()

        filled_size = self._simulate_fill_size(size, fill_type)
        filled_price = self._simulate_price(price, is_buy=True) if filled_size > 0 else 0.0
        cost = filled_size * filled_price

        # Update balance
        if filled_size > 0:
            self._balance -= cost

            # Record trade
            trade = PaperTrade(
                trade_id=trade_id,
                market_slug=market.slug,
                side=side_upper,
                price=filled_price,
                size=filled_size,
                cost=cost,
                is_pair=False,
                pair_trade_id=None,
            )
            self._trades.append(trade)

            # Update position
            if side_upper == "UP":
                self._update_position(
                    market=market,
                    up_size=filled_size,
                    down_size=0,
                    up_cost=cost,
                    down_cost=0,
                )
            else:
                self._update_position(
                    market=market,
                    up_size=0,
                    down_size=filled_size,
                    up_cost=0,
                    down_cost=cost,
                )

            # Update stats
            self._stats.total_trades += 1

            logger.info(
                f"Single-side {side_upper} trade: {filled_size}/{size} filled @ ${filled_price:.4f}, "
                f"cost=${cost:.4f}, balance=${self._balance:.2f}"
            )
        else:
            logger.warning(f"Single-side {side_upper} trade failed to fill")
            self._stats.failed_fills += 1

        return {
            "success": filled_size > 0,
            "filled_size": filled_size,
            "filled_price": filled_price,
            "cost": cost,
            "trade_id": trade_id,
            "side": side_upper,
            "status": "filled" if filled_size > 0 else "failed",
        }

    async def cancel_and_replace(
        self,
        market: BTCMarket,
        side: str,
        new_price: float,
        new_size: float,
        best_ask: Optional[float] = None,
        price_tolerance: float = 0.005,
        stale_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Cancel existing order and replace if price drifted significantly.

        Mirrors live_trading.py cancel_and_replace behavior for paper mode.

        Args:
            market: The BTCMarket to trade
            side: "UP" or "DOWN"
            new_price: New target price
            new_size: New target size
            best_ask: Current best ask price
            price_tolerance: Minimum price drift to trigger replace (default 0.5%)
            stale_seconds: Maximum order age before replacing (default 10s)

        Returns:
            Dict with action taken and result
        """
        side_upper = side.upper()
        key = f"{market.slug}_{side_upper}"
        current_time = time.time()

        pending = self._pending_orders.get(key)

        if not pending:
            # No existing order - place new one
            return await self.execute_single_side_trade(
                market=market,
                side=side,
                price=new_price,
                size=new_size,
                best_ask=best_ask,
                use_pending_orders=True,
            )

        # Check if we should replace
        order_age = current_time - pending["placed_at"]
        old_price = pending["price"]
        price_diff = abs(new_price - old_price) / old_price if old_price > 0 else 0

        should_replace = (
            price_diff > price_tolerance or
            order_age >= stale_seconds
        )

        if not should_replace:
            # Keep existing order
            return {
                "success": True,
                "action": "kept",
                "order_id": pending["order_id"],
                "price": old_price,
                "size": pending["size"],
                "age": order_age,
                "side": side_upper,
            }

        # Replace: cancel existing and place new
        reason = "price_drift" if price_diff > price_tolerance else "stale"
        logger.info(
            f"[PAPER_REPLACE] {side_upper}: ${old_price:.4f} → ${new_price:.4f} "
            f"(reason={reason}, age={order_age:.1f}s, drift={price_diff:.2%})"
        )

        # Remove old order
        del self._pending_orders[key]

        # Place new order
        result = await self.execute_single_side_trade(
            market=market,
            side=side,
            price=new_price,
            size=new_size,
            best_ask=best_ask,
            use_pending_orders=True,
        )

        result["action"] = "replaced"
        result["old_price"] = old_price
        result["reason"] = reason

        return result

    def _update_position(
        self,
        market: BTCMarket,
        up_size: float,
        down_size: float,
        up_cost: float,
        down_cost: float,
    ) -> None:
        """Update paper position using unified Position model."""
        slug = market.slug

        if slug not in self._positions:
            self._positions[slug] = Position(
                market_slug=slug,
                market=market,
            )
            self._stats.markets_traded += 1

        pos = self._positions[slug]
        # Update underlying fields (not the property aliases)
        pos.up_balance += up_size
        pos.down_balance += down_size
        pos.up_total_cost += up_cost
        pos.down_total_cost += down_cost
        # Recalculate average prices
        pos.up_avg_price = pos.up_total_cost / pos.up_balance if pos.up_balance > 0 else 0.0
        pos.down_avg_price = pos.down_total_cost / pos.down_balance if pos.down_balance > 0 else 0.0

    def get_position(self, market: BTCMarket) -> Optional[Position]:
        """Get paper position for a market."""
        return self._positions.get(market.slug)

    # =========================================================================
    # PENDING ORDER MANAGEMENT (Quote Pulling Simulation)
    # =========================================================================

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending orders being tracked."""
        return self._pending_orders.copy()

    async def cancel_pending_order(self, key: str) -> bool:
        """
        Cancel a specific pending order by key.

        Args:
            key: Order key in format "{market_slug}_{side}"

        Returns:
            True if order was cancelled, False if not found
        """
        if key in self._pending_orders:
            order = self._pending_orders[key]
            logger.info(
                f"[PAPER_CANCEL] Cancelled {order['side']} order: "
                f"${order['price']:.4f} x {order['size']}"
            )
            del self._pending_orders[key]
            return True
        return False

    async def cancel_all_pending(self, market_slug: Optional[str] = None) -> int:
        """
        Cancel all pending orders, optionally filtered by market.

        Args:
            market_slug: If provided, only cancel orders for this market

        Returns:
            Number of orders cancelled
        """
        cancelled = 0
        keys_to_remove = []

        for key, order in self._pending_orders.items():
            if market_slug is None or order.get("market_slug") == market_slug:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._pending_orders[key]
            cancelled += 1

        if cancelled > 0:
            logger.info(f"[PAPER_CANCEL] Cancelled {cancelled} pending orders")

        return cancelled

    async def check_and_pull_stale_quotes(
        self,
        market: BTCMarket,
        trend_detector: 'TrendDetector',
        max_age_secs: float = 10.0,
        velocity_threshold_bps: float = 15.0,
    ) -> Dict[str, bool]:
        """
        Cancel pending orders when Binance moves against them (quote pulling).

        Implements professional MM behavior:
        - Cancel stale orders (held too long)
        - Cancel when Binance moves against the position (latency advantage)

        This simulates the protective benefit of quote pulling:
        - DOWN bid + UP trend → Pull DOWN (prevents buying losing side)
        - UP bid + DOWN trend → Pull UP

        Args:
            market: Current market being traded
            trend_detector: TrendDetector instance with Binance feed
            max_age_secs: Maximum order age before considering stale (default 10s)
            velocity_threshold_bps: Price velocity to trigger pull (default 15 bps/sec)

        Returns:
            Dict mapping side ("UP"/"DOWN") to whether quote was pulled
        """
        pulled = {"UP": False, "DOWN": False}
        current_time = time.time()

        for side in ["UP", "DOWN"]:
            key = f"{market.slug}_{side}"
            pending = self._pending_orders.get(key)

            if not pending:
                continue

            order_age = current_time - pending["placed_at"]

            # Condition 1: Order too old (stale quote - standard MM behavior)
            is_stale = order_age > max_age_secs

            # Condition 2: Binance moving against this side (latency advantage)
            should_pull_trend = trend_detector.should_pull_quote(side, velocity_threshold_bps)

            if is_stale or should_pull_trend:
                reason = "stale" if is_stale else "binance_move"
                logger.info(
                    f"[PAPER_QUOTE_PULL] Cancelling {side} order: "
                    f"reason={reason}, age={order_age:.1f}s, "
                    f"price=${pending['price']:.4f}"
                )

                # Remove from tracking
                del self._pending_orders[key]
                pulled[side] = True

                # Log the protective benefit
                if should_pull_trend and not is_stale:
                    logger.info(
                        f"[PAPER_QUOTE_PULL] Pulled {side} due to Binance movement "
                        f"(simulated latency advantage - prevented adverse fill)"
                    )

        return pulled

    async def check_pending_fills(
        self,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Check pending orders for fills using tick-based probability.

        Called each decision loop tick (~100ms) to simulate orders sitting
        in the orderbook and potentially getting filled.

        Fill probability accumulates over time:
        - P(fill by time t) = 1 - (1 - base_rate)^(t/expected_fill_time)

        Args:
            current_prices: Optional dict with current best_ask prices per side
                           {"UP": 0.45, "DOWN": 0.55}

        Returns:
            List of filled orders with fill details
        """
        fills = []
        current_time = time.time()
        expected_fill_time = 3.0  # seconds to achieve base fill probability (faster than stale timeout)

        keys_to_remove = []

        for key, pending in self._pending_orders.items():
            order_age = current_time - pending["placed_at"]

            # Minimum delay before fills can happen (queue simulation)
            if order_age < self.config.min_fill_delay_ms / 1000.0:
                continue

            # Calculate cumulative fill probability based on time
            # P(fill by t) = 1 - (1 - base)^(t/expected_time)
            base_prob = self.config.fill_probability
            cumulative_prob = 1 - (1 - base_prob) ** (order_age / expected_fill_time)

            # Adjust for price distance from ask
            best_ask = pending.get("best_ask", pending["price"])
            if pending["price"] < best_ask:
                # Patient order below ask - use dynamic fill calculation
                price_diff = best_ask - pending["price"]
                price_diff_pct = price_diff / best_ask if best_ask > 0 else 0

                # Price movement factor: orders closer to ask fill faster
                # Also consider if price moved toward our limit
                if current_prices:
                    side = pending["side"]
                    new_ask = current_prices.get(side, best_ask)
                    if new_ask < best_ask:
                        # Price moved toward our limit - higher fill chance
                        cumulative_prob *= 1.3
                    elif new_ask > best_ask:
                        # Price moved away - lower fill chance
                        cumulative_prob *= 0.7

                # Distance penalty
                distance_factor = max(0.5, 1.0 - (price_diff * 15))
                cumulative_prob *= distance_factor

            # NOTE: Removed per-tick competition check - it compounded to nearly 0% over many ticks
            # Competition is already modeled in the base fill_probability (55%)

            # Roll for fill
            if random.random() < cumulative_prob:
                # FILL!
                fill_type = FillType.PARTIAL if random.random() < self.config.partial_fill_rate else FillType.FULL
                filled_size = self._simulate_fill_size(pending["size"], fill_type)
                filled_price = self._simulate_price(pending["price"], is_buy=True)
                cost = filled_size * filled_price

                # Update balance
                self._balance -= cost

                # Record trade
                trade_id = pending.get("order_id", self._generate_trade_id())
                market = pending["market"]
                side = pending["side"]

                trade = PaperTrade(
                    trade_id=trade_id,
                    market_slug=market.slug,
                    side=side,
                    price=filled_price,
                    size=filled_size,
                    cost=cost,
                    is_pair=False,
                )
                self._trades.append(trade)

                # Update position
                if side == "UP":
                    self._update_position(market, filled_size, 0, cost, 0)
                else:
                    self._update_position(market, 0, filled_size, 0, cost)

                self._stats.total_trades += 1

                logger.info(
                    f"[PAPER_FILL] {side} order filled after {order_age:.1f}s: "
                    f"{filled_size}/{pending['size']} @ ${filled_price:.4f}, "
                    f"cost=${cost:.4f}"
                )

                fills.append({
                    "success": True,
                    "filled_size": filled_size,
                    "filled_price": filled_price,
                    "cost": cost,
                    "trade_id": trade_id,
                    "side": side,
                    "market_slug": market.slug,
                    "order_age": order_age,
                })

                keys_to_remove.append(key)

        # Remove filled orders from pending
        for key in keys_to_remove:
            del self._pending_orders[key]

        return fills

    def get_total_pnl(self) -> float:
        """
        Get total unrealized P&L from all positions.

        Returns:
            Total expected profit from complete pairs
        """
        total = 0.0
        for pos in self._positions.values():
            total += pos.expected_profit
        return total

    def get_realized_pnl(self) -> float:
        """Get realized P&L from resolved markets."""
        return self._stats.realized_pnl

    def resolve_market(self, market_slug: str, winning_side: str) -> float:
        """
        Resolve a market and calculate P&L.

        Args:
            market_slug: Market to resolve
            winning_side: "UP" or "DOWN"

        Returns:
            P&L from resolution
        """
        if market_slug not in self._positions:
            return 0.0

        pos = self._positions[market_slug]

        # Complete pairs always pay $1.00
        pairs = pos.pair_count
        pair_payout = pairs * 1.0
        pair_cost = pairs * pos.pair_cost
        pair_pnl = pair_payout - pair_cost

        # Unmatched tokens
        unmatched_pnl = 0.0
        if pos.up_size > pos.down_size:
            # Extra Up tokens
            extra = pos.up_size - pos.down_size
            extra_cost = extra * pos.up_avg_price
            if winning_side == "UP":
                unmatched_pnl = extra * 1.0 - extra_cost
            else:
                unmatched_pnl = -extra_cost
        elif pos.down_size > pos.up_size:
            # Extra Down tokens
            extra = pos.down_size - pos.up_size
            extra_cost = extra * pos.down_avg_price
            if winning_side == "DOWN":
                unmatched_pnl = extra * 1.0 - extra_cost
            else:
                unmatched_pnl = -extra_cost

        total_pnl = pair_pnl + unmatched_pnl

        # Update balance with payout
        payout = pairs * 1.0
        if winning_side == "UP" and pos.up_size > pos.down_size:
            payout += (pos.up_size - pos.down_size) * 1.0
        elif winning_side == "DOWN" and pos.down_size > pos.up_size:
            payout += (pos.down_size - pos.up_size) * 1.0

        self._balance += payout
        self._stats.realized_pnl += total_pnl

        # Remove position
        del self._positions[market_slug]

        logger.info(
            f"Market {market_slug} resolved ({winning_side}): "
            f"Pair P&L: ${pair_pnl:.4f}, Unmatched P&L: ${unmatched_pnl:.4f}, "
            f"Total: ${total_pnl:.4f}"
        )

        return total_pnl

    def get_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive simulation summary.

        Returns:
            Dictionary with all simulation data
        """
        return {
            "balance": {
                "initial": self.initial_balance,
                "current": self._balance,
                "change": self._balance - self.initial_balance,
            },
            "positions": {
                slug: pos.to_dict()
                for slug, pos in self._positions.items()
            },
            "stats": self._stats.to_dict(),
            "unrealized_pnl": self.get_total_pnl(),
            "realized_pnl": self.get_realized_pnl(),
            "total_trades": len(self._trades),
        }

    def reset(self) -> None:
        """Reset simulation to initial state."""
        self._balance = self.initial_balance
        self._positions.clear()
        self._trades.clear()
        self._pending_orders.clear()
        self._stats = SimulationStats()
        self._trade_counter = 0
        logger.info("Paper trading engine reset")

    def __repr__(self) -> str:
        return (
            f"PaperTradingEngine(balance=${self._balance:.2f}, "
            f"positions={len(self._positions)}, "
            f"trades={len(self._trades)})"
        )
