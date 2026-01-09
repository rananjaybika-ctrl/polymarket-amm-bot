#!/usr/bin/env python3
"""
WebSocket-based spread analyzer for detecting sub-second negative spread opportunities.

Connects to Polymarket's WebSocket API for real-time orderbook updates,
tracking pair cost (UP ask + DOWN ask) at millisecond resolution.
"""

import asyncio
import sys
import os
import json
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.websocket_client import WebSocketClient, BookUpdate
from src.api.polymarket_client import PolymarketClient
from src.config import Config


@dataclass
class SpreadSnapshot:
    """A single spread observation."""
    timestamp: datetime
    up_ask: float
    down_ask: float
    pair_cost: float
    edge_pct: float
    latency_ms: float = 0  # Time since last update


@dataclass
class OpportunityWindow:
    """Track a negative spread opportunity window."""
    start_time: datetime
    end_time: Optional[datetime] = None
    min_pair_cost: float = 1.0
    max_edge_pct: float = 0.0
    samples: int = 0
    snapshots: List[SpreadSnapshot] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if self.end_time is None:
            return 0
        return (self.end_time - self.start_time).total_seconds() * 1000

    @property
    def is_open(self) -> bool:
        return self.end_time is None


class WebSocketSpreadAnalyzer:
    """
    Real-time spread analyzer using WebSocket for sub-second detection.
    """

    def __init__(
        self,
        up_token_id: str,
        down_token_id: str,
        market_slug: str = "unknown",
        threshold: float = 1.0,
    ):
        self.up_token_id = up_token_id
        self.down_token_id = down_token_id
        self.market_slug = market_slug
        self.threshold = threshold

        # Current best asks
        self.up_ask: Optional[float] = None
        self.down_ask: Optional[float] = None

        # Timestamps for latency tracking
        self.up_last_update: Optional[datetime] = None
        self.down_last_update: Optional[datetime] = None

        # Statistics
        self.total_updates = 0
        self.sub_threshold_count = 0
        self.opportunities: List[OpportunityWindow] = []
        self.current_window: Optional[OpportunityWindow] = None
        self.all_snapshots: List[SpreadSnapshot] = []

        # Update rate tracking
        self.update_times: List[datetime] = []

        # WebSocket client
        self.ws_client = WebSocketClient(auto_reconnect=True)
        self.ws_client.on_book_update(self._handle_book_update)

        # Running state
        self._running = False
        self._start_time: Optional[datetime] = None

    def _handle_book_update(self, update: BookUpdate) -> None:
        """Process orderbook update from WebSocket."""
        now = datetime.now(timezone.utc)

        # Track which side updated
        if update.token_id == self.up_token_id:
            if update.best_ask is not None:
                self.up_ask = update.best_ask
                self.up_last_update = now
        elif update.token_id == self.down_token_id:
            if update.best_ask is not None:
                self.down_ask = update.best_ask
                self.down_last_update = now
        else:
            return  # Unknown token

        self.total_updates += 1
        self.update_times.append(now)

        # Check spread if we have both sides
        if self.up_ask is not None and self.down_ask is not None:
            self._check_spread(now)

    def _check_spread(self, now: datetime) -> None:
        """Check current spread and record if below threshold."""
        pair_cost = self.up_ask + self.down_ask
        edge_pct = (1.0 - pair_cost) * 100 if pair_cost < 1.0 else 0

        # Calculate latency (time since oldest of the two updates)
        latency_ms = 0
        if self.up_last_update and self.down_last_update:
            older = min(self.up_last_update, self.down_last_update)
            latency_ms = (now - older).total_seconds() * 1000

        snapshot = SpreadSnapshot(
            timestamp=now,
            up_ask=self.up_ask,
            down_ask=self.down_ask,
            pair_cost=pair_cost,
            edge_pct=edge_pct,
            latency_ms=latency_ms,
        )

        if pair_cost < self.threshold:
            self.sub_threshold_count += 1
            self.all_snapshots.append(snapshot)

            # Track opportunity window
            if self.current_window is None:
                # Start new window
                self.current_window = OpportunityWindow(
                    start_time=now,
                    min_pair_cost=pair_cost,
                    max_edge_pct=edge_pct,
                    samples=1,
                )
                self.current_window.snapshots.append(snapshot)

                # Print alert
                elapsed = (now - self._start_time).total_seconds() if self._start_time else 0
                print(f"\n{'='*60}")
                print(f"🔥 NEGATIVE SPREAD DETECTED @ {elapsed:.3f}s")
                print(f"   UP Ask: ${self.up_ask:.4f} | DOWN Ask: ${self.down_ask:.4f}")
                print(f"   Pair Cost: ${pair_cost:.4f} | Edge: {edge_pct:.2f}%")
                print(f"{'='*60}")
            else:
                # Continue existing window
                self.current_window.min_pair_cost = min(self.current_window.min_pair_cost, pair_cost)
                self.current_window.max_edge_pct = max(self.current_window.max_edge_pct, edge_pct)
                self.current_window.samples += 1
                self.current_window.snapshots.append(snapshot)

                # Print update
                elapsed = (now - self._start_time).total_seconds() if self._start_time else 0
                window_dur = (now - self.current_window.start_time).total_seconds() * 1000
                print(f"  [{elapsed:.3f}s] ${pair_cost:.4f} ({edge_pct:.2f}%) - window: {window_dur:.0f}ms")
        else:
            # Close window if open
            if self.current_window is not None:
                self.current_window.end_time = now
                self.opportunities.append(self.current_window)

                # Print window close
                print(f"  Window closed: {self.current_window.duration_ms:.0f}ms, "
                      f"min=${self.current_window.min_pair_cost:.4f}, "
                      f"max_edge={self.current_window.max_edge_pct:.2f}%")
                print()

                self.current_window = None

    async def run(self, duration_secs: int = 60) -> None:
        """
        Run the analyzer for specified duration.

        Args:
            duration_secs: How long to monitor (seconds)
        """
        self._start_time = datetime.now(timezone.utc)
        self._running = True

        print("="*70)
        print("WEBSOCKET SPREAD ANALYZER - Sub-Second Negative Spread Detection")
        print("="*70)
        print(f"Market: {self.market_slug}")
        print(f"UP Token: {self.up_token_id[:20]}...")
        print(f"DOWN Token: {self.down_token_id[:20]}...")
        print(f"Threshold: ${self.threshold:.4f}")
        print(f"Duration: {duration_secs}s")
        print()
        print("Connecting to WebSocket...")

        # Connect
        if not await self.ws_client.connect():
            print("Failed to connect to WebSocket!")
            return

        print("Connected! Subscribing to orderbook updates...")

        # Subscribe to both tokens
        await self.ws_client.subscribe([self.up_token_id, self.down_token_id])
        print("Subscribed. Monitoring for negative spreads...")
        print("-"*70)

        # Run for duration
        try:
            await self.ws_client.run_for_duration(duration_secs)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            await self.ws_client.disconnect()
            self._running = False

            # Close any open window
            if self.current_window:
                self.current_window.end_time = datetime.now(timezone.utc)
                self.opportunities.append(self.current_window)

        # Print summary
        self._print_summary()

    def _print_summary(self) -> None:
        """Print analysis summary."""
        print()
        print("="*70)
        print("ANALYSIS SUMMARY")
        print("="*70)

        total_duration = (datetime.now(timezone.utc) - self._start_time).total_seconds()

        print(f"Duration: {total_duration:.1f}s")
        print(f"Total WebSocket updates: {self.total_updates}")
        print(f"Sub-${self.threshold:.2f} observations: {self.sub_threshold_count}")
        print(f"Opportunity windows: {len(self.opportunities)}")

        # Update rate
        if len(self.update_times) >= 2:
            intervals = []
            for i in range(1, len(self.update_times)):
                interval = (self.update_times[i] - self.update_times[i-1]).total_seconds() * 1000
                intervals.append(interval)
            avg_interval = sum(intervals) / len(intervals)
            min_interval = min(intervals)
            max_interval = max(intervals)
            print(f"\nUpdate intervals (ms): avg={avg_interval:.1f}, min={min_interval:.1f}, max={max_interval:.1f}")
            print(f"Updates per second: {len(self.update_times) / total_duration:.1f}")

        # Opportunity windows detail
        if self.opportunities:
            print(f"\n{'─'*70}")
            print("OPPORTUNITY WINDOWS:")
            print(f"{'─'*70}")
            print(f"{'#':>3} | {'Start':>10} | {'Duration':>10} | {'Min Pair$':>10} | {'Max Edge':>10} | {'Samples':>8}")
            print(f"{'─'*70}")

            for i, opp in enumerate(self.opportunities, 1):
                start_offset = (opp.start_time - self._start_time).total_seconds()
                print(f"{i:>3} | {start_offset:>9.3f}s | {opp.duration_ms:>8.1f}ms | ${opp.min_pair_cost:>8.4f} | {opp.max_edge_pct:>8.2f}% | {opp.samples:>8}")

            # Aggregate stats
            total_window_time = sum(o.duration_ms for o in self.opportunities)
            avg_window_time = total_window_time / len(self.opportunities)
            best_edge = max(o.max_edge_pct for o in self.opportunities)
            best_pair = min(o.min_pair_cost for o in self.opportunities)

            print(f"{'─'*70}")
            print(f"Total window time: {total_window_time:.1f}ms ({total_window_time/1000/total_duration*100:.2f}% of session)")
            print(f"Average window duration: {avg_window_time:.1f}ms")
            print(f"Best pair cost: ${best_pair:.4f}")
            print(f"Best edge: {best_edge:.2f}%")

            # Duration distribution
            if len(self.opportunities) > 1:
                print(f"\nWindow duration distribution:")
                buckets = {
                    "<10ms": len([o for o in self.opportunities if o.duration_ms < 10]),
                    "10-50ms": len([o for o in self.opportunities if 10 <= o.duration_ms < 50]),
                    "50-100ms": len([o for o in self.opportunities if 50 <= o.duration_ms < 100]),
                    "100-500ms": len([o for o in self.opportunities if 100 <= o.duration_ms < 500]),
                    "500ms-1s": len([o for o in self.opportunities if 500 <= o.duration_ms < 1000]),
                    ">1s": len([o for o in self.opportunities if o.duration_ms >= 1000]),
                }
                for bucket, count in buckets.items():
                    if count > 0:
                        bar = "█" * count
                        print(f"  {bucket:>10}: {count:>3} {bar}")
        else:
            print(f"\nNo negative spread opportunities detected during {total_duration:.1f}s monitoring.")
            print("The market may be efficiently priced or low activity.")

        print()
        print("="*70)


