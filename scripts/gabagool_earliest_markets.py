#!/usr/bin/env python3
"""
Find Gabagool22's EARLIEST BTC 15-minute markets.

Searches back to October 2025 to find when Gabagool started trading
and how they scaled their capital.

Usage:
    python scripts/gabagool_earliest_markets.py
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
import csv
import os

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

# Time zones
ET = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')

# Search parameters
# Start from October 1, 2025 00:00 ET and work forward
SEARCH_START = datetime(2025, 10, 1, 0, 0, 0, tzinfo=ET)
SEARCH_END = datetime(2025, 11, 15, 0, 0, 0, tzinfo=ET)  # Search through mid-November


@dataclass
class Trade:
    """Single trade record."""
    timestamp: float
    timestamp_dt: datetime
    side: str
    outcome: str
    price: float
    size: float
    cost: float
    market_slug: str
    condition_id: str


@dataclass
class MarketAnalysis:
    """Analysis for a single market."""
    slug: str
    start_time: datetime
    condition_id: str

    total_trades: int = 0
    up_buys: int = 0
    down_buys: int = 0
    up_sells: int = 0
    down_sells: int = 0

    total_up_shares: float = 0.0
    total_down_shares: float = 0.0
    total_up_cost: float = 0.0
    total_down_cost: float = 0.0

    up_prices: List[float] = field(default_factory=list)
    down_prices: List[float] = field(default_factory=list)

    first_trade_time: Optional[datetime] = None
    last_trade_time: Optional[datetime] = None

    trades: List[Trade] = field(default_factory=list)


def generate_market_slugs(start: datetime, end: datetime) -> List[Tuple[str, datetime]]:
    """
    Generate BTC 15-min market slugs for a time range.
    Markets start every 15 minutes.
    """
    slugs = []
    current = start

    # Align to 15-minute boundary
    minute = current.minute
    aligned_minute = (minute // 15) * 15
    current = current.replace(minute=aligned_minute, second=0, microsecond=0)

    while current < end:
        unix_ts = int(current.timestamp())
        slug = f"btc-updown-15m-{unix_ts}"
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


def analyze_market(slug: str, market_time: datetime) -> Optional[MarketAnalysis]:
    """Analyze a single market for Gabagool activity."""

    # Fetch market info
    market_info = fetch_market_info(slug)
    if not market_info:
        return None

    condition_id = market_info.get("conditionId", "")
    if not condition_id:
        return None

    # Create analysis object
    analysis = MarketAnalysis(
        slug=slug,
        start_time=market_time,
        condition_id=condition_id,
    )

    # Fetch trades
    raw_trades = fetch_trades_for_market(condition_id, WALLET)

    if not raw_trades:
        return None  # No Gabagool activity

    # Process trades
    buys = [t for t in raw_trades if t.get("side", "").upper() == "BUY"]
    sells = [t for t in raw_trades if t.get("side", "").upper() == "SELL"]

    if not buys and not sells:
        return None

    # Sort by timestamp
    all_trades = buys + sells
    all_trades.sort(key=lambda t: t.get("timestamp", 0))

    for trade in all_trades:
        outcome = trade.get("outcome", "").lower()
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        cost = price * size
        timestamp_ms = trade.get("timestamp", 0)
        timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC) if timestamp_ms else None
        side = trade.get("side", "").upper()

        is_up = outcome == "up"
        is_buy = side == "BUY"

        # Create trade object
        t = Trade(
            timestamp=timestamp_ms / 1000 if timestamp_ms else 0,
            timestamp_dt=timestamp_dt,
            side=side,
            outcome="Up" if is_up else "Down",
            price=price,
            size=size,
            cost=cost,
            market_slug=slug,
            condition_id=condition_id,
        )
        analysis.trades.append(t)

        # Update counts
        if is_buy:
            if is_up:
                analysis.up_buys += 1
                analysis.total_up_shares += size
                analysis.total_up_cost += cost
                analysis.up_prices.append(price)
            else:
                analysis.down_buys += 1
                analysis.total_down_shares += size
                analysis.total_down_cost += cost
                analysis.down_prices.append(price)
        else:  # SELL
            if is_up:
                analysis.up_sells += 1
            else:
                analysis.down_sells += 1

        # Track timing
        if analysis.first_trade_time is None:
            analysis.first_trade_time = timestamp_dt
        analysis.last_trade_time = timestamp_dt

    analysis.total_trades = len(analysis.trades)

    return analysis


def search_for_earliest_markets():
    """Search for Gabagool's earliest BTC 15-min market activity."""

    print("=" * 80)
    print("SEARCHING FOR GABAGOOL22'S EARLIEST BTC 15-MIN MARKETS")
    print("=" * 80)
    print(f"Wallet: {WALLET}")
    print(f"Search range: {SEARCH_START.strftime('%Y-%m-%d %H:%M ET')} to {SEARCH_END.strftime('%Y-%m-%d %H:%M ET')}")

    # Generate all slugs
    market_slugs = generate_market_slugs(SEARCH_START, SEARCH_END)
    print(f"\nGenerated {len(market_slugs)} market slugs to check")

    # Find markets with activity
    active_markets = []
    markets_checked = 0
    last_print = 0

    for slug, market_time in market_slugs:
        markets_checked += 1

        # Progress update every 100 markets
        if markets_checked - last_print >= 100:
            print(f"  Checked {markets_checked}/{len(market_slugs)} markets, found {len(active_markets)} with activity...")
            last_print = markets_checked

        analysis = analyze_market(slug, market_time)

        if analysis:
            active_markets.append(analysis)
            print(f"\n  FOUND: {slug}")
            print(f"    Time: {market_time.strftime('%Y-%m-%d %H:%M ET')}")
            print(f"    Trades: {analysis.total_trades} (UP: {analysis.up_buys}, DOWN: {analysis.down_buys})")
            print(f"    Shares: UP={analysis.total_up_shares:.0f}, DOWN={analysis.total_down_shares:.0f}")
            print(f"    Cost: UP=${analysis.total_up_cost:.2f}, DOWN=${analysis.total_down_cost:.2f}")

        # Rate limiting
        time.sleep(0.3)

        # Stop after finding first 20 markets (for initial scan)
        if len(active_markets) >= 30:
            print(f"\nFound 30 markets, stopping search...")
            break

    return active_markets


