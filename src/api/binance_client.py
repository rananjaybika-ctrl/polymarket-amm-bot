"""
Binance WebSocket client for real-time BTCUSDT price feed.

Provides strike price tracking and rolling window statistics for
directional trading mode flip detection.
"""

import asyncio
import json
import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Deque, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


# Type for velocity threshold callbacks
# callback(velocity_bps: float, direction: str)
# Can be sync or async - we handle both
VelocityCallback = Callable[[float, str], None]
AsyncVelocityCallback = Callable[[float, str], 'asyncio.coroutine']


@dataclass
class PricePoint:
    """Single price observation."""
    timestamp: datetime
    price: float


@dataclass
class PriceStats:
    """Rolling window price statistics."""
    current_price: float
    strike_price: float
    price_vs_strike_pct: float
    mean_change: float
    std_dev: float
    window_seconds: int
    sample_count: int


class BinanceClient:
    """
    Real-time BTCUSDT price feed from Binance.

    Uses public WebSocket stream (no API key required) to track:
    - Current price
    - Strike price (reference at market open)
    - Rolling window statistics for flip detection
    """

    WEBSOCKET_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    KLINES_URL = "https://api.binance.com/api/v3/klines"

    # Velocity threshold for event-driven callbacks (bps/sec)
    VELOCITY_PULL_THRESHOLD = 0.05  # ~$5 BTC move in 10s

    def __init__(self, window_seconds: int = 60, max_history_seconds: int = 3600):
        """
        Initialize Binance client.

        Args:
            window_seconds: Default rolling window for statistics (default 60s)
            max_history_seconds: Maximum price history to keep (default 1 hour)
        """
        self._current_price: float = 0.0
        self._strike_price: float = 0.0
        self._strike_timestamp: Optional[datetime] = None

        # Keep up to max_history_seconds of data (assuming ~1 update/sec)
        self._price_history: Deque[PricePoint] = deque(maxlen=max_history_seconds)

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running: bool = False
        self._connected: bool = False
        self._window_seconds = window_seconds

        # Reconnection settings
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 30.0
        self._reconnect_task: Optional[asyncio.Task] = None

        # Event-driven velocity threshold callbacks
        # These fire IMMEDIATELY when velocity exceeds threshold
        self._velocity_callbacks: List[VelocityCallback] = []
        self._callback_cooldown_secs: float = 1.0  # Minimum time between callbacks
        self._last_callback_time: float = 0.0
        self._velocity_window_secs: int = 10  # Window for velocity calculation

    async def connect(self) -> None:
        """Connect to Binance WebSocket stream and start receiving prices."""
        if self._running:
            logger.warning("BinanceClient already running")
            return

        self._running = True
        self._reconnect_task = asyncio.create_task(self._connection_loop())
        logger.info("BinanceClient started")

    async def _connection_loop(self) -> None:
        """Main connection loop with automatic reconnection."""
        reconnect_delay = self._reconnect_delay

        while self._running:
            try:
                async with websockets.connect(
                    self.WEBSOCKET_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    reconnect_delay = self._reconnect_delay  # Reset on successful connect
                    logger.info(f"Connected to Binance WebSocket")

                    await self._receive_loop(ws)

            except ConnectionClosed as e:
                logger.warning(f"Binance WebSocket closed: {e}")
            except Exception as e:
                logger.error(f"Binance WebSocket error: {e}")
            finally:
                self._connected = False
                self._ws = None

            if self._running:
                logger.info(f"Reconnecting in {reconnect_delay:.1f}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._max_reconnect_delay)

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Receive and process messages from WebSocket."""
        async for message in ws:
            if not self._running:
                break

            try:
                data = json.loads(message)
                # Trade stream format: {"e": "trade", "p": "23456.78", ...}
                if "p" in data:
                    price = float(data["p"])
                    now = datetime.now(timezone.utc)

                    self._current_price = price
                    self._price_history.append(PricePoint(timestamp=now, price=price))

                    # EVENT-DRIVEN: Check velocity on every tick
                    # This is the key latency advantage - react within 100ms of Binance move
                    if self._velocity_callbacks and self._strike_price > 0:
                        self._check_velocity_and_fire()

            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.debug(f"Failed to parse Binance message: {e}")

    async def disconnect(self) -> None:
        """Disconnect from WebSocket."""
        self._running = False

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        self._connected = False
        logger.info("BinanceClient disconnected")

    def set_strike_price(self, price: Optional[float] = None) -> float:
        """
        Set the strike price (market open reference).

        Args:
            price: Specific price to set, or None to use current price

        Returns:
            The strike price that was set
        """
        if price is not None:
            self._strike_price = price
        else:
            self._strike_price = self._current_price

        self._strike_timestamp = datetime.now(timezone.utc)
        logger.info(f"Strike price set: ${self._strike_price:,.2f}")
        return self._strike_price

    async def fetch_previous_candle_close(self, interval: str = "15m") -> Optional[float]:
        """
        Fetch the closing price of the previous completed candle.

        Args:
            interval: Candle interval (e.g., "15m", "1h", "1m")

        Returns:
            Close price of the previous candle, or None if failed
        """
        import aiohttp

        try:
            url = f"{self.KLINES_URL}?symbol=BTCUSDT&interval={interval}&limit=2"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        # Kline format: [open_time, open, high, low, close, volume, ...]
                        # We want the previous closed candle (index 0), close price is at index 4
                        if len(data) >= 2:
                            previous_candle = data[0]  # The completed candle
                            close_price = float(previous_candle[4])
                            logger.info(f"Previous {interval} candle close: ${close_price:,.2f}")
                            return close_price
                        else:
                            logger.warning(f"Unexpected klines response: {data}")
                            return None
                    else:
                        logger.warning(f"Failed to fetch klines: HTTP {response.status}")
                        return None
        except Exception as e:
            logger.warning(f"Error fetching previous candle close: {e}")
            return None

    async def set_strike_from_previous_candle(self, interval: str = "15m") -> float:
        """
        Set strike price from the previous candle's close price.

        Args:
            interval: Candle interval (e.g., "15m" for 15-minute markets)

        Returns:
            The strike price that was set (or current price as fallback)
        """
        close_price = await self.fetch_previous_candle_close(interval)
        if close_price is not None:
            self._strike_price = close_price
            self._strike_timestamp = datetime.now(timezone.utc)
            logger.info(f"Strike price set from previous {interval} candle: ${self._strike_price:,.2f}")
            return self._strike_price
        else:
            # Fallback to current price if we can't fetch candle
            logger.warning(f"Falling back to current price for strike")
            return self.set_strike_price()

    @property
    def current_price(self) -> float:
        """Current BTCUSDT price."""
        return self._current_price

    @property
    def strike_price(self) -> float:
        """Strike price (market open reference)."""
        return self._strike_price

    @property
    def is_connected(self) -> bool:
        """Whether WebSocket is currently connected."""
        return self._connected

    @property
    def price_vs_strike_pct(self) -> float:
        """
        Current price change vs strike as percentage.

        Returns:
            Percentage change (e.g., 0.5 means +0.5%)
            Returns 0.0 if no strike price set
        """
        if self._strike_price <= 0:
            return 0.0
        return ((self._current_price - self._strike_price) / self._strike_price) * 100

    def get_price_changes(self, window_seconds: Optional[int] = None) -> List[float]:
        """
        Get percentage price changes over rolling window.

        Args:
            window_seconds: Window size (default: self._window_seconds)

        Returns:
            List of percentage changes between consecutive prices
        """
        window = window_seconds or self._window_seconds
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - window

        # Filter to window
        prices_in_window = [
            p for p in self._price_history
            if p.timestamp.timestamp() >= cutoff
        ]

        if len(prices_in_window) < 2:
            return []

        # Calculate percentage changes
        changes = []
        for i in range(1, len(prices_in_window)):
            prev_price = prices_in_window[i - 1].price
            curr_price = prices_in_window[i].price
            if prev_price > 0:
                pct_change = ((curr_price - prev_price) / prev_price) * 100
                changes.append(pct_change)

        return changes

    def get_std_dev(self, window_seconds: Optional[int] = None) -> float:
        """
        Calculate standard deviation of price changes over window.

        Args:
            window_seconds: Window size (default: self._window_seconds)

        Returns:
            Standard deviation, or 0.0 if insufficient data
        """
        changes = self.get_price_changes(window_seconds)

        if len(changes) < 2:
            return 0.0

        try:
            return statistics.stdev(changes)
        except statistics.StatisticsError:
            return 0.0

    def get_mean_change(self, window_seconds: Optional[int] = None) -> float:
        """
        Calculate mean price change over window.

        Args:
            window_seconds: Window size (default: self._window_seconds)

        Returns:
            Mean change, or 0.0 if insufficient data
        """
        changes = self.get_price_changes(window_seconds)

        if not changes:
            return 0.0

        return statistics.mean(changes)

    def get_stats(self, window_seconds: Optional[int] = None) -> PriceStats:
        """
        Get comprehensive price statistics.

        Args:
            window_seconds: Window size (default: self._window_seconds)

        Returns:
            PriceStats with all statistics
        """
        window = window_seconds or self._window_seconds
        changes = self.get_price_changes(window)

        return PriceStats(
            current_price=self._current_price,
            strike_price=self._strike_price,
            price_vs_strike_pct=self.price_vs_strike_pct,
            mean_change=statistics.mean(changes) if changes else 0.0,
            std_dev=statistics.stdev(changes) if len(changes) >= 2 else 0.0,
            window_seconds=window,
            sample_count=len(changes),
        )

    def calculate_velocity(self, window_seconds: Optional[int] = None) -> float:
        """
        Calculate price velocity in basis points per second.

        Positive = price rising (UP winning)
        Negative = price falling (DOWN winning)

        Args:
            window_seconds: Window for velocity calculation (default 10s)

        Returns:
            Velocity in bps/sec
        """
        window = window_seconds or self._velocity_window_secs
        changes = self.get_price_changes(window)

        if not changes:
            return 0.0

        # Sum of percentage changes over window
        total_change_pct = sum(changes)

        # Convert to bps/sec
        velocity_pct_per_sec = total_change_pct / window

        # Convert % to bps (1% = 100 bps)
        return velocity_pct_per_sec * 100

    def calculate_volatility_ratio(
        self,
        short_window: int = 10,
        long_window: int = 60,
    ) -> float:
        """
        Calculate volatility ratio: short-term vol / long-term vol.

        This measures whether the market is MORE or LESS volatile than usual
        RIGHT NOW (relative volatility vs absolute price movement).

        Interpretation:
            ratio > 1.5: Market is spiking (more volatile than usual)
            ratio < 0.5: Market is calm (less volatile than usual)
            ratio ≈ 1.0: Normal volatility

        Args:
            short_window: Short-term window in seconds (default 10s)
            long_window: Long-term window in seconds (default 60s)

        Returns:
            Volatility ratio, or 1.0 if insufficient data
        """
        short_changes = self.get_price_changes(short_window)
        long_changes = self.get_price_changes(long_window)

        if len(short_changes) < 5 or len(long_changes) < 20:
            return 1.0  # Not enough data, assume normal

        try:
            short_vol = statistics.stdev(short_changes)
            long_vol = statistics.stdev(long_changes)
        except statistics.StatisticsError:
            return 1.0

        if long_vol <= 0:
            return 1.0

        ratio = short_vol / long_vol

        # Cap to reasonable range
        return min(max(ratio, 0.1), 5.0)

    # =========================================================================
    # EVENT-DRIVEN QUOTE PULLING (VELOCITY-BASED)
    # =========================================================================
    # React within 100-200ms of Binance price moves instead of waiting for
    # main loop iteration (1-2 seconds). This is the key latency advantage.

    def on_velocity_threshold_crossed(self, callback: VelocityCallback) -> None:
        """
        Register callback for when velocity exceeds threshold (0.05 bps/sec).

        The callback is fired IMMEDIATELY when velocity exceeds the pull threshold.
        This enables 100-200ms reaction time to Binance price moves.

        Args:
            callback: Function(velocity_bps, direction) called on threshold cross
                      velocity_bps: Current velocity in basis points per second
                      direction: "UP" if velocity > 0, "DOWN" if velocity < 0

        Example:
            def on_velocity_alert(velocity_bps, direction):
                if direction == "UP":
                    # Cancel DOWN orders immediately (price rising)
                    asyncio.create_task(cancel_down_orders())

            binance.on_velocity_threshold_crossed(on_velocity_alert)
        """
        self._velocity_callbacks.append(callback)
        logger.info(f"Registered velocity callback (total: {len(self._velocity_callbacks)})")

    def remove_velocity_callback(self, callback: VelocityCallback) -> bool:
        """Remove a previously registered callback."""
        try:
            self._velocity_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def clear_velocity_callbacks(self) -> None:
        """Remove all velocity callbacks."""
        self._velocity_callbacks.clear()
        logger.info("Cleared all velocity callbacks")

    def reset_velocity_state(self) -> None:
        """
        Reset velocity state tracking for new market.

        Call this when switching to a new market.
        """
        self._last_callback_time = 0.0
        logger.debug("Reset velocity state for new market")

    def _check_velocity_and_fire(self) -> None:
        """
        Check velocity and fire callbacks when threshold exceeded.

        Called on every Binance WebSocket tick.
        """
        import time

        # Calculate current velocity
        velocity = self.calculate_velocity()
        abs_velocity = abs(velocity)

        # Determine direction
        if velocity > 0:
            direction = "UP"  # Price rising
        elif velocity < 0:
            direction = "DOWN"  # Price falling
        else:
            direction = "FLAT"

        # Check if velocity exceeds threshold
        should_fire = abs_velocity > self.VELOCITY_PULL_THRESHOLD

        # Cooldown to prevent callback spam
        now = time.time()
        if should_fire and (now - self._last_callback_time) < self._callback_cooldown_secs:
            should_fire = False  # Still in cooldown

        # Fire callbacks
        if should_fire and self._velocity_callbacks:
            logger.info(
                f"[EVENT] Velocity threshold: vel={velocity:.3f}bps | "
                f"dir={direction}, price=${self._current_price:,.2f}"
            )
            self._last_callback_time = now

            for callback in self._velocity_callbacks:
                try:
                    # Handle both sync and async callbacks
                    result = callback(velocity, direction)
                    if asyncio.iscoroutine(result):
                        # Schedule async callback without blocking
                        asyncio.create_task(result)
                except Exception as e:
                    logger.error(f"Velocity callback error: {e}")
