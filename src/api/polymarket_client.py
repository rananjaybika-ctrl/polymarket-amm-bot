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
from typing import Optional, List, Dict, Any
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    AssetType,
    BookParams,
)

from src.config import Config


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

    async def disconnect(self) -> None:
        """
        Disconnect from the API.

        Cleans up resources. Safe to call multiple times.
        """
        self._client = None
        self._api_creds = None
        self.connected = False

    def __repr__(self) -> str:
        """String representation showing connection status."""
        status = "connected" if self.connected else "disconnected"
        return f"PolymarketClient({status})"
