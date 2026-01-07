#!/usr/bin/env python3
"""
Polywalltrack - Multi-Market Polymarket Wallet Analyzer

Analyzes a wallet's trading performance across multiple markets,
outputting a concise quantitative report.

Usage:
    python scripts/polywalltrack.py -w <wallet_address> -m <market1,market2,...>
    python scripts/polywalltrack.py -w 0x6031b... -m "btc-updown-15m-1766221200,btc-updown-15m-1766222100"
    python scripts/polywalltrack.py -w 0x6031b... -m "Bitcoin Up or Down December 20"

Examples:
    # Analyze specific markets by slug
    python scripts/polywalltrack.py -w 0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d -m "btc-updown-15m-1766221200"

    # Analyze multiple markets
    python scripts/polywalltrack.py -w 0x6031b... -m "btc-updown-15m-1766221200,btc-updown-15m-1766222100"

    # Search by market name
    python scripts/polywalltrack.py -w 0x6031b... -m "Bitcoin Up or Down December 20 4:00AM"
"""

import argparse
import requests
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from datetime import datetime

# API endpoints
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
TRADES_URL = "https://data-api.polymarket.com/trades"
MARKET_URL = "https://gamma-api.polymarket.com/markets"

# Price threshold for inferring resolution
PRICE_RESOLUTION_THRESHOLD = 0.5


@dataclass
class MarketAnalysis:
    """Analysis results for a single market."""
    slug: str
    title: str
    condition_id: str
    trade_count: int
    yes_bought: float
    yes_sold: float
    no_bought: float
    no_sold: float
    yes_cost: float
    no_cost: float
    yes_revenue: float
    no_revenue: float
    net_yes: float  # remaining YES shares
    net_no: float   # remaining NO shares
    net_cost: float  # total cost (exposure)
    winner: Optional[str]  # YES, NO, or None (open)
    pnl: Optional[float]
    resolved: bool


def search_market(query: str) -> Optional[Dict[str, Any]]:
    """Search for a market by name/query. Returns market dict or None."""
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


