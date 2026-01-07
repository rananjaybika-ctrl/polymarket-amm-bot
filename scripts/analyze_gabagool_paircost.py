#!/usr/bin/env python3
"""
Gabagool22 Pair Cost Analysis

Analyzes gabagool22's trading patterns to reverse-engineer their
opportunistic market maker constraints.

Key insight: Polymarket uses unified orderbook for binary markets:
- Sell YES = Buy NO (at 1 - price)
- Sell NO = Buy YES (at 1 - price)
Therefore, we IGNORE all SELL trades - they're artifacts, not real sells.

Usage:
    python scripts/analyze_gabagool_paircost.py
"""

import requests
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import statistics
import random

# API endpoints
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
TRADES_URL = "https://data-api.polymarket.com/trades"
MARKET_URL = "https://gamma-api.polymarket.com/markets"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"


def generate_random_market_queries(count: int = 8, days_back: int = 7) -> List[str]:
    """
    Generate random BTC 15-min market queries for past markets.

    Format: "Bitcoin Up or Down - December 23, 4:00AM-4:15AM ET"

    Args:
        count: Number of random markets to generate
        days_back: How many days back to look (default 7)

    Returns:
        List of market query strings
    """
    et = ZoneInfo('America/New_York')
    now = datetime.now(et)

    # Generate random timestamps from the past (at 15-min boundaries)
    queries = []
    attempts = 0
    max_attempts = count * 10  # Prevent infinite loop

    while len(queries) < count and attempts < max_attempts:
        attempts += 1

        # Random day in the past (1 to days_back days ago)
        days_ago = random.randint(1, days_back)
        random_date = now - timedelta(days=days_ago)

        # Random hour (0-23) and minute (0, 15, 30, 45)
        hour = random.randint(0, 23)
        minute = random.choice([0, 15, 30, 45])

        # Create the start time
        start_time = random_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
        end_time = start_time + timedelta(minutes=15)

        # Skip if this would be in the future
        if start_time >= now:
            continue

        # Format the query
        # "Bitcoin Up or Down - December 23, 4:00AM-4:15AM ET"
        month = start_time.strftime("%B")
        day = start_time.day

        # Format times (4:00AM, 10:30PM, etc.)
        start_hour = start_time.hour
        start_minute = start_time.minute
        end_hour = end_time.hour
        end_minute = end_time.minute

        start_ampm = "AM" if start_hour < 12 else "PM"
        end_ampm = "AM" if end_hour < 12 else "PM"

        start_hour_12 = start_hour % 12 or 12
        end_hour_12 = end_hour % 12 or 12

        start_str = f"{start_hour_12}:{start_minute:02d}{start_ampm}"
        end_str = f"{end_hour_12}:{end_minute:02d}{end_ampm}"

        query = f"Bitcoin Up or Down - {month} {day}, {start_str}-{end_str} ET"

        # Avoid duplicates
        if query not in queries:
            queries.append(query)

    return queries


def fetch_random_gabagool_markets(count: int = 8, days_back: int = 7, verbose: bool = True) -> List[Dict]:
    """
    Fetch random past markets where gabagool22 traded.

    Args:
        count: Number of markets to fetch
        days_back: How many days back to search
        verbose: Print progress

    Returns:
        List of market analysis results
    """
    if verbose:
        print("=" * 60)
        print(f"FETCHING {count} RANDOM GABAGOOL22 MARKETS")
        print("=" * 60)
        print(f"Looking back {days_back} days")

    # Generate more queries than needed (some may not have gabagool trades)
    queries = generate_random_market_queries(count * 3, days_back)

    if verbose:
        print(f"Generated {len(queries)} candidate market queries")

    results = []

    for query in queries:
        if len(results) >= count:
            break

        if verbose:
            print(f"\nTrying: {query}")

        # Search for market
        market = search_market(query)
        if not market:
            if verbose:
                print("  → Market not found")
            continue

        condition_id = market.get("conditionId", "")
        if not condition_id:
            if verbose:
                print("  → No condition ID")
            continue

        # Check if gabagool22 traded this market
        trades = fetch_trades(condition_id, WALLET)
        buys = [t for t in trades if t.get("side", "").upper() == "BUY"]

        if not buys:
            if verbose:
                print(f"  → No gabagool22 trades")
            continue

        if verbose:
            print(f"  → Found {len(buys)} BUY trades!")

        results.append({
            "query": query,
            "slug": market.get("slug", "unknown"),
            "condition_id": condition_id,
            "buy_count": len(buys),
            "market": market,
        })

    if verbose:
        print(f"\n{'=' * 60}")
        print(f"Found {len(results)} markets with gabagool22 trades")
        print("=" * 60)
        for r in results:
            print(f"  {r['query']}: {r['buy_count']} buys")

    return results


