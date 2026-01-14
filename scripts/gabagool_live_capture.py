#!/usr/bin/env python3
"""
Gabagool22 Live Data Capture with WebSocket Orderbook Monitoring

Continuously captures Gabagool22's trading activity in real-time for long time frames.
Uses WebSocket for real-time orderbook monitoring (sub-second precision).

Features:
1. Real-time trade monitoring (REST polling)
2. WebSocket orderbook monitoring (real-time order detection)
3. Streaming to CSV (no data loss on crash)
4. Configurable duration (hours, days)
5. Auto-rollover to new files each day
6. Detect Gabagool-sized orders appearing with exact timestamps
7. Track timing relative to market open

Usage:
    # Capture for 24 hours
    python scripts/gabagool_live_capture.py --hours 24

    # Capture for 7 days
    python scripts/gabagool_live_capture.py --days 7

    # Capture until stopped (Ctrl+C)
    python scripts/gabagool_live_capture.py --continuous

    # Capture specific assets
    python scripts/gabagool_live_capture.py --assets btc eth --hours 12

    # Disable orderbook monitoring (trades only)
    python scripts/gabagool_live_capture.py --no-orderbook --hours 24

    # Use REST polling instead of WebSocket (fallback)
    python scripts/gabagool_live_capture.py --no-websocket --hours 24
"""

import requests
import json
import time
import csv
import os
import signal
import sys
import asyncio
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Set, Tuple
from collections import defaultdict
import argparse

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("Warning: websockets library not installed. Using REST polling fallback.")
    print("Install with: pip install websockets")

# API endpoints
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
TRADES_URL = "https://data-api.polymarket.com/trades"
EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

# Gabagool order size profiles (from analysis)
GABAGOOL_SIZES = {
    "btc": {"min": 20, "max": 28, "typical": 24},
    "eth": {"min": 8, "max": 14, "typical": 11},
}

# Timezones
ET = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')

# Output directory
OUTPUT_DIR = "research/live_capture"


@dataclass
class Trade:
    """Single trade record."""
    timestamp: float
    timestamp_dt: str
    market_slug: str
    condition_id: str
    side: str  # BUY or SELL
    outcome: str  # Up or Down
    price: float
    size: float
    cost: float
    tx_hash: str
    asset: str  # btc, eth, sol


@dataclass
class OrderbookLevel:
    """A single price level in the orderbook."""
    price: float
    size: float


@dataclass
class OrderbookSnapshot:
    """Snapshot of an orderbook at a point in time."""
    token_id: str
    token_type: str  # "UP" or "DOWN"
    timestamp: datetime
    bids: List[OrderbookLevel]
    asks: List[OrderbookLevel]

    def get_levels_at_size(self, min_size: float, max_size: float) -> List[Tuple[str, float, float]]:
        """Get all levels with size in range. Returns (side, price, size)."""
        matches = []
        for bid in self.bids:
            if min_size <= bid.size <= max_size:
                matches.append(("BID", bid.price, bid.size))
        for ask in self.asks:
            if min_size <= ask.size <= max_size:
                matches.append(("ASK", ask.price, ask.size))
        return matches


@dataclass
class DetectedOrder:
    """An order detected in the orderbook that matches Gabagool's profile."""
    timestamp: datetime
    market_slug: str
    token_type: str  # "UP" or "DOWN"
    side: str  # "BID" or "ASK"
    price: float
    size: float
    asset: str
    time_since_market_open: float  # seconds
    is_new: bool  # True if this order appeared since last snapshot
    has_complement: bool  # True if complementary order exists (UP@X + DOWN@(1-X))
    complement_price: Optional[float] = None
    source: str = "REST"  # "REST" or "WS" (WebSocket)


@dataclass
class MarketStats:
    """Running stats for a market."""
    slug: str
    condition_id: str
    asset: str
    start_time: datetime
    up_token_id: Optional[str] = None
    down_token_id: Optional[str] = None
    trade_count: int = 0
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    first_trade_time: Optional[datetime] = None
    last_trade_time: Optional[datetime] = None
    # Orderbook tracking
    detected_orders: List[DetectedOrder] = field(default_factory=list)
    first_order_detected_time: Optional[datetime] = None
    grid_detected: bool = False

    @property
    def imbalance(self) -> float:
        return self.up_shares - self.down_shares

    @property
    def imbalance_pct(self) -> float:
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0.0
        return abs(self.imbalance) / total

    @property
    def pair_cost(self) -> float:
        if self.up_shares == 0 or self.down_shares == 0:
            return 0.0
        avg_up = self.up_cost / self.up_shares
        avg_down = self.down_cost / self.down_shares
        return avg_up + avg_down


