#!/usr/bin/env python3
"""
Spread Capture Observer - Real WebSocket Data Collection

Captures REAL market data at sub-second speeds for strategy analysis.
NO simulated fills - just logs what the strategy WOULD do.

Features:
1. Real Binance WebSocket for velocity (100-200ms)
2. Real Polymarket WebSocket for orderbook (100-500ms)
3. Three parameter scenarios in parallel (default/conservative/aggressive)
4. Theoretical position tracking with merge profit calculation
5. Crash-safe CSV streaming

Usage:
    python scripts/spread_capture_observer.py --hours 12
    python scripts/spread_capture_observer.py --continuous
    python scripts/spread_capture_observer.py --hours 8 --interval 100
"""

import asyncio
import csv
import os
import signal
import sys
import time
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum

import aiohttp

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.binance_client import BinanceClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder


# =============================================================================
# PARAMETER SCENARIOS
# =============================================================================

@dataclass
class ScenarioParams:
    """Parameters for a strategy scenario."""
    name: str
    velocity_threshold: float  # bps/sec to trigger directional offset
    velocity_strong: float     # bps/sec for strong signal
    base_offset: float         # Neutral zone offset
    tight_offset: float        # Aggressive offset (negative = bid above best_bid)
    wide_offset: float         # Conservative offset
    very_wide_offset: float    # Very conservative offset


SCENARIOS = {
    "default": ScenarioParams(
        name="default",
        velocity_threshold=0.05,
        velocity_strong=0.10,
        base_offset=0.02,
        tight_offset=-0.01,
        wide_offset=0.02,
        very_wide_offset=0.04,
    ),
    "conservative": ScenarioParams(
        name="conservative",
        velocity_threshold=0.08,
        velocity_strong=0.15,
        base_offset=0.025,
        tight_offset=0.00,
        wide_offset=0.03,
        very_wide_offset=0.05,
    ),
    "aggressive": ScenarioParams(
        name="aggressive",
        velocity_threshold=0.03,
        velocity_strong=0.07,
        base_offset=0.015,
        tight_offset=-0.02,
        wide_offset=0.015,
        very_wide_offset=0.03,
    ),
}


class VelocityZone(Enum):
    NEUTRAL = "neutral"
    MODERATE = "moderate"
    STRONG = "strong"


# =============================================================================
# THEORETICAL POSITION TRACKER
# =============================================================================

@dataclass
class TheoreticalPosition:
    """Track theoretical position for a scenario."""
    up_shares: float = 0.0
    down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0

    @property
    def pairs(self) -> int:
        return int(min(self.up_shares, self.down_shares))

    @property
    def up_avg_price(self) -> float:
        return self.up_cost / self.up_shares if self.up_shares > 0 else 0.0

    @property
    def down_avg_price(self) -> float:
        return self.down_cost / self.down_shares if self.down_shares > 0 else 0.0

    @property
    def locked_profit(self) -> float:
        """Profit locked in by pairs (merge value - cost)."""
        if self.pairs == 0:
            return 0.0
        pair_cost = self.up_avg_price + self.down_avg_price
        return self.pairs * (1.00 - pair_cost)

    def add_fill(self, side: str, price: float, size: float):
        """Add a theoretical fill."""
        cost = price * size
        if side == "UP":
            self.up_shares += size
            self.up_cost += cost
        else:
            self.down_shares += size
            self.down_cost += cost

    def reset(self):
        """Reset for new market."""
        self.up_shares = 0.0
        self.down_shares = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0


# =============================================================================
# SIGNAL GENERATOR
# =============================================================================

def get_velocity_zone(velocity_bps: float, params: ScenarioParams) -> VelocityZone:
    """Determine velocity zone for given params."""
    abs_vel = abs(velocity_bps)
    if abs_vel < params.velocity_threshold:
        return VelocityZone.NEUTRAL
    elif abs_vel < params.velocity_strong:
        return VelocityZone.MODERATE
    else:
        return VelocityZone.STRONG


