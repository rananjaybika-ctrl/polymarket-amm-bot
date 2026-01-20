"""
Market Finder service for discovering BTC 15-minute Up/Down markets.

This service queries the Polymarket CLOB API (primary) and Gamma API (fallback)
to find active BTC 15-minute markets and provides methods for market discovery
and rotation.

HYBRID APPROACH:
- CLOB API: Primary source (more reliable for market data)
- Gamma API: Fallback if CLOB fails
- Circuit breaker: 30s backoff after 3 consecutive failures
- Cache: 30s TTL to reduce API calls
"""

import asyncio
import logging
import time
from typing import List, Optional, TypeVar, Callable, Any, Dict, Tuple
from datetime import datetime, timezone, timedelta
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

    Uses CLOB API (primary) with Gamma API fallback for reliability.
    Includes circuit breaker (30s backoff after 3 failures) and caching (30s TTL).

    Attributes:
        CLOB_API_URL: Base URL for the CLOB API (primary)
        GAMMA_API_URL: Base URL for the Gamma API (fallback)

    Example:
        finder = MarketFinder()
        markets = await finder.find_btc_15min_markets()
        active = await finder.get_active_market()
    """

    CLOB_API_URL = "https://clob.polymarket.com"
    GAMMA_API_URL = "https://gamma-api.polymarket.com"
    MARKET_INTERVAL_SECONDS = 900  # 15 minutes
    CACHE_TTL = 30  # seconds
    CIRCUIT_BREAKER_THRESHOLD = 3  # failures before opening circuit
    CIRCUIT_BREAKER_RESET = 30  # seconds before resetting circuit

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

        # Circuit breaker state
        self._clob_failures: int = 0
        self._clob_circuit_open: bool = False
        self._clob_circuit_reset_time: float = 0.0
        self._gamma_failures: int = 0
        self._gamma_circuit_open: bool = False
        self._gamma_circuit_reset_time: float = 0.0

        # Market cache: slug -> (BTCMarket, timestamp)
        self._market_cache: Dict[str, Tuple[BTCMarket, float]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """
        Get or create HTTP client with HTTP/2 and connection pooling.

        LATENCY OPTIMIZATION:
        - HTTP/2 enables multiplexing (multiple requests on single connection)
        - Connection limits optimized for Polymarket API patterns
        """
        if self._client is None or self._client.is_closed:
            # Configure connection limits for better pooling
            limits = httpx.Limits(
                max_connections=20,         # Total connection pool
                max_keepalive_connections=10,  # Keep-alive connections
                keepalive_expiry=30.0,      # Keep connections alive 30s
            )

            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                http2=True,                 # Enable HTTP/2 multiplexing
                limits=limits,
            )
            logger.debug("Created httpx client with HTTP/2 enabled")
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
        fetches them IN PARALLEL for lower latency. This ensures we get
        currently active markets rather than only future ones.

        Args:
            count: Number of markets to fetch (including current)

        Returns:
            List of BTCMarket objects, starting with current window
        """
        now = datetime.now(timezone.utc)
        slugs = self._generate_market_slugs(count=count, now=now)

        # LATENCY OPTIMIZATION: Fetch all markets in parallel instead of sequentially
        # This reduces ~50ms per market * count = significant savings
        async def fetch_market_safe(slug: str) -> Optional[BTCMarket]:
            """Fetch a single market, returning None on error."""
            try:
                market = await self.get_market_by_slug(slug)
                if market:
                    logger.debug(f"Fetched market: {slug}")
                return market
            except Exception as e:
                logger.warning(f"Failed to fetch market {slug}: {e}")
                return None

        # Fetch all markets concurrently
        results = await asyncio.gather(*[fetch_market_safe(slug) for slug in slugs])

        # Filter out None results and maintain order
        markets = [m for m in results if m is not None]

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

    def _check_circuit_breaker(self, api: str) -> bool:
        """Check if circuit breaker is open for an API.

        Args:
            api: "clob" or "gamma"

        Returns:
            True if circuit is open (should skip this API), False otherwise
        """
        now = time.time()

        if api == "clob":
            if self._clob_circuit_open:
                if now >= self._clob_circuit_reset_time:
                    # Reset circuit breaker
                    self._clob_circuit_open = False
                    self._clob_failures = 0
                    logger.info("[CIRCUIT] CLOB circuit breaker reset")
                    return False
                return True
        else:  # gamma
            if self._gamma_circuit_open:
                if now >= self._gamma_circuit_reset_time:
                    self._gamma_circuit_open = False
                    self._gamma_failures = 0
                    logger.info("[CIRCUIT] Gamma circuit breaker reset")
                    return False
                return True
        return False

    def _record_failure(self, api: str) -> None:
        """Record a failure and potentially open circuit breaker.

        Args:
            api: "clob" or "gamma"
        """
        now = time.time()

        if api == "clob":
            self._clob_failures += 1
            if self._clob_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
                self._clob_circuit_open = True
                self._clob_circuit_reset_time = now + self.CIRCUIT_BREAKER_RESET
                logger.warning(
                    f"[CIRCUIT] CLOB circuit breaker OPEN after {self._clob_failures} failures. "
                    f"Reset in {self.CIRCUIT_BREAKER_RESET}s"
                )
        else:  # gamma
            self._gamma_failures += 1
            if self._gamma_failures >= self.CIRCUIT_BREAKER_THRESHOLD:
                self._gamma_circuit_open = True
                self._gamma_circuit_reset_time = now + self.CIRCUIT_BREAKER_RESET
                logger.warning(
                    f"[CIRCUIT] Gamma circuit breaker OPEN after {self._gamma_failures} failures. "
                    f"Reset in {self.CIRCUIT_BREAKER_RESET}s"
                )

    def _record_success(self, api: str) -> None:
        """Record a success and reset failure counter.

        Args:
            api: "clob" or "gamma"
        """
        if api == "clob":
            self._clob_failures = 0
        else:
            self._gamma_failures = 0

    def _get_from_cache(self, slug: str) -> Optional[BTCMarket]:
        """Get market from cache if not expired.

        Args:
            slug: Market slug

        Returns:
            BTCMarket if in cache and not expired, None otherwise
        """
        if slug in self._market_cache:
            market, timestamp = self._market_cache[slug]
            if time.time() - timestamp < self.CACHE_TTL:
                logger.debug(f"[CACHE] Hit for {slug}")
                return market
            else:
                # Expired, remove from cache
                del self._market_cache[slug]
        return None

    def _add_to_cache(self, slug: str, market: BTCMarket) -> None:
        """Add market to cache.

        Args:
            slug: Market slug
            market: BTCMarket to cache
        """
        self._market_cache[slug] = (market, time.time())
        logger.debug(f"[CACHE] Added {slug} (TTL={self.CACHE_TTL}s)")

    async def _get_market_from_clob(self, slug: str) -> Optional[BTCMarket]:
        """
        Fetch market from CLOB API (primary source).

        Args:
            slug: Market slug

        Returns:
            BTCMarket if found, None otherwise
        """
        if self._check_circuit_breaker("clob"):
            return None

        client = await self._get_client()

        try:
            # CLOB API: query markets endpoint
            # Note: CLOB doesn't support slug query directly, so we need to search
            # by the timestamp extracted from the slug
            url = f"{self.CLOB_API_URL}/markets"
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()

            markets = data.get("data", []) if isinstance(data, dict) else data

            # Search for matching market by slug
            for market_data in markets:
                market_slug = market_data.get("market_slug", "") or market_data.get("slug", "")
                if slug in market_slug or market_slug in slug:
                    # Found matching market - construct BTCMarket
                    market = self._clob_data_to_btc_market(market_data, slug)
                    if market:
                        self._record_success("clob")
                        logger.debug(f"[CLOB] Found market: {slug}")
                        return market

            # Not found in CLOB
            return None

        except Exception as e:
            self._record_failure("clob")
            logger.warning(f"[CLOB] Failed to fetch {slug}: {e}")
            return None

    def _clob_data_to_btc_market(self, data: dict, slug: str) -> Optional[BTCMarket]:
        """Convert CLOB API response to BTCMarket.

        Args:
            data: CLOB market data
            slug: Expected slug

        Returns:
            BTCMarket if conversion successful, None otherwise
        """
        try:
            # Extract token IDs from tokens array
            tokens = data.get("tokens", [])
            if len(tokens) < 2:
                return None

            up_token = None
            down_token = None
            for token in tokens:
                outcome = (token.get("outcome") or "").upper()
                token_id = token.get("token_id", "")
                if outcome in ["YES", "UP"]:
                    up_token = token_id
                elif outcome in ["NO", "DOWN"]:
                    down_token = token_id

            if not up_token or not down_token:
                # Fallback: assume first is UP, second is DOWN
                up_token = tokens[0].get("token_id", "")
                down_token = tokens[1].get("token_id", "")

            # Parse timestamps from slug (more reliable than API dates)
            start_time = None
            end_time = None
            if "15m" in slug:
                try:
                    parts = slug.split("-")
                    if len(parts) >= 4:
                        start_timestamp = int(parts[-1])
                        start_time = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
                        end_time = datetime.fromtimestamp(start_timestamp + 900, tz=timezone.utc)
                except (ValueError, IndexError):
                    pass

            # Fallback to API dates if slug parsing failed
            if not start_time or not end_time:
                end_date_str = data.get("end_date_iso") or data.get("end_date")
                if end_date_str:
                    end_time = BTCMarket._parse_timestamp(str(end_date_str))
                    start_time = end_time - timedelta(seconds=900)
                else:
                    return None

            return BTCMarket(
                condition_id=data.get("condition_id", ""),
                question=data.get("question", f"BTC Up or Down - {slug}"),
                slug=slug,
                up_token_id=up_token,
                down_token_id=down_token,
                start_time=start_time,
                end_time=end_time,
                accepting_orders=data.get("accepting_orders", False),
                best_bid=float(data.get("best_bid", 0) or 0),
                best_ask=float(data.get("best_ask", 1) or 1),
                liquidity=float(data.get("liquidity", 0) or 0),
            )
        except Exception as e:
            logger.warning(f"[CLOB] Failed to convert market data: {e}")
            return None

    async def _get_market_from_gamma(self, slug: str) -> Optional[BTCMarket]:
        """
        Fetch market from Gamma API (fallback source).

        Args:
            slug: Market slug

        Returns:
            BTCMarket if found, None otherwise
        """
        if self._check_circuit_breaker("gamma"):
            return None

        client = await self._get_client()

        try:
            url = f"{self.GAMMA_API_URL}/events?slug={slug}"
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            events = response.json()

            if events:
                market = BTCMarket.from_gamma_api(events[0])
                self._record_success("gamma")
                logger.debug(f"[GAMMA] Found market: {slug}")
                return market

            return None

        except Exception as e:
            self._record_failure("gamma")
            logger.warning(f"[GAMMA] Failed to fetch {slug}: {e}")
            return None

    async def get_market_by_slug(self, slug: str) -> Optional[BTCMarket]:
        """
        Get a specific market by its slug.

        For btc-updown-15m markets: Uses Gamma API (CLOB doesn't index these)
        For other markets: Uses CLOB API with Gamma fallback.
        Results are cached for 30 seconds.

        Args:
            slug: Market slug (e.g., 'btc-updown-15m-1766167200')

        Returns:
            BTCMarket if found, None otherwise
        """
        # Check cache first
        cached = self._get_from_cache(slug)
        if cached:
            return cached

        # BTC 15-minute markets are ONLY in Gamma API, not CLOB
        # CLOB's /markets endpoint doesn't include btc-updown-15m markets
        if "btc-updown-15m" in slug:
            market = await self._get_market_from_gamma(slug)
            if market:
                self._add_to_cache(slug, market)
                return market
            logger.warning(f"[MARKET_FINDER] Gamma failed for {slug}")
            return None

        # For other markets: Try CLOB first, then Gamma
        market = await self._get_market_from_clob(slug)
        if market:
            self._add_to_cache(slug, market)
            return market

        logger.debug(f"[FALLBACK] CLOB failed for {slug}, trying Gamma")
        market = await self._get_market_from_gamma(slug)
        if market:
            self._add_to_cache(slug, market)
            return market

        logger.warning(f"[MARKET_FINDER] Both CLOB and Gamma failed for {slug}")
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
                f"no 15-minute markets fit entirely within window"
            )
            # FALLBACK: Get current active market even if it doesn't fit entirely
            logger.info("Falling back to current active market via REST API")
            active_market = await self.get_active_market()
            if active_market:
                logger.info(f"Using active market as fallback: {active_market.slug}")
                return [active_market]
            return []

        # Generate slugs for all markets that END within the range
        # Slug uses START time, so subtract 900 from END time to get the correct slug
        slugs = []
        ts = first_boundary
        while ts <= last_boundary:
            start_time = ts - 900  # Market that ENDS at ts STARTS 15 min earlier
            slugs.append(f"btc-updown-15m-{start_time}")
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

    async def get_market_resolution(self, slug: str) -> Optional[str]:
        """
        Get the resolution (winner) for a settled market.

        Queries the Gamma API and checks outcomePrices to determine winner.
        - outcomePrices: ["1", "0"] means first outcome (Up) won
        - outcomePrices: ["0", "1"] means second outcome (Down) won

        Args:
            slug: Market slug (e.g., 'btc-updown-15m-1768343400')

        Returns:
            "UP" or "DOWN" if resolved, None if not resolved or error
        """
        client = await self._get_client()

        try:
            url = f"{self.GAMMA_API_URL}/events?slug={slug}"
            response = await client.get(url, timeout=10.0)
            response.raise_for_status()
            data = response.json()

            if not data:
                logger.warning(f"[RESOLUTION] Market not found: {slug}")
                return None

            event = data[0]
            market = event.get("markets", [{}])[0]

            # Check if market is closed/resolved
            closed = market.get("closed", False)
            resolution_status = market.get("umaResolutionStatus", "")

            if not closed and resolution_status != "resolved":
                logger.debug(f"[RESOLUTION] Market not yet resolved: {slug}")
                return None

            # Parse outcomes and prices
            import json
            outcomes_str = market.get("outcomes", "[]")
            prices_str = market.get("outcomePrices", "[]")

            outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str

            if not prices:
                logger.warning(f"[RESOLUTION] No outcome prices for: {slug}")
                return None

            # Find winner (price == "1" or 1)
            for i, price in enumerate(prices):
                if str(price) == "1":
                    if i < len(outcomes):
                        winner = outcomes[i].upper()
                        logger.info(f"[RESOLUTION] {slug} resolved to {winner}")
                        return winner

            logger.warning(f"[RESOLUTION] Could not determine winner for: {slug}")
            return None

        except Exception as e:
            logger.warning(f"[RESOLUTION] Failed to get resolution for {slug}: {e}")
            return None

    async def wait_for_resolution(
        self,
        slug: str,
        timeout_seconds: float = 120.0,
        poll_interval: float = 5.0,
    ) -> Optional[str]:
        """
        Wait for a market to resolve and return the winner.

        Useful for tracking unhedged positions after market ends.

        Args:
            slug: Market slug
            timeout_seconds: Maximum time to wait for resolution
            poll_interval: Seconds between resolution checks

        Returns:
            "UP" or "DOWN" if resolved within timeout, None otherwise
        """
        import asyncio

        start_time = time.time()
        logger.info(f"[RESOLUTION] Waiting for {slug} to resolve (timeout={timeout_seconds}s)")

        while time.time() - start_time < timeout_seconds:
            resolution = await self.get_market_resolution(slug)
            if resolution:
                return resolution

            await asyncio.sleep(poll_interval)

        logger.warning(f"[RESOLUTION] Timeout waiting for {slug} to resolve")
        return None

    def __repr__(self) -> str:
        """String representation."""
        return f"MarketFinder(timeout={self.timeout})"
