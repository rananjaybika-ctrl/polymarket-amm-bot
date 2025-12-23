"""
Live Trading Engine for real order execution.

Executes real trades via PolymarketClient with position tracking
and balance management. Uses same interface as PaperTradingEngine
for drop-in replacement.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import uuid

from src.api.polymarket_client import PolymarketClient, PolymarketClientError
from src.models.market import BTCMarket
from src.models.position import Position, Fill


logger = logging.getLogger(__name__)


@dataclass
class LivePosition:
    """
    Tracks a live position in a market.

    Mirrors PaperPosition interface for compatibility with bot.
    """
    market_slug: str
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_avg_price: float = 0.0
    down_avg_price: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    realized_pnl: float = 0.0
    fills: List[Dict[str, Any]] = field(default_factory=list)
    # Store token IDs for emergency sell
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None

    @property
    def pair_count(self) -> float:
        """Number of complete pairs (min of UP and DOWN)."""
        return min(self.up_shares, self.down_shares)

    @property
    def total_cost(self) -> float:
        """Total cost of position."""
        return self.up_cost + self.down_cost

    @property
    def avg_pair_cost(self) -> float:
        """Average pair cost (UP avg + DOWN avg)."""
        if self.pair_count == 0:
            return 0.0
        return self.up_avg_price + self.down_avg_price

    def calculate_expected_pnl_range(self) -> tuple:
        """
        Calculate expected P&L range based on resolution scenarios.

        Returns:
            Tuple of (min_pnl, max_pnl, locked_profit)
        """
        # Complete pairs: guaranteed $1 each, profit = $1 - pair_cost
        pairs = self.pair_count
        locked_profit = pairs * (1.0 - self.avg_pair_cost) if pairs > 0 else 0.0

        # Unhedged exposure
        unhedged_up = self.up_shares - pairs
        unhedged_down = self.down_shares - pairs

        # UP wins: unhedged UP worth $1 each, unhedged DOWN worth $0
        up_scenario = locked_profit + unhedged_up - (unhedged_up * self.up_avg_price if unhedged_up > 0 else 0)

        # DOWN wins: unhedged DOWN worth $1 each, unhedged UP worth $0
        down_scenario = locked_profit + unhedged_down - (unhedged_down * self.down_avg_price if unhedged_down > 0 else 0)

        # Adjust for cost of unhedged
        if unhedged_up > 0:
            up_scenario = locked_profit + unhedged_up * (1.0 - self.up_avg_price)
            down_scenario = locked_profit - unhedged_up * self.up_avg_price
        elif unhedged_down > 0:
            up_scenario = locked_profit - unhedged_down * self.down_avg_price
            down_scenario = locked_profit + unhedged_down * (1.0 - self.down_avg_price)
        else:
            up_scenario = locked_profit
            down_scenario = locked_profit

        min_pnl = min(up_scenario, down_scenario)
        max_pnl = max(up_scenario, down_scenario)

        return (min_pnl, max_pnl, locked_profit)

    def add_fill(self, side: str, price: float, size: float, cost: float) -> None:
        """Record a fill and update averages."""
        if side.upper() == "UP":
            new_cost = self.up_cost + cost
            new_shares = self.up_shares + size
            self.up_avg_price = new_cost / new_shares if new_shares > 0 else price
            self.up_shares = new_shares
            self.up_cost = new_cost
        else:
            new_cost = self.down_cost + cost
            new_shares = self.down_shares + size
            self.down_avg_price = new_cost / new_shares if new_shares > 0 else price
            self.down_shares = new_shares
            self.down_cost = new_cost

        self.fills.append({
            "side": side.upper(),
            "price": price,
            "size": size,
            "cost": cost,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


class LiveTradingEngine:
    """
    Live trading engine that executes real orders via Polymarket API.

    Implements same interface as PaperTradingEngine for compatibility.

    Example:
        client = PolymarketClient(config)
        await client.connect()

        engine = LiveTradingEngine(client, starting_balance=100.0)

        result = await engine.execute_single_side_trade(
            market=market,
            side="UP",
            price=0.45,
            size=10,
        )
    """

    def __init__(
        self,
        client: PolymarketClient,
        starting_balance: float = 100.0,
    ):
        """
        Initialize LiveTradingEngine.

        Args:
            client: Connected PolymarketClient
            starting_balance: Initial balance (for tracking, actual balance from API)
        """
        self.client = client
        self._starting_balance = starting_balance
        self._cached_balance: Optional[float] = None
        self._positions: Dict[str, LivePosition] = {}
        self._realized_pnl: float = 0.0
        self._trade_count: int = 0

        logger.info(f"LiveTradingEngine initialized with starting balance ${starting_balance:.2f}")

    @property
    def balance(self) -> float:
        """Current USDC balance (cached, refresh with sync_balance())."""
        if self._cached_balance is None:
            return self._starting_balance
        return self._cached_balance

    @property
    def positions(self) -> List[LivePosition]:
        """List of all positions."""
        return list(self._positions.values())

    async def sync_balance(self) -> float:
        """Fetch and cache current balance from API."""
        try:
            self._cached_balance = await self.client.get_balance()
            logger.debug(f"Balance synced: ${self._cached_balance:.2f}")
            return self._cached_balance
        except Exception as e:
            logger.error(f"Failed to sync balance: {e}")
            return self.balance

    def get_position(self, market: BTCMarket) -> Optional[LivePosition]:
        """Get position for a market."""
        return self._positions.get(market.slug)

    def get_realized_pnl(self) -> float:
        """Get total realized P&L from resolved markets."""
        return self._realized_pnl

    def get_total_pnl(self) -> float:
        """Get total unrealized P&L across all positions."""
        total = 0.0
        for pos in self._positions.values():
            _, _, locked = pos.calculate_expected_pnl_range()
            total += locked
        return total + self._realized_pnl

    async def execute_single_side_trade(
        self,
        market: BTCMarket,
        side: str,
        price: float,
        size: float,
    ) -> Dict[str, Any]:
        """
        Execute a single-side LIVE trade (UP or DOWN only).

        Args:
            market: The BTCMarket to trade
            side: "UP" or "DOWN"
            price: The price to buy at
            size: Number of shares to buy

        Returns:
            Dict with trade result:
                - success: bool
                - filled_size: float
                - filled_price: float
                - cost: float
                - trade_id: str
                - order_id: str (from Polymarket)
        """
        trade_id = f"LIVE-{uuid.uuid4().hex[:8]}"
        side_upper = side.upper()

        # Get token ID
        token_id = market.up_token_id if side_upper == "UP" else market.down_token_id

        logger.info(f"[LIVE] Placing order: {size} {side_upper} @ ${price:.4f} on {market.slug}")

        try:
            # Place real order
            result = await self.client.place_order(
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
            )

            # Parse result
            order_id = result.get("orderID") or result.get("order_id", trade_id)
            status = result.get("status", "unknown")

            # Check if order was matched
            if status in ["MATCHED", "FILLED", "LIVE"]:
                # For now, assume full fill at requested price
                # In production, should poll for actual fill status
                filled_size = size
                filled_price = price
                cost = filled_size * filled_price

                # Update position
                position = self._positions.get(market.slug)
                if position is None:
                    position = LivePosition(market_slug=market.slug)
                    self._positions[market.slug] = position

                # Store token ID for emergency sell capability
                if side_upper == "UP":
                    position.up_token_id = token_id
                else:
                    position.down_token_id = token_id

                position.add_fill(side_upper, filled_price, filled_size, cost)
                self._trade_count += 1

                # Sync balance after trade
                await self.sync_balance()

                logger.info(
                    f"[LIVE] Order filled: {filled_size} {side_upper} @ ${filled_price:.4f} "
                    f"(order_id={order_id[:16]}...)"
                )

                return {
                    "success": True,
                    "filled_size": filled_size,
                    "filled_price": filled_price,
                    "cost": cost,
                    "trade_id": trade_id,
                    "order_id": order_id,
                }
            else:
                logger.warning(f"[LIVE] Order not filled: status={status}")
                return {
                    "success": False,
                    "filled_size": 0,
                    "filled_price": 0,
                    "cost": 0,
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "error": f"Order status: {status}",
                }

        except PolymarketClientError as e:
            logger.error(f"[LIVE] Order failed: {e}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "error": str(e),
            }
        except Exception as e:
            logger.error(f"[LIVE] Unexpected error: {e}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "error": str(e),
            }

    def resolve_market(self, market_slug: str, winner: str) -> float:
        """
        Resolve a market and calculate realized P&L.

        Note: Actual redemption of winning tokens to USDC happens automatically
        on Polymarket after 1-5 minutes for 15-minute markets. This method
        calculates P&L based on the resolution outcome.

        TODO: Integrate proper redeem() call via polymarket-apis or similar
        to actively claim winnings rather than waiting for auto-redemption.

        Args:
            market_slug: The market slug
            winner: "UP" or "DOWN"

        Returns:
            Realized P&L from this market
        """
        position = self._positions.get(market_slug)
        if position is None:
            return 0.0

        # Calculate payout - winning shares worth $1 each
        if winner.upper() == "UP":
            payout = position.up_shares * 1.0
        else:
            payout = position.down_shares * 1.0

        pnl = payout - position.total_cost
        self._realized_pnl += pnl

        # Clear position
        del self._positions[market_slug]

        logger.info(
            f"[LIVE] Market resolved: {market_slug} winner={winner} "
            f"P&L=${pnl:.4f}"
        )

        return pnl

    async def sync_position(self, market: BTCMarket) -> Optional[LivePosition]:
        """
        Sync position from chain for a market.

        Fetches actual holdings from Polymarket API and updates local tracking.
        """
        try:
            up_balance = await self.client.get_position_balance(market.up_token_id)
            down_balance = await self.client.get_position_balance(market.down_token_id)

            if up_balance > 0 or down_balance > 0:
                position = self._positions.get(market.slug)
                if position is None:
                    position = LivePosition(market_slug=market.slug)
                    self._positions[market.slug] = position

                position.up_shares = up_balance
                position.down_shares = down_balance

                logger.debug(
                    f"Position synced for {market.slug}: "
                    f"UP={up_balance:.2f}, DOWN={down_balance:.2f}"
                )

                return position

            return None

        except Exception as e:
            logger.error(f"Failed to sync position for {market.slug}: {e}")
            return self._positions.get(market.slug)

    async def emergency_sell_all(self) -> Dict[str, Any]:
        """
        Emergency sell ALL positions at market price.

        Places aggressive sell orders (at low price to ensure fill) for all
        UP and DOWN shares across all markets.

        Returns:
            Dict with:
                - positions_closed: Number of markets with positions sold
                - total_up_sold: Total UP shares sold
                - total_down_sold: Total DOWN shares sold
                - realized_pnl: Estimated P&L (actual depends on fill prices)
        """
        logger.warning("=" * 50)
        logger.warning("[LIVE] EMERGENCY SELL ALL TRIGGERED")
        logger.warning("=" * 50)

        results = {
            "positions_closed": 0,
            "total_up_sold": 0.0,
            "total_down_sold": 0.0,
            "total_proceeds": 0.0,
            "total_cost": 0.0,
            "realized_pnl": 0.0,
            "errors": [],
        }

        # Process each position
        for market_slug, position in list(self._positions.items()):
            if position.up_shares <= 0 and position.down_shares <= 0:
                continue

            logger.warning(f"[LIVE] Selling position in {market_slug}: UP={position.up_shares:.2f}, DOWN={position.down_shares:.2f}")

            # We need the market object to get token IDs
            # For now, try to extract from existing fills or use aggressive pricing
            market_closed = False

            # Sell UP shares if any
            if position.up_shares > 0:
                try:
                    # Get token ID from position
                    up_token = position.up_token_id

                    if up_token:
                        # Sell at very low price to ensure fill (emergency)
                        sell_price = 0.01
                        result = await self.client.place_order(
                            token_id=up_token,
                            side="SELL",
                            price=sell_price,
                            size=position.up_shares,
                        )
                        if result.get("status") in ["MATCHED", "FILLED", "LIVE"]:
                            results["total_up_sold"] += position.up_shares
                            results["total_proceeds"] += position.up_shares * sell_price
                            market_closed = True
                            logger.info(f"[LIVE] Sold {position.up_shares:.2f} UP @ ${sell_price:.4f}")
                        else:
                            results["errors"].append(f"UP sell failed for {market_slug}: {result.get('status')}")
                    else:
                        logger.warning(f"[LIVE] No token ID for UP shares in {market_slug} - cannot sell")
                        results["errors"].append(f"No UP token ID for {market_slug}")
                except Exception as e:
                    logger.error(f"[LIVE] Failed to sell UP for {market_slug}: {e}")
                    results["errors"].append(f"UP sell error {market_slug}: {str(e)}")

            # Sell DOWN shares if any
            if position.down_shares > 0:
                try:
                    # Get token ID from position
                    down_token = position.down_token_id

                    if down_token:
                        sell_price = 0.01
                        result = await self.client.place_order(
                            token_id=down_token,
                            side="SELL",
                            price=sell_price,
                            size=position.down_shares,
                        )
                        if result.get("status") in ["MATCHED", "FILLED", "LIVE"]:
                            results["total_down_sold"] += position.down_shares
                            results["total_proceeds"] += position.down_shares * sell_price
                            market_closed = True
                            logger.info(f"[LIVE] Sold {position.down_shares:.2f} DOWN @ ${sell_price:.4f}")
                        else:
                            results["errors"].append(f"DOWN sell failed for {market_slug}: {result.get('status')}")
                    else:
                        logger.warning(f"[LIVE] No token ID for DOWN shares in {market_slug} - cannot sell")
                        results["errors"].append(f"No DOWN token ID for {market_slug}")
                except Exception as e:
                    logger.error(f"[LIVE] Failed to sell DOWN for {market_slug}: {e}")
                    results["errors"].append(f"DOWN sell error {market_slug}: {str(e)}")

            if market_closed:
                results["positions_closed"] += 1
                results["total_cost"] += position.total_cost

        # Calculate realized P&L
        results["realized_pnl"] = results["total_proceeds"] - results["total_cost"]

        logger.warning(
            f"[LIVE] Emergency sell complete: "
            f"{results['positions_closed']} positions, "
            f"UP={results['total_up_sold']:.2f}, DOWN={results['total_down_sold']:.2f}, "
            f"P&L=${results['realized_pnl']:.2f}"
        )

        # Sync balance after selling
        await self.sync_balance()

        return results
