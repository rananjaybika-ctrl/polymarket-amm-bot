# API modules for Polymarket AMM Bot
"""
Contains:
- polymarket_client: Polymarket CLOB API wrapper
- websocket_client: Real-time WebSocket streaming
"""

from src.api.polymarket_client import (
    PolymarketClient,
    PolymarketClientError,
    AuthenticationError,
    ConnectionError,
)
from src.api.websocket_client import (
    WebSocketClient,
    BookUpdate,
    PriceChange,
    TradeUpdate,
    MessageType,
)

__all__ = [
    "PolymarketClient",
    "PolymarketClientError",
    "AuthenticationError",
    "ConnectionError",
    "WebSocketClient",
    "BookUpdate",
    "PriceChange",
    "TradeUpdate",
    "MessageType",
]
