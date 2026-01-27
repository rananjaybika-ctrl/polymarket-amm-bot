#!/usr/bin/env python3
"""
Live Monitor for Wallet 0xa5e8...95f5 on Polymarket BTC 15-min Markets

Watches the wallet's trades in real-time and captures TA context
(round number proximity, SFP detection, Z-score estimate) to determine
if they use any technical analysis beyond simple Z-score timing.

Architecture:
    [Binance WS] -> BTC price stream (real-time)
    [Polymarket data-api] -> Poll trades every 3s for wallet
    [Polymarket gamma-api] -> Get current active BTC 15-min markets

    -> On new trade detected:
       - Log: time since market open, BTC price, entry side, price, size
       - Compute: BTC move from open, Z-score, round number proximity
       - Check: prior window high/low (SFP context)

Usage:
    python scripts/monitor_0xa5e8_live.py
    python scripts/monitor_0xa5e8_live.py --duration 60m --verbose
    python scripts/monitor_0xa5e8_live.py --duration 4h
"""

import asyncio
import argparse
import json
import math
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict

import aiohttp
import websockets

# ─── Constants ────────────────────────────────────────────────────────────────

WALLET = "0xa5e83423126dbc6cdb34f10f37f5d27668ab95f5"

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"
EVENTS_URL = "https://gamma-api.polymarket.com/events"

# Binance WebSocket for BTC price
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"

# Polling interval for wallet trades (seconds)
TRADE_POLL_INTERVAL = 3.0

# 15-minute window in seconds
WINDOW_SECONDS = 900


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class PriceState:
    """Real-time BTC price tracking for current and prior windows."""
    current_price: float = 0.0
    last_update_ms: int = 0

    # Current window tracking
    window_start_ts: int = 0  # Unix timestamp of current window start
    window_open_price: float = 0.0
    window_high: float = 0.0
    window_low: float = float('inf')
    window_high_time: float = 0.0  # seconds since window open
    window_low_time: float = 0.0

    # Prior window tracking
    prior_window_high: float = 0.0
    prior_window_low: float = float('inf')
    prior_window_open: float = 0.0
    prior_window_close: float = 0.0

    # Volatility tracking (1-second returns for Z-score)
    recent_prices: List[float] = field(default_factory=list)
    recent_timestamps: List[float] = field(default_factory=list)


@dataclass
class DetectedTrade:
    """A trade detected from the wallet."""
    timestamp: datetime
    side: str  # BUY or SELL
    outcome: str  # Up or Down
    price: float
    size: float
    condition_id: str
    trade_id: str

    # Context at detection time
    btc_price: float = 0.0
    btc_open: float = 0.0
    move_pct: float = 0.0
    seconds_since_open: float = 0.0
    z_score: float = 0.0
    nearest_100: float = 0.0
    nearest_1000: float = 0.0
    dist_100_pct: float = 0.0
    dist_1000_pct: float = 0.0
    sfp_confirmed: bool = False
    sfp_detail: str = ""
    window_high: float = 0.0
    window_low: float = 0.0
    prior_high: float = 0.0
    prior_low: float = 0.0


@dataclass
class SessionStats:
    """Aggregate session statistics."""
    start_time: float = 0.0
    trades_detected: List[DetectedTrade] = field(default_factory=list)
    windows_observed: int = 0
    windows_with_trades: int = 0
    windows_without_trades: int = 0


# ─── Market Window Utilities ──────────────────────────────────────────────────

def get_current_window_start() -> int:
    """Get the Unix timestamp of the current 15-min window start."""
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def get_window_slug(window_ts: int) -> str:
    """Generate the Polymarket event slug for a window timestamp."""
    return f"btc-updown-15m-{window_ts}"


