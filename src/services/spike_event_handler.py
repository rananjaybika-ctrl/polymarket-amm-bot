"""
Spike Event Handler - Event-driven spike signal validation and queuing.

Validates spike signals from BinanceClient and queues valid ones for execution.
This enables ~500ms response latency instead of ~5000ms with polling.

Key responsibilities:
1. Receive spike callbacks from BinanceClient (~60Hz)
2. Validate signals using strategy filters (velocity, OBI, time remaining)
3. Queue valid signals for the trading loop to execute

Architecture:
    BinanceClient (60Hz) --[spike callback]--> SpikeEventHandler
                                                    |
                                                    v
                                              Validate signal
                                                    |
                                                    v
                                              Queue to signal_queue
                                                    |
                                                    v
    Trading Loop (0.5s) <---[dequeue]--- signal_queue

Author: Claude Code
Date: February 4, 2026
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.strategies.enhanced_spike import EnhancedSpikeStrategy
    from src.services.trend_detector import TrendDetector

logger = logging.getLogger(__name__)


@dataclass
class SpikeSignal:
    """
    Validated spike signal ready for execution.

    Contains all information needed to execute an entry trade.
    """
    direction: str           # "UP" or "DOWN"
    magnitude: float         # Spike magnitude (absolute %)
    binance_price: float     # Binance price at detection
    ewma_price: float        # EWMA price at detection
    timestamp: float         # Detection time (time.time())
    velocity_bps: float      # Velocity at detection (bps/sec)

    # Optional validation metadata
    enhanced_score: float = 0.0  # Composite score from should_take_enhanced_signal
    reason: str = ""             # Reason for acceptance

    def age_ms(self) -> float:
        """Get signal age in milliseconds."""
        return (time.time() - self.timestamp) * 1000

    def is_stale(self, max_age_ms: float = 1000.0) -> bool:
        """Check if signal is too old to execute."""
        return self.age_ms() > max_age_ms


class SpikeEventHandler:
    """
    Validates spike signals and queues valid ones for execution.

    This handler bridges the gap between the high-frequency BinanceClient
    spike detection (~60Hz) and the trading loop (~2Hz with 0.5s interval).

    Key features:
    - Uses strategy's should_take_enhanced_signal() for velocity confirmation
    - Respects position state (skips if position already open)
    - Respects time remaining (skips if too close to resolution)
    - Thread-safe queueing for async execution

    Usage:
        handler = SpikeEventHandler(strategy, trend_detector, signal_queue)
        handler.set_market_context(market, time_remaining_getter)
        binance_client.on_spike_detected(handler.on_spike_detected)
    """

    # Default configuration
    DEFAULT_HIGH_ENTRY_THRESHOLD = 0.90  # Skip entries at/above this price
    DEFAULT_MIN_TIME_REMAINING = 90.0    # Skip if time_remaining < this
    DEFAULT_MAX_SIGNAL_AGE_MS = 1000.0   # Drop signals older than this

    def __init__(
        self,
        strategy: "EnhancedSpikeStrategy",
        trend_detector: "TrendDetector",
        signal_queue: asyncio.Queue,
        high_entry_threshold: float = DEFAULT_HIGH_ENTRY_THRESHOLD,
        min_time_remaining: float = DEFAULT_MIN_TIME_REMAINING,
        max_signal_age_ms: float = DEFAULT_MAX_SIGNAL_AGE_MS,
    ):
        """
        Initialize spike event handler.

        Args:
            strategy: EnhancedSpikeStrategy instance for signal validation
            trend_detector: TrendDetector for velocity calculation
            signal_queue: Queue to push validated signals to
            high_entry_threshold: Skip entries at/above this price
            min_time_remaining: Skip if time_remaining < this (seconds)
            max_signal_age_ms: Drop signals older than this (milliseconds)
        """
        self._strategy = strategy
        self._trend_detector = trend_detector
        self._signal_queue = signal_queue
        self._high_entry_threshold = high_entry_threshold
        self._min_time_remaining = min_time_remaining
        self._max_signal_age_ms = max_signal_age_ms

        # Market context (set via set_market_context)
        self._market: Optional[Any] = None
        self._time_remaining_getter: Optional[Callable[[], float]] = None

        # Statistics
        self._signals_received = 0
        self._signals_queued = 0
        self._signals_rejected = 0
        self._last_rejection_reason = ""

        logger.info(
            f"[SPIKE_HANDLER] Initialized: high_entry={high_entry_threshold}, "
            f"min_time={min_time_remaining}s, max_age={max_signal_age_ms}ms"
        )

    def set_market_context(
        self,
        market: Any,
        time_remaining_getter: Callable[[], float],
    ) -> None:
        """
        Set market context for signal validation.

        Must be called before processing spike signals.
        Call again when switching to a new market.

        Args:
            market: Current market being traded
            time_remaining_getter: Callable that returns seconds until resolution
        """
        self._market = market
        self._time_remaining_getter = time_remaining_getter

        # Reset statistics for new market
        self._signals_received = 0
        self._signals_queued = 0
        self._signals_rejected = 0

        logger.info(f"[SPIKE_HANDLER] Market context set: {market.slug if market else 'None'}")

    def clear_market_context(self) -> None:
        """Clear market context (call when switching markets)."""
        self._market = None
        self._time_remaining_getter = None

    async def on_spike_detected(
        self,
        direction: str,
        magnitude: float,
        price: float,
        ewma_price: float,
    ) -> None:
        """
        Callback from BinanceClient when EWMA spike is detected.

        This is called at ~60Hz rate when price deviates from EWMA.
        Validates the signal and queues it if valid.

        Args:
            direction: "UP" or "DOWN"
            magnitude: Absolute percentage deviation from EWMA
            price: Current Binance price
            ewma_price: Current EWMA price
        """
        self._signals_received += 1
        timestamp = time.time()

        # === VALIDATION CHECKS ===

        # 1. Check market context is set
        if self._market is None or self._time_remaining_getter is None:
            self._reject("No market context")
            return

        # 2. Check if position already exists (first_fill_side is set)
        if self._strategy.state.first_fill_side is not None:
            self._reject("Position already open")
            return

        # 3. Check time remaining
        time_remaining = self._time_remaining_getter()
        if time_remaining < self._min_time_remaining:
            self._reject(f"Time remaining {time_remaining:.0f}s < {self._min_time_remaining}s")
            return

        # 4. Get velocity from trend detector
        velocity_bps = 0.0
        if self._trend_detector:
            trend_signal = self._trend_detector.get_trend_signal()
            if trend_signal:
                velocity_bps = trend_signal.velocity_bps

        # 5. Apply enhanced signal filter (velocity confirmation)
        should_trade, score, reason = self._strategy.should_take_enhanced_signal(
            spike_dir=direction,
            spike_magnitude=magnitude,
            velocity_bps=velocity_bps,
            time_remaining=time_remaining,
            min_score=0.40,  # Default threshold
        )

        if not should_trade:
            self._reject(f"Enhanced filter: {reason}")
            return

        # 6. Check queue capacity (prevent unbounded growth)
        if self._signal_queue.full():
            self._reject("Signal queue full")
            return

        # === SIGNAL VALIDATED - QUEUE IT ===

        signal = SpikeSignal(
            direction=direction,
            magnitude=magnitude,
            binance_price=price,
            ewma_price=ewma_price,
            timestamp=timestamp,
            velocity_bps=velocity_bps,
            enhanced_score=score,
            reason=reason,
        )

        try:
            self._signal_queue.put_nowait(signal)
            self._signals_queued += 1

            logger.info(
                f"[SPIKE_EVENT] Queued: {direction} {magnitude:.4f}% | "
                f"vel={velocity_bps:.3f} bps | score={score:.3f} | "
                f"queue_size={self._signal_queue.qsize()}"
            )
        except asyncio.QueueFull:
            self._reject("Queue full on put")

    def _reject(self, reason: str) -> None:
        """Record rejection and update statistics."""
        self._signals_rejected += 1
        self._last_rejection_reason = reason

        # Log rejections sparingly (every 100th or on new reason)
        if self._signals_rejected % 100 == 1:
            logger.debug(f"[SPIKE_HANDLER] Rejected: {reason} (total={self._signals_rejected})")

    def get_statistics(self) -> dict:
        """Get handler statistics."""
        return {
            "signals_received": self._signals_received,
            "signals_queued": self._signals_queued,
            "signals_rejected": self._signals_rejected,
            "queue_size": self._signal_queue.qsize(),
            "last_rejection_reason": self._last_rejection_reason,
            "acceptance_rate": (
                self._signals_queued / self._signals_received * 100
                if self._signals_received > 0 else 0.0
            ),
        }

    @property
    def max_signal_age_ms(self) -> float:
        """Get maximum signal age before considered stale."""
        return self._max_signal_age_ms