# Markets to analyze (search queries)
# IMPULSIVE/TRENDING MARKETS (single direction moves)
MARKETS_TO_ANALYZE = [
    "Bitcoin Up or Down December 21 9:00PM",
    "Bitcoin Up or Down December 17 9:45AM",
    "Bitcoin Up or Down December 17 10:30AM",
    "Bitcoin Up or Down December 17 10:45AM",
    "Bitcoin Up or Down December 15 9:45AM",
]


@dataclass
class TradeRecord:
    """Single trade with running position info."""
    trade_num: int
    timestamp: datetime
    side: str  # UP or DOWN
    price: float
    shares: float
    cost: float
    # Running position after this trade
    up_pos: float
    down_pos: float
    up_cost: float
    down_cost: float
    # Calculated metrics
    pair_cost: float  # avg_up + avg_down for hedged portion
    imbalance: float  # abs(up_pos - down_pos)
    hedged_pairs: float  # min(up_pos, down_pos)


@dataclass
class MarketPairCostAnalysis:
    """Complete pair cost analysis for one market."""
    slug: str
    title: str
    condition_id: str
    total_buys: int
    total_sells_ignored: int
    # Final position
    final_up_pos: float
    final_down_pos: float
    final_up_cost: float
    final_down_cost: float
    final_pair_cost: float
    final_hedged_pairs: float
    final_imbalance: float
    # Trade records
    trades: List[TradeRecord] = field(default_factory=list)
    # Constraint analysis
    max_imbalance_observed: float = 0.0
    max_imbalance_ratio: float = 1.0
    pair_costs_when_balanced: List[float] = field(default_factory=list)
    all_prospective_pair_costs: List[float] = field(default_factory=list)
    up_prices: List[float] = field(default_factory=list)
    down_prices: List[float] = field(default_factory=list)


def search_market(query: str) -> Optional[Dict[str, Any]]:
    """Search for a market by name/query."""
    try:
        resp = requests.get(SEARCH_URL, params={"q": query}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"  Error searching market: {e}")
        return None

    events = data.get("events", []) if isinstance(data, dict) else []
    for event in events:
        markets = event.get("markets") or []
        if markets:
            return markets[0]
    return None


def fetch_trades(condition_id: str, user_address: str, page_limit: int = 5000) -> List[Dict]:
    """Fetch all trades for a condition/user with pagination."""
    all_trades = []
    offset = 0

    while True:
        params = {
            "limit": page_limit,
            "offset": offset,
            "takerOnly": "false",
            "market": condition_id,
            "user": user_address,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"  Error fetching trades: {e}")
            return []

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


