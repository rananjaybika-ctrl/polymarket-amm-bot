"""
Fill Processor - Fill tracking, WebSocket fills, REST verification

Extracted from run_paper_bot.py to provide a clean interface for:
- WebSocket fill notifications (~100ms latency)
- REST API fill verification (backup)
- Fill event tracking and callbacks
- Pending order management
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, TYPE_CHECKING

if TYPE_CHECKING:
    from src.api.websocket_client import UserWebSocketClient, OrderFill
    from src.config import Config

logger = logging.getLogger(__name__)


@dataclass
class FillEvent:
    """
    Represents a fill notification.
    """
    side: str  # "UP" or "DOWN"
    price: float
    size: float
    order_id: Optional[str] = None
    status: str = "MATCHED"
    timestamp: Optional[datetime] = None
    source: str = "unknown"  # "websocket", "rest", "paper"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "side": self.side,
            "price": self.price,
            "size": self.size,
            "order_id": self.order_id,
            "status": self.status,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "source": self.source,
        }


@dataclass
class PendingOrder:
    """
    Tracks a pending order awaiting fill.
    """
    order_id: str
    side: str
    price: float
    size: int
    strategy: str
    placed_time: float = field(default_factory=time.time)
    chase_count: int = 0
    is_expensive_side: bool = False

    @property
    def age_seconds(self) -> float:
        """How long the order has been pending."""
        return time.time() - self.placed_time


class FillProcessor:
    """
    Processes fill notifications from WebSocket and REST API.

    Provides a unified interface for tracking fills in both paper and live modes.
    Supports instant hedging on fill detection.
    """

    def __init__(
        self,
        trading_mode: str = "paper",
        rest_verify_interval: float = 30.0,
        on_fill_callback: Optional[Callable[[FillEvent], None]] = None,
    ):
        """
        Initialize fill processor.

        Args:
            trading_mode: "paper" or "live"
            rest_verify_interval: How often to verify fills via REST (seconds)
            on_fill_callback: Callback function for fill events
        """
        self.trading_mode = trading_mode
        self.rest_verify_interval = rest_verify_interval
        self._on_fill_callback = on_fill_callback

        # WebSocket client and task
        self._user_ws: Optional["UserWebSocketClient"] = None
        self._user_ws_task: Optional[asyncio.Task] = None

        # Fill queue for async processing
        self._fill_queue: asyncio.Queue = asyncio.Queue()

        # Pending orders awaiting fill
        self._pending_orders: Dict[str, PendingOrder] = {}

        # Confirmed fills (prevent duplicates)
        self._paper_confirmed_fills: Set[str] = set()  # paper fill IDs
        self._live_confirmed_fills: Set[str] = set()  # real order IDs

        # REST verification timing
        self._last_rest_verification: float = 0.0

        # Instant hedge tracking
        self._pending_expensive_orders: Dict[str, Dict[str, Any]] = {}

    def set_fill_callback(self, callback: Callable[[FillEvent], None]) -> None:
        """Set the callback for fill events."""
        self._on_fill_callback = callback

    def add_pending_order(
        self,
        order_id: str,
        side: str,
        price: float,
        size: int,
        strategy: str,
        is_expensive_side: bool = False,
    ) -> None:
        """
        Track a pending order.

        Args:
            order_id: Order identifier
            side: "UP" or "DOWN"
            price: Order price
            size: Order size
            strategy: Strategy name
            is_expensive_side: True if this is the expensive side (for instant hedge)
        """
        self._pending_orders[order_id] = PendingOrder(
            order_id=order_id,
            side=side,
            price=price,
            size=size,
            strategy=strategy,
            is_expensive_side=is_expensive_side,
        )

        if is_expensive_side:
            # Track for instant hedge
            self._pending_expensive_orders[order_id] = {
                "side": side,
                "price": price,
                "size": size,
                "strategy": strategy,
            }

        logger.debug(f"[FILL_PROC] Added pending order: {side} {size} @ ${price:.4f}")

    def remove_pending_order(self, order_id: str) -> Optional[PendingOrder]:
        """Remove and return a pending order."""
        order = self._pending_orders.pop(order_id, None)
        self._pending_expensive_orders.pop(order_id, None)
        return order

    def get_pending_order(self, order_id: str) -> Optional[PendingOrder]:
        """Get a pending order by ID."""
        return self._pending_orders.get(order_id)

    def is_fill_confirmed(self, fill_id: str) -> bool:
        """Check if a fill has already been processed."""
        if self.trading_mode == "paper":
            return fill_id in self._paper_confirmed_fills
        else:
            return fill_id in self._live_confirmed_fills

    def mark_fill_confirmed(self, fill_id: str) -> None:
        """Mark a fill as processed."""
        if self.trading_mode == "paper":
            self._paper_confirmed_fills.add(fill_id)
        else:
            self._live_confirmed_fills.add(fill_id)

    def process_fill(
        self,
        side: str,
        price: float,
        size: float,
        order_id: Optional[str] = None,
        source: str = "unknown",
    ) -> Optional[FillEvent]:
        """
        Process a fill notification.

        Args:
            side: "UP" or "DOWN"
            price: Fill price
            size: Fill size
            order_id: Order ID (for deduplication)
            source: Fill source ("websocket", "rest", "paper")

        Returns:
            FillEvent if processed, None if duplicate
        """
        # Generate fill ID for deduplication
        if order_id:
            fill_id = order_id
        else:
            fill_id = f"{self.trading_mode}_{side}_{price:.4f}_{size}_{time.time()}"

        # Check for duplicate
        if self.is_fill_confirmed(fill_id):
            logger.debug(f"[FILL_PROC] Duplicate fill ignored: {fill_id}")
            return None

        # Mark as confirmed
        self.mark_fill_confirmed(fill_id)

        # Create fill event
        fill_event = FillEvent(
            side=side,
            price=price,
            size=size,
            order_id=order_id,
            status="MATCHED",
            timestamp=datetime.now(timezone.utc),
            source=source,
        )

        # Remove from pending
        if order_id:
            self.remove_pending_order(order_id)

        # Invoke callback
        if self._on_fill_callback:
            try:
                self._on_fill_callback(fill_event)
            except Exception as e:
                logger.error(f"[FILL_PROC] Callback error: {e}")

        logger.info(
            f"[FILL_PROC] Processed: {side} {size:.0f} @ ${price:.4f} "
            f"(source={source})"
        )

        return fill_event

    async def setup_websocket(self) -> bool:
        """
        Set up user WebSocket for instant fill notifications.

        Returns:
            True if connected successfully
        """
        if self.trading_mode != "live":
            return False

        if self._user_ws is not None:
            return self._user_ws.connected

        try:
            from src.config import Config
            from src.api.websocket_client import UserWebSocketClient, OrderFill

            config = Config()
            api_key = config.polymarket_api_key
            api_secret = config.polymarket_secret
            api_passphrase = config.polymarket_passphrase

            if not all([api_key, api_secret, api_passphrase]):
                logger.warning("[FILL_PROC] Missing API credentials, skipping WebSocket")
                return False

            # Create client
            self._user_ws = UserWebSocketClient(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )

            # Register fill callback
            def on_fill(fill: OrderFill):
                """Handle fill notification from WebSocket."""
                try:
                    # Map outcome to side
                    outcome_upper = fill.outcome.upper() if fill.outcome else ""
                    if outcome_upper in ("YES", "UP"):
                        fill_side = "UP"
                    elif outcome_upper in ("NO", "DOWN"):
                        fill_side = "DOWN"
                    else:
                        fill_side = fill.side

                    # Put in queue for async processing
                    self._fill_queue.put_nowait({
                        "side": fill_side,
                        "size": fill.size_matched,
                        "price": fill.price,
                        "status": fill.status,
                        "order_id": fill.order_id,
                        "timestamp": fill.timestamp,
                    })

                    logger.info(
                        f"[WS_FILL] {fill_side} {fill.size_matched:.0f} @ ${fill.price:.4f}"
                    )

                except Exception as e:
                    logger.error(f"[WS_FILL] Callback error: {e}")

            self._user_ws.on_fill(on_fill)

            # Connect
            connected = await self._user_ws.connect()
            if not connected:
                logger.warning("[FILL_PROC] WebSocket connection failed")
                return False

            # Start WebSocket loop in background
            self._user_ws_task = asyncio.create_task(self._user_ws.run())
            logger.info("[FILL_PROC] WebSocket started - instant fills enabled (~100ms)")

            return True

        except Exception as e:
            logger.error(f"[FILL_PROC] WebSocket setup failed: {e}")
            return False

    async def teardown_websocket(self) -> None:
        """Disconnect WebSocket."""
        if self._user_ws:
            await self._user_ws.disconnect()
            self._user_ws = None

        if self._user_ws_task:
            self._user_ws_task.cancel()
            try:
                await self._user_ws_task
            except asyncio.CancelledError:
                pass
            self._user_ws_task = None

        logger.debug("[FILL_PROC] WebSocket disconnected")

    async def process_fill_queue(self) -> List[FillEvent]:
        """
        Process all pending fills from the queue.

        Returns:
            List of processed fill events
        """
        fills = []

        while not self._fill_queue.empty():
            try:
                fill_data = self._fill_queue.get_nowait()
                fill_event = self.process_fill(
                    side=fill_data["side"],
                    price=fill_data["price"],
                    size=fill_data["size"],
                    order_id=fill_data.get("order_id"),
                    source="websocket",
                )
                if fill_event:
                    fills.append(fill_event)
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                logger.error(f"[FILL_PROC] Queue processing error: {e}")

        return fills

    async def verify_fills_via_rest(
        self,
        client: Any,  # LiveTradingEngine or similar
        strategy: Any,  # Strategy with on_fill method
    ) -> List[FillEvent]:
        """
        REST API backup for fill verification.

        Periodically checks pending orders via REST API to catch any fills
        that might have been missed by WebSocket.

        Args:
            client: Trading client with get_order method
            strategy: Strategy with on_fill method

        Returns:
            List of fills detected via REST
        """
        current_time = time.time()

        # Only check periodically
        if current_time - self._last_rest_verification < self.rest_verify_interval:
            return []

        self._last_rest_verification = current_time

        if self.trading_mode != "live":
            return []

        fills = []
        orders_to_remove = []

        for order_id, pending in self._pending_orders.items():
            if self.is_fill_confirmed(order_id):
                orders_to_remove.append(order_id)
                continue

            try:
                # Query order status via REST
                status = await client.get_order(order_id)
                if status:
                    order_status = status.get("status", "").upper()
                    if order_status in ["MATCHED", "FILLED"]:
                        fill_price = float(status.get("price", pending.price))
                        fill_size = int(float(status.get("size_matched", pending.size)))

                        if fill_size > 0:
                            logger.info(
                                f"[REST_VERIFY] Caught missed fill: {pending.side} "
                                f"{fill_size} @ ${fill_price:.4f}"
                            )

                            # Process the fill
                            fill_event = self.process_fill(
                                side=pending.side,
                                price=fill_price,
                                size=fill_size,
                                order_id=order_id,
                                source="rest",
                            )
                            if fill_event:
                                fills.append(fill_event)

                            # Notify strategy
                            if hasattr(strategy, 'on_fill'):
                                strategy.on_fill(
                                    side=pending.side,
                                    price=fill_price,
                                    size=fill_size,
                                )

                        orders_to_remove.append(order_id)

                    elif order_status == "CANCELLED":
                        orders_to_remove.append(order_id)

            except Exception as e:
                logger.warning(f"[REST_VERIFY] Error checking order {order_id[:16]}...: {e}")

        # Clean up processed orders
        for order_id in orders_to_remove:
            self.remove_pending_order(order_id)

        return fills

    def reset(self) -> None:
        """Reset state for new market/session."""
        self._pending_orders.clear()
        self._pending_expensive_orders.clear()
        # Don't clear confirmed fills - they're for deduplication

    def get_metrics(self) -> Dict[str, Any]:
        """Get fill processor metrics."""
        return {
            "pending_orders": len(self._pending_orders),
            "paper_fills_processed": len(self._paper_confirmed_fills),
            "live_fills_processed": len(self._live_confirmed_fills),
            "websocket_connected": self._user_ws.connected if self._user_ws else False,
            "trading_mode": self.trading_mode,
        }