class WebSocketOrderbookMonitor:
    """WebSocket-based orderbook monitor for real-time order detection."""

    def __init__(self, capture: 'GabagoolLiveCapture'):
        self.capture = capture
        self.running = False
        self.ws = None
        self.subscribed_tokens: Set[str] = set()
        self.orderbooks: Dict[str, Dict] = {}  # token_id -> {bids: {price: size}, asks: {price: size}}
        self.token_info: Dict[str, Dict] = {}  # token_id -> {token_type, market_slug, asset, start_time}
        self.loop = None
        self.thread = None

    def start(self):
        """Start WebSocket monitoring in a background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.thread.start()
        print("  WebSocket monitor started")

    def stop(self):
        """Stop WebSocket monitoring."""
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
        print("  WebSocket monitor stopped")

    def _run_async_loop(self):
        """Run the async event loop in a thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._monitor_loop())
        except Exception as e:
            print(f"  WebSocket error: {e}")
        finally:
            self.loop.close()

    async def _monitor_loop(self):
        """Main WebSocket monitoring loop with reconnection."""
        while self.running:
            try:
                async with websockets.connect(WS_URL, ping_interval=30, ping_timeout=10) as ws:
                    self.ws = ws
                    print(f"  WebSocket connected to {WS_URL}")

                    # Subscribe to any pending tokens
                    for token_id in list(self.subscribed_tokens):
                        await self._subscribe(token_id)

                    # Process messages
                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            await self._handle_message(message)
                        except asyncio.TimeoutError:
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            print("  WebSocket connection closed, reconnecting...")
                            break

            except Exception as e:
                if self.running:
                    print(f"  WebSocket error: {e}, reconnecting in 5s...")
                    await asyncio.sleep(5)

    async def _subscribe(self, token_id: str):
        """Subscribe to orderbook updates for a token."""
        if not self.ws:
            return

        subscribe_msg = {
            "type": "subscribe",
            "channel": "book",
            "assets_ids": [token_id]
        }
        try:
            await self.ws.send(json.dumps(subscribe_msg))
        except Exception as e:
            print(f"  WebSocket subscribe error: {e}")

    def subscribe_token(self, token_id: str, token_type: str, market_slug: str, asset: str, start_time: datetime):
        """Add a token to monitor."""
        if token_id in self.subscribed_tokens:
            return

        self.subscribed_tokens.add(token_id)
        self.token_info[token_id] = {
            "token_type": token_type,
            "market_slug": market_slug,
            "asset": asset,
            "start_time": start_time,
        }
        self.orderbooks[token_id] = {"bids": {}, "asks": {}}

        # Subscribe if WebSocket is connected
        if self.ws and self.loop:
            asyncio.run_coroutine_threadsafe(self._subscribe(token_id), self.loop)

    def unsubscribe_token(self, token_id: str):
        """Remove a token from monitoring."""
        self.subscribed_tokens.discard(token_id)
        self.token_info.pop(token_id, None)
        self.orderbooks.pop(token_id, None)

    async def _handle_message(self, message: str):
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        # Handle different message types
        msg_type = data.get("type") or data.get("event_type")

        if msg_type == "book":
            await self._handle_book_update(data)
        elif msg_type == "price_change":
            pass  # Ignore price changes for now
        elif msg_type == "last_trade_price":
            pass  # Ignore for now

    async def _handle_book_update(self, data: dict):
        """Handle orderbook update message."""
        token_id = data.get("asset_id")
        if not token_id or token_id not in self.subscribed_tokens:
            return

        now = datetime.now(UTC)
        info = self.token_info.get(token_id, {})
        asset = info.get("asset", "unknown")
        size_profile = GABAGOOL_SIZES.get(asset, {"min": 15, "max": 30})
        min_size = size_profile["min"]
        max_size = size_profile["max"]

        market_start = info.get("start_time")
        if market_start and market_start.tzinfo is None:
            market_start = market_start.replace(tzinfo=UTC)
        time_since_open = (now - market_start).total_seconds() if market_start else 0

        # Get current book state
        current_book = self.orderbooks.get(token_id, {"bids": {}, "asks": {}})
        prev_bids = set(current_book["bids"].items())
        prev_asks = set(current_book["asks"].items())

        # Update with new data
        new_bids = {}
        new_asks = {}

        for bid in data.get("bids", []):
            try:
                price = round(float(bid.get("price", 0)), 2)
                size = float(bid.get("size", 0))
                if size > 0:
                    new_bids[price] = size
            except (ValueError, TypeError):
                pass

        for ask in data.get("asks", []):
            try:
                price = round(float(ask.get("price", 0)), 2)
                size = float(ask.get("size", 0))
                if size > 0:
                    new_asks[price] = size
            except (ValueError, TypeError):
                pass

        # Detect NEW orders matching Gabagool's size
        for price, size in new_bids.items():
            if min_size <= size <= max_size:
                if (price, size) not in prev_bids:
                    # New Gabagool-sized bid detected!
                    order = DetectedOrder(
                        timestamp=now,
                        market_slug=info.get("market_slug", ""),
                        token_type=info.get("token_type", ""),
                        side="BID",
                        price=price,
                        size=size,
                        asset=asset,
                        time_since_market_open=time_since_open,
                        is_new=True,
                        has_complement=False,
                        complement_price=round(1.0 - price, 2),
                        source="WS"
                    )
                    self._report_detected_order(order)

        for price, size in new_asks.items():
            if min_size <= size <= max_size:
                if (price, size) not in prev_asks:
                    # New Gabagool-sized ask detected!
                    order = DetectedOrder(
                        timestamp=now,
                        market_slug=info.get("market_slug", ""),
                        token_type=info.get("token_type", ""),
                        side="ASK",
                        price=price,
                        size=size,
                        asset=asset,
                        time_since_market_open=time_since_open,
                        is_new=True,
                        has_complement=False,
                        complement_price=round(1.0 - price, 2),
                        source="WS"
                    )
                    self._report_detected_order(order)

        # Update stored orderbook
        self.orderbooks[token_id] = {"bids": new_bids, "asks": new_asks}

    def _report_detected_order(self, order: DetectedOrder):
        """Report a detected order to the main capture system."""
        # Thread-safe callback to main capture
        self.capture._on_ws_order_detected(order)