async def get_market_tokens(market_slug: str) -> tuple[str, str, str]:
    """
    Get UP and DOWN token IDs for a market.

    Returns:
        (up_token_id, down_token_id, market_question)
    """
    config = Config()
    client = PolymarketClient(config)

    # Try to find market by slug/condition_id
    # The slug format is btc-updown-15m-{timestamp}
    # Extract timestamp
    parts = market_slug.split("-")
    if len(parts) >= 4:
        timestamp = parts[-1]
    else:
        timestamp = market_slug

    # Search gamma API for the market
    url = f"https://gamma-api.polymarket.com/markets?slug_contains=btc-updown-15m-{timestamp}"

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                markets = await resp.json()
                if markets:
                    market = markets[0]
                    # Get tokens from the market
                    tokens = market.get("clobTokenIds", [])
                    outcomes = market.get("outcomes", [])

                    up_token = None
                    down_token = None

                    for i, outcome in enumerate(outcomes):
                        if outcome.upper() in ("YES", "UP"):
                            up_token = tokens[i] if i < len(tokens) else None
                        elif outcome.upper() in ("NO", "DOWN"):
                            down_token = tokens[i] if i < len(tokens) else None

                    if up_token and down_token:
                        return up_token, down_token, market.get("question", market_slug)

    # Fallback: try direct condition_id lookup
    url = f"https://clob.polymarket.com/markets/{market_slug}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                market = await resp.json()
                tokens = market.get("tokens", [])
                if len(tokens) >= 2:
                    # Assume first is YES/UP, second is NO/DOWN
                    return tokens[0]["token_id"], tokens[1]["token_id"], market_slug

    raise ValueError(f"Could not find market: {market_slug}")


async def main():
    parser = argparse.ArgumentParser(description="WebSocket spread analyzer for Polymarket")
    parser.add_argument("market", help="Market slug (e.g., btc-updown-15m-1767834000)")
    parser.add_argument("--duration", "-d", type=int, default=60, help="Duration in seconds (default: 60)")
    parser.add_argument("--threshold", "-t", type=float, default=1.0, help="Pair cost threshold (default: 1.0)")
    args = parser.parse_args()

    print(f"Looking up market: {args.market}")

    try:
        up_token, down_token, question = await get_market_tokens(args.market)
        print(f"Found: {question[:60]}...")
        print(f"UP Token: {up_token[:30]}...")
        print(f"DOWN Token: {down_token[:30]}...")
        print()
    except Exception as e:
        print(f"Error finding market: {e}")
        return

    analyzer = WebSocketSpreadAnalyzer(
        up_token_id=up_token,
        down_token_id=down_token,
        market_slug=args.market,
        threshold=args.threshold,
    )

    await analyzer.run(args.duration)


if __name__ == "__main__":
    asyncio.run(main())