def format_duration(seconds: float) -> str:
    """Format seconds as Xm Ys."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# ─── Live Monitor ─────────────────────────────────────────────────────────────

class LiveWalletMonitor:
    """Real-time monitor for 0xa5e8 wallet trades with TA context."""

    def __init__(self, duration_minutes: float = 30, verbose: bool = False):
        self.duration_minutes = duration_minutes
        self.verbose = verbose
        self.running = False

        # State
        self.price_state = PriceState()
        self.session = SessionStats()
        self.last_seen_trade_ids: set = set()
        self.current_market_condition_ids: Dict[str, str] = {}  # condition_id -> outcome

        # Window tracking
        self.current_window_ts: int = 0
        self.windows_completed: int = 0

        # Price buffer for volatility (last 300 seconds of 1-second samples)
        self.price_buffer: deque = deque(maxlen=300)
        self.last_buffer_second: int = 0

    async def run(self, standalone: bool = True):
        """Main entry point.

        Args:
            standalone: If True, sets up signal handlers. Set False when
                       embedded in another manager (e.g., run_data_collection).
        """
        self.running = True
        self.session.start_time = time.time()
        end_time = self.session.start_time + (self.duration_minutes * 60)

        print("=" * 70)
        print("  LIVE WALLET MONITOR: 0xa5e8...95f5")
        print("=" * 70)
        print(f"  Duration: {format_duration(self.duration_minutes * 60)}")
        print(f"  Wallet: {WALLET}")
        print(f"  Trade poll interval: {TRADE_POLL_INTERVAL}s")
        print(f"  Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 70)
        print()

        # Set up signal handlers only when running standalone
        if standalone:
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._shutdown)

        try:
            async with aiohttp.ClientSession() as http_session:
                self.http_session = http_session

                # Initialize current window
                await self._handle_new_window()

                # Run tasks concurrently
                await asyncio.gather(
                    self._binance_price_stream(),
                    self._trade_poll_loop(end_time),
                    self._window_monitor_loop(end_time),
                    self._status_printer(end_time),
                )
        except asyncio.CancelledError:
            pass
        finally:
            self._print_session_summary()

    def _shutdown(self):
        """Handle graceful shutdown."""
        print("\n\n  [Shutting down...]")
        self.running = False
        # Cancel all tasks
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    # ─── Binance Price Stream ─────────────────────────────────────────────────

    async def _binance_price_stream(self):
        """Connect to Binance WS and stream BTC trade prices."""
        reconnect_delay = 1.0

        while self.running:
            try:
                async with websockets.connect(
                    BINANCE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    print("  [Binance] Connected to btcusdt@trade stream")
                    reconnect_delay = 1.0

                    while self.running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            data = json.loads(msg)
                            price = float(data['p'])
                            ts_ms = int(data['T'])

                            self._update_price(price, ts_ms)

                        except asyncio.TimeoutError:
                            continue
                        except (websockets.exceptions.ConnectionClosed, Exception) as e:
                            if self.running:
                                print(f"  [Binance] Connection error: {e}")
                            break

            except Exception as e:
                if self.running:
                    print(f"  [Binance] Connection failed: {e}, retrying in {reconnect_delay}s")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 30)

    def _update_price(self, price: float, ts_ms: int):
        """Update price state with new BTC price."""
        ps = self.price_state
        ps.current_price = price
        ps.last_update_ms = ts_ms

        # Track window high/low
        if price > ps.window_high:
            ps.window_high = price
            ps.window_high_time = (ts_ms / 1000) - ps.window_start_ts
        if price < ps.window_low:
            ps.window_low = price
            ps.window_low_time = (ts_ms / 1000) - ps.window_start_ts

        # Buffer 1-second samples for volatility
        current_second = ts_ms // 1000
        if current_second != self.last_buffer_second:
            self.price_buffer.append((current_second, price))
            self.last_buffer_second = current_second

    # ─── Market Discovery ─────────────────────────────────────────────────────

    async def _handle_new_window(self):
        """Handle transition to a new 15-min window."""
        new_window_ts = get_current_window_start()

        if new_window_ts == self.current_window_ts:
            return  # Same window

        # Save prior window data
        ps = self.price_state
        if self.current_window_ts > 0:
            ps.prior_window_high = ps.window_high
            ps.prior_window_low = ps.window_low
            ps.prior_window_open = ps.window_open_price
            ps.prior_window_close = ps.current_price
            self.windows_completed += 1
            self.session.windows_observed += 1

        # Reset for new window
        self.current_window_ts = new_window_ts
        ps.window_start_ts = new_window_ts
        ps.window_open_price = ps.current_price if ps.current_price > 0 else 0
        ps.window_high = ps.current_price if ps.current_price > 0 else 0
        ps.window_low = ps.current_price if ps.current_price > 0 else float('inf')
        ps.window_high_time = 0
        ps.window_low_time = 0

        # Discover market for this window
        slug = get_window_slug(new_window_ts)
        window_dt = datetime.fromtimestamp(new_window_ts, tz=timezone.utc)
        print(f"\n  [Window] New: {window_dt.strftime('%H:%M:%S UTC')} | slug={slug}")

        await self._discover_market(slug)

    async def _discover_market(self, slug: str):
        """Find condition IDs for the current market window."""
        self.current_market_condition_ids = {}

        try:
            params = {"slug": slug}
            async with self.http_session.get(EVENTS_URL, params=params, timeout=10) as resp:
                if resp.status != 200:
                    if self.verbose:
                        print(f"  [Market] Slug {slug} not found (status {resp.status})")
                    return

                data = await resp.json()
                if not data:
                    if self.verbose:
                        print(f"  [Market] No data for slug {slug}")
                    return

                event = data[0] if isinstance(data, list) else data
                markets = event.get("markets", [])

                for market in markets:
                    cid = market.get("conditionId", "")
                    outcomes = market.get("outcomes", "")
                    question = market.get("question", "")
                    if cid:
                        self.current_market_condition_ids[cid] = outcomes
                        if self.verbose:
                            print(f"  [Market] {question[:50]} -> {cid[:16]}...")

                if self.current_market_condition_ids:
                    print(f"  [Market] Tracking {len(self.current_market_condition_ids)} outcomes")
                else:
                    print(f"  [Market] No condition IDs found for {slug}")

        except Exception as e:
            print(f"  [Market] Discovery error: {e}")

    # ─── Window Monitor ───────────────────────────────────────────────────────

    async def _window_monitor_loop(self, end_time: float):
        """Monitor for window transitions."""
        while self.running and time.time() < end_time:
            await self._handle_new_window()
            # Check every second for window transitions
            await asyncio.sleep(1.0)

    # ─── Trade Polling ────────────────────────────────────────────────────────

    async def _trade_poll_loop(self, end_time: float):
        """Poll Polymarket for new trades from the wallet."""
        # Wait for initial BTC price
        while self.running and self.price_state.current_price == 0:
            await asyncio.sleep(0.5)

        # Seed known trades to avoid false alerts
        await self._seed_known_trades()

        print(f"  [Trades] Polling every {TRADE_POLL_INTERVAL}s for wallet trades...")
        print(f"  [BTC] Initial price: ${self.price_state.current_price:,.2f}")
        print()

        while self.running and time.time() < end_time:
            try:
                await self._check_for_new_trades()
            except Exception as e:
                if self.verbose:
                    print(f"  [Trades] Poll error: {e}")
            await asyncio.sleep(TRADE_POLL_INTERVAL)

    async def _seed_known_trades(self):
        """On first poll, seed known trade IDs to avoid alerting on old trades."""
        params = {"user": WALLET, "limit": 50}
        try:
            async with self.http_session.get(TRADES_URL, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                trades = data if isinstance(data, list) else data.get("trades", [])
                for trade in trades:
                    trade_id = self._get_trade_id(trade)
                    if trade_id:
                        self.last_seen_trade_ids.add(trade_id)
                print(f"  [Trades] Seeded {len(self.last_seen_trade_ids)} known trade IDs")
        except Exception as e:
            print(f"  [Trades] Seed error: {e}")

    def _get_trade_id(self, trade: dict) -> str:
        """Generate a unique ID for a trade."""
        # Use combination of timestamp + asset + size for uniqueness
        tid = trade.get("id", "")
        if tid:
            return tid
        # Fallback: composite key
        ts = trade.get("timestamp", "")
        asset = trade.get("asset", "")[:20]
        size = trade.get("size", "")
        return f"{ts}_{asset}_{size}"

    async def _check_for_new_trades(self):
        """Check for new trades from the wallet."""
        params = {"user": WALLET, "limit": 10}

        try:
            async with self.http_session.get(TRADES_URL, params=params, timeout=10) as resp:
                if resp.status != 200:
                    return

                data = await resp.json()
                if not data:
                    return

                trades = data if isinstance(data, list) else data.get("trades", [])

                for trade in trades:
                    trade_id = self._get_trade_id(trade)
                    if not trade_id or trade_id in self.last_seen_trade_ids:
                        continue

                    self.last_seen_trade_ids.add(trade_id)

                    # Only process BTC 15-min trades
                    slug = trade.get("slug", "")
                    if "btc-updown-15m" not in slug:
                        if self.verbose:
                            print(f"  [Trades] Skipping non-BTC-15m: {slug[:50]}")
                        continue

                    await self._process_new_trade(trade, trade_id)

        except Exception as e:
            if self.verbose:
                print(f"  [Trades] Fetch error: {e}")

    def _parse_trade_timestamp(self, trade: dict) -> Optional[float]:
        """Parse trade timestamp to Unix seconds."""
        ts_str = trade.get("matchTime") or trade.get("timestamp") or trade.get("createdAt")
        if not ts_str:
            return None
        try:
            # Handle ISO format
            if isinstance(ts_str, str):
                if ts_str.endswith('Z'):
                    ts_str = ts_str[:-1] + '+00:00'
                dt = datetime.fromisoformat(ts_str)
                return dt.timestamp()
            # Handle Unix timestamp (seconds or ms)
            ts_num = float(ts_str)
            if ts_num > 1e12:
                return ts_num / 1000
            return ts_num
        except (ValueError, TypeError):
            return None

    async def _process_new_trade(self, trade: dict, trade_id: str):
        """Process a newly detected trade and compute TA context."""
        ps = self.price_state

        # Extract trade details
        side = trade.get("side", "UNKNOWN")
        outcome = trade.get("outcome", "")  # "Up" or "Down"
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        condition_id = trade.get("conditionId", "") or trade.get("market", "")
        slug = trade.get("slug", "")

        # Determine if this is for the current active window
        current_slug = get_window_slug(self.current_window_ts)
        is_current_market = (slug == current_slug)
        market_outcome = outcome

        trade_ts = self._parse_trade_timestamp(trade)
        trade_dt = datetime.fromtimestamp(trade_ts, tz=timezone.utc) if trade_ts else datetime.now(timezone.utc)

        # Compute TA context
        seconds_since_open = (trade_ts - ps.window_start_ts) if trade_ts else 0
        btc_price = ps.current_price
        btc_open = ps.window_open_price
        move_pct = ((btc_price - btc_open) / btc_open * 100) if btc_open > 0 else 0

        # Round number proximity
        nearest_100 = round(btc_price / 100) * 100
        nearest_1000 = round(btc_price / 1000) * 1000
        dist_100 = btc_price - nearest_100
        dist_1000 = btc_price - nearest_1000
        dist_100_pct = (dist_100 / btc_price * 100) if btc_price > 0 else 0
        dist_1000_pct = (dist_1000 / btc_price * 100) if btc_price > 0 else 0

        # SFP check: did BTC break prior window high/low?
        sfp_confirmed = False
        sfp_detail = "N/A (no prior window data)"
        if ps.prior_window_high > 0 and ps.prior_window_low < float('inf'):
            if btc_price > ps.prior_window_high:
                sfp_detail = f"YES - broke prior high ${ps.prior_window_high:,.2f}"
                sfp_confirmed = True
            elif btc_price < ps.prior_window_low:
                sfp_detail = f"YES - broke prior low ${ps.prior_window_low:,.2f}"
                sfp_confirmed = True
            else:
                sfp_detail = f"NO (${ps.prior_window_low:,.2f} < price < ${ps.prior_window_high:,.2f})"

        # Z-score estimate
        vol = self._compute_volatility()
        z_score = 0.0
        if vol > 0 and seconds_since_open > 0:
            expected_move = vol * math.sqrt(seconds_since_open)
            z_score = abs(move_pct / 100) / expected_move if expected_move > 0 else 0

        # Determine if contrarian
        is_contrarian = ""
        if move_pct > 0 and "Down" in market_outcome and side == "BUY":
            is_contrarian = "CONTRARIAN (BTC up, buying DOWN)"
        elif move_pct < 0 and "Up" in market_outcome and side == "BUY":
            is_contrarian = "CONTRARIAN (BTC down, buying UP)"
        elif move_pct > 0 and "Up" in market_outcome and side == "BUY":
            is_contrarian = "MOMENTUM (BTC up, buying UP)"
        elif move_pct < 0 and "Down" in market_outcome and side == "BUY":
            is_contrarian = "MOMENTUM (BTC down, buying DOWN)"

        # Store detected trade
        detected = DetectedTrade(
            timestamp=trade_dt,
            side=side,
            outcome=market_outcome or outcome,
            price=price,
            size=size,
            condition_id=condition_id,
            trade_id=trade_id,
            btc_price=btc_price,
            btc_open=btc_open,
            move_pct=move_pct,
            seconds_since_open=seconds_since_open,
            z_score=z_score,
            nearest_100=nearest_100,
            nearest_1000=nearest_1000,
            dist_100_pct=dist_100_pct,
            dist_1000_pct=dist_1000_pct,
            sfp_confirmed=sfp_confirmed,
            sfp_detail=sfp_detail,
            window_high=ps.window_high,
            window_low=ps.window_low,
            prior_high=ps.prior_window_high,
            prior_low=ps.prior_window_low,
        )
        self.session.trades_detected.append(detected)

        # Print trade alert
        self._print_trade_alert(detected, is_contrarian, vol, is_current_market)

    def _compute_volatility(self) -> float:
        """Compute volatility (std of 1-second log returns) from price buffer."""
        if len(self.price_buffer) < 10:
            return 0.0

        prices = [p for _, p in self.price_buffer]
        returns = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                returns.append(math.log(prices[i] / prices[i-1]))

        if len(returns) < 5:
            return 0.0

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        return math.sqrt(variance)

    # ─── Display ──────────────────────────────────────────────────────────────

    def _print_trade_alert(self, t: DetectedTrade, contrarian: str, vol: float, is_current: bool):
        """Print formatted trade alert with TA context."""
        W = 68  # Box inner width

        def box_line(text):
            """Format a line to fit in the box."""
            content = f"  {text}"
            padding = W - len(content)
            if padding < 0:
                content = content[:W]
                padding = 0
            return f"|{content}{' ' * padding}|"

        print()
        print("+" + "=" * W + "+")
        print(box_line(f"TRADE DETECTED: {t.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}"))
        print("+" + "-" * W + "+")

        dur = format_duration(t.seconds_since_open)
        print(box_line(f"Market: BTC 15min (opened {dur} ago)"))
        print(box_line(f"Side: {t.side} {t.outcome} @ ${t.price:.2f} x {t.size:,.0f} shares"))
        if contrarian:
            print(box_line(f"Direction: {contrarian}"))
        if not is_current:
            print(box_line("NOTE: Trade may be for a DIFFERENT market window"))

        print(box_line(f"BTC at open: ${t.btc_open:,.2f}"))
        sign = "+" if t.move_pct >= 0 else ""
        print(box_line(f"BTC now: ${t.btc_price:,.2f} ({sign}{t.move_pct:.4f}% from open)"))

        print("|" + " " * W + "|")
        print(box_line("--- TA Context ---"))
        print(box_line(f"Nearest $100 level: ${t.nearest_100:,.0f} (distance: {t.dist_100_pct:+.4f}%)"))
        print(box_line(f"Nearest $1000 level: ${t.nearest_1000:,.0f} (distance: {t.dist_1000_pct:+.4f}%)"))

        if t.prior_high > 0:
            print(box_line(f"Prior 15m window: High=${t.prior_high:,.2f}, Low=${t.prior_low:,.2f}"))
        print(box_line(f"SFP check: {t.sfp_detail}"))

        if t.window_high > 0 and t.window_low < float('inf'):
            print(box_line(f"Current window high: ${t.window_high:,.2f} | low: ${t.window_low:,.2f}"))

        print("|" + " " * W + "|")
        print(box_line("--- Signal ---"))
        print(box_line(f"Pre-window vol: {vol:.6f} (std of 1s returns)"))
        print(box_line(f"Z-score estimate: ~{t.z_score:.2f}"))
        print("+" + "=" * W + "+")
        print()

    async def _status_printer(self, end_time: float):
        """Print periodic status updates."""
        last_status = time.time()
        STATUS_INTERVAL = 60  # Print status every 60 seconds

        while self.running and time.time() < end_time:
            await asyncio.sleep(5.0)

            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                last_status = now
                elapsed = now - self.session.start_time
                remaining = end_time - now
                ps = self.price_state

                if ps.current_price > 0:
                    move = ""
                    if ps.window_open_price > 0:
                        m = (ps.current_price - ps.window_open_price) / ps.window_open_price * 100
                        move = f" ({'+' if m >= 0 else ''}{m:.4f}% from open)"

                    window_elapsed = now - ps.window_start_ts
                    print(f"  [{format_duration(elapsed)} elapsed] "
                          f"BTC=${ps.current_price:,.2f}{move} | "
                          f"Window: {format_duration(window_elapsed)}/15m | "
                          f"Trades: {len(self.session.trades_detected)} | "
                          f"Remaining: {format_duration(remaining)}")

    # ─── Session Summary ──────────────────────────────────────────────────────

    def _print_session_summary(self):
        """Print end-of-session summary."""
        elapsed = time.time() - self.session.start_time
        trades = self.session.trades_detected

        print()
        print("=" * 70)
        print("  SESSION SUMMARY")
        print("=" * 70)
        print(f"  Duration: {format_duration(elapsed)}")
        print(f"  Windows observed: {self.windows_completed}")
        print(f"  Trades detected: {len(trades)}")
        print()

        if not trades:
            print("  No trades detected during this session.")
            print("=" * 70)
            return

        # Entry timing
        delays = [t.seconds_since_open for t in trades if t.seconds_since_open > 0]
        if delays:
            avg_delay = sum(delays) / len(delays)
            print(f"  Avg entry delay: {avg_delay:.0f}s ({avg_delay/60:.1f} min)")
            print(f"  Entry delays: {', '.join(f'{d:.0f}s' for d in delays)}")

        # Entry prices
        prices = [t.price for t in trades]
        print(f"  Entry prices: {', '.join(f'${p:.2f}' for p in prices)}")

        # BTC moves at entry
        moves = [t.move_pct for t in trades]
        print(f"  BTC moves at entry: {', '.join(f'{m:+.4f}%' for m in moves)}")

        # Round number proximity
        dist_100s = [abs(t.dist_100_pct) for t in trades]
        if dist_100s:
            print(f"  Round number proximity: avg {sum(dist_100s)/len(dist_100s):.4f}% from nearest $100")

        # SFP
        sfp_count = sum(1 for t in trades if t.sfp_confirmed)
        print(f"  SFP confirmed: {sfp_count}/{len(trades)} trades")

        # Direction analysis
        contrarian_count = 0
        for t in trades:
            if t.move_pct > 0 and "Down" in t.outcome and t.side == "BUY":
                contrarian_count += 1
            elif t.move_pct < 0 and "Up" in t.outcome and t.side == "BUY":
                contrarian_count += 1
        print(f"  Direction: {contrarian_count}/{len(trades)} contrarian")

        # Z-scores
        zscores = [t.z_score for t in trades if t.z_score > 0]
        if zscores:
            print(f"  Z-scores at entry: {', '.join(f'{z:.2f}' for z in zscores)}")
            print(f"  Avg Z-score: {sum(zscores)/len(zscores):.2f}")

        # Size analysis
        sizes = [t.size for t in trades]
        if sizes:
            print(f"  Trade sizes: {', '.join(f'{s:,.0f}' for s in sizes)}")
            if len(set(sizes)) == 1:
                print(f"  Size: CONSISTENT ({sizes[0]:,.0f} every trade)")
            else:
                print(f"  Size: VARIABLE (range {min(sizes):,.0f} - {max(sizes):,.0f})")

        print()
        print("  --- Key Questions ---")
        if delays:
            if max(delays) - min(delays) < 60:
                print(f"  Timing: CONSISTENT (~{avg_delay:.0f}s delay)")
            else:
                print(f"  Timing: VARIABLE ({min(delays):.0f}s - {max(delays):.0f}s)")

        if dist_100s:
            avg_dist = sum(dist_100s) / len(dist_100s)
            if avg_dist < 0.02:
                print(f"  Round numbers: POSSIBLE INFLUENCE (avg {avg_dist:.4f}% from $100)")
            else:
                print(f"  Round numbers: NO APPARENT INFLUENCE (avg {avg_dist:.4f}%)")

        if sfp_count > len(trades) * 0.7:
            print(f"  SFP: LIKELY USED ({sfp_count}/{len(trades)} confirmed)")
        else:
            print(f"  SFP: NOT CONSISTENTLY USED ({sfp_count}/{len(trades)})")

        windows_traded = len(set(
            int(t.timestamp.timestamp()) // WINDOW_SECONDS * WINDOW_SECONDS
            for t in trades
        ))
        if self.windows_completed > 0:
            skip_rate = 1 - (windows_traded / self.windows_completed)
            print(f"  Gating: Traded {windows_traded}/{self.windows_completed} windows "
                  f"(skip rate: {skip_rate*100:.0f}%)")

        print("=" * 70)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def parse_duration(duration_str: str) -> float:
    """Parse duration string like '30m', '2h', '90m' to minutes."""
    duration_str = duration_str.strip().lower()
    if duration_str.endswith('h'):
        return float(duration_str[:-1]) * 60
    elif duration_str.endswith('m'):
        return float(duration_str[:-1])
    else:
        return float(duration_str)


def main():
    parser = argparse.ArgumentParser(
        description="Live monitor for wallet 0xa5e8 on Polymarket BTC 15-min markets"
    )
    parser.add_argument(
        "--duration", type=str, default="30m",
        help="Duration to run (e.g., 30m, 2h, 90m). Default: 30m"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()
    duration_minutes = parse_duration(args.duration)

    monitor = LiveWalletMonitor(
        duration_minutes=duration_minutes,
        verbose=args.verbose,
    )

    asyncio.run(monitor.run())


if __name__ == "__main__":
    main()