def analyze_capital_scaling(markets: List[MarketAnalysis]):
    """Analyze how Gabagool's capital scaled over time."""

    if not markets:
        print("\nNo markets found with Gabagool activity.")
        return

    # Sort by first trade time
    markets.sort(key=lambda m: m.first_trade_time or datetime.min.replace(tzinfo=UTC))

    print("\n" + "=" * 80)
    print("CAPITAL SCALING ANALYSIS")
    print("=" * 80)

    print(f"\nEarliest market: {markets[0].slug}")
    print(f"  Time: {markets[0].start_time.strftime('%Y-%m-%d %H:%M ET')}")
    print(f"  First trade: {markets[0].first_trade_time}")

    print(f"\nLatest market: {markets[-1].slug}")
    print(f"  Time: {markets[-1].start_time.strftime('%Y-%m-%d %H:%M ET')}")

    # Analyze capital per market
    print(f"\n{'─'*80}")
    print("MARKET-BY-MARKET CAPITAL DEPLOYMENT")
    print("─"*80)
    print(f"\n{'Market Time':<22} {'Trades':>8} {'UP Shares':>12} {'DOWN Shares':>12} {'Total Cost':>12} {'Avg Size':>10}")
    print("-" * 80)

    total_costs = []
    avg_sizes = []

    for m in markets:
        total_cost = m.total_up_cost + m.total_down_cost
        total_shares = m.total_up_shares + m.total_down_shares
        avg_size = total_shares / m.total_trades if m.total_trades > 0 else 0

        total_costs.append(total_cost)
        avg_sizes.append(avg_size)

        time_str = m.start_time.strftime('%Y-%m-%d %H:%M')
        print(f"{time_str:<22} {m.total_trades:>8} {m.total_up_shares:>12.0f} {m.total_down_shares:>12.0f} ${total_cost:>10.2f} {avg_size:>10.1f}")

    # Summary statistics
    print(f"\n{'─'*80}")
    print("SUMMARY STATISTICS")
    print("─"*80)

    print(f"\nTotal Cost per Market:")
    print(f"  Min: ${min(total_costs):.2f}")
    print(f"  Max: ${max(total_costs):.2f}")
    print(f"  Mean: ${statistics.mean(total_costs):.2f}")
    print(f"  Median: ${statistics.median(total_costs):.2f}")

    print(f"\nAverage Order Size:")
    print(f"  Min: {min(avg_sizes):.1f} shares")
    print(f"  Max: {max(avg_sizes):.1f} shares")
    print(f"  Mean: {statistics.mean(avg_sizes):.1f} shares")
    print(f"  Median: {statistics.median(avg_sizes):.1f} shares")

    # Price analysis
    all_up_prices = []
    all_down_prices = []
    for m in markets:
        all_up_prices.extend(m.up_prices)
        all_down_prices.extend(m.down_prices)

    if all_up_prices and all_down_prices:
        print(f"\nPrice Distribution:")
        print(f"  UP prices: ${min(all_up_prices):.2f} - ${max(all_up_prices):.2f} (mean ${statistics.mean(all_up_prices):.2f})")
        print(f"  DOWN prices: ${min(all_down_prices):.2f} - ${max(all_down_prices):.2f} (mean ${statistics.mean(all_down_prices):.2f})")

        # Pair cost estimate
        avg_up = statistics.mean(all_up_prices)
        avg_down = statistics.mean(all_down_prices)
        print(f"\n  Estimated Pair Cost: ${avg_up + avg_down:.3f}")

    # Check if there's capital growth
    if len(markets) >= 5:
        first_5_costs = total_costs[:5]
        last_5_costs = total_costs[-5:]

        print(f"\n{'─'*80}")
        print("CAPITAL GROWTH ANALYSIS")
        print("─"*80)

        print(f"\nFirst 5 markets avg cost: ${statistics.mean(first_5_costs):.2f}")
        print(f"Last 5 markets avg cost: ${statistics.mean(last_5_costs):.2f}")

        growth = (statistics.mean(last_5_costs) / statistics.mean(first_5_costs) - 1) * 100 if statistics.mean(first_5_costs) > 0 else 0
        print(f"Growth: {growth:.1f}%")

    return markets


