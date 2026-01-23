#!/usr/bin/env python3
"""
Reverse Engineer Trading Strategy for Wallet 0xa5e8...95f5

Comprehensive multi-stage analysis to determine the exact trading strategy
used by wallet 0xa5e83423126dbc6cdb34f10f37f5d27668ab95f5.

Bot started after January 20, 2026.

Stages:
1. Discovery - Fetch ALL trades
2. Market Classification
3. Strategy Pattern Analysis
4. Advanced Pattern Detection
5. Strategy Hypothesis & Report

Usage:
    python scripts/reverse_engineer_wallet.py
    python scripts/reverse_engineer_wallet.py --start "2026-01-20 00:00" --end "2026-01-23 23:59"
"""

import requests
import json
import time
import csv
import os
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict

# ─── Constants ────────────────────────────────────────────────────────────────

WALLET = "0xa5e83423126dbc6cdb34f10f37f5d27668ab95f5"
START_DATE = datetime(2026, 1, 20, tzinfo=ZoneInfo('UTC'))

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"
MARKET_URL = "https://gamma-api.polymarket.com/markets"
EVENTS_URL = "https://gamma-api.polymarket.com/events"

UTC = ZoneInfo('UTC')
ET = ZoneInfo('America/New_York')

OUTPUT_DIR = "research"
CSV_FILE = f"{OUTPUT_DIR}/wallet_0xa5e8_trades.csv"
REPORT_FILE = f"{OUTPUT_DIR}/WALLET_0xa5e8_STRATEGY_ANALYSIS.md"


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Single trade record."""
    timestamp: float  # unix seconds
    timestamp_dt: datetime
    side: str  # BUY or SELL
    outcome: str  # Up or Down
    price: float
    size: float
    cost: float
    market_slug: str
    condition_id: str
    tx_hash: str
    maker_address: str = ""
    taker_address: str = ""
    is_maker: bool = False


@dataclass
class MarketSummary:
    """Summary of activity in a single market."""
    slug: str
    condition_id: str
    title: str = ""
    market_type: str = ""  # btc-15m, eth-15m, other
    resolution: Optional[str] = None  # YES, NO, None

    # Trades
    trades: List[Trade] = field(default_factory=list)
    total_trades: int = 0
    up_buys: int = 0
    down_buys: int = 0
    up_sells: int = 0
    down_sells: int = 0

    # Position
    net_up_shares: float = 0.0
    net_down_shares: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    up_revenue: float = 0.0
    down_revenue: float = 0.0
    net_cost: float = 0.0

    # Timing
    first_trade_dt: Optional[datetime] = None
    last_trade_dt: Optional[datetime] = None
    market_open_dt: Optional[datetime] = None

    # PnL
    pnl: Optional[float] = None

    # Two-sided
    is_two_sided: bool = False
    pair_cost: Optional[float] = None


# ─── Stage 1: Discovery - Fetch ALL Trades ────────────────────────────────────

def fetch_all_trades_direct() -> List[Dict]:
    """
    Primary approach: Fetch all trades for wallet without market filter.
    The data-api may support querying by user alone.
    """
    print("\n" + "=" * 70)
    print("STAGE 1: DISCOVERY - Fetching All Trades")
    print("=" * 70)

    all_trades = []
    offset = 0
    page_limit = 5000

    print(f"\nAttempting direct fetch (no market filter)...")

    while True:
        params = {
            "user": WALLET,
            "limit": page_limit,
            "offset": offset,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"  Error at offset {offset}: {e}")
            break

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        if not batch:
            break

        all_trades.extend(batch)
        print(f"  Fetched {len(batch)} trades (total: {len(all_trades)}, offset: {offset})")

        if len(batch) < page_limit:
            break
        offset += page_limit
        time.sleep(0.3)

    if all_trades:
        print(f"\n  Direct fetch successful: {len(all_trades)} trades")
        return all_trades

    # Fallback: scan BTC 15-min markets from Jan 20 onwards
    print(f"\n  Direct fetch returned 0 trades. Trying market-by-market scan...")
    return fetch_trades_by_market_scan()


def fetch_trades_by_market_scan() -> List[Dict]:
    """
    Fallback: Scan BTC 15-min markets from Jan 20 to now.
    Markets are every 15 minutes = 96 per day.
    """
    now = datetime.now(UTC)
    start_ts = int(START_DATE.timestamp())
    end_ts = int(now.timestamp())

    # Generate slugs for every 15-min interval
    total_intervals = (end_ts - start_ts) // 900
    print(f"  Scanning {total_intervals} potential BTC 15-min markets...")

    all_trades = []
    markets_with_trades = 0
    markets_scanned = 0

    current_ts = start_ts
    while current_ts < end_ts:
        slug = f"btc-updown-15m-{current_ts}"
        markets_scanned += 1

        if markets_scanned % 50 == 0:
            print(f"  Progress: {markets_scanned}/{total_intervals} markets scanned, "
                  f"{len(all_trades)} trades found in {markets_with_trades} markets")

        # First get condition_id for this market
        trades = fetch_trades_for_slug(slug)
        if trades:
            markets_with_trades += 1
            all_trades.extend(trades)

        current_ts += 900  # 15 minutes
        time.sleep(0.2)  # Rate limiting

    print(f"\n  Market scan complete: {len(all_trades)} trades in {markets_with_trades} markets")
    print(f"  Scanned {markets_scanned} markets total")
    return all_trades


def fetch_trades_for_slug(slug: str) -> List[Dict]:
    """Fetch trades for a specific market slug."""
    # Get condition_id from market info
    try:
        resp = requests.get(EVENTS_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if not data:
            return []

        event = data[0] if isinstance(data, list) else data
        markets = event.get("markets", [])
        if not markets:
            return []

        condition_id = markets[0].get("conditionId", "")
        if not condition_id:
            return []

    except Exception:
        return []

    # Fetch trades for this condition_id
    return fetch_trades_for_condition(condition_id, slug)


def fetch_trades_for_condition(condition_id: str, slug: str = "") -> List[Dict]:
    """Fetch all trades for a specific condition_id."""
    all_trades = []
    offset = 0

    while True:
        params = {
            "market": condition_id,
            "user": WALLET,
            "limit": 1000,
            "offset": offset,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            break

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        if not batch:
            break

        # Tag trades with slug
        for t in batch:
            t['_slug'] = slug

        all_trades.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    return all_trades


def parse_trades(raw_trades: List[Dict]) -> List[Trade]:
    """Parse raw trade dicts into Trade objects."""
    trades = []
    for raw in raw_trades:
        try:
            ts_raw = raw.get("timestamp", 0)
            # Handle both ms and seconds timestamps
            if isinstance(ts_raw, str):
                ts_raw = float(ts_raw)
            ts = ts_raw / 1000 if ts_raw > 1e12 else float(ts_raw)

            trade = Trade(
                timestamp=ts,
                timestamp_dt=datetime.fromtimestamp(ts, tz=UTC),
                side=raw.get("side", "BUY").upper(),
                outcome=raw.get("outcome", "").capitalize(),
                price=float(raw.get("price", 0)),
                size=float(raw.get("size", 0)),
                cost=float(raw.get("price", 0)) * float(raw.get("size", 0)),
                market_slug=raw.get("_slug", raw.get("market", raw.get("conditionId", "unknown"))),
                condition_id=raw.get("market", raw.get("conditionId", "")),
                tx_hash=raw.get("transactionHash", raw.get("txHash", "")),
                maker_address=raw.get("maker", ""),
                taker_address=raw.get("taker", ""),
            )
            # Determine if wallet was maker
            trade.is_maker = trade.maker_address.lower() == WALLET.lower()
            trades.append(trade)
        except (ValueError, TypeError) as e:
            continue

    # Sort by timestamp
    trades.sort(key=lambda t: t.timestamp)
    return trades


# ─── Stage 2: Market Classification ──────────────────────────────────────────

def resolve_condition_id(condition_id: str) -> Optional[Dict]:
    """Look up market info by condition_id via CLOB API."""
    try:
        resp = requests.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        # Normalize CLOB response to match expected format
        result = {
            "question": data.get("question", ""),
            "title": data.get("question", ""),
            "slug": data.get("market_slug", ""),
            "conditionId": data.get("condition_id", condition_id),
            "closed": data.get("closed", False),
            "description": data.get("description", ""),
        }

        # Extract resolution from tokens
        tokens = data.get("tokens", [])
        for token in tokens:
            if token.get("winner"):
                outcome = token.get("outcome", "").lower()
                if outcome in ("up", "yes"):
                    result["winner"] = "Up"
                elif outcome in ("down", "no"):
                    result["winner"] = "Down"
                break

        return result
    except Exception as e:
        return None


def classify_markets(trades: List[Trade]) -> Dict[str, MarketSummary]:
    """Group trades by market and classify market types."""
    print("\n" + "=" * 70)
    print("STAGE 2: MARKET CLASSIFICATION")
    print("=" * 70)

    markets = {}

    for trade in trades:
        key = trade.condition_id or trade.market_slug
        if key not in markets:
            markets[key] = MarketSummary(
                slug=trade.market_slug,
                condition_id=trade.condition_id,
            )
        ms = markets[key]
        ms.trades.append(trade)

    # Resolve condition_ids to market names
    print(f"\n  Resolving {len(markets)} condition_ids to market names...")
    resolved_count = 0
    for key, ms in markets.items():
        market_info = resolve_condition_id(ms.condition_id)
        if market_info:
            ms.title = market_info.get("question", market_info.get("title", ""))
            ms.slug = market_info.get("slug", ms.slug)
            resolved_count += 1

            # Check resolution
            if market_info.get("closed"):
                winner = market_info.get("winner", "")
                if winner.lower() in ("up", "yes"):
                    ms.resolution = "YES"
                elif winner.lower() in ("down", "no"):
                    ms.resolution = "NO"
                else:
                    # Try outcomePrices
                    op = market_info.get("outcomePrices")
                    if op:
                        try:
                            prices = json.loads(op) if isinstance(op, str) else op
                            if float(prices[0]) > 0.99:
                                ms.resolution = "YES"
                            elif float(prices[1]) > 0.99:
                                ms.resolution = "NO"
                        except (json.JSONDecodeError, IndexError, ValueError):
                            pass

        time.sleep(0.2)

    print(f"  Resolved {resolved_count}/{len(markets)} markets")

    # Print market titles
    print(f"\n  MARKETS TRADED:")
    for ms in sorted(markets.values(), key=lambda x: x.trades[0].timestamp if x.trades else 0):
        title_short = ms.title[:60] if ms.title else ms.condition_id[:20] + "..."
        res_str = f" [{ms.resolution}]" if ms.resolution else ""
        print(f"    {ms.trades[0].timestamp_dt.strftime('%m/%d %H:%M') if ms.trades else '???'} | "
              f"{len(ms.trades):2d} trades | {title_short}{res_str}")

    # Classify and compute stats for each market
    for key, ms in markets.items():
        ms.total_trades = len(ms.trades)

        # Classify market type from title/slug
        slug = ms.slug.lower()
        title = ms.title.lower()
        combined = slug + " " + title

        if "btc-updown-15m" in slug or ("btc" in combined and "15" in combined and ("up" in combined or "down" in combined)):
            ms.market_type = "btc-15m"
        elif "eth-updown-15m" in slug:
            ms.market_type = "eth-15m"
        elif "btc" in combined or "bitcoin" in combined:
            ms.market_type = "btc-other"
        elif "eth" in combined or "ethereum" in combined:
            ms.market_type = "eth-other"
        elif "crypto" in combined:
            ms.market_type = "crypto"
        elif "trump" in combined or "politi" in combined or "president" in combined:
            ms.market_type = "politics"
        elif "sport" in combined or "nfl" in combined or "nba" in combined:
            ms.market_type = "sports"
        else:
            ms.market_type = "other"

        # Extract market open time from slug if btc-15m
        if ms.market_type == "btc-15m":
            try:
                ts_str = slug.split("-")[-1]
                ms.market_open_dt = datetime.fromtimestamp(int(ts_str), tz=UTC)
            except (ValueError, IndexError):
                pass

        # Compute position stats
        for t in ms.trades:
            is_up = t.outcome.lower() == "up"
            if t.side == "BUY":
                if is_up:
                    ms.up_buys += 1
                    ms.net_up_shares += t.size
                    ms.up_cost += t.cost
                else:
                    ms.down_buys += 1
                    ms.net_down_shares += t.size
                    ms.down_cost += t.cost
            else:  # SELL
                if is_up:
                    ms.up_sells += 1
                    ms.net_up_shares -= t.size
                    ms.up_revenue += t.cost
                else:
                    ms.down_sells += 1
                    ms.net_down_shares -= t.size
                    ms.down_revenue += t.cost

        ms.net_cost = (ms.up_cost - ms.up_revenue) + (ms.down_cost - ms.down_revenue)

        # Timing
        if ms.trades:
            ms.first_trade_dt = ms.trades[0].timestamp_dt
            ms.last_trade_dt = ms.trades[-1].timestamp_dt

        # Two-sided detection
        ms.is_two_sided = ms.up_buys > 0 and ms.down_buys > 0

        # Pair cost
        if ms.net_up_shares > 0 and ms.net_down_shares > 0:
            avg_up = ms.up_cost / ms.net_up_shares if ms.net_up_shares > 0 else 0
            avg_down = ms.down_cost / ms.net_down_shares if ms.net_down_shares > 0 else 0
            ms.pair_cost = avg_up + avg_down

    # Report
    type_counts = defaultdict(int)
    type_trades = defaultdict(int)
    for ms in markets.values():
        type_counts[ms.market_type] += 1
        type_trades[ms.market_type] += ms.total_trades

    print(f"\n  Total unique markets: {len(markets)}")
    print(f"\n  Market Type Distribution:")
    for mtype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {mtype:15s}: {count:4d} markets, {type_trades[mtype]:6d} trades")

    two_sided = sum(1 for ms in markets.values() if ms.is_two_sided)
    one_sided = len(markets) - two_sided
    print(f"\n  Two-sided markets: {two_sided}")
    print(f"  One-sided markets: {one_sided}")

    return markets


# ─── Stage 3: Strategy Pattern Analysis ──────────────────────────────────────

def analyze_patterns(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """Comprehensive pattern analysis."""
    print("\n" + "=" * 70)
    print("STAGE 3: STRATEGY PATTERN ANALYSIS")
    print("=" * 70)

    analyze_position_structure(markets)
    analyze_entry_timing(trades, markets)
    analyze_grid_structure(trades, markets)
    analyze_position_sizing(trades)
    analyze_exit_strategy(markets)
    analyze_trade_frequency(trades, markets)
    analyze_performance(markets)


def analyze_position_structure(markets: Dict[str, MarketSummary]):
    """3a. Position Structure - Two-sided vs directional."""
    print(f"\n{'─' * 60}")
    print("3a. POSITION STRUCTURE")
    print("─" * 60)

    two_sided = [ms for ms in markets.values() if ms.is_two_sided]
    up_only = [ms for ms in markets.values() if ms.up_buys > 0 and ms.down_buys == 0]
    down_only = [ms for ms in markets.values() if ms.down_buys > 0 and ms.up_buys == 0]

    print(f"\n  Two-sided (both UP and DOWN): {len(two_sided)} markets")
    print(f"  UP-only (directional UP):     {len(up_only)} markets")
    print(f"  DOWN-only (directional DOWN): {len(down_only)} markets")

    # Pair cost analysis for two-sided markets
    pair_costs = [ms.pair_cost for ms in two_sided if ms.pair_cost is not None]
    if pair_costs:
        print(f"\n  Pair Cost Stats (two-sided markets):")
        print(f"    Min:    ${min(pair_costs):.4f}")
        print(f"    Max:    ${max(pair_costs):.4f}")
        print(f"    Mean:   ${statistics.mean(pair_costs):.4f}")
        print(f"    Median: ${statistics.median(pair_costs):.4f}")

        profitable = sum(1 for pc in pair_costs if pc < 1.0)
        print(f"    Profitable (< $1.00): {profitable}/{len(pair_costs)} ({profitable/len(pair_costs)*100:.0f}%)")

    # Imbalance in two-sided markets
    imbalances = []
    for ms in two_sided:
        total = ms.net_up_shares + ms.net_down_shares
        if total > 0:
            imb = abs(ms.net_up_shares - ms.net_down_shares) / total
            imbalances.append(imb)

    if imbalances:
        print(f"\n  Position Imbalance (two-sided):")
        print(f"    Mean:   {statistics.mean(imbalances):.1%}")
        print(f"    Median: {statistics.median(imbalances):.1%}")
        print(f"    Max:    {max(imbalances):.1%}")


def analyze_entry_timing(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """3b. Entry Timing - When does the bot enter relative to market open."""
    print(f"\n{'─' * 60}")
    print("3b. ENTRY TIMING")
    print("─" * 60)

    # Time from market open to first trade (for btc-15m markets)
    entry_delays = []
    for ms in markets.values():
        if ms.market_type == "btc-15m" and ms.market_open_dt and ms.first_trade_dt:
            delay = (ms.first_trade_dt - ms.market_open_dt).total_seconds()
            if 0 <= delay <= 900:  # Within 15 min window
                entry_delays.append(delay)

    if entry_delays:
        print(f"\n  Entry delay from market open (BTC 15m):")
        print(f"    Count:  {len(entry_delays)} markets")
        print(f"    Min:    {min(entry_delays):.1f}s")
        print(f"    Max:    {max(entry_delays):.1f}s")
        print(f"    Mean:   {statistics.mean(entry_delays):.1f}s")
        print(f"    Median: {statistics.median(entry_delays):.1f}s")

        # Distribution buckets
        buckets = {"0-30s": 0, "30-60s": 0, "60-120s": 0, "120-300s": 0, "300s+": 0}
        for d in entry_delays:
            if d < 30:
                buckets["0-30s"] += 1
            elif d < 60:
                buckets["30-60s"] += 1
            elif d < 120:
                buckets["60-120s"] += 1
            elif d < 300:
                buckets["120-300s"] += 1
            else:
                buckets["300s+"] += 1

        print(f"\n    Entry Delay Distribution:")
        for bucket, count in buckets.items():
            bar = "#" * (count * 2)
            print(f"      {bucket:>8s}: {bar} ({count})")
    else:
        print(f"\n  No btc-15m markets with timing data found.")

    # Trade clustering - are entries burst or spread?
    if len(trades) > 10:
        intervals = []
        for i in range(1, len(trades)):
            interval = trades[i].timestamp - trades[i-1].timestamp
            if 0 < interval < 3600:  # Within 1 hour
                intervals.append(interval)

        if intervals:
            print(f"\n  Inter-trade Intervals (all trades):")
            print(f"    Min:    {min(intervals):.3f}s")
            print(f"    Max:    {max(intervals):.1f}s")
            print(f"    Mean:   {statistics.mean(intervals):.2f}s")
            print(f"    Median: {statistics.median(intervals):.2f}s")

            sub_second = sum(1 for i in intervals if i < 1.0)
            sub_5s = sum(1 for i in intervals if i < 5.0)
            print(f"    <1s:  {sub_second}/{len(intervals)} ({sub_second/len(intervals)*100:.1f}%)")
            print(f"    <5s:  {sub_5s}/{len(intervals)} ({sub_5s/len(intervals)*100:.1f}%)")


def analyze_grid_structure(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """3c. Grid Structure - Price levels and spacing."""
    print(f"\n{'─' * 60}")
    print("3c. GRID STRUCTURE")
    print("─" * 60)

    # Collect all buy prices
    up_prices = [t.price for t in trades if t.side == "BUY" and t.outcome.lower() == "up"]
    down_prices = [t.price for t in trades if t.side == "BUY" and t.outcome.lower() == "down"]

    if up_prices:
        up_rounded = sorted(set(round(p, 2) for p in up_prices))
        print(f"\n  UP Buy Prices:")
        print(f"    Unique levels: {len(up_rounded)}")
        print(f"    Range: ${min(up_rounded):.2f} - ${max(up_rounded):.2f}")

        if len(up_rounded) >= 2:
            diffs = [up_rounded[i+1] - up_rounded[i] for i in range(len(up_rounded)-1)]
            print(f"    Grid spacing: min=${min(diffs):.3f}, median=${statistics.median(diffs):.3f}, max=${max(diffs):.3f}")

        # Most used prices
        up_counts = defaultdict(int)
        for p in up_prices:
            up_counts[round(p, 2)] += 1
        top_up = sorted(up_counts.items(), key=lambda x: -x[1])[:10]
        print(f"    Top prices: {', '.join(f'${p:.2f}({c})' for p, c in top_up)}")

    if down_prices:
        down_rounded = sorted(set(round(p, 2) for p in down_prices))
        print(f"\n  DOWN Buy Prices:")
        print(f"    Unique levels: {len(down_rounded)}")
        print(f"    Range: ${min(down_rounded):.2f} - ${max(down_rounded):.2f}")

        if len(down_rounded) >= 2:
            diffs = [down_rounded[i+1] - down_rounded[i] for i in range(len(down_rounded)-1)]
            print(f"    Grid spacing: min=${min(diffs):.3f}, median=${statistics.median(diffs):.3f}, max=${max(diffs):.3f}")

        down_counts = defaultdict(int)
        for p in down_prices:
            down_counts[round(p, 2)] += 1
        top_down = sorted(down_counts.items(), key=lambda x: -x[1])[:10]
        print(f"    Top prices: {', '.join(f'${p:.2f}({c})' for p, c in top_down)}")

    # Complementary pairs (UP + DOWN ~ $1.00)
    if up_prices and down_prices:
        up_set = set(round(p, 2) for p in up_prices)
        down_set = set(round(p, 2) for p in down_prices)

        pairs = []
        for up_p in sorted(up_set):
            for tol in [0.00, 0.01, 0.02]:
                complement = round(1.0 - up_p - tol, 2)
                if complement in down_set:
                    pairs.append((up_p, complement, up_p + complement))
                    break
                complement2 = round(1.0 - up_p + tol, 2)
                if tol > 0 and complement2 in down_set:
                    pairs.append((up_p, complement2, up_p + complement2))
                    break

        print(f"\n  Complementary Pairs (UP + DOWN ~ $1.00): {len(pairs)} found")
        if pairs:
            pair_sums = [s for _, _, s in pairs]
            print(f"    Sum range: ${min(pair_sums):.3f} - ${max(pair_sums):.3f}")
            print(f"    Mean sum:  ${statistics.mean(pair_sums):.3f}")


def analyze_position_sizing(trades: List[Trade]):
    """3d. Position Sizing."""
    print(f"\n{'─' * 60}")
    print("3d. POSITION SIZING")
    print("─" * 60)

    buy_sizes = [t.size for t in trades if t.side == "BUY"]
    sell_sizes = [t.size for t in trades if t.side == "SELL"]

    if buy_sizes:
        print(f"\n  Buy Order Sizes:")
        print(f"    Count:  {len(buy_sizes)}")
        print(f"    Min:    {min(buy_sizes):.2f}")
        print(f"    Max:    {max(buy_sizes):.2f}")
        print(f"    Mean:   {statistics.mean(buy_sizes):.2f}")
        print(f"    Median: {statistics.median(buy_sizes):.2f}")
        if len(buy_sizes) > 1:
            print(f"    Stdev:  {statistics.stdev(buy_sizes):.2f}")

        # Size distribution
        size_counts = defaultdict(int)
        for s in buy_sizes:
            size_counts[round(s)] += 1
        top_sizes = sorted(size_counts.items(), key=lambda x: -x[1])[:5]
        print(f"    Most common: {', '.join(f'~{s}({c})' for s, c in top_sizes)}")

    if sell_sizes:
        print(f"\n  Sell Order Sizes:")
        print(f"    Count:  {len(sell_sizes)}")
        print(f"    Min:    {min(sell_sizes):.2f}")
        print(f"    Max:    {max(sell_sizes):.2f}")
        print(f"    Mean:   {statistics.mean(sell_sizes):.2f}")
        print(f"    Median: {statistics.median(sell_sizes):.2f}")

    # Cost per trade
    buy_costs = [t.cost for t in trades if t.side == "BUY"]
    if buy_costs:
        print(f"\n  Cost per Buy Trade:")
        print(f"    Mean:   ${statistics.mean(buy_costs):.2f}")
        print(f"    Median: ${statistics.median(buy_costs):.2f}")
        print(f"    Total:  ${sum(buy_costs):.2f}")


def analyze_exit_strategy(markets: Dict[str, MarketSummary]):
    """3e. Exit Strategy - Hold to resolution or sell before."""
    print(f"\n{'─' * 60}")
    print("3e. EXIT STRATEGY")
    print("─" * 60)

    total_markets = len(markets)
    markets_with_sells = sum(1 for ms in markets.values() if ms.up_sells > 0 or ms.down_sells > 0)
    markets_buy_only = total_markets - markets_with_sells

    total_sells = sum(ms.up_sells + ms.down_sells for ms in markets.values())
    total_buys = sum(ms.up_buys + ms.down_buys for ms in markets.values())

    print(f"\n  Markets with sells: {markets_with_sells}/{total_markets} ({markets_with_sells/total_markets*100:.0f}%)")
    print(f"  Markets buy-only:  {markets_buy_only}/{total_markets} ({markets_buy_only/total_markets*100:.0f}%)")
    print(f"  Total buys:  {total_buys}")
    print(f"  Total sells: {total_sells}")
    print(f"  Sell/Buy ratio: {total_sells/total_buys:.2f}" if total_buys > 0 else "")

    if markets_with_sells > 0:
        print(f"\n  -> Bot sells before resolution in some markets (possible stop-loss or hedging)")
    else:
        print(f"\n  -> Bot holds ALL positions to resolution (pure directional/MM strategy)")


def analyze_trade_frequency(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """3f. Trade Frequency."""
    print(f"\n{'─' * 60}")
    print("3f. TRADE FREQUENCY")
    print("─" * 60)

    if not trades:
        return

    # Overall stats
    time_span = trades[-1].timestamp - trades[0].timestamp
    hours = time_span / 3600

    print(f"\n  Time span: {hours:.1f} hours ({time_span/86400:.1f} days)")
    print(f"  Total trades: {len(trades)}")
    if hours > 0:
        print(f"  Trades/hour: {len(trades)/hours:.1f}")
        print(f"  Trades/day:  {len(trades)/(hours/24):.1f}")

    # Trades per market
    trades_per_market = [ms.total_trades for ms in markets.values()]
    if trades_per_market:
        print(f"\n  Trades per Market:")
        print(f"    Min:    {min(trades_per_market)}")
        print(f"    Max:    {max(trades_per_market)}")
        print(f"    Mean:   {statistics.mean(trades_per_market):.1f}")
        print(f"    Median: {statistics.median(trades_per_market):.1f}")

    # Active hours analysis
    hour_counts = defaultdict(int)
    for t in trades:
        hour_counts[t.timestamp_dt.hour] += 1

    print(f"\n  Active Hours (UTC):")
    for h in sorted(hour_counts.keys()):
        count = hour_counts[h]
        bar = "#" * (count // max(1, max(hour_counts.values()) // 30))
        print(f"    {h:02d}:00  {bar} ({count})")


def analyze_performance(markets: Dict[str, MarketSummary]):
    """3g. Performance - Win rate and PnL."""
    print(f"\n{'─' * 60}")
    print("3g. PERFORMANCE")
    print("─" * 60)

    # Use resolution data already fetched in classify_markets
    resolved_markets = []
    for ms in markets.values():
        if ms.resolution:
            # Calculate PnL based on resolution
            if ms.resolution == "YES":
                # YES won: UP shares pay $1, DOWN shares worth $0
                ms.pnl = ms.net_up_shares - ms.net_cost
            else:
                # NO won: DOWN shares pay $1, UP shares worth $0
                ms.pnl = ms.net_down_shares - ms.net_cost
            resolved_markets.append(ms)

    if resolved_markets:
        wins = [ms for ms in resolved_markets if ms.pnl and ms.pnl > 0]
        losses = [ms for ms in resolved_markets if ms.pnl and ms.pnl < 0]
        breakeven = [ms for ms in resolved_markets if ms.pnl is not None and ms.pnl == 0]

        total_pnl = sum(ms.pnl for ms in resolved_markets if ms.pnl is not None)
        win_pnls = [ms.pnl for ms in wins]
        loss_pnls = [ms.pnl for ms in losses]

        print(f"\n  Resolved Markets: {len(resolved_markets)}")
        print(f"  Wins:       {len(wins)}")
        print(f"  Losses:     {len(losses)}")
        print(f"  Breakeven:  {len(breakeven)}")
        print(f"  Win Rate:   {len(wins)/len(resolved_markets)*100:.1f}%")
        print(f"\n  Total PnL:  ${total_pnl:.2f}")
        print(f"  Avg PnL:    ${total_pnl/len(resolved_markets):.2f}/market")

        if win_pnls:
            print(f"  Avg Win:    ${statistics.mean(win_pnls):.2f}")
            print(f"  Max Win:    ${max(win_pnls):.2f}")
        if loss_pnls:
            print(f"  Avg Loss:   ${statistics.mean(loss_pnls):.2f}")
            print(f"  Max Loss:   ${min(loss_pnls):.2f}")

        # PnL per hour
        if trades := list(resolved_markets[0].trades if resolved_markets else []):
            pass
        time_span_h = sum(
            (ms.last_trade_dt - ms.first_trade_dt).total_seconds()
            for ms in resolved_markets
            if ms.first_trade_dt and ms.last_trade_dt
        ) / 3600
        if time_span_h > 0:
            print(f"\n  PnL/hour (active): ${total_pnl/time_span_h:.2f}")
    else:
        print(f"\n  No resolved markets found (resolution data unavailable)")
        print(f"  Attempting PnL estimation from net cost...")

        # Estimate: if pair cost < 1.0 consistently, bot is profitable
        pair_costs = [ms.pair_cost for ms in markets.values() if ms.pair_cost is not None]
        if pair_costs:
            avg_pc = statistics.mean(pair_costs)
            total_shares = sum(min(ms.net_up_shares, ms.net_down_shares) for ms in markets.values())
            estimated_profit_per_share = 1.0 - avg_pc if avg_pc < 1.0 else avg_pc - 1.0
            estimated_pnl = estimated_profit_per_share * total_shares
            print(f"  Avg pair cost: ${avg_pc:.4f}")
            print(f"  Estimated profit/share: ${estimated_profit_per_share:.4f}")
            print(f"  Total paired shares: {total_shares:.0f}")
            print(f"  Estimated total PnL: ${estimated_pnl:.2f}")


# ─── Stage 4: Advanced Pattern Detection ─────────────────────────────────────

def detect_advanced_patterns(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """Advanced pattern detection."""
    print("\n" + "=" * 70)
    print("STAGE 4: ADVANCED PATTERN DETECTION")
    print("=" * 70)

    detect_contrarian_pattern(trades, markets)
    detect_trade_bursts(trades)
    detect_maker_taker(trades)
    detect_cycling_behavior(markets)
    detect_timing_patterns(trades, markets)


def detect_contrarian_pattern(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """Analyze if bot buys the cheap/losing side (contrarian/mean-reversion)."""
    print(f"\n{'─' * 60}")
    print("4e. CONTRARIAN / MEAN-REVERSION ANALYSIS")
    print("─" * 60)

    # For each market: what side did the bot buy, and what won?
    up_bought_up_won = 0
    up_bought_down_won = 0
    down_bought_up_won = 0
    down_bought_down_won = 0
    up_bought_markets = []
    down_bought_markets = []

    for ms in markets.values():
        if not ms.resolution:
            continue

        # Determine which side was bought
        if ms.up_buys > 0 and ms.down_buys == 0:
            side_bought = "UP"
        elif ms.down_buys > 0 and ms.up_buys == 0:
            side_bought = "DOWN"
        else:
            continue  # Skip two-sided

        won = ms.resolution == "YES"  # YES = UP won

        if side_bought == "UP":
            avg_price = ms.up_cost / ms.net_up_shares if ms.net_up_shares > 0 else 0
            up_bought_markets.append((ms, avg_price, won))
            if won:
                up_bought_up_won += 1
            else:
                up_bought_down_won += 1
        else:
            avg_price = ms.down_cost / ms.net_down_shares if ms.net_down_shares > 0 else 0
            down_bought_markets.append((ms, avg_price, won))
            if won:
                down_bought_up_won += 1
            else:
                down_bought_down_won += 1

    total_up = up_bought_up_won + up_bought_down_won
    total_down = down_bought_up_won + down_bought_down_won

    print(f"\n  When bot buys UP ({total_up} markets):")
    print(f"    UP won (correct):   {up_bought_up_won} ({up_bought_up_won/max(1,total_up)*100:.0f}%)")
    print(f"    DOWN won (wrong):   {up_bought_down_won} ({up_bought_down_won/max(1,total_up)*100:.0f}%)")
    if up_bought_markets:
        prices = [p for _, p, _ in up_bought_markets]
        print(f"    Avg entry price:    ${statistics.mean(prices):.3f}")

    print(f"\n  When bot buys DOWN ({total_down} markets):")
    print(f"    DOWN won (correct): {down_bought_down_won} ({down_bought_down_won/max(1,total_down)*100:.0f}%)")
    print(f"    UP won (wrong):     {down_bought_up_won} ({down_bought_up_won/max(1,total_down)*100:.0f}%)")
    if down_bought_markets:
        prices = [p for _, p, _ in down_bought_markets]
        print(f"    Avg entry price:    ${statistics.mean(prices):.3f}")

    # Entry timing vs side
    print(f"\n  Entry Timing by Side Bought:")
    for side_name, side_markets in [("UP", up_bought_markets), ("DOWN", down_bought_markets)]:
        delays = []
        for ms_item, _, _ in side_markets:
            ms = ms_item
            if ms.market_open_dt and ms.first_trade_dt:
                d = (ms.first_trade_dt - ms.market_open_dt).total_seconds()
                if 0 <= d <= 900:
                    delays.append(d)
        if delays:
            print(f"    {side_name}: avg {statistics.mean(delays):.0f}s, median {statistics.median(delays):.0f}s delay")

    # Price at entry vs outcome
    print(f"\n  Entry Price vs Win/Loss:")
    win_prices = [p for _, p, won in up_bought_markets + down_bought_markets if won]
    loss_prices = [p for _, p, won in up_bought_markets + down_bought_markets if not won]

    if win_prices:
        print(f"    Winning trades avg price: ${statistics.mean(win_prices):.3f}")
    if loss_prices:
        print(f"    Losing trades avg price:  ${statistics.mean(loss_prices):.3f}")

    # Capital deployed per trade
    print(f"\n  Capital per Market:")
    for side_name, side_markets in [("UP buyers", up_bought_markets), ("DOWN buyers", down_bought_markets)]:
        costs = []
        for ms_item, _, _ in side_markets:
            ms = ms_item
            costs.append(ms.up_cost + ms.down_cost)
        if costs:
            print(f"    {side_name}: avg ${statistics.mean(costs):.2f}, total ${sum(costs):.2f}")

    # Determine if contrarian
    all_prices = [p for _, p, _ in up_bought_markets + down_bought_markets]
    avg_entry = statistics.mean(all_prices) if all_prices else 0.5

    if avg_entry < 0.35:
        print(f"\n  CONCLUSION: Bot buys the CHEAP side (avg ${avg_entry:.2f})")
        print(f"  -> This is a CONTRARIAN/MEAN-REVERSION strategy")
        print(f"  -> Bot buys after price has moved against this side")
        print(f"  -> Bets that BTC will reverse direction before market closes")
    elif avg_entry > 0.55:
        print(f"\n  CONCLUSION: Bot buys the EXPENSIVE side (avg ${avg_entry:.2f})")
        print(f"  -> This is a MOMENTUM strategy")
        print(f"  -> Bot buys the direction BTC is already moving")
    else:
        print(f"\n  CONCLUSION: Mixed entry prices (avg ${avg_entry:.2f})")
        print(f"  -> Strategy may be based on other signals")


def detect_trade_bursts(trades: List[Trade]):
    """4a. Trade Burst Analysis - Find trades within 100ms of each other."""
    print(f"\n{'─' * 60}")
    print("4a. TRADE BURST ANALYSIS (100ms windows)")
    print("─" * 60)

    if len(trades) < 2:
        print("  Not enough trades for burst analysis")
        return

    # Group by market
    by_market = defaultdict(list)
    for t in trades:
        by_market[t.condition_id or t.market_slug].append(t)

    total_bursts = 0
    burst_sizes = []
    burst_pair_costs = []

    for market_id, mtrades in by_market.items():
        if len(mtrades) < 2:
            continue

        mtrades.sort(key=lambda t: t.timestamp)

        # Find bursts (within 100ms)
        bursts = []
        current_burst = [mtrades[0]]

        for t in mtrades[1:]:
            if t.timestamp - current_burst[-1].timestamp < 0.1:
                current_burst.append(t)
            else:
                if len(current_burst) > 1:
                    bursts.append(current_burst)
                current_burst = [t]

        if len(current_burst) > 1:
            bursts.append(current_burst)

        total_bursts += len(bursts)

        for burst in bursts:
            burst_sizes.append(len(burst))

            up_in_burst = [t for t in burst if t.outcome.lower() == "up"]
            down_in_burst = [t for t in burst if t.outcome.lower() == "down"]

            if up_in_burst and down_in_burst:
                up_cost = sum(t.cost for t in up_in_burst)
                down_cost = sum(t.cost for t in down_in_burst)
                up_shares = sum(t.size for t in up_in_burst)
                down_shares = sum(t.size for t in down_in_burst)
                if up_shares > 0 and down_shares > 0:
                    pc = (up_cost / up_shares) + (down_cost / down_shares)
                    burst_pair_costs.append(pc)

    print(f"\n  Total bursts found: {total_bursts}")

    if burst_sizes:
        print(f"  Burst sizes: min={min(burst_sizes)}, max={max(burst_sizes)}, "
              f"mean={statistics.mean(burst_sizes):.1f}")

    if burst_pair_costs:
        print(f"\n  Burst Pair Costs (simultaneous UP+DOWN fills):")
        print(f"    Count:  {len(burst_pair_costs)}")
        print(f"    Min:    ${min(burst_pair_costs):.4f}")
        print(f"    Max:    ${max(burst_pair_costs):.4f}")
        print(f"    Mean:   ${statistics.mean(burst_pair_costs):.4f}")
        print(f"    Median: ${statistics.median(burst_pair_costs):.4f}")
        print(f"\n    -> Bursts with both sides indicate PRE-POSTED grid orders")


def detect_maker_taker(trades: List[Trade]):
    """4c. Order Type Detection - Maker vs Taker."""
    print(f"\n{'─' * 60}")
    print("4c. MAKER vs TAKER ANALYSIS")
    print("─" * 60)

    makers = [t for t in trades if t.is_maker]
    takers = [t for t in trades if not t.is_maker and t.maker_address]  # Only count if we have data
    unknown = [t for t in trades if not t.maker_address and not t.taker_address]

    total_known = len(makers) + len(takers)

    print(f"\n  Maker trades: {len(makers)}")
    print(f"  Taker trades: {len(takers)}")
    print(f"  Unknown:      {len(unknown)}")

    if total_known > 0:
        print(f"  Maker %:      {len(makers)/total_known*100:.1f}%")
        print(f"\n  -> {'MAKER-dominated' if len(makers) > len(takers) else 'TAKER-dominated'} strategy")
        if len(makers) > total_known * 0.7:
            print(f"     Bot primarily POSTS limit orders (passive market making)")
        elif len(takers) > total_known * 0.7:
            print(f"     Bot primarily TAKES from orderbook (aggressive/momentum)")
        else:
            print(f"     Bot uses a MIX of maker and taker orders")


def detect_cycling_behavior(markets: Dict[str, MarketSummary]):
    """4d. Cycling Behavior - Multiple entries in same market."""
    print(f"\n{'─' * 60}")
    print("4d. CYCLING BEHAVIOR")
    print("─" * 60)

    # Check if any market has both buys and sells suggesting cycling
    cycling_markets = []
    for ms in markets.values():
        total_sells = ms.up_sells + ms.down_sells
        total_buys = ms.up_buys + ms.down_buys
        if total_sells > 0 and total_buys > total_sells:
            cycling_markets.append(ms)

    print(f"\n  Markets with sell+rebuy (cycling): {len(cycling_markets)}/{len(markets)}")

    if cycling_markets:
        for ms in cycling_markets[:5]:
            print(f"    {ms.slug}: {ms.up_buys+ms.down_buys} buys, {ms.up_sells+ms.down_sells} sells")
    else:
        print(f"  -> Bot does NOT cycle within markets (single entry, hold to resolution)")


def detect_timing_patterns(trades: List[Trade], markets: Dict[str, MarketSummary]):
    """4b. Timing patterns - velocity vs consistent."""
    print(f"\n{'─' * 60}")
    print("4b. TIMING PATTERN ANALYSIS")
    print("─" * 60)

    # Per-market interval analysis
    cv_values = []
    for ms in markets.values():
        if len(ms.trades) < 3:
            continue

        intervals = []
        for i in range(1, len(ms.trades)):
            interval = ms.trades[i].timestamp - ms.trades[i-1].timestamp
            if 0 < interval < 900:  # Within 15 min
                intervals.append(interval)

        if len(intervals) >= 2:
            mean_int = statistics.mean(intervals)
            if mean_int > 0:
                cv = statistics.stdev(intervals) / mean_int
                cv_values.append(cv)

    if cv_values:
        avg_cv = statistics.mean(cv_values)
        print(f"\n  Coefficient of Variation (per market):")
        print(f"    Mean CV:   {avg_cv:.2f}")
        print(f"    Median CV: {statistics.median(cv_values):.2f}")
        print(f"    (CV < 0.5 = consistent timing, CV > 1.0 = bursty/velocity-based)")

        if avg_cv < 0.5:
            print(f"\n    -> CONSISTENT timing (grid/scheduled posting)")
        elif avg_cv > 1.0:
            print(f"\n    -> BURSTY timing (velocity/signal-based entries)")
        else:
            print(f"\n    -> MODERATE variability (mixed pattern)")

    # Check for specific time-of-day patterns
    # Group first trades by minute-of-hour
    first_trade_minutes = []
    for ms in markets.values():
        if ms.first_trade_dt:
            first_trade_minutes.append(ms.first_trade_dt.minute)

    if first_trade_minutes:
        min_counts = defaultdict(int)
        for m in first_trade_minutes:
            min_counts[m] += 1

        top_minutes = sorted(min_counts.items(), key=lambda x: -x[1])[:5]
        print(f"\n  Most common first-trade minutes: {top_minutes}")


# ─── Stage 5: Strategy Hypothesis & Report ───────────────────────────────────

def generate_report(trades: List[Trade], markets: Dict[str, MarketSummary]) -> str:
    """Generate final strategy hypothesis and markdown report."""
    print("\n" + "=" * 70)
    print("STAGE 5: STRATEGY HYPOTHESIS")
    print("=" * 70)

    # Determine strategy type
    total_markets = len(markets)
    two_sided = sum(1 for ms in markets.values() if ms.is_two_sided)
    total_buys = sum(ms.up_buys + ms.down_buys for ms in markets.values())
    total_sells = sum(ms.up_sells + ms.down_sells for ms in markets.values())
    btc_15m = sum(1 for ms in markets.values() if ms.market_type == "btc-15m")

    pair_costs = [ms.pair_cost for ms in markets.values() if ms.pair_cost is not None]
    avg_pair_cost = statistics.mean(pair_costs) if pair_costs else None

    buy_sizes = [t.size for t in trades if t.side == "BUY"]
    avg_size = statistics.mean(buy_sizes) if buy_sizes else 0

    # Compute entry timing stats for strategy detection
    entry_delays = []
    for ms in markets.values():
        if ms.market_type == "btc-15m" and ms.market_open_dt and ms.first_trade_dt:
            delay = (ms.first_trade_dt - ms.market_open_dt).total_seconds()
            if 0 <= delay <= 900:
                entry_delays.append(delay)
    avg_delay = statistics.mean(entry_delays) if entry_delays else 0
    late_entries = sum(1 for d in entry_delays if d > 300) if entry_delays else 0
    late_pct = late_entries / len(entry_delays) * 100 if entry_delays else 0

    # Compute avg buy price (how cheap are entries?)
    buy_prices = [t.price for t in trades if t.side == "BUY"]
    avg_buy_price = statistics.mean(buy_prices) if buy_prices else 0.5

    # Compute resolved PnL stats
    resolved = [ms for ms in markets.values() if ms.pnl is not None]
    wins = [ms for ms in resolved if ms.pnl > 0]
    losses = [ms for ms in resolved if ms.pnl < 0]
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0
    avg_win = statistics.mean([ms.pnl for ms in wins]) if wins else 0
    avg_loss = statistics.mean([ms.pnl for ms in losses]) if losses else 0

    # Strategy classification
    strategy_type = "UNKNOWN"
    strategy_details = []

    if two_sided / total_markets > 0.7 and avg_pair_cost and avg_pair_cost < 1.05:
        strategy_type = "GRID MARKET MAKER"
        strategy_details = [
            "Two-sided posting on both UP and DOWN",
            f"Pair cost target: ~${avg_pair_cost:.4f}",
            "Profits from pair cost < $1.00",
            "Pre-posts grid orders at multiple price levels",
        ]
    elif two_sided / total_markets < 0.3 and avg_buy_price < 0.40 and avg_delay > 200:
        strategy_type = "CONTRARIAN / MEAN-REVERSION"
        strategy_details = [
            f"Directional positions (98% one-sided, {total_markets - two_sided}/{total_markets} markets)",
            f"Buys cheap side at avg ${avg_buy_price:.2f} (betting on reversal)",
            f"Waits {avg_delay:.0f}s avg ({late_pct:.0f}% entries after 5 min) for price to establish direction",
            f"Then bets AGAINST current direction (mean reversion)",
            f"Holds to resolution (sell/buy ratio: {total_sells/max(1,total_buys):.2f})",
            f"Win rate: {win_rate:.1f}% with {avg_win/max(1,abs(avg_loss)):.1f}:1 reward/risk ratio",
            f"Asymmetric payoff: buys at ${avg_buy_price:.2f}, wins pay $1.00 (${1-avg_buy_price:.2f} profit)",
        ]
    elif two_sided / total_markets < 0.3 and avg_buy_price >= 0.40:
        strategy_type = "DIRECTIONAL / MOMENTUM"
        strategy_details = [
            "Primarily one-sided positions",
            f"Buys expensive side (avg ${avg_buy_price:.2f}), following momentum",
            "Holds to resolution",
        ]
    elif total_sells / max(1, total_buys) > 0.3:
        strategy_type = "ACTIVE TRADER"
        strategy_details = [
            "Buys and sells within markets",
            "Possible momentum or mean-reversion",
        ]
    else:
        strategy_type = "HYBRID"
        strategy_details = [
            "Mix of two-sided and directional",
            "May adapt strategy to market conditions",
        ]

    # Print summary
    print(f"\n  STRATEGY TYPE: {strategy_type}")
    for detail in strategy_details:
        print(f"    - {detail}")

    print(f"\n  KEY METRICS:")
    print(f"    Markets traded:    {total_markets}")
    print(f"    BTC 15-min:        {btc_15m}")
    print(f"    Two-sided:         {two_sided}/{total_markets} ({two_sided/total_markets*100:.0f}%)")
    print(f"    Total trades:      {len(trades)}")
    print(f"    Avg size:          {avg_size:.1f} shares")
    print(f"    Sell/Buy ratio:    {total_sells/max(1,total_buys):.2f}")
    if avg_pair_cost:
        print(f"    Avg pair cost:     ${avg_pair_cost:.4f}")

    # Generate markdown report
    report = generate_markdown_report(trades, markets, strategy_type, strategy_details)
    return report


def generate_markdown_report(
    trades: List[Trade],
    markets: Dict[str, MarketSummary],
    strategy_type: str,
    strategy_details: List[str]
) -> str:
    """Generate the markdown report file."""

    total_markets = len(markets)
    two_sided = sum(1 for ms in markets.values() if ms.is_two_sided)
    btc_15m = sum(1 for ms in markets.values() if ms.market_type == "btc-15m")
    total_buys = sum(ms.up_buys + ms.down_buys for ms in markets.values())
    total_sells = sum(ms.up_sells + ms.down_sells for ms in markets.values())

    pair_costs = [ms.pair_cost for ms in markets.values() if ms.pair_cost is not None]
    avg_pair_cost = statistics.mean(pair_costs) if pair_costs else None

    buy_sizes = [t.size for t in trades if t.side == "BUY"]
    avg_size = statistics.mean(buy_sizes) if buy_sizes else 0

    up_prices = [t.price for t in trades if t.side == "BUY" and t.outcome.lower() == "up"]
    down_prices = [t.price for t in trades if t.side == "BUY" and t.outcome.lower() == "down"]

    time_span = (trades[-1].timestamp - trades[0].timestamp) / 3600 if len(trades) > 1 else 0

    lines = [
        f"# Strategy Analysis: Wallet 0xa5e8...95f5",
        f"",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"**Wallet:** `{WALLET}`",
        f"**Analysis Period:** {trades[0].timestamp_dt.strftime('%Y-%m-%d %H:%M')} to {trades[-1].timestamp_dt.strftime('%Y-%m-%d %H:%M')} UTC" if trades else "",
        f"",
        f"---",
        f"",
        f"## Strategy Classification: **{strategy_type}**",
        f"",
    ]

    for detail in strategy_details:
        lines.append(f"- {detail}")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## Key Metrics",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Markets | {total_markets} |",
        f"| BTC 15-min Markets | {btc_15m} |",
        f"| Two-sided Markets | {two_sided} ({two_sided/max(1,total_markets)*100:.0f}%) |",
        f"| Total Trades | {len(trades)} |",
        f"| Total Buys | {total_buys} |",
        f"| Total Sells | {total_sells} |",
        f"| Avg Order Size | {avg_size:.1f} shares |",
        f"| Time Span | {time_span:.1f} hours |",
        f"| Trades/Hour | {len(trades)/max(0.1,time_span):.1f} |",
    ])

    if avg_pair_cost:
        lines.append(f"| Avg Pair Cost | ${avg_pair_cost:.4f} |")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## Position Structure",
        f"",
    ])

    if two_sided / max(1, total_markets) > 0.5:
        lines.append(f"**Two-sided market maker** - buys both UP and DOWN in {two_sided}/{total_markets} markets.")
    else:
        lines.append(f"**Primarily directional** - one-sided in {total_markets - two_sided}/{total_markets} markets.")

    if pair_costs:
        lines.extend([
            f"",
            f"### Pair Costs",
            f"- Min: ${min(pair_costs):.4f}",
            f"- Max: ${max(pair_costs):.4f}",
            f"- Mean: ${statistics.mean(pair_costs):.4f}",
            f"- Median: ${statistics.median(pair_costs):.4f}",
            f"- Profitable (< $1.00): {sum(1 for p in pair_costs if p < 1.0)}/{len(pair_costs)}",
        ])

    lines.extend([
        f"",
        f"---",
        f"",
        f"## Price Levels",
        f"",
    ])

    if up_prices:
        lines.extend([
            f"### UP Prices",
            f"- Range: ${min(up_prices):.2f} - ${max(up_prices):.2f}",
            f"- Mean: ${statistics.mean(up_prices):.2f}",
            f"- Unique levels: {len(set(round(p,2) for p in up_prices))}",
            f"",
        ])

    if down_prices:
        lines.extend([
            f"### DOWN Prices",
            f"- Range: ${min(down_prices):.2f} - ${max(down_prices):.2f}",
            f"- Mean: ${statistics.mean(down_prices):.2f}",
            f"- Unique levels: {len(set(round(p,2) for p in down_prices))}",
            f"",
        ])

    lines.extend([
        f"---",
        f"",
        f"## Exit Strategy",
        f"",
        f"- Sell/Buy ratio: {total_sells/max(1,total_buys):.2f}",
    ])

    if total_sells == 0:
        lines.append(f"- **Holds ALL positions to resolution** (no pre-resolution exits)")
    else:
        lines.append(f"- Exits some positions before resolution ({total_sells} sells)")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## Market Types Traded",
        f"",
    ])

    type_counts = defaultdict(int)
    for ms in markets.values():
        type_counts[ms.market_type] += 1

    for mtype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        lines.append(f"- **{mtype}**: {count} markets")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## Raw Data",
        f"",
        f"- Trade CSV: `{CSV_FILE}`",
        f"- Total records: {len(trades)}",
        f"",
    ])

    return "\n".join(lines)


# ─── Export Functions ─────────────────────────────────────────────────────────

def export_trades_csv(trades: List[Trade]):
    """Export all trades to CSV."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(CSV_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'timestamp', 'datetime', 'market_slug', 'condition_id',
            'side', 'outcome', 'price', 'size', 'cost',
            'is_maker', 'tx_hash'
        ])
        for t in trades:
            writer.writerow([
                t.timestamp,
                t.timestamp_dt.isoformat(),
                t.market_slug,
                t.condition_id,
                t.side,
                t.outcome,
                t.price,
                t.size,
                t.cost,
                t.is_maker,
                t.tx_hash,
            ])

    print(f"\n  Exported {len(trades)} trades to {CSV_FILE}")


