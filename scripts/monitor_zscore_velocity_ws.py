#!/usr/bin/env python3
"""
ENHANCED WebSocket-based Monitoring Script for Deep Edge Analysis

Captures expanded metrics to test hypotheses about market efficiency:
- Orderbook depth (3 levels) and imbalance ratios
- Update frequency per second
- Price stickiness (time since last change)
- Volatility ratio (short vs long term)
- Time into market position
- Binance price changes per second

Uses Polymarket WebSocket for real-time orderbook updates.
Binance WebSocket for price/z-score data.

Usage:
    python scripts/monitor_zscore_velocity_ws.py --duration 30
    # Monitors for 30 minutes (2 market cycles)

Output:
    research/zscore_velocity_ws_YYYYMMDD_HHMMSS.csv
"""

import argparse
import asyncio
import csv
import sys
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate, PriceChange
from src.services.market_finder import MarketFinder
from src.services.trend_detector import TrendDetector
from src.models.market import BTCMarket


class ZScoreVelocityMonitorWS:
    """Monitor z-score, velocity, and orderbook using WebSockets."""

    def __init__(
        self,
        config: Config,
        output_dir: Path,
        sample_interval_ms: int = 100,
    ):
        self.config = config
        self.output_dir = output_dir
        self.sample_interval_ms = sample_interval_ms

        # Clients
        self.binance_client: Optional[BinanceClient] = None
        self.ws_client: Optional[WebSocketClient] = None
        self.trend_detector: Optional[TrendDetector] = None
        self.market_finder: Optional[MarketFinder] = None

        # Current market
        self.current_market: Optional[BTCMarket] = None
        self.strike_price: float = 0.0

        # Latest orderbook data (updated by WebSocket)
        self._up_book: Optional[BookUpdate] = None
        self._down_book: Optional[BookUpdate] = None
        self._book_update_count: int = 0
        self._price_change_count: int = 0
        self._last_poly_update_time: float = 0.0

        # Binance tick tracking
        self._binance_tick_count: int = 0
        self._binance_tick_start: float = 0.0

        # ENHANCED: Price stickiness tracking
        self._last_up_ask: float = 0.0
        self._last_up_ask_time: float = 0.0
        self._last_down_ask: float = 0.0
        self._last_down_ask_time: float = 0.0

        # ENHANCED: Rolling update frequency (1-second window)
        self._update_timestamps: list = []  # timestamps of recent book updates

        # ENHANCED: Market start time for time_into_market
        self._market_start_time: float = 0.0

        # ENHANCED: Last BTC price for 1-second change
        self._btc_1s_ago: float = 0.0
        self._btc_1s_timestamp: float = 0.0

        # Data collection
        self.samples: list = []
        self.csv_path: Optional[Path] = None

    def _on_book_update(self, update: BookUpdate):
        """Callback for WebSocket book updates (full orderbook snapshots)."""
        if not self.current_market:
            return

        now = time.time()

        if update.token_id == self.current_market.up_token_id:
            # ENHANCED: Track price stickiness
            if update.best_ask and update.best_ask != self._last_up_ask:
                self._last_up_ask = update.best_ask
                self._last_up_ask_time = now
            self._up_book = update
        elif update.token_id == self.current_market.down_token_id:
            # ENHANCED: Track price stickiness
            if update.best_ask and update.best_ask != self._last_down_ask:
                self._last_down_ask = update.best_ask
                self._last_down_ask_time = now
            self._down_book = update

        self._book_update_count += 1
        self._last_poly_update_time = now

        # ENHANCED: Track update frequency
        self._update_timestamps.append(now)
        # Keep only last 5 seconds of timestamps
        cutoff = now - 5.0
        self._update_timestamps = [t for t in self._update_timestamps if t > cutoff]

    def _on_price_change(self, change: PriceChange):
        """Callback for WebSocket price change events (bid/ask updates)."""
        if not self.current_market:
            return

        # Update our cached book with price changes
        if change.token_id == self.current_market.up_token_id:
            if change.best_bid and self._up_book:
                # Update bid
                pass  # We'll just count for now
            if change.best_ask and self._up_book:
                pass
        elif change.token_id == self.current_market.down_token_id:
            pass

        self._price_change_count += 1
        self._last_poly_update_time = time.time()

    async def initialize(self):
        """Initialize all clients."""
        print("Initializing clients...")

        # Binance client (WebSocket)
        self.binance_client = BinanceClient()
        await self.binance_client.connect()

        # Wait for Binance WebSocket to actually connect and receive first price
        # This fixes the race condition where current_price = 0.0
        print("Waiting for Binance WebSocket connection...")
        for i in range(100):  # Up to 10 seconds
            if self.binance_client.is_connected and self.binance_client.current_price > 0:
                break
            await asyncio.sleep(0.1)
            if i % 20 == 19:  # Every 2 seconds
                print(f"  Still waiting... connected={self.binance_client.is_connected}, price={self.binance_client.current_price}")

        if not self.binance_client.is_connected:
            raise RuntimeError("Failed to connect to Binance WebSocket")
        if self.binance_client.current_price <= 0:
            raise RuntimeError("Binance WebSocket connected but no price received")

        print(f"Binance connected! BTC price: ${self.binance_client.current_price:,.2f}")

        # Trend detector
        self.trend_detector = TrendDetector(self.binance_client)

        # Market finder
        self.market_finder = MarketFinder()

        # Polymarket WebSocket client
        self.ws_client = WebSocketClient(auto_reconnect=True)
        self.ws_client.on_book_update(self._on_book_update)
        self.ws_client.on_price_change(self._on_price_change)  # Also track price changes

        print("Clients initialized.")

    async def find_active_market(self) -> Optional[BTCMarket]:
        """Find the current active BTC 15-min market using slug calculation (NOT gamma API)."""
        # CRITICAL: Use get_current_market() which calculates the correct slug
        # based on current time. Do NOT use find_btc_15min_markets() which
        # queries gamma API and returns future markets!
        market = await self.market_finder.get_current_market()
        if market:
            remaining = market.time_remaining()
            print(f"Found current market via slug: {market.slug} ({remaining:.0f}s remaining)")
            return market

        # Fallback: get current and upcoming markets
        markets = await self.market_finder.get_current_and_upcoming_markets(count=3)
        for m in markets:
            if m.is_active() and m.time_remaining() > 60:
                return m

        return markets[0] if markets else None

    def get_orderbook_snapshot(self) -> Dict[str, Any]:
        """Get current orderbook prices and depth from WebSocket cache."""
        now = time.time()

        result = {
            "up_bid": 0.0,
            "up_ask": 0.0,
            "down_bid": 0.0,
            "down_ask": 0.0,
            "pair_cost": 1.0,
            "spread": 0.0,
            # ENHANCED: Orderbook depth (top 3 levels)
            "up_bid_depth_3": 0.0,
            "up_ask_depth_3": 0.0,
            "down_bid_depth_3": 0.0,
            "down_ask_depth_3": 0.0,
            # ENHANCED: Imbalance ratios
            "up_imbalance": 0.0,
            "down_imbalance": 0.0,
            # ENHANCED: Price stickiness
            "up_ask_unchanged_secs": 999.0,
            "down_ask_unchanged_secs": 999.0,
            # ENHANCED: Update frequency (per second)
            "update_freq_1s": 0.0,
        }

        if self._up_book:
            result["up_bid"] = self._up_book.best_bid or 0.0
            result["up_ask"] = self._up_book.best_ask or 1.0
            # Depth from bids/asks lists
            if hasattr(self._up_book, 'bids') and self._up_book.bids:
                result["up_bid_depth_3"] = sum(o.size for o in self._up_book.bids[:3])
            if hasattr(self._up_book, 'asks') and self._up_book.asks:
                result["up_ask_depth_3"] = sum(o.size for o in self._up_book.asks[:3])
            # Imbalance
            total = result["up_bid_depth_3"] + result["up_ask_depth_3"] + 0.001
            result["up_imbalance"] = (result["up_bid_depth_3"] - result["up_ask_depth_3"]) / total
            # Price stickiness
            if self._last_up_ask_time > 0:
                result["up_ask_unchanged_secs"] = now - self._last_up_ask_time

        if self._down_book:
            result["down_bid"] = self._down_book.best_bid or 0.0
            result["down_ask"] = self._down_book.best_ask or 1.0
            # Depth from bids/asks lists
            if hasattr(self._down_book, 'bids') and self._down_book.bids:
                result["down_bid_depth_3"] = sum(o.size for o in self._down_book.bids[:3])
            if hasattr(self._down_book, 'asks') and self._down_book.asks:
                result["down_ask_depth_3"] = sum(o.size for o in self._down_book.asks[:3])
            # Imbalance
            total = result["down_bid_depth_3"] + result["down_ask_depth_3"] + 0.001
            result["down_imbalance"] = (result["down_bid_depth_3"] - result["down_ask_depth_3"]) / total
            # Price stickiness
            if self._last_down_ask_time > 0:
                result["down_ask_unchanged_secs"] = now - self._last_down_ask_time

        # Calculate pair cost and spread
        result["pair_cost"] = result["up_ask"] + result["down_ask"]
        result["spread"] = 1.0 - result["pair_cost"]

        # Update frequency (updates in last 1 second)
        one_sec_ago = now - 1.0
        result["update_freq_1s"] = len([t for t in self._update_timestamps if t > one_sec_ago])

        return result

    def collect_sample(self, orderbook: Dict[str, Any]) -> Dict[str, Any]:
        """Collect a single sample with all metrics."""
        now = datetime.now(timezone.utc)
        now_ts = time.time()

        # Get trend signal
        trend_signal = self.trend_detector.get_trend_signal() if self.trend_detector else None

        # ENHANCED: Calculate volatility ratio
        vol_ratio = 1.0
        if self.binance_client:
            vol_ratio = self.binance_client.calculate_volatility_ratio(10, 60)

        # ENHANCED: Calculate time into market
        time_into_market = 0.0
        if self._market_start_time > 0:
            time_into_market = now_ts - self._market_start_time

        # ENHANCED: Calculate BTC 1-second change
        btc_1s_change = 0.0
        if self.binance_client and self._btc_1s_ago > 0:
            btc_1s_change = self.binance_client.current_price - self._btc_1s_ago

        # Update 1-second tracking
        if now_ts - self._btc_1s_timestamp >= 1.0:
            self._btc_1s_ago = self.binance_client.current_price if self.binance_client else 0.0
            self._btc_1s_timestamp = now_ts

        sample = {
            "timestamp": now.isoformat(),
            "epoch_ms": int(now_ts * 1000),

            # Binance data
            "btc_price": self.binance_client.current_price if self.binance_client else 0.0,
            "strike_price": self.strike_price,
            "price_vs_strike_pct": self.binance_client.price_vs_strike_pct if self.binance_client else 0.0,

            # Z-score and velocity
            "z_score": trend_signal.z_score if trend_signal else 0.0,
            "velocity_bps": trend_signal.velocity_bps if trend_signal else 0.0,
            "trend_direction": trend_signal.direction.value if trend_signal else "FLAT",
            "trend_state": trend_signal.state.value if trend_signal else "NEUTRAL",
            "confidence": trend_signal.confidence if trend_signal else 0.0,

            # Orderbook data
            "up_bid": orderbook.get("up_bid", 0.0),
            "up_ask": orderbook.get("up_ask", 0.0),
            "down_bid": orderbook.get("down_bid", 0.0),
            "down_ask": orderbook.get("down_ask", 0.0),
            "pair_cost": orderbook.get("pair_cost", 1.0),
            "spread": orderbook.get("spread", 0.0),

            # Derived metrics
            "expensive_side": "UP" if orderbook.get("up_ask", 0) > orderbook.get("down_ask", 0) else "DOWN",
            "up_down_diff": orderbook.get("up_ask", 0.5) - orderbook.get("down_ask", 0.5),

            # WebSocket stats
            "ws_book_updates": self._book_update_count,
            "ws_price_changes": self._price_change_count,
            "binance_ticks": len(self.binance_client._price_history) if self.binance_client else 0,

            # ENHANCED: Orderbook depth (top 3 levels)
            "up_bid_depth_3": orderbook.get("up_bid_depth_3", 0.0),
            "up_ask_depth_3": orderbook.get("up_ask_depth_3", 0.0),
            "down_bid_depth_3": orderbook.get("down_bid_depth_3", 0.0),
            "down_ask_depth_3": orderbook.get("down_ask_depth_3", 0.0),

            # ENHANCED: Imbalance ratios
            "up_imbalance": orderbook.get("up_imbalance", 0.0),
            "down_imbalance": orderbook.get("down_imbalance", 0.0),

            # ENHANCED: Price stickiness
            "up_ask_unchanged_secs": orderbook.get("up_ask_unchanged_secs", 999.0),
            "down_ask_unchanged_secs": orderbook.get("down_ask_unchanged_secs", 999.0),

            # ENHANCED: Update frequency (per second)
            "update_freq_1s": orderbook.get("update_freq_1s", 0.0),

            # ENHANCED: Volatility ratio
            "vol_ratio": vol_ratio,

            # ENHANCED: Time into market (seconds)
            "time_into_market": time_into_market,

            # ENHANCED: BTC 1-second price change
            "btc_1s_change": btc_1s_change,
        }

        return sample

    async def run_monitoring(self, duration_minutes: float):
        """Run the monitoring loop for specified duration."""
        print(f"\nStarting {duration_minutes} minute WebSocket monitoring session...")
        print(f"Sample interval: {self.sample_interval_ms}ms")

        # Find active market
        self.current_market = await self.find_active_market()
        if not self.current_market:
            print("ERROR: No active BTC 15-min market found!")
            return

        market_slug = self.current_market.slug
        print(f"Monitoring market: {market_slug}")
        print(f"  UP token: {self.current_market.up_token_id[:20]}...")
        print(f"  DOWN token: {self.current_market.down_token_id[:20]}...")

        # Set strike price from Binance (already verified in initialize)
        self.strike_price = self.binance_client.current_price
        self.binance_client.set_strike_price(self.strike_price)
        print(f"Strike price set: ${self.strike_price:,.2f}")

        # Connect WebSocket and subscribe
        print("Connecting to Polymarket WebSocket...")
        if not await self.ws_client.connect():
            print("ERROR: Failed to connect to WebSocket!")
            return

        token_ids = [self.current_market.up_token_id, self.current_market.down_token_id]
        if not await self.ws_client.subscribe(token_ids):
            print("ERROR: Failed to subscribe to tokens!")
            return

        print("WebSocket connected and subscribed.")

        # Start WebSocket message loop in background
        ws_task = asyncio.create_task(self.ws_client.run())

        # Create output file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = self.output_dir / f"zscore_velocity_ws_{timestamp}.csv"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # ENHANCED: Set market start time for time_into_market tracking
        # Calculate actual market start from slug (e.g., btc-updown-15m-1767988800)
        try:
            market_epoch = int(self.current_market.slug.split("-")[-1])
            self._market_start_time = float(market_epoch)
        except (ValueError, IndexError):
            self._market_start_time = time.time()  # Fallback

        # Calculate end time
        end_time = time.time() + (duration_minutes * 60)
        sample_count = 0

        print(f"\nCollecting samples to: {self.csv_path}")
        print("Press Ctrl+C to stop early.\n")

        # Track Binance tick rate
        self._binance_tick_start = time.time()
        initial_binance_ticks = len(self.binance_client._price_history) if self.binance_client else 0

        try:
            while time.time() < end_time:
                loop_start = time.time()

                # Get orderbook from WebSocket cache
                orderbook = self.get_orderbook_snapshot()

                # Collect sample
                sample = self.collect_sample(orderbook)
                self.samples.append(sample)
                sample_count += 1

                # Progress update every 100 samples (10 seconds at 100ms interval)
                if sample_count % 100 == 0:
                    remaining = int((end_time - time.time()) / 60)
                    elapsed_secs = time.time() - self._binance_tick_start
                    current_binance_ticks = len(self.binance_client._price_history) if self.binance_client else 0
                    binance_tick_rate = (current_binance_ticks - initial_binance_ticks) / max(elapsed_secs, 1)

                    # Calculate time since last Polymarket update
                    poly_last_update = ""
                    if self._last_poly_update_time > 0:
                        secs_ago = int(time.time() - self._last_poly_update_time)
                        poly_last_update = f"{secs_ago}s ago"
                    else:
                        poly_last_update = "never"

                    # ENHANCED: Show additional metrics
                    vol_r = sample.get('vol_ratio', 1.0)
                    time_in = int(sample.get('time_into_market', 0))
                    upd_freq = sample.get('update_freq_1s', 0)
                    print(
                        f"Samples: {sample_count} | "
                        f"z={sample['z_score']:.2f} | "
                        f"vel={sample['velocity_bps']:.1f}bps | "
                        f"spread={sample['spread']:.4f} | "
                        f"BTC=${sample['btc_price']:,.0f} | "
                        f"volR={vol_r:.2f} | "
                        f"t={time_in}s | "
                        f"updFreq={upd_freq:.0f}/s | "
                        f"Poly: {self._book_update_count} books ({poly_last_update}) | "
                        f"{remaining}min left"
                    )

                # Wait for next sample
                elapsed = time.time() - loop_start
                sleep_time = max(0, (self.sample_interval_ms / 1000) - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped by user.")

        # Clean up WebSocket
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass

        # Save data
        await self.save_data()

    async def save_data(self):
        """Save collected samples to CSV."""
        if not self.samples:
            print("No samples to save.")
            return

        print(f"\nSaving {len(self.samples)} samples to {self.csv_path}...")

        # Write CSV
        fieldnames = list(self.samples[0].keys())
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.samples)

        print(f"Data saved to: {self.csv_path}")

        # Print summary stats
        self.print_summary()

    def print_summary(self):
        """Print summary statistics."""
        if not self.samples:
            return

        z_scores = [s["z_score"] for s in self.samples]
        velocities = [s["velocity_bps"] for s in self.samples]
        spreads = [s["spread"] for s in self.samples]

        print("\n" + "="*70)
        print("SUMMARY STATISTICS (WebSocket)")
        print("="*70)
        duration_mins = len(self.samples) * self.sample_interval_ms / 1000 / 60
        print(f"Total samples: {len(self.samples)}")
        print(f"Duration: {duration_mins:.1f} minutes")
        print()

        # WebSocket stats
        print("WEBSOCKET STATS:")
        print(f"  Binance ticks: {len(self.binance_client._price_history) if self.binance_client else 0}")
        elapsed_secs = duration_mins * 60
        if elapsed_secs > 0 and self.binance_client:
            binance_rate = len(self.binance_client._price_history) / elapsed_secs
            print(f"  Binance tick rate: {binance_rate:.1f}/sec (sub-second confirmed)")
        print(f"  Polymarket book updates: {self._book_update_count}")
        print(f"  Polymarket price changes: {self._price_change_count}")
        total_poly = self._book_update_count + self._price_change_count
        if total_poly < 10:
            print(f"  ⚠️  LOW POLYMARKET ACTIVITY: Only {total_poly} updates in {duration_mins:.0f} min")
            print(f"      This is expected for low-liquidity markets - orderbook doesn't change often")
        print()

        print("DATA RANGES:")
        print(f"  Z-Score:  min={min(z_scores):.2f}  max={max(z_scores):.2f}  avg={sum(z_scores)/len(z_scores):.2f}")
        print(f"  Velocity: min={min(velocities):.1f}  max={max(velocities):.1f}  avg={sum(velocities)/len(velocities):.1f} bps")
        print(f"  Spread:   min={min(spreads):.4f}  max={max(spreads):.4f}  avg={sum(spreads)/len(spreads):.4f}")
        print()

        # Count profitable opportunities (spread > 0)
        profitable = len([s for s in self.samples if s["spread"] > 0])
        print(f"Profitable spread (>0): {profitable}/{len(self.samples)} ({100*profitable/len(self.samples):.1f}%)")

        # Z-score distribution
        z_strong = len([s for s in self.samples if abs(s["z_score"]) >= 2.0])
        z_mild = len([s for s in self.samples if 1.0 <= abs(s["z_score"]) < 2.0])
        z_neutral = len([s for s in self.samples if abs(s["z_score"]) < 1.0])
        print(f"Z-score distribution: Strong={z_strong}, Mild={z_mild}, Neutral={z_neutral}")
        print()

        # Explain z-score formula
        print("Z-SCORE FORMULA:")
        print("  z = abs(price_vs_strike_pct) / (std_dev_per_tick × sqrt(N_ticks))")
        print("  A $62 move from $100k = 0.062% move")
        print("  If per-tick volatility is tiny (stable market), even small moves")
        print("  are statistically significant vs random walk expectation.")
        print("  Z=5.0 means the move is 5× larger than expected random walk.")
        print("="*70)

    async def cleanup(self):
        """Clean up resources."""
        if self.ws_client:
            await self.ws_client.disconnect()
        if self.binance_client:
            await self.binance_client.disconnect()
        print("Cleanup complete.")


