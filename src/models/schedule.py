"""
Trading schedule models for controlling bot operating hours.

Supports:
- Daily trading hours (e.g., 9:00 AM - 5:00 PM ET)
- Date range (e.g., Dec 19 - Dec 31, 2025)
- Timezone-aware scheduling (defaults to US/Eastern for Polymarket)
"""

from dataclasses import dataclass, field
from datetime import datetime, date, time, timezone, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


# Polymarket markets use Eastern Time
ET = ZoneInfo("America/New_York")
UTC = timezone.utc


@dataclass
class TradingSchedule:
    """
    Defines when the bot should be actively trading.

    Examples:
        # Trade 24/7 (default)
        schedule = TradingSchedule()

        # Trade only during market hours (9:30 AM - 4:00 PM ET)
        schedule = TradingSchedule(
            start_time=time(9, 30),
            end_time=time(16, 0),
        )

        # Trade specific date range
        schedule = TradingSchedule(
            start_date=date(2025, 12, 19),
            end_date=date(2025, 12, 31),
        )

        # Combine both
        schedule = TradingSchedule(
            start_time=time(9, 0),
            end_time=time(17, 0),
            start_date=date(2025, 12, 19),
            end_date=date(2025, 12, 25),
        )

    Attributes:
        start_time: Daily start time (None = midnight/00:00)
        end_time: Daily end time (None = midnight/00:00, meaning end of day)
        start_date: First date to trade (None = no start limit)
        end_date: Last date to trade (None = no end limit)
        timezone: Timezone for schedule (default: America/New_York)
        enabled: Whether schedule restrictions are active
    """

    start_time: Optional[time] = None  # None = 00:00 (start of day)
    end_time: Optional[time] = None    # None = 23:59:59 (end of day)
    start_date: Optional[date] = None  # None = no start limit
    end_date: Optional[date] = None    # None = no end limit
    timezone: ZoneInfo = field(default_factory=lambda: ET)
    enabled: bool = True

    def __post_init__(self):
        """Validate schedule configuration."""
        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                raise ValueError(
                    f"start_time ({self.start_time}) must be before end_time ({self.end_time})"
                )

        if self.start_date and self.end_date:
            if self.start_date > self.end_date:
                raise ValueError(
                    f"start_date ({self.start_date}) must be before end_date ({self.end_date})"
                )

    @property
    def is_24_7(self) -> bool:
        """Whether schedule allows 24/7 trading (no restrictions)."""
        return (
            not self.enabled
            or (
                self.start_time is None
                and self.end_time is None
                and self.start_date is None
                and self.end_date is None
            )
        )

    def now_in_tz(self) -> datetime:
        """Get current time in schedule's timezone."""
        return datetime.now(self.timezone)

    def is_within_hours(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if given time is within daily trading hours.

        Args:
            dt: Datetime to check (default: current time)

        Returns:
            True if within trading hours
        """
        if not self.enabled:
            return True

        if self.start_time is None and self.end_time is None:
            return True

        if dt is None:
            dt = self.now_in_tz()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        else:
            dt = dt.astimezone(self.timezone)

        current_time = dt.time()

        start = self.start_time or time(0, 0, 0)
        end = self.end_time or time(23, 59, 59)

        return start <= current_time <= end

    def is_within_dates(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if given date is within trading date range.

        Args:
            dt: Datetime to check (default: current time)

        Returns:
            True if within date range
        """
        if not self.enabled:
            return True

        if self.start_date is None and self.end_date is None:
            return True

        if dt is None:
            dt = self.now_in_tz()
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.timezone)
        else:
            dt = dt.astimezone(self.timezone)

        current_date = dt.date()

        if self.start_date and current_date < self.start_date:
            return False

        if self.end_date and current_date > self.end_date:
            return False

        return True

    def is_active(self, dt: Optional[datetime] = None) -> bool:
        """
        Check if trading should be active at the given time.

        Combines both hours and date range checks.

        Args:
            dt: Datetime to check (default: current time)

        Returns:
            True if within both trading hours and date range
        """
        if not self.enabled:
            return True

        return self.is_within_hours(dt) and self.is_within_dates(dt)

    def time_until_active(self) -> Optional[timedelta]:
        """
        Calculate time until trading becomes active.

        Returns:
            Timedelta until active, or None if already active or schedule disabled
        """
        if not self.enabled or self.is_active():
            return None

        now = self.now_in_tz()
        current_date = now.date()
        current_time = now.time()

        # Check if we need to wait for start_date
        if self.start_date and current_date < self.start_date:
            # Calculate time until start_date at start_time
            start_dt = datetime.combine(
                self.start_date,
                self.start_time or time(0, 0, 0),
                tzinfo=self.timezone,
            )
            return start_dt - now

        # Check if we need to wait for start_time (same day)
        if self.start_time and current_time < self.start_time:
            start_dt = datetime.combine(current_date, self.start_time, tzinfo=self.timezone)
            return start_dt - now

        # We're past end_time today, calculate until tomorrow's start_time
        if self.end_time and current_time > self.end_time:
            tomorrow = current_date + timedelta(days=1)

            # Check if tomorrow is past end_date
            if self.end_date and tomorrow > self.end_date:
                return None  # Schedule has ended

            start_dt = datetime.combine(
                tomorrow,
                self.start_time or time(0, 0, 0),
                tzinfo=self.timezone,
            )
            return start_dt - now

        return None

    def time_until_inactive(self) -> Optional[timedelta]:
        """
        Calculate time until trading becomes inactive.

        Returns:
            Timedelta until inactive, or None if already inactive or no end defined
        """
        if not self.enabled or not self.is_active():
            return None

        now = self.now_in_tz()
        current_date = now.date()

        # If we have an end_time today
        if self.end_time:
            end_dt = datetime.combine(current_date, self.end_time, tzinfo=self.timezone)
            if end_dt > now:
                return end_dt - now

        # If we have an end_date
        if self.end_date:
            end_of_day = datetime.combine(
                self.end_date,
                self.end_time or time(23, 59, 59),
                tzinfo=self.timezone,
            )
            if end_of_day > now:
                return end_of_day - now

        return None

    def get_status(self) -> dict:
        """
        Get current schedule status.

        Returns:
            Dict with active status, next state change, and schedule info
        """
        now = self.now_in_tz()
        is_active = self.is_active()

        status = {
            "enabled": self.enabled,
            "is_active": is_active,
            "current_time": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "timezone": str(self.timezone),
        }

        if self.start_time or self.end_time:
            status["trading_hours"] = {
                "start": self.start_time.strftime("%H:%M") if self.start_time else "00:00",
                "end": self.end_time.strftime("%H:%M") if self.end_time else "23:59",
            }

        if self.start_date or self.end_date:
            status["date_range"] = {
                "start": self.start_date.isoformat() if self.start_date else None,
                "end": self.end_date.isoformat() if self.end_date else None,
            }

        if is_active:
            until_inactive = self.time_until_inactive()
            if until_inactive:
                status["active_for"] = str(until_inactive).split(".")[0]  # Remove microseconds
        else:
            until_active = self.time_until_active()
            if until_active:
                status["inactive_for"] = str(until_active).split(".")[0]

        return status

    def __str__(self) -> str:
        """Human-readable schedule description."""
        if not self.enabled:
            return "Schedule: Disabled (24/7 trading)"

        if self.is_24_7:
            return "Schedule: 24/7 (no restrictions)"

        parts = []

        if self.start_time or self.end_time:
            start = self.start_time.strftime("%I:%M %p") if self.start_time else "12:00 AM"
            end = self.end_time.strftime("%I:%M %p") if self.end_time else "11:59 PM"
            parts.append(f"{start} - {end} {self.timezone}")

        if self.start_date or self.end_date:
            start = self.start_date.strftime("%b %d, %Y") if self.start_date else "..."
            end = self.end_date.strftime("%b %d, %Y") if self.end_date else "..."
            parts.append(f"{start} to {end}")

        return "Schedule: " + " | ".join(parts)

    def __repr__(self) -> str:
        return (
            f"TradingSchedule("
            f"hours={self.start_time}-{self.end_time}, "
            f"dates={self.start_date} to {self.end_date}, "
            f"tz={self.timezone})"
        )