def export_report(report: str):
    """Save markdown report."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(REPORT_FILE, 'w') as f:
        f.write(report)

    print(f"  Saved report to {REPORT_FILE}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Reverse Engineer Wallet Trading Strategy")
    parser.add_argument('--start', type=str, help='Start time "YYYY-MM-DD HH:MM" UTC')
    parser.add_argument('--end', type=str, help='End time "YYYY-MM-DD HH:MM" UTC')
    parser.add_argument('--skip-resolution', action='store_true',
                        help='Skip fetching resolution data (faster)')
    args = parser.parse_args()

    global START_DATE
    if args.start:
        START_DATE = datetime.strptime(args.start, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)

    print("=" * 70)
    print("  REVERSE ENGINEER TRADING STRATEGY")
    print(f"  Wallet: {WALLET}")
    print(f"  Start:  {START_DATE.strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    # Stage 1: Fetch all trades
    raw_trades = fetch_all_trades_direct()

    if not raw_trades:
        print("\nERROR: No trades found for this wallet. Exiting.")
        return

    # Parse into Trade objects
    trades = parse_trades(raw_trades)
    print(f"\n  Parsed {len(trades)} valid trades")

    # Filter to trades after START_DATE
    trades = [t for t in trades if t.timestamp_dt >= START_DATE]
    print(f"  After filtering to >= {START_DATE.strftime('%Y-%m-%d')}: {len(trades)} trades")

    if not trades:
        print("\nNo trades found after start date. Exiting.")
        return

    # Stage 2: Classify markets
    markets = classify_markets(trades)

    # Stage 3: Strategy patterns
    analyze_patterns(trades, markets)

    # Stage 4: Advanced patterns
    detect_advanced_patterns(trades, markets)

    # Stage 5: Generate report
    report = generate_report(trades, markets)

    # Export
    print("\n" + "=" * 70)
    print("EXPORTING DATA")
    print("=" * 70)
    export_trades_csv(trades)
    export_report(report)

    print("\n" + "=" * 70)
    print("ANALYSIS COMPLETE")
    print("=" * 70)
    print(f"  Trades CSV: {CSV_FILE}")
    print(f"  Report:     {REPORT_FILE}")


if __name__ == "__main__":
    main()
