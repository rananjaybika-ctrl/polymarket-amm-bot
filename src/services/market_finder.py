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

        Returns the market with the soonest end_time that is
        currently accepting orders and hasn't expired.

        Returns:
            The active BTCMarket

        Raises:
            NoMarketsFoundError: If no active markets found
        """
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

        Used by MarketRotator in continuous mode to select which markets
        to consider for trading. Window rolls forward with each call.

        Args:
            hours: Time window in hours (default 1.0 = 60 minutes)

        Returns:
            List of BTCMarket objects ending within the time window
        """
        markets = await self.find_btc_15min_markets(active_only=False)

        now = datetime.now(timezone.utc)
        window_seconds = hours * 3600

        # Filter to markets ending within the window
        in_window = [
            m for m in markets
            if 0 < m.time_remaining() <= window_seconds
        ]

        logger.info(f"Found {len(in_window)} markets in {hours}h window")
        return in_window

    def __repr__(self) -> str:
        """String representation."""
        return f"MarketFinder(timeout={self.timeout})"
