#!/usr/bin/env python3
"""
Live monitor for sub-$1.00 pair cost opportunities in BTC 15-min markets.
Tracks frequency and duration of negative spread windows.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.services.market_finder import MarketFinder


@dataclass
class OpportunityWindow:
    """Track a single opportunity window."""
    start_time: datetime
    end_time: Optional[datetime] = None
    min_pair_cost: float = 1.0
    samples: int = 0

    @property
    def duration_ms(self) -> int:
        end = self.end_time or datetime.now(timezone.utc)
        return int((end - self.start_time).total_seconds() * 1000)

    @property
    def is_open(self) -> bool:
        return self.end_time is None


@dataclass
class MarketStats:
    """Stats for a single market."""
    market_slug: str
    opportunities: List[OpportunityWindow] = field(default_factory=list)
    current_window: Optional[OpportunityWindow] = None
    total_samples: int = 0
    sub_1_samples: int = 0

    def record_sample(self, pair_cost: float, threshold: float = 1.0):
        self.total_samples += 1
        now = datetime.now(timezone.utc)

        if pair_cost < threshold:
            self.sub_1_samples += 1

            if self.current_window is None:
                # Start new opportunity window
                self.current_window = OpportunityWindow(
                    start_time=now,
                    min_pair_cost=pair_cost,
                    samples=1
                )
            else:
                # Continue existing window
                self.current_window.min_pair_cost = min(
                    self.current_window.min_pair_cost, pair_cost
                )
                self.current_window.samples += 1
        else:
            if self.current_window is not None:
                # Close the window
                self.current_window.end_time = now
                self.opportunities.append(self.current_window)
                self.current_window = None

    @property
    def opportunity_rate(self) -> float:
        if self.total_samples == 0:
            return 0.0
        return self.sub_1_samples / self.total_samples * 100

    @property
    def avg_duration_ms(self) -> float:
        closed = [o for o in self.opportunities if not o.is_open]
        if not closed:
            return 0.0
        return sum(o.duration_ms for o in closed) / len(closed)


async def monitor_spreads(duration_secs: int = 120, poll_interval_ms: int = 500):
    """
    Monitor live spreads for sub-$1.00 opportunities.

    Args:
        duration_secs: How long to monitor (default 2 minutes)
        poll_interval_ms: Polling interval in milliseconds
    """
    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    print("=" * 80)
    print("LIVE SPREAD MONITOR - Tracking Sub-$1.00 Pair Cost Opportunities")
    print("=" * 80)
    print(f"Duration: {duration_secs}s | Poll interval: {poll_interval_ms}ms")
    print()

    # Find active markets
    print("Finding active BTC 15-min markets...")
    markets = await finder.find_btc_15min_markets()

    if not markets:
        print("No active markets found!")
        return

    print(f"Found {len(markets)} active market(s)")
    for m in markets:
        print(f"  - {m.question[:60]}...")
    print()

    # Initialize stats
    stats: Dict[str, MarketStats] = {}
    for m in markets:
        stats[m.condition_id] = MarketStats(market_slug=m.condition_id)

    # Track global stats
    all_opportunities: List[dict] = []
    start_time = datetime.now(timezone.utc)
    iteration = 0

    print("Monitoring... (Ctrl+C to stop early)")
    print("-" * 80)
    print(f"{'Time':>8} | {'Market':>20} | {'UP Ask':>8} | {'DN Ask':>8} | {'Pair$':>8} | {'Status':>12}")
    print("-" * 80)

    try:
        while (datetime.now(timezone.utc) - start_time).total_seconds() < duration_secs:
            iteration += 1

            for market in markets:
                try:
                    # Fetch orderbook
                    up_book = await client.get_orderbook(market.up_token_id)
                    down_book = await client.get_orderbook(market.down_token_id)

                    # Get best asks
                    up_asks = up_book.get("asks", [])
                    down_asks = down_book.get("asks", [])

                    if not up_asks or not down_asks:
                        continue

                    up_ask = float(up_asks[0]["price"])
                    down_ask = float(down_asks[0]["price"])
                    pair_cost = up_ask + down_ask

                    # Record sample
                    market_stats = stats[market.condition_id]
                    market_stats.record_sample(pair_cost)

                    # Determine status
                    if pair_cost < 0.97:
                        status = f"🔥 {(1-pair_cost)*100:.1f}% EDGE"
                    elif pair_cost < 0.98:
                        status = f"✨ {(1-pair_cost)*100:.1f}% edge"
                    elif pair_cost < 0.99:
                        status = f"📊 {(1-pair_cost)*100:.1f}% edge"
                    elif pair_cost < 1.0:
                        status = f"📈 {(1-pair_cost)*100:.2f}%"
                    else:
                        status = "—"

                    # Print if sub-$1.00 or every 10th iteration
                    if pair_cost < 1.0 or iteration % 10 == 0:
                        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
                        market_short = market.question[20:40] if len(market.question) > 40 else market.question[:20]

                        print(f"{elapsed:7.1f}s | {market_short:>20} | ${up_ask:>6.3f} | ${down_ask:>6.3f} | ${pair_cost:>6.4f} | {status:>12}")

                        if pair_cost < 1.0:
                            all_opportunities.append({
                                "time": datetime.now(timezone.utc),
                                "market": market.condition_id,
                                "up_ask": up_ask,
                                "down_ask": down_ask,
                                "pair_cost": pair_cost,
                                "edge_pct": (1 - pair_cost) * 100
                            })

                except Exception as e:
                    pass  # Skip errors silently

            await asyncio.sleep(poll_interval_ms / 1000)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")

    # Print summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    total_duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"Monitoring duration: {total_duration:.1f}s")
    print(f"Total opportunity snapshots: {len(all_opportunities)}")
    print()

    # Per-market stats
    print("PER-MARKET STATISTICS:")
    print("-" * 80)
    print(f"{'Market':>40} | {'Samples':>8} | {'Sub-$1':>8} | {'Rate':>8} | {'Windows':>8} | {'Avg Dur':>10}")
    print("-" * 80)

    for cid, market_stats in stats.items():
        # Close any open windows
        if market_stats.current_window:
            market_stats.current_window.end_time = datetime.now(timezone.utc)
            market_stats.opportunities.append(market_stats.current_window)

        market_name = cid[:40]
        windows = len(market_stats.opportunities)
        avg_dur = market_stats.avg_duration_ms

        print(f"{market_name:>40} | {market_stats.total_samples:>8} | {market_stats.sub_1_samples:>8} | {market_stats.opportunity_rate:>6.1f}% | {windows:>8} | {avg_dur:>8.0f}ms")

    print()

    # Opportunity distribution
    if all_opportunities:
        print("OPPORTUNITY DISTRIBUTION:")
        print("-" * 80)

        edges = [o["edge_pct"] for o in all_opportunities]

        # Buckets
        buckets = {
            "0-1%": len([e for e in edges if 0 < e <= 1]),
            "1-2%": len([e for e in edges if 1 < e <= 2]),
            "2-3%": len([e for e in edges if 2 < e <= 3]),
            "3-4%": len([e for e in edges if 3 < e <= 4]),
            "4-5%": len([e for e in edges if 4 < e <= 5]),
            ">5%": len([e for e in edges if e > 5]),
        }

        for bucket, count in buckets.items():
            bar = "█" * (count // 2) if count > 0 else ""
            print(f"  {bucket:>6}: {count:>4} {bar}")

        print()
        print(f"  Min pair cost: ${min(o['pair_cost'] for o in all_opportunities):.4f}")
        print(f"  Max edge seen: {max(edges):.2f}%")
        print(f"  Avg edge: {sum(edges)/len(edges):.2f}%")
    else:
        print("No sub-$1.00 opportunities detected during monitoring period.")

    print()
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=120, help="Monitor duration in seconds")
    parser.add_argument("--interval", type=int, default=500, help="Poll interval in milliseconds")
    args = parser.parse_args()

    asyncio.run(monitor_spreads(args.duration, args.interval))
