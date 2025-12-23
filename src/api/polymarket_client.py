"""
Polymarket CLOB API client wrapper.

This module provides a simplified interface to the Polymarket Central Limit
Order Book (CLOB) API using the py-clob-client library.

Usage:
    from src.config import Config
    from src.api.polymarket_client import PolymarketClient

    config = Config()
    client = PolymarketClient(config)

    # Connect and authenticate
    await client.connect()

    # Fetch data
    balance = await client.get_balance()
    markets = await client.get_markets()
"""

import asyncio
import logging
import math
from typing import Optional, List, Dict, Any, Literal
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
    BookParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersArgs,
    OpenOrderParams,
)
# Using vendored copy for security (not pulled from PyPI)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "vendor"))
from polymarket_apis import PolymarketGaslessWeb3Client, PolymarketWeb3Client

from src.config import Config

logger = logging.getLogger(__name__)


# Type alias for tick sizes
TickSize = Literal["0.1", "0.01", "0.001", "0.0001"]


class PolymarketClientError(Exception):
    """Base exception for Polymarket client errors."""
    pass


class AuthenticationError(PolymarketClientError):
    """Raised when authentication fails."""
    pass


class ConnectionError(PolymarketClientError):
    """Raised when connection to API fails."""
    pass


class PolymarketClient:
    """
    Wrapper around py-clob-client for Polymarket CLOB operations.

    This class simplifies the Polymarket API for trading operations.
    It handles authentication, connection management, and provides
    easy-to-use methods for common operations.

    Attributes:
        config: Bot configuration containing API credentials
        connected: Whether the client is connected and authenticated
    """

    def __init__(self, config: Config):
        """
        Initialize the Polymarket client.

        Args:
            config: Configuration object with wallet credentials
        """
        self.config = config
        self._client: Optional[ClobClient] = None
        self._api_creds: Optional[ApiCreds] = None
        self._web3_client: Optional[PolymarketGaslessWeb3Client] = None
        self.connected: bool = False

    async def connect(self) -> bool:
        """
        Connect to Polymarket and authenticate.

        This method:
        1. Creates a ClobClient with your wallet
        2. Derives API credentials for authenticated operations
        3. Verifies the connection by fetching markets

        Supports two wallet types:
        - "eoa": Standard MetaMask/hardware wallet
        - "magic": Email login (Magic wallet) - requires funder_address

        Returns:
            True if connection successful

        Raises:
            AuthenticationError: If wallet key is invalid
            ConnectionError: If cannot reach Polymarket servers
        """
        try:
            # Create the base client with wallet for signing
            # Configuration differs based on wallet type
            if self.config.wallet_type == "magic":
                # Magic/email wallet - uses proxy signing
                # signature_type=1 for Magic wallet signatures
                self._client = ClobClient(
                    host=self.config.polymarket_host,
                    key=self.config.wallet_private_key,
                    chain_id=self.config.chain_id,
                    signature_type=1,  # Magic wallet signature type
                    funder=self.config.funder_address,  # Actual Polymarket account
                )
            else:
                # Standard EOA wallet (MetaMask, hardware wallet)
                self._client = ClobClient(
                    host=self.config.polymarket_host,
                    key=self.config.wallet_private_key,
                    chain_id=self.config.chain_id,
                )

            # Derive API credentials for authenticated operations
            # This creates a session key derived from your wallet
            try:
                self._api_creds = self._client.derive_api_key()
                self._client.set_api_creds(self._api_creds)
            except Exception as e:
                raise AuthenticationError(
                    f"Failed to derive API credentials. "
                    f"Check your private key format. Error: {e}"
                )

            # Verify connection by fetching markets
            try:
                markets = self._client.get_markets()
                if markets is None:
                    raise ConnectionError("API returned empty response")
            except Exception as e:
                raise ConnectionError(
                    f"Cannot reach Polymarket servers. "
                    f"Check your internet connection. Error: {e}"
                )

            # Initialize Web3 client for merge/redeem operations
            try:
                if self.config.wallet_type == "magic":
                    # Magic wallet uses gasless relay (signature_type=1)
                    self._web3_client = PolymarketGaslessWeb3Client(
                        private_key=self.config.wallet_private_key,
                        signature_type=1,
                    )
                else:
                    # EOA wallet pays gas directly (signature_type=0)
                    self._web3_client = PolymarketWeb3Client(
                        private_key=self.config.wallet_private_key,
                        signature_type=0,
                    )
                logger.info("Web3 client initialized for merge/redeem operations")
            except Exception as e:
                logger.warning(f"Web3 client init failed (merge/redeem unavailable): {e}")
                self._web3_client = None

            self.connected = True
            return True

        except (AuthenticationError, ConnectionError):
            raise
        except Exception as e:
            raise PolymarketClientError(f"Unexpected error during connection: {e}")

    def _ensure_connected(self) -> None:
        """Verify client is connected before operations."""
        if not self.connected or self._client is None:
            raise PolymarketClientError(
                "Client not connected. Call connect() first."
            )

    async def get_balance(self) -> float:
        """
        Get your USDC (collateral) balance.

        Returns:
            USDC balance as a float (e.g., 100.50)

        Raises:
            PolymarketClientError: If not connected or API error
        """
        self._ensure_connected()

        try:
            result = self._client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # Balance is returned in string format, convert to float
            balance_str = result.get("balance", "0")
            # Balance is in wei (6 decimals for USDC)
            return float(balance_str) / 1_000_000
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch balance: {e}")

    async def get_markets(self, next_cursor: str = "") -> Dict[str, Any]:
        """
        Get list of available markets.

        Args:
            next_cursor: Pagination cursor for fetching more markets

        Returns:
            Dictionary with 'data' (list of markets) and 'next_cursor'

        Raises:
            PolymarketClientError: If API error occurs
        """
        self._ensure_connected()

        try:
            return self._client.get_markets(next_cursor=next_cursor)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch markets: {e}")

    async def get_simplified_markets(self, next_cursor: str = "") -> Dict[str, Any]:
        """
        Get simplified list of markets (less data, faster).

        Returns:
            Dictionary with 'data' (list of simplified markets) and 'next_cursor'
        """
        self._ensure_connected()

        try:
            return self._client.get_simplified_markets(next_cursor=next_cursor)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch simplified markets: {e}")

    async def get_orderbook(self, token_id: str) -> Dict[str, Any]:
        """
        Get orderbook for a specific market token.

        The orderbook shows all open buy and sell orders at different prices.

        Args:
            token_id: The token ID for the market (YES or NO token)

        Returns:
            Dictionary with 'bids' and 'asks' lists, each containing
            orders with 'price' and 'size' fields

        Example:
            orderbook = await client.get_orderbook(token_id)
            best_bid = orderbook['bids'][0]['price']  # Highest buy price
            best_ask = orderbook['asks'][0]['price']  # Lowest sell price
        """
        self._ensure_connected()

        try:
            return self._client.get_order_book(token_id)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch orderbook: {e}")

    async def get_orderbooks(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Get orderbooks for multiple tokens at once.

        More efficient than calling get_orderbook multiple times.

        Args:
            token_ids: List of token IDs

        Returns:
            List of orderbook dictionaries
        """
        self._ensure_connected()

        try:
            params = [BookParams(token_id=tid) for tid in token_ids]
            return self._client.get_order_books(params)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch orderbooks: {e}")

    async def get_price(self, token_id: str) -> Dict[str, float]:
        """
        Get current mid-market price for a token.

        Args:
            token_id: The token ID

        Returns:
            Dictionary with 'price' (mid price) and 'spread'
        """
        self._ensure_connected()

        try:
            return self._client.get_midpoint(token_id)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch price: {e}")

    async def get_spread(self, token_id: str) -> Dict[str, Any]:
        """
        Get bid-ask spread for a token.

        The spread is the difference between best bid and best ask.
        Lower spread = more liquid market.

        Args:
            token_id: The token ID

        Returns:
            Dictionary with spread information
        """
        self._ensure_connected()

        try:
            return self._client.get_spread(token_id)
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch spread: {e}")

    async def get_position_balance(self, token_id: str) -> float:
        """
        Get your balance for a specific position token (YES or NO shares).

        Args:
            token_id: The token ID for the position

        Returns:
            Number of shares held as float
        """
        self._ensure_connected()

        try:
            result = self._client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id
                )
            )
            balance_str = result.get("balance", "0")
            # Conditional tokens have 6 decimals
            return float(balance_str) / 1_000_000
        except Exception as e:
            raise PolymarketClientError(f"Failed to fetch position balance: {e}")

    def get_wallet_address(self) -> str:
        """
        Get the Polymarket account address.

        For Magic wallets, returns the funder address (actual Polymarket account).
        For EOA wallets, returns the address derived from the private key.

        Returns:
            Wallet address as hex string (e.g., '0x...')
        """
        self._ensure_connected()

        try:
            # For Magic wallets, use the funder address (actual Polymarket account)
            if self.config.wallet_type == "magic" and self.config.funder_address:
                return self.config.funder_address

            # For EOA wallets, derive from private key
            from eth_account import Account
            account = Account.from_key(self.config.wallet_private_key)
            return account.address
        except Exception as e:
            raise PolymarketClientError(f"Failed to get wallet address: {e}")

    # ==================== Order Methods ====================

    def get_tick_size(self, token_id: str) -> TickSize:
        """
        Get the tick size (price precision) for a token.

        Args:
            token_id: The token ID

        Returns:
            Tick size as string ("0.1", "0.01", "0.001", or "0.0001")
        """
        self._ensure_connected()
        return self._client.get_tick_size(token_id)

    def get_neg_risk(self, token_id: str) -> bool:
        """
        Check if a token is a negative risk market.

        Args:
            token_id: The token ID

        Returns:
            True if negative risk market
        """
        self._ensure_connected()
        return self._client.get_neg_risk(token_id)

    def round_price(self, price: float, tick_size: TickSize) -> float:
        """
        Round a price to valid tick size.

        Args:
            price: The price to round
            tick_size: The tick size for the market

        Returns:
            Price rounded to nearest valid tick
        """
        tick = float(tick_size)
        return round(round(price / tick) * tick, 4)

    def create_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        fee_rate_bps: int = 0,
    ) -> Any:
        """
        Create a signed order (does not submit).

        Args:
            token_id: Token to trade
            side: "BUY" or "SELL"
            price: Order price (0-1)
            size: Number of shares
            fee_rate_bps: Fee rate in basis points (default 0)

        Returns:
            Signed order object ready for submission
        """
        self._ensure_connected()

        # Get market parameters
        tick_size = self.get_tick_size(token_id)
        neg_risk = self.get_neg_risk(token_id)

        # Round price to valid tick
        rounded_price = self.round_price(price, tick_size)

        order_args = OrderArgs(
            token_id=token_id,
            price=rounded_price,
            size=size,
            side=side.upper(),
            fee_rate_bps=fee_rate_bps,
        )

        options = PartialCreateOrderOptions(
            tick_size=tick_size,
            neg_risk=neg_risk,
        )

        return self._client.create_order(order_args, options)

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC,
    ) -> Dict[str, Any]:
        """
        Create and submit a single order.

        Args:
            token_id: Token to trade
            side: "BUY" or "SELL"
            price: Order price (0-1)
            size: Number of shares
            order_type: GTC, FOK, FAK, or GTD

        Returns:
            Order response with order_id and status

        Raises:
            PolymarketClientError: If order value < $1.00 minimum
        """
        self._ensure_connected()

        # POLYMARKET $1 MINIMUM ORDER VALUE ENFORCEMENT
        # New orders must have value >= $1.00
        order_value = price * size
        if order_value < 1.00:
            min_size = math.ceil(1.00 / price) if price > 0 else 1
            raise PolymarketClientError(
                f"Order value ${order_value:.2f} < $1.00 minimum. "
                f"At price ${price:.4f}, minimum size is {min_size} shares"
            )

        try:
            order = self.create_order(token_id, side, price, size)
            result = self._client.post_order(order, order_type)

            # Check for API errors in response
            if result is None:
                raise PolymarketClientError("Order placement returned None (API error)")

            error_msg = result.get('errorMsg', '')
            if error_msg:
                raise PolymarketClientError(f"Order rejected: {error_msg}")

            if not result.get('success', True):
                raise PolymarketClientError(f"Order failed: status={result.get('status', 'unknown')}")

            return result
        except PolymarketClientError:
            raise
        except Exception as e:
            raise PolymarketClientError(f"Failed to place order: {e}")

    async def place_orders(
        self,
        orders: List[Any],
        order_type: OrderType = OrderType.GTC,
    ) -> List[Dict[str, Any]]:
        """
        Submit multiple orders in a single batch (atomic).

        Use this for pair trading to prevent legging risk.

        Args:
            orders: List of signed orders from create_order()
            order_type: GTC, FOK, FAK, or GTD

        Returns:
            List of order responses
        """
        self._ensure_connected()

        try:
            args = [PostOrdersArgs(order=o, orderType=order_type) for o in orders]
            result = self._client.post_orders(args)
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to place orders: {e}")

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel a single order.

        Args:
            order_id: The order ID to cancel

        Returns:
            Cancellation response
        """
        self._ensure_connected()

        try:
            result = self._client.cancel_orders([order_id])
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to cancel order: {e}")

    async def cancel_orders(self, order_ids: List[str]) -> Dict[str, Any]:
        """
        Cancel multiple orders.

        Args:
            order_ids: List of order IDs to cancel

        Returns:
            Cancellation response
        """
        self._ensure_connected()

        try:
            result = self._client.cancel_orders(order_ids)
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to cancel orders: {e}")

    async def get_open_orders(
        self,
        market: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get all open orders, optionally filtered by market.

        Args:
            market: Optional market/condition ID to filter by

        Returns:
            List of open orders
        """
        self._ensure_connected()

        try:
            params = OpenOrderParams(market=market) if market else None
            result = self._client.get_orders(params)
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to get open orders: {e}")

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """
        Get details for a specific order.

        Args:
            order_id: The order ID

        Returns:
            Order details
        """
        self._ensure_connected()

        try:
            result = self._client.get_order(order_id)
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to get order: {e}")

    async def disconnect(self) -> None:
        """
        Disconnect from the API.

        Cleans up resources. Safe to call multiple times.
        """
        self._client = None
        self._api_creds = None
        self.connected = False

    # ==================== Claiming / Redemption Methods ====================

    async def merge_positions(
        self,
        condition_id: str,
        amount: float,
        neg_risk: bool = True
    ) -> Dict[str, Any]:
        """
        Merge complementary positions (1 YES + 1 NO) into USDC.

        This converts balanced pairs back to collateral WITHOUT waiting for
        market resolution. Useful for locking in profits on hedged positions.

        Args:
            condition_id: The market's condition ID (bytes32 hex string)
            amount: Number of pairs to merge (e.g., 10 = merge 10 YES + 10 NO)
            neg_risk: Whether this is a neg risk market (default True for BTC markets)

        Returns:
            Transaction receipt dict with 'status', 'transaction_hash', 'gas_used'

        Raises:
            PolymarketClientError: If web3 client not available or tx fails
        """
        self._ensure_connected()

        if self._web3_client is None:
            raise PolymarketClientError(
                "Web3 client not initialized. Merge requires web3 connection."
            )

        try:
            logger.info(f"Merging {amount} pairs for condition {condition_id[:16]}...")
            receipt = self._web3_client.merge_position(
                condition_id=condition_id,
                amount=amount,
                neg_risk=neg_risk,
            )
            logger.info(f"Merge successful: {receipt.transaction_hash.hex()}")
            return {
                "status": receipt.status,
                "transaction_hash": receipt.transaction_hash.hex(),
                "gas_used": receipt.gas_used,
            }
        except Exception as e:
            raise PolymarketClientError(f"Failed to merge positions: {e}")

    async def redeem_positions(
        self,
        condition_id: str,
        amounts: List[float],
        neg_risk: bool = True
    ) -> Dict[str, Any]:
        """
        Redeem winning positions into USDC after market resolution.

        Call this after a market resolves to convert winning tokens to USDC.

        Args:
            condition_id: The market's condition ID (bytes32 hex string)
            amounts: [yes_amount, no_amount] - shares of each outcome to redeem
            neg_risk: Whether this is a neg risk market (default True for BTC markets)

        Returns:
            Transaction receipt dict with 'status', 'transaction_hash', 'gas_used'

        Raises:
            PolymarketClientError: If web3 client not available or tx fails
        """
        self._ensure_connected()

        if self._web3_client is None:
            raise PolymarketClientError(
                "Web3 client not initialized. Redeem requires web3 connection."
            )

        try:
            logger.info(f"Redeeming positions for condition {condition_id[:16]}...")
            receipt = self._web3_client.redeem_position(
                condition_id=condition_id,
                amounts=amounts,
                neg_risk=neg_risk,
            )
            logger.info(f"Redeem successful: {receipt.transaction_hash.hex()}")
            return {
                "status": receipt.status,
                "transaction_hash": receipt.transaction_hash.hex(),
                "gas_used": receipt.gas_used,
            }
        except Exception as e:
            raise PolymarketClientError(f"Failed to redeem positions: {e}")

    async def get_market_info(self, condition_id: str = None, slug: str = None) -> Optional[Dict[str, Any]]:
        """
        Get market information including resolution status.

        Args:
            condition_id: The market's condition ID (optional)
            slug: The market's slug (preferred - more reliable for resolution data)

        Returns:
            Market info dict with 'closed', 'resolved', 'winning_token_id' etc.
            None if market not found
        """
        self._ensure_connected()

        try:
            import aiohttp

            # Prefer slug-based query (returns accurate outcomePrices for resolved markets)
            if slug:
                url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
            elif condition_id:
                url = f"https://gamma-api.polymarket.com/markets?conditionId={condition_id}"
            else:
                return None

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        data = await response.json()
                        # API returns list when querying by parameters
                        if isinstance(data, list) and len(data) > 0:
                            return data[0]
                        return data if data else None
                    return None
        except Exception as e:
            raise PolymarketClientError(f"Failed to get market info: {e}")

    async def is_market_resolved(self, condition_id: str) -> bool:
        """
        Check if a market has been resolved.

        Args:
            condition_id: The market's condition ID

        Returns:
            True if market is resolved and claimable
        """
        market = await self.get_market_info(condition_id)
        if not market:
            return False

        # Check various resolution indicators
        return (
            market.get("resolved", False) or
            market.get("closed", False) or
            market.get("active", True) is False
        )

    async def get_winning_token(self, condition_id: str, max_retries: int = 3) -> Optional[str]:
        """
        Get the winning token ID for a resolved market.

        For BTC Up/Down markets:
        - If BTC went UP: UP token wins (pays $1)
        - If BTC went DOWN: DOWN token wins (pays $1)

        Args:
            condition_id: The market's condition ID
            max_retries: Number of retry attempts for API calls

        Returns:
            Token ID of the winning outcome, or None if not resolved
        """
        import json
        import asyncio

        market = None
        last_error = None

        # Retry logic for API reliability
        for attempt in range(max_retries):
            try:
                market = await self.get_market_info(condition_id)
                if market:
                    break
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))  # Exponential backoff

        if not market:
            if last_error:
                logger.warning(f"get_winning_token failed after {max_retries} retries: {last_error}")
            return None

        # Method 1: Check outcomePrices (most reliable for BTC up/down markets)
        # Format: "[\"1\",\"0\"]" means first outcome won, "[\"0\",\"1\"]" means second won
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            try:
                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices

                if len(prices) >= 2:
                    up_price = float(prices[0])
                    down_price = float(prices[1])

                    # Winner has price = 1.0, loser has price = 0.0
                    if up_price > 0.99 and down_price < 0.01:
                        # UP won - return UP token
                        tokens = market.get("tokens", [])
                        for token in tokens:
                            outcome = token.get("outcome", "").upper()
                            if outcome in ["YES", "UP"]:
                                logger.debug(f"Winner from outcomePrices: UP (token {token.get('token_id', '')[:10]}...)")
                                return token.get("token_id")
                        # Fallback: first token is typically UP/YES
                        if tokens:
                            return tokens[0].get("token_id")

                    elif down_price > 0.99 and up_price < 0.01:
                        # DOWN won - return DOWN token
                        tokens = market.get("tokens", [])
                        for token in tokens:
                            outcome = token.get("outcome", "").upper()
                            if outcome in ["NO", "DOWN"]:
                                logger.debug(f"Winner from outcomePrices: DOWN (token {token.get('token_id', '')[:10]}...)")
                                return token.get("token_id")
                        # Fallback: second token is typically DOWN/NO
                        if len(tokens) >= 2:
                            return tokens[1].get("token_id")

            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.debug(f"Could not parse outcomePrices: {e}")

        # Method 2: Check tokens for winner flag or price
        tokens = market.get("tokens", [])
        for token in tokens:
            # Check explicit winner field
            if token.get("winner", False):
                logger.debug(f"Winner from token.winner flag: {token.get('outcome')}")
                return token.get("token_id")

            # Check if price indicates winner (price >= 0.99)
            try:
                token_price = float(token.get("price", 0))
                if token_price >= 0.99:
                    logger.debug(f"Winner from token.price: {token.get('outcome')} (price={token_price})")
                    return token.get("token_id")
            except (ValueError, TypeError):
                pass

        # Method 3: Check resolved flag (least reliable for Gamma API)
        if market.get("resolved", False) or market.get("closed", False):
            # Market is marked resolved but we couldn't determine winner
            logger.warning(f"Market {condition_id[:20]}... is resolved but winner unclear")

        return None

    async def get_winning_side(
        self,
        condition_id: str = None,
        slug: str = None,
        max_retries: int = 3
    ) -> Optional[str]:
        """
        Get the winning side ("UP" or "DOWN") for a resolved BTC up/down market.

        This method is specifically designed for BTC up/down markets and returns
        the winning side directly, without needing to match token IDs.

        Args:
            condition_id: The market's condition ID (optional)
            slug: The market's slug (preferred - more reliable)
            max_retries: Number of retry attempts for API calls

        Returns:
            "UP" or "DOWN" if winner can be determined, None otherwise
        """
        import json
        import asyncio

        if not condition_id and not slug:
            logger.warning("get_winning_side called without condition_id or slug")
            return None

        market = None
        last_error = None

        # Retry logic for API reliability
        for attempt in range(max_retries):
            try:
                # Prefer slug-based query (more reliable for resolution data)
                market = await self.get_market_info(condition_id=condition_id, slug=slug)
                if market:
                    break
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))

        if not market:
            if last_error:
                logger.warning(f"get_winning_side failed after {max_retries} retries: {last_error}")
            return None

        # Primary method: Check outcomePrices (most reliable)
        # Format: ["1", "0"] = UP won, ["0", "1"] = DOWN won
        outcome_prices = market.get("outcomePrices")
        if outcome_prices:
            try:
                if isinstance(outcome_prices, str):
                    prices = json.loads(outcome_prices)
                else:
                    prices = outcome_prices

                if len(prices) >= 2:
                    up_price = float(prices[0])
                    down_price = float(prices[1])

                    if up_price > 0.99 and down_price < 0.01:
                        logger.info(f"[POLYMARKET] Winner from outcomePrices: UP")
                        return "UP"
                    elif down_price > 0.99 and up_price < 0.01:
                        logger.info(f"[POLYMARKET] Winner from outcomePrices: DOWN")
                        return "DOWN"

            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.debug(f"Could not parse outcomePrices: {e}")

        # Secondary: Check tokens if available
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("winner", False):
                outcome = token.get("outcome", "").upper()
                if outcome in ["YES", "UP"]:
                    logger.info(f"[POLYMARKET] Winner from token.winner: UP")
                    return "UP"
                elif outcome in ["NO", "DOWN"]:
                    logger.info(f"[POLYMARKET] Winner from token.winner: DOWN")
                    return "DOWN"

            try:
                token_price = float(token.get("price", 0))
                if token_price >= 0.99:
                    outcome = token.get("outcome", "").upper()
                    if outcome in ["YES", "UP"]:
                        logger.info(f"[POLYMARKET] Winner from token.price: UP")
                        return "UP"
                    elif outcome in ["NO", "DOWN"]:
                        logger.info(f"[POLYMARKET] Winner from token.price: DOWN")
                        return "DOWN"
            except (ValueError, TypeError):
                pass

        # Market may be closed but winner not determinable
        if market.get("closed", False):
            logger.warning(f"Market {condition_id[:20]}... is closed but winner unclear from API")

        return None

    async def get_claimable_positions(self) -> List[Dict[str, Any]]:
        """
        Get all positions that are in resolved markets and can be claimed.

        Returns:
            List of dicts with:
            - token_id: The token ID
            - condition_id: The market condition ID
            - balance: Number of shares held
            - is_winning: Whether this is the winning token
            - estimated_value: Estimated USDC value ($1 per winning share)
        """
        self._ensure_connected()

        claimable = []

        try:
            # Get all positions from Gamma API
            import aiohttp
            wallet = self.get_wallet_address()
            url = f"https://gamma-api.polymarket.com/positions?user={wallet}"

            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        return []
                    positions = await response.json()

            # Filter for resolved markets with balance
            for pos in positions:
                balance = float(pos.get("size", 0))
                if balance <= 0:
                    continue

                condition_id = pos.get("conditionId") or pos.get("condition_id", "")
                token_id = pos.get("tokenId") or pos.get("token_id", "")

                if not condition_id or not token_id:
                    continue

                # Check if market is resolved
                is_resolved = await self.is_market_resolved(condition_id)
                if not is_resolved:
                    continue

                # Check if this is winning token
                winning_token = await self.get_winning_token(condition_id)
                is_winning = (winning_token == token_id)

                claimable.append({
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "balance": balance,
                    "is_winning": is_winning,
                    "estimated_value": balance if is_winning else 0,
                    "outcome": pos.get("outcome", ""),
                })

            return claimable

        except Exception as e:
            raise PolymarketClientError(f"Failed to get claimable positions: {e}")

    async def claim_winnings_via_sell(
        self,
        token_id: str,
        amount: float,
        min_price: float = 0.99,
    ) -> Dict[str, Any]:
        """
        Claim winnings by selling winning tokens at market price.

        This is the workaround for the lack of native redeem API.
        After market resolution, winning tokens trade at ~$0.99-1.00.
        Selling at 0.99 recovers 99% of the value.

        Args:
            token_id: The winning token to sell
            amount: Number of shares to sell
            min_price: Minimum sell price (default 0.99)

        Returns:
            Order result dict
        """
        self._ensure_connected()

        try:
            # Create and submit sell order
            result = await self.place_order(
                token_id=token_id,
                side="SELL",
                price=min_price,
                size=amount,
                order_type=OrderType.GTC,
            )
            return result
        except Exception as e:
            raise PolymarketClientError(f"Failed to claim via sell: {e}")

    async def claim_all_winnings(
        self,
        dry_run: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Claim all available winning positions.

        Args:
            dry_run: If True, only simulate (don't execute trades)

        Returns:
            List of claim results
        """
        results = []
        claimable = await self.get_claimable_positions()

        for pos in claimable:
            if not pos["is_winning"]:
                continue  # Skip losing positions

            if pos["balance"] <= 0:
                continue

            result = {
                "token_id": pos["token_id"],
                "condition_id": pos["condition_id"],
                "balance": pos["balance"],
                "estimated_value": pos["estimated_value"],
                "outcome": pos["outcome"],
                "dry_run": dry_run,
            }

            if dry_run:
                result["status"] = "simulated"
                result["message"] = f"Would sell {pos['balance']} shares at $0.99"
            else:
                try:
                    order_result = await self.claim_winnings_via_sell(
                        token_id=pos["token_id"],
                        amount=pos["balance"],
                        min_price=0.99,
                    )
                    result["status"] = "success"
                    result["order"] = order_result
                except Exception as e:
                    result["status"] = "error"
                    result["error"] = str(e)

            results.append(result)

        return results

    def __repr__(self) -> str:
        """String representation showing connection status."""
        status = "connected" if self.connected else "disconnected"
        return f"PolymarketClient({status})"
