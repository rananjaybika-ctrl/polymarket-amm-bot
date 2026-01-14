#!/usr/bin/env python3
"""
Analyze first 15 pairs vs all pairs per market.

Tests hypothesis: Are early entries more profitable than later ones?

Usage:
    python scripts/analyze_first_15_pairs.py research/observer/spread_capture_obs_20260113.csv
"""

import sys
import csv
from dataclasses import dataclass
from typing import Dict, List, Tuple
from collections import defaultdict


@dataclass
class FillTracker:
    """Track fills for a side."""
    fills: List[Tuple[float, float]] = None  # (price, size)

    def __post_init__(self):
        self.fills = []

    def add_fill(self, price: float, size: float):
        self.fills.append((price, size))

    @property
    def total_shares(self) -> float:
        return sum(size for _, size in self.fills)

    @property
    def total_cost(self) -> float:
        return sum(price * size for price, size in self.fills)

    @property
    def avg_price(self) -> float:
        return self.total_cost / self.total_shares if self.total_shares > 0 else 0.0

    def shares_at_n(self, n: int) -> float:
        """Total shares from first n fills."""
        return sum(size for _, size in self.fills[:n])

    def cost_at_n(self, n: int) -> float:
        """Total cost from first n fills."""
        return sum(price * size for price, size in self.fills[:n])

    def avg_price_at_n(self, n: int) -> float:
        """Average price from first n fills."""
        shares = self.shares_at_n(n)
        return self.cost_at_n(n) / shares if shares > 0 else 0.0


