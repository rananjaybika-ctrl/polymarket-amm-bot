"""
Market Rotator service for managing trading sessions across multiple markets.

Handles automatic rotation between consecutive BTC 15-minute Up/Down markets.
Supports both session-based (limited) and continuous (24/7) operation modes.
Includes optional trading schedule for restricted hours/date ranges.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Callable
from enum import Enum

from src.models.market import BTCMarket
from src.models.schedule import TradingSchedule
from src.services.market_finder import MarketFinder


logger = logging.getLogger(__name__)


class RotationReason(Enum):
    """Reasons for market rotation."""
    MARKET_EXPIRED = "market_expired"
    MARKET_NOT_ACCEPTING = "market_not_accepting_orders"
    MANUAL_ROTATION = "manual_rotation"
    SESSION_START = "session_start"


class SessionEndReason(Enum):
    """Reasons for session ending."""
    MAX_MARKETS_REACHED = "max_markets_reached"
    MAX_DURATION_REACHED = "max_duration_reached"
    NO_NEXT_MARKET = "no_next_market"
    MANUAL_STOP = "manual_stop"
    ALL_MARKETS_EXPIRED = "all_markets_expired"
    OUTSIDE_SCHEDULE = "outside_trading_schedule"
    SCHEDULE_ENDED = "schedule_date_range_ended"


@dataclass
class RotationEvent:
    """Record of a market rotation."""
    timestamp: datetime
    from_market: Optional[BTCMarket]
    to_market: Optional[BTCMarket]
    reason: RotationReason


@dataclass
class SessionStats:
    """Statistics for a trading session."""
    start_time: datetime
    end_time: Optional[datetime] = None
    markets_traded: int = 0
    rotations: List[RotationEvent] = field(default_factory=list)
    end_reason: Optional[SessionEndReason] = None

    @property
    def duration(self) -> timedelta:
        """Session duration."""
        end = self.end_time or datetime.now(timezone.utc)
        return end - self.start_time

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        return self.duration.total_seconds()

    @property
    def duration_minutes(self) -> float:
        """Session duration in minutes."""
        return self.duration_seconds / 60

    def __str__(self) -> str:
        status = "Active" if self.end_time is None else "Ended"
        return (
            f"Session ({status})\n"
            f"  Duration: {self.duration_minutes:.1f} minutes\n"
            f"  Markets Traded: {self.markets_traded}\n"
            f"  Rotations: {len(self.rotations)}"
        )


class MarketRotator:
    """
    Manages rotation between consecutive BTC 15-minute markets.

    Supports two modes:
    - **Continuous mode** (continuous=True): Bot runs 24/7 with no limits.
      The 60-min window means "only consider markets ending within 60 mins".
    - **Session mode** (continuous=False): Bot stops after max_markets or max_duration.

    Optional trading schedule restricts when the bot actively trades.

    Example (continuous 24/7 operation):
        rotator = MarketRotator(finder, continuous=True)
        await rotator.start_session()

        while True:  # Runs forever
            market = rotator.current_market
            # ... trade on market ...

            if rotator.should_rotate():
                await rotator.rotate()

    Example (session-based with limits):
        rotator = MarketRotator(finder, continuous=False, max_markets=4)
        await rotator.start_session()

        while not rotator.is_session_complete():
            # ... trade ...

    Example (with trading schedule):
        from datetime import time, date
        from src.models.schedule import TradingSchedule

        # Trade only 9 AM - 5 PM ET, Dec 19-31
        schedule = TradingSchedule(
            start_time=time(9, 0),
            end_time=time(17, 0),
            start_date=date(2025, 12, 19),
            end_date=date(2025, 12, 31),
        )
        rotator = MarketRotator(finder, schedule=schedule)
    """

    DEFAULT_MAX_MARKETS = 4
    DEFAULT_MAX_DURATION_MINUTES = 60
    DEFAULT_MARKET_WINDOW_MINUTES = 60  # Only consider markets within this window

    def __init__(
        self,
        finder: MarketFinder,
        continuous: bool = True,
        max_markets: int = DEFAULT_MAX_MARKETS,
        max_duration_minutes: float = DEFAULT_MAX_DURATION_MINUTES,
        market_window_minutes: float = DEFAULT_MARKET_WINDOW_MINUTES,
        schedule: Optional[TradingSchedule] = None,
        on_rotation: Optional[Callable[[RotationEvent], None]] = None,
        session_start_utc: Optional[datetime] = None,
        session_end_utc: Optional[datetime] = None,
    ):
        """
        Initialize MarketRotator.

        Args:
            finder: MarketFinder for discovering markets
            continuous: If True, run 24/7 without stopping (ignores max_markets/max_duration)
            max_markets: Maximum markets per session (only used if continuous=False)
            max_duration_minutes: Maximum session duration (only used if continuous=False)
            market_window_minutes: Only consider markets ending within this window (default 60)
            schedule: Optional TradingSchedule to restrict trading hours/dates
            on_rotation: Callback when rotation occurs
            session_start_utc: Optional UTC start time for session - only trade markets ending AFTER this
            session_end_utc: Optional UTC end time for session - only trade markets ending BEFORE this
        """
        self.finder = finder
        self.continuous = continuous
        self.max_markets = max_markets
        self.max_duration_minutes = max_duration_minutes
        self.market_window_minutes = market_window_minutes
        self.schedule = schedule
        self.on_rotation = on_rotation

        # CRITICAL: Session time window for market selection enforcement
        self.session_start_utc = session_start_utc
        self.session_end_utc = session_end_utc

        # Session state
        self._current_market: Optional[BTCMarket] = None
        self._session_stats: Optional[SessionStats] = None
        self._available_markets: List[BTCMarket] = []
        self._session_active = False

        # Pre-fetched next market for instant rotation (<100ms)
        self._prefetched_market: Optional[BTCMarket] = None
        self._prefetch_slug: Optional[str] = None

    @property
    def current_market(self) -> Optional[BTCMarket]:
        """Currently active market, or None if no session."""
        return self._current_market

    @property
    def session_stats(self) -> Optional[SessionStats]:
        """Current session statistics."""
        return self._session_stats

    @property
    def is_session_active(self) -> bool:
        """Whether a session is currently active."""
        return self._session_active

    @property
    def markets_remaining(self) -> int:
        """Number of markets remaining in session."""
        if not self._session_stats:
            return self.max_markets
        return self.max_markets - self._session_stats.markets_traded

    @property
    def time_remaining(self) -> float:
        """Seconds remaining in session."""
        if not self._session_stats:
            return self.max_duration_minutes * 60

        elapsed = self._session_stats.duration_seconds
        max_seconds = self.max_duration_minutes * 60
        return max(0, max_seconds - elapsed)

    def is_trading_allowed(self) -> bool:
        """
        Check if trading is currently allowed by the schedule.

        Returns:
            True if no schedule set or if within scheduled trading hours/dates
        """
        if self.schedule is None:
            return True
        return self.schedule.is_active()

    def is_within_trading_hours(self) -> bool:
        """Check if current time is within daily trading hours."""
        if self.schedule is None:
            return True
        return self.schedule.is_within_hours()

    def is_within_trading_dates(self) -> bool:
        """Check if current date is within trading date range."""
        if self.schedule is None:
            return True
        return self.schedule.is_within_dates()

    def get_schedule_status(self) -> Optional[dict]:
        """
        Get current trading schedule status.

        Returns:
            Dict with schedule info, or None if no schedule set
        """
        if self.schedule is None:
            return None
        return self.schedule.get_status()

    def time_until_trading_active(self) -> Optional[timedelta]:
        """
        Get time until trading becomes active.

        Returns:
            Timedelta until active, or None if already active or no schedule
        """
        if self.schedule is None:
            return None
        return self.schedule.time_until_active()

    def time_until_trading_inactive(self) -> Optional[timedelta]:
        """
        Get time until trading becomes inactive.

        Returns:
            Timedelta until inactive, or None if already inactive or no end defined
        """
        if self.schedule is None:
            return None
        return self.schedule.time_until_inactive()

    async def start_session(self) -> bool:
        """
        Start a new trading session.

        Discovers available markets and initializes the first market.
        If session_start_utc and session_end_utc are set, uses time-range based
        market discovery to ONLY select markets within the configured window.

        Returns:
            True if session started successfully, False if no markets available
        """
        logger.info("Starting new trading session")

        # CRITICAL: Use time-range based discovery if session window is configured
        if self.session_start_utc and self.session_end_utc:
            logger.info(
                f"Using time-range market selection: "
                f"{self.session_start_utc.isoformat()} to {self.session_end_utc.isoformat()}"
            )
            self._available_markets = await self.finder.get_markets_in_time_range(
                start_utc=self.session_start_utc,
                end_utc=self.session_end_utc,
            )
        else:
            # Fallback to rolling window approach
            window_hours = self.market_window_minutes / 60.0
            self._available_markets = await self.finder.get_markets_in_window(
                hours=window_hours,
            )

        # If no markets in window, log warning but don't fall back to all markets
        # This prevents trading on markets that are hours away
        if not self._available_markets:
            logger.warning(
                f"No markets found within configured time window. "
                "Will not trade on out-of-range markets."
            )

        if not self._available_markets:
            logger.warning("No active markets found, cannot start session")
            return False

        # Initialize session
        self._session_stats = SessionStats(
            start_time=datetime.now(timezone.utc),
        )
        self._session_active = True

        # Set first market
        self._current_market = self._available_markets[0]
        self._session_stats.markets_traded = 1

        # Record rotation event
        event = RotationEvent(
            timestamp=datetime.now(timezone.utc),
            from_market=None,
            to_market=self._current_market,
            reason=RotationReason.SESSION_START,
        )
        self._session_stats.rotations.append(event)

        if self.on_rotation:
            self.on_rotation(event)

        logger.info(
            f"Session started with market: {self._current_market.slug}, "
            f"{len(self._available_markets)} markets available"
        )

        return True

    def should_rotate(self) -> bool:
        """
        Check if rotation to next market is needed.

        Returns:
            True if current market is expired or not accepting orders

        FIX Feb 1, 2026: Delay rotation until merge window ends (time_remaining < -20).
        This prevents race condition where rotation triggers at time_remaining=0,
        losing the 20-second post-market-end merge opportunity.
        """
        if not self._session_active or not self._current_market:
            return False

        time_left = self._current_market.time_remaining()

        # Check if current market expired
        # FIX: Don't rotate until AFTER merge window ends (time_remaining < -20)
        # Merge window is -20s to +10s, so we wait until -20s to rotate
        if self._current_market.is_expired():
            if time_left > -20:
                # Still in merge window, don't rotate yet
                logger.debug(f"Market {self._current_market.slug} expired but in merge window ({time_left:.0f}s) - waiting")
                return False
            logger.debug(f"Market {self._current_market.slug} expired and merge window closed ({time_left:.0f}s)")
            return True

        # Only rotate on accepting_orders=False if < 60 seconds remaining
        # Polymarket API sets accepting_orders=False 7-10 min early - ignore until near end
        if not self._current_market.accepting_orders:
            if time_left < 60:
                logger.debug(f"Market {self._current_market.slug} not accepting orders and {time_left:.0f}s remaining")
                return True
            else:
                logger.debug(f"Market {self._current_market.slug} not accepting orders but {time_left:.0f}s remaining - waiting")

        return False

    def get_rotation_reason(self) -> Optional[RotationReason]:
        """Get the reason rotation is needed, or None if not needed.

        FIX Feb 1, 2026: Consistent with should_rotate() - only return
        MARKET_EXPIRED after merge window closes (time_remaining < -20).
        """
        if not self._current_market:
            return None

        time_left = self._current_market.time_remaining()

        # FIX: Only count as expired after merge window closes
        if self._current_market.is_expired():
            if time_left < -20:  # Merge window closed
                return RotationReason.MARKET_EXPIRED
            return None  # Still in merge window

        # Only count as not accepting if < 60s remaining
        if not self._current_market.accepting_orders:
            if time_left < 60:
                return RotationReason.MARKET_NOT_ACCEPTING

        return None

    async def prefetch_next_market(self) -> Optional[BTCMarket]:
        """
        Pre-fetch the next market for instant rotation (<100ms).

        Call this periodically during trading to have the next market ready.
        When rotation happens, use the pre-fetched market if available.

        Returns:
            Pre-fetched BTCMarket, or None if pre-fetch failed
        """
        if not self._current_market:
            return None

        # Calculate next market slug from current market
        try:
            current_slug = self._current_market.slug
            parts = current_slug.split("-")
            if len(parts) >= 4 and "15m" in current_slug:
                current_ts = int(parts[-1])
                next_ts = current_ts + 900  # 15 minutes later
                next_slug = f"btc-updown-15m-{next_ts}"

                # Only fetch if not already pre-fetched
                if self._prefetch_slug != next_slug:
                    self._prefetched_market = await self.finder.get_market_by_slug(next_slug)
                    self._prefetch_slug = next_slug

                    if self._prefetched_market:
                        logger.debug(f"[PREFETCH] Pre-fetched next market: {next_slug}")
                    else:
                        logger.debug(f"[PREFETCH] Failed to pre-fetch: {next_slug}")

                return self._prefetched_market

        except Exception as e:
            logger.warning(f"[PREFETCH] Error pre-fetching next market: {e}")

        return None

    @property
    def has_prefetched_market(self) -> bool:
        """Check if a market is pre-fetched and ready for instant rotation."""
        return self._prefetched_market is not None

    async def rotate(self, reason: Optional[RotationReason] = None) -> bool:
        """
        Rotate to the next market.

        Uses pre-fetched market if available for instant rotation (<100ms).

        Args:
            reason: Rotation reason (auto-detected if not provided)

        Returns:
            True if rotation successful, False if no next market or session ended
        """
        if not self._session_active:
            logger.warning("Cannot rotate: no active session")
            return False

        # Check session limits
        if self.is_session_complete():
            logger.info("Session complete, cannot rotate")
            return False

        # Determine rotation reason
        if reason is None:
            reason = self.get_rotation_reason() or RotationReason.MANUAL_ROTATION

        # Use pre-fetched market if available (instant rotation)
        next_market = None
        if self._prefetched_market:
            next_market = self._prefetched_market
            self._prefetched_market = None
            self._prefetch_slug = None
            logger.info(f"[ROTATION] Using pre-fetched market: {next_market.slug} (instant)")
        else:
            # Fallback to finding next market (slower)
            next_market = await self._find_next_market()

        if not next_market:
            logger.warning("No next market available")
            await self.end_session(SessionEndReason.NO_NEXT_MARKET)
            return False

        # Perform rotation
        old_market = self._current_market
        self._current_market = next_market
        self._session_stats.markets_traded += 1

        # Record event
        event = RotationEvent(
            timestamp=datetime.now(timezone.utc),
            from_market=old_market,
            to_market=next_market,
            reason=reason,
        )
        self._session_stats.rotations.append(event)

        if self.on_rotation:
            self.on_rotation(event)

        logger.info(
            f"Rotated from {old_market.slug if old_market else 'None'} "
            f"to {next_market.slug} ({reason.value})"
        )

        return True

    async def _find_next_market(self) -> Optional[BTCMarket]:
        """Find the next market to rotate to within the configured time window."""
        # CRITICAL: Use time-range based discovery if session window is configured
        if self.session_start_utc and self.session_end_utc:
            self._available_markets = await self.finder.get_markets_in_time_range(
                start_utc=self.session_start_utc,
                end_utc=self.session_end_utc,
            )
        else:
            # Fallback to rolling window approach
            window_hours = self.market_window_minutes / 60.0
            self._available_markets = await self.finder.get_markets_in_window(
                hours=window_hours,
            )

        # Filter out current market and expired markets
        candidates = [
            m for m in self._available_markets
            if not m.is_expired()
            and m.slug != (self._current_market.slug if self._current_market else None)
        ]

        if not candidates:
            logger.info("No more markets available within configured time window")
            return None

        # Return the one ending soonest
        candidates.sort(key=lambda m: m.end_time)
        next_market = candidates[0]
        logger.debug(f"Next market candidate: {next_market.slug}")
        return next_market

    def is_session_complete(self) -> bool:
        """
        Check if the session has reached its limits.

        In continuous mode, returns False unless outside trading schedule.
        In session mode, returns True if max_markets or max_duration reached.

        Returns:
            True if session should end, False otherwise
        """
        if not self._session_active or not self._session_stats:
            return True

        # Check trading schedule (applies to both modes)
        if self.schedule and not self.schedule.is_active():
            return True

        # Continuous mode never ends automatically (except schedule)
        if self.continuous:
            return False

        # Session mode: check limits
        if self._session_stats.markets_traded >= self.max_markets:
            return True

        if self._session_stats.duration_minutes >= self.max_duration_minutes:
            return True

        return False

    def get_session_end_reason(self) -> Optional[SessionEndReason]:
        """Get why session should end, or None if still valid."""
        if not self._session_stats:
            return None

        # Check trading schedule first (applies to both modes)
        if self.schedule and not self.schedule.is_active():
            # Distinguish between outside hours vs date range ended
            if not self.schedule.is_within_dates():
                return SessionEndReason.SCHEDULE_ENDED
            return SessionEndReason.OUTSIDE_SCHEDULE

        # Continuous mode: only ends if all markets expired (no time/count limits)
        if self.continuous:
            if self._current_market and self._current_market.is_expired():
                if not self._available_markets or all(m.is_expired() for m in self._available_markets):
                    return SessionEndReason.ALL_MARKETS_EXPIRED
            return None

        # Session mode: check limits
        if self._session_stats.markets_traded >= self.max_markets:
            return SessionEndReason.MAX_MARKETS_REACHED

        if self._session_stats.duration_minutes >= self.max_duration_minutes:
            return SessionEndReason.MAX_DURATION_REACHED

        # Check if all markets expired
        if self._current_market and self._current_market.is_expired():
            if not self._available_markets or all(m.is_expired() for m in self._available_markets):
                return SessionEndReason.ALL_MARKETS_EXPIRED

        return None

    async def end_session(self, reason: Optional[SessionEndReason] = None) -> SessionStats:
        """
        End the current session.

        Args:
            reason: Why session is ending

        Returns:
            Final session statistics
        """
        if not self._session_stats:
            raise RuntimeError("No active session to end")

        # Determine end reason
        if reason is None:
            reason = self.get_session_end_reason() or SessionEndReason.MANUAL_STOP

        # Finalize stats
        self._session_stats.end_time = datetime.now(timezone.utc)
        self._session_stats.end_reason = reason

        # Clear state
        self._session_active = False
        self._current_market = None

        logger.info(
            f"Session ended: {reason.value}, "
            f"traded {self._session_stats.markets_traded} markets in "
            f"{self._session_stats.duration_minutes:.1f} minutes"
        )

        return self._session_stats

    async def run_session(
        self,
        on_market: Optional[Callable[[BTCMarket], None]] = None,
        check_interval: float = 5.0,
        wait_for_schedule: bool = True,
    ) -> SessionStats:
        """
        Run a complete trading session with automatic rotation.

        This is a convenience method that handles the full session lifecycle.
        Respects trading schedule and can optionally wait for schedule to become active.

        Args:
            on_market: Callback when a new market becomes active
            check_interval: Seconds between rotation checks
            wait_for_schedule: If True, wait for schedule to become active instead of failing

        Returns:
            Final session statistics
        """
        import asyncio

        # Wait for trading schedule if configured
        if self.schedule and wait_for_schedule:
            while not self.schedule.is_active():
                wait_time = self.schedule.time_until_active()
                if wait_time is None:
                    # Schedule has ended permanently
                    raise RuntimeError("Trading schedule has ended")

                logger.info(f"Outside trading schedule, waiting {wait_time}...")
                # Wait in smaller increments to allow cancellation
                wait_seconds = min(wait_time.total_seconds(), 60)
                await asyncio.sleep(wait_seconds)

        if not await self.start_session():
            raise RuntimeError("Failed to start session: no markets available")

        try:
            while not self.is_session_complete():
                # Notify of current market
                if on_market and self._current_market:
                    on_market(self._current_market)

                # Wait and check for rotation
                await asyncio.sleep(check_interval)

                if self.should_rotate():
                    if not await self.rotate():
                        break  # Session ended

        finally:
            if self._session_active:
                return await self.end_session()

        return self._session_stats

    def __repr__(self) -> str:
        status = "active" if self._session_active else "inactive"
        market = self._current_market.slug if self._current_market else "None"
        schedule_str = ""
        if self.schedule:
            schedule_str = f", schedule={'active' if self.schedule.is_active() else 'inactive'}"
        return f"MarketRotator({status}, market={market}{schedule_str})"
