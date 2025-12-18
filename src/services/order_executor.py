"""
Order Executor service for pair trading on Polymarket.

Handles simultaneous execution of Up/Down token orders to prevent
legging risk (one side filling while the other doesn't).
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict
from enum import Enum

from py_clob_client.clob_types import OrderType

from src.api.polymarket_client import PolymarketClient, PolymarketClientError
from src.models.market import BTCMarket
from src.services.pair_analyzer import PairOpportunity


logger = logging.getLogger(__name__)


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(Enum):
    """Status of order execution."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class OrderInfo:
    """Information about a single order."""
    token_id: str
    side: str
    price: float
    size: float
    order_id: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    filled_size: float = 0.0
    filled_price: float = 0.0
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_filled(self) -> bool:
        """Check if order is completely filled."""
        return self.status == ExecutionStatus.FILLED

    @property
    def is_partial(self) -> bool:
        """Check if order is partially filled."""
        return self.status == ExecutionStatus.PARTIAL

    @property
    def remaining_size(self) -> float:
        """Size remaining to fill."""
        return self.size - self.filled_size

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "token_id": self.token_id,
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "order_id": self.order_id,
            "status": self.status.value,
            "filled_size": self.filled_size,
            "filled_price": self.filled_price,
            "error": self.error,
        }


@dataclass
class PairExecutionResult:
    """Result of a pair trade execution."""
    market: BTCMarket
    up_order: OrderInfo
    down_order: OrderInfo
    expected_cost: float
    actual_cost: float = 0.0
    success: bool = False
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def both_filled(self) -> bool:
        """Check if both orders filled."""
        return self.up_order.is_filled and self.down_order.is_filled

    @property
    def any_filled(self) -> bool:
        """Check if either order filled."""
        return self.up_order.is_filled or self.down_order.is_filled

    @property
    def profit_per_pair(self) -> float:
        """Profit per pair if both filled."""
        if self.actual_cost <= 0:
            return 0.0
        return 1.0 - self.actual_cost

    @property
    def total_profit(self) -> float:
        """Total profit if both filled."""
        if not self.both_filled:
            return 0.0
        size = min(self.up_order.filled_size, self.down_order.filled_size)
        return size * self.profit_per_pair

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "market": self.market.slug,
            "up_order": self.up_order.to_dict(),
            "down_order": self.down_order.to_dict(),
            "expected_cost": self.expected_cost,
            "actual_cost": self.actual_cost,
            "success": self.success,
            "profit_per_pair": self.profit_per_pair,
            "total_profit": self.total_profit,
            "error": self.error,
        }


