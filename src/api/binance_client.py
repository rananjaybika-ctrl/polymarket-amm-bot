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


# Type for spike detection callbacks
# callback(direction: str, magnitude_pct: float, price: float, ewma_price: float)
# ewma_price added in Feb 2026 for event-driven spike detection
SpikeCallback = Callable[[str, float, float, float], None]
AsyncSpikeCallback = Callable[[str, float, float, float], 'asyncio.coroutine']


class BinanceClient:
    """
    Real-time BTCUSDT price feed from Binance.

    Uses public WebSocket stream (no API key required) to track:
    - Current price
    - Strike price (reference at market open)
    - Rolling window statistics for flip detection
    - Raw spike detection (NEW - faster than velocity)

    UPGRADED: Now supports @bookTicker stream for faster detection (50-100ms vs 200ms).
    Use use_book_ticker=True in constructor for spike capture strategy.
    """

    # Trade stream (default) - ~5 updates/sec
    WEBSOCKET_URL_TRADE = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    # Book ticker stream (faster) - ~20-50 updates/sec
    WEBSOCKET_URL_BOOK_TICKER = "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"
    # Combined stream (both)
    WEBSOCKET_URL_COMBINED = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@bookTicker"

    # Default to trade stream for backward compatibility
    WEBSOCKET_URL = WEBSOCKET_URL_TRADE
    REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    KLINES_URL = "https://api.binance.com/api/v3/klines"

    # Velocity threshold for event-driven callbacks (bps/sec)
    VELOCITY_PULL_THRESHOLD = 0.05  # ~$5 BTC move in 10s

    # Spike detection parameters - CANONICAL from TRADING_CONFIGS.py (Jan 27, 2026)
    # Source of truth: lookback_ms=1200, lookback_ticks=72 at 60Hz
    # For bookTicker (~60Hz avg): 72 ticks ≈ 1200ms
    DEFAULT_SPIKE_LOOKBACK = 72     # 72 ticks ≈ 1200ms at ~60Hz bookTicker (CANONICAL)
    DEFAULT_SPIKE_THRESHOLD = 0.02  # 0.02% minimum to trigger

    def __init__(
        self,
        window_seconds: int = 60,
        max_history_seconds: int = 3600,
        use_book_ticker: bool = False,
        spike_lookback: int = None,
        spike_threshold: float = None,
    ):
        """
        Initialize Binance client.

        Args:
            window_seconds: Default rolling window for statistics (default 60s)
            max_history_seconds: Maximum price history to keep (default 1 hour)
            use_book_ticker: Use @bookTicker stream for faster updates (NEW)
            spike_lookback: Ticks to look back for spike detection (default 3)
            spike_threshold: Minimum % change to trigger spike (default 0.02)
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

        # Stream selection (NEW)
        self._use_book_ticker = use_book_ticker
        if use_book_ticker:
            self._websocket_url = self.WEBSOCKET_URL_BOOK_TICKER
            logger.info("BinanceClient using @bookTicker stream (faster detection)")
        else:
            self._websocket_url = self.WEBSOCKET_URL_TRADE

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

        # Spike detection (NEW - faster than velocity)
        self._spike_lookback = spike_lookback or self.DEFAULT_SPIKE_LOOKBACK
        self._spike_threshold = spike_threshold or self.DEFAULT_SPIKE_THRESHOLD
        self._spike_callbacks: List[SpikeCallback] = []
        self._spike_price_history: List[float] = []
        self._spike_history_size = 150  # Keep last 150 prices (enough for 72-tick lookback + buffer)
        self._last_spike_callback_time: float = 0.0
        self._spike_callback_cooldown_secs: float = 0.5  # Faster cooldown for spikes

        # Buffer diagnostics (Feb 2, 2026) - track fill rate for debugging
        self._tick_count: int = 0  # Total ticks received since connect
        self._last_diagnostic_tick_count: int = 0  # Tick count at last diagnostic log
        self._last_diagnostic_time: float = 0.0  # Time of last diagnostic log

        # EWMA spike detection state (Feb 3, 2026)
        # CRITICAL: EWMA must update on every unique TIMESTAMP tick (~60Hz) to match backtest behavior.
        # Backtest deduplicates by timestamp_ms - we MUST do the same (Feb 5, 2026 FIX).
        # Previous approach (dedup by price value) was WRONG - caused 2.4x trade count mismatch.
        # Alpha = 1 - 0.5 ** (1.0 / (halflife_ms / 16.67)) for 60Hz data
        # EWMA_1000 (1000ms half-life): alpha ≈ 0.0115
        self._ewma_price: Optional[float] = None  # Current EWMA state
        self._ewma_alpha: float = 1 - 0.5 ** (1.0 / (1000 / 16.67))  # ~0.0115 for EWMA_1000
        self._last_ewma_timestamp_ms: int = 0  # Last timestamp_ms when EWMA was updated

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
                    self._websocket_url,  # Use configured URL (trade or bookTicker)
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    reconnect_delay = self._reconnect_delay  # Reset on successful connect
                    stream_type = "bookTicker" if self._use_book_ticker else "trade"
                    logger.info(f"Connected to Binance WebSocket ({stream_type} stream)")

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
                price = None

                # Handle bookTicker format: {"u":123,"s":"BTCUSDT","b":"95000.00","B":"1.5","a":"95001.00","A":"2.0"}
                if 'b' in data and 'a' in data:
                    bid = float(data['b'])
                    ask = float(data['a'])
                    price = (bid + ask) / 2  # Mid price for spike detection

                # Handle trade stream format: {"e": "trade", "p": "23456.78", ...}
                elif "p" in data:
                    price = float(data["p"])

                if price is not None:
                    now = datetime.now(timezone.utc)
                    timestamp_ms = int(now.timestamp() * 1000)

                    self._current_price = price
                    self._price_history.append(PricePoint(timestamp=now, price=price))

                    # TIMESTAMP-BASED DEDUPLICATION (Feb 5, 2026 FIX)
                    # Backtest deduplicates by timestamp_ms: df.drop_duplicates(subset=['timestamp_ms'])
                    # We MUST do the same for EWMA updates to match backtest behavior.
                    # Previous approach (dedup by price value) caused 2.4x trade count mismatch.
                    # At 60Hz, we expect ~16.67ms between unique timestamps.
                    if timestamp_ms != self._last_ewma_timestamp_ms:
                        self._last_ewma_timestamp_ms = timestamp_ms

                        # Update spike price history (one price per unique timestamp)
                        self._spike_price_history.append(price)
                        if len(self._spike_price_history) > self._spike_history_size:
                            self._spike_price_history = self._spike_price_history[-self._spike_history_size:]

                        # Update EWMA on every unique timestamp tick
                        # CRITICAL: This makes EWMA update at ~60Hz to match backtest behavior.
                        # Strategy's _detect_spike_ewma() will read from this state.
                        if self._ewma_price is None:
                            self._ewma_price = price
                        else:
                            self._ewma_price = self._ewma_alpha * price + (1 - self._ewma_alpha) * self._ewma_price

                    # Increment tick counter for diagnostics (counts all ticks, not just unique)
                    self._tick_count += 1

                    # EVENT-DRIVEN: Check velocity on every tick (LEGACY)
                    if self._velocity_callbacks and self._strike_price > 0:
                        self._check_velocity_and_fire()

                    # EVENT-DRIVEN: Check spike on every tick (fires callbacks)
                    # Uses EWMA-based detection for better signal quality
                    if self._spike_callbacks:
                        self._check_spike_ewma_and_fire(price)

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

    # =========================================================================
    # EVENT-DRIVEN SPIKE DETECTION (NEW - FASTER THAN VELOCITY)
    # =========================================================================
    # Raw spike detection reacts within 100-600ms of Binance price moves.
    # This is faster than velocity-based detection which averages over 10s.

    def on_spike_detected(self, callback: SpikeCallback) -> None:
        """
        Register callback for when raw price spike is detected.

        The callback is fired IMMEDIATELY when price change exceeds threshold.
        This enables 100-600ms reaction time to Binance price moves.

        Args:
            callback: Function(direction, magnitude_pct, price) called on spike
                      direction: "UP" if price rising, "DOWN" if falling
                      magnitude_pct: Absolute percentage change (e.g., 0.05 for 0.05%)
                      price: Current Binance price

        Example:
            def on_spike(direction, magnitude_pct, price):
                if direction == "UP":
                    # Buy UP immediately - BTC is spiking up
                    asyncio.create_task(buy_winner("UP", magnitude_pct))

            binance.on_spike_detected(on_spike)
        """
        self._spike_callbacks.append(callback)
        logger.info(f"Registered spike callback (total: {len(self._spike_callbacks)})")

    def remove_spike_callback(self, callback: SpikeCallback) -> bool:
        """Remove a previously registered spike callback."""
        try:
            self._spike_callbacks.remove(callback)
            return True
        except ValueError:
            return False

    def clear_spike_callbacks(self) -> None:
        """Remove all spike callbacks."""
        self._spike_callbacks.clear()
        logger.info("Cleared all spike callbacks")

    def reset_spike_state(self) -> None:
        """
        Reset spike detection state for new market.

        Call this when switching to a new market.
        """
        self._spike_price_history = []
        self._last_spike_callback_time = 0.0
        logger.debug("Reset spike state for new market")

    def detect_spike(self, binance_price: float) -> tuple:
        """
        Detect raw Binance price spike over last N ticks.

        Args:
            price: Current Binance BTCUSDT price

        Returns:
            (direction, magnitude_pct) or (None, 0) if no spike
        """
        # Note: Buffer is filled by WebSocket handler, not here (Feb 2, 2026)
        # This avoids duplicate appends when callbacks are registered

        # Need enough history
        if len(self._spike_price_history) < self._spike_lookback + 1:
            return None, 0

        current = self._spike_price_history[-1]
        previous = self._spike_price_history[-self._spike_lookback - 1]

        if previous <= 0:
            return None, 0

        change_pct = (current - previous) / previous * 100
        magnitude = abs(change_pct)

        if magnitude >= self._spike_threshold:
            direction = "UP" if change_pct > 0 else "DOWN"
            return direction, magnitude

        return None, 0

    @property
    def spike_price_history(self) -> List[float]:
        """
        Expose the spike price history buffer for external use.

        This allows strategies to share the 60Hz price buffer instead of
        maintaining their own buffer that only fills at the slower trading loop rate.

        Returns:
            List of recent BTC prices updated at WebSocket rate (~60Hz)
        """
        return self._spike_price_history

    @property
    def ewma_price(self) -> Optional[float]:
        """
        Get current EWMA price state (Feb 3, 2026).

        CRITICAL: This EWMA is updated at ~60Hz (on every unique price tick) to match
        backtest behavior. Strategies should use this instead of maintaining their own
        EWMA state that only updates at the 5-second trading loop rate.

        Returns:
            Current EWMA price, or None if not yet initialized
        """
        return self._ewma_price

    @property
    def ewma_alpha(self) -> float:
        """Get EWMA alpha (decay rate) for diagnostics."""
        return self._ewma_alpha

    def reset_ewma_state(self) -> None:
        """
        Reset EWMA state for new market or reconnection.

        Call this when switching markets or after a long gap in data.
        """
        self._ewma_price = None
        self._last_ewma_timestamp_ms = 0
        logger.debug("Reset EWMA state")

    def get_buffer_diagnostics(self) -> dict:
        """
        Get buffer diagnostics for debugging (Feb 2, 2026).

        Returns:
            dict with buffer_size, tick_count, ticks_per_sec, expected_ticks_per_sec
        """
        import time
        now = time.time()

        # Calculate tick rate since last check
        elapsed = now - self._last_diagnostic_time if self._last_diagnostic_time > 0 else 0
        ticks_since_last = self._tick_count - self._last_diagnostic_tick_count

        ticks_per_sec = ticks_since_last / elapsed if elapsed > 0 else 0

        # Update diagnostic tracking
        self._last_diagnostic_tick_count = self._tick_count
        self._last_diagnostic_time = now

        return {
            "buffer_size": len(self._spike_price_history),
            "buffer_capacity": self._spike_history_size,
            "tick_count": self._tick_count,
            "ticks_per_sec": round(ticks_per_sec, 1),
            "expected_ticks_per_sec": 60,  # bookTicker is ~60Hz
            "is_healthy": ticks_per_sec >= 30 if elapsed > 5 else True,  # At least 50% of expected
        }

    def _check_spike_and_fire(self, price: float) -> None:
        """
        Check for spike and fire callbacks when threshold exceeded.

        Called on every Binance WebSocket tick. Uses fixed lookback window.
        DEPRECATED: Use _check_spike_ewma_and_fire for better signal quality.
        """
        import time

        # Detect spike
        direction, magnitude = self.detect_spike(price)

        if direction is None:
            return

        # Cooldown to prevent callback spam
        now = time.time()
        if (now - self._last_spike_callback_time) < self._spike_callback_cooldown_secs:
            return  # Still in cooldown

        # Fire callbacks
        ewma_price = self._ewma_price or price  # Use current price if EWMA not ready
        logger.info(
            f"[EVENT] Spike detected: {direction} {magnitude:.4f}% | "
            f"price=${price:,.2f}"
        )
        self._last_spike_callback_time = now

        for callback in self._spike_callbacks:
            try:
                # Handle both sync and async callbacks
                # Pass ewma_price as 4th argument for new callback signature
                result = callback(direction, magnitude, price, ewma_price)
                if asyncio.iscoroutine(result):
                    # Schedule async callback without blocking
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"Spike callback error: {e}")

    def _check_spike_ewma_and_fire(self, price: float) -> None:
        """
        Check for EWMA-based spike and fire callbacks when threshold exceeded.

        Uses EWMA deviation for spike detection instead of fixed lookback window.
        This provides better signal quality as EWMA adapts after spikes, reducing
        redundant signals from the same price move.

        Key advantages over fixed lookback:
        - One price move → one spike (not 14 duplicate signals)
        - EWMA_1000 (1000ms half-life) validated as winner on OOS7-9

        Called on every unique Binance WebSocket tick (~60Hz).
        """
        import time

        # Need EWMA to be initialized
        if self._ewma_price is None:
            return

        # Calculate deviation from EWMA
        change_pct = (price - self._ewma_price) / self._ewma_price * 100
        magnitude = abs(change_pct)

        # Check if spike exceeds threshold
        if magnitude < self._spike_threshold:
            return

        # Determine direction
        direction = "UP" if change_pct > 0 else "DOWN"

        # Cooldown to prevent callback spam (0.5s between callbacks)
        now = time.time()
        if (now - self._last_spike_callback_time) < self._spike_callback_cooldown_secs:
            return  # Still in cooldown

        # Fire callbacks
        ewma_price = self._ewma_price
        logger.info(
            f"[SPIKE_EVENT] EWMA spike: {direction} {magnitude:.4f}% | "
            f"price=${price:,.2f}, ewma=${ewma_price:,.2f}"
        )
        self._last_spike_callback_time = now

        for callback in self._spike_callbacks:
            try:
                # Handle both sync and async callbacks
                # Signature: callback(direction, magnitude_pct, price, ewma_price)
                result = callback(direction, magnitude, price, ewma_price)
                if asyncio.iscoroutine(result):
                    # Schedule async callback without blocking
                    asyncio.create_task(result)
            except Exception as e:
                logger.error(f"EWMA spike callback error: {e}")
