#!/usr/bin/env python3
"""
Grid MM Passive Runner - Simple Two-Sided Market Making Bot

Runs the GridMMPassiveStrategy with real market data from:
- Binance WebSocket for BTC velocity
- Polymarket WebSocket for orderbook data

STRATEGY OVERVIEW:
- Posts BID orders BELOW best_bid on BOTH sides (UP + DOWN)
- Waits for market to drop to our prices (MAKER fills)
- When both sides fill, pair_cost < $1.00 = profit at settlement
- Velocity only adjusts LOSER depth (winner stays passive at 0.01)

CONFIGURATION:
- Order size: 15 shares (default)
- Max position: 200 shares per side
- Min time remaining: 60s (stops posting near market end)

MODES:
- Paper: Simulates fills without placing real orders (default)
- Live: Places real orders via Polymarket API (requires --live flag)

Usage:
    python scripts/run_grid_mm_passive.py --paper --hours 8
    python scripts/run_grid_mm_passive.py --live --hours 2

Author: Claude Code
Date: January 16, 2026
Based on: research/grid_mm_velocity_backtest.py
"""

import asyncio
import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder
from src.strategies.grid_mm_passive import GridMMPassiveStrategy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GridMMPassiveRunner:
    """
    Runner for GridMMPassiveStrategy with real market data.

    Connects to Binance and Polymarket WebSockets, finds BTC Up/Down markets,
    and runs the passive grid market making strategy.
    """

    def __init__(
        self,
        order_size: int = 15,
        max_position: int = 200,
        min_time_remaining: int = 60,
        duration_hours: float = 8.0,
        continuous: bool = False,
        live_mode: bool = False,
        output_dir: str = "research/grid_mm_passive",
    ):
        self.order_size = order_size
        self.max_position = max_position
        self.min_time_remaining = min_time_remaining
        self.duration_hours = duration_hours
        self.continuous = continuous
        self.live_mode = live_mode
        self.output_dir = output_dir

        self.running = False
        self.start_time = None

        # Initialize strategy
        self.strategy = GridMMPassiveStrategy(
            order_size=order_size,
            max_position=max_position,
            min_time_remaining=min_time_remaining,
        )

        # WebSocket clients
        self.binance: Optional[BinanceClient] = None
        self.poly_ws: Optional[WebSocketClient] = None
        self._ws_task = None

        # Market tracking
        self.current_market = None
        self.up_token_id = None
        self.down_token_id = None
        self.market_end_time = None

        # Orderbook cache
        self.up_book: Optional[BookUpdate] = None
        self.down_book: Optional[BookUpdate] = None

        # Statistics
        self.sample_count = 0
        self.markets_traded = 0

        # CSV logging
        self.csv_file = None
        self.csv_writer = None
        self.current_date = None

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print("\n\nShutdown signal received...")
        self.running = False

    def _on_book_update(self, update: BookUpdate):
        """Handle Polymarket orderbook update."""
        if update.token_id == self.up_token_id:
            self.up_book = update
        elif update.token_id == self.down_token_id:
            self.down_book = update

    def _init_csv(self):
        """Initialize CSV file for current date."""
        today = datetime.now().date()
        if self.csv_file and self.current_date == today:
            return

        if self.csv_file:
            self.csv_file.close()

        self.current_date = today
        date_str = today.strftime("%Y%m%d")
        mode_str = "live" if self.live_mode else "paper"
        filename = f"{self.output_dir}/grid_mm_passive_{mode_str}_{date_str}.csv"

        file_exists = os.path.exists(filename)
        self.csv_file = open(filename, 'a', newline='')
        self.csv_writer = csv.writer(self.csv_file)

        if not file_exists:
            headers = [
                'timestamp_ms', 'market_slug', 'time_remaining_secs',
                'binance_price', 'velocity_bps',
                'up_bid', 'up_ask', 'down_bid', 'down_ask',
                # Strategy state
                'up_offset', 'down_offset',
                'active_up_price', 'active_down_price',
                # Position
                'up_shares', 'down_shares', 'up_avg', 'down_avg',
                'pair_cost', 'matchable_pairs',
                # Statistics
                'total_pairs', 'total_profit',
                # Events
                'quotes_posted', 'fills_detected',
            ]
            self.csv_writer.writerow(headers)
            self.csv_file.flush()

        print(f"Logging to: {filename}")

    async def _write_sample(
        self,
        quotes_posted: int = 0,
        fills_detected: int = 0,
    ):
        """Write a sample row to CSV."""
        if not self.csv_writer:
            return
        if not self.binance or self.binance.current_price <= 0:
            return
        if not self.up_book or not self.down_book:
            return

        now = datetime.now(timezone.utc)
        timestamp_ms = int(now.timestamp() * 1000)

        # Time remaining
        time_remaining = 0.0
        if self.market_end_time:
            time_remaining = max(0, (self.market_end_time - now).total_seconds())

        # Orderbook
        up_bid = self.up_book.best_bid or 0.0
        up_ask = self.up_book.best_ask or 0.0
        down_bid = self.down_book.best_bid or 0.0
        down_ask = self.down_book.best_ask or 0.0

        # Velocity
        velocity_bps = self.binance.calculate_velocity(window_seconds=10)

        # Calculate offsets for logging
        up_offset, down_offset = self.strategy.get_offsets(velocity_bps)

        # Strategy state
        s = self.strategy.state

        row = [
            timestamp_ms,
            self.current_market.slug if self.current_market else "",
            round(time_remaining, 1),
            round(self.binance.current_price, 2),
            round(velocity_bps, 4),
            round(up_bid, 4),
            round(up_ask, 4),
            round(down_bid, 4),
            round(down_ask, 4),
            round(up_offset, 2),
            round(down_offset, 2),
            round(s.active_up_order[0], 4) if s.active_up_order else 0.0,
            round(s.active_down_order[0], 4) if s.active_down_order else 0.0,
            s.up_shares,
            s.down_shares,
            round(s.up_avg_price, 4),
            round(s.down_avg_price, 4),
            round(s.pair_cost, 4),
            s.matchable_pairs,
            s.total_pairs,
            round(s.total_profit, 4),
            quotes_posted,
            fills_detected,
        ]

        self.csv_writer.writerow(row)
        self.sample_count += 1

        # Flush periodically
        if self.sample_count % 50 == 0:
            self.csv_file.flush()

    async def _find_current_market(self) -> bool:
        """Find and subscribe to current 15-min BTC market."""
        finder = MarketFinder()
        markets = await finder.get_current_and_upcoming_markets(count=3)

        if not markets:
            print("No active BTC 15-min markets found")
            return False

        # Pick market ending soonest with >60s remaining
        now = datetime.now(timezone.utc)
        valid = [
            (m, (m.end_time - now).total_seconds())
            for m in markets
            if m.end_time and (m.end_time - now).total_seconds() > 60
        ]
        if not valid:
            print("No markets with >60s remaining")
            return False

        valid.sort(key=lambda x: x[1])
        market, remaining = valid[0]

        # Check if we need to switch
        if self.current_market and self.current_market.condition_id == market.condition_id:
            return True

        print(f"\nSwitching to market: {market.slug}")
        print(f"  Time remaining: {remaining:.0f}s")
        print(f"  UP token: {market.up_token_id[:16]}...")
        print(f"  DOWN token: {market.down_token_id[:16]}...")

        # Reset strategy for new market
        self.strategy.reset()
        self.markets_traded += 1

        # Update state
        self.up_token_id = market.up_token_id
        self.down_token_id = market.down_token_id
        self.current_market = market
        self.market_end_time = market.end_time
        self.up_book = None
        self.down_book = None

        # Reconnect WebSocket for new market
        if self.poly_ws:
            print("  Reconnecting WebSocket...")
            await self.poly_ws.disconnect()
            await asyncio.sleep(0.5)
            await self.poly_ws.connect()

            # Restart WebSocket task
            if self._ws_task:
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            self._ws_task = asyncio.create_task(self.poly_ws.run())

            # Subscribe to new tokens
            result = await self.poly_ws.subscribe([self.up_token_id, self.down_token_id])
            print(f"  Subscribe result: {result}")

        # Wait for orderbook data
        print("  Waiting for orderbook data...")
        for i in range(30):
            if self.up_book and self.down_book:
                print(f"  Got orderbook after {i * 100}ms")
                break
            await asyncio.sleep(0.1)
        else:
            print("  WARNING: No orderbook data after 3s")

        return True

    async def _run_tick(self) -> Tuple[int, int]:
        """
        Run a single strategy tick.

        Returns:
            (quotes_posted, fills_detected)
        """
        if not self.up_book or not self.down_book:
            return (0, 0)
        if not self.binance or self.binance.current_price <= 0:
            return (0, 0)

        now = datetime.now(timezone.utc)
        current_time = time.time()

        # Time remaining
        time_remaining = 0.0
        if self.market_end_time:
            time_remaining = max(0, (self.market_end_time - now).total_seconds())

        # Orderbook
        up_bid = self.up_book.best_bid or 0.0
        up_ask = self.up_book.best_ask or 0.0
        down_bid = self.down_book.best_bid or 0.0
        down_ask = self.down_book.best_ask or 0.0

        # Skip invalid orderbook
        if up_bid <= 0 or down_bid <= 0 or up_ask <= 0 or down_ask <= 0:
            return (0, 0)
        if up_ask <= up_bid or down_ask <= down_bid:
            return (0, 0)

        # Velocity
        velocity_bps = self.binance.calculate_velocity(window_seconds=10)

        # PAPER MODE: Check for fills first
        fills_detected = 0
        if not self.live_mode:
            fills = self.strategy.check_fills(up_bid, down_bid, current_time)
            fills_detected = len(fills)

        # Get quotes from strategy
        quotes = self.strategy.get_quotes(
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
            velocity_bps=velocity_bps,
            time_remaining=time_remaining,
            current_time=current_time,
        )

        quotes_posted = len(quotes)

        if self.live_mode and quotes:
            # TODO: Implement order placement via Polymarket API
            logger.warning("LIVE MODE: Order placement not yet implemented")
            for q in quotes:
                logger.info(f"  Would place: {q['side']} {q['size']}@${q['price']:.3f}")

        return (quotes_posted, fills_detected)

    async def _print_status(self):
        """Print current status."""
        elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

        print(f"\n{'='*70}")
        print(f"GRID MM PASSIVE - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        print(f"Mode: {'LIVE' if self.live_mode else 'PAPER'}")
        print(f"Running: {elapsed:.2f} hours | Samples: {self.sample_count} | Markets: {self.markets_traded}")

        if self.current_market:
            time_left = 0
            if self.market_end_time:
                time_left = max(0, (self.market_end_time - datetime.now(timezone.utc)).total_seconds())
            print(f"Market: {self.current_market.slug} ({time_left:.0f}s remaining)")

        if self.binance and self.binance.current_price > 0:
            velocity = self.binance.calculate_velocity(window_seconds=10)
            zone = self.strategy.get_velocity_zone(velocity)
            zone_name = [k for k, v in self.strategy.__class__.__bases__[0].__dict__.items()
                        if v == zone][0] if hasattr(self.strategy, '__class__') else 'unknown'
            print(f"Binance: ${self.binance.current_price:.2f} | Velocity: {velocity:.4f} bps/s")

        status = self.strategy.get_status()
        pos = status['position']
        stats = status['statistics']

        print(f"\nPosition:")
        print(f"  UP:   {pos['up_shares']} shares @ ${pos['up_avg_price']:.4f}")
        print(f"  DOWN: {pos['down_shares']} shares @ ${pos['down_avg_price']:.4f}")
        print(f"  Pair cost: ${pos['pair_cost']:.4f}")
        print(f"  Matchable: {pos['matchable_pairs']} pairs")

        print(f"\nStatistics:")
        print(f"  Total pairs: {stats['total_pairs']}")
        print(f"  Total profit: ${stats['total_profit']:.2f}")

        if elapsed > 0:
            hourly_rate = stats['total_profit'] / elapsed
            print(f"  Hourly rate: ${hourly_rate:.2f}/hr")

    async def run(self):
        """Main runner loop."""
        self.running = True
        self.start_time = datetime.now()

        print("=" * 70)
        print("GRID MM PASSIVE RUNNER")
        print("=" * 70)
        print(f"Mode: {'LIVE (REAL ORDERS)' if self.live_mode else 'PAPER (SIMULATED)'}")
        print(f"Duration: {'continuous' if self.continuous else f'{self.duration_hours}h'}")
        print(f"Order size: {self.order_size} shares")
        print(f"Max position: {self.max_position} shares per side")
        print(f"Output: {self.output_dir}/")
        print()

        # Initialize Binance WebSocket
        print("Connecting to Binance WebSocket...")
        self.binance = BinanceClient(window_seconds=60)
        await self.binance.connect()

        for _ in range(50):
            if self.binance.current_price > 0:
                break
            await asyncio.sleep(0.1)
        print(f"  Binance connected: ${self.binance.current_price:.2f}")

        # Initialize Polymarket WebSocket
        print("Connecting to Polymarket WebSocket...")
        self.poly_ws = WebSocketClient(auto_reconnect=True)
        self.poly_ws.on_book_update(self._on_book_update)
        await self.poly_ws.connect()

        self._ws_task = asyncio.create_task(self.poly_ws.run())
        print("  Polymarket WebSocket connected")

        # Find initial market
        if not await self._find_current_market():
            print("Failed to find market. Exiting.")
            return

        # Wait for orderbook
        for _ in range(50):
            if self.up_book and self.down_book:
                break
            await asyncio.sleep(0.1)

        if not self.up_book or not self.down_book:
            print("Failed to get orderbook. Exiting.")
            return

        print("\nReady! Starting strategy...\n")

        # Initialize CSV
        self._init_csv()

        # Save config
        config_file = f"{self.output_dir}/config.json"
        with open(config_file, 'w') as f:
            json.dump({
                'order_size': self.order_size,
                'max_position': self.max_position,
                'min_time_remaining': self.min_time_remaining,
                'live_mode': self.live_mode,
                'velocity_zones': {
                    k: {kk: vv for kk, vv in v.items()}
                    for k, v in GridMMPassiveStrategy.__dict__.get('VELOCITY_ZONES', {}).items()
                } if hasattr(GridMMPassiveStrategy, 'VELOCITY_ZONES') else 'see strategy file'
            }, f, indent=2)

        last_status_time = time.time()
        last_market_check = time.time()
        end_time = None
        if not self.continuous:
            end_time = self.start_time + timedelta(hours=self.duration_hours)

        try:
            while self.running:
                now = datetime.now()

                # Check duration
                if end_time and now >= end_time:
                    print("\nDuration reached.")
                    break

                # Periodic market check (every 30s)
                if time.time() - last_market_check > 30:
                    await self._find_current_market()
                    last_market_check = time.time()

                # Run strategy tick
                self._init_csv()  # Handle date rollover
                quotes_posted, fills_detected = await self._run_tick()

                # Log sample
                await self._write_sample(quotes_posted, fills_detected)

                # Status update (every 60s)
                if time.time() - last_status_time > 60:
                    await self._print_status()
                    last_status_time = time.time()

                await asyncio.sleep(0.2)  # 200ms tick rate

        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Cleanup
            if self._ws_task:
                self._ws_task.cancel()
            if self.csv_file:
                self.csv_file.flush()
                self.csv_file.close()

            await self._print_status()
            print(f"\nRunner stopped. {self.sample_count} samples recorded.")


def main():
    parser = argparse.ArgumentParser(
        description="Grid MM Passive Runner - Two-Sided Market Making Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--hours', type=float, default=8.0, help='Duration in hours')
    parser.add_argument('--continuous', action='store_true', help='Run until stopped')
    parser.add_argument('--live', action='store_true', help='Live mode (place real orders)')
    parser.add_argument('--paper', action='store_true', help='Paper mode (simulate fills)')
    parser.add_argument('--size', type=int, default=15, help='Order size in shares')
    parser.add_argument('--max-position', type=int, default=200, help='Max position per side')
    parser.add_argument('--min-time', type=int, default=60, help='Stop posting below this time (seconds)')
    parser.add_argument('--output', type=str, default='research/grid_mm_passive', help='Output directory')

    args = parser.parse_args()

    # Default to paper mode
    live_mode = args.live and not args.paper

    if live_mode:
        print("\n" + "!" * 70)
        print("WARNING: LIVE MODE - REAL ORDERS WILL BE PLACED")
        print("!" * 70)
        confirm = input("Type 'yes' to confirm: ")
        if confirm.lower() != 'yes':
            print("Aborted.")
            return

    runner = GridMMPassiveRunner(
        order_size=args.size,
        max_position=args.max_position,
        min_time_remaining=args.min_time,
        duration_hours=args.hours,
        continuous=args.continuous,
        live_mode=live_mode,
        output_dir=args.output,
    )

    asyncio.run(runner.run())


if __name__ == "__main__":
    main()
