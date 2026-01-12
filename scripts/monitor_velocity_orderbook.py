#!/usr/bin/env python3
"""
100ms Monitoring Script for Velocity vs Orderbook Correlation

Collects data to understand:
1. How does velocity lead or lag orderbook price changes?
2. Does velocity catch sudden BTC moves?
3. How does Binance price correlate with Polymarket orderbook (Chainlink lag)?

Usage:
    python scripts/monitor_velocity_orderbook.py --duration 15
    # Monitors for 15 minutes (1 market cycle)

Output:
    research/velocity_orderbook_YYYYMMDD_HHMMSS.csv
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
from src.api.polymarket_client import PolymarketClient
from src.api.binance_client import BinanceClient
from src.services.market_finder import MarketFinder
from src.services.trend_detector import TrendDetector
from src.models.market import BTCMarket


class VelocityOrderbookMonitor:
    """Monitor velocity and orderbook at 100ms intervals."""

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
        self.poly_client: Optional[PolymarketClient] = None
        self.binance_client: Optional[BinanceClient] = None
        self.trend_detector: Optional[TrendDetector] = None
        self.market_finder: Optional[MarketFinder] = None

        # Current market
        self.current_market: Optional[BTCMarket] = None
        self.strike_price: float = 0.0

        # Data collection
        self.samples: list = []
        self.csv_path: Optional[Path] = None

    async def initialize(self):
        """Initialize all clients."""
        print("Initializing clients...")

        # Polymarket client
        self.poly_client = PolymarketClient(self.config)
        await self.poly_client.connect()

        # Binance client
        self.binance_client = BinanceClient()
        await self.binance_client.connect()

        # Trend detector
        self.trend_detector = TrendDetector(self.binance_client)

        # Market finder (standalone - doesn't need PolymarketClient)
        self.market_finder = MarketFinder()

        print("Clients initialized.")

    async def find_active_market(self) -> Optional[BTCMarket]:
        """Find the current active BTC 15-min market."""
        markets = await self.market_finder.find_btc_15min_markets()
        if not markets:
            return None

        # Find the market closest to resolution but still active
        for market in markets:
            remaining = market.time_remaining()
            if 60 < remaining < 900:  # Between 1 and 15 minutes remaining
                return market

        # Return first market if none in sweet spot
        return markets[0] if markets else None

    async def get_orderbook_snapshot(self) -> Dict[str, float]:
        """Get current orderbook prices."""
        if not self.current_market:
            return {}

        # Get tokens from BTCMarket
        up_token = self.current_market.up_token_id
        down_token = self.current_market.down_token_id

        result = {
            "up_bid": 0.0,
            "up_ask": 0.0,
            "down_bid": 0.0,
            "down_ask": 0.0,
            "pair_cost": 1.0,
            "spread": 0.0,
        }

        try:
            # Get orderbooks
            if up_token:
                up_book = await self.poly_client.get_orderbook(up_token)
                if up_book:
                    # Handle OrderBookSummary object or dict
                    bids = up_book.bids if hasattr(up_book, 'bids') else up_book.get("bids", [])
                    asks = up_book.asks if hasattr(up_book, 'asks') else up_book.get("asks", [])
                    if bids:
                        # Get best (highest) bid
                        if hasattr(bids[0], 'price'):
                            result["up_bid"] = max(float(b.price) for b in bids)
                        else:
                            result["up_bid"] = max(float(b["price"]) for b in bids)
                    if asks:
                        # Get best (lowest) ask
                        if hasattr(asks[0], 'price'):
                            result["up_ask"] = min(float(a.price) for a in asks)
                        else:
                            result["up_ask"] = min(float(a["price"]) for a in asks)

            if down_token:
                down_book = await self.poly_client.get_orderbook(down_token)
                if down_book:
                    bids = down_book.bids if hasattr(down_book, 'bids') else down_book.get("bids", [])
                    asks = down_book.asks if hasattr(down_book, 'asks') else down_book.get("asks", [])
                    if bids:
                        # Get best (highest) bid
                        if hasattr(bids[0], 'price'):
                            result["down_bid"] = max(float(b.price) for b in bids)
                        else:
                            result["down_bid"] = max(float(b["price"]) for b in bids)
                    if asks:
                        # Get best (lowest) ask
                        if hasattr(asks[0], 'price'):
                            result["down_ask"] = min(float(a.price) for a in asks)
                        else:
                            result["down_ask"] = min(float(a["price"]) for a in asks)

            # Calculate pair cost and spread
            result["pair_cost"] = result["up_ask"] + result["down_ask"]
            result["spread"] = 1.0 - result["pair_cost"]

        except Exception as e:
            print(f"Error getting orderbook: {e}")

        return result

    def collect_sample(self, orderbook: Dict[str, float]) -> Dict[str, Any]:
        """Collect a single sample with all metrics."""
        now = datetime.now(timezone.utc)

        # Get trend signal
        trend_signal = self.trend_detector.get_trend_signal() if self.trend_detector else None

        sample = {
            "timestamp": now.isoformat(),
            "epoch_ms": int(time.time() * 1000),

            # Binance data
            "btc_price": self.binance_client.current_price if self.binance_client else 0.0,
            "strike_price": self.strike_price,
            "price_vs_strike_pct": self.binance_client.price_vs_strike_pct if self.binance_client else 0.0,

            # Velocity-based metrics
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
        }

        return sample

    async def run_monitoring(self, duration_minutes: float):
        """Run the monitoring loop for specified duration."""
        print(f"\nStarting {duration_minutes} minute monitoring session...")
        print(f"Sample interval: {self.sample_interval_ms}ms")

        # Find active market
        self.current_market = await self.find_active_market()
        if not self.current_market:
            print("ERROR: No active BTC 15-min market found!")
            return

        market_slug = self.current_market.slug
        print(f"Monitoring market: {market_slug}")

        # Set strike price from Binance
        if self.binance_client:
            self.strike_price = self.binance_client.current_price
            self.binance_client.set_strike_price(self.strike_price)
            print(f"Strike price set: ${self.strike_price:,.2f}")

        # Create output file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.csv_path = self.output_dir / f"velocity_orderbook_{timestamp}.csv"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Calculate end time
        end_time = time.time() + (duration_minutes * 60)
        sample_count = 0

        print(f"\nCollecting samples to: {self.csv_path}")
        print("Press Ctrl+C to stop early.\n")

        try:
            while time.time() < end_time:
                loop_start = time.time()

                # Get orderbook
                orderbook = await self.get_orderbook_snapshot()

                # Collect sample
                sample = self.collect_sample(orderbook)
                self.samples.append(sample)
                sample_count += 1

                # Progress update every 100 samples
                if sample_count % 100 == 0:
                    remaining = int((end_time - time.time()) / 60)
                    print(
                        f"Samples: {sample_count} | "
                        f"vel={sample['velocity_bps']:.3f}bps | "
                        f"state={sample['trend_state']} | "
                        f"spread={sample['spread']:.4f} | "
                        f"{remaining}min left"
                    )

                # Wait for next sample
                elapsed = time.time() - loop_start
                sleep_time = max(0, (self.sample_interval_ms / 1000) - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        except KeyboardInterrupt:
            print("\n\nMonitoring stopped by user.")

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

        velocities = [s["velocity_bps"] for s in self.samples]
        spreads = [s["spread"] for s in self.samples]

        print("\n" + "="*60)
        print("SUMMARY STATISTICS")
        print("="*60)
        print(f"Total samples: {len(self.samples)}")
        print(f"Duration: {len(self.samples) * self.sample_interval_ms / 1000 / 60:.1f} minutes")
        print()
        print(f"Velocity: min={min(velocities):.3f}  max={max(velocities):.3f}  avg={sum(velocities)/len(velocities):.3f} bps")
        print(f"Spread:   min={min(spreads):.4f}  max={max(spreads):.4f}  avg={sum(spreads)/len(spreads):.4f}")
        print()

        # Count profitable opportunities (spread > 0)
        profitable = len([s for s in self.samples if s["spread"] > 0])
        print(f"Profitable spread (>0): {profitable}/{len(self.samples)} ({100*profitable/len(self.samples):.1f}%)")

        # Velocity distribution (based on trend state)
        vel_extreme = len([s for s in self.samples if abs(s["velocity_bps"]) >= 0.10])
        vel_strong = len([s for s in self.samples if 0.05 <= abs(s["velocity_bps"]) < 0.10])
        vel_mild = len([s for s in self.samples if 0.02 <= abs(s["velocity_bps"]) < 0.05])
        vel_neutral = len([s for s in self.samples if abs(s["velocity_bps"]) < 0.02])
        print(f"Velocity distribution: Extreme={vel_extreme}, Strong={vel_strong}, Mild={vel_mild}, Neutral={vel_neutral}")
        print("="*60)

    async def cleanup(self):
        """Clean up resources."""
        if self.binance_client:
            await self.binance_client.disconnect()
        print("Cleanup complete.")


async def main():
    parser = argparse.ArgumentParser(description="Monitor velocity vs orderbook correlation")
    parser.add_argument("--duration", type=float, default=15, help="Duration in minutes (default: 15)")
    parser.add_argument("--interval", type=int, default=100, help="Sample interval in ms (default: 100)")
    parser.add_argument("--output-dir", type=str, default="research", help="Output directory (default: research)")
    args = parser.parse_args()

    # Initialize
    config = Config()
    output_dir = Path(__file__).parent.parent / args.output_dir

    monitor = VelocityOrderbookMonitor(
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