def export_data(markets: List[MarketAnalysis]):
    """Export data to CSV."""
    if not markets:
        return

    os.makedirs("research", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Export markets summary
    markets_file = f"research/gabagool_earliest_markets_{timestamp}.csv"
    with open(markets_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'slug', 'start_time', 'total_trades', 'up_buys', 'down_buys',
            'up_sells', 'down_sells', 'total_up_shares', 'total_down_shares',
            'total_up_cost', 'total_down_cost', 'first_trade_time'
        ])

        for m in markets:
            writer.writerow([
                m.slug,
                m.start_time.isoformat(),
                m.total_trades,
                m.up_buys,
                m.down_buys,
                m.up_sells,
                m.down_sells,
                m.total_up_shares,
                m.total_down_shares,
                m.total_up_cost,
                m.total_down_cost,
                m.first_trade_time.isoformat() if m.first_trade_time else ""
            ])

    print(f"\nExported to {markets_file}")

    # Export all trades
    trades_file = f"research/gabagool_earliest_trades_{timestamp}.csv"
    with open(trades_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'market_slug', 'timestamp', 'datetime', 'side', 'outcome',
            'price', 'size', 'cost'
        ])

        for m in markets:
            for t in m.trades:
                writer.writerow([
                    m.slug,
                    t.timestamp,
                    t.timestamp_dt.isoformat() if t.timestamp_dt else "",
                    t.side,
                    t.outcome,
                    t.price,
                    t.size,
                    t.cost
                ])

    print(f"Exported to {trades_file}")


def main():
    # Search for earliest markets
    markets = search_for_earliest_markets()

    # Analyze capital scaling
    analyze_capital_scaling(markets)

    # Export data
    export_data(markets)

    return markets


if __name__ == "__main__":
    main()