class GabagoolLiveCapture:
    """Real-time Gabagool trade and orderbook capture."""

    def __init__(
        self,
        assets: List[str] = None,
        duration_hours: float = 24.0,
        continuous: bool = False,
        output_dir: str = OUTPUT_DIR,
        monitor_orderbook: bool = True,
        use_websocket: bool = True,
    ):
        self.assets = assets or ["btc", "eth"]
        self.duration_hours = duration_hours
        self.continuous = continuous
        self.output_dir = output_dir
        self.monitor_orderbook = monitor_orderbook
        self.use_websocket = use_websocket and WEBSOCKETS_AVAILABLE

        self.running = False
        self.start_time = None
        self.end_time = None

        # Tracking
        self.seen_trades: Set[str] = set()
        self.market_stats: Dict[str, MarketStats] = {}
        self.total_trades = 0
        self.total_detected_orders = 0
        self.ws_detected_orders = 0
        self.rest_detected_orders = 0
        self.last_poll_time = None

        # Orderbook tracking for REST fallback
        self.prev_orderbooks: Dict[str, OrderbookSnapshot] = {}

        # WebSocket monitor
        self.ws_monitor: Optional[WebSocketOrderbookMonitor] = None

        # CSV writers
        self.trades_file = None
        self.trades_writer = None
        self.orderbook_file = None
        self.orderbook_writer = None
        self.current_date = None

        # Thread lock for CSV writing
        self.csv_lock = threading.Lock()

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Signal handling for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle Ctrl+C gracefully."""
        print("\n\nShutdown signal received. Finishing up...")
        self.running = False

    def _on_ws_order_detected(self, order: DetectedOrder):
        """Callback from WebSocket monitor when order is detected."""
        with self.csv_lock:
            self._write_detected_order(order)
            self.total_detected_orders += 1
            self.ws_detected_orders += 1

            # Update market stats
            slug = order.market_slug
            if slug in self.market_stats:
                stats = self.market_stats[slug]
                stats.detected_orders.append(order)
                if stats.first_order_detected_time is None:
                    stats.first_order_detected_time = order.timestamp

                complement_count = sum(1 for o in stats.detected_orders if o.has_complement)
                if complement_count >= 10:
                    stats.grid_detected = True

            # Print detection with WS marker
            print(f"  [WS-ORDER] [{order.asset.upper()}] {order.token_type} {order.side} "
                  f"@ ${order.price:.2f} x {order.size:.0f} "
                  f"(+{order.time_since_market_open:.1f}s)")

    def _get_current_markets(self) -> List[Dict]:
        """Get currently active 15-min markets with token IDs."""
        markets = []
        now = datetime.now(UTC)

        for asset in self.assets:
            current_minute = (now.minute // 15) * 15
            current_market_time = now.replace(
                minute=current_minute, second=0, microsecond=0
            )

            for offset in [-15, 0, 15]:
                market_time = current_market_time + timedelta(minutes=offset)
                unix_ts = int(market_time.timestamp())
                slug = f"{asset}-updown-15m-{unix_ts}"

                try:
                    resp = requests.get(
                        EVENTS_URL,
                        params={"slug": slug},
                        timeout=10
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if data and len(data) > 0:
                        event = data[0]
                        market_list = event.get("markets", [])
                        if market_list:
                            market = market_list[0]
                            if not market.get("closed"):
                                tokens = market.get("tokens", [])
                                up_token = None
                                down_token = None
                                for token in tokens:
                                    outcome = token.get("outcome", "").lower()
                                    if outcome == "up":
                                        up_token = token.get("token_id")
                                    elif outcome == "down":
                                        down_token = token.get("token_id")

                                markets.append({
                                    "slug": slug,
                                    "condition_id": market.get("conditionId", ""),
                                    "asset": asset,
                                    "start_time": market_time,
                                    "up_token_id": up_token,
                                    "down_token_id": down_token,
                                })
                except Exception:
                    pass

        return markets

    def _fetch_orderbook(self, token_id: str) -> Optional[Dict]:
        """Fetch orderbook for a token (REST)."""
        if not token_id:
            return None

        try:
            resp = requests.get(
                CLOB_BOOK_URL,
                params={"token_id": token_id},
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def _parse_orderbook(self, data: Dict, token_id: str, token_type: str) -> OrderbookSnapshot:
        """Parse orderbook response into snapshot."""
        bids = []
        asks = []

        for bid in data.get("bids", []):
            try:
                bids.append(OrderbookLevel(
                    price=float(bid.get("price", 0)),
                    size=float(bid.get("size", 0))
                ))
            except (ValueError, TypeError):
                pass

        for ask in data.get("asks", []):
            try:
                asks.append(OrderbookLevel(
                    price=float(ask.get("price", 0)),
                    size=float(ask.get("size", 0))
                ))
            except (ValueError, TypeError):
                pass

        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        return OrderbookSnapshot(
            token_id=token_id,
            token_type=token_type,
            timestamp=datetime.now(UTC),
            bids=bids,
            asks=asks
        )

    def _detect_gabagool_orders_rest(
        self,
        snapshot: OrderbookSnapshot,
        prev_snapshot: Optional[OrderbookSnapshot],
        market: Dict
    ) -> List[DetectedOrder]:
        """Detect orders matching Gabagool's profile (REST fallback)."""
        detected = []
        asset = market["asset"]
        size_profile = GABAGOOL_SIZES.get(asset, {"min": 15, "max": 30})
        min_size = size_profile["min"]
        max_size = size_profile["max"]

        now = datetime.now(UTC)
        market_start = market["start_time"]
        if market_start.tzinfo is None:
            market_start = market_start.replace(tzinfo=UTC)
        time_since_open = (now - market_start).total_seconds()

        prev_levels = set()
        if prev_snapshot:
            for bid in prev_snapshot.bids:
                prev_levels.add(("BID", round(bid.price, 2), round(bid.size, 1)))
            for ask in prev_snapshot.asks:
                prev_levels.add(("ASK", round(ask.price, 2), round(ask.size, 1)))

        current_levels = snapshot.get_levels_at_size(min_size, max_size)

        for side, price, size in current_levels:
            level_key = (side, round(price, 2), round(size, 1))
            is_new = level_key not in prev_levels

            detected.append(DetectedOrder(
                timestamp=now,
                market_slug=market["slug"],
                token_type=snapshot.token_type,
                side=side,
                price=price,
                size=size,
                asset=asset,
                time_since_market_open=time_since_open,
                is_new=is_new,
                has_complement=False,
                complement_price=round(1.0 - price, 2),
                source="REST"
            ))

        return detected

    def _check_complements(self, up_orders: List[DetectedOrder], down_orders: List[DetectedOrder]):
        """Check if UP and DOWN orders form complementary pairs."""
        down_prices = set(round(o.price, 2) for o in down_orders)
        for order in up_orders:
            if round(1.0 - order.price, 2) in down_prices:
                order.has_complement = True

        up_prices = set(round(o.price, 2) for o in up_orders)
        for order in down_orders:
            if round(1.0 - order.price, 2) in up_prices:
                order.has_complement = True

    def _fetch_recent_trades(self, condition_id: str, since_ts: int = None) -> List[Dict]:
        """Fetch recent trades for a market."""
        params = {
            "limit": 500,
            "market": condition_id,
            "user": WALLET,
        }
        if since_ts:
            params["after"] = since_ts

        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                return data.get("trades", [])
            elif isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _init_csv_for_date(self, date: datetime):
        """Initialize or rotate CSV files for a given date."""
        if self.trades_file and self.current_date == date.date():
            return

        if self.trades_file:
            self.trades_file.close()
        if self.orderbook_file:
            self.orderbook_file.close()

        self.current_date = date.date()
        date_str = date.strftime("%Y%m%d")

        trades_filename = f"{self.output_dir}/gabagool_trades_{date_str}.csv"
        trades_exists = os.path.exists(trades_filename)
        self.trades_file = open(trades_filename, 'a', newline='')
        self.trades_writer = csv.writer(self.trades_file)
        if not trades_exists:
            self.trades_writer.writerow([
                'timestamp', 'timestamp_dt', 'market_slug', 'condition_id',
                'side', 'outcome', 'price', 'size', 'cost', 'tx_hash', 'asset'
            ])
            self.trades_file.flush()

        if self.monitor_orderbook:
            orderbook_filename = f"{self.output_dir}/gabagool_orderbook_{date_str}.csv"
            orderbook_exists = os.path.exists(orderbook_filename)
            self.orderbook_file = open(orderbook_filename, 'a', newline='')
            self.orderbook_writer = csv.writer(self.orderbook_file)
            if not orderbook_exists:
                self.orderbook_writer.writerow([
                    'timestamp', 'market_slug', 'asset', 'token_type', 'side',
                    'price', 'size', 'time_since_open_sec', 'is_new',
                    'has_complement', 'complement_price', 'source'
                ])
                self.orderbook_file.flush()

        print(f"Writing trades to: {trades_filename}")
        if self.monitor_orderbook:
            print(f"Writing orderbook to: {orderbook_filename}")

    def _process_trade(self, raw_trade: Dict, market: Dict) -> Optional[Trade]:
        """Process a raw trade into a Trade object."""
        tx_hash = raw_trade.get("transactionHash", "")

        if tx_hash in self.seen_trades:
            return None

        self.seen_trades.add(tx_hash)

        timestamp_ms = raw_trade.get("timestamp", 0)
        timestamp = timestamp_ms / 1000 if timestamp_ms else time.time()
        timestamp_dt = datetime.fromtimestamp(timestamp, tz=UTC)

        outcome = raw_trade.get("outcome", "").lower()
        is_up = outcome == "up"

        price = float(raw_trade.get("price", 0))
        size = float(raw_trade.get("size", 0))
        cost = price * size

        trade = Trade(
            timestamp=timestamp,
            timestamp_dt=timestamp_dt.isoformat(),
            market_slug=market["slug"],
            condition_id=market["condition_id"],
            side=raw_trade.get("side", "").upper(),
            outcome="Up" if is_up else "Down",
            price=price,
            size=size,
            cost=cost,
            tx_hash=tx_hash,
            asset=market["asset"],
        )

        slug = market["slug"]
        if slug not in self.market_stats:
            self.market_stats[slug] = MarketStats(
                slug=slug,
                condition_id=market["condition_id"],
                asset=market["asset"],
                start_time=market["start_time"],
                up_token_id=market.get("up_token_id"),
                down_token_id=market.get("down_token_id"),
            )

        stats = self.market_stats[slug]
        stats.trade_count += 1

        if trade.side == "BUY":
            if is_up:
                stats.up_shares += size
                stats.up_cost += cost
            else:
                stats.down_shares += size
                stats.down_cost += cost

        if stats.first_trade_time is None:
            stats.first_trade_time = timestamp_dt
        stats.last_trade_time = timestamp_dt

        return trade

    def _write_trade(self, trade: Trade):
        """Write a trade to CSV."""
        with self.csv_lock:
            self._init_csv_for_date(datetime.now(UTC))

            self.trades_writer.writerow([
                trade.timestamp,
                trade.timestamp_dt,
                trade.market_slug,
                trade.condition_id,
                trade.side,
                trade.outcome,
                trade.price,
                trade.size,
                trade.cost,
                trade.tx_hash,
                trade.asset,
            ])
            self.trades_file.flush()

    def _write_detected_order(self, order: DetectedOrder):
        """Write a detected order to CSV."""
        if not self.orderbook_writer:
            return

        self._init_csv_for_date(datetime.now(UTC))

        self.orderbook_writer.writerow([
            order.timestamp.isoformat(),
            order.market_slug,
            order.asset,
            order.token_type,
            order.side,
            order.price,
            order.size,
            order.time_since_market_open,
            order.is_new,
            order.has_complement,
            order.complement_price,
            order.source,
        ])
        self.orderbook_file.flush()

    def _monitor_orderbook_rest(self, market: Dict) -> List[DetectedOrder]:
        """Monitor orderbook using REST (fallback when WebSocket not used)."""
        all_detected = []

        up_token_id = market.get("up_token_id")
        down_token_id = market.get("down_token_id")

        up_orders = []
        down_orders = []

        if up_token_id:
            up_data = self._fetch_orderbook(up_token_id)
            if up_data:
                up_snapshot = self._parse_orderbook(up_data, up_token_id, "UP")
                prev_up = self.prev_orderbooks.get(up_token_id)
                up_orders = self._detect_gabagool_orders_rest(up_snapshot, prev_up, market)
                self.prev_orderbooks[up_token_id] = up_snapshot

        if down_token_id:
            down_data = self._fetch_orderbook(down_token_id)
            if down_data:
                down_snapshot = self._parse_orderbook(down_data, down_token_id, "DOWN")
                prev_down = self.prev_orderbooks.get(down_token_id)
                down_orders = self._detect_gabagool_orders_rest(down_snapshot, prev_down, market)
                self.prev_orderbooks[down_token_id] = down_snapshot

        self._check_complements(up_orders, down_orders)

        all_detected.extend(up_orders)
        all_detected.extend(down_orders)

        return all_detected

    def _print_status(self):
        """Print current capture status."""
        now = datetime.now(ET)
        elapsed = (now - self.start_time).total_seconds() / 3600

        active_markets = len([
            s for s in self.market_stats.values()
            if s.last_trade_time and
            (datetime.now(UTC) - s.last_trade_time.replace(tzinfo=UTC)).total_seconds() < 900
        ])

        total_up = sum(s.up_shares for s in self.market_stats.values())
        total_down = sum(s.down_shares for s in self.market_stats.values())
        total_cost = sum(s.up_cost + s.down_cost for s in self.market_stats.values())

        markets_with_grid = sum(1 for s in self.market_stats.values() if s.grid_detected)

        print(f"\n{'='*70}")
        print(f"GABAGOOL LIVE CAPTURE STATUS - {now.strftime('%Y-%m-%d %H:%M:%S ET')}")
        print(f"{'='*70}")
        print(f"Running for: {elapsed:.1f} hours")
        print(f"\nTRADE MONITORING:")
        print(f"  Total trades captured: {self.total_trades}")
        print(f"  Markets tracked: {len(self.market_stats)}")
        print(f"  Active markets: {active_markets}")
        print(f"  Total UP shares: {total_up:.1f}")
        print(f"  Total DOWN shares: {total_down:.1f}")
        print(f"  Total cost: ${total_cost:.2f}")
        print(f"  Net imbalance: {total_up - total_down:+.1f} shares")

        if self.monitor_orderbook:
            print(f"\nORDERBOOK MONITORING:")
            print(f"  Total detected orders: {self.total_detected_orders}")
            print(f"    - WebSocket: {self.ws_detected_orders}")
            print(f"    - REST: {self.rest_detected_orders}")
            print(f"  Markets with grid detected: {markets_with_grid}")

        print(f"\nRecent Market Activity:")
        recent_markets = sorted(
            self.market_stats.values(),
            key=lambda s: s.last_trade_time or datetime.min.replace(tzinfo=UTC),
            reverse=True
        )[:5]

        for stats in recent_markets:
            if stats.trade_count > 0:
                grid_marker = " [GRID]" if stats.grid_detected else ""
                print(f"  {stats.slug}: {stats.trade_count} trades, "
                      f"UP={stats.up_shares:.0f}, DOWN={stats.down_shares:.0f}, "
                      f"Imbal={stats.imbalance:+.0f}{grid_marker}")

        if not self.continuous:
            remaining = self.end_time - now
            print(f"\nTime remaining: {remaining}")

    def run(self):
        """Main capture loop."""
        self.running = True
        self.start_time = datetime.now(ET)

        if not self.continuous:
            self.end_time = self.start_time + timedelta(hours=self.duration_hours)
            print(f"Capture will run until: {self.end_time.strftime('%Y-%m-%d %H:%M:%S ET')}")
        else:
            print("Continuous capture mode - press Ctrl+C to stop")

        print(f"Tracking assets: {', '.join(self.assets)}")
        print(f"Wallet: {WALLET}")
        print(f"Output directory: {self.output_dir}")

        if self.monitor_orderbook:
            if self.use_websocket:
                print(f"Orderbook monitoring: WEBSOCKET (real-time)")
            else:
                print(f"Orderbook monitoring: REST POLLING (2s interval)")
        else:
            print(f"Orderbook monitoring: DISABLED")

        if self.monitor_orderbook:
            print(f"\nGabagool order size profiles:")
            for asset, profile in GABAGOOL_SIZES.items():
                if asset in self.assets:
                    print(f"  {asset.upper()}: {profile['min']}-{profile['max']} shares (typical: {profile['typical']})")

        # Start WebSocket monitor if enabled
        if self.monitor_orderbook and self.use_websocket:
            self.ws_monitor = WebSocketOrderbookMonitor(self)
            self.ws_monitor.start()

        print(f"\nStarting capture...")

        last_status_time = time.time()
        status_interval = 300
        trade_poll_interval = 10
        rest_orderbook_poll_interval = 2
        last_rest_orderbook_poll = 0

        # Track subscribed tokens for WebSocket
        subscribed_tokens: Set[str] = set()

        while self.running:
            try:
                now = datetime.now(ET)
                current_time = time.time()

                if not self.continuous and now >= self.end_time:
                    print("\nCapture duration reached.")
                    break

                markets = self._get_current_markets()

                self._init_csv_for_date(datetime.now(UTC))

                # Subscribe new tokens to WebSocket
                if self.ws_monitor:
                    for market in markets:
                        up_token = market.get("up_token_id")
                        down_token = market.get("down_token_id")

                        if up_token and up_token not in subscribed_tokens:
                            self.ws_monitor.subscribe_token(
                                up_token, "UP", market["slug"],
                                market["asset"], market["start_time"]
                            )
                            subscribed_tokens.add(up_token)

                        if down_token and down_token not in subscribed_tokens:
                            self.ws_monitor.subscribe_token(
                                down_token, "DOWN", market["slug"],
                                market["asset"], market["start_time"]
                            )
                            subscribed_tokens.add(down_token)

                # Poll for trades (REST)
                for market in markets:
                    trades = self._fetch_recent_trades(market["condition_id"])

                    for raw_trade in trades:
                        trade = self._process_trade(raw_trade, market)
                        if trade:
                            self._write_trade(trade)
                            self.total_trades += 1

                            print(f"  [TRADE] [{trade.asset.upper()}] {trade.side} {trade.outcome} "
                                  f"@ ${trade.price:.2f} x {trade.size:.0f} = ${trade.cost:.2f}")

                # REST orderbook polling (fallback or additional)
                if self.monitor_orderbook and not self.use_websocket:
                    if (current_time - last_rest_orderbook_poll) >= rest_orderbook_poll_interval:
                        for market in markets:
                            detected_orders = self._monitor_orderbook_rest(market)
                            new_orders = [o for o in detected_orders if o.is_new]

                            for order in new_orders:
                                with self.csv_lock:
                                    self._write_detected_order(order)
                                    self.total_detected_orders += 1
                                    self.rest_detected_orders += 1

                                    slug = market["slug"]
                                    if slug in self.market_stats:
                                        stats = self.market_stats[slug]
                                        stats.detected_orders.append(order)
                                        if stats.first_order_detected_time is None:
                                            stats.first_order_detected_time = order.timestamp

                                        complement_count = sum(1 for o in stats.detected_orders if o.has_complement)
                                        if complement_count >= 10:
                                            stats.grid_detected = True

                                print(f"  [REST-ORDER] [{order.asset.upper()}] {order.token_type} {order.side} "
                                      f"@ ${order.price:.2f} x {order.size:.0f} "
                                      f"(+{order.time_since_market_open:.1f}s)")

                        last_rest_orderbook_poll = current_time

                self.last_poll_time = datetime.now(UTC)

                if current_time - last_status_time >= status_interval:
                    self._print_status()
                    last_status_time = current_time

                time.sleep(min(trade_poll_interval, rest_orderbook_poll_interval))

            except Exception as e:
                print(f"Error in capture loop: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(30)

        # Cleanup
        if self.ws_monitor:
            self.ws_monitor.stop()

        self._print_status()
        self._export_summary()

        if self.trades_file:
            self.trades_file.close()
        if self.orderbook_file:
            self.orderbook_file.close()

        print("\nCapture complete!")

    def _export_summary(self):
        """Export market summary to CSV."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        summary_file = f"{self.output_dir}/gabagool_summary_{timestamp}.csv"

        with open(summary_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'slug', 'asset', 'condition_id', 'start_time',
                'trade_count', 'up_shares', 'down_shares', 'up_cost', 'down_cost',
                'imbalance', 'imbalance_pct', 'pair_cost',
                'first_trade_time', 'last_trade_time',
                'detected_orders_count', 'first_order_time', 'grid_detected'
            ])

            for stats in self.market_stats.values():
                writer.writerow([
                    stats.slug,
                    stats.asset,
                    stats.condition_id,
                    stats.start_time.isoformat() if stats.start_time else "",
                    stats.trade_count,
                    stats.up_shares,
                    stats.down_shares,
                    stats.up_cost,
                    stats.down_cost,
                    stats.imbalance,
                    stats.imbalance_pct,
                    stats.pair_cost,
                    stats.first_trade_time.isoformat() if stats.first_trade_time else "",
                    stats.last_trade_time.isoformat() if stats.last_trade_time else "",
                    len(stats.detected_orders),
                    stats.first_order_detected_time.isoformat() if stats.first_order_detected_time else "",
                    stats.grid_detected,
                ])

        print(f"\nSummary exported to: {summary_file}")

        # Export timing analysis
        timing_file = f"{self.output_dir}/gabagool_order_timing_{timestamp}.csv"
        with open(timing_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'market_slug', 'asset', 'market_start_time',
                'first_order_detected', 'time_to_first_order_sec',
                'first_trade_time', 'time_to_first_trade_sec',
                'total_detected_orders', 'complementary_pairs',
                'grid_detected', 'ws_orders', 'rest_orders'
            ])

            for stats in self.market_stats.values():
                market_start = stats.start_time
                if market_start and market_start.tzinfo is None:
                    market_start = market_start.replace(tzinfo=UTC)

                time_to_first_order = None
                if stats.first_order_detected_time and market_start:
                    time_to_first_order = (stats.first_order_detected_time - market_start).total_seconds()

                time_to_first_trade = None
                if stats.first_trade_time and market_start:
                    first_trade = stats.first_trade_time
                    if first_trade.tzinfo is None:
                        first_trade = first_trade.replace(tzinfo=UTC)
                    time_to_first_trade = (first_trade - market_start).total_seconds()

                complement_count = sum(1 for o in stats.detected_orders if o.has_complement)
                ws_count = sum(1 for o in stats.detected_orders if o.source == "WS")
                rest_count = sum(1 for o in stats.detected_orders if o.source == "REST")

                writer.writerow([
                    stats.slug,
                    stats.asset,
                    market_start.isoformat() if market_start else "",
                    stats.first_order_detected_time.isoformat() if stats.first_order_detected_time else "",
                    time_to_first_order,
                    stats.first_trade_time.isoformat() if stats.first_trade_time else "",
                    time_to_first_trade,
                    len(stats.detected_orders),
                    complement_count,
                    stats.grid_detected,
                    ws_count,
                    rest_count,
                ])

        print(f"Order timing analysis exported to: {timing_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Gabagool22 Live Data Capture with WebSocket Orderbook Monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Capture with WebSocket monitoring (default)
    python scripts/gabagool_live_capture.py --hours 24

    # Capture for 7 days
    python scripts/gabagool_live_capture.py --days 7

    # Continuous capture until Ctrl+C
    python scripts/gabagool_live_capture.py --continuous

    # Use REST polling instead of WebSocket
    python scripts/gabagool_live_capture.py --no-websocket --hours 12

    # Trades only (no orderbook monitoring)
    python scripts/gabagool_live_capture.py --no-orderbook --hours 12
        """
    )

    parser.add_argument('--hours', type=float, default=None, help='Capture duration in hours')
    parser.add_argument('--days', type=float, default=None, help='Capture duration in days')
    parser.add_argument('--continuous', action='store_true', help='Run continuously until stopped')
    parser.add_argument('--assets', nargs='+', default=['btc', 'eth'], help='Assets to track')
    parser.add_argument('--output', type=str, default=OUTPUT_DIR, help='Output directory')
    parser.add_argument('--no-orderbook', action='store_true', help='Disable orderbook monitoring')
    parser.add_argument('--no-websocket', action='store_true', help='Use REST polling instead of WebSocket')

    args = parser.parse_args()

    if args.days:
        duration_hours = args.days * 24
    elif args.hours:
        duration_hours = args.hours
    else:
        duration_hours = 24.0

    continuous = args.continuous
    monitor_orderbook = not args.no_orderbook
    use_websocket = not args.no_websocket

    print("=" * 70)
    if continuous:
        print("GABAGOOL22 LIVE DATA CAPTURE - CONTINUOUS MODE")
    else:
        print(f"GABAGOOL22 LIVE DATA CAPTURE - {duration_hours:.1f} HOURS")
    print("=" * 70)

    capture = GabagoolLiveCapture(
        assets=args.assets,
        duration_hours=duration_hours,
        continuous=continuous,
        output_dir=args.output,
        monitor_orderbook=monitor_orderbook,
        use_websocket=use_websocket,
    )

    capture.run()


if __name__ == "__main__":
    main()