def analyze_market_pair_cost(market_query: str) -> Optional[MarketPairCostAnalysis]:
    """Analyze pair cost patterns for a single market."""
    print(f"\n{'='*60}")
    print(f"Analyzing: {market_query}")
    print("="*60)

    # Find market
    market = search_market(market_query)
    if not market:
        print(f"  Market not found: {market_query}")
        return None

    slug = market.get("slug", "unknown")
    title = market.get("question") or market.get("title") or slug
    condition_id = market.get("conditionId", "")

    print(f"  Slug: {slug}")
    print(f"  Title: {title[:60]}...")

    if not condition_id:
        print(f"  No condition ID found")
        return None

    # Fetch trades
    all_trades = fetch_trades(condition_id, WALLET)
    print(f"  Total trades fetched: {len(all_trades)}")

    if not all_trades:
        return None

    # Separate BUY and SELL (we ignore SELL)
    buys = [t for t in all_trades if t.get("side", "").upper() == "BUY"]
    sells = [t for t in all_trades if t.get("side", "").upper() == "SELL"]

    print(f"  BUY trades: {len(buys)} (analyzing these)")
    print(f"  SELL trades: {len(sells)} (ignoring - unified orderbook artifacts)")

    if not buys:
        return None

    # Sort by timestamp (oldest first)
    buys.sort(key=lambda t: t.get("timestamp", 0))

    # Initialize position tracking
    up_pos = 0.0
    down_pos = 0.0
    up_cost = 0.0
    down_cost = 0.0

    trade_records = []
    max_imbalance = 0.0
    max_imbalance_ratio = 1.0
    pair_costs_balanced = []
    prospective_costs = []
    up_prices = []
    down_prices = []

    for i, trade in enumerate(buys, 1):
        outcome = trade.get("outcome", "").lower()
        price = float(trade.get("price", 0))
        shares = float(trade.get("size", 0))
        cost = price * shares
        timestamp_ms = trade.get("timestamp", 0)

        # Determine side (Up = YES, Down = NO)
        is_up = outcome == "up"
        side = "UP" if is_up else "DOWN"

        # Calculate prospective pair cost BEFORE this trade
        if up_pos > 0 and down_pos > 0:
            # What would pair cost be after this buy?
            if is_up:
                new_up_cost = up_cost + cost
                new_up_pos = up_pos + shares
                avg_up = new_up_cost / new_up_pos
                avg_down = down_cost / down_pos
            else:
                new_down_cost = down_cost + cost
                new_down_pos = down_pos + shares
                avg_up = up_cost / up_pos
                avg_down = new_down_cost / new_down_pos
            prospective = avg_up + avg_down
            prospective_costs.append(prospective)

        # Update position
        if is_up:
            up_pos += shares
            up_cost += cost
            up_prices.append(price)
        else:
            down_pos += shares
            down_cost += cost
            down_prices.append(price)

        # Calculate current pair cost (for hedged portion)
        hedged = min(up_pos, down_pos)
        if hedged > 0:
            avg_up = up_cost / up_pos if up_pos > 0 else 0
            avg_down = down_cost / down_pos if down_pos > 0 else 0
            pair_cost = avg_up + avg_down
        else:
            # Not yet hedged - use current price as placeholder
            pair_cost = price

        # Track imbalance
        imbalance = abs(up_pos - down_pos)
        if imbalance > max_imbalance:
            max_imbalance = imbalance

        # Track imbalance ratio
        if hedged > 0:
            ratio = max(up_pos, down_pos) / hedged
            if ratio > max_imbalance_ratio:
                max_imbalance_ratio = ratio

        # Track pair cost at balanced moments
        if abs(up_pos - down_pos) < 1:  # Within 1 share of balanced
            pair_costs_balanced.append(pair_cost)

        # Create trade record
        record = TradeRecord(
            trade_num=i,
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000) if timestamp_ms else datetime.now(),
            side=side,
            price=price,
            shares=shares,
            cost=cost,
            up_pos=up_pos,
            down_pos=down_pos,
            up_cost=up_cost,
            down_cost=down_cost,
            pair_cost=pair_cost,
            imbalance=imbalance,
            hedged_pairs=hedged,
        )
        trade_records.append(record)

    # Final calculations
    final_hedged = min(up_pos, down_pos)
    final_pair_cost = 0.0
    if final_hedged > 0:
        final_pair_cost = (up_cost / up_pos) + (down_cost / down_pos)

    analysis = MarketPairCostAnalysis(
        slug=slug,
        title=title[:60],
        condition_id=condition_id,
        total_buys=len(buys),
        total_sells_ignored=len(sells),
        final_up_pos=up_pos,
        final_down_pos=down_pos,
        final_up_cost=up_cost,
        final_down_cost=down_cost,
        final_pair_cost=final_pair_cost,
        final_hedged_pairs=final_hedged,
        final_imbalance=abs(up_pos - down_pos),
        trades=trade_records,
        max_imbalance_observed=max_imbalance,
        max_imbalance_ratio=max_imbalance_ratio,
        pair_costs_when_balanced=pair_costs_balanced,
        all_prospective_pair_costs=prospective_costs,
        up_prices=up_prices,
        down_prices=down_prices,
    )

    return analysis


