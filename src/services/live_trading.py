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
from typing import Optional, List, Dict, Any, Callable
import uuid

from py_clob_client.clob_types import OrderType

from src.api.polymarket_client import PolymarketClient, PolymarketClientError
from src.models.market import BTCMarket
from src.models.position import Position, Fill


logger = logging.getLogger(__name__)


# Polymarket order constraints
MIN_ORDER_SHARES = 5      # Minimum 5 shares per order
MIN_ORDER_VALUE = 1.0     # Minimum $1 order value


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


def calculate_price_depth_timeout(our_price: float, best_bid: float) -> float:
    """
    Calculate timeout based on how deep we are in the orderbook.

    Deeper in book = longer timeout (need more price movement to fill).
    Close to best bid = shorter timeout (should fill soon or give up).

    Args:
        our_price: The price we're placing our order at
        best_bid: Current best bid in the orderbook

    Returns:
        Timeout in seconds
    """
    depth = best_bid - our_price
    if depth >= 0.03:
        return 30.0  # Deep in book, be patient
    if depth >= 0.02:
        return 20.0
    if depth >= 0.01:
        return 10.0
    return 5.0  # At or above best bid, should fill quickly


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
        on_fill_callback: Optional[Callable[[dict], None]] = None,
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
            on_fill_callback: Optional callback for real-time fill notifications.
                     Called with dict containing: side, size, price, action, position_after
        """
        self.client = client
        self._starting_balance = starting_balance
        self._cached_balance: Optional[float] = None
        self._positions: Dict[str, Position] = {}
        self._realized_pnl: float = 0.0
        self._trade_count: int = 0
        self._use_fok = use_fok
        self._order_type = OrderType.FOK if use_fok else OrderType.GTC
        self._on_fill_callback = on_fill_callback

        # Pending order tracking for cancel-and-replace
        # Key: "{market_slug}_{side}" -> {"order_id": str, "price": float, "size": float, "placed_at": float}
        self._pending_orders: Dict[str, Dict[str, Any]] = {}

        # Fill rate monitoring - tracks order execution quality
        self._fill_stats: Dict[str, Dict[str, Any]] = {}  # market_slug -> stats

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

    def _notify_fill(self, side: str, size: float, price: float, market_slug: str, action: str = "BUY") -> None:
        """Notify callback of an order fill for real-time UI updates."""
        if self._on_fill_callback is None:
            return
        try:
            position = self._positions.get(market_slug)
            position_after = {"up": 0, "down": 0}
            if position:
                position_after = {"up": position.up_shares, "down": position.down_shares}

            fill_data = {
                "type": "trade_event",
                "side": side,
                "size": size,
                "price": price,
                "action": action,
                "position_after": position_after,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }
            self._on_fill_callback(fill_data)
            logger.debug(f"[LIVE] Fill notification sent: {side} {size} @ ${price:.4f}")
        except Exception as e:
            logger.warning(f"[LIVE] Failed to send fill notification: {e}")

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

    # ===== Fill Rate Monitoring =====

    def _init_fill_stats(self, market_slug: str) -> None:
        """Initialize fill stats for a market."""
        if market_slug not in self._fill_stats:
            self._fill_stats[market_slug] = {
                "orders_placed": 0,
                "orders_filled": 0,
                "orders_cancelled": 0,
                "orders_partial": 0,
                "total_size_requested": 0.0,
                "total_size_filled": 0.0,
                "target_prices": [],      # Prices we wanted
                "fill_prices": [],        # Prices we got
                "chase_orders": 0,        # Orders that were chased
                "emergency_orders": 0,    # Emergency hedge orders
            }

    def record_order_placed(self, market_slug: str, size: float, target_price: float, is_chase: bool = False, is_emergency: bool = False) -> None:
        """Record that an order was placed."""
        self._init_fill_stats(market_slug)
        stats = self._fill_stats[market_slug]
        stats["orders_placed"] += 1
        stats["total_size_requested"] += size
        stats["target_prices"].append(target_price)
        if is_chase:
            stats["chase_orders"] += 1
        if is_emergency:
            stats["emergency_orders"] += 1

    def record_order_filled(self, market_slug: str, size: float, fill_price: float, was_partial: bool = False) -> None:
        """Record that an order was filled."""
        self._init_fill_stats(market_slug)
        stats = self._fill_stats[market_slug]
        stats["orders_filled"] += 1
        stats["total_size_filled"] += size
        stats["fill_prices"].append(fill_price)
        if was_partial:
            stats["orders_partial"] += 1

    def record_order_cancelled(self, market_slug: str) -> None:
        """Record that an order was cancelled (timeout/unfilled)."""
        self._init_fill_stats(market_slug)
        stats = self._fill_stats[market_slug]
        stats["orders_cancelled"] += 1

    def get_fill_stats(self, market_slug: str) -> Dict[str, Any]:
        """Get fill stats for a market."""
        self._init_fill_stats(market_slug)
        stats = self._fill_stats[market_slug]

        # Calculate derived metrics
        fill_rate = stats["orders_filled"] / max(1, stats["orders_placed"])
        size_fill_rate = stats["total_size_filled"] / max(1.0, stats["total_size_requested"])

        # Calculate average slippage (fill price - target price)
        if stats["fill_prices"] and stats["target_prices"]:
            # Match fills to targets (simplified - assumes same order)
            slippages = []
            for i, fill_price in enumerate(stats["fill_prices"]):
                if i < len(stats["target_prices"]):
                    slippage = fill_price - stats["target_prices"][i]
                    slippages.append(slippage)
            avg_slippage = sum(slippages) / len(slippages) if slippages else 0
        else:
            avg_slippage = 0

        return {
            **stats,
            "fill_rate": fill_rate,
            "size_fill_rate": size_fill_rate,
            "avg_slippage": avg_slippage,
        }

    def log_fill_stats(self, market_slug: str) -> None:
        """Log fill rate stats for a market."""
        stats = self.get_fill_stats(market_slug)

        if stats["orders_placed"] == 0:
            return

        fill_pct = stats["fill_rate"] * 100
        cancel_pct = (stats["orders_cancelled"] / max(1, stats["orders_placed"])) * 100
        chase_pct = (stats["chase_orders"] / max(1, stats["orders_placed"])) * 100
        emergency_pct = (stats["emergency_orders"] / max(1, stats["orders_placed"])) * 100

        log_msg = (
            f"[FILL_STATS] {market_slug}: "
            f"Fill={fill_pct:.0f}%, Cancel={cancel_pct:.0f}%, "
            f"Chase={chase_pct:.0f}%, Emergency={emergency_pct:.0f}%, "
            f"Slippage=${stats['avg_slippage']:.4f}"
        )

        # Alert if fill rate is too low
        if stats["fill_rate"] < 0.5:
            logger.warning(f"{log_msg} [LOW_FILL_RATE]")
        else:
            logger.info(log_msg)

    def clear_fill_stats(self, market_slug: str) -> None:
        """Clear fill stats for a market (call on market rotation)."""
        if market_slug in self._fill_stats:
            del self._fill_stats[market_slug]

    # ===== End Fill Rate Monitoring =====

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

        # Enforce Polymarket order constraints: min 5 shares AND min $1 value
        order_value = size * price
        if size < MIN_ORDER_SHARES:
            logger.warning(f"[LIVE] Order rejected: {size} shares < min {MIN_ORDER_SHARES}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "error": f"Size {size} < min {MIN_ORDER_SHARES} shares",
            }
        if order_value < MIN_ORDER_VALUE:
            logger.warning(f"[LIVE] Order rejected: ${order_value:.2f} value < min ${MIN_ORDER_VALUE:.2f}")
            return {
                "success": False,
                "filled_size": 0,
                "filled_price": 0,
                "cost": 0,
                "trade_id": trade_id,
                "error": f"Value ${order_value:.2f} < min ${MIN_ORDER_VALUE:.2f}",
            }

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
                self._notify_fill(side_upper, filled_size, filled_price, market.slug)

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
                # Use time-based timeout only
                timeout = calculate_dynamic_timeout(
                    time_remaining_secs=time_remaining if time_remaining else 600,
                    is_emergency=False,
                )
                logger.info(f"[LIVE] GTC order LIVE, polling (timeout={timeout:.0f}s)")
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
                    self._notify_fill(side_upper, filled_size, filled_price, market.slug)
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

    async def cancel_and_replace(
        self,
        market: BTCMarket,
        side: str,
        new_price: float,
        new_size: float,
        price_tolerance: float = 0.005,
        stale_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Cancel existing order and place new one if price changed significantly.

        For patient MAKER strategies (like Calculus Maker), this enables
        dynamic price adjustment without waiting for full timeout.

        Args:
            market: The BTCMarket to trade
            side: "UP" or "DOWN"
            new_price: Target price for new order
            new_size: Target size for new order
            price_tolerance: Only replace if price differs by more than this (default 0.5%)
            stale_seconds: Only replace if order older than this (default 10s)

        Returns:
            Dict with:
                - action: "placed" | "replaced" | "kept" | "error"
                - order_id: Current order ID
                - price: Current order price
                - cancelled_order_id: ID of cancelled order (if replaced)
                - filled_before_cancel: Size filled before cancellation
        """
        side_upper = side.upper()
        key = f"{market.slug}_{side_upper}"

        # Check for existing pending order
        pending = self._pending_orders.get(key)
        if pending:
            order_id = pending["order_id"]
            old_price = pending["price"]
            placed_at = pending["placed_at"]
            elapsed = asyncio.get_event_loop().time() - placed_at

            # Check if order is still live
            try:
                status = await self.client.get_order(order_id)
                if status:
                    order_status = status.get("status", "").upper()
                    filled_size = float(status.get("size_matched", 0))

                    if order_status in ["MATCHED", "FILLED"]:
                        # Order filled! Update position and clear pending
                        logger.info(f"[LIVE] Pending order FILLED: {filled_size} {side_upper} @ ${old_price:.4f}")
                        del self._pending_orders[key]

                        # Update position
                        position = self._positions.get(market.slug)
                        if position is None:
                            position = Position(market_slug=market.slug)
                            self._positions[market.slug] = position
                        token_id = market.up_token_id if side_upper == "UP" else market.down_token_id
                        if side_upper == "UP":
                            position.up_token_id = token_id
                        else:
                            position.down_token_id = token_id
                        position.add_fill(side_upper, old_price, filled_size, filled_size * old_price)
                        self._trade_count += 1
                        self._notify_fill(side_upper, filled_size, old_price, market.slug)
                        await self.sync_balance()

                        return {
                            "action": "filled",
                            "order_id": order_id,
                            "price": old_price,
                            "filled_size": filled_size,
                        }

                    elif order_status == "CANCELLED":
                        # Order was cancelled externally
                        logger.info(f"[LIVE] Pending order was cancelled externally")
                        del self._pending_orders[key]
                        # Fall through to place new order

                    elif order_status == "LIVE":
                        # Order still live - check if we should replace
                        price_diff = abs(new_price - old_price)
                        should_replace = (
                            price_diff > price_tolerance and
                            elapsed >= stale_seconds
                        )

                        if not should_replace:
                            # Keep existing order
                            reason = "price within tolerance" if price_diff <= price_tolerance else f"order too new ({elapsed:.1f}s < {stale_seconds}s)"
                            logger.debug(f"[LIVE] Keeping order @ ${old_price:.4f}: {reason}")
                            return {
                                "action": "kept",
                                "order_id": order_id,
                                "price": old_price,
                                "reason": reason,
                            }

                        # Cancel and replace
                        logger.info(
                            f"[LIVE] Cancel-and-replace: ${old_price:.4f} -> ${new_price:.4f} "
                            f"(diff=${price_diff:.4f}, age={elapsed:.1f}s)"
                        )
                        try:
                            await self.client.cancel_order(order_id)
                            logger.info(f"[LIVE] Cancelled order {order_id[:16]}...")

                            # Check if any was filled before cancel
                            final_status = await self.client.get_order(order_id)
                            filled_before = float(final_status.get("size_matched", 0)) if final_status else 0
                            if filled_before > 0:
                                # Partial fill happened - update position
                                position = self._positions.get(market.slug)
                                if position is None:
                                    position = Position(market_slug=market.slug)
                                    self._positions[market.slug] = position
                                token_id = market.up_token_id if side_upper == "UP" else market.down_token_id
                                if side_upper == "UP":
                                    position.up_token_id = token_id
                                else:
                                    position.down_token_id = token_id
                                position.add_fill(side_upper, old_price, filled_before, filled_before * old_price)
                                self._trade_count += 1
                                self._notify_fill(side_upper, filled_before, old_price, market.slug)
                                logger.info(f"[LIVE] Partial fill captured: {filled_before} @ ${old_price:.4f}")

                            del self._pending_orders[key]

                        except Exception as e:
                            logger.warning(f"[LIVE] Failed to cancel order: {e}")
                            return {
                                "action": "error",
                                "error": f"Cancel failed: {e}",
                                "order_id": order_id,
                            }

            except Exception as e:
                logger.warning(f"[LIVE] Failed to check order status: {e}")
                # Assume order is stale, remove tracking
                del self._pending_orders[key]

        # Place new order
        token_id = market.up_token_id if side_upper == "UP" else market.down_token_id
        trade_id = f"LIVE-{uuid.uuid4().hex[:8]}"

        try:
            result = await self.client.place_order(
                token_id=token_id,
                side="BUY",
                price=new_price,
                size=new_size,
                order_type=OrderType.GTC,  # Always GTC for cancel-and-replace
            )

            new_order_id = result.get("orderID") or result.get("order_id", trade_id)
            status = result.get("status", "unknown").upper()

            if status in ["MATCHED", "FILLED"]:
                # Immediate fill
                logger.info(f"[LIVE] Order immediately filled: {new_size} {side_upper} @ ${new_price:.4f}")

                position = self._positions.get(market.slug)
                if position is None:
                    position = Position(market_slug=market.slug)
                    self._positions[market.slug] = position
                if side_upper == "UP":
                    position.up_token_id = token_id
                else:
                    position.down_token_id = token_id
                position.add_fill(side_upper, new_price, new_size, new_size * new_price)
                self._trade_count += 1
                self._notify_fill(side_upper, new_size, new_price, market.slug)
                await self.sync_balance()

                return {
                    "action": "filled",
                    "order_id": new_order_id,
                    "price": new_price,
                    "filled_size": new_size,
                }

            elif status == "LIVE":
                # Order in book - track it
                self._pending_orders[key] = {
                    "order_id": new_order_id,
                    "price": new_price,
                    "size": new_size,
                    "placed_at": asyncio.get_event_loop().time(),
                }
                action = "replaced" if pending else "placed"
                logger.info(f"[LIVE] Order {action}: {new_size} {side_upper} @ ${new_price:.4f}")

                return {
                    "action": action,
                    "order_id": new_order_id,
                    "price": new_price,
                    "cancelled_order_id": pending["order_id"] if pending else None,
                }

            else:
                logger.warning(f"[LIVE] Unexpected order status: {status}")
                return {
                    "action": "error",
                    "error": f"Order status: {status}",
                }

        except Exception as e:
            logger.error(f"[LIVE] Failed to place order: {e}")
            return {
                "action": "error",
                "error": str(e),
            }

    def get_pending_orders(self) -> Dict[str, Dict[str, Any]]:
        """Get all pending orders being tracked."""
        return self._pending_orders.copy()

    async def cancel_pending_order(self, key: str) -> bool:
        """
        Cancel a specific pending order by key.

        Used by emergency hedge system to cancel chased orders before
        placing emergency orders (prevents double fills).

        Args:
            key: Order key in format "{market_slug}_{side}" (e.g. "1767556800_UP")

        Returns:
            True if order was found and cancelled, False otherwise
        """
        pending = self._pending_orders.get(key)
        if not pending:
            logger.debug(f"[LIVE] No pending order found for key: {key}")
            return False

        order_id = pending["order_id"]
        old_price = pending["price"]

        try:
            # Check current status before cancelling
            status = await self.client.get_order(order_id)
            if status:
                order_status = status.get("status", "").upper()
                filled_size = float(status.get("size_matched", 0))

                if order_status in ["MATCHED", "FILLED"]:
                    # Already filled - update position and clean up tracking
                    logger.info(f"[LIVE] Pending order already filled: {filled_size} @ ${old_price:.4f}")
                    del self._pending_orders[key]
                    # Note: Position update happens in the calling code or next sync
                    return False  # Wasn't cancelled, was already filled

                elif order_status == "CANCELLED":
                    # Already cancelled
                    logger.info(f"[LIVE] Pending order was already cancelled")
                    del self._pending_orders[key]
                    return True

            # Cancel the order
            await self.client.cancel_order(order_id)
            logger.info(f"[LIVE] Cancelled pending order: {key} @ ${old_price:.4f}")

            # Check for partial fill after cancellation
            final_status = await self.client.get_order(order_id)
            filled_before = float(final_status.get("size_matched", 0)) if final_status else 0
            if filled_before > 0:
                logger.info(f"[LIVE] Partial fill captured before cancel: {filled_before} @ ${old_price:.4f}")

            # Remove from tracking
            del self._pending_orders[key]
            return True

        except Exception as e:
            logger.warning(f"[LIVE] Failed to cancel pending order {key}: {e}")
            # Remove from tracking anyway to avoid stale entries
            if key in self._pending_orders:
                del self._pending_orders[key]
            return False

    async def check_and_pull_stale_quotes(
        self,
        market: BTCMarket,
        trend_detector: 'TrendDetector',
        max_age_secs: float = 10.0,
        velocity_threshold_bps: float = 15.0,
    ) -> Dict[str, bool]:
        """
        Cancel pending orders when Binance moves against them (quote pulling).

        Implements professional MM behavior from Telegram alpha:
        - "Cancel all unfilled orders after 5-20 seconds"
        - "You get rolled over if you're not quick enough to pull quotes when Binance moves"

        This is the key latency advantage:
        - With 70ms latency, you can see Binance spike UP and cancel your DOWN bid
          before a taker fills it (180ms head start vs 250ms latency)

        Args:
            market: Current market being traded
            trend_detector: TrendDetector instance with Binance feed
            max_age_secs: Maximum order age before considering stale (default 10s)
            velocity_threshold_bps: Price velocity to trigger pull (default 15 bps/sec)

        Returns:
            Dict mapping side ("UP"/"DOWN") to whether quote was pulled
        """
        pulled = {"UP": False, "DOWN": False}
        current_time = asyncio.get_event_loop().time()

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
                    f"[QUOTE_PULL] Cancelling {side} order: "
                    f"reason={reason}, age={order_age:.1f}s, "
                    f"price=${pending['price']:.4f}"
                )

                try:
                    # Check for partial fill first
                    status = await self.client.get_order(pending["order_id"])
                    if status:
                        filled_size = float(status.get("size_matched", 0))
                        order_status = status.get("status", "").upper()

                        if order_status in ["MATCHED", "FILLED"]:
                            # Already filled - good, remove from tracking
                            logger.info(f"[QUOTE_PULL] Order already filled: {filled_size}")
                            del self._pending_orders[key]
                            continue

                        if filled_size > 0:
                            # Partial fill - update position before cancelling
                            position = self._positions.get(market.slug)
                            if position is None:
                                position = Position(market_slug=market.slug)
                                self._positions[market.slug] = position

                            token_id = market.up_token_id if side == "UP" else market.down_token_id
                            if side == "UP":
                                position.up_token_id = token_id
                            else:
                                position.down_token_id = token_id

                            position.add_fill(side, pending["price"], filled_size, filled_size * pending["price"])
                            self._trade_count += 1
                            self._notify_fill(side, filled_size, pending["price"], market.slug)
                            logger.info(f"[QUOTE_PULL] Captured partial fill: {filled_size} @ ${pending['price']:.4f}")

                    # Cancel the order
                    await self.client.cancel_order(pending["order_id"])
                    del self._pending_orders[key]
                    pulled[side] = True

                    # Log reason for transparency
                    if should_pull_trend and not is_stale:
                        logger.info(
                            f"[QUOTE_PULL] Pulled {side} due to Binance movement "
                            f"(latency advantage - prevented adverse fill)"
                        )

                except Exception as e:
                    logger.warning(f"[QUOTE_PULL] Failed to cancel {side}: {e}")
                    # Remove from tracking anyway to avoid stale entries
                    if key in self._pending_orders:
                        del self._pending_orders[key]

        return pulled

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

        for key, pending in self._pending_orders.items():
            if market_slug and not key.startswith(market_slug):
                continue

            try:
                await self.client.cancel_order(pending["order_id"])
                logger.info(f"[LIVE] Cancelled pending order: {key}")
                keys_to_remove.append(key)
                cancelled += 1
            except Exception as e:
                logger.warning(f"[LIVE] Failed to cancel {key}: {e}")

        for key in keys_to_remove:
            del self._pending_orders[key]

        return cancelled

    async def event_driven_pull(self, direction: str, market_slug: str, z_score: float) -> bool:
        """
        Immediately cancel pending order on opposite side of trend direction.

        Called from BinanceClient WebSocket callback when z-score crosses threshold.
        This is the fastest reaction path - ~100-200ms from Binance price move.

        Args:
            direction: "UP" or "DOWN" - which way BTC is trending
            market_slug: Current market being traded
            z_score: Current z-score that triggered the alert

        Returns:
            True if an order was cancelled, False otherwise

        Example:
            # In BinanceClient callback:
            def on_z_alert(z, direction, state):
                asyncio.create_task(engine.event_driven_pull(direction, market_slug, z))
        """
        # Cancel order on OPPOSITE side of trend
        # If BTC trending UP, DOWN shares are losing -> cancel DOWN orders
        side_to_pull = "DOWN" if direction == "UP" else "UP"
        key = f"{market_slug}_{side_to_pull}"

        pending = self._pending_orders.get(key)
        if not pending:
            logger.debug(f"[EVENT_PULL] No pending {side_to_pull} order to pull")
            return False

        order_id = pending["order_id"]
        price = pending["price"]

        try:
            # Cancel immediately - no status check, just cancel
            await self.client.cancel_order(order_id)
            del self._pending_orders[key]

            logger.warning(
                f"[EVENT_PULL] Cancelled {side_to_pull} @ ${price:.4f} | "
                f"z={z_score:.2f}, dir={direction} | ~100ms reaction"
            )
            return True

        except Exception as e:
            logger.error(f"[EVENT_PULL] Failed to cancel {side_to_pull}: {e}")
            # Clean up tracking
            if key in self._pending_orders:
                del self._pending_orders[key]
            return False

    def resolve_market(self, market_slug: str, winner: str) -> float:
        """
        Resolve a market and calculate realized P&L.

        Note: Actual redemption of winning tokens to USDC happens automatically
        on Polymarket after 1-5 minutes for 15-minute markets. This method
        calculates P&L based on the resolution outcome.

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
