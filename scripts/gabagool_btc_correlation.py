#!/usr/bin/env python3
"""
Gabagool vs BTC Price Correlation Analysis

Captures Gabagool's trading activity AND Binance BTC prices simultaneously
to analyze correlation between their fills and BTC price movements.

Key Questions:
1. Does Gabagool buy UP when BTC is rising?
2. Does Gabagool buy DOWN when BTC is falling?
3. How fast do they react to BTC moves?
4. What is the lag between BTC move and Gabagool fill?

Usage:
    python scripts/gabagool_btc_correlation.py --minutes 15
"""

import asyncio
import csv
import json
import os
import signal
import sys
import time
import threading
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set
from collections import deque
from zoneinfo import ZoneInfo
import argparse

import requests

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("Warning: websockets not installed. pip install websockets")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"
EVENTS_URL = "https://gamma-api.polymarket.com/events"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Gabagool wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

# Gabagool typical sizes
GABAGOOL_SIZES = {"btc": {"min": 20, "max": 28, "typical": 24}}

UTC = ZoneInfo('UTC')
ET = ZoneInfo('America/New_York')

OUTPUT_DIR = "research/live_capture"


@dataclass
class BTCPriceSample:
    """BTC price at a point in time."""
    timestamp: float  # Unix timestamp
    price: float
    velocity_1s: float = 0.0  # Price change per second (last 1s)
    velocity_5s: float = 0.0  # Price change per second (last 5s)


@dataclass
class GabagoolFill:
    """A Gabagool trade fill."""
    timestamp: float
    market_slug: str
    side: str  # BUY or SELL
    outcome: str  # Up or Down
    price: float
    size: float
    cost: float
    tx_hash: str
    # BTC context at fill time
    btc_price: float = 0.0
    btc_velocity_1s: float = 0.0
    btc_velocity_5s: float = 0.0
    btc_pct_from_strike: float = 0.0


class BinancePriceMonitor:
    """Real-time BTC price monitoring from Binance."""

    def __init__(self, history_seconds: int = 60):
        self.running = False
        self.current_price: float = 0.0
        self.price_history: deque = deque(maxlen=history_seconds * 10)  # 100ms samples
        self.last_update: float = 0.0
        self.loop = None
        self.thread = None

        # Velocity calculation
        self._velocity_1s: float = 0.0
        self._velocity_5s: float = 0.0

    def start(self):
        """Start Binance WebSocket in background thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self.thread.start()
        print("  Binance price monitor started")

    def stop(self):
        """Stop monitoring."""
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread:
            self.thread.join(timeout=5)
        print("  Binance price monitor stopped")

    def _run_async_loop(self):
        """Run async event loop in thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._monitor_loop())
        except Exception as e:
            print(f"  Binance WS error: {e}")
        finally:
            self.loop.close()

    async def _monitor_loop(self):
        """Main WebSocket loop with reconnection."""
        while self.running:
            try:
                async with websockets.connect(BINANCE_WS, ping_interval=30) as ws:
                    print(f"  Connected to Binance WebSocket")

                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            data = json.loads(message)

                            # Binance trade format: {"p": "94521.50", "T": 1736621234567, ...}
                            price = float(data.get("p", 0))
                            timestamp = data.get("T", 0) / 1000  # Convert ms to sec

                            if price > 0:
                                self.current_price = price
                                self.last_update = timestamp
                                self.price_history.append((timestamp, price))
                                self._update_velocities()

                        except asyncio.TimeoutError:
                            continue
                        except websockets.exceptions.ConnectionClosed:
                            print("  Binance WS closed, reconnecting...")
                            break

            except Exception as e:
                if self.running:
                    print(f"  Binance WS error: {e}, reconnecting in 5s...")
                    await asyncio.sleep(5)

    def _update_velocities(self):
        """Calculate velocity (price change per second)."""
        now = time.time()

        # 1-second velocity
        price_1s_ago = self._get_price_at(now - 1.0)
        if price_1s_ago and self.current_price:
            self._velocity_1s = (self.current_price - price_1s_ago) / 1.0

        # 5-second velocity
        price_5s_ago = self._get_price_at(now - 5.0)
        if price_5s_ago and self.current_price:
            self._velocity_5s = (self.current_price - price_5s_ago) / 5.0

    def _get_price_at(self, target_time: float) -> Optional[float]:
        """Get price closest to target time."""
        if not self.price_history:
            return None

        # Binary search would be faster but list is small
        closest = None
        closest_diff = float('inf')

        for ts, price in self.price_history:
            diff = abs(ts - target_time)
            if diff < closest_diff:
                closest_diff = diff
                closest = price

        return closest if closest_diff < 2.0 else None

    def get_sample(self) -> BTCPriceSample:
        """Get current BTC price sample with velocities."""
        return BTCPriceSample(
            timestamp=time.time(),
            price=self.current_price,
            velocity_1s=self._velocity_1s,
            velocity_5s=self._velocity_5s,
        )