def get_offsets(velocity_bps: float, params: ScenarioParams, inventory_bias: float = 0) -> tuple:
    """
    Get (up_offset, down_offset) for given velocity and params.
    Mirrors spread_capture.py logic.
    """
    abs_velocity = abs(velocity_bps)

    # Neutral zone - use inventory to decide
    if abs_velocity < params.velocity_threshold:
        if abs(inventory_bias) <= 2:
            return (params.tight_offset, params.tight_offset)
        elif inventory_bias > 0:
            return (params.tight_offset, params.wide_offset)
        else:
            return (params.wide_offset, params.tight_offset)

    # Strong velocity
    if abs_velocity > params.velocity_strong:
        if velocity_bps > 0:  # BTC rising
            return (params.tight_offset, params.very_wide_offset)
        else:  # BTC falling
            return (params.very_wide_offset, params.tight_offset)

    # Moderate velocity
    if velocity_bps > 0:  # BTC rising
        return (params.tight_offset, params.wide_offset)
    else:  # BTC falling
        return (params.wide_offset, params.tight_offset)


def would_fill(our_bid: float, best_bid: float, best_ask: float) -> bool:
    """
    Determine if our bid would likely fill.
    - If our_bid >= best_ask: instant fill (lift the ask)
    - If our_bid >= best_bid + 0.005: very likely fill (top of book)
    """
    if our_bid >= best_ask:
        return True
    if our_bid >= best_bid + 0.005:
        return True
    return False


# =============================================================================
# MAIN OBSERVER
# =============================================================================

