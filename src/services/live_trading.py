"""
Live Trading Engine for real order execution.

Executes real trades via PolymarketClient with position tracking
and balance management. Uses same interface as PaperTradingEngine
for drop-in replacement.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import uuid

from py_clob_client.clob_types import OrderType

from src.api.polymarket_client import PolymarketClient, PolymarketClientError
from src.models.market import BTCMarket
from src.models.position import Position, Fill


logger = logging.getLogger(__name__)


def calculate_dynamic_timeout(time_remaining_secs: float, is_emergency: bool = False) -> float:
    """
    Calculate appropriate order timeout based on time remaining in market.

    Returns shorter timeouts when less time remains to avoid hanging orders
    that waste valuable trading time.

    Args:
        time_remaining_secs: Seconds until market ends
        is_emergency: If True, return minimum timeout for urgent situations

    Returns:
        Timeout in seconds (3.0 to 30.0)
    """
    if is_emergency:
        return 3.0
    if time_remaining_secs > 600:   # >10 min: full patience
        return 30.0
    if time_remaining_secs > 300:   # 5-10 min: moderate patience
        return 20.0
    if time_remaining_secs > 120:   # 2-5 min: reduced patience
        return 10.0
    # <2 min: minimal patience
    return 5.0


def calculate_fallback_price(best_ask: float, urgency: str = "normal") -> float:
    """
    Calculate fallback price when patient pricing fails.

    Progressively more aggressive pricing based on urgency level.

    Args:
        best_ask: Current best ask price
        urgency: "normal", "urgent", or "critical"

    Returns:
        Fallback price (more likely to fill)
    """
    if urgency == "normal":
        # Slight improvement over patient price
        return min(0.98, best_ask + 0.005)
    if urgency == "urgent":
        # Meet the ask
        return min(0.98, best_ask)
    if urgency == "critical":
        # Improve slightly over ask
        return min(0.98, best_ask + 0.01)
    return min(0.98, best_ask + 0.02)  # panic


# LivePosition has been consolidated into the unified Position class
# in src/models/position.py. This eliminates ~140 lines of duplicate code
# and ensures paper and live trading use identical position calculations.


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
        use_fok: bool = False,
    ):
        """
        Initialize LiveTradingEngine.

        Args:
            client: Connected PolymarketClient
            starting_balance: Initial balance (for tracking, actual balance from API)
            use_fok: Use Fill-Or-Kill orders (default False).
                     GTC (default) is required for patient pricing strategy where
                     bids are placed below best ask. Orders are polled for fill
                     status with 30s timeout.
                     FOK can be enabled for aggressive orders that must fill immediately.
        """
        self.client = client
        self._starting_balance = starting_balance
        self._cached_balance: Optional[float] = None
        self._positions: Dict[str, Position] = {}
        self._realized_pnl: float = 0.0
        self._trade_count: int = 0
        self._use_fok = use_fok
        self._order_type = OrderType.FOK if use_fok else OrderType.GTC

        order_type_str = "FOK (Fill-Or-Kill)" if use_fok else "GTC (Good-Til-Cancelled)"
        logger.info(f"LiveTradingEngine initialized: balance=${starting_balance:.2f}, order_type={order_type_str}")

    @property
    def balance(self) -> float:
        """Current USDC balance (cached, refresh with sync_balance())."""
        if self._cached_balance is None:
            return self._starting_balance
        return self._cached_balance

    @property
    def positions(self) -> List[Position]:
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

    async def _poll_order_until_filled(
        self,
        order_id: str,
        requested_size: float,
        timeout_seconds: float = 30.0,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """
        Poll order status until filled, cancelled, or timeout.

        For GTC orders that may sit in the orderbook waiting to fill,
        this method polls the order status and handles timeouts.

        Args:
            order_id: The order ID to poll
            requested_size: Original requested size (for partial fill detection)
            timeout_seconds: Max time to wait before cancelling (default 30s)
            poll_interval: Time between status checks (default 2s)

        Returns:
            Dict with filled_size, final_status, was_cancelled
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time

            if elapsed >= timeout_seconds:
                # Timeout - cancel unfilled portion
                try:
                    await self.client.cancel_order(order_id)
                    logger.info(f"[LIVE] Timeout after {timeout_seconds:.0f}s, cancelled order {order_id[:16]}...")
                except Exception as e:
                    logger.warning(f"[LIVE] Failed to cancel order on timeout: {e}")

                # Get final fill status after cancellation
                try:
                    final_status = await self.client.get_order(order_id)
                    filled = float(final_status.get("size_matched", 0)) if final_status else 0
                except Exception:
                    filled = 0

                return {"filled_size": filled, "final_status": "TIMEOUT", "was_cancelled": True}

            # Poll order status
            try:
                status = await self.client.get_order(order_id)
                if not status:
                    await asyncio.sleep(poll_interval)
                    continue

                order_status = status.get("status", "").upper()
                filled_size = float(status.get("size_matched", 0))

                if order_status in ["MATCHED", "FILLED"]:
                    return {"filled_size": filled_size, "final_status": "FILLED", "was_cancelled": False}
                elif order_status == "CANCELLED":
                    return {"filled_size": filled_size, "final_status": "CANCELLED", "was_cancelled": False}

                # Still LIVE - log partial fill progress
                if filled_size > 0 and filled_size < requested_size:
                    logger.debug(f"[LIVE] Partial fill in progress: {filled_size}/{requested_size}")

            except Exception as e:
                logger.warning(f"[LIVE] Poll error: {e}")

            await asyncio.sleep(poll_interval)

    def get_position(self, market: BTCMarket) -> Optional[Position]:
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
        best_ask: float = None,
        time_remaining: float = None,
        enable_fallback: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a single-side LIVE trade (UP or DOWN only).

        Args:
            market: The BTCMarket to trade
            side: "UP" or "DOWN"
            price: The price to buy at
            size: Number of shares to buy
            best_ask: Current best ask (used for fallback pricing)
            time_remaining: Seconds until market ends (for dynamic timeout)
            enable_fallback: If True and patient price times out, retry at aggressive price

        Returns:
            Dict with trade result:
                - success: bool
                - filled_size: float
                - filled_price: float
                - cost: float
                - trade_id: str
                - order_id: str (from Polymarket)
                - used_fallback: bool (if fallback pricing was used)
        """
        trade_id = f"LIVE-{uuid.uuid4().hex[:8]}"
        side_upper = side.upper()

        # Get token ID
        token_id = market.up_token_id if side_upper == "UP" else market.down_token_id

        order_type_label = "FOK" if self._use_fok else "GTC"
        logger.info(f"[LIVE] Placing {order_type_label} order: {size} {side_upper} @ ${price:.4f} on {market.slug}")

        try:
            # Place real order with configured order type
            result = await self.client.place_order(
                token_id=token_id,
                side="BUY",
                price=price,
                size=size,
                order_type=self._order_type,
            )

            # Parse result
            order_id = result.get("orderID") or result.get("order_id", trade_id)
            status = result.get("status", "unknown").upper()

            # MATCHED/FILLED = immediate fill (works for both FOK and GTC)
            # LIVE = GTC order sitting in orderbook (needs polling)
            # CANCELLED = FOK couldn't fill or order was cancelled
            if status in ["MATCHED", "FILLED"]:
                # Immediate fill - use requested size
                filled_size = size
                filled_price = price
                cost = filled_size * filled_price

                # Update position
                position = self._positions.get(market.slug)
                if position is None:
                    position = Position(market_slug=market.slug)
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
            elif status == "LIVE" and not self._use_fok:
                # GTC order is live (sitting in orderbook) - poll until filled or timeout
                # Use dynamic timeout based on time remaining in market
                timeout = calculate_dynamic_timeout(
                    time_remaining_secs=time_remaining if time_remaining else 600,
                    is_emergency=False,
                )
                logger.info(f"[LIVE] GTC order LIVE, polling for fill (timeout={timeout:.0f}s)...")
                poll_result = await self._poll_order_until_filled(
                    order_id=order_id,
                    requested_size=size,
                    timeout_seconds=timeout,
                    poll_interval=2.0,
                )

                filled_size = poll_result["filled_size"]
                if filled_size > 0:
                    filled_price = price  # Limit order fills at our price
                    cost = filled_size * filled_price

                    # Update position with ACTUAL filled size (may be partial)
                    position = self._positions.get(market.slug)
                    if position is None:
                        position = Position(market_slug=market.slug)
                        self._positions[market.slug] = position

                    if side_upper == "UP":
                        position.up_token_id = token_id
                    else:
                        position.down_token_id = token_id

                    position.add_fill(side_upper, filled_price, filled_size, cost)
                    self._trade_count += 1
                    await self.sync_balance()

                    if filled_size < size:
                        logger.info(
                            f"[LIVE] PARTIAL FILL: {filled_size}/{size} {side_upper} @ ${filled_price:.4f} "
                            f"(order_id={order_id[:16]}...)"
                        )
                    else:
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
                        "partial": filled_size < size,
                    }
                else:
                    # No fill after timeout - try fallback pricing if enabled
                    logger.info(f"[LIVE] Order unfilled after {timeout:.0f}s timeout")

                    # Determine if fallback is viable
                    should_fallback = (
                        enable_fallback and
                        best_ask is not None and
                        time_remaining is not None and
                        time_remaining < 300  # Only fallback with <5 min remaining
                    )

                    if should_fallback:
                        urgency = "urgent" if time_remaining < 120 else "normal"
                        fallback_price = calculate_fallback_price(best_ask, urgency)
                        logger.info(
                            f"[LIVE] Retrying at fallback price ${fallback_price:.4f} "
                            f"(was ${price:.4f}, urgency={urgency})"
                        )

                        # Recursive call with fallback disabled to prevent infinite loop
                        fallback_result = await self.execute_single_side_trade(
                            market=market,
                            side=side,
                            price=fallback_price,
                            size=size,
                            best_ask=best_ask,
                            time_remaining=time_remaining,
                            enable_fallback=False,  # Prevent infinite recursion
                        )
                        fallback_result["used_fallback"] = True
                        fallback_result["original_price"] = price
                        return fallback_result

                    # No fallback - return failure
                    return {
                        "success": False,
                        "filled_size": 0,
                        "filled_price": 0,
                        "cost": 0,
                        "trade_id": trade_id,
                        "order_id": order_id,
                        "error": "Timeout - no fill",
                        "used_fallback": False,
                    }
            else:
                # CANCELLED (FOK couldn't fill) or other status
                if status == "CANCELLED" and self._use_fok:
                    logger.info(f"[LIVE] FOK order cancelled (insufficient liquidity at ${price:.4f})")
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

    async def sync_position(self, market: BTCMarket) -> Optional[Position]:
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
                    position = Position(market_slug=market.slug)
                    self._positions[market.slug] = position

                # Use sync_balances() to update position from chain
                position.sync_balances(up_balance, down_balance)

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
