#!/usr/bin/env python3
"""
CONSERVATIVE Opportunistic MM Backtest

Only counts fills when ASK crosses below our posted BID.
This is a GUARANTEED fill scenario - no queue position assumptions.

Usage:
    python research/mm_backtest_conservative.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from collections import defaultdict


# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MAX_POSITION_PER_SIDE = 200
MIN_TIME = 60

# Polymarket restrictions
MIN_ORDER_QTY = 5
MIN_ORDER_VALUE = 1.0

# Market filtering
MIN_RUNTIME_SECS = 300

# MM parameters
BASE_OFFSET = 0.01
MAX_OFFSET = 0.05
INVENTORY_ADJUSTMENT_FACTOR = 0.02
REBALANCE_THRESHOLD = 0.30

VELOCITY_ZONES = {
    'neutral':     {'vel_min': 0.00, 'vel_max': 0.10, 'base_offset': 0.01},
    'moderate':    {'vel_min': 0.10, 'vel_max': 0.30, 'base_offset': 0.01},
    'strong':      {'vel_min': 0.30, 'vel_max': 0.50, 'base_offset': 0.02},
    'very_strong': {'vel_min': 0.50, 'vel_max': 99.0, 'base_offset': 0.03},
}


# =============================================================================
# RESOLUTION CACHE
# =============================================================================

_RESOLUTION_CACHE: Dict[str, str] = {}


def load_resolution_cache():
    global _RESOLUTION_CACHE
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')
    if resolution_file.exists():
        df = pd.read_csv(resolution_file)
        for _, row in df.iterrows():
            if row['winner'] in ('UP', 'DOWN'):
                _RESOLUTION_CACHE[row['market']] = row['winner']
        print(f"  Loaded {len(_RESOLUTION_CACHE)} resolutions")


def get_resolution(slug: str, mdf: pd.DataFrame) -> Optional[str]:
    if slug in _RESOLUTION_CACHE:
        return _RESOLUTION_CACHE[slug]
    # Guess from final prices
    final = mdf.iloc[-1]
    if final['up_bid'] >= 0.90:
        return 'UP'
    elif final['down_bid'] >= 0.90:
        return 'DOWN'
    return 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'


# =============================================================================
# MARKET VALIDATION
# =============================================================================

def is_valid_market(mdf: pd.DataFrame, slug: str) -> Tuple[bool, str]:
    if len(mdf) < 25:
        return False, "too_few_samples"

    first = mdf.iloc[0]['time_remaining_secs']
    last = mdf.iloc[-1]['time_remaining_secs']

    if first - last < MIN_RUNTIME_SECS:
        return False, "runtime_under_5min"

    if first < 800 or last > 60:
        return False, "incomplete_observation"

    return True, "valid"


# =============================================================================
# CONSERVATIVE MM SIMULATION
# =============================================================================

def simulate_mm_conservative(mdf: pd.DataFrame, slug: str) -> Optional[Dict]:
    """
    CONSERVATIVE fill model:
    Only count a fill when ASK <= posted_bid (guaranteed fill).

    This is stricter than:
    - bid <= posted_bid (requires queue priority)
    - price drop heuristics (speculative)
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    resolution = get_resolution(slug, mdf)
    if resolution is None:
        return None

    up_shares = 0
    down_shares = 0
    up_cost = 0.0
    down_cost = 0.0

    fills_up = 0
    fills_down = 0
    fills_conservative = 0
    fills_optimistic = 0  # Track how many we'd get with optimistic model

    up_posted_bid = 0.0
    down_posted_bid = 0.0

    for i in range(len(mdf)):
        row = mdf.iloc[i]
        time_rem = row['time_remaining_secs']
        velocity_bps = row.get('velocity_bps', 0)

        if time_rem < MIN_TIME:
            continue

        up_bid = row['up_bid']
        up_ask = row['up_ask']
        down_bid = row['down_bid']
        down_ask = row['down_ask']

        # Calculate imbalance
        total = up_shares + down_shares
        imbalance = (up_shares - down_shares) / total if total > 0 else 0

        # Get offset
        abs_vel = abs(velocity_bps)
        base_offset = 0.01
        for zone in VELOCITY_ZONES.values():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                base_offset = zone['base_offset']
                break

        adjustment = imbalance * INVENTORY_ADJUSTMENT_FACTOR
        up_offset = max(0.005, min(MAX_OFFSET, base_offset + adjustment))
        down_offset = max(0.005, min(MAX_OFFSET, base_offset - adjustment))

        # Post bids
        if up_shares < MAX_POSITION_PER_SIDE:
            up_posted_bid = max(0.01, min(0.95, up_bid - up_offset))
        if down_shares < MAX_POSITION_PER_SIDE:
            down_posted_bid = max(0.01, min(0.95, down_bid - down_offset))

        # CONSERVATIVE fill check: ONLY when ASK <= posted_bid
        if up_posted_bid > 0:
            # Track optimistic (what we had before)
            if up_bid <= up_posted_bid or up_ask <= up_posted_bid:
                fills_optimistic += 1

            # CONSERVATIVE: Only when ask crosses below our bid
            if up_ask <= up_posted_bid:
                if up_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                    up_cost += up_posted_bid * TARGET_SHARES
                    up_shares += TARGET_SHARES
                    fills_up += 1
                    fills_conservative += 1
                up_posted_bid = 0.0

        if down_posted_bid > 0:
            # Track optimistic
            if down_bid <= down_posted_bid or down_ask <= down_posted_bid:
                fills_optimistic += 1

            # CONSERVATIVE: Only when ask crosses below our bid
            if down_ask <= down_posted_bid:
                if down_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                    down_cost += down_posted_bid * TARGET_SHARES
                    down_shares += TARGET_SHARES
                    fills_down += 1
                    fills_conservative += 1
                down_posted_bid = 0.0

        # Rebalancing (keep same logic)
        total_pos = up_shares + down_shares
        abs_diff = abs(up_shares - down_shares)
        abs_imb = abs(imbalance)

        if abs_diff > 60 or (abs_imb > REBALANCE_THRESHOLD and total_pos >= 30):
            if imbalance > 0:
                rebal_size = min(TARGET_SHARES, abs_diff // 2)
                if rebal_size >= MIN_ORDER_QTY:
                    up_shares -= rebal_size
                    if up_shares > 0:
                        up_cost = (up_cost / (up_shares + rebal_size)) * up_shares
                    else:
                        up_cost = 0
            else:
                rebal_size = min(TARGET_SHARES, abs_diff // 2)
                if rebal_size >= MIN_ORDER_QTY:
                    down_shares -= rebal_size
                    if down_shares > 0:
                        down_cost = (down_cost / (down_shares + rebal_size)) * down_shares
                    else:
                        down_cost = 0

    if fills_conservative == 0:
        return None

    # Calculate PnL at resolution
    pairs = min(up_shares, down_shares)
    unmatched_up = up_shares - pairs
    unmatched_down = down_shares - pairs

    total_cost = up_cost + down_cost

    pair_payout = pairs * 1.0
    unmatched_up_payout = unmatched_up * (1.0 if resolution == "UP" else 0.0)
    unmatched_down_payout = unmatched_down * (1.0 if resolution == "DOWN" else 0.0)
    total_payout = pair_payout + unmatched_up_payout + unmatched_down_payout

    total_pnl = total_payout - total_cost

    up_avg = up_cost / up_shares if up_shares > 0 else 0
    down_avg = down_cost / down_shares if down_shares > 0 else 0
    pair_cost = up_avg + down_avg if pairs > 0 else 0

    return {
        "slug": slug,
        "resolution": resolution,
        "pairs": pairs,
        "pair_cost": pair_cost,
        "unmatched_up": unmatched_up,
        "unmatched_down": unmatched_down,
        "total_pnl": total_pnl,
        "fills_up": fills_up,
        "fills_down": fills_down,
        "fills_conservative": fills_conservative,
        "fills_optimistic": fills_optimistic,
    }


# =============================================================================
# DATA LOADING
# =============================================================================

def load_market_data() -> Dict[str, pd.DataFrame]:
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('grid_obs_*.csv'))
    csv_files.extend(sorted(observer_dir.glob('spread_capture_obs_*.csv')))

    print(f"Loading from {len(csv_files)} files...")

    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            if df.empty:
                continue

            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug]
                is_valid, _ = is_valid_market(mdf, slug)
                if is_valid:
                    if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                        all_markets[slug] = mdf.copy()
        except:
            continue

    print(f"Valid markets: {len(all_markets)}")
    return all_markets


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 80)
    print("CONSERVATIVE MM BACKTEST")
    print("Fill condition: ASK <= posted_bid (guaranteed fills only)")
    print("=" * 80)

    print("\nLoading resolutions...")
    load_resolution_cache()

    all_markets = load_market_data()

    if not all_markets:
        print("No markets found!")
        return

    total_hours = len(all_markets) * 15 / 60

    # Run simulation
    print(f"\nRunning CONSERVATIVE simulation...")
    results = []
    for slug, mdf in all_markets.items():
        result = simulate_mm_conservative(mdf, slug)
        if result:
            results.append(result)

    if not results:
        print("No results!")
        return

    # Aggregate
    total_pnl = sum(r['total_pnl'] for r in results)
    total_pairs = sum(r['pairs'] for r in results)
    total_fills_conservative = sum(r['fills_conservative'] for r in results)
    total_fills_optimistic = sum(r['fills_optimistic'] for r in results)

    pair_costs = [r['pair_cost'] for r in results if r['pairs'] > 0]
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    # Print results
    print("\n" + "=" * 80)
    print("CONSERVATIVE vs OPTIMISTIC COMPARISON")
    print("=" * 80)

    print(f"""
    FILL MODEL COMPARISON:
    ----------------------
    Optimistic fills (bid OR ask <= posted):  {total_fills_optimistic}
    Conservative fills (ask <= posted ONLY):  {total_fills_conservative}
    Fill reduction:                           {(1 - total_fills_conservative/total_fills_optimistic)*100:.1f}%

    CONSERVATIVE RESULTS:
    ---------------------
    Markets with fills:     {len(results)}
    Total hours:            {total_hours:.1f}

    Total pairs:            {total_pairs}
    Pairs per hour:         {total_pairs / total_hours:.1f}

    Average pair cost:      ${avg_pair_cost:.4f}
    Profit per pair:        ${1.0 - avg_pair_cost:.4f}

    Total PnL:              ${total_pnl:.2f}
    Hourly rate:            ${total_pnl / total_hours:.2f}/hr
    """)

    # Compare with original
    original_hourly = 10.60
    print(f"""
    COMPARISON WITH OPTIMISTIC MODEL:
    ---------------------------------
    Optimistic hourly rate:    ${original_hourly:.2f}/hr
    Conservative hourly rate:  ${total_pnl / total_hours:.2f}/hr
    Reduction:                 {(1 - (total_pnl/total_hours)/original_hourly)*100:.1f}%
    """)

    # Show sample results
    print("\n" + "=" * 80)
    print("SAMPLE MARKET RESULTS")
    print("=" * 80)

    sorted_results = sorted(results, key=lambda r: r['total_pnl'], reverse=True)

    print("\nTop 5 profitable:")
    for r in sorted_results[:5]:
        print(f"  {r['slug']}: {r['pairs']} pairs @ ${r['pair_cost']:.3f} = ${r['total_pnl']:.2f}")

    print("\nBottom 5 (losses):")
    for r in sorted_results[-5:]:
        print(f"  {r['slug']}: {r['pairs']} pairs, unmatched UP={r['unmatched_up']} DOWN={r['unmatched_down']} = ${r['total_pnl']:.2f}")


if __name__ == "__main__":
    main()