class GabagoolBTCCorrelation:
    """Capture Gabagool fills with BTC price context."""

    def __init__(self, duration_minutes: float = 15.0, output_dir: str = OUTPUT_DIR):
        self.duration_minutes = duration_minutes
        self.output_dir = output_dir
        self.running = False

        # BTC price monitor
        self.btc_monitor = BinancePriceMonitor()

        # Strike prices for current markets
        self.strike_prices: Dict[str, float] = {}

        # Tracking
        self.seen_trades: Set[str] = set()
        self.fills: List[GabagoolFill] = []
        self.btc_samples: List[BTCPriceSample] = []

        # CSV files
        os.makedirs(output_dir, exist_ok=True)
        self.timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print("\n\nShutdown signal received...")
        self.running = False

    def _get_current_markets(self) -> List[Dict]:
        """Get current BTC 15-min markets."""
        markets = []
        now = datetime.now(UTC)

        current_minute = (now.minute // 15) * 15
        current_market_time = now.replace(minute=current_minute, second=0, microsecond=0)

        for offset in [-15, 0, 15]:
            market_time = current_market_time + timedelta(minutes=offset)
            unix_ts = int(market_time.timestamp())
            slug = f"btc-updown-15m-{unix_ts}"

            try:
                resp = requests.get(EVENTS_URL, params={"slug": slug}, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                if data and len(data) > 0:
                    event = data[0]
                    market_list = event.get("markets", [])
                    if market_list:
                        market = market_list[0]
                        if not market.get("closed"):
                            markets.append({
                                "slug": slug,
                                "condition_id": market.get("conditionId", ""),
                                "start_time": market_time,
                            })

                            # Set strike price (BTC price at market open)
                            if slug not in self.strike_prices and self.btc_monitor.current_price > 0:
                                # Approximate - in reality should get from market data
                                self.strike_prices[slug] = self.btc_monitor.current_price

            except Exception:
                pass

        return markets

    def _fetch_gabagool_trades(self, condition_id: str) -> List[Dict]:
        """Fetch recent Gabagool trades for a market."""
        try:
            resp = requests.get(
                TRADES_URL,
                params={"limit": 500, "market": condition_id, "user": WALLET},
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                return data.get("trades", [])
            elif isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    def _process_trade(self, raw_trade: Dict, market: Dict) -> Optional[GabagoolFill]:
        """Process raw trade into GabagoolFill with BTC context."""
        tx_hash = raw_trade.get("transactionHash", "")

        if tx_hash in self.seen_trades:
            return None

        self.seen_trades.add(tx_hash)

        timestamp_ms = raw_trade.get("timestamp", 0)
        timestamp = timestamp_ms / 1000 if timestamp_ms else time.time()

        outcome = raw_trade.get("outcome", "").lower()
        price = float(raw_trade.get("price", 0))
        size = float(raw_trade.get("size", 0))

        # Get BTC context at fill time
        btc_sample = self.btc_monitor.get_sample()
        strike = self.strike_prices.get(market["slug"], btc_sample.price)
        pct_from_strike = ((btc_sample.price - strike) / strike * 100) if strike else 0

        fill = GabagoolFill(
            timestamp=timestamp,
            market_slug=market["slug"],
            side=raw_trade.get("side", "").upper(),
            outcome="Up" if outcome == "up" else "Down",
            price=price,
            size=size,
            cost=price * size,
            tx_hash=tx_hash,
            btc_price=btc_sample.price,
            btc_velocity_1s=btc_sample.velocity_1s,
            btc_velocity_5s=btc_sample.velocity_5s,
            btc_pct_from_strike=pct_from_strike,
        )

        return fill

    def _sample_btc_price(self):
        """Record current BTC price sample."""
        sample = self.btc_monitor.get_sample()
        if sample.price > 0:
            self.btc_samples.append(sample)

    def run(self):
        """Main capture loop."""
        self.running = True
        start_time = time.time()
        end_time = start_time + (self.duration_minutes * 60)

        print(f"\n{'='*70}")
        print(f"GABAGOOL vs BTC PRICE CORRELATION CAPTURE")
        print(f"{'='*70}")
        print(f"Duration: {self.duration_minutes} minutes")
        print(f"Wallet: {WALLET}")
        print(f"Output: {self.output_dir}")
        print(f"\nStarting capture...")

        # Start BTC price monitor
        self.btc_monitor.start()

        # Wait for initial price
        print("  Waiting for Binance connection...")
        for _ in range(50):
            if self.btc_monitor.current_price > 0:
                break
            time.sleep(0.1)

        if self.btc_monitor.current_price <= 0:
            print("  WARNING: No Binance price yet, continuing anyway")
        else:
            print(f"  BTC price: ${self.btc_monitor.current_price:,.2f}")

        last_btc_sample = 0.0
        last_status = 0.0
        poll_interval = 5.0  # Poll trades every 5 seconds
        btc_sample_interval = 0.5  # Sample BTC every 500ms

        try:
            while self.running and time.time() < end_time:
                now = time.time()

                # Sample BTC price frequently
                if now - last_btc_sample >= btc_sample_interval:
                    self._sample_btc_price()
                    last_btc_sample = now

                # Poll for Gabagool trades
                markets = self._get_current_markets()

                for market in markets:
                    trades = self._fetch_gabagool_trades(market["condition_id"])

                    for raw_trade in trades:
                        fill = self._process_trade(raw_trade, market)
                        if fill:
                            self.fills.append(fill)

                            # Print fill with BTC context
                            direction = "+" if fill.btc_velocity_1s > 0 else "-" if fill.btc_velocity_1s < 0 else "="
                            print(f"\n  [FILL] {fill.side} {fill.outcome} @ ${fill.price:.2f} x {fill.size:.0f}")
                            print(f"         BTC: ${fill.btc_price:,.2f} ({direction}${abs(fill.btc_velocity_1s):.2f}/s)")
                            print(f"         BTC vs strike: {fill.btc_pct_from_strike:+.3f}%")

                            # Quick correlation hint
                            if fill.side == "BUY":
                                if fill.outcome == "Up" and fill.btc_velocity_1s > 0:
                                    print(f"         >> BULLISH alignment (buying UP while BTC rising)")
                                elif fill.outcome == "Down" and fill.btc_velocity_1s < 0:
                                    print(f"         >> BEARISH alignment (buying DOWN while BTC falling)")
                                elif fill.outcome == "Up" and fill.btc_velocity_1s < 0:
                                    print(f"         ?? CONTRARIAN (buying UP while BTC falling)")
                                elif fill.outcome == "Down" and fill.btc_velocity_1s > 0:
                                    print(f"         ?? CONTRARIAN (buying DOWN while BTC rising)")

                # Status update
                if now - last_status >= 60:
                    elapsed = now - start_time
                    remaining = end_time - now
                    print(f"\n  [{elapsed/60:.1f}m elapsed, {remaining/60:.1f}m remaining] "
                          f"Fills: {len(self.fills)}, BTC samples: {len(self.btc_samples)}, "
                          f"BTC: ${self.btc_monitor.current_price:,.2f}")
                    last_status = now

                time.sleep(poll_interval)

        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()

        finally:
            self.btc_monitor.stop()
            self._save_results()
            self._analyze_correlation()

    def _save_results(self):
        """Save captured data to CSV."""
        # Save fills
        fills_file = f"{self.output_dir}/gabagool_btc_fills_{self.timestamp_str}.csv"
        with open(fills_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'market_slug', 'side', 'outcome', 'price', 'size', 'cost',
                'btc_price', 'btc_velocity_1s', 'btc_velocity_5s', 'btc_pct_from_strike', 'tx_hash'
            ])
            for fill in self.fills:
                writer.writerow([
                    fill.timestamp, fill.market_slug, fill.side, fill.outcome,
                    fill.price, fill.size, fill.cost,
                    fill.btc_price, fill.btc_velocity_1s, fill.btc_velocity_5s,
                    fill.btc_pct_from_strike, fill.tx_hash
                ])
        print(f"\nSaved fills to: {fills_file}")

        # Save BTC samples
        btc_file = f"{self.output_dir}/btc_samples_{self.timestamp_str}.csv"
        with open(btc_file, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'price', 'velocity_1s', 'velocity_5s'])
            for sample in self.btc_samples:
                writer.writerow([sample.timestamp, sample.price, sample.velocity_1s, sample.velocity_5s])
        print(f"Saved BTC samples to: {btc_file}")

    def _analyze_correlation(self):
        """Analyze correlation between Gabagool fills and BTC price."""
        print(f"\n{'='*70}")
        print(f"CORRELATION ANALYSIS")
        print(f"{'='*70}")

        if not self.fills:
            print("No fills captured - no correlation to analyze")
            return

        # Filter to BUY trades only (meaningful for analysis)
        buys = [f for f in self.fills if f.side == "BUY"]

        if not buys:
            print("No BUY fills captured - no correlation to analyze")
            return

        print(f"\nTotal fills: {len(self.fills)}")
        print(f"BUY fills: {len(buys)}")

        # Categorize by outcome and BTC direction
        up_buys = [f for f in buys if f.outcome == "Up"]
        down_buys = [f for f in buys if f.outcome == "Down"]

        print(f"\nUP buys: {len(up_buys)}")
        print(f"DOWN buys: {len(down_buys)}")

        # Analyze alignment with BTC direction
        up_aligned = sum(1 for f in up_buys if f.btc_velocity_1s > 0)  # Buying UP when BTC rising
        down_aligned = sum(1 for f in down_buys if f.btc_velocity_1s < 0)  # Buying DOWN when BTC falling

        up_contrarian = sum(1 for f in up_buys if f.btc_velocity_1s < 0)  # Buying UP when BTC falling
        down_contrarian = sum(1 for f in down_buys if f.btc_velocity_1s > 0)  # Buying DOWN when BTC rising

        total_aligned = up_aligned + down_aligned
        total_contrarian = up_contrarian + down_contrarian
        total = len(buys)

        print(f"\n--- DIRECTIONAL ALIGNMENT ---")
        print(f"Aligned with BTC: {total_aligned}/{total} ({100*total_aligned/total:.1f}%)")
        print(f"  - UP buys when BTC rising: {up_aligned}/{len(up_buys) if up_buys else 1}")
        print(f"  - DOWN buys when BTC falling: {down_aligned}/{len(down_buys) if down_buys else 1}")
        print(f"\nContrarian to BTC: {total_contrarian}/{total} ({100*total_contrarian/total:.1f}%)")
        print(f"  - UP buys when BTC falling: {up_contrarian}")
        print(f"  - DOWN buys when BTC rising: {down_contrarian}")

        # Velocity at fill time
        if up_buys:
            avg_vel_up = sum(f.btc_velocity_1s for f in up_buys) / len(up_buys)
            print(f"\nAvg BTC velocity at UP buys: ${avg_vel_up:+.2f}/sec")

        if down_buys:
            avg_vel_down = sum(f.btc_velocity_1s for f in down_buys) / len(down_buys)
            print(f"Avg BTC velocity at DOWN buys: ${avg_vel_down:+.2f}/sec")

        # Position vs strike
        if up_buys:
            avg_pct_up = sum(f.btc_pct_from_strike for f in up_buys) / len(up_buys)
            print(f"\nAvg BTC vs strike at UP buys: {avg_pct_up:+.4f}%")

        if down_buys:
            avg_pct_down = sum(f.btc_pct_from_strike for f in down_buys) / len(down_buys)
            print(f"Avg BTC vs strike at DOWN buys: {avg_pct_down:+.4f}%")

        # Imbalance analysis
        total_up_size = sum(f.size for f in up_buys)
        total_down_size = sum(f.size for f in down_buys)
        imbalance = total_up_size - total_down_size

        print(f"\n--- POSITION IMBALANCE ---")
        print(f"Total UP shares bought: {total_up_size:.0f}")
        print(f"Total DOWN shares bought: {total_down_size:.0f}")
        print(f"Net imbalance: {imbalance:+.0f} shares")

        if imbalance != 0:
            ratio = max(total_up_size, total_down_size) / min(total_up_size, total_down_size) if min(total_up_size, total_down_size) > 0 else float('inf')
            print(f"Imbalance ratio: {ratio:.1f}:1 ({'UP' if imbalance > 0 else 'DOWN'} heavy)")

        # Conclusion
        print(f"\n--- CONCLUSION ---")
        if total_aligned > total_contrarian:
            print(f"Gabagool appears to FOLLOW BTC direction")
            print(f"  They buy UP when BTC rises, DOWN when BTC falls")
        elif total_contrarian > total_aligned:
            print(f"Gabagool appears to FADE BTC direction")
            print(f"  They buy UP when BTC falls, DOWN when BTC rises (mean reversion)")
        else:
            print(f"No clear directional pattern detected")
            print(f"  May need more data or they trade based on other signals")

        # Save analysis
        analysis_file = f"{self.output_dir}/gabagool_btc_analysis_{self.timestamp_str}.md"
        with open(analysis_file, 'w') as f:
            f.write(f"# Gabagool vs BTC Correlation Analysis\n\n")
            f.write(f"**Capture Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"**Duration:** {self.duration_minutes} minutes\n")
            f.write(f"**Total Fills:** {len(self.fills)}\n")
            f.write(f"**BUY Fills:** {len(buys)}\n\n")
            f.write(f"## Directional Alignment\n\n")
            f.write(f"- Aligned with BTC: {total_aligned}/{total} ({100*total_aligned/total:.1f}%)\n")
            f.write(f"- Contrarian to BTC: {total_contrarian}/{total} ({100*total_contrarian/total:.1f}%)\n\n")
            f.write(f"## Position Imbalance\n\n")
            f.write(f"- UP shares: {total_up_size:.0f}\n")
            f.write(f"- DOWN shares: {total_down_size:.0f}\n")
            f.write(f"- Net: {imbalance:+.0f}\n")
        print(f"\nAnalysis saved to: {analysis_file}")


def main():
    parser = argparse.ArgumentParser(description="Gabagool vs BTC Price Correlation Analysis")
    parser.add_argument('--minutes', type=float, default=15.0, help='Capture duration in minutes')
    parser.add_argument('--output', type=str, default=OUTPUT_DIR, help='Output directory')
    args = parser.parse_args()

    capture = GabagoolBTCCorrelation(
        duration_minutes=args.minutes,
        output_dir=args.output,
    )
    capture.run()


if __name__ == "__main__":
    main()