async def wait_for_next_market() -> float:
    """Wait until the next 15-minute market starts. Returns seconds until start."""
    now = time.time()
    # 15-min markets start at :00, :15, :30, :45 of each hour
    current_minute = (now % 3600) / 60  # Minutes into current hour
    # Find next 15-min boundary
    next_boundary_minute = ((int(current_minute) // 15) + 1) * 15
    if next_boundary_minute >= 60:
        next_boundary_minute = 0
        next_start = (now // 3600 + 1) * 3600  # Next hour
    else:
        next_start = (now // 3600) * 3600 + next_boundary_minute * 60

    wait_secs = next_start - now
    if wait_secs > 0:
        from datetime import datetime as dt
        start_time = dt.fromtimestamp(next_start)
        print(f"Waiting {wait_secs:.0f} seconds until next market at {start_time.strftime('%H:%M:%S')} local time...")
        await asyncio.sleep(wait_secs + 2)  # +2 seconds buffer for market to be discoverable
    return wait_secs


async def main():
    parser = argparse.ArgumentParser(description="Monitor z-score/velocity vs orderbook (WebSocket)")
    parser.add_argument("--duration", type=float, default=15, help="Duration in minutes (default: 15)")
    parser.add_argument("--interval", type=int, default=100, help="Sample interval in ms (default: 100)")
    parser.add_argument("--output-dir", type=str, default="research", help="Output directory (default: research)")
    parser.add_argument("--wait-for-next", action="store_true", help="Wait for next 15-min market to start")
    args = parser.parse_args()

    # Wait for next market if requested
    if args.wait_for_next:
        await wait_for_next_market()

    # Initialize
    config = Config()
    output_dir = Path(__file__).parent.parent / args.output_dir

    monitor = ZScoreVelocityMonitorWS(
        config=config,
        output_dir=output_dir,
        sample_interval_ms=args.interval,
    )

    try:
        await monitor.initialize()
        await monitor.run_monitoring(args.duration)
    finally:
        await monitor.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
