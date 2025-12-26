"""
Paper Trading Engine for dry run simulation.

Simulates order execution without real API calls, allowing
strategy validation before risking real capital.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from enum import Enum

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
        Simulate whether an order fills.

        Returns:
            FillType indicating fill result
        """
        roll = random.random()

        if roll > self.config.fill_probability:
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

        # STATIC COMPONENT: Original distance-based penalty
        # Linear decay: 100% at ask → 40% at 3+ cents below
        static_prob = max(0.40, 1.0 - (price_diff * 20))

        if not self.config.dynamic_fill_enabled:
            return static_prob * self.config.fill_probability

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

        Returns:
            Dict with trade result:
                - success: bool
                - filled_size: float
                - filled_price: float
                - cost: float
                - trade_id: str
        """
        trade_id = self._generate_trade_id()
        side_upper = side.upper()

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
        }

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
        self._stats = SimulationStats()
        self._trade_counter = 0
        logger.info("Paper trading engine reset")

    def __repr__(self) -> str:
        return (
            f"PaperTradingEngine(balance=${self._balance:.2f}, "
            f"positions={len(self._positions)}, "
            f"trades={len(self._trades)})"
        )