class OrderExecutor:
    """
    Executes pair trades on Polymarket markets.

    Handles simultaneous Up/Down token purchases with legging protection.

    Example:
        executor = OrderExecutor(client)

        # Execute from opportunity
        result = await executor.execute_opportunity(opportunity, size=10)

        # Or execute directly
        result = await executor.execute_pair_buy(
            market, size=10, up_price=0.51, down_price=0.51
        )
    """

    def __init__(
        self,
        client: PolymarketClient,
        default_order_type: OrderType = OrderType.GTC,
    ):
        """
        Initialize OrderExecutor.

        Args:
            client: Connected PolymarketClient
            default_order_type: Default order type (GTC, FOK, FAK)
        """
        self.client = client
        self.default_order_type = default_order_type

    async def execute_opportunity(
        self,
        opportunity: PairOpportunity,
        size: Optional[float] = None,
        order_type: Optional[OrderType] = None,
        dry_run: bool = False,
    ) -> PairExecutionResult:
        """
        Execute a pair trade from a PairOpportunity.

        Args:
            opportunity: PairOpportunity from PairAnalyzer
            size: Number of pairs (defaults to executable_size)
            order_type: Order type (defaults to self.default_order_type)
            dry_run: If True, create orders but don't submit

        Returns:
            PairExecutionResult with execution details
        """
        if not opportunity.is_profitable:
            logger.warning(
                f"Opportunity not profitable: pair_cost=${opportunity.pair_cost:.4f}"
            )

        # Use executable size if not specified
        if size is None:
            size = opportunity.executable_size

        if size <= 0:
            return PairExecutionResult(
                market=opportunity.market,
                up_order=OrderInfo(
                    token_id=opportunity.market.up_token_id,
                    side="BUY",
                    price=opportunity.up_ask or 0,
                    size=0,
                    status=ExecutionStatus.FAILED,
                    error="No executable size",
                ),
                down_order=OrderInfo(
                    token_id=opportunity.market.down_token_id,
                    side="BUY",
                    price=opportunity.down_ask or 0,
                    size=0,
                    status=ExecutionStatus.FAILED,
                    error="No executable size",
                ),
                expected_cost=opportunity.pair_cost or 0,
                success=False,
                error="No executable size available",
            )

        return await self.execute_pair_buy(
            market=opportunity.market,
            size=size,
            up_price=opportunity.up_ask or 0,
            down_price=opportunity.down_ask or 0,
            order_type=order_type,
            dry_run=dry_run,
        )

    async def execute_pair_buy(
        self,
        market: BTCMarket,
        size: float,
        up_price: float,
        down_price: float,
        order_type: Optional[OrderType] = None,
        dry_run: bool = False,
    ) -> PairExecutionResult:
        """
        Execute a pair buy (purchase both Up and Down tokens).

        Args:
            market: BTCMarket to trade
            size: Number of pairs to buy
            up_price: Price per Up token
            down_price: Price per Down token
            order_type: Order type (defaults to self.default_order_type)
            dry_run: If True, create orders but don't submit

        Returns:
            PairExecutionResult with execution details
        """
        order_type = order_type or self.default_order_type
        expected_cost = up_price + down_price

        logger.info(
            f"Executing pair buy: {market.slug}, size={size}, "
            f"up=${up_price:.4f}, down=${down_price:.4f}, "
            f"cost=${expected_cost:.4f}, dry_run={dry_run}"
        )

        # Initialize order info
        up_order = OrderInfo(
            token_id=market.up_token_id,
            side="BUY",
            price=up_price,
            size=size,
        )
        down_order = OrderInfo(
            token_id=market.down_token_id,
            side="BUY",
            price=down_price,
            size=size,
        )

        result = PairExecutionResult(
            market=market,
            up_order=up_order,
            down_order=down_order,
            expected_cost=expected_cost,
        )

        try:
            # Create signed orders
            logger.debug("Creating signed orders...")
            up_signed = self.client.create_order(
                token_id=market.up_token_id,
                side="BUY",
                price=up_price,
                size=size,
            )
            down_signed = self.client.create_order(
                token_id=market.down_token_id,
                side="BUY",
                price=down_price,
                size=size,
            )

            if dry_run:
                logger.info("Dry run - orders created but not submitted")
                up_order.status = ExecutionStatus.PENDING
                down_order.status = ExecutionStatus.PENDING
                result.success = True
                return result

            # Submit both orders atomically via batch API
            logger.debug("Submitting orders via batch API...")
            responses = await self.client.place_orders(
                orders=[up_signed, down_signed],
                order_type=order_type,
            )

            # Parse responses
            await self._parse_order_responses(responses, up_order, down_order)

            # Calculate actual cost
            if up_order.is_filled and down_order.is_filled:
                result.actual_cost = up_order.filled_price + down_order.filled_price
                result.success = True
                logger.info(
                    f"Pair execution successful: {size} pairs at ${result.actual_cost:.4f}"
                )
            elif up_order.is_filled or down_order.is_filled:
                # Partial execution - one side filled
                logger.warning("Partial execution detected - recovery needed")
                result.error = "Partial execution - one side filled"
            else:
                result.error = "Both orders failed to fill"

        except PolymarketClientError as e:
            logger.error(f"Order execution failed: {e}")
            up_order.status = ExecutionStatus.FAILED
            up_order.error = str(e)
            down_order.status = ExecutionStatus.FAILED
            down_order.error = str(e)
            result.error = str(e)

        except Exception as e:
            logger.error(f"Unexpected error in pair execution: {e}")
            up_order.status = ExecutionStatus.FAILED
            up_order.error = str(e)
            down_order.status = ExecutionStatus.FAILED
            down_order.error = str(e)
            result.error = f"Unexpected error: {e}"

        return result

    async def _parse_order_responses(
        self,
        responses: List[Dict[str, Any]],
        up_order: OrderInfo,
        down_order: OrderInfo,
    ) -> None:
        """Parse order responses and update OrderInfo objects."""
        if not responses or len(responses) < 2:
            logger.error(f"Invalid response count: {len(responses) if responses else 0}")
            return

        # Map responses to orders by token_id
        for resp in responses:
            order_id = resp.get("orderID") or resp.get("order_id")
            status = resp.get("status", "").upper()
            token_id = resp.get("asset_id") or resp.get("tokenID")

            # Determine which order this response belongs to
            if token_id == up_order.token_id:
                order_info = up_order
            elif token_id == down_order.token_id:
                order_info = down_order
            else:
                # Try to match by order index if token_id not in response
                idx = responses.index(resp)
                order_info = up_order if idx == 0 else down_order

            order_info.order_id = order_id

            if status in ("MATCHED", "FILLED"):
                order_info.status = ExecutionStatus.FILLED
                order_info.filled_size = order_info.size
                order_info.filled_price = order_info.price
            elif status == "LIVE":
                order_info.status = ExecutionStatus.SUBMITTED
            elif status == "CANCELLED":
                order_info.status = ExecutionStatus.CANCELLED
            else:
                order_info.status = ExecutionStatus.SUBMITTED

            logger.debug(f"Order {order_id}: status={status}, token={token_id}")

    async def cancel_market_orders(self, market: BTCMarket) -> int:
        """
        Cancel all open orders for a market.

        Args:
            market: BTCMarket to cancel orders for

        Returns:
            Number of orders cancelled
        """
        try:
            orders = await self.client.get_open_orders(market=market.condition_id)

            if not orders:
                logger.info(f"No open orders for {market.slug}")
                return 0

            order_ids = [o.get("orderID") or o.get("order_id") for o in orders if o]
            order_ids = [oid for oid in order_ids if oid]

            if order_ids:
                await self.client.cancel_orders(order_ids)
                logger.info(f"Cancelled {len(order_ids)} orders for {market.slug}")

            return len(order_ids)

        except Exception as e:
            logger.error(f"Failed to cancel orders for {market.slug}: {e}")
            return 0

    async def get_order_status(self, order_id: str) -> Optional[OrderInfo]:
        """
        Get current status of an order.

        Args:
            order_id: The order ID to check

        Returns:
            OrderInfo with current status, or None if not found
        """
        try:
            resp = await self.client.get_order(order_id)

            if not resp:
                return None

            status_str = resp.get("status", "").upper()
            status = ExecutionStatus.SUBMITTED

            if status_str in ("MATCHED", "FILLED"):
                status = ExecutionStatus.FILLED
            elif status_str == "CANCELLED":
                status = ExecutionStatus.CANCELLED
            elif status_str == "LIVE":
                status = ExecutionStatus.SUBMITTED

            return OrderInfo(
                token_id=resp.get("asset_id", ""),
                side=resp.get("side", ""),
                price=float(resp.get("price", 0)),
                size=float(resp.get("original_size", 0)),
                order_id=order_id,
                status=status,
                filled_size=float(resp.get("size_matched", 0)),
                filled_price=float(resp.get("price", 0)),
            )

        except Exception as e:
            logger.error(f"Failed to get order status: {e}")
            return None

    def __repr__(self) -> str:
        """String representation."""
        return f"OrderExecutor(order_type={self.default_order_type.value})"
