"""
Market Finder service for discovering BTC 15-minute Up/Down markets.

This service queries the Polymarket gamma API to find active BTC 15-minute
markets and provides methods for market discovery and rotation.
"""

import asyncio
import logging
from typing import List, Optional, TypeVar, Callable, Any
from datetime import datetime, timezone
import httpx

from src.models.market import BTCMarket


logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    **kwargs: Any,
) -> T:
    """
    Retry an async function with exponential backoff.

    Args:
        func: Async function to retry
        max_retries: Maximum number of retry attempts
        delay: Initial delay between retries (seconds)
        backoff: Multiplier for delay after each retry
        *args, **kwargs: Arguments to pass to func

    Returns:
        Result of the function

    Raises:
        The last exception if all retries fail
    """
    last_exception = None
    current_delay = delay

    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}. "
                    f"Retrying in {current_delay:.1f}s..."
                )
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(f"All {max_retries + 1} attempts failed: {e}")

    raise last_exception  # type: ignore


class MarketFinderError(Exception):
    """Base exception for MarketFinder errors."""
    pass


class NoMarketsFoundError(MarketFinderError):
    """Raised when no matching markets are found."""
    pass


class MarketFinder:
    """
    Service for finding BTC 15-minute Up/Down markets on Polymarket.

    Uses the gamma API (gamma-api.polymarket.com) to discover markets.
    The gamma API provides richer event data than the CLOB API.

    Attributes:
        gamma_api_url: Base URL for the gamma API

    Example:
        finder = MarketFinder()
        markets = await finder.find_btc_15min_markets()
        active = await finder.get_active_market()
    """

    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    MARKET_INTERVAL_SECONDS = 900  # 15 minutes

    def __init__(self, timeout: float = 30.0, max_retries: int = 3):
        """
        Initialize MarketFinder.

        Args:
            timeout: HTTP request timeout in seconds
            max_retries: Maximum retry attempts for failed requests
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _fetch_with_retry(self, url: str) -> List[dict]:
        """
        Fetch JSON from URL with retry logic.

        Args:
            url: URL to fetch

        Returns:
            JSON response as list

        Raises:
            httpx.HTTPError: If all retries fail
        """
        client = await self._get_client()

        async def do_fetch() -> List[dict]:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []

        return await retry_async(do_fetch, max_retries=self.max_retries)

    @staticmethod
    def _get_current_window_timestamp(now: Optional[datetime] = None) -> int:
        """
        Calculate the start timestamp of the current 15-minute window.

        BTC 15-minute markets run on fixed intervals (00, 15, 30, 45 minutes).
        This returns the Unix timestamp for the start of the current window.

        Args:
            now: Optional datetime, defaults to current UTC time

        Returns:
            Unix timestamp of current window start
        """
        if now is None:
            now = datetime.now(timezone.utc)

        current_ts = int(now.timestamp())
        # Round down to nearest 15-minute interval
        window_start = (current_ts // 900) * 900
        return window_start

    @staticmethod
    def _generate_market_slugs(count: int = 5, now: Optional[datetime] = None) -> List[str]:
        """
        Generate slugs for current and upcoming BTC 15-minute markets.

        Args:
            count: Number of market slugs to generate (including current)
            now: Optional datetime, defaults to current UTC time

        Returns:
            List of market slugs, starting with current window
        """
        if now is None:
            now = datetime.now(timezone.utc)

        current_ts = int(now.timestamp())
        # Slug timestamp is the START time of the market window
        # Market btc-updown-15m-1766745000 runs from 10:30 to 10:45 (starts at ts)
        # So we need the market that started at or before now (round DOWN to boundary)
        current_boundary = (current_ts // 900) * 900

        slugs = []
        for i in range(count):
            ts = current_boundary + (i * 900)
            slugs.append(f"btc-updown-15m-{ts}")

        return slugs

    async def get_current_market(self) -> Optional[BTCMarket]:
        """
        Get the market for the current 15-minute window.

        Calculates the expected market slug based on current time and
        fetches it directly from the API. This is more reliable than
        searching through all markets.

        Returns:
            BTCMarket for current window, or None if not found
        """
        now = datetime.now(timezone.utc)
        slugs = self._generate_market_slugs(count=1, now=now)

        if slugs:
            market = await self.get_market_by_slug(slugs[0])
            if market:
                logger.info(f"Found current market: {market.slug}")
                return market

        logger.warning("Current market not found by calculated slug")
        return None

    async def get_current_and_upcoming_markets(
        self,
        count: int = 5,
    ) -> List[BTCMarket]:
        """
        Get the current market and upcoming markets.

        Calculates expected market slugs based on current time and
        fetches them directly. This ensures we get currently active
        markets rather than only future ones.

        Args:
            count: Number of markets to fetch (including current)

        Returns:
            List of BTCMarket objects, starting with current window
        """
        now = datetime.now(timezone.utc)
        slugs = self._generate_market_slugs(count=count, now=now)

        markets = []
        for slug in slugs:
            try:
                market = await self.get_market_by_slug(slug)
                if market:
                    markets.append(market)
                    logger.debug(f"Fetched market: {slug}")
                else:
                    logger.debug(f"Market not found: {slug}")
            except Exception as e:
                logger.warning(f"Failed to fetch market {slug}: {e}")

        if markets:
            logger.info(
                f"Found {len(markets)} current/upcoming markets, "
                f"first: {markets[0].slug}"
            )
        else:
            logger.warning("No current/upcoming markets found by slug calculation")

        return markets

    async def find_btc_15min_markets(
        self,
        active_only: bool = True,
        limit: int = 50,
    ) -> List[BTCMarket]:
        """
        Find BTC 15-minute Up/Down markets.

        Queries the gamma API and filters for BTC 15-minute markets
        based on the slug pattern 'btc-updown-15m-'.

        Args:
            active_only: If True, only return markets accepting orders
            limit: Maximum number of markets to return

        Returns:
            List of BTCMarket objects sorted by end_time (soonest first)

        Raises:
            MarketFinderError: If API request fails
        """
        markets: List[BTCMarket] = []

        try:
            # Query gamma API for active events
            # Use multiple queries to maximize coverage
            queries = [
                f"{self.GAMMA_API_URL}/events?active=true&closed=false&limit=200",
                f"{self.GAMMA_API_URL}/events?tag=Crypto&limit=200",
                f"{self.GAMMA_API_URL}/events?order=startDate&ascending=false&limit=200",
            ]

            seen_slugs = set()

            for url in queries:
                try:
                    events = await self._fetch_with_retry(url)

                    for event in events:
                        slug = (event.get("slug") or "").lower()

                        # Filter for BTC 15-minute markets
                        if "btc-updown-15m" not in slug:
                            continue

                        # Skip duplicates
                        if slug in seen_slugs:
                            continue
                        seen_slugs.add(slug)

                        try:
                            market = BTCMarket.from_gamma_api(event)

                            # Filter by active status if requested
                            if active_only and not market.is_active():
                                continue

                            markets.append(market)

                        except (ValueError, KeyError) as e:
                            logger.warning(f"Failed to parse market {slug}: {e}")
                            continue

                except httpx.HTTPError as e:
                    logger.warning(f"API request failed for {url}: {e}")
                    continue

            # Sort by end_time (soonest first)
            markets.sort(key=lambda m: m.end_time)

            # Limit results
            markets = markets[:limit]

            logger.info(f"Found {len(markets)} BTC 15-minute markets")
            return markets

        except Exception as e:
            raise MarketFinderError(f"Failed to find markets: {e}") from e

    async def get_active_market(self) -> BTCMarket:
        """
        Get the currently active BTC 15-minute market.

        Uses slug calculation to find the current window's market first,
        then falls back to searching all markets if not found.

        Returns:
            The active BTCMarket

        Raises:
            NoMarketsFoundError: If no active markets found
        """
        # First, try to get the current market by calculated slug
        # This is more reliable as it directly targets the current window
        current_markets = await self.get_current_and_upcoming_markets(count=3)

        # Find the first market that's currently active (not expired, accepting orders)
        for market in current_markets:
            if market.is_active() and not market.is_expired():
                logger.info(
                    f"Active market (via slug): {market.slug} "
                    f"(ends in {int(market.time_remaining())}s)"
                )
                return market

        # Fallback: search through all markets
        logger.debug("Falling back to general market search")
        markets = await self.find_btc_15min_markets(active_only=True)

        if not markets:
            raise NoMarketsFoundError("No active BTC 15-minute markets found")

        # Return the one ending soonest (already sorted)
        active = markets[0]
        logger.info(f"Active market: {active.slug} (ends in {int(active.time_remaining())}s)")
        return active

    async def get_next_market(self) -> Optional[BTCMarket]:
        """
        Get the next upcoming BTC 15-minute market.

        Returns the market that will start after the current active one.
        Useful for market rotation planning.

        Returns:
            The next BTCMarket, or None if none available
        """
        markets = await self.find_btc_15min_markets(active_only=False)

        # Find markets that haven't started yet
        now = datetime.now(timezone.utc)
        upcoming = [m for m in markets if m.time_until_start() > 0]

        if upcoming:
            # Return the soonest upcoming
            upcoming.sort(key=lambda m: m.start_time)
            next_market = upcoming[0]
            logger.info(f"Next market: {next_market.slug} (starts in {int(next_market.time_until_start())}s)")
            return next_market

        return None

    async def get_market_by_slug(self, slug: str) -> Optional[BTCMarket]:
        """
        Get a specific market by its slug.

        Args:
            slug: Market slug (e.g., 'btc-updown-15m-1766167200')

        Returns:
            BTCMarket if found, None otherwise
        """
        client = await self._get_client()

        try:
            # Query for specific slug
            url = f"{self.GAMMA_API_URL}/events?slug={slug}"
            response = await client.get(url)
            response.raise_for_status()
            events = response.json()

            if events:
                return BTCMarket.from_gamma_api(events[0])

            return None

        except Exception as e:
            logger.warning(f"Failed to fetch market {slug}: {e}")
            return None

    async def get_markets_in_window(
        self,
        hours: float = 1.0,
    ) -> List[BTCMarket]:
        """
        Get all BTC 15-minute markets within a rolling time window.

        Uses slug calculation to directly fetch current and upcoming markets,
        which is more reliable than searching through general API results.

        Args:
            hours: Time window in hours (default 1.0 = 60 minutes)

        Returns:
            List of BTCMarket objects within the time window
        """
        # Calculate how many 15-min markets fit in the window
        # Each market is 15 minutes, so hours * 4 markets per hour
        count = max(1, int(hours * 4) + 1)

        # Get current and upcoming markets by calculated slug
        markets = await self.get_current_and_upcoming_markets(count=count)

        if markets:
            logger.info(
                f"Found {len(markets)} markets in {hours}h window via slug calculation"
            )
            return markets

        # Fallback to general search if slug-based approach fails
        logger.debug("Slug-based search failed, falling back to general search")
        all_markets = await self.find_btc_15min_markets(active_only=False)

        now = datetime.now(timezone.utc)
        window_seconds = hours * 3600

        # Filter to markets ending within the window
        in_window = [
            m for m in all_markets
            if 0 < m.time_remaining() <= window_seconds
        ]

        logger.info(f"Found {len(in_window)} markets in {hours}h window (fallback)")
        return in_window

    async def get_markets_in_time_range(
        self,
        start_utc: datetime,
        end_utc: datetime,
    ) -> List[BTCMarket]:
        """
        Get all BTC 15-minute markets that END within a specific UTC time range.

        CRITICAL: This method ensures the bot ONLY trades markets that fall within
        the user's configured session window. It prevents trading on markets that
        end before the session starts or after the session ends.

        A market is included if its end_time falls within [start_utc, end_utc].

        Args:
            start_utc: Session start time (UTC, timezone-aware)
            end_utc: Session end time (UTC, timezone-aware)

        Returns:
            List of BTCMarket objects that END within the time range, sorted by end_time

        Example:
            # User configures session from 14:00 to 16:00 IST (08:30 to 10:30 UTC)
            markets = await finder.get_markets_in_time_range(
                start_utc=datetime(2025, 12, 26, 8, 30, tzinfo=timezone.utc),
                end_utc=datetime(2025, 12, 26, 10, 30, tzinfo=timezone.utc),
            )
            # Returns markets ending at 08:30, 08:45, 09:00, ..., 10:30 UTC
        """
        # Ensure times are UTC
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        if end_utc.tzinfo is None:
            end_utc = end_utc.replace(tzinfo=timezone.utc)

        start_ts = int(start_utc.timestamp())
        end_ts = int(end_utc.timestamp())

        # Calculate all 15-minute boundary timestamps within the range
        # Round start_ts UP to next 15-min boundary (that's when first eligible market ends)
        first_boundary = ((start_ts + 899) // 900) * 900  # Round up to next 15-min
        # Round end_ts DOWN to current or previous 15-min boundary
        last_boundary = (end_ts // 900) * 900  # Round down

        if first_boundary > last_boundary:
            logger.warning(
                f"Time range too short: {start_utc.isoformat()} to {end_utc.isoformat()}, "
                f"no 15-minute markets fit"
            )
            return []

        # Generate slugs for all markets that END within the range
        slugs = []
        ts = first_boundary
        while ts <= last_boundary:
            slugs.append(f"btc-updown-15m-{ts}")
            ts += 900  # Next 15-min boundary

        logger.info(
            f"Fetching {len(slugs)} markets in range "
            f"{start_utc.isoformat()} to {end_utc.isoformat()}"
        )

        # Fetch each market by slug
        markets = []
        for slug in slugs:
            try:
                market = await self.get_market_by_slug(slug)
                if market:
                    markets.append(market)
                    logger.debug(f"Found market in range: {slug}")
                else:
                    logger.debug(f"Market not found: {slug}")
            except Exception as e:
                logger.warning(f"Failed to fetch market {slug}: {e}")

        # Filter to only markets that haven't expired yet
        now = datetime.now(timezone.utc)
        active_markets = [m for m in markets if not m.is_expired()]

        logger.info(
            f"Found {len(active_markets)} active markets in time range "
            f"(out of {len(markets)} total)"
        )

        return active_markets

    def __repr__(self) -> str:
        """String representation."""
        return f"MarketFinder(timeout={self.timeout})"
