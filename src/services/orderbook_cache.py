"""
WebSocket-based orderbook cache with REST fallback.

Converts BookUpdate (datetime timestamp) -> Orderbook (int timestamp)
to maintain compatibility with existing codebase.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple, List, Set, Any, TYPE_CHECKING

from src.api.websocket_client import WebSocketClient, BookUpdate
from src.models.orderbook import Orderbook, Order

if TYPE_CHECKING:
    from src.api.polymarket_client import PolymarketClient

logger = logging.getLogger(__name__)


@dataclass
class CachedOrderbook:
    """Cached orderbook with metadata."""
    token_id: str
    orderbook: Orderbook  # CONVERTED from BookUpdate
    received_at: datetime
    source: str  # "websocket" or "rest"

    def age_ms(self) -> float:
        """Age in milliseconds since received."""
        return (datetime.now(timezone.utc) - self.received_at).total_seconds() * 1000

    def is_stale(self, max_age_ms: int = 5000) -> bool:
        """Check if orderbook is older than max_age_ms."""
        return self.age_ms() > max_age_ms


class OrderbookCache:
    """
    Thread-safe orderbook cache.

    Converts WebSocket BookUpdate -> Orderbook for compatibility.
    """

    def __init__(self, max_age_ms: int = 5000):
        self._cache: Dict[str, CachedOrderbook] = {}
        self._lock = asyncio.Lock()
        self.max_age_ms = max_age_ms
        self._update_count = 0

    async def update_from_websocket(self, book_update: BookUpdate) -> None:
        """
        Update cache from WebSocket BookUpdate.

        CRITICAL: Converts BookUpdate -> Orderbook for compatibility:
        - datetime -> int (unix ms)
        - Adds is_garbage(), has_liquidity() methods via Orderbook class
        """
        # Convert BookUpdate bids/asks to Orderbook format
        # BookUpdate.bids/asks are already List[Order] from websocket_client.py
        orderbook = Orderbook(
            token_id=book_update.token_id,
            bids=book_update.bids,  # Already List[Order]
            asks=book_update.asks,  # Already List[Order]
            timestamp=int(book_update.timestamp.timestamp() * 1000),  # datetime -> int ms
        )

        async with self._lock:
            self._cache[book_update.token_id] = CachedOrderbook(
                token_id=book_update.token_id,
                orderbook=orderbook,
                received_at=datetime.now(timezone.utc),
                source="websocket",
            )
            self._update_count += 1

    async def get(self, token_id: str) -> Optional[CachedOrderbook]:
        """Get cached orderbook if fresh."""
        async with self._lock:
            cached = self._cache.get(token_id)
            if cached and not cached.is_stale(self.max_age_ms):
                return cached
            return None

    async def get_pair(
        self, up_token_id: str, down_token_id: str
    ) -> Tuple[Optional[Orderbook], Optional[Orderbook]]:
        """Get both UP and DOWN orderbooks if fresh."""
        up_cached = await self.get(up_token_id)
        down_cached = await self.get(down_token_id)
        return (
            up_cached.orderbook if up_cached else None,
            down_cached.orderbook if down_cached else None,
        )

    async def clear(self, token_id: str = None) -> None:
        """Clear cache for token or all."""
        async with self._lock:
            if token_id:
                self._cache.pop(token_id, None)
            else:
                self._cache.clear()

    def stats(self) -> Dict[str, Any]:
        """Cache statistics."""
        return {
            "cached_tokens": len(self._cache),
            "tokens": list(self._cache.keys())[:4],  # First 4 for brevity
            "update_count": self._update_count,
        }


class OrderbookManager:
    """
    Unified manager for WebSocket orderbook streaming.

    Usage:
        manager = OrderbookManager(rest_client=client)
        await manager.start()
        await manager.subscribe_market(market)  # Subscribes to UP + DOWN

        # In trading loop:
        up_ob, down_ob = await manager.get_orderbooks(up_token, down_token)

        # On market rotation:
        await manager.rotate_to_market(new_market)

        await manager.stop()
    """

    def __init__(
        self,
        rest_client: "PolymarketClient",
        max_cache_age_ms: int = 5000,
        custom_features: bool = True,
    ):
        self.ws_client = WebSocketClient(custom_features=custom_features)
        self.cache = OrderbookCache(max_age_ms=max_cache_age_ms)
        self.rest_client = rest_client

        self._current_tokens: Set[str] = set()
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False

        # Stats
        self._cache_hits = 0
        self._cache_misses = 0

        # Register callback
        self.ws_client.on_book_update(self._on_book_update)

    def _on_book_update(self, update: BookUpdate) -> None:
        """Handle WebSocket book update (sync callback)."""
        # Schedule async cache update
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.cache.update_from_websocket(update))
        except RuntimeError:
            # No running loop (shouldn't happen in normal operation)
            pass

    async def start(self) -> bool:
        """Start WebSocket connection."""
        if self._running:
            return True

        try:
            connected = await self.ws_client.connect()
            if not connected:
                logger.warning("WebSocket connection failed, will use REST fallback")
                return False

            self._running = True
            self._ws_task = asyncio.create_task(self.ws_client.run())
            logger.info("OrderbookManager started (WebSocket mode)")
            return True
        except Exception as e:
            logger.error(f"OrderbookManager start failed: {e}")
            return False

    async def stop(self) -> None:
        """Stop WebSocket and cleanup."""
        self._running = False

        await self.ws_client.disconnect()

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        await self.cache.clear()
        self._current_tokens.clear()
        logger.info("OrderbookManager stopped")

    async def subscribe_market(self, market) -> bool:
        """
        Subscribe to market's UP and DOWN tokens.

        Args:
            market: BTCMarket with up_token_id and down_token_id
        """
        tokens = [market.up_token_id, market.down_token_id]
        return await self._subscribe_tokens(tokens)

    async def rotate_to_market(self, new_market) -> bool:
        """
        Rotate subscription to new market.

        CRITICAL: Polymarket WebSocket doesn't properly switch subscriptions.
        Must reconnect WebSocket entirely to get fresh subscription for new tokens.
        """
        new_tokens = [new_market.up_token_id, new_market.down_token_id]

        # Clear old cache entries
        for old_token in self._current_tokens:
            if old_token not in new_tokens:
                await self.cache.clear(old_token)

        # CRITICAL: Reconnect WebSocket for reliable subscription switch
        # Polymarket WS keeps sending old token data if you just subscribe()
        if self._running:
            logger.info(f"[ROTATION] Reconnecting WebSocket for {new_market.slug}")

            # Cancel old ws_task
            if self._ws_task:
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass

            # Disconnect and reconnect
            await self.ws_client.disconnect()
            await asyncio.sleep(0.3)  # Brief pause for clean disconnect

            connected = await self.ws_client.connect()
            if connected:
                self._ws_task = asyncio.create_task(self.ws_client.run())
                # Subscribe to new tokens on fresh connection
                success = await self.ws_client.subscribe(new_tokens)
                if success:
                    self._current_tokens = set(new_tokens)
                    logger.info(f"[ROTATION] WebSocket reconnected and subscribed to {new_market.slug}")
                    return True
                else:
                    logger.warning(f"[ROTATION] Subscribe failed after reconnect")
            else:
                logger.warning(f"[ROTATION] WebSocket reconnect failed, using REST fallback")

        return False

    async def _subscribe_tokens(self, token_ids: List[str]) -> bool:
        """Subscribe to tokens."""
        if not self.ws_client.connected:
            logger.warning("WebSocket not connected, will use REST fallback")
            return False

        success = await self.ws_client.subscribe(token_ids)
        if success:
            self._current_tokens = set(token_ids)
        return success

    async def get_orderbooks(
        self,
        up_token_id: str,
        down_token_id: str,
    ) -> Tuple[Optional[Orderbook], Optional[Orderbook]]:
        """
        Get orderbooks for UP/DOWN pair.

        Uses WebSocket cache if fresh, falls back to REST if stale/missing.

        Returns:
            Tuple of (up_orderbook, down_orderbook)
        """
        # Try cache first
        up_ob, down_ob = await self.cache.get_pair(up_token_id, down_token_id)

        # If both cached and fresh, return immediately
        if up_ob and down_ob:
            self._cache_hits += 1
            return up_ob, down_ob

        # Fallback to REST for missing/stale data
        self._cache_misses += 1
        logger.debug("WebSocket cache miss, falling back to REST")

        tasks = []
        fetch_up = up_ob is None
        fetch_down = down_ob is None

        if fetch_up:
            tasks.append(self.rest_client.get_orderbook(up_token_id))
        if fetch_down:
            tasks.append(self.rest_client.get_orderbook(down_token_id))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)

            idx = 0
            if fetch_up:
                if not isinstance(results[idx], Exception):
                    up_ob = Orderbook.from_clob_response(results[idx])
                else:
                    logger.warning(f"REST fallback failed for UP: {results[idx]}")
                idx += 1
            if fetch_down:
                if not isinstance(results[idx], Exception):
                    down_ob = Orderbook.from_clob_response(results[idx])
                else:
                    logger.warning(f"REST fallback failed for DOWN: {results[idx]}")

        return up_ob, down_ob

    @property
    def connected(self) -> bool:
        """Whether WebSocket is connected."""
        return self.ws_client.connected

    @property
    def stats(self) -> Dict[str, Any]:
        """Manager statistics."""
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total * 100 if total > 0 else 0
        return {
            "ws_connected": self.ws_client.connected,
            "subscribed_tokens": len(self._current_tokens),
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate_pct": round(hit_rate, 1),
            "cache": self.cache.stats(),
        }

    def __repr__(self) -> str:
        status = "connected" if self.connected else "disconnected"
        return f"OrderbookManager({status}, tokens={len(self._current_tokens)})"
