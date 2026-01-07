"""
WebSocket client for Polymarket real-time data streaming.

Provides real-time orderbook updates and price changes via
Polymarket's CLOB WebSocket API.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Callable, Dict, Any, Set
from enum import Enum

import websockets
from websockets.exceptions import ConnectionClosed

from src.models.orderbook import Order, Orderbook


logger = logging.getLogger(__name__)


# WebSocket endpoints
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


class MessageType(Enum):
    """WebSocket message types."""
    BOOK = "book"
    PRICE_CHANGE = "price_change"
    LAST_TRADE_PRICE = "last_trade_price"
    MARKET_RESOLVED = "market_resolved"  # Requires custom_feature_enabled flag


@dataclass
class BookUpdate:
    """Order book update message."""
    token_id: str
    bids: List[Order] = field(default_factory=list)
    asks: List[Order] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    hash: str = ""

    @classmethod
    def from_message(cls, data: dict) -> "BookUpdate":
        """Create from WebSocket message."""
        token_id = data.get("asset_id", "")

        bids = []
        asks = []

        # Bids and asks are at top level, not nested in "market"
        for bid in data.get("bids", []):
            bids.append(Order(
                price=float(bid.get("price", 0)),
                size=float(bid.get("size", 0)),
            ))
        for ask in data.get("asks", []):
            asks.append(Order(
                price=float(ask.get("price", 0)),
                size=float(ask.get("size", 0)),
            ))

        return cls(
            token_id=token_id,
            bids=sorted(bids, key=lambda o: -o.price),  # Highest first
            asks=sorted(asks, key=lambda o: o.price),   # Lowest first
            hash=data.get("hash", ""),
        )

    @property
    def best_bid(self) -> Optional[float]:
        """Best bid price."""
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        """Best ask price."""
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        """Bid-ask spread."""
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None


@dataclass
class PriceChange:
    """Price change notification."""
    token_id: str
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_message(cls, data: dict) -> "PriceChange":
        """Create from WebSocket message."""
        changes = data.get("changes", [])

        if not changes:
            return cls(token_id=data.get("asset_id", ""))

        change = changes[0]  # Usually one change per message
        return cls(
            token_id=change.get("asset_id", ""),
            best_bid=float(change["price"]) if change.get("side") == "BUY" else None,
            best_ask=float(change["price"]) if change.get("side") == "SELL" else None,
        )


@dataclass
class TradeUpdate:
    """Trade execution notification."""
    token_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_message(cls, data: dict) -> "TradeUpdate":
        """Create from WebSocket message."""
        return cls(
            token_id=data.get("asset_id", ""),
            price=float(data.get("price", 0)),
            size=float(data.get("size", 0)),
            side=data.get("side", ""),
        )


@dataclass
class MarketResolved:
    """
    Market resolution notification.

    Emitted when a market is resolved on Polymarket.
    Requires custom_feature_enabled flag to receive.
    """
    market_id: str
    question: str
    condition_id: str
    slug: str
    description: str
    assets_ids: List[str]
    outcomes: List[str]
    winning_asset_id: str
    winning_outcome: str
    event_id: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_message(cls, data: dict) -> "MarketResolved":
        """Create from WebSocket message."""
        event_msg = data.get("event_message", {}) or {}
        ts_str = data.get("timestamp")
        timestamp = datetime.now(timezone.utc)
        if ts_str:
            try:
                timestamp = datetime.fromtimestamp(int(ts_str) / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        return cls(
            market_id=data.get("id", ""),
            question=data.get("question", ""),
            condition_id=data.get("market", ""),
            slug=data.get("slug", ""),
            description=data.get("description", ""),
            assets_ids=data.get("assets_ids", []),
            outcomes=data.get("outcomes", []),
            winning_asset_id=data.get("winning_asset_id", ""),
            winning_outcome=data.get("winning_outcome", ""),
            event_id=event_msg.get("id"),
            timestamp=timestamp,
        )

    def is_btc_15min_market(self) -> bool:
        """Check if this is a BTC 15-minute Up/Down market."""
        return "btc-updown-15m" in self.slug.lower()


# Callback type aliases
BookCallback = Callable[[BookUpdate], None]
PriceCallback = Callable[[PriceChange], None]
TradeCallback = Callable[[TradeUpdate], None]
MarketResolvedCallback = Callable[[MarketResolved], None]


class WebSocketClient:
    """
    WebSocket client for Polymarket real-time data.

    Provides streaming orderbook updates with auto-reconnect.
    Supports market_resolved events for instant resolution notifications.

    Example:
        client = WebSocketClient(custom_features=True)

        # Register callbacks
        client.on_book_update(lambda update: print(update))
        client.on_market_resolved(lambda m: print(f"Resolved: {m.winning_outcome}"))

        # Connect and subscribe
        await client.connect()
        await client.subscribe([up_token_id, down_token_id])

        # Run event loop
        await client.run()
    """

    DEFAULT_RECONNECT_DELAY = 1.0
    MAX_RECONNECT_DELAY = 60.0

    def __init__(
        self,
        url: str = WS_MARKET_URL,
        auto_reconnect: bool = True,
        custom_features: bool = False,
    ):
        """
        Initialize WebSocket client.

        Args:
            url: WebSocket endpoint URL
            auto_reconnect: Whether to auto-reconnect on disconnect
            custom_features: Enable market_resolved events (for instant resolution notifications)
        """
        self.url = url
        self.auto_reconnect = auto_reconnect
        self.custom_features = custom_features

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._running = False
        self._subscribed_tokens: Set[str] = set()

        # Callbacks
        self._book_callbacks: List[BookCallback] = []
        self._price_callbacks: List[PriceCallback] = []
        self._trade_callbacks: List[TradeCallback] = []
        self._market_resolved_callbacks: List[MarketResolvedCallback] = []

        # Reconnect state
        self._reconnect_delay = self.DEFAULT_RECONNECT_DELAY
        self._reconnect_task: Optional[asyncio.Task] = None

    @property
    def connected(self) -> bool:
        """Whether WebSocket is connected."""
        return self._connected and self._ws is not None

    def on_book_update(self, callback: BookCallback) -> None:
        """Register callback for book updates."""
        self._book_callbacks.append(callback)

    def on_price_change(self, callback: PriceCallback) -> None:
        """Register callback for price changes."""
        self._price_callbacks.append(callback)

    def on_trade(self, callback: TradeCallback) -> None:
        """Register callback for trade updates."""
        self._trade_callbacks.append(callback)

    def on_market_resolved(self, callback: MarketResolvedCallback) -> None:
        """
        Register callback for market resolution notifications.

        Requires custom_features=True to receive events.
        Use this to trigger position redemption when a market resolves.
        """
        self._market_resolved_callbacks.append(callback)

    async def connect(self) -> bool:
        """
        Connect to WebSocket server.

        Returns:
            True if connected successfully
        """
        try:
            logger.info(f"Connecting to {self.url}")
            self._ws = await websockets.connect(
                self.url,
                ping_interval=10,
                ping_timeout=10,
                open_timeout=10,
                close_timeout=10,
            )
            self._connected = True
            self._reconnect_delay = self.DEFAULT_RECONNECT_DELAY

            logger.info("WebSocket connected")
            return True

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server."""
        self._running = False
        self._connected = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("WebSocket disconnected")

    async def subscribe(self, token_ids: List[str]) -> bool:
        """
        Subscribe to market updates for tokens.

        Args:
            token_ids: List of token IDs to subscribe to

        Returns:
            True if subscription sent successfully
        """
        if not self.connected:
            logger.warning("Cannot subscribe: not connected")
            return False

        # Build subscription message with custom features flag if enabled
        message: Dict[str, Any] = {
            "assets_ids": token_ids,
        }

        if self.custom_features:
            message["custom_feature_enabled"] = True
        else:
            message["type"] = "market"

        try:
            await self._ws.send(json.dumps(message))
            self._subscribed_tokens.update(token_ids)

            logger.info(f"Subscribed to {len(token_ids)} tokens (custom_features={self.custom_features})")
            return True

        except Exception as e:
            logger.error(f"Subscription failed: {e}")
            return False

    async def unsubscribe(self, token_ids: List[str]) -> bool:
        """
        Unsubscribe from market updates.

        Args:
            token_ids: Token IDs to unsubscribe from

        Returns:
            True if unsubscription sent
        """
        if not self.connected:
            return False

        # Remove from tracked set
        for tid in token_ids:
            self._subscribed_tokens.discard(tid)

        # Re-subscribe to remaining (Polymarket replaces subscription)
        if self._subscribed_tokens:
            return await self.subscribe(list(self._subscribed_tokens))

        return True

    async def run(self) -> None:
        """
        Run the WebSocket event loop.

        Processes incoming messages until disconnect.
        """
        self._running = True

        while self._running:
            try:
                if not self.connected:
                    if self.auto_reconnect:
                        await self._reconnect()
                    else:
                        break

                # Receive message
                message = await self._ws.recv()
                await self._handle_message(message)

            except ConnectionClosed as e:
                logger.warning(f"WebSocket closed: {e}")
                self._connected = False

                if self.auto_reconnect and self._running:
                    await self._reconnect()
                else:
                    break

            except Exception as e:
                logger.error(f"Error in WebSocket loop: {e}")

                if not self._running:
                    break

    async def _reconnect(self) -> None:
        """Handle reconnection with exponential backoff."""
        logger.info(f"Reconnecting in {self._reconnect_delay:.1f}s...")
        await asyncio.sleep(self._reconnect_delay)

        # Exponential backoff
        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self.MAX_RECONNECT_DELAY,
        )

        if await self.connect():
            # Re-subscribe to previous tokens
            if self._subscribed_tokens:
                await self.subscribe(list(self._subscribed_tokens))

    async def _handle_message(self, raw_message: str) -> None:
        """Process incoming WebSocket message."""
        try:
            data = json.loads(raw_message)

            # Handle list of messages (initial snapshot)
            if isinstance(data, list):
                for item in data:
                    await self._process_single_message(item)
            else:
                await self._process_single_message(data)

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON message: {e}")

        except Exception as e:
            logger.error(f"Error handling message: {e}")

    async def _process_single_message(self, data: dict) -> None:
        """Process a single message (not a list)."""
        try:
            event_type = data.get("event_type")

            # Market resolved notification (custom feature)
            if event_type == "market_resolved":
                update = MarketResolved.from_message(data)
                for callback in self._market_resolved_callbacks:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"MarketResolved callback error: {e}")

            # Trade update
            elif event_type == "last_trade_price":
                update = TradeUpdate.from_message(data)
                for callback in self._trade_callbacks:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"Trade callback error: {e}")

            # Book update - has bids/asks
            elif "bids" in data or "asks" in data:
                update = BookUpdate.from_message(data)
                for callback in self._book_callbacks:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"Book callback error: {e}")

            # Price change - has changes array
            elif "changes" in data:
                update = PriceChange.from_message(data)
                for callback in self._price_callbacks:
                    try:
                        callback(update)
                    except Exception as e:
                        logger.error(f"Price callback error: {e}")

            # Silently ignore other event types (tick_size_change, etc.)

        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def run_for_duration(self, seconds: float) -> None:
        """
        Run the event loop for a specified duration.

        Args:
            seconds: Duration to run
        """
        task = asyncio.create_task(self.run())

        try:
            await asyncio.sleep(seconds)
        finally:
            self._running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def __repr__(self) -> str:
        """String representation."""
        status = "connected" if self.connected else "disconnected"
        return f"WebSocketClient({status}, subs={len(self._subscribed_tokens)})"