def analyze_market(rows: List[dict], scenario_prefix: str = "defa") -> dict:
    """
    Analyze a single market's fills.

    Returns dict with:
    - pairs_all: total pairs
    - profit_all: total profit
    - pairs_15: pairs from first 15 fills each side
    - profit_15: profit from first 15 pairs
    """
    up_tracker = FillTracker()
    down_tracker = FillTracker()

    prev_up = 0.0
    prev_down = 0.0

    for row in rows:
        # Get current positions
        up_pos = float(row[f'{scenario_prefix}_up_pos'])
        down_pos = float(row[f'{scenario_prefix}_down_pos'])

        # Detect fills by position increase
        if up_pos > prev_up:
            # UP fill occurred - get price
            entry_side = row[f'{scenario_prefix}_entry_side']
            if entry_side == "UP":
                price = float(row[f'{scenario_prefix}_entry_price'])
            else:
                price = float(row[f'{scenario_prefix}_hedge_price'])
            if price > 0:
                size = up_pos - prev_up
                up_tracker.add_fill(price, size)

        if down_pos > prev_down:
            # DOWN fill occurred - get price
            entry_side = row[f'{scenario_prefix}_entry_side']
            if entry_side == "DOWN":
                price = float(row[f'{scenario_prefix}_entry_price'])
            else:
                price = float(row[f'{scenario_prefix}_hedge_price'])
            if price > 0:
                size = down_pos - prev_down
                down_tracker.add_fill(price, size)

        prev_up = up_pos
        prev_down = down_pos

    # Calculate all pairs
    pairs_all = int(min(up_tracker.total_shares, down_tracker.total_shares))
    if pairs_all > 0:
        pair_cost_all = up_tracker.avg_price + down_tracker.avg_price
        profit_all = pairs_all * (1.00 - pair_cost_all)
    else:
        profit_all = 0.0
        pair_cost_all = 0.0

    # Calculate first 15 pairs (limit to 3 fills each side = 15 shares at 5 share size)
    # Actually, need to think about this differently...
    # First N shares on each side, not first N fills

    # Find how many fills needed to get 15 shares
    up_fills_for_15 = 0
    up_shares = 0.0
    for i, (price, size) in enumerate(up_tracker.fills):
        up_shares += size
        up_fills_for_15 = i + 1
        if up_shares >= 15:
            break

    down_fills_for_15 = 0
    down_shares = 0.0
    for i, (price, size) in enumerate(down_tracker.fills):
        down_shares += size
        down_fills_for_15 = i + 1
        if down_shares >= 15:
            break

    # Calculate profit for first 15 pairs
    up_shares_15 = min(15, up_tracker.shares_at_n(up_fills_for_15))
    down_shares_15 = min(15, down_tracker.shares_at_n(down_fills_for_15))
    pairs_15 = int(min(up_shares_15, down_shares_15))

    if pairs_15 > 0:
        up_avg_15 = up_tracker.avg_price_at_n(up_fills_for_15)
        down_avg_15 = down_tracker.avg_price_at_n(down_fills_for_15)
        pair_cost_15 = up_avg_15 + down_avg_15
        profit_15 = pairs_15 * (1.00 - pair_cost_15)
    else:
        profit_15 = 0.0
        pair_cost_15 = 0.0

    return {
        'pairs_all': pairs_all,
        'profit_all': profit_all,
        'pair_cost_all': pair_cost_all,
        'pairs_15': pairs_15,
        'profit_15': profit_15,
        'pair_cost_15': pair_cost_15,
        'up_fills': len(up_tracker.fills),
        'down_fills': len(down_tracker.fills),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/analyze_first_15_pairs.py <csv_file>")
        sys.exit(1)

    csv_file = sys.argv[1]

    # Read and group by market
    markets: Dict[str, List[dict]] = defaultdict(list)

    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            market = row['market_slug']
            markets[market].append(row)

    print(f"Loaded {sum(len(v) for v in markets.values())} rows from {len(markets)} markets")
    print()

    # Analyze each market
    results = []
    for market, rows in sorted(markets.items()):
        result = analyze_market(rows, "defa")
        result['market'] = market
        results.append(result)

    # Print results
    print("=" * 100)
    print("FIRST 15 PAIRS vs ALL PAIRS ANALYSIS")
    print("=" * 100)
    print()
    print(f"{'Market':<30} {'Pairs15':>8} {'Profit15':>10} {'Cost15':>8} | {'PairsAll':>8} {'ProfitAll':>10} {'CostAll':>8}")
    print("-" * 100)

    total_profit_15 = 0.0
    total_profit_all = 0.0
    total_pairs_15 = 0
    total_pairs_all = 0

    for r in results:
        market_short = r['market'][-20:]  # Last 20 chars
        print(f"{market_short:<30} {r['pairs_15']:>8} ${r['profit_15']:>9.2f} ${r['pair_cost_15']:>7.4f} | "
              f"{r['pairs_all']:>8} ${r['profit_all']:>9.2f} ${r['pair_cost_all']:>7.4f}")
        total_profit_15 += r['profit_15']
        total_profit_all += r['profit_all']
        total_pairs_15 += r['pairs_15']
        total_pairs_all += r['pairs_all']

    print("-" * 100)
    print(f"{'TOTAL':<30} {total_pairs_15:>8} ${total_profit_15:>9.2f} {'':>8} | "
          f"{total_pairs_all:>8} ${total_profit_all:>9.2f}")
    print()

    # Summary
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"First 15 pairs total profit: ${total_profit_15:.2f} ({total_pairs_15} pairs)")
    print(f"All pairs total profit:      ${total_profit_all:.2f} ({total_pairs_all} pairs)")
    print()

    if total_pairs_15 > 0 and total_pairs_all > 0:
        profit_per_pair_15 = total_profit_15 / total_pairs_15
        profit_per_pair_all = total_profit_all / total_pairs_all
        print(f"Profit per pair (first 15):  ${profit_per_pair_15:.4f}")
        print(f"Profit per pair (all):       ${profit_per_pair_all:.4f}")
        print()

        if profit_per_pair_15 > profit_per_pair_all:
            improvement = (profit_per_pair_15 - profit_per_pair_all) / abs(profit_per_pair_all) * 100
            print(f"FINDING: First 15 pairs are {improvement:.1f}% MORE profitable per pair")
        else:
            decline = (profit_per_pair_all - profit_per_pair_15) / abs(profit_per_pair_all) * 100
            print(f"FINDING: First 15 pairs are {decline:.1f}% LESS profitable per pair")


if __name__ == "__main__":
    main()
