"""
BTC Market data model.

Represents a BTC 15-minute Up/Down market from Polymarket.
These markets resolve based on whether Bitcoin price goes up or down
during a 15-minute window.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List
import json


@dataclass
class BTCMarket:
    """
    Represents a BTC 15-minute Up/Down market.

    Attributes:
        condition_id: Unique market identifier (hex string)
        question: Market question text (e.g., "Bitcoin Up or Down - Dec 19, 1:00PM ET")
        slug: URL-friendly market identifier (e.g., "btc-updown-15m-1766167200")
        up_token_id: Token ID for the "Up" outcome
        down_token_id: Token ID for the "Down" outcome
        start_time: When the 15-minute window begins
        end_time: When the market resolves
        accepting_orders: Whether the market is currently accepting trades
        best_bid: Current best bid price (0.0-1.0)
        best_ask: Current best ask price (0.0-1.0)
        liquidity: Total liquidity in the market (USD)

    Example:
        market = BTCMarket(
            condition_id="0x148c3cb...",
            question="Bitcoin Up or Down - December 19, 1:00PM-1:15PM ET",
            slug="btc-updown-15m-1766167200",
            up_token_id="116407338...",
            down_token_id="926052176...",
            start_time=datetime(2025, 12, 19, 18, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2025, 12, 19, 18, 15, 0, tzinfo=timezone.utc),
            accepting_orders=True,
        )
    """

    condition_id: str
    question: str
    slug: str
    up_token_id: str
    down_token_id: str
    start_time: datetime
    end_time: datetime
    accepting_orders: bool = False
    best_bid: float = 0.0
    best_ask: float = 1.0
    liquidity: float = 0.0

    @classmethod
    def from_gamma_api(cls, data: dict) -> "BTCMarket":
        """
        Create a BTCMarket from gamma API response data.

        Args:
            data: Market data from gamma-api.polymarket.com/events

        Returns:
            BTCMarket instance

        Raises:
            ValueError: If required fields are missing or invalid
        """
        # Handle nested market data (gamma API returns events with markets inside)
        market_data = data
        if "markets" in data and data["markets"]:
            markets = data["markets"]
            if isinstance(markets, list):
                market_data = markets[0]
            elif isinstance(markets, dict):
                market_data = markets

        # Extract token IDs from clobTokenIds JSON string or list
        clob_tokens = market_data.get("clobTokenIds", "[]")
        if isinstance(clob_tokens, str):
            token_ids = json.loads(clob_tokens)
        else:
            token_ids = clob_tokens

        if len(token_ids) < 2:
            raise ValueError(f"Market {market_data.get('slug')} has insufficient tokens")

        # Parse timestamps
        start_time_str = market_data.get("eventStartTime") or market_data.get("startDate")
        end_time_str = market_data.get("endDate")

        if not start_time_str or not end_time_str:
            raise ValueError(f"Market {market_data.get('slug')} missing time data")

        # Parse ISO timestamps
        start_time = cls._parse_timestamp(start_time_str)

        # For BTC 15-min markets, prefer slug-based end_time (API endDate is often wrong)
        # Slug format: btc-updown-15m-TIMESTAMP where TIMESTAMP is the market START time
        # End time = start time + 15 minutes (900 seconds)
        slug = market_data.get("slug", "")
        end_time = None
        if "15m" in slug:
            try:
                parts = slug.split("-")
                if len(parts) >= 4:
                    start_timestamp = int(parts[-1])
                    # Add 15 minutes to get end time
                    end_time = datetime.fromtimestamp(start_timestamp + 900, tz=timezone.utc)
            except (ValueError, IndexError):
                pass

        # Fallback to API endDate if slug parsing failed
        if end_time is None:
            end_time = cls._parse_timestamp(end_time_str)

        return cls(
            condition_id=market_data.get("conditionId", ""),
            question=market_data.get("question", ""),
            slug=market_data.get("slug", ""),
            up_token_id=token_ids[0],  # First token is "Up"
            down_token_id=token_ids[1],  # Second token is "Down"
            start_time=start_time,
            end_time=end_time,
            accepting_orders=market_data.get("acceptingOrders", False),
            best_bid=float(market_data.get("bestBid", 0) or 0),
            best_ask=float(market_data.get("bestAsk", 1) or 1),
            liquidity=float(market_data.get("liquidityClob", 0) or market_data.get("liquidity", 0) or 0),
        )

    @staticmethod
    def _parse_timestamp(ts: str) -> datetime:
        """Parse ISO timestamp string to datetime."""
        # Handle various formats
        ts = ts.replace("Z", "+00:00")
        if "+" not in ts and ts.endswith("00"):
            ts = ts[:-2] + "+00:00"

        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            # Fallback: try without timezone
            ts_clean = ts.split("+")[0].split("Z")[0]
            dt = datetime.fromisoformat(ts_clean)
            return dt.replace(tzinfo=timezone.utc)

    def time_remaining(self) -> float:
        """
        Get seconds remaining until market resolution.

        Returns:
            Seconds until end_time. Negative if market has ended.
        """
        now = datetime.now(timezone.utc)
        delta = self.end_time - now
        return delta.total_seconds()

    def time_until_start(self) -> float:
        """
        Get seconds until the 15-minute window begins.

        Returns:
            Seconds until start_time. Negative if already started.
        """
        now = datetime.now(timezone.utc)
        delta = self.start_time - now
        return delta.total_seconds()

    def is_expired(self) -> bool:
        """
        Check if the market has ended.

        Returns:
            True if current time is past end_time
        """
        return self.time_remaining() <= 0

    def is_active(self) -> bool:
        """
        Check if the market is currently active for trading.

        Returns:
            True if accepting orders and not expired
        """
        return self.accepting_orders and not self.is_expired()

    def is_15min_market(self) -> bool:
        """
        Verify this is a 15-minute market based on slug pattern.

        Returns:
            True if slug contains '15m' pattern
        """
        return "15m" in self.slug or "15-min" in self.slug.lower()

    @property
    def spread(self) -> float:
        """
        Calculate bid-ask spread.

        Returns:
            Spread as decimal (e.g., 0.02 for 2% spread)
        """
        return self.best_ask - self.best_bid

    @property
    def pair_cost(self) -> float:
        """
        Calculate pair cost (Up ask + Down ask).

        For a profitable arbitrage, pair_cost should be < $1.00.
        Note: This is an approximation since we need both orderbooks.
        When best_ask is available, it represents the Up token's ask.
        The Down token's ask would be approximately (1 - best_bid).

        Returns:
            Estimated pair cost
        """
        # If spread is very tight (competitive market), pair cost is close to $1
        # For wider spreads, there may be arbitrage opportunity
        # This is a rough estimate - actual pair cost requires both orderbooks
        return self.best_ask + (1 - self.best_bid)

    def __repr__(self) -> str:
        """String representation."""
        remaining = int(self.time_remaining())
        status = "active" if self.is_active() else "inactive"
        return (
            f"BTCMarket('{self.slug}', {status}, "
            f"remaining={remaining}s, spread={self.spread:.2%})"
        )

    def __str__(self) -> str:
        """Human-readable string."""
        remaining = int(self.time_remaining())
        mins, secs = divmod(abs(remaining), 60)
        time_str = f"{mins}m {secs}s"
        if remaining < 0:
            time_str = f"ended {time_str} ago"
        else:
            time_str = f"{time_str} remaining"

        return (
            f"{self.question}\n"
            f"  Status: {'Active' if self.is_active() else 'Inactive'}\n"
            f"  Time: {time_str}\n"
            f"  Spread: {self.spread:.1%} (bid={self.best_bid:.2f}, ask={self.best_ask:.2f})\n"
            f"  Liquidity: ${self.liquidity:,.2f}"
        )
