"""
Pair Analyzer service for detecting profitable trading opportunities.

Analyzes Up/Down token orderbooks to find pair_cost < $1.00 arbitrage.
When pair_cost < $1.00, buying both tokens guarantees profit on resolution.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional, List, TYPE_CHECKING
from datetime import datetime, timezone

if TYPE_CHECKING:
    from src.models.position import Position

from src.models.market import BTCMarket
from src.models.orderbook import Orderbook
from src.api.polymarket_client import PolymarketClient


logger = logging.getLogger(__name__)


@dataclass
class PairOpportunity:
    """
    Represents a potential arbitrage opportunity in a BTC Up/Down market.

    When pair_cost < $1.00, you can:
    1. Buy X shares of Up at up_ask price
    2. Buy X shares of Down at down_ask price
    3. Total cost = X * (up_ask + down_ask) = X * pair_cost
    4. On resolution: one side pays $1.00/share, other pays $0
    5. Profit = X * ($1.00 - pair_cost)

    Attributes:
        market: The BTCMarket this opportunity is for
        up_orderbook: Orderbook for the Up token
        down_orderbook: Orderbook for the Down token
        timestamp: When this analysis was performed
    """

    market: BTCMarket
    up_orderbook: Orderbook
    down_orderbook: Orderbook
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now(timezone.utc)

    @property
    def up_ask(self) -> Optional[float]:
        """Best ask price for Up token."""
        return self.up_orderbook.best_ask

    @property
    def down_ask(self) -> Optional[float]:
        """Best ask price for Down token."""
        return self.down_orderbook.best_ask

    @property
    def up_bid(self) -> Optional[float]:
        """Best bid price for Up token."""
        return self.up_orderbook.best_bid

    @property
    def down_bid(self) -> Optional[float]:
        """Best bid price for Down token."""
        return self.down_orderbook.best_bid

    @property
    def pair_cost(self) -> Optional[float]:
        """
        Cost to buy one pair (1 Up + 1 Down share).

        If < $1.00, this is a profitable arbitrage opportunity.

        Returns:
            Pair cost or None if asks unavailable
        """
        if self.up_ask is None or self.down_ask is None:
            return None
        return self.up_ask + self.down_ask

    @property
    def pair_value(self) -> Optional[float]:
        """
        Value received when selling one pair.

        If > $1.00 and you hold pairs, you can sell for profit.

        Returns:
            Combined bid prices or None if bids unavailable
        """
        if self.up_bid is None or self.down_bid is None:
            return None
        return self.up_bid + self.down_bid

    @property
    def profit_per_pair(self) -> Optional[float]:
        """
        Profit per pair if bought at current ask prices.

        Returns:
            $1.00 - pair_cost, or None if unavailable
        """
        if self.pair_cost is None:
            return None
        return 1.0 - self.pair_cost

    @property
    def is_profitable(self) -> bool:
        """
        Check if this opportunity is profitable.

        Returns:
            True if pair_cost < $1.00
        """
        return self.pair_cost is not None and self.pair_cost < 1.0

    @property
    def executable_size(self) -> float:
        """
        Maximum pairs that can be executed at best prices.

        Limited by the smaller of Up ask size and Down ask size.

        Returns:
            Number of pairs executable at best price
        """
        up_size = self.up_orderbook.best_ask_size
        down_size = self.down_orderbook.best_ask_size
        return min(up_size, down_size)

    @property
    def max_profit(self) -> float:
        """
        Maximum profit at current best prices.

        Returns:
            executable_size * profit_per_pair, or 0 if not profitable
        """
        if not self.is_profitable:
            return 0.0
        return self.executable_size * self.profit_per_pair

    @property
    def up_spread(self) -> Optional[float]:
        """Spread on the Up token orderbook."""
        return self.up_orderbook.spread

    @property
    def down_spread(self) -> Optional[float]:
        """Spread on the Down token orderbook."""
        return self.down_orderbook.spread

    def cost_for_pairs(self, num_pairs: float) -> float:
        """
        Calculate cost to buy a specific number of pairs.

        Walks through orderbook depth to get accurate cost.

        Args:
            num_pairs: Number of pairs to buy

        Returns:
            Total cost in dollars
        """
        up_cost = self.up_orderbook.cost_for_size(num_pairs, side="ask")
        down_cost = self.down_orderbook.cost_for_size(num_pairs, side="ask")
        return up_cost + down_cost

    def profit_for_pairs(self, num_pairs: float) -> float:
        """
        Calculate profit for buying a specific number of pairs.

        Args:
            num_pairs: Number of pairs to buy

        Returns:
            Expected profit (payout - cost)
        """
        cost = self.cost_for_pairs(num_pairs)
        payout = num_pairs * 1.0  # Each pair pays $1.00 on resolution
        return payout - cost

    def has_liquidity(self) -> bool:
        """Check if both orderbooks have liquidity."""
        return self.up_orderbook.has_liquidity() and self.down_orderbook.has_liquidity()

    def __repr__(self) -> str:
        pair_cost = self.pair_cost
        if pair_cost is None:
            return f"PairOpportunity({self.market.slug}, no liquidity)"

        profit_str = f"+${self.profit_per_pair:.4f}" if self.is_profitable else f"${self.profit_per_pair:.4f}"
        return f"PairOpportunity({self.market.slug}, cost=${pair_cost:.4f}, profit={profit_str})"

    def __str__(self) -> str:
        lines = [
            f"Pair Opportunity: {self.market.question}",
            f"",
            f"  Up Token:",
            f"    Best Bid: ${self.up_bid:.4f}" if self.up_bid else "    Best Bid: None",
            f"    Best Ask: ${self.up_ask:.4f} x {self.up_orderbook.best_ask_size:.1f}" if self.up_ask else "    Best Ask: None",
            f"",
            f"  Down Token:",
            f"    Best Bid: ${self.down_bid:.4f}" if self.down_bid else "    Best Bid: None",
            f"    Best Ask: ${self.down_ask:.4f} x {self.down_orderbook.best_ask_size:.1f}" if self.down_ask else "    Best Ask: None",
            f"",
            f"  Analysis:",
        ]

        if self.pair_cost is not None:
            lines.append(f"    Pair Cost: ${self.pair_cost:.4f}")
            lines.append(f"    Profit/Pair: ${self.profit_per_pair:.4f}")
            lines.append(f"    Executable: {self.executable_size:.1f} pairs")
            lines.append(f"    Max Profit: ${self.max_profit:.2f}")
            lines.append(f"    Status: {'PROFITABLE' if self.is_profitable else 'Not profitable'}")
        else:
            lines.append("    Insufficient liquidity for analysis")

        return "\n".join(lines)


@dataclass
class AsymmetricOpportunity:
    """
    Opportunity analysis for gabagool-style asymmetric trading.

    Unlike PairOpportunity which requires both sides cheap NOW,
    this checks each side independently against the prospective pair cost
    based on current position.

    Attributes:
        market: The BTCMarket being analyzed
        up_orderbook: Current Up token orderbook
        down_orderbook: Current Down token orderbook
        current_up_size: Current position Up size
        current_down_size: Current position Down size
        current_up_cost: Current position Up cost
        current_down_cost: Current position Down cost
        pair_cost_threshold: Maximum pair cost to allow buying (default 0.99)
    """
    market: BTCMarket
    up_orderbook: Orderbook
    down_orderbook: Orderbook
    current_up_size: float = 0.0
    current_down_size: float = 0.0
    current_up_cost: float = 0.0
    current_down_cost: float = 0.0
    pair_cost_threshold: float = 0.99
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def up_ask(self) -> Optional[float]:
        """Best ask price for Up token."""
        return self.up_orderbook.best_ask

    @property
    def down_ask(self) -> Optional[float]:
        """Best ask price for Down token."""
        return self.down_orderbook.best_ask

    @property
    def up_bid(self) -> Optional[float]:
        """Best bid price for Up token (for patient bidding)."""
        return self.up_orderbook.best_bid

    @property
    def down_bid(self) -> Optional[float]:
        """Best bid price for Down token (for patient bidding)."""
        return self.down_orderbook.best_bid

    @property
    def up_available_size(self) -> float:
        """Available size at best ask for Up token."""
        return self.up_orderbook.best_ask_size

    @property
    def down_available_size(self) -> float:
        """Available size at best ask for Down token."""
        return self.down_orderbook.best_ask_size

    @property
    def current_pair_cost(self) -> float:
        """Current average pair cost from existing position."""
        hedged = min(self.current_up_size, self.current_down_size)
        if hedged <= 0:
            return 0.0
        avg_up = self.current_up_cost / self.current_up_size if self.current_up_size > 0 else 0
        avg_down = self.current_down_cost / self.current_down_size if self.current_down_size > 0 else 0
        return avg_up + avg_down

    @property
    def current_pair_count(self) -> int:
        """Number of complete pairs in current position."""
        return int(min(self.current_up_size, self.current_down_size))

    def calculate_prospective_pair_cost(self, side: str, buy_price: float, buy_qty: float) -> float:
        """
        Calculate what pair cost would be after a prospective buy.

        Args:
            side: "UP" or "DOWN"
            buy_price: Price we'd pay per share
            buy_qty: Number of shares to buy

        Returns:
            Prospective pair cost after buy, or inf if no hedge possible
        """
        if side.upper() == "UP":
            new_up_cost = self.current_up_cost + (buy_price * buy_qty)
            new_up_size = self.current_up_size + buy_qty
            new_down_cost = self.current_down_cost
            new_down_size = self.current_down_size
        else:
            new_up_cost = self.current_up_cost
            new_up_size = self.current_up_size
            new_down_cost = self.current_down_cost + (buy_price * buy_qty)
            new_down_size = self.current_down_size + buy_qty

        hedged_qty = min(new_up_size, new_down_size)
        if hedged_qty <= 0:
            # First buy - return buy price as prospective cost
            # This allows buying cheap when no hedge exists yet
            return buy_price

        avg_up = new_up_cost / new_up_size if new_up_size > 0 else 0
        avg_down = new_down_cost / new_down_size if new_down_size > 0 else 0
        return avg_up + avg_down

    def should_buy_up(self, buy_qty: float) -> bool:
        """
        Check if buying UP would keep pair cost below threshold.

        Args:
            buy_qty: Number of UP shares to buy

        Returns:
            True if we should buy
        """
        if self.up_ask is None or buy_qty <= 0:
            return False
        prospective = self.calculate_prospective_pair_cost("UP", self.up_ask, buy_qty)
        return prospective < self.pair_cost_threshold

    def should_buy_down(self, buy_qty: float) -> bool:
        """
        Check if buying DOWN would keep pair cost below threshold.

        Args:
            buy_qty: Number of DOWN shares to buy

        Returns:
            True if we should buy
        """
        if self.down_ask is None or buy_qty <= 0:
            return False
        prospective = self.calculate_prospective_pair_cost("DOWN", self.down_ask, buy_qty)
        return prospective < self.pair_cost_threshold

    @property
    def up_prospective_pair_cost(self) -> Optional[float]:
        """Prospective pair cost if we bought UP at best ask."""
        if self.up_ask is None or self.up_available_size <= 0:
            return None
        return self.calculate_prospective_pair_cost("UP", self.up_ask, self.up_available_size)

    @property
    def down_prospective_pair_cost(self) -> Optional[float]:
        """Prospective pair cost if we bought DOWN at best ask."""
        if self.down_ask is None or self.down_available_size <= 0:
            return None
        return self.calculate_prospective_pair_cost("DOWN", self.down_ask, self.down_available_size)

    @property
    def up_should_buy(self) -> bool:
        """Whether we should buy UP based on prospective pair cost."""
        return self.should_buy_up(self.up_available_size)

    @property
    def down_should_buy(self) -> bool:
        """Whether we should buy DOWN based on prospective pair cost."""
        return self.should_buy_down(self.down_available_size)

    @property
    def has_opportunity(self) -> bool:
        """Whether either side should be bought."""
        return self.up_should_buy or self.down_should_buy

    def __repr__(self) -> str:
        up_info = f"UP@${self.up_ask:.4f}->{'BUY' if self.up_should_buy else 'SKIP'}" if self.up_ask else "UP:None"
        down_info = f"DOWN@${self.down_ask:.4f}->{'BUY' if self.down_should_buy else 'SKIP'}" if self.down_ask else "DOWN:None"
        return f"AsymmetricOpportunity({self.market.slug}, {up_info}, {down_info}, pairs={self.current_pair_count})"


class PairAnalyzer:
    """
    Service for analyzing BTC Up/Down markets for trading opportunities.

    Fetches orderbooks and calculates pair costs to identify arbitrage.

    Example:
        analyzer = PairAnalyzer(polymarket_client)
        opportunity = await analyzer.analyze_market(market)
        if opportunity.is_profitable:
            print(f"Profit: ${opportunity.profit_per_pair}")
    """

    def __init__(self, client: PolymarketClient):
        """
        Initialize PairAnalyzer.

        Args:
            client: Connected PolymarketClient for API calls
        """
        self.client = client

    async def analyze_market(self, market: BTCMarket) -> PairOpportunity:
        """
        Analyze a market for trading opportunities.

        Fetches orderbooks for both Up and Down tokens and
        calculates pair cost and potential profit.

        Args:
            market: BTCMarket to analyze

        Returns:
            PairOpportunity with analysis results
        """
        # Fetch both orderbooks in parallel for speed
        up_response, down_response = await asyncio.gather(
            self.client.get_orderbook(market.up_token_id),
            self.client.get_orderbook(market.down_token_id),
        )

        # Convert to our Orderbook model
        up_orderbook = Orderbook.from_clob_response(up_response)
        down_orderbook = Orderbook.from_clob_response(down_response)

        opportunity = PairOpportunity(
            market=market,
            up_orderbook=up_orderbook,
            down_orderbook=down_orderbook,
        )

        logger.info(
            f"Analyzed {market.slug}: pair_cost=${opportunity.pair_cost:.4f}, "
            f"profitable={opportunity.is_profitable}"
            if opportunity.pair_cost else f"Analyzed {market.slug}: no liquidity"
        )

        return opportunity

    async def analyze_asymmetric_opportunity(
        self,
        market: BTCMarket,
        current_up_size: float = 0.0,
        current_down_size: float = 0.0,
        current_up_cost: float = 0.0,
        current_down_cost: float = 0.0,
        pair_cost_threshold: float = 0.99,
    ) -> AsymmetricOpportunity:
        """
        Analyze market for asymmetric trading opportunity.

        This is the gabagool-style analysis: instead of checking if
        pair_cost < $1.00 right now, we check if buying either side
        would keep our AVERAGE pair cost below threshold.

        Args:
            market: BTCMarket to analyze
            current_up_size: Current UP position size
            current_down_size: Current DOWN position size
            current_up_cost: Total cost of current UP position
            current_down_cost: Total cost of current DOWN position
            pair_cost_threshold: Max acceptable pair cost (default 0.99)

        Returns:
            AsymmetricOpportunity with buy recommendations for each side
        """
        # Fetch both orderbooks in parallel for speed
        up_response, down_response = await asyncio.gather(
            self.client.get_orderbook(market.up_token_id),
            self.client.get_orderbook(market.down_token_id),
        )

        up_orderbook = Orderbook.from_clob_response(up_response)
        down_orderbook = Orderbook.from_clob_response(down_response)

        opportunity = AsymmetricOpportunity(
            market=market,
            up_orderbook=up_orderbook,
            down_orderbook=down_orderbook,
            current_up_size=current_up_size,
            current_down_size=current_down_size,
            current_up_cost=current_up_cost,
            current_down_cost=current_down_cost,
            pair_cost_threshold=pair_cost_threshold,
        )

        logger.debug(
            f"Asymmetric analysis {market.slug}: "
            f"UP@${opportunity.up_ask:.4f}->{'BUY' if opportunity.up_should_buy else 'SKIP'}, "
            f"DOWN@${opportunity.down_ask:.4f}->{'BUY' if opportunity.down_should_buy else 'SKIP'}, "
            f"current_pairs={opportunity.current_pair_count}"
            if opportunity.up_ask and opportunity.down_ask else
            f"Asymmetric analysis {market.slug}: no liquidity"
        )

        return opportunity

    async def analyze_markets(self, markets: List[BTCMarket]) -> List[PairOpportunity]:
        """
        Analyze multiple markets.

        Args:
            markets: List of markets to analyze

        Returns:
            List of PairOpportunity results
        """
        opportunities = []
        for market in markets:
            try:
                opp = await self.analyze_market(market)
                opportunities.append(opp)
            except Exception as e:
                logger.warning(f"Failed to analyze {market.slug}: {e}")

        return opportunities

    async def get_best_opportunity(
        self,
        markets: List[BTCMarket],
        min_profit: float = 0.0,
    ) -> Optional[PairOpportunity]:
        """
        Find the best profitable opportunity across markets.

        Args:
            markets: Markets to analyze
            min_profit: Minimum profit per pair required

        Returns:
            Best PairOpportunity, or None if none meet criteria
        """
        opportunities = await self.analyze_markets(markets)

        # Filter to profitable ones meeting minimum
        profitable = [
            opp for opp in opportunities
            if opp.is_profitable and opp.profit_per_pair >= min_profit
        ]

        if not profitable:
            return None

        # Return the one with highest profit per pair
        return max(profitable, key=lambda o: o.profit_per_pair)

    async def find_opportunities(
        self,
        markets: List[BTCMarket],
        min_profit: float = 0.0,
        min_size: float = 1.0,
    ) -> List[PairOpportunity]:
        """
        Find all profitable opportunities meeting criteria.

        Args:
            markets: Markets to analyze
            min_profit: Minimum profit per pair
            min_size: Minimum executable size

        Returns:
            List of profitable opportunities, sorted by profit desc
        """
        opportunities = await self.analyze_markets(markets)

        # Filter
        profitable = [
            opp for opp in opportunities
            if opp.is_profitable
            and opp.profit_per_pair >= min_profit
            and opp.executable_size >= min_size
        ]

        # Sort by profit descending
        profitable.sort(key=lambda o: o.profit_per_pair, reverse=True)

        return profitable

    async def monitor_market(
        self,
        market: BTCMarket,
        threshold: float = 1.0,
        interval: float = 1.0,
        on_opportunity: Optional[callable] = None,
        max_iterations: int = 0,
    ) -> Optional[PairOpportunity]:
        """
        Monitor a single market for pair cost below threshold.

        Continuously polls the orderbook and checks pair cost.
        Stops when pair_cost < threshold or market expires.

        Args:
            market: Market to monitor
            threshold: Pair cost threshold (default $1.00)
            interval: Seconds between checks
            on_opportunity: Callback when opportunity found (receives PairOpportunity)
            max_iterations: Maximum iterations (0 = until market expires)

        Returns:
            PairOpportunity if threshold crossed, None if market expired
        """
        import asyncio

        iteration = 0

        while True:
            # Check if market expired
            if market.is_expired():
                logger.info(f"Market {market.slug} expired, stopping monitor")
                return None

            # Check iteration limit
            if max_iterations > 0 and iteration >= max_iterations:
                logger.info(f"Reached max iterations ({max_iterations})")
                return None

            try:
                opportunity = await self.analyze_market(market)

                if opportunity.pair_cost is not None:
                    logger.debug(
                        f"Monitor {market.slug}: pair_cost=${opportunity.pair_cost:.4f}"
                    )

                    if opportunity.pair_cost < threshold:
                        logger.info(
                            f"OPPORTUNITY: {market.slug} pair_cost=${opportunity.pair_cost:.4f} "
                            f"< threshold=${threshold:.4f}"
                        )
                        if on_opportunity:
                            on_opportunity(opportunity)
                        return opportunity

            except Exception as e:
                logger.warning(f"Monitor error for {market.slug}: {e}")

            iteration += 1
            await asyncio.sleep(interval)

    async def monitor_markets(
        self,
        markets: List[BTCMarket],
        threshold: float = 1.0,
        interval: float = 2.0,
        on_opportunity: Optional[callable] = None,
        max_iterations: int = 0,
    ) -> Optional[PairOpportunity]:
        """
        Monitor multiple markets for opportunities.

        Rotates through markets checking pair cost on each.
        Returns first opportunity found below threshold.

        Args:
            markets: Markets to monitor
            threshold: Pair cost threshold
            interval: Seconds between full scans
            on_opportunity: Callback when opportunity found
            max_iterations: Maximum scan iterations (0 = indefinite)

        Returns:
            First PairOpportunity below threshold, or None
        """
        import asyncio

        iteration = 0

        while True:
            # Filter to non-expired markets
            active = [m for m in markets if not m.is_expired()]

            if not active:
                logger.info("All markets expired, stopping monitor")
                return None

            if max_iterations > 0 and iteration >= max_iterations:
                logger.info(f"Reached max iterations ({max_iterations})")
                return None

            # Analyze all active markets
            for market in active:
                try:
                    opportunity = await self.analyze_market(market)

                    if opportunity.pair_cost is not None and opportunity.pair_cost < threshold:
                        logger.info(
                            f"OPPORTUNITY: {market.slug} pair_cost=${opportunity.pair_cost:.4f}"
                        )
                        if on_opportunity:
                            on_opportunity(opportunity)
                        return opportunity

                except Exception as e:
                    logger.warning(f"Error analyzing {market.slug}: {e}")

            iteration += 1
            await asyncio.sleep(interval)

    def __repr__(self) -> str:
        return f"PairAnalyzer(client={self.client})"
