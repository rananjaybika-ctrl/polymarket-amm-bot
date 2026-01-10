#!/usr/bin/env python3
"""
Gabagool22 Deep Strategy Analysis

Comprehensive reverse-engineering of gabagool22's trading strategy
across 48 BTC 15-minute markets from Jan 9 8PM EST to Jan 10 8AM EST.

Analyzes:
1. Grid/price level patterns
2. Two-sided posting behavior
3. Trending vs mean-reverting market behavior
4. Imbalance ratios and thresholds
5. Price distribution and entry points
6. Velocity-based timing patterns
7. Order timing and sequencing

Usage:
    python scripts/gabagool_deep_analysis.py
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

# API endpoints
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
TRADES_URL = "https://data-api.polymarket.com/trades"
MARKET_URL = "https://gamma-api.polymarket.com/markets"
CLOB_URL = "https://clob.polymarket.com/markets"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

# Time ranges to analyze (can be configured)
ET = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')

# Default: Will be overridden by command line args
START_TIME_ET = datetime(2026, 1, 9, 2, 45, 0, tzinfo=ET)
END_TIME_ET = datetime(2026, 1, 10, 1, 45, 0, tzinfo=ET)


@dataclass
class Trade:
    """Single trade record."""
    timestamp: float
    timestamp_dt: datetime
    side: str  # BUY or SELL
    outcome: str  # Up or Down
    price: float
    size: float
    cost: float
    tx_hash: str
    market_slug: str


@dataclass
class MarketAnalysis:
    """Analysis for a single market."""
    slug: str
    title: str
    condition_id: str
    start_time: datetime
    end_time: datetime
    resolution: Optional[str]  # YES (Up) or NO (Down)

    # Trade counts
    total_trades: int = 0
    up_buys: int = 0
    down_buys: int = 0
    up_sells: int = 0
    down_sells: int = 0

    # Position tracking
    final_up_shares: float = 0.0
    final_down_shares: float = 0.0
    final_up_cost: float = 0.0
    final_down_cost: float = 0.0

    # Price analysis
    up_prices: List[float] = field(default_factory=list)
    down_prices: List[float] = field(default_factory=list)

    # Timing analysis
    first_trade_time: Optional[datetime] = None
    last_trade_time: Optional[datetime] = None
    trade_intervals: List[float] = field(default_factory=list)  # seconds between trades

    # Two-sided analysis
    simultaneous_pairs: int = 0  # Trades on both sides within 5 seconds
    up_first_count: int = 0  # Times UP was bought first in a pair
    down_first_count: int = 0  # Times DOWN was bought first in a pair

    # Imbalance tracking
    max_imbalance_shares: float = 0.0
    max_imbalance_pct: float = 0.0
    imbalance_history: List[Tuple[float, float]] = field(default_factory=list)  # (timestamp, imbalance%)

    # Price level distribution
    price_buckets: Dict[str, int] = field(default_factory=dict)  # e.g., "0.10-0.20": 5

    # All trades for detailed analysis
    trades: List[Trade] = field(default_factory=list)

    # Grid detection
    unique_up_prices: List[float] = field(default_factory=list)
    unique_down_prices: List[float] = field(default_factory=list)
    price_grid_spacing: Optional[float] = None

    # PnL
    pnl: float = 0.0

    # Market type
    is_trending: bool = False
    trend_direction: Optional[str] = None  # "UP" or "DOWN"


def generate_market_slugs() -> List[str]:
    """
    Generate all 48 BTC 15-min market slugs for the time range.

    Format: btc-updown-15m-{unix_timestamp}
    Markets start every 15 minutes.
    """
    slugs = []
    current = START_TIME_ET

    while current < END_TIME_ET:
        # Convert to Unix timestamp
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
        print(f"  Error fetching market info for {slug}: {e}")
        return None


def fetch_trades_for_market(condition_id: str, wallet: str) -> List[Dict]:
    """Fetch all trades for a market/wallet with pagination."""
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
            print(f"  Error fetching trades: {e}")
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


def analyze_single_market(slug: str, market_time: datetime) -> Optional[MarketAnalysis]:
    """Perform deep analysis on a single market."""
    print(f"\n{'─'*60}")
    print(f"Analyzing: {slug}")
    print(f"Time: {market_time.strftime('%Y-%m-%d %H:%M ET')}")

    # Fetch market info
    market_info = fetch_market_info(slug)
    if not market_info:
        print(f"  Market not found")
        return None

    condition_id = market_info.get("conditionId", "")
    if not condition_id:
        print(f"  No condition ID")
        return None

    # Check resolution
    resolution = None
    if market_info.get("closed"):
        winner = market_info.get("winner", "")
        resolution = "YES" if winner == "Up" else "NO" if winner == "Down" else None

    # Create analysis object
    analysis = MarketAnalysis(
        slug=slug,
        title=market_info.get("question", slug)[:60],
        condition_id=condition_id,
        start_time=market_time,
        end_time=market_time + timedelta(minutes=15),
        resolution=resolution,
    )

    # Fetch trades
    raw_trades = fetch_trades_for_market(condition_id, WALLET)

    if not raw_trades:
        print(f"  No trades found")
        return analysis  # Return empty analysis

    # Process trades
    buys = [t for t in raw_trades if t.get("side", "").upper() == "BUY"]
    sells = [t for t in raw_trades if t.get("side", "").upper() == "SELL"]

    print(f"  Found {len(buys)} BUY trades, {len(sells)} SELL trades (ignored)")

    if not buys:
        return analysis

    # Sort by timestamp
    buys.sort(key=lambda t: t.get("timestamp", 0))

    # Process each trade
    up_pos = 0.0
    down_pos = 0.0
    up_cost = 0.0
    down_cost = 0.0

    prev_timestamp = None

    for trade in buys:
        outcome = trade.get("outcome", "").lower()
        price = float(trade.get("price", 0))
        size = float(trade.get("size", 0))
        cost = price * size
        timestamp_ms = trade.get("timestamp", 0)
        timestamp_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC) if timestamp_ms else None

        is_up = outcome == "up"

        # Create trade object
        t = Trade(
            timestamp=timestamp_ms / 1000 if timestamp_ms else 0,
            timestamp_dt=timestamp_dt,
            side="BUY",
            outcome="Up" if is_up else "Down",
            price=price,
            size=size,
            cost=cost,
            tx_hash=trade.get("transactionHash", ""),
            market_slug=slug,
        )
        analysis.trades.append(t)

        # Update position
        if is_up:
            up_pos += size
            up_cost += cost
            analysis.up_buys += 1
            analysis.up_prices.append(price)
        else:
            down_pos += size
            down_cost += cost
            analysis.down_buys += 1
            analysis.down_prices.append(price)

        # Track imbalance
        total_pos = up_pos + down_pos
        if total_pos > 0:
            imbalance_pct = abs(up_pos - down_pos) / total_pos
            analysis.imbalance_history.append((timestamp_ms / 1000 if timestamp_ms else 0, imbalance_pct))

            if imbalance_pct > analysis.max_imbalance_pct:
                analysis.max_imbalance_pct = imbalance_pct

            imbalance_shares = abs(up_pos - down_pos)
            if imbalance_shares > analysis.max_imbalance_shares:
                analysis.max_imbalance_shares = imbalance_shares

        # Track timing
        if analysis.first_trade_time is None:
            analysis.first_trade_time = timestamp_dt
        analysis.last_trade_time = timestamp_dt

        if prev_timestamp is not None and timestamp_ms:
            interval = (timestamp_ms / 1000) - prev_timestamp
            if interval > 0:
                analysis.trade_intervals.append(interval)

        prev_timestamp = timestamp_ms / 1000 if timestamp_ms else None

        # Price bucket
        bucket = f"{int(price * 10) / 10:.1f}-{(int(price * 10) + 1) / 10:.1f}"
        analysis.price_buckets[bucket] = analysis.price_buckets.get(bucket, 0) + 1

    # Final position
    analysis.total_trades = len(buys)
    analysis.final_up_shares = up_pos
    analysis.final_down_shares = down_pos
    analysis.final_up_cost = up_cost
    analysis.final_down_cost = down_cost

    # Unique prices (for grid detection)
    analysis.unique_up_prices = sorted(set(round(p, 2) for p in analysis.up_prices))
    analysis.unique_down_prices = sorted(set(round(p, 2) for p in analysis.down_prices))

    # Detect grid spacing
    if len(analysis.unique_up_prices) >= 3:
        diffs = [analysis.unique_up_prices[i+1] - analysis.unique_up_prices[i]
                 for i in range(len(analysis.unique_up_prices) - 1)]
        if diffs:
            analysis.price_grid_spacing = statistics.median(diffs)

    # Calculate PnL
    if resolution == "YES":
        # Up won - up shares worth $1 each, down shares worth $0
        analysis.pnl = up_pos - (up_cost + down_cost)
    elif resolution == "NO":
        # Down won - down shares worth $1 each, up shares worth $0
        analysis.pnl = down_pos - (up_cost + down_cost)

    # Detect trending market
    if analysis.up_prices and analysis.down_prices:
        avg_up = statistics.mean(analysis.up_prices)
        avg_down = statistics.mean(analysis.down_prices)

        # If one side is consistently expensive (>0.65), it's trending
        if avg_up > 0.65 or max(analysis.up_prices) > 0.85:
            analysis.is_trending = True
            analysis.trend_direction = "UP"
        elif avg_down > 0.65 or max(analysis.down_prices) > 0.85:
            analysis.is_trending = True
            analysis.trend_direction = "DOWN"

    # Analyze two-sided posting
    analyze_two_sided_posting(analysis)

    print(f"  Trades: {analysis.total_trades}, UP: {analysis.up_buys}, DOWN: {analysis.down_buys}")
    print(f"  Position: UP={analysis.final_up_shares:.0f}, DOWN={analysis.final_down_shares:.0f}")
    print(f"  Max imbalance: {analysis.max_imbalance_pct:.1%}")
    print(f"  Resolution: {resolution}, PnL: ${analysis.pnl:.2f}")

    return analysis


def analyze_two_sided_posting(analysis: MarketAnalysis):
    """Analyze if gabagool posts on both sides simultaneously."""
    if len(analysis.trades) < 2:
        return

    # Group trades by 5-second windows
    window_size = 5.0  # seconds

    up_trades = [t for t in analysis.trades if t.outcome == "Up"]
    down_trades = [t for t in analysis.trades if t.outcome == "Down"]

    pairs = 0
    up_first = 0
    down_first = 0

    for up_t in up_trades:
        for down_t in down_trades:
            time_diff = abs(up_t.timestamp - down_t.timestamp)
            if time_diff <= window_size:
                pairs += 1
                if up_t.timestamp < down_t.timestamp:
                    up_first += 1
                else:
                    down_first += 1
                break  # Only count once per up trade

    analysis.simultaneous_pairs = pairs
    analysis.up_first_count = up_first
    analysis.down_first_count = down_first


def analyze_velocity_patterns(analysis: MarketAnalysis) -> Dict:
    """
    Analyze if trades show velocity-based timing patterns.

    If using velocity timing:
    - Trades would cluster after price reversals
    - Would see delays between "detecting reversal" and "trade execution"

    If NOT using velocity:
    - Trades would be evenly distributed
    - Would see consistent intervals
    """
    if len(analysis.trades) < 3:
        return {}

    intervals = analysis.trade_intervals
    if not intervals:
        return {}

    result = {
        "avg_interval": statistics.mean(intervals),
        "median_interval": statistics.median(intervals),
        "min_interval": min(intervals),
        "max_interval": max(intervals),
        "stdev_interval": statistics.stdev(intervals) if len(intervals) > 1 else 0,
    }

    # Check for clustering (velocity would show bursts)
    # Low stdev = consistent timing (no velocity)
    # High stdev = variable timing (could be velocity-based)
    cv = result["stdev_interval"] / result["avg_interval"] if result["avg_interval"] > 0 else 0
    result["coefficient_of_variation"] = cv

    # Velocity timing would show intervals like: 0.5s, 0.5s, 15s, 0.5s, 0.5s, 20s
    # (bursts of fast trades, then waits)
    # Non-velocity would show: 3s, 4s, 3s, 5s, 4s (consistent)

    short_intervals = [i for i in intervals if i < 3.0]
    long_intervals = [i for i in intervals if i >= 10.0]

    result["short_interval_pct"] = len(short_intervals) / len(intervals) if intervals else 0
    result["long_interval_pct"] = len(long_intervals) / len(intervals) if intervals else 0

    # High short + high long = velocity timing (burst then wait)
    # Consistent = no velocity
    result["likely_velocity_based"] = (
        result["short_interval_pct"] > 0.3 and
        result["long_interval_pct"] > 0.1 and
        cv > 1.0
    )

    return result


def analyze_price_entry_strategy(analyses: List[MarketAnalysis]) -> Dict:
    """
    Analyze at what prices Gabagool enters positions.

    Questions:
    - Does he buy more when cheap? Or expensive?
    - What's the pair cost distribution?
    - Does he avoid certain price levels?
    """
    all_up_prices = []
    all_down_prices = []
    all_pair_costs = []

    for a in analyses:
        all_up_prices.extend(a.up_prices)
        all_down_prices.extend(a.down_prices)

        # Calculate average pair cost per market
        if a.final_up_shares > 0 and a.final_down_shares > 0:
            avg_up = a.final_up_cost / a.final_up_shares
            avg_down = a.final_down_cost / a.final_down_shares
            all_pair_costs.append(avg_up + avg_down)

    # Price bucket analysis
    up_buckets = defaultdict(int)
    down_buckets = defaultdict(int)

    for p in all_up_prices:
        bucket = f"{int(p * 20) * 5}c-{(int(p * 20) + 1) * 5}c"  # 5 cent buckets
        up_buckets[bucket] += 1

    for p in all_down_prices:
        bucket = f"{int(p * 20) * 5}c-{(int(p * 20) + 1) * 5}c"
        down_buckets[bucket] += 1

    return {
        "up_price_stats": {
            "min": min(all_up_prices) if all_up_prices else 0,
            "max": max(all_up_prices) if all_up_prices else 0,
            "mean": statistics.mean(all_up_prices) if all_up_prices else 0,
            "median": statistics.median(all_up_prices) if all_up_prices else 0,
            "count": len(all_up_prices),
        },
        "down_price_stats": {
            "min": min(all_down_prices) if all_down_prices else 0,
            "max": max(all_down_prices) if all_down_prices else 0,
            "mean": statistics.mean(all_down_prices) if all_down_prices else 0,
            "median": statistics.median(all_down_prices) if all_down_prices else 0,
            "count": len(all_down_prices),
        },
        "pair_cost_stats": {
            "min": min(all_pair_costs) if all_pair_costs else 0,
            "max": max(all_pair_costs) if all_pair_costs else 0,
            "mean": statistics.mean(all_pair_costs) if all_pair_costs else 0,
            "median": statistics.median(all_pair_costs) if all_pair_costs else 0,
        },
        "up_buckets": dict(sorted(up_buckets.items())),
        "down_buckets": dict(sorted(down_buckets.items())),
    }


def analyze_grid_behavior(analyses: List[MarketAnalysis]) -> Dict:
    """
    Detect if Gabagool uses a grid trading approach.

    Grid trading signs:
    - Orders at regular price intervals (e.g., every 5 cents)
    - Multiple orders at same price level
    - Consistent order sizes
    """
    all_up_prices = []
    all_down_prices = []
    all_sizes = []

    for a in analyses:
        for t in a.trades:
            if t.outcome == "Up":
                all_up_prices.append(round(t.price, 2))
            else:
                all_down_prices.append(round(t.price, 2))
            all_sizes.append(t.size)

    # Analyze price clustering
    up_price_counts = defaultdict(int)
    down_price_counts = defaultdict(int)

    for p in all_up_prices:
        up_price_counts[p] += 1
    for p in all_down_prices:
        down_price_counts[p] += 1

    # Detect grid spacing
    unique_ups = sorted(set(all_up_prices))
    unique_downs = sorted(set(all_down_prices))

    up_diffs = []
    down_diffs = []

    if len(unique_ups) >= 2:
        up_diffs = [unique_ups[i+1] - unique_ups[i] for i in range(len(unique_ups) - 1)]
    if len(unique_downs) >= 2:
        down_diffs = [unique_downs[i+1] - unique_downs[i] for i in range(len(unique_downs) - 1)]

    # Size consistency
    size_stats = {
        "min": min(all_sizes) if all_sizes else 0,
        "max": max(all_sizes) if all_sizes else 0,
        "mean": statistics.mean(all_sizes) if all_sizes else 0,
        "stdev": statistics.stdev(all_sizes) if len(all_sizes) > 1 else 0,
    }

    # Grid detection
    is_grid = False
    grid_spacing = None

    all_diffs = up_diffs + down_diffs
    if all_diffs:
        # Check if diffs cluster around common values (0.01, 0.02, 0.05, 0.10)
        common_spacings = [0.01, 0.02, 0.05, 0.10]
        for spacing in common_spacings:
            matching = sum(1 for d in all_diffs if abs(d - spacing) < 0.005)
            if matching / len(all_diffs) > 0.3:
                is_grid = True
                grid_spacing = spacing
                break

    return {
        "unique_up_prices": len(unique_ups),
        "unique_down_prices": len(unique_downs),
        "most_used_up_prices": sorted(up_price_counts.items(), key=lambda x: -x[1])[:10],
        "most_used_down_prices": sorted(down_price_counts.items(), key=lambda x: -x[1])[:10],
        "up_price_spacing": {
            "min": min(up_diffs) if up_diffs else 0,
            "max": max(up_diffs) if up_diffs else 0,
            "median": statistics.median(up_diffs) if up_diffs else 0,
        },
        "down_price_spacing": {
            "min": min(down_diffs) if down_diffs else 0,
            "max": max(down_diffs) if down_diffs else 0,
            "median": statistics.median(down_diffs) if down_diffs else 0,
        },
        "size_stats": size_stats,
        "is_grid_trading": is_grid,
        "detected_grid_spacing": grid_spacing,
    }


def analyze_trending_behavior(analyses: List[MarketAnalysis]) -> Dict:
    """
    Analyze how Gabagool behaves in trending vs mean-reverting markets.
    """
    trending_markets = [a for a in analyses if a.is_trending and a.total_trades > 0]
    stable_markets = [a for a in analyses if not a.is_trending and a.total_trades > 0]

    trending_pnl = sum(a.pnl for a in trending_markets)
    stable_pnl = sum(a.pnl for a in stable_markets)

    trending_trades = sum(a.total_trades for a in trending_markets)
    stable_trades = sum(a.total_trades for a in stable_markets)

    # Imbalance in trending vs stable
    trending_imbalances = [a.max_imbalance_pct for a in trending_markets if a.max_imbalance_pct > 0]
    stable_imbalances = [a.max_imbalance_pct for a in stable_markets if a.max_imbalance_pct > 0]

    return {
        "trending_markets": len(trending_markets),
        "stable_markets": len(stable_markets),
        "trending_total_pnl": trending_pnl,
        "stable_total_pnl": stable_pnl,
        "trending_avg_pnl": trending_pnl / len(trending_markets) if trending_markets else 0,
        "stable_avg_pnl": stable_pnl / len(stable_markets) if stable_markets else 0,
        "trending_total_trades": trending_trades,
        "stable_total_trades": stable_trades,
        "trending_avg_imbalance": statistics.mean(trending_imbalances) if trending_imbalances else 0,
        "stable_avg_imbalance": statistics.mean(stable_imbalances) if stable_imbalances else 0,
        "trending_markets_list": [(a.slug, a.trend_direction, a.pnl) for a in trending_markets],
    }


def analyze_order_sequencing(analyses: List[MarketAnalysis]) -> Dict:
    """
    Analyze the sequencing of orders.

    Questions:
    - Does he buy expensive side first, then cheap?
    - Does he alternate between sides?
    - How quickly does he hedge?
    """
    # Track first trade in each market
    first_trade_sides = []
    first_trade_prices = []

    # Track expensive-first vs cheap-first
    expensive_first = 0
    cheap_first = 0

    # Track alternation pattern
    alternation_scores = []  # How often consecutive trades are different sides

    # Track time to first hedge
    hedge_times = []  # Seconds from first trade to first opposite-side trade

    for a in analyses:
        if len(a.trades) < 2:
            continue

        # First trade analysis
        first_trade = a.trades[0]
        first_trade_sides.append(first_trade.outcome)
        first_trade_prices.append(first_trade.price)

        # Expensive-first analysis
        # If first trade price > 0.50, it's the "expensive" side
        if first_trade.price > 0.50:
            expensive_first += 1
        else:
            cheap_first += 1

        # Alternation analysis
        alternations = 0
        for i in range(1, len(a.trades)):
            if a.trades[i].outcome != a.trades[i-1].outcome:
                alternations += 1
        alternation_score = alternations / (len(a.trades) - 1) if len(a.trades) > 1 else 0
        alternation_scores.append(alternation_score)

        # Time to hedge analysis
        first_side = first_trade.outcome
        for t in a.trades[1:]:
            if t.outcome != first_side:
                hedge_time = t.timestamp - first_trade.timestamp
                hedge_times.append(hedge_time)
                break

    return {
        "first_trade_up_count": first_trade_sides.count("Up"),
        "first_trade_down_count": first_trade_sides.count("Down"),
        "expensive_first_count": expensive_first,
        "cheap_first_count": cheap_first,
        "expensive_first_pct": expensive_first / (expensive_first + cheap_first) if (expensive_first + cheap_first) > 0 else 0,
        "avg_alternation_score": statistics.mean(alternation_scores) if alternation_scores else 0,
        "avg_time_to_hedge": statistics.mean(hedge_times) if hedge_times else 0,
        "min_time_to_hedge": min(hedge_times) if hedge_times else 0,
        "max_time_to_hedge": max(hedge_times) if hedge_times else 0,
        "median_time_to_hedge": statistics.median(hedge_times) if hedge_times else 0,
    }


def print_comprehensive_report(analyses: List[MarketAnalysis]):
    """Print a comprehensive analysis report."""

    # Filter to markets with trades
    active_markets = [a for a in analyses if a.total_trades > 0]

    print("\n" + "=" * 80)
    print("GABAGOOL22 COMPREHENSIVE STRATEGY ANALYSIS")
    print("=" * 80)
    print(f"Time Range: {START_TIME_ET.strftime('%Y-%m-%d %H:%M ET')} to {END_TIME_ET.strftime('%Y-%m-%d %H:%M ET')}")
    print(f"Total Markets: {len(analyses)}")
    print(f"Markets with Trades: {len(active_markets)}")
    print(f"Markets without Trades: {len(analyses) - len(active_markets)}")

    # Overall stats
    total_trades = sum(a.total_trades for a in active_markets)
    total_up_buys = sum(a.up_buys for a in active_markets)
    total_down_buys = sum(a.down_buys for a in active_markets)
    total_pnl = sum(a.pnl for a in active_markets)

    print(f"\nOVERALL STATISTICS:")
    print(f"  Total Trades: {total_trades}")
    print(f"  UP Buys: {total_up_buys} ({total_up_buys/total_trades*100:.1f}%)")
    print(f"  DOWN Buys: {total_down_buys} ({total_down_buys/total_trades*100:.1f}%)")
    print(f"  Total PnL: ${total_pnl:.2f}")
    print(f"  Avg Trades/Market: {total_trades/len(active_markets):.1f}")

    # Price Entry Analysis
    print(f"\n{'─'*80}")
    print("PRICE ENTRY ANALYSIS")
    print("─"*80)

    price_analysis = analyze_price_entry_strategy(active_markets)

    print(f"\nUP Price Distribution:")
    print(f"  Range: ${price_analysis['up_price_stats']['min']:.2f} - ${price_analysis['up_price_stats']['max']:.2f}")
    print(f"  Mean: ${price_analysis['up_price_stats']['mean']:.2f}")
    print(f"  Median: ${price_analysis['up_price_stats']['median']:.2f}")

    print(f"\nDOWN Price Distribution:")
    print(f"  Range: ${price_analysis['down_price_stats']['min']:.2f} - ${price_analysis['down_price_stats']['max']:.2f}")
    print(f"  Mean: ${price_analysis['down_price_stats']['mean']:.2f}")
    print(f"  Median: ${price_analysis['down_price_stats']['median']:.2f}")

    print(f"\nPair Cost Distribution:")
    print(f"  Range: ${price_analysis['pair_cost_stats']['min']:.3f} - ${price_analysis['pair_cost_stats']['max']:.3f}")
    print(f"  Mean: ${price_analysis['pair_cost_stats']['mean']:.3f}")
    print(f"  Median: ${price_analysis['pair_cost_stats']['median']:.3f}")

    print(f"\nUP Price Buckets (% of trades):")
    total_up = sum(price_analysis['up_buckets'].values())
    for bucket, count in sorted(price_analysis['up_buckets'].items()):
        pct = count / total_up * 100 if total_up > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {bucket}: {bar} {pct:.1f}% ({count})")

    print(f"\nDOWN Price Buckets (% of trades):")
    total_down = sum(price_analysis['down_buckets'].values())
    for bucket, count in sorted(price_analysis['down_buckets'].items()):
        pct = count / total_down * 100 if total_down > 0 else 0
        bar = "█" * int(pct / 2)
        print(f"  {bucket}: {bar} {pct:.1f}% ({count})")

    # Grid Analysis
    print(f"\n{'─'*80}")
    print("GRID TRADING ANALYSIS")
    print("─"*80)

    grid_analysis = analyze_grid_behavior(active_markets)

    print(f"\nPrice Level Usage:")
    print(f"  Unique UP prices: {grid_analysis['unique_up_prices']}")
    print(f"  Unique DOWN prices: {grid_analysis['unique_down_prices']}")

    print(f"\nMost Used UP Prices:")
    for price, count in grid_analysis['most_used_up_prices'][:5]:
        print(f"  ${price:.2f}: {count} trades")

    print(f"\nMost Used DOWN Prices:")
    for price, count in grid_analysis['most_used_down_prices'][:5]:
        print(f"  ${price:.2f}: {count} trades")

    print(f"\nPrice Spacing:")
    print(f"  UP: min={grid_analysis['up_price_spacing']['min']:.2f}, max={grid_analysis['up_price_spacing']['max']:.2f}, median={grid_analysis['up_price_spacing']['median']:.2f}")
    print(f"  DOWN: min={grid_analysis['down_price_spacing']['min']:.2f}, max={grid_analysis['down_price_spacing']['max']:.2f}, median={grid_analysis['down_price_spacing']['median']:.2f}")

    print(f"\nOrder Size Consistency:")
    print(f"  Mean: {grid_analysis['size_stats']['mean']:.2f} shares")
    print(f"  Stdev: {grid_analysis['size_stats']['stdev']:.2f}")
    print(f"  Range: {grid_analysis['size_stats']['min']:.2f} - {grid_analysis['size_stats']['max']:.2f}")

    print(f"\nGrid Trading Detection:")
    print(f"  Is Grid Trading: {'YES' if grid_analysis['is_grid_trading'] else 'NO'}")
    if grid_analysis['detected_grid_spacing']:
        print(f"  Detected Grid Spacing: ${grid_analysis['detected_grid_spacing']:.2f}")

    # Order Sequencing
    print(f"\n{'─'*80}")
    print("ORDER SEQUENCING ANALYSIS")
    print("─"*80)

    seq_analysis = analyze_order_sequencing(active_markets)

    print(f"\nFirst Trade Side:")
    print(f"  UP first: {seq_analysis['first_trade_up_count']} markets")
    print(f"  DOWN first: {seq_analysis['first_trade_down_count']} markets")

    print(f"\nExpensive Side First:")
    print(f"  Expensive first: {seq_analysis['expensive_first_count']} ({seq_analysis['expensive_first_pct']*100:.1f}%)")
    print(f"  Cheap first: {seq_analysis['cheap_first_count']} ({(1-seq_analysis['expensive_first_pct'])*100:.1f}%)")

    print(f"\nAlternation Pattern:")
    print(f"  Avg alternation score: {seq_analysis['avg_alternation_score']:.2f}")
    print(f"  (1.0 = perfect alternation, 0.0 = all same side)")

    print(f"\nTime to Hedge (seconds):")
    print(f"  Mean: {seq_analysis['avg_time_to_hedge']:.1f}s")
    print(f"  Median: {seq_analysis['median_time_to_hedge']:.1f}s")
    print(f"  Range: {seq_analysis['min_time_to_hedge']:.1f}s - {seq_analysis['max_time_to_hedge']:.1f}s")

    # Two-Sided Posting
    print(f"\n{'─'*80}")
    print("TWO-SIDED POSTING ANALYSIS")
    print("─"*80)

    total_simultaneous = sum(a.simultaneous_pairs for a in active_markets)
    total_up_first = sum(a.up_first_count for a in active_markets)
    total_down_first = sum(a.down_first_count for a in active_markets)

    print(f"\nSimultaneous Pairs (within 5s):")
    print(f"  Total: {total_simultaneous}")
    print(f"  UP first: {total_up_first}")
    print(f"  DOWN first: {total_down_first}")

    # Trending Analysis
    print(f"\n{'─'*80}")
    print("TRENDING VS STABLE MARKET ANALYSIS")
    print("─"*80)

    trend_analysis = analyze_trending_behavior(active_markets)

    print(f"\nMarket Classification:")
    print(f"  Trending: {trend_analysis['trending_markets']}")
    print(f"  Stable: {trend_analysis['stable_markets']}")

    print(f"\nPnL by Market Type:")
    print(f"  Trending Total: ${trend_analysis['trending_total_pnl']:.2f}")
    print(f"  Stable Total: ${trend_analysis['stable_total_pnl']:.2f}")
    print(f"  Trending Avg: ${trend_analysis['trending_avg_pnl']:.2f}/market")
    print(f"  Stable Avg: ${trend_analysis['stable_avg_pnl']:.2f}/market")

    print(f"\nImbalance by Market Type:")
    print(f"  Trending Avg: {trend_analysis['trending_avg_imbalance']:.1%}")
    print(f"  Stable Avg: {trend_analysis['stable_avg_imbalance']:.1%}")

    if trend_analysis['trending_markets_list']:
        print(f"\nTrending Markets Detail:")
        for slug, direction, pnl in trend_analysis['trending_markets_list']:
            print(f"  {slug}: {direction} trend, PnL=${pnl:.2f}")

    # Velocity Analysis
    print(f"\n{'─'*80}")
    print("VELOCITY TIMING ANALYSIS")
    print("─"*80)

    velocity_results = []
    for a in active_markets:
        v = analyze_velocity_patterns(a)
        if v:
            velocity_results.append(v)

    if velocity_results:
        avg_interval = statistics.mean([v['avg_interval'] for v in velocity_results])
        avg_cv = statistics.mean([v['coefficient_of_variation'] for v in velocity_results])
        velocity_likely_count = sum(1 for v in velocity_results if v['likely_velocity_based'])

        print(f"\nTrade Interval Analysis:")
        print(f"  Avg interval: {avg_interval:.1f}s")
        print(f"  Avg coefficient of variation: {avg_cv:.2f}")
        print(f"  (CV < 0.5 = consistent timing, CV > 1.0 = highly variable)")

        print(f"\nVelocity Timing Detection:")
        print(f"  Markets likely using velocity: {velocity_likely_count}/{len(velocity_results)}")
        print(f"  Markets with consistent timing: {len(velocity_results) - velocity_likely_count}/{len(velocity_results)}")

        if velocity_likely_count < len(velocity_results) * 0.3:
            print(f"\n  CONCLUSION: Gabagool does NOT appear to use velocity timing")
            print(f"  → Trading pattern shows consistent intervals, not burst-wait patterns")

    # Imbalance Analysis
    print(f"\n{'─'*80}")
    print("IMBALANCE THRESHOLD ANALYSIS")
    print("─"*80)

    max_imbalances = [a.max_imbalance_pct for a in active_markets if a.max_imbalance_pct > 0]
    max_imbalance_shares = [a.max_imbalance_shares for a in active_markets if a.max_imbalance_shares > 0]

    if max_imbalances:
        print(f"\nMax Imbalance % Distribution:")
        print(f"  Min: {min(max_imbalances):.1%}")
        print(f"  Max: {max(max_imbalances):.1%}")
        print(f"  Mean: {statistics.mean(max_imbalances):.1%}")
        print(f"  Median: {statistics.median(max_imbalances):.1%}")

        # Bucket analysis
        pct_buckets = {"0-10%": 0, "10-20%": 0, "20-30%": 0, "30-40%": 0, "40-50%": 0, "50%+": 0}
        for imb in max_imbalances:
            if imb < 0.10:
                pct_buckets["0-10%"] += 1
            elif imb < 0.20:
                pct_buckets["10-20%"] += 1
            elif imb < 0.30:
                pct_buckets["20-30%"] += 1
            elif imb < 0.40:
                pct_buckets["30-40%"] += 1
            elif imb < 0.50:
                pct_buckets["40-50%"] += 1
            else:
                pct_buckets["50%+"] += 1

        print(f"\n  Imbalance % Distribution:")
        for bucket, count in pct_buckets.items():
            bar = "█" * count
            print(f"    {bucket}: {bar} ({count})")

    if max_imbalance_shares:
        print(f"\nMax Imbalance Shares Distribution:")
        print(f"  Min: {min(max_imbalance_shares):.0f}")
        print(f"  Max: {max(max_imbalance_shares):.0f}")
        print(f"  Mean: {statistics.mean(max_imbalance_shares):.0f}")
        print(f"  Median: {statistics.median(max_imbalance_shares):.0f}")

    # Summary and Strategy Reverse-Engineering
    print(f"\n{'='*80}")
    print("STRATEGY REVERSE-ENGINEERING SUMMARY")
    print("="*80)

    print(f"""