class SpreadCaptureObserver:
    """Real-time spread capture observation with WebSocket data."""

    def __init__(
        self,
        duration_hours: float = 12.0,
        continuous: bool = False,
        sample_interval_ms: int = 200,
        output_dir: str = "research/observer",
        trade_size: float = 5.0,
    ):
        self.duration_hours = duration_hours
        self.continuous = continuous
        self.sample_interval_ms = sample_interval_ms
        self.output_dir = output_dir
        self.trade_size = trade_size

        self.running = False
        self.start_time = None

        # WebSocket clients
        self.binance: Optional[BinanceClient] = None
        self.poly_ws: Optional[WebSocketClient] = None

        # Market tracking
        self.current_market = None
        self.up_token_id = None
        self.down_token_id = None
        self.market_end_time = None

        # Orderbook cache (updated by WebSocket)
        self.up_book: Optional[BookUpdate] = None
        self.down_book: Optional[BookUpdate] = None
        self._subscription_time = 0.0

        # Theoretical positions per scenario
        self.positions: Dict[str, TheoreticalPosition] = {
            name: TheoreticalPosition() for name in SCENARIOS.keys()
        }

        # CSV logging
        self.csv_file = None
        self.csv_writer = None
        self.current_date = None
        self.sample_count = 0

        # Create output directory
        os.makedirs(output_dir, exist_ok=True)

        # Signal handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        print("\n\nShutdown signal received...")
        self.running = False

    def _on_book_update(self, update: BookUpdate):
        """Handle Polymarket orderbook update."""
        # Only accept updates for current market's tokens
        if update.token_id == self.up_token_id:
            self.up_book = update
        elif update.token_id == self.down_token_id:
            self.down_book = update
        # Ignore updates for old tokens (after market rotation)

    def _is_garbage_orderbook(self, up_bid: float, up_ask: float, down_bid: float, down_ask: float) -> bool:
        """
        Detect garbage orderbook data.

        Only flag truly invalid data:
        - Zeros/empty orderbook
        - No price movement over many samples (track via _last_prices)

        NOTE: Static $0.49/$0.51 early in market is VALID.
        NOTE: Extreme prices ($0.01 or $0.99) near end are VALID.
        """
        # Check for empty/zero prices (definitely garbage)
        if up_bid <= 0 or down_bid <= 0 or up_ask <= 0 or down_ask <= 0:
            return True
        return False

    async def _fetch_clob_prices(self) -> Optional[Tuple[float, float, float, float]]:
        """Fetch prices from CLOB REST endpoint as fallback."""
        if not self.current_market:
            return None
        url = f"https://clob.polymarket.com/markets/{self.current_market.condition_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return self._extract_prices_from_clob(data)
        except Exception as e:
            logger.error(f"CLOB REST fetch failed: {e}")
        return None

    def _extract_prices_from_clob(self, clob_data: dict) -> Tuple[float, float, float, float]:
        """Extract UP/DOWN prices from CLOB response, create synthetic bid/ask."""
        up_price = down_price = 0.5
        for t in clob_data.get('tokens', []):
            outcome = t.get('outcome', '').upper()
            price = float(t.get('price', 0.5))
            if outcome in ('YES', 'UP'):
                up_price = price
            elif outcome in ('NO', 'DOWN'):
                down_price = price
        return (
            max(0.01, up_price - 0.005),
            min(0.99, up_price + 0.005),
            max(0.01, down_price - 0.005),
            min(0.99, down_price + 0.005),
        )

    async def _get_validated_orderbook(self) -> Optional[Tuple[float, float, float, float, str]]:
        """Get validated orderbook with garbage detection and REST fallback."""
        if self.up_book and self.down_book:
            up_bid = self.up_book.best_bid or 0.0
            up_ask = self.up_book.best_ask or 0.0
            down_bid = self.down_book.best_bid or 0.0
            down_ask = self.down_book.best_ask or 0.0
            if not self._is_garbage_orderbook(up_bid, up_ask, down_bid, down_ask):
                return (up_bid, up_ask, down_bid, down_ask, "websocket")
        # Fallback to REST
        prices = await self._fetch_clob_prices()
        if prices:
            return (*prices, "rest_fallback")
        return None

    def _init_csv(self):
        """Initialize CSV file for current date."""
        today = datetime.now().date()
        if self.csv_file and self.current_date == today:
            return

        if self.csv_file:
            self.csv_file.close()

        self.current_date = today
        date_str = today.strftime("%Y%m%d")
        filename = f"{self.output_dir}/spread_capture_obs_{date_str}.csv"

        file_exists = os.path.exists(filename)
        self.csv_file = open(filename, 'a', newline='')
        self.csv_writer = csv.writer(self.csv_file)

        if not file_exists:
            headers = [
                'timestamp_ms', 'market_slug', 'time_remaining_secs',
                'binance_price', 'velocity_bps',
                'up_bid', 'up_ask', 'down_bid', 'down_ask', 'pair_cost',
                'data_source',
            ]
            # Add columns for each scenario
            for scenario in SCENARIOS.keys():
                prefix = scenario[:4]  # default->defa, conservative->cons, aggressive->aggr
                headers.extend([
                    f'{prefix}_zone', f'{prefix}_entry_signal', f'{prefix}_entry_side',
                    f'{prefix}_up_offset', f'{prefix}_down_offset',
                    f'{prefix}_entry_price', f'{prefix}_hedge_price',
                    f'{prefix}_would_fill_entry', f'{prefix}_would_fill_hedge',
                    f'{prefix}_up_pos', f'{prefix}_down_pos',
                    f'{prefix}_pairs', f'{prefix}_locked_profit',
                ])
            self.csv_writer.writerow(headers)
            self.csv_file.flush()

        print(f"Logging to: {filename}")

    async def _write_sample(self):
        """Write a sample row to CSV with validated orderbook."""
        if not self.csv_writer:
            return
        if not self.binance or self.binance.current_price <= 0:
            return

        # Get validated orderbook (with garbage detection and REST fallback)
        orderbook = await self._get_validated_orderbook()
        if not orderbook:
            return

        up_bid, up_ask, down_bid, down_ask, data_source = orderbook

        now = datetime.now(timezone.utc)
        timestamp_ms = int(now.timestamp() * 1000)
        pair_cost = up_ask + down_ask

        # Time remaining
        time_remaining = 0.0
        if self.market_end_time:
            time_remaining = max(0, (self.market_end_time - now).total_seconds())

        # Binance data
        velocity_bps = self.binance.calculate_velocity(window_seconds=10)

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
            round(pair_cost, 4),
            data_source,
        ]

        # Generate signals for each scenario
        for scenario_name, params in SCENARIOS.items():
            pos = self.positions[scenario_name]
            inventory_bias = pos.up_shares - pos.down_shares

            # Get velocity zone and offsets
            zone = get_velocity_zone(velocity_bps, params)
            up_offset, down_offset = get_offsets(velocity_bps, params, inventory_bias)

            # Calculate bid prices
            entry_price_up = max(0.01, min(0.95, up_bid - up_offset))
            entry_price_down = max(0.01, min(0.95, down_bid - down_offset))

            # Determine entry side (buy predicted winner first)
            if velocity_bps > params.velocity_threshold:
                entry_side = "UP"
                entry_price = entry_price_up
                hedge_price = entry_price_down
                entry_would_fill = would_fill(entry_price_up, up_bid, up_ask)
                hedge_would_fill = would_fill(entry_price_down, down_bid, down_ask)
            elif velocity_bps < -params.velocity_threshold:
                entry_side = "DOWN"
                entry_price = entry_price_down
                hedge_price = entry_price_up
                entry_would_fill = would_fill(entry_price_down, down_bid, down_ask)
                hedge_would_fill = would_fill(entry_price_up, up_bid, up_ask)
            else:
                # Neutral - no directional signal
                entry_side = "NONE"
                entry_price = 0.0
                hedge_price = 0.0
                entry_would_fill = False
                hedge_would_fill = False

            # Entry signal: velocity signal (pair_cost is a RESULT, not a gate)
            entry_signal = entry_side != "NONE"

            # Track theoretical fills (only if both would fill)
            if entry_signal and entry_would_fill and hedge_would_fill:
                if entry_side == "UP":
                    pos.add_fill("UP", entry_price, self.trade_size)
                    pos.add_fill("DOWN", hedge_price, self.trade_size)
                else:
                    pos.add_fill("DOWN", entry_price, self.trade_size)
                    pos.add_fill("UP", hedge_price, self.trade_size)

            prefix = scenario_name[:4]
            row.extend([
                zone.value,
                entry_signal,
                entry_side,
                round(up_offset, 4),
                round(down_offset, 4),
                round(entry_price, 4),
                round(hedge_price, 4),
                entry_would_fill,
                hedge_would_fill,
                round(pos.up_shares, 1),
                round(pos.down_shares, 1),
                pos.pairs,
                round(pos.locked_profit, 4),
            ])

        self.csv_writer.writerow(row)
        self.sample_count += 1

        # Flush periodically
        if self.sample_count % 50 == 0:
            self.csv_file.flush()

    async def _find_current_market(self) -> bool:
        """Find and subscribe to current 15-min BTC market using REST API."""
        finder = MarketFinder()
        # Use slug-based method - find_btc_15min_markets() doesn't work for btc-updown
        markets = await finder.get_current_and_upcoming_markets(count=3)

        if not markets:
            print("No active BTC 15-min markets found")
            return False

        # Pick market ending SOONEST with >60s remaining
        now = datetime.now(timezone.utc)
        valid = [(m, (m.end_time - now).total_seconds()) for m in markets if m.end_time and (m.end_time - now).total_seconds() > 60]
        if not valid:
            print("No markets with >60s remaining")
            return False
        valid.sort(key=lambda x: x[1])
        market, remaining = valid[0]
        print(f"Selected market with {remaining:.0f}s remaining (of {len(markets)} found)")

        # Check if we need to switch
        if self.current_market and self.current_market.condition_id == market.condition_id:
            return True

        print(f"\nSwitching to market: {market.slug}", flush=True)
        print(f"  UP token: {market.up_token_id[:16]}...", flush=True)
        print(f"  DOWN token: {market.down_token_id[:16]}...", flush=True)

        # Reset positions for new market
        for pos in self.positions.values():
            pos.reset()

        # Update state to new market
        self.up_token_id = market.up_token_id
        self.down_token_id = market.down_token_id
        self.current_market = market
        self.market_end_time = market.end_time
        self.up_book = None
        self.down_book = None

        # CRITICAL: Polymarket WebSocket doesn't properly switch subscriptions
        # Must reconnect to get fresh subscription for new tokens
        if self.poly_ws:
            print("  Reconnecting WebSocket for new market...", flush=True)
            await self.poly_ws.disconnect()
            await asyncio.sleep(0.5)
            await self.poly_ws.connect()
            # Restart the WebSocket event loop
            if hasattr(self, '_ws_task') and self._ws_task:
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            self._ws_task = asyncio.create_task(self.poly_ws.run())
            # Subscribe to new market tokens
            result = await self.poly_ws.subscribe([self.up_token_id, self.down_token_id])
            print(f"  Subscribe result: {result}", flush=True)

        # Wait for first orderbook data
        print("  Waiting for orderbook data...", flush=True)
        for i in range(30):  # 3 seconds max
            if self.up_book and self.down_book:
                print(f"  Got orderbook after {i * 100}ms", flush=True)
                break
            await asyncio.sleep(0.1)
        else:
            print("  No WebSocket data after 3s, using REST fallback", flush=True)

        return True

    async def _print_status(self):
        """Print current status."""
        elapsed = (datetime.now() - self.start_time).total_seconds() / 3600

        print(f"\n{'='*70}")
        print(f"SPREAD CAPTURE OBSERVER - {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'='*70}")
        print(f"Running: {elapsed:.2f} hours | Samples: {self.sample_count}")

        if self.current_market:
            time_left = 0
            if self.market_end_time:
                time_left = max(0, (self.market_end_time - datetime.now(timezone.utc)).total_seconds())
            print(f"Market: {self.current_market.slug} ({time_left:.0f}s remaining)")

        if self.binance and self.binance.current_price > 0:
            velocity = self.binance.calculate_velocity(window_seconds=10)
            print(f"Binance: ${self.binance.current_price:.2f} | Velocity: {velocity:.4f} bps/s")

        if self.up_book and self.down_book:
            pair_cost = (self.up_book.best_ask or 0) + (self.down_book.best_ask or 0)
            print(f"Pair Cost: ${pair_cost:.4f}")

        print(f"\nTheoretical Positions:")
        for name, pos in self.positions.items():
            print(f"  {name:12}: UP={pos.up_shares:.0f} DOWN={pos.down_shares:.0f} "
                  f"Pairs={pos.pairs} Locked=${pos.locked_profit:.2f}")

    async def run(self):
        """Main observation loop."""
        self.running = True
        self.start_time = datetime.now()

        print("=" * 70)
        print("SPREAD CAPTURE OBSERVER")
        print("=" * 70)
        print(f"Duration: {'continuous' if self.continuous else f'{self.duration_hours}h'}")
        print(f"Sample interval: {self.sample_interval_ms}ms")
        print(f"Output: {self.output_dir}/")
        print(f"\nScenarios:")
        for name, params in SCENARIOS.items():
            print(f"  {name}: vel_thresh={params.velocity_threshold}, "
                  f"tight={params.tight_offset}, wide={params.wide_offset}")
        print()

        # Initialize Binance WebSocket
        print("Connecting to Binance WebSocket...")
        self.binance = BinanceClient(window_seconds=60)
        await self.binance.connect()

        # Wait for first price
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

        # Start WebSocket event loop in background
        self._ws_task = asyncio.create_task(self.poly_ws.run())
        print("  Polymarket WebSocket connected")

        # Find initial market
        if not await self._find_current_market():
            print("Failed to find market. Exiting.")
            return

        # Wait for orderbook data
        print("Waiting for orderbook data...")
        for _ in range(50):
            if self.up_book and self.down_book:
                break
            await asyncio.sleep(0.1)

        if not self.up_book or not self.down_book:
            print("Failed to get orderbook data. Exiting.")
            return

        print("Ready! Starting observation...\n")

        # Initialize CSV
        self._init_csv()

        # Save params
        params_file = f"{self.output_dir}/spread_capture_params.json"
        with open(params_file, 'w') as f:
            json.dump({
                name: {
                    "velocity_threshold": p.velocity_threshold,
                    "velocity_strong": p.velocity_strong,
                    "base_offset": p.base_offset,
                    "tight_offset": p.tight_offset,
                    "wide_offset": p.wide_offset,
                    "very_wide_offset": p.very_wide_offset,
                }
                for name, p in SCENARIOS.items()
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

                # Write sample
                self._init_csv()  # Handle date rollover
                await self._write_sample()

                # Status update (every 60s)
                if time.time() - last_status_time > 60:
                    await self._print_status()
                    last_status_time = time.time()

                await asyncio.sleep(self.sample_interval_ms / 1000)

        except Exception as e:
            print(f"\nError: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Cleanup
            if hasattr(self, '_ws_task') and self._ws_task:
                self._ws_task.cancel()
            if self.csv_file:
                self.csv_file.flush()
                self.csv_file.close()

            await self._print_status()
            print(f"\nObservation complete. {self.sample_count} samples recorded.")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Spread Capture Observer - Real WebSocket Data Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--hours', type=float, default=12.0, help='Duration in hours')
    parser.add_argument('--continuous', action='store_true', help='Run until stopped')
    parser.add_argument('--interval', type=int, default=200, help='Sample interval in ms')
    parser.add_argument('--output', type=str, default='research/observer', help='Output directory')

    args = parser.parse_args()

    observer = SpreadCaptureObserver(
        duration_hours=args.hours,
        continuous=args.continuous,
        sample_interval_ms=args.interval,
        output_dir=args.output,
    )

    asyncio.run(observer.run())


if __name__ == "__main__":
    main()