def analyze_hedge_timing(analysis: MarketPairCostAnalysis):
    """Analyze when cheap vs expensive buys happen."""
    if not analysis.trades:
        return

    print(f"\n{'─'*60}")
    print(f"HEDGE TIMING ANALYSIS: {analysis.slug}")
    print("─"*60)

    # Categorize trades by price
    cheap_threshold = 0.45
    expensive_threshold = 0.55

    cheap_up = [t for t in analysis.trades if t.side == "UP" and t.price < cheap_threshold]
    mid_up = [t for t in analysis.trades if t.side == "UP" and cheap_threshold <= t.price <= expensive_threshold]
    expensive_up = [t for t in analysis.trades if t.side == "UP" and t.price > expensive_threshold]

    cheap_down = [t for t in analysis.trades if t.side == "DOWN" and t.price < cheap_threshold]
    mid_down = [t for t in analysis.trades if t.side == "DOWN" and cheap_threshold <= t.price <= expensive_threshold]
    expensive_down = [t for t in analysis.trades if t.side == "DOWN" and t.price > expensive_threshold]

    print(f"\nPRICE DISTRIBUTION:")
    print(f"  UP trades:   {len(cheap_up):>3} cheap (<${cheap_threshold}) | {len(mid_up):>3} mid | {len(expensive_up):>3} expensive (>${expensive_threshold})")
    print(f"  DOWN trades: {len(cheap_down):>3} cheap (<${cheap_threshold}) | {len(mid_down):>3} mid | {len(expensive_down):>3} expensive (>${expensive_threshold})")

    # Analyze sequence - when do expensive buys happen?
    print(f"\nTRADE SEQUENCE ANALYSIS:")

    # Divide trades into thirds (early, mid, late)
    n = len(analysis.trades)
    early = analysis.trades[:n//3]
    mid = analysis.trades[n//3:2*n//3]
    late = analysis.trades[2*n//3:]

    for period_name, period_trades in [("Early (first 1/3)", early), ("Mid (middle 1/3)", mid), ("Late (last 1/3)", late)]:
        up_trades = [t for t in period_trades if t.side == "UP"]
        down_trades = [t for t in period_trades if t.side == "DOWN"]

        avg_up = statistics.mean([t.price for t in up_trades]) if up_trades else 0
        avg_down = statistics.mean([t.price for t in down_trades]) if down_trades else 0

        up_expensive_count = len([t for t in up_trades if t.price > expensive_threshold])
        down_expensive_count = len([t for t in down_trades if t.price > expensive_threshold])

        print(f"\n  {period_name}:")
        print(f"    UP:   {len(up_trades):>3} trades, avg ${avg_up:.3f}, {up_expensive_count} expensive")
        print(f"    DOWN: {len(down_trades):>3} trades, avg ${avg_down:.3f}, {down_expensive_count} expensive")

    # Find max imbalance point and what happened after
    max_imb_trade = max(analysis.trades, key=lambda t: t.imbalance)
    max_imb_idx = analysis.trades.index(max_imb_trade)

    print(f"\n  MAX IMBALANCE POINT:")
    print(f"    Trade #{max_imb_trade.trade_num}: {max_imb_trade.imbalance:.0f} shares imbalanced")
    print(f"    Position: UP={max_imb_trade.up_pos:.0f}, DOWN={max_imb_trade.down_pos:.0f}")
    print(f"    Pair cost at this point: ${max_imb_trade.pair_cost:.4f}")

    # What happened after max imbalance?
    if max_imb_idx < len(analysis.trades) - 1:
        trades_after = analysis.trades[max_imb_idx+1:]
        deficit_side = "UP" if max_imb_trade.down_pos > max_imb_trade.up_pos else "DOWN"
        hedge_trades = [t for t in trades_after if t.side == deficit_side]

        if hedge_trades:
            avg_hedge_price = statistics.mean([t.price for t in hedge_trades])
            expensive_hedges = [t for t in hedge_trades if t.price > expensive_threshold]
            print(f"\n  RECOVERY AFTER MAX IMBALANCE:")
            print(f"    Deficit side: {deficit_side}")
            print(f"    {len(hedge_trades)} {deficit_side} buys after max imbalance")
            print(f"    Avg price for recovery buys: ${avg_hedge_price:.4f}")
            print(f"    Expensive recovery buys (>${expensive_threshold}): {len(expensive_hedges)}")

    # Timeline view - show position evolution
    print(f"\n  POSITION EVOLUTION (every 50 trades):")
    print(f"  +-------+--------+--------+-----------+--------+")
    print(f"  | Trade |  UP_pos| DN_pos | Pair_cost | Imbal  |")
    print(f"  +-------+--------+--------+-----------+--------+")
    for i, t in enumerate(analysis.trades):
        if i % 50 == 0 or i == len(analysis.trades) - 1:
            print(f"  | {t.trade_num:>5} | {t.up_pos:>6.0f} | {t.down_pos:>6.0f} | ${t.pair_cost:>8.4f} | {t.imbalance:>6.0f} |")
    print(f"  +-------+--------+--------+-----------+--------+")


def print_market_analysis(analysis: MarketPairCostAnalysis, verbose: bool = False):
    """Print analysis results for a market."""
    print(f"\n{'─'*60}")
    print(f"Market: {analysis.slug}")
    print(f"{'─'*60}")

    print(f"\nFINAL POSITION:")
    print(f"  UP:   {analysis.final_up_pos:,.0f} shares (${analysis.final_up_cost:,.2f} cost)")
    print(f"  DOWN: {analysis.final_down_pos:,.0f} shares (${analysis.final_down_cost:,.2f} cost)")
    print(f"  Hedged Pairs: {analysis.final_hedged_pairs:,.0f}")
    print(f"  Imbalance: {analysis.final_imbalance:,.0f} shares")
    print(f"  Final Pair Cost: ${analysis.final_pair_cost:.4f}")

    print(f"\nPRICE ANALYSIS:")
    if analysis.up_prices:
        print(f"  UP prices:   min=${min(analysis.up_prices):.4f}, max=${max(analysis.up_prices):.4f}, avg=${statistics.mean(analysis.up_prices):.4f}")
    if analysis.down_prices:
        print(f"  DOWN prices: min=${min(analysis.down_prices):.4f}, max=${max(analysis.down_prices):.4f}, avg=${statistics.mean(analysis.down_prices):.4f}")

    print(f"\nIMBALANCE ANALYSIS:")
    print(f"  Max imbalance observed: {analysis.max_imbalance_observed:,.0f} shares")
    print(f"  Max imbalance ratio: {analysis.max_imbalance_ratio:.2f}x")

    print(f"\nPAIR COST ANALYSIS:")
    if analysis.pair_costs_when_balanced:
        print(f"  Pair costs when balanced ({len(analysis.pair_costs_when_balanced)} observations):")
        print(f"    Min: ${min(analysis.pair_costs_when_balanced):.4f}")
        print(f"    Max: ${max(analysis.pair_costs_when_balanced):.4f}")
        print(f"    Avg: ${statistics.mean(analysis.pair_costs_when_balanced):.4f}")
        if len(analysis.pair_costs_when_balanced) > 1:
            print(f"    Std: ${statistics.stdev(analysis.pair_costs_when_balanced):.4f}")

    if analysis.all_prospective_pair_costs:
        print(f"  Prospective pair costs ({len(analysis.all_prospective_pair_costs)} buys after first hedge):")
        print(f"    Min: ${min(analysis.all_prospective_pair_costs):.4f}")
        print(f"    Max: ${max(analysis.all_prospective_pair_costs):.4f}")
        print(f"    Avg: ${statistics.mean(analysis.all_prospective_pair_costs):.4f}")
        # Count how many exceeded various thresholds
        for threshold in [0.99, 0.995, 0.999, 1.0]:
            count = sum(1 for p in analysis.all_prospective_pair_costs if p > threshold)
            pct = count / len(analysis.all_prospective_pair_costs) * 100
            print(f"    > ${threshold}: {count} trades ({pct:.1f}%)")

    if verbose and analysis.trades:
        print(f"\nTRADE-BY-TRADE (first 20, last 10):")
        print("+-------+------+--------+--------+----------+----------+-----------+-----------+")
        print("| Trade | Side |  Price | Shares |   UP_pos |  DOWN_pos| Pair_cost | Imbalance |")
        print("+-------+------+--------+--------+----------+----------+-----------+-----------+")

        trades_to_show = analysis.trades[:20] + analysis.trades[-10:] if len(analysis.trades) > 30 else analysis.trades
        shown_indices = set()

        for t in analysis.trades[:20]:
            shown_indices.add(t.trade_num)
            print(f"| {t.trade_num:>5} | {t.side:>4} | {t.price:>6.4f} | {t.shares:>6.0f} | {t.up_pos:>8.0f} | {t.down_pos:>8.0f} | {t.pair_cost:>9.4f} | {t.imbalance:>9.0f} |")

        if len(analysis.trades) > 30:
            print("|  ...  |  ... |   ...  |   ...  |     ...  |     ...  |      ...  |      ...  |")

        for t in analysis.trades[-10:]:
            if t.trade_num not in shown_indices:
                print(f"| {t.trade_num:>5} | {t.side:>4} | {t.price:>6.4f} | {t.shares:>6.0f} | {t.up_pos:>8.0f} | {t.down_pos:>8.0f} | {t.pair_cost:>9.4f} | {t.imbalance:>9.0f} |")

        print("+-------+------+--------+--------+----------+----------+-----------+-----------+")


def print_aggregate_analysis(analyses: List[MarketPairCostAnalysis]):
    """Print aggregate analysis across all markets."""
    print("\n" + "="*60)
    print("AGGREGATE ANALYSIS ACROSS ALL MARKETS")
    print("="*60)

    # Collect all data
    all_pair_costs_balanced = []
    all_prospective_costs = []
    all_up_prices = []
    all_down_prices = []
    all_max_imbalances = []
    all_max_ratios = []
    all_final_pair_costs = []

    for a in analyses:
        all_pair_costs_balanced.extend(a.pair_costs_when_balanced)
        all_prospective_costs.extend(a.all_prospective_pair_costs)
        all_up_prices.extend(a.up_prices)
        all_down_prices.extend(a.down_prices)
        all_max_imbalances.append(a.max_imbalance_observed)
        all_max_ratios.append(a.max_imbalance_ratio)
        if a.final_pair_cost > 0:
            all_final_pair_costs.append(a.final_pair_cost)

    print(f"\nMARKETS ANALYZED: {len(analyses)}")
    print(f"Total BUY trades: {sum(a.total_buys for a in analyses):,}")
    print(f"Total hedged pairs: {sum(a.final_hedged_pairs for a in analyses):,.0f}")

    print(f"\n{'─'*60}")
    print("CONSTRAINT PARAMETERS (REVERSE-ENGINEERED)")
    print("─"*60)

    # Pair cost threshold
    if all_prospective_costs:
        max_prospective = max(all_prospective_costs)
        p99_prospective = sorted(all_prospective_costs)[int(len(all_prospective_costs) * 0.99)]
        print(f"\nPAIR_COST_THRESHOLD:")
        print(f"  Max prospective pair cost observed: ${max_prospective:.4f}")
        print(f"  99th percentile: ${p99_prospective:.4f}")
        print(f"  → Recommended threshold: ${max(max_prospective, 0.999):.4f}")

    # Final pair costs
    if all_final_pair_costs:
        print(f"\nFINAL PAIR COSTS (at market resolution):")
        print(f"  Min: ${min(all_final_pair_costs):.4f}")
        print(f"  Max: ${max(all_final_pair_costs):.4f}")
        print(f"  Avg: ${statistics.mean(all_final_pair_costs):.4f}")

    # Imbalance constraints
    if all_max_imbalances:
        print(f"\nMAX_IMBALANCE_SHARES:")
        print(f"  Across markets: {min(all_max_imbalances):.0f} - {max(all_max_imbalances):.0f}")
        print(f"  Average: {statistics.mean(all_max_imbalances):.0f}")
        print(f"  → Recommended limit: {max(all_max_imbalances):.0f} shares")

    if all_max_ratios:
        print(f"\nMAX_IMBALANCE_RATIO:")
        print(f"  Across markets: {min(all_max_ratios):.2f}x - {max(all_max_ratios):.2f}x")
        print(f"  Average: {statistics.mean(all_max_ratios):.2f}x")
        print(f"  → Recommended limit: {max(all_max_ratios):.2f}x")

    # Price constraints
    if all_up_prices and all_down_prices:
        print(f"\nPRICE CONSTRAINTS:")
        print(f"  UP prices:   ${min(all_up_prices):.4f} - ${max(all_up_prices):.4f} (avg ${statistics.mean(all_up_prices):.4f})")
        print(f"  DOWN prices: ${min(all_down_prices):.4f} - ${max(all_down_prices):.4f} (avg ${statistics.mean(all_down_prices):.4f})")
        all_prices = all_up_prices + all_down_prices
        print(f"  All prices:  ${min(all_prices):.4f} - ${max(all_prices):.4f} (avg ${statistics.mean(all_prices):.4f})")
        print(f"  → Max price paid: ${max(all_prices):.4f}")

    # Summary table
    print(f"\n{'─'*60}")
    print("SUMMARY: GABAGOOL22 CONSTRAINTS")
    print("─"*60)
    print("+---------------------------+------------------+")
    print("| Parameter                 | Value            |")
    print("+---------------------------+------------------+")
    if all_prospective_costs:
        print(f"| PAIR_COST_THRESHOLD       | ${max(all_prospective_costs):.4f}          |")
    if all_final_pair_costs:
        print(f"| Avg Final Pair Cost       | ${statistics.mean(all_final_pair_costs):.4f}          |")
    if all_max_imbalances:
        print(f"| MAX_IMBALANCE_SHARES      | {max(all_max_imbalances):>6.0f} shares   |")
    if all_max_ratios:
        print(f"| MAX_IMBALANCE_RATIO       | {max(all_max_ratios):>6.2f}x          |")
    if all_up_prices:
        print(f"| Avg UP Price              | ${statistics.mean(all_up_prices):.4f}          |")
    if all_down_prices:
        print(f"| Avg DOWN Price            | ${statistics.mean(all_down_prices):.4f}          |")
    print("+---------------------------+------------------+")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Analyze gabagool22's trading patterns")
    parser.add_argument(
        '--random', '-r',
        type=int,
        default=0,
        metavar='N',
        help='Fetch N random past markets (default: use predefined markets)'
    )
    parser.add_argument(
        '--days-back', '-d',
        type=int,
        default=7,
        help='How many days back to search for random markets (default: 7)'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed trade-by-trade analysis'
    )
    parser.add_argument(
        '--query', '-q',
        type=str,
        nargs='+',
        help='Analyze specific market(s) by query string'
    )

    args = parser.parse_args()

    print("="*60)
    print("GABAGOOL22 PAIR COST ANALYSIS")
    print("="*60)
    print(f"Wallet: {WALLET}")
    print("\nNote: SELL trades are ignored (unified orderbook artifacts)")

    # Determine which markets to analyze
    if args.random > 0:
        # Fetch random markets
        print(f"\nMode: Random {args.random} markets from past {args.days_back} days")
        random_markets = fetch_random_gabagool_markets(
            count=args.random,
            days_back=args.days_back,
            verbose=True
        )
        markets_to_analyze = [m['query'] for m in random_markets]
    elif args.query:
        # Use provided queries
        markets_to_analyze = args.query
        print(f"\nMode: Custom queries ({len(markets_to_analyze)} markets)")
    else:
        # Use predefined markets
        markets_to_analyze = MARKETS_TO_ANALYZE
        print(f"\nMode: Predefined markets ({len(markets_to_analyze)} markets)")

    print(f"Markets to analyze: {len(markets_to_analyze)}")

    analyses = []

    for market_query in markets_to_analyze:
        analysis = analyze_market_pair_cost(market_query)
        if analysis:
            analyses.append(analysis)
            print_market_analysis(analysis, verbose=args.verbose or args.random > 0)

    if analyses:
        print_aggregate_analysis(analyses)

        # Hedge timing analysis for each market
        print("\n" + "="*60)
        print("HEDGE TIMING ANALYSIS")
        print("="*60)
        for analysis in analyses:
            analyze_hedge_timing(analysis)
    else:
        print("\nNo markets successfully analyzed.")


if __name__ == "__main__":
    main()