# =============================================================================
# USER WEBSOCKET CLIENT - Real-time fill notifications
# =============================================================================

@dataclass
class OrderFill:
    """Order fill notification from user WebSocket."""
    order_id: str
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size_matched: float
    original_size: float
    status: str  # "LIVE", "MATCHED", "CANCELED"
    outcome: str  # "Yes" or "No" (UP or DOWN)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_fully_filled(self) -> bool:
        return self.status == "MATCHED" or self.size_matched >= self.original_size

    @property
    def fill_pct(self) -> float:
        return self.size_matched / self.original_size if self.original_size > 0 else 0


# Callback type for fill notifications
FillCallback = Callable[[OrderFill], None]


class UserWebSocketClient:
    """
    WebSocket client for user-specific events (order fills, cancellations).

    Connects to WS_USER_URL and provides instant fill notifications,
    replacing the 2-second polling approach.

    Usage:
        client = UserWebSocketClient(api_key="...", api_secret="...", api_passphrase="...")
        client.on_fill(lambda fill: print(f"Filled: {fill.side} {fill.size_matched} @ {fill.price}"))
        await client.connect()
        await client.run()
    """

    DEFAULT_RECONNECT_DELAY = 1.0
    MAX_RECONNECT_DELAY = 30.0

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        auto_reconnect: bool = True,
    ):
        """
        Initialize user WebSocket client.

        Args:
            api_key: Polymarket API key
            api_secret: Polymarket API secret
            api_passphrase: Polymarket API passphrase
            auto_reconnect: Whether to auto-reconnect on disconnect
        """
        self.url = WS_USER_URL
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.auto_reconnect = auto_reconnect

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._connected = False
        self._running = False
        self._authenticated = False

        # Callbacks
        self._fill_callbacks: List[FillCallback] = []

        # Reconnect state
        self._reconnect_delay = self.DEFAULT_RECONNECT_DELAY
        self._reconnect_task: Optional[asyncio.Task] = None

        # Track orders we're watching
        self._watched_orders: Dict[str, dict] = {}  # order_id -> {side, token_id, ...}

    @property
    def connected(self) -> bool:
        return self._connected and self._ws is not None and self._authenticated

    def on_fill(self, callback: FillCallback) -> None:
        """Register callback for fill notifications."""
        self._fill_callbacks.append(callback)

    def watch_order(self, order_id: str, side: str, token_id: str, size: float, price: float) -> None:
        """
        Add an order to watch for fills.

        Args:
            order_id: The order ID to watch
            side: "UP" or "DOWN"
            token_id: The token ID
            size: Expected fill size
            price: Order price
        """
        self._watched_orders[order_id] = {
            "side": side,
            "token_id": token_id,
            "size": size,
            "price": price,
            "watched_at": datetime.now(timezone.utc),
        }
        logger.debug(f"[USER_WS] Watching order {order_id[:16]}... for {side} fill")

    def unwatch_order(self, order_id: str) -> None:
        """Stop watching an order."""
        self._watched_orders.pop(order_id, None)

    async def connect(self) -> bool:
        """Connect to user WebSocket and authenticate."""
        try:
            logger.info(f"[USER_WS] Connecting to {self.url}")
            self._ws = await websockets.connect(
                self.url,
                ping_interval=10,
                ping_timeout=10,
                open_timeout=10,
                close_timeout=10,
            )
            self._connected = True
            self._reconnect_delay = self.DEFAULT_RECONNECT_DELAY

            # Authenticate
            auth_message = {
                "auth": {
                    "apiKey": self.api_key,
                    "secret": self.api_secret,
                    "passphrase": self.api_passphrase,
                }
            }
            await self._ws.send(json.dumps(auth_message))
            logger.info("[USER_WS] Sent authentication")

            # Wait for auth response
            try:
                response = await asyncio.wait_for(self._ws.recv(), timeout=5.0)
                data = json.loads(response)
                if data.get("error"):
                    logger.error(f"[USER_WS] Auth failed: {data.get('error')}")
                    self._authenticated = False
                    return False
                self._authenticated = True
                logger.info("[USER_WS] Authenticated successfully")
            except asyncio.TimeoutError:
                logger.warning("[USER_WS] Auth response timeout, assuming success")
                self._authenticated = True

            return True

        except Exception as e:
            logger.error(f"[USER_WS] Connection failed: {e}")
            self._connected = False
            self._authenticated = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False
        self._connected = False
        self._authenticated = False

        if self._reconnect_task:
            self._reconnect_task.cancel()
            self._reconnect_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        logger.info("[USER_WS] Disconnected")

    async def run(self) -> None:
        """Run the WebSocket event loop."""
        self._running = True

        while self._running:
            try:
                if not self._ws:
                    if self.auto_reconnect:
                        await self._reconnect()
                    else:
                        break
                    continue

                message = await self._ws.recv()
                await self._handle_message(message)

            except ConnectionClosed:
                logger.warning("[USER_WS] Connection closed")
                self._connected = False
                self._authenticated = False

                if self.auto_reconnect and self._running:
                    await self._reconnect()
                else:
                    break

            except Exception as e:
                logger.error(f"[USER_WS] Error in loop: {e}")
                if not self._running:
                    break

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        logger.info(f"[USER_WS] Reconnecting in {self._reconnect_delay:.1f}s...")
        await asyncio.sleep(self._reconnect_delay)

        self._reconnect_delay = min(
            self._reconnect_delay * 2,
            self.MAX_RECONNECT_DELAY,
        )

        await self.connect()

    async def _handle_message(self, raw_message: str) -> None:
        """Process incoming message."""
        try:
            data = json.loads(raw_message)

            # Handle list of messages
            if isinstance(data, list):
                for item in data:
                    await self._process_single_message(item)
            else:
                await self._process_single_message(data)

        except json.JSONDecodeError as e:
            logger.debug(f"[USER_WS] Invalid JSON: {e}")
        except Exception as e:
            logger.error(f"[USER_WS] Error handling message: {e}")

    async def _process_single_message(self, data: dict) -> None:
        """Process a single message."""
        try:
            event_type = data.get("event_type") or data.get("type")

            # Order events (placement, update, cancellation)
            if event_type == "order" or data.get("original_size") is not None:
                order_id = data.get("id", "")
                status = data.get("status", "")
                size_matched = float(data.get("size_matched", 0))
                original_size = float(data.get("original_size", 0))

                # Check if this is a fill we care about
                if size_matched > 0:
                    fill = OrderFill(
                        order_id=order_id,
                        token_id=data.get("asset_id", ""),
                        side=data.get("side", ""),
                        price=float(data.get("price", 0)),
                        size_matched=size_matched,
                        original_size=original_size,
                        status=status,
                        outcome=data.get("outcome", ""),
                    )

                    # Map outcome to UP/DOWN
                    outcome_upper = fill.outcome.upper() if fill.outcome else ""
                    if outcome_upper in ("YES", "UP"):
                        fill_side = "UP"
                    elif outcome_upper in ("NO", "DOWN"):
                        fill_side = "DOWN"
                    else:
                        fill_side = fill.side

                    logger.info(
                        f"[USER_WS] 🔔 FILL: {fill_side} {fill.size_matched:.0f}/{fill.original_size:.0f} "
                        f"@ ${fill.price:.4f} ({fill.status})"
                    )

                    # Fire callbacks
                    for callback in self._fill_callbacks:
                        try:
                            callback(fill)
                        except Exception as e:
                            logger.error(f"[USER_WS] Fill callback error: {e}")

            # Trade events
            elif event_type == "trade":
                trade_status = data.get("status", "")
                size = float(data.get("size", 0))
                price = float(data.get("price", 0))
                side = data.get("side", "")
                outcome = data.get("outcome", "")

                logger.debug(
                    f"[USER_WS] Trade: {outcome} {side} {size:.0f} @ ${price:.4f} ({trade_status})"
                )

        except Exception as e:
            logger.error(f"[USER_WS] Error processing message: {e}")

    def __repr__(self) -> str:
        status = "connected" if self.connected else "disconnected"
        return f"UserWebSocketClient({status}, watching={len(self._watched_orders)})"