def get_market_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """Get market info directly by slug."""
    try:
        resp = requests.get(MARKET_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    except requests.RequestException as e:
        print(f"  Error fetching market by slug: {e}")
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


def infer_winner_from_trades(trades: List[Dict]) -> Optional[str]:
    """Infer the winner from the most recent trade's price."""
    if not trades:
        return None

    latest = max(trades, key=lambda t: t.get("timestamp", 0))
    price = float(latest.get("price", 0))
    outcome = latest.get("outcome", "").lower()

    if outcome not in {"up", "down"}:
        return None

    # High price = that side won
    if price >= PRICE_RESOLUTION_THRESHOLD:
        return "YES" if outcome == "up" else "NO"
    else:
        return "NO" if outcome == "up" else "YES"


def get_winner_from_market(market: Dict) -> Optional[str]:
    """Get winner from market's outcomePrices if resolved."""
    outcome_prices = market.get("outcomePrices")
    if not outcome_prices:
        return None

    try:
        import json
        prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
        up_price = float(prices[0])
        down_price = float(prices[1])

        if up_price > 0.99 and down_price < 0.01:
            return "YES"
        elif down_price > 0.99 and up_price < 0.01:
            return "NO"
    except (json.JSONDecodeError, IndexError, ValueError):
        pass

    return None


def analyze_market(wallet: str, market_input: str) -> Optional[MarketAnalysis]:
    """Analyze a single market for the given wallet."""
    # Try to get market info
    market = None

    # First try as slug
    if market_input.startswith("btc-") or "-" in market_input:
        market = get_market_by_slug(market_input)

    # If not found, try search
    if not market:
        market = search_market(market_input)

    if not market:
        print(f"  Market not found: {market_input}")
        return None

    slug = market.get("slug", market_input)
    title = market.get("question") or market.get("title") or slug
    condition_id = market.get("conditionId", "")

    if not condition_id:
        print(f"  No condition ID for market: {slug}")
        return None

    # Fetch trades
    trades = fetch_trades(condition_id, wallet)

    if not trades:
        return MarketAnalysis(
            slug=slug,
            title=title,
            condition_id=condition_id,
            trade_count=0,
            yes_bought=0, yes_sold=0, no_bought=0, no_sold=0,
            yes_cost=0, no_cost=0, yes_revenue=0, no_revenue=0,
            net_yes=0, net_no=0, net_cost=0,
            winner=None, pnl=None, resolved=False
        )

    # Parse trades
    yes_bought = yes_sold = no_bought = no_sold = 0.0
    yes_cost = no_cost = yes_revenue = no_revenue = 0.0

    for trade in trades:
        side = trade.get("side", "BUY").upper()
        outcome = trade.get("outcome", "Up").lower()
        shares = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        cost = shares * price

        is_yes = outcome == "up"
        is_buy = side == "BUY"

        if is_buy:
            if is_yes:
                yes_bought += shares
                yes_cost += cost
            else:
                no_bought += shares
                no_cost += cost
        else:  # SELL
            if is_yes:
                yes_sold += shares
                yes_revenue += cost
            else:
                no_sold += shares
                no_revenue += cost

    net_yes = yes_bought - yes_sold
    net_no = no_bought - no_sold
    net_cost = (yes_cost - yes_revenue) + (no_cost - no_revenue)

    # Determine winner
    winner = get_winner_from_market(market)
    resolved = winner is not None

    if not winner:
        winner = infer_winner_from_trades(trades)
        if winner:
            resolved = True  # Inferred from price

    # Calculate PNL
    pnl = None
    if resolved and winner:
        payout = net_yes if winner == "YES" else net_no
        pnl = payout - net_cost

    return MarketAnalysis(
        slug=slug,
        title=title[:50] + "..." if len(title) > 50 else title,
        condition_id=condition_id,
        trade_count=len(trades),
        yes_bought=yes_bought, yes_sold=yes_sold,
        no_bought=no_bought, no_sold=no_sold,
        yes_cost=yes_cost, no_cost=no_cost,
        yes_revenue=yes_revenue, no_revenue=no_revenue,
        net_yes=net_yes, net_no=net_no, net_cost=net_cost,
        winner=winner, pnl=pnl, resolved=resolved
    )


def generate_report(wallet: str, analyses: List[MarketAnalysis]) -> str:
    """Generate the summary report."""
    lines = []

    # Filter out empty analyses
    valid = [a for a in analyses if a is not None]
    with_trades = [a for a in valid if a.trade_count > 0]
    resolved = [a for a in with_trades if a.resolved and a.pnl is not None]

    # Header
    lines.append("=" * 60)
    lines.append("  POLYWALLTRACK ANALYSIS")
    lines.append("=" * 60)
    lines.append(f"  Wallet: {wallet[:10]}...{wallet[-6:]}")
    lines.append(f"  Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  Markets: {len(valid)} requested, {len(with_trades)} with trades")
    lines.append("")

    if not with_trades:
        lines.append("  No trades found for this wallet in the specified markets.")
        return "\n".join(lines)

    # Calculate aggregates
    total_trades = sum(a.trade_count for a in with_trades)
    total_volume = sum(a.yes_cost + a.no_cost for a in with_trades)
    total_shares = sum(a.yes_bought + a.no_bought for a in with_trades)

    wins = [a for a in resolved if a.pnl and a.pnl > 0]
    losses = [a for a in resolved if a.pnl and a.pnl < 0]

    total_pnl = sum(a.pnl for a in resolved if a.pnl) if resolved else 0
    avg_pnl = total_pnl / len(resolved) if resolved else 0
    max_win = max((a.pnl for a in resolved if a.pnl and a.pnl > 0), default=0)
    max_loss = min((a.pnl for a in resolved if a.pnl and a.pnl < 0), default=0)
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0

    # Summary table
    lines.append("+" + "-" * 35 + "+" + "-" * 14 + "+")
    lines.append(f"| {'METRIC':<33} | {'VALUE':>12} |")
    lines.append("+" + "-" * 35 + "+" + "-" * 14 + "+")
    lines.append(f"| {'Markets Analyzed':<33} | {len(with_trades):>12} |")
    lines.append(f"| {'Markets Resolved':<33} | {len(resolved):>12} |")
    lines.append(f"| {'Markets Won':<33} | {len(wins):>12} |")
    lines.append(f"| {'Markets Lost':<33} | {len(losses):>12} |")
    lines.append(f"| {'Win Rate':<33} | {win_rate:>11.1f}% |")
    lines.append("+" + "-" * 35 + "+" + "-" * 14 + "+")
    lines.append(f"| {'Total PNL':<33} | ${total_pnl:>+11.2f} |")
    lines.append(f"| {'Avg PNL/Market':<33} | ${avg_pnl:>+11.2f} |")
    lines.append(f"| {'Max Win':<33} | ${max_win:>+11.2f} |")
    lines.append(f"| {'Max Loss':<33} | ${max_loss:>+11.2f} |")
    lines.append("+" + "-" * 35 + "+" + "-" * 14 + "+")
    lines.append(f"| {'Total Trades':<33} | {total_trades:>12} |")
    lines.append(f"| {'Total Volume (cost)':<33} | ${total_volume:>11.2f} |")
    lines.append(f"| {'Total Shares Bought':<33} | {total_shares:>12.1f} |")
    if total_trades > 0:
        lines.append(f"| {'Avg Trade Size':<33} | ${total_volume/total_trades:>11.2f} |")
    lines.append("+" + "-" * 35 + "+" + "-" * 14 + "+")

    # Per-market breakdown
    lines.append("")
    lines.append("PER-MARKET BREAKDOWN:")
    lines.append("+" + "-" * 28 + "+" + "-" * 8 + "+" + "-" * 8 + "+" + "-" * 11 + "+" + "-" * 10 + "+")
    lines.append(f"| {'MARKET':<26} | {'TRADES':>6} | {'WINNER':>6} | {'PNL':>9} | {'VOLUME':>8} |")
    lines.append("+" + "-" * 28 + "+" + "-" * 8 + "+" + "-" * 8 + "+" + "-" * 11 + "+" + "-" * 10 + "+")

    for a in sorted(with_trades, key=lambda x: x.slug):
        slug_short = a.slug[-26:] if len(a.slug) > 26 else a.slug
        winner_str = a.winner if a.winner else "OPEN"
        pnl_str = f"${a.pnl:+.2f}" if a.pnl is not None else "---"
        volume = a.yes_cost + a.no_cost

        lines.append(f"| {slug_short:<26} | {a.trade_count:>6} | {winner_str:>6} | {pnl_str:>9} | ${volume:>7.2f} |")

    lines.append("+" + "-" * 28 + "+" + "-" * 8 + "+" + "-" * 8 + "+" + "-" * 11 + "+" + "-" * 10 + "+")

    # Position details for each market
    lines.append("")
    lines.append("POSITION DETAILS:")
    for a in sorted(with_trades, key=lambda x: x.slug):
        lines.append(f"  {a.slug}:")
        lines.append(f"    YES: {a.net_yes:+.2f} shares (bought {a.yes_bought:.2f}, sold {a.yes_sold:.2f})")
        lines.append(f"    NO:  {a.net_no:+.2f} shares (bought {a.no_bought:.2f}, sold {a.no_sold:.2f})")
        lines.append(f"    Net Cost: ${a.net_cost:.2f}")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Polywalltrack - Multi-Market Polymarket Wallet Analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -w 0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d -m "btc-updown-15m-1766221200"
  %(prog)s -w 0x6031b... -m "btc-updown-15m-1766221200,btc-updown-15m-1766222100"
  %(prog)s -w 0x6031b... -m "Bitcoin Up or Down December 20 4:00AM"
        """
    )
    parser.add_argument(
        "-w", "--wallet",
        required=True,
        help="Wallet address to analyze"
    )
    parser.add_argument(
        "-m", "--markets",
        required=True,
        help="Comma-separated market slugs or search queries"
    )
    parser.add_argument(
        "-o", "--output",
        help="Output file (default: stdout)"
    )

    args = parser.parse_args()

    # Parse markets
    market_inputs = [m.strip() for m in args.markets.split(",") if m.strip()]

    if not market_inputs:
        print("Error: No markets specified")
        return

    print(f"\nAnalyzing wallet: {args.wallet[:10]}...{args.wallet[-6:]}")
    print(f"Markets to analyze: {len(market_inputs)}")
    print("-" * 40)

    # Analyze each market
    analyses = []
    for i, market_input in enumerate(market_inputs, 1):
        print(f"[{i}/{len(market_inputs)}] Analyzing: {market_input[:40]}...")
        analysis = analyze_market(args.wallet, market_input)
        analyses.append(analysis)
        if analysis:
            print(f"  Found {analysis.trade_count} trades")

    print("-" * 40)

    # Generate report
    report = generate_report(args.wallet, analyses)

    if args.output:
        with open(args.output, "w") as f:
            f.write(report)
        print(f"Report saved to: {args.output}")
    else:
        print("\n" + report)


if __name__ == "__main__":
    main()