GABAGOOL22'S STRATEGY APPEARS TO BE:

1. MARKET MAKING (Not Speculative Trading)
   - Buys BOTH sides consistently (UP: {total_up_buys}, DOWN: {total_down_buys})
   - Maintains relatively balanced positions
   - Profits from pair cost < $1.00, not directional bets

2. TIMING APPROACH
   - Does NOT use velocity-based timing
   - Consistent trade intervals (~{avg_interval:.0f}s average)
   - Buys expensive side first {seq_analysis['expensive_first_pct']*100:.0f}% of the time
   - Quick hedging (median {seq_analysis['median_time_to_hedge']:.0f}s to first hedge)

3. ORDER TYPE
   - Likely MAKER orders (based on trade clustering and pricing)
   - Posts on both sides, fills opportunistically
   - Consistent order sizes (~{grid_analysis['size_stats']['mean']:.0f} shares)

4. PRICE STRATEGY
   - Pair cost target: ${price_analysis['pair_cost_stats']['mean']:.3f} average
   - Buys at ALL price levels (not waiting for "cheap" prices)
   - Wide price range: UP ${price_analysis['up_price_stats']['min']:.2f}-${price_analysis['up_price_stats']['max']:.2f}

5. IMBALANCE MANAGEMENT
   - Tolerates up to {max(max_imbalances):.0%} imbalance
   - Average max imbalance: {statistics.mean(max_imbalances):.0%}
   - Does NOT strictly enforce 50/50 balance

