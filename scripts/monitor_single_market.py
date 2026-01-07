#!/usr/bin/env python3
"""
Monitor a single market for sub-$1.00 pair cost opportunities.

Uses WebSocket for sub-second orderbook updates.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import Config
from src.api.polymarket_client import PolymarketClient
from src.api.websocket_client import WebSocketClient, BookUpdate
from src.services.market_finder import MarketFinder


@dataclass
class OpportunityWindow:
    start_time: datetime
    end_time: Optional[datetime] = None
    min_pair_cost: float = 1.0
    samples: int = 0

    @property
    def duration_ms(self) -> int:
        end = self.end_time or datetime.now(timezone.utc)
        return int((end - self.start_time).total_seconds() * 1000)


async def monitor_market(market_slug: str, duration_secs: int = 900, poll_interval_ms: int = 300):
    """Monitor a specific market for spread opportunities."""

    config = Config()
    client = PolymarketClient(config)
    finder = MarketFinder()

    print("=" * 80)
    print(f"SPREAD MONITOR - {market_slug}")
    print("=" * 80)
    print(f"Duration: {duration_secs}s | Poll interval: {poll_interval_ms}ms")
    print()

    # Find the market
    print("Finding market...")
    markets = await finder.find_btc_15min_markets()

    target_market = None
    for m in markets:
        if market_slug in m.slug:
            target_market = m
            break

    if not target_market:
        print(f"Market not found: {market_slug}")
        print(f"Available markets: {[m.slug for m in markets]}")
        return

    print(f"Found: {target_market.question}")
    print(f"  UP token:   {target_market.up_token_id[:20]}...")
    print(f"  DOWN token: {target_market.down_token_id[:20]}...")
    print(f"  End time:   {target_market.end_time}")
    print()

    # Stats tracking
    all_samples: List[dict] = []
    opportunities: List[OpportunityWindow] = []
    current_window: Optional[OpportunityWindow] = None

    start_time = datetime.now(timezone.utc)
    iteration = 0

    print("Monitoring... (waiting for data)")
    print("-" * 80)
    print(f"{'Time':>8} | {'UP Ask':>8} | {'DN Ask':>8} | {'Pair$':>8} | {'Edge':>8} | {'Status':>15}")
    print("-" * 80)

    try:
        while (datetime.now(timezone.utc) - start_time).total_seconds() < duration_secs:
            iteration += 1
            now = datetime.now(timezone.utc)

            try:
                # Fetch orderbooks
                up_book = await client.get_orderbook(target_market.up_token_id)
                down_book = await client.get_orderbook(target_market.down_token_id)

                up_asks = up_book.get("asks", [])
                down_asks = down_book.get("asks", [])

                if not up_asks or not down_asks:
                    if iteration % 20 == 0:
                        elapsed = (now - start_time).total_seconds()
                        print(f"{elapsed:7.1f}s | {'—':>8} | {'—':>8} | {'—':>8} | {'—':>8} | No orderbook")
                    await asyncio.sleep(poll_interval_ms / 1000)
                    continue

                up_ask = float(up_asks[0]["price"])
                down_ask = float(down_asks[0]["price"])
                pair_cost = up_ask + down_ask
                edge_pct = (1 - pair_cost) * 100

                # Record sample
                sample = {
                    "time": now,
                    "up_ask": up_ask,
                    "down_ask": down_ask,
                    "pair_cost": pair_cost,
                    "edge_pct": edge_pct
                }
                all_samples.append(sample)

                # Track opportunity windows
                if pair_cost < 1.0:
                    if current_window is None:
                        current_window = OpportunityWindow(start_time=now, min_pair_cost=pair_cost, samples=1)
                    else:
                        current_window.min_pair_cost = min(current_window.min_pair_cost, pair_cost)
                        current_window.samples += 1
                else:
                    if current_window is not None:
                        current_window.end_time = now
                        opportunities.append(current_window)
                        current_window = None

                # Determine status
                if pair_cost < 0.96:
                    status = f"🔥🔥 {edge_pct:.1f}% HUGE"
                elif pair_cost < 0.97:
                    status = f"🔥 {edge_pct:.1f}% GREAT"
                elif pair_cost < 0.98:
                    status = f"✨ {edge_pct:.1f}% good"
                elif pair_cost < 0.99:
                    status = f"📊 {edge_pct:.1f}%"
                elif pair_cost < 1.0:
                    status = f"📈 {edge_pct:.2f}%"
                else:
                    status = f"— {edge_pct:.2f}%"

                # Print every sample if sub-$1, otherwise every 10th
                elapsed = (now - start_time).total_seconds()
                if pair_cost < 1.0 or iteration % 10 == 0:
                    print(f"{elapsed:7.1f}s | ${up_ask:>6.3f} | ${down_ask:>6.3f} | ${pair_cost:>6.4f} | {edge_pct:>6.2f}% | {status:>15}")

            except Exception as e:
                if iteration % 30 == 0:
                    print(f"Error: {e}")

            await asyncio.sleep(poll_interval_ms / 1000)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")

    # Close any open window
    if current_window:
        current_window.end_time = datetime.now(timezone.utc)
        opportunities.append(current_window)

    # Print summary
    print()
    print("=" * 80)
    print("SUMMARY")
    print("=" * 80)

    total_duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    sub_1_samples = [s for s in all_samples if s["pair_cost"] < 1.0]

    print(f"Total monitoring time: {total_duration:.1f}s")
    print(f"Total samples: {len(all_samples)}")
    print(f"Sub-$1.00 samples: {len(sub_1_samples)} ({len(sub_1_samples)/max(1,len(all_samples))*100:.1f}%)")
    print(f"Opportunity windows: {len(opportunities)}")
    print()

    if opportunities:
        print("OPPORTUNITY WINDOWS:")
        print("-" * 80)
        for i, w in enumerate(opportunities):
            print(f"  Window {i+1}: {w.duration_ms}ms, min=${w.min_pair_cost:.4f} ({(1-w.min_pair_cost)*100:.2f}% edge), {w.samples} samples")

        avg_duration = sum(w.duration_ms for w in opportunities) / len(opportunities)
        print()
        print(f"  Average window duration: {avg_duration:.0f}ms")
        print(f"  Longest window: {max(w.duration_ms for w in opportunities)}ms")
        print(f"  Best edge seen: {max((1-w.min_pair_cost)*100 for w in opportunities):.2f}%")

    if sub_1_samples:
        print()
        print("EDGE DISTRIBUTION:")
        edges = [s["edge_pct"] for s in sub_1_samples]
        buckets = {
            "0-1%": len([e for e in edges if 0 < e <= 1]),
            "1-2%": len([e for e in edges if 1 < e <= 2]),
            "2-3%": len([e for e in edges if 2 < e <= 3]),
            "3-4%": len([e for e in edges if 3 < e <= 4]),
            "4-5%": len([e for e in edges if 4 < e <= 5]),
            ">5%": len([e for e in edges if e > 5]),
        }
        for bucket, count in buckets.items():
            bar = "█" * min(50, count)
            print(f"  {bucket:>6}: {count:>4} {bar}")

    print()
    print("=" * 80)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", type=str, required=True, help="Market slug to monitor")
    parser.add_argument("--duration", type=int, default=900, help="Monitor duration in seconds")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in milliseconds")
    args = parser.parse_args()

    asyncio.run(monitor_market(args.slug, args.duration, args.interval))
