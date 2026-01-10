#!/usr/bin/env python3
"""
Gabagool22 Multi-Asset Analysis

Analyzes gabagool22's trading across BTC, ETH, and SOL 15-minute markets.
"""

import requests
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict
import statistics

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

ET = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')


def generate_market_slugs(asset: str, start_time: datetime, end_time: datetime) -> List[Tuple[str, datetime]]:
    """
    Generate market slugs for a given asset and time range.

    Assets: btc, eth, sol
    Format: {asset}-updown-15m-{unix_timestamp}
    """
    slugs = []
    current = start_time

    # Round down to nearest 15 minutes
    minute = (current.minute // 15) * 15
    current = current.replace(minute=minute, second=0, microsecond=0)

    while current < end_time:
        unix_ts = int(current.timestamp())
        slug = f"{asset}-updown-15m-{unix_ts}"
        slugs.append((slug, current))
        current += timedelta(minutes=15)

    return slugs


def fetch_market_info(slug: str) -> Optional[Dict]:
    """Fetch market metadata from gamma API."""
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if data and len(data) > 0:
            event = data[0]
            markets = event.get("markets", [])
            if markets:
                return markets[0]
        return None
    except Exception as e:
        return None


def fetch_trades_for_market(condition_id: str, wallet: str) -> List[Dict]:
    """Fetch all trades for a market/wallet."""
    all_trades = []
    offset = 0
    page_limit = 1000

    while True:
        params = {
            "limit": page_limit,
            "offset": offset,
            "market": condition_id,
            "user": wallet,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            break

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        all_trades.extend(batch)

        if len(batch) < page_limit:
            break
        offset += page_limit

    return all_trades


def analyze_asset(asset: str, start_time: datetime, end_time: datetime) -> Dict:
    """Analyze all markets for a given asset in the time range."""

    print(f"\n{'='*60}")
    print(f"ANALYZING {asset.upper()} MARKETS")
    print(f"{'='*60}")
    print(f"Time Range: {start_time.strftime('%Y-%m-%d %H:%M ET')} to {end_time.strftime('%Y-%m-%d %H:%M ET')}")

    market_slugs = generate_market_slugs(asset, start_time, end_time)
    print(f"Generated {len(market_slugs)} market slugs")

    total_trades = 0
    markets_with_trades = 0
    markets_checked = 0
    all_up_prices = []
    all_down_prices = []
    all_sizes = []
    trade_intervals = []
    pair_costs = []

    for i, (slug, market_time) in enumerate(market_slugs):
        markets_checked += 1

        # Fetch market info
        market_info = fetch_market_info(slug)
        if not market_info:
            continue

        condition_id = market_info.get("conditionId", "")
        if not condition_id:
            continue

        # Fetch trades
        raw_trades = fetch_trades_for_market(condition_id, WALLET)
        buys = [t for t in raw_trades if t.get("side", "").upper() == "BUY"]

        if buys:
            markets_with_trades += 1
            total_trades += len(buys)

            # Sort by timestamp
            buys.sort(key=lambda t: t.get("timestamp", 0))

            up_cost = 0
            down_cost = 0
            up_shares = 0
            down_shares = 0

            prev_ts = None
            for trade in buys:
                outcome = trade.get("outcome", "").lower()
                price = float(trade.get("price", 0))
                size = float(trade.get("size", 0))
                ts = trade.get("timestamp", 0)

                all_sizes.append(size)

                if outcome == "up":
                    all_up_prices.append(price)
                    up_cost += price * size
                    up_shares += size
                else:
                    all_down_prices.append(price)
                    down_cost += price * size
                    down_shares += size

                if prev_ts:
                    interval = (ts - prev_ts) / 1000  # Convert to seconds
                    if interval > 0:
                        trade_intervals.append(interval)
                prev_ts = ts

            # Calculate pair cost
            if up_shares > 0 and down_shares > 0:
                avg_up = up_cost / up_shares
                avg_down = down_cost / down_shares
                pc = avg_up + avg_down
                pair_costs.append(pc)

            print(f"  [{i+1}/{len(market_slugs)}] {slug[-15:]}: {len(buys)} trades, UP={len([t for t in buys if t.get('outcome','').lower()=='up'])}, DOWN={len([t for t in buys if t.get('outcome','').lower()=='down'])}")

        # Rate limiting
        time.sleep(0.3)

    # Compile results
    result = {
        "asset": asset.upper(),
        "start_time": start_time,
        "end_time": end_time,
        "markets_checked": markets_checked,
        "markets_with_trades": markets_with_trades,
        "total_trades": total_trades,
        "avg_trades_per_market": total_trades / markets_with_trades if markets_with_trades > 0 else 0,
    }

    if all_up_prices:
        result["up_price_stats"] = {
            "min": min(all_up_prices),
            "max": max(all_up_prices),
            "mean": statistics.mean(all_up_prices),
            "median": statistics.median(all_up_prices),
            "count": len(all_up_prices),
        }

    if all_down_prices:
        result["down_price_stats"] = {
            "min": min(all_down_prices),
            "max": max(all_down_prices),
            "mean": statistics.mean(all_down_prices),
            "median": statistics.median(all_down_prices),
            "count": len(all_down_prices),
        }

    if all_sizes:
        result["size_stats"] = {
            "min": min(all_sizes),
            "max": max(all_sizes),
            "mean": statistics.mean(all_sizes),
            "median": statistics.median(all_sizes),
        }

    if pair_costs:
        result["pair_cost_stats"] = {
            "min": min(pair_costs),
            "max": max(pair_costs),
            "mean": statistics.mean(pair_costs),
            "median": statistics.median(pair_costs),
            "profitable_pct": sum(1 for pc in pair_costs if pc < 1.0) / len(pair_costs) * 100,
        }

    if trade_intervals:
        result["interval_stats"] = {
            "min": min(trade_intervals),
            "max": max(trade_intervals),
            "mean": statistics.mean(trade_intervals),
            "median": statistics.median(trade_intervals),
        }

    return result


def print_asset_summary(result: Dict):
    """Print summary for an asset."""
    print(f"\n{'─'*60}")
    print(f"{result['asset']} SUMMARY")
    print(f"{'─'*60}")

    print(f"\nMARKET ACTIVITY:")
    print(f"  Markets checked: {result['markets_checked']}")
    print(f"  Markets with trades: {result['markets_with_trades']} ({result['markets_with_trades']/result['markets_checked']*100:.1f}%)")
    print(f"  Total trades: {result['total_trades']}")
    print(f"  Avg trades/market: {result['avg_trades_per_market']:.1f}")

    if "up_price_stats" in result:
        stats = result["up_price_stats"]
        print(f"\nUP PRICE DISTRIBUTION:")
        print(f"  Range: ${stats['min']:.2f} - ${stats['max']:.2f}")
        print(f"  Mean: ${stats['mean']:.2f}, Median: ${stats['median']:.2f}")
        print(f"  Count: {stats['count']}")

    if "down_price_stats" in result:
        stats = result["down_price_stats"]
        print(f"\nDOWN PRICE DISTRIBUTION:")
        print(f"  Range: ${stats['min']:.2f} - ${stats['max']:.2f}")
        print(f"  Mean: ${stats['mean']:.2f}, Median: ${stats['median']:.2f}")
        print(f"  Count: {stats['count']}")

    if "size_stats" in result:
        stats = result["size_stats"]
        print(f"\nORDER SIZE:")
        print(f"  Range: {stats['min']:.1f} - {stats['max']:.1f} shares")
        print(f"  Mean: {stats['mean']:.1f}, Median: {stats['median']:.1f}")

    if "pair_cost_stats" in result:
        stats = result["pair_cost_stats"]
        print(f"\nPAIR COST:")
        print(f"  Range: ${stats['min']:.3f} - ${stats['max']:.3f}")
        print(f"  Mean: ${stats['mean']:.3f}, Median: ${stats['median']:.3f}")
        print(f"  Profitable (<$1.00): {stats['profitable_pct']:.1f}%")

    if "interval_stats" in result:
        stats = result["interval_stats"]
        print(f"\nTRADE INTERVALS:")
        print(f"  Min: {stats['min']:.3f}s, Max: {stats['max']:.1f}s")
        print(f"  Mean: {stats['mean']:.2f}s, Median: {stats['median']:.2f}s")


def main():
    print("=" * 80)
    print("GABAGOOL22 MULTI-ASSET ANALYSIS")
    print("=" * 80)
    print(f"Wallet: {WALLET}")

    # Time ranges
    ranges = [
        (datetime(2026, 1, 9, 2, 45, 0, tzinfo=ET), datetime(2026, 1, 10, 1, 45, 0, tzinfo=ET), "Jan 9 02:45 - Jan 10 01:45"),
        (datetime(2026, 1, 7, 2, 30, 0, tzinfo=ET), datetime(2026, 1, 8, 3, 15, 0, tzinfo=ET), "Jan 7 02:30 - Jan 8 03:15"),
    ]

    # Assets to analyze
    assets = ["eth", "sol"]

    all_results = []

    for start_time, end_time, range_name in ranges:
        print(f"\n{'#'*80}")
        print(f"# TIME RANGE: {range_name}")
        print(f"{'#'*80}")

        for asset in assets:
            result = analyze_asset(asset, start_time, end_time)
            result["range_name"] = range_name
            all_results.append(result)
            print_asset_summary(result)

    # Final comparison
    print("\n" + "=" * 80)
    print("CROSS-ASSET COMPARISON")
    print("=" * 80)

    print("\n{:<10} {:<25} {:>10} {:>10} {:>12} {:>12}".format(
        "Asset", "Time Range", "Markets", "Trades", "Avg/Market", "Pair Cost"))
    print("-" * 80)

    for r in all_results:
        pc = r.get("pair_cost_stats", {}).get("mean", 0)
        pc_str = f"${pc:.3f}" if pc > 0 else "N/A"
        print("{:<10} {:<25} {:>10} {:>10} {:>12.1f} {:>12}".format(
            r["asset"],
            r["range_name"][:25],
            r["markets_with_trades"],
            r["total_trades"],
            r["avg_trades_per_market"],
            pc_str))


if __name__ == "__main__":
    main()