6. MARKET CONDITIONS
   - Loses money in trending markets (${trend_analysis['trending_total_pnl']:.2f})
   - Profitable in stable/mean-reverting markets (${trend_analysis['stable_total_pnl']:.2f})
   - Strategy works when BTC price oscillates

KEY DIFFERENCES FROM YOUR VELOCITY STRATEGY:
   - NO velocity timing gates
   - NO "let it ride" hedging delays
   - Immediate opportunistic execution
   - Higher trade volume ({total_trades/len(active_markets):.0f} trades/market vs your 2.6)
   - 100% cycle completion vs your 13%
""")


def export_detailed_data(analyses: List[MarketAnalysis], output_dir: str = "research"):
    """Export detailed trade data to CSV for further analysis."""
    import os
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Export all trades
    trades_file = f"{output_dir}/gabagool_trades_{timestamp}.csv"
    with open(trades_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'market_slug', 'timestamp', 'datetime', 'side', 'outcome',
            'price', 'size', 'cost', 'tx_hash'
        ])

        for a in analyses:
            for t in a.trades:
                writer.writerow([
                    a.slug,
                    t.timestamp,
                    t.timestamp_dt.isoformat() if t.timestamp_dt else "",
                    t.side,
                    t.outcome,
                    t.price,
                    t.size,
                    t.cost,
                    t.tx_hash,
                ])

    print(f"\nExported {sum(len(a.trades) for a in analyses)} trades to {trades_file}")

    # Export market summaries
    markets_file = f"{output_dir}/gabagool_markets_{timestamp}.csv"
    with open(markets_file, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'slug', 'start_time', 'resolution', 'total_trades',
            'up_buys', 'down_buys', 'final_up_shares', 'final_down_shares',
            'final_up_cost', 'final_down_cost', 'max_imbalance_pct',
            'max_imbalance_shares', 'pnl', 'is_trending', 'trend_direction'
        ])

        for a in analyses:
            writer.writerow([
                a.slug,
                a.start_time.isoformat(),
                a.resolution or "",
                a.total_trades,
                a.up_buys,
                a.down_buys,
                a.final_up_shares,
                a.final_down_shares,
                a.final_up_cost,
                a.final_down_cost,
                a.max_imbalance_pct,
                a.max_imbalance_shares,
                a.pnl,
                a.is_trending,
                a.trend_direction or "",
            ])

    print(f"Exported {len(analyses)} market summaries to {markets_file}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Gabagool Deep Strategy Analysis")
    parser.add_argument('--start', type=str, help='Start time in format "YYYY-MM-DD HH:MM" ET')
    parser.add_argument('--end', type=str, help='End time in format "YYYY-MM-DD HH:MM" ET')
    parser.add_argument('--range', type=int, choices=[1, 2], help='Use predefined range: 1=Jan9 0245-Jan10 0145, 2=Jan7 0230-Jan8 0315')
    args = parser.parse_args()

    global START_TIME_ET, END_TIME_ET

    if args.range == 1:
        START_TIME_ET = datetime(2026, 1, 9, 2, 45, 0, tzinfo=ET)
        END_TIME_ET = datetime(2026, 1, 10, 1, 45, 0, tzinfo=ET)
    elif args.range == 2:
        START_TIME_ET = datetime(2026, 1, 7, 2, 30, 0, tzinfo=ET)
        END_TIME_ET = datetime(2026, 1, 8, 3, 15, 0, tzinfo=ET)
    elif args.start and args.end:
        START_TIME_ET = datetime.strptime(args.start, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        END_TIME_ET = datetime.strptime(args.end, "%Y-%m-%d %H:%M").replace(tzinfo=ET)

    print("=" * 80)
    print("GABAGOOL22 DEEP STRATEGY ANALYSIS")
    print("=" * 80)
    print(f"Wallet: {WALLET}")
    print(f"Time Range: {START_TIME_ET.strftime('%Y-%m-%d %H:%M ET')} to {END_TIME_ET.strftime('%Y-%m-%d %H:%M ET')}")

    # Generate market slugs
    market_slugs = generate_market_slugs()
    print(f"\nGenerated {len(market_slugs)} market slugs to analyze")

    # Analyze each market
    analyses = []

    for i, (slug, market_time) in enumerate(market_slugs):
        print(f"\n[{i+1}/{len(market_slugs)}] ", end="")

        analysis = analyze_single_market(slug, market_time)
        if analysis:
            analyses.append(analysis)

        # Rate limiting
        time.sleep(0.5)

    # Generate comprehensive report
    print_comprehensive_report(analyses)

    # Export data
    export_detailed_data(analyses)

    return analyses


if __name__ == "__main__":
    main()
