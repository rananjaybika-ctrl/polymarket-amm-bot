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


# Type for z-score threshold callbacks
# callback(z_score: float, direction: str, trend_state: str)
ZScoreCallback = Callable[[float, str, str], None]


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

    # Z-score thresholds for event-driven callbacks
    Z_STRONG_THRESHOLD = 2.0
    Z_EXTREME_THRESHOLD = 3.0

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

        # Event-driven z-score threshold callbacks
        # These fire IMMEDIATELY when z-score crosses STRONG threshold
        self._z_threshold_callbacks: List[ZScoreCallback] = []
        self._last_z_state: str = "neutral"  # Track state changes: neutral, strong, extreme
        self._callback_cooldown_secs: float = 1.0  # Minimum time between callbacks
        self._last_callback_time: float = 0.0

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

                    # EVENT-DRIVEN: Check z-score on every tick
                    # This is the key latency advantage - react within 100ms of Binance move
                    if self._z_threshold_callbacks and self._strike_price > 0:
                        self._check_z_threshold_and_fire()

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

    def calculate_z_score(self, window_seconds: Optional[int] = None) -> float:
        """
        Calculate z-score of current price vs strike deviation.

        Measures how many standard deviations the current move from strike is,
        scaled by the number of observations (random walk adjustment).

        The calculation accounts for the fact that price changes accumulate over time:
        - current_deviation: Total % move from strike
        - std_dev: Per-tick volatility
        - num_ticks: Number of price observations in window

        For a random walk, expected deviation after N steps ~ std_dev * sqrt(N).
        Z-score = current_deviation / (std_dev * sqrt(N))

        Args:
            window_seconds: Window size for std dev calculation

        Returns:
            Z-score (absolute value), or 0.0 if insufficient data
        """
        window = window_seconds or self._window_seconds
        changes = self.get_price_changes(window)

        if len(changes) < 2:
            return 0.0

        try:
            std_dev = statistics.stdev(changes)
        except statistics.StatisticsError:
            return 0.0

        if std_dev <= 0:
            return 0.0

        # Number of ticks in the window
        num_ticks = len(changes)

        # Expected deviation for a random walk: std_dev * sqrt(N)
        # This normalizes the z-score to account for time elapsed
        expected_deviation = std_dev * (num_ticks ** 0.5)

        if expected_deviation <= 0:
            return 0.0

        # Current move from strike (absolute value)
        current_deviation = abs(self.price_vs_strike_pct)

        # Z-score: how many "expected deviations" is the current move?
        raw_z = current_deviation / expected_deviation

        # Cap z-score to reasonable range (standard practice)
        # Raw z can be huge when per-tick volatility is tiny but total move is large
        return min(raw_z, 5.0)

    # =========================================================================
    # EVENT-DRIVEN QUOTE PULLING
    # =========================================================================
    # React within 100-200ms of Binance price moves instead of waiting for
    # main loop iteration (1-2 seconds). This is the key latency advantage.

    def on_z_threshold_crossed(self, callback: ZScoreCallback) -> None:
        """
        Register callback for when z-score crosses STRONG threshold (2.0).

        The callback is fired IMMEDIATELY when z-score transitions from
        neutral/mild to strong/extreme. This enables 100-200ms reaction time
        to Binance price moves, compared to 1-2 second polling.

        Args:
            callback: Function(z_score, direction, trend_state) called on threshold cross
                      direction: "UP" if price > strike, "DOWN" if price < strike
                      trend_state: "strong" or "extreme"

        Example:
            def on_trend_alert(z_score, direction, trend_state):
                if direction == "UP":
                    # Cancel DOWN orders immediately
                    asyncio.create_task(cancel_down_orders())

            binance.on_z_threshold_crossed(on_trend_alert)
        """
        self._z_threshold_callbacks.append(callback)
        logger.info(f"Registered z-threshold callback (total: {len(self._z_threshold_callbacks)})")

    def remove_z_threshold_callback(self, callback: ZScoreCallback) -> bool:
        """Remove a previously registered callback."""
        try:
            self._z_threshold_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def clear_z_threshold_callbacks(self) -> None:
        """Remove all z-threshold callbacks."""
        self._z_threshold_callbacks.clear()
        self._last_z_state = "neutral"
        logger.info("Cleared all z-threshold callbacks")

    def reset_z_state(self) -> None:
        """
        Reset z-state tracking for new market.

        Call this when switching to a new market so we don't miss the first
        threshold crossing. The strike price change means old z-state is invalid.
        """
        self._last_z_state = "neutral"
        self._last_callback_time = 0.0
        logger.debug("Reset z-state for new market")

    def _check_z_threshold_and_fire(self) -> None:
        """
        Check z-score and fire callbacks on state CHANGE to strong/extreme.

        Called on every Binance WebSocket tick. Tracks state transitions to
        avoid firing repeatedly while in same state.

        State machine:
            neutral <-> strong <-> extreme
                     ↑ FIRE        ↑ FIRE (on entry)
        """
        import time

        # Calculate current z-score
        z_score = self.calculate_z_score()
        abs_z = abs(z_score)

        # Determine current state
        if abs_z >= self.Z_EXTREME_THRESHOLD:
            new_state = "extreme"
        elif abs_z >= self.Z_STRONG_THRESHOLD:
            new_state = "strong"
        else:
            new_state = "neutral"

        # Determine direction
        if self.price_vs_strike_pct > 0.1:
            direction = "UP"
        elif self.price_vs_strike_pct < -0.1:
            direction = "DOWN"
        else:
            direction = "FLAT"

        # Check for state TRANSITION into strong/extreme
        # Fire when: neutral -> strong, neutral -> extreme, or strong -> extreme
        should_fire = False

        if self._last_z_state == "neutral" and new_state in ("strong", "extreme"):
            should_fire = True  # Crossed into danger zone
        elif self._last_z_state == "strong" and new_state == "extreme":
            should_fire = True  # Getting worse

        # Cooldown to prevent callback spam
        now = time.time()
        if should_fire and (now - self._last_callback_time) < self._callback_cooldown_secs:
            should_fire = False  # Still in cooldown

        # Fire callbacks
        if should_fire and self._z_threshold_callbacks:
            logger.warning(
                f"[EVENT] Z-threshold crossed: {self._last_z_state} -> {new_state} | "
                f"z={z_score:.2f}, dir={direction}, price=${self._current_price:,.2f}"
            )
            self._last_callback_time = now

            for callback in self._z_threshold_callbacks:
                try:
                    callback(z_score, direction, new_state)
                except Exception as e:
                    logger.error(f"Z-threshold callback error: {e}")

        # Update state
        self._last_z_state = new_state
