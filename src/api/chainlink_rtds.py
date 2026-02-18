"""
Chainlink RTDS Client — Real-time BTC/USD from Polymarket's RTDS WebSocket.

Connects to wss://ws-live-data.polymarket.com and subscribes to
the crypto_prices_chainlink topic for btc/usd. This is the SAME price
feed Polymarket uses to set strikes and resolve BTC Up/Down markets.

No authentication required.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL_SEC = 5
RECONNECT_DELAY_SEC = 3


class ChainlinkRTDS:
    """Streams Chainlink BTC/USD price from Polymarket RTDS."""

    def __init__(self):
        self._price: float = 0.0
        self._last_update_ms: int = 0
        self._ws = None
        self._running = False
        self._task: Optional[asyncio.Task] = None

    @property
    def price(self) -> float:
        """Latest Chainlink BTC/USD price."""
        return self._price

    @property
    def last_update_ms(self) -> int:
        """Timestamp (ms) of the last price update."""
        return self._last_update_ms

    @property
    def is_fresh(self) -> bool:
        """True if we received a price in the last 10 seconds."""
        if self._last_update_ms == 0:
            return False
        age_ms = int(time.time() * 1000) - self._last_update_ms
        return age_ms < 10_000

    async def connect(self):
        """Start the background receive loop."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def disconnect(self):
        """Stop the background loop and close the WebSocket."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _run_loop(self):
        """Connect, subscribe, and receive prices. Auto-reconnects."""
        while self._running:
            try:
                async with websockets.connect(
                    RTDS_URL,
                    ping_interval=PING_INTERVAL_SEC,
                    close_timeout=5,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    logger.info("Chainlink RTDS connected")

                    # Subscribe to Chainlink BTC/USD
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "update",
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub))

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except asyncio.TimeoutError:
                            # No message in 15s — send ping to keep alive
                            continue

                        if not raw:
                            continue

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        topic = data.get("topic", "")
                        if topic != "crypto_prices_chainlink":
                            continue

                        payload = data.get("payload", {})
                        if isinstance(payload, dict) and "value" in payload:
                            if payload.get("symbol") == "btc/usd":
                                self._price = float(payload["value"])
                                self._last_update_ms = int(payload.get("timestamp", time.time() * 1000))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Chainlink RTDS error: {e}, reconnecting in {RECONNECT_DELAY_SEC}s")
                await asyncio.sleep(RECONNECT_DELAY_SEC)

        self._ws = None
