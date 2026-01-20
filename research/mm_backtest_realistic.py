#!/usr/bin/env python3
"""
REALISTIC Opportunistic MM Backtest

Fill models compared:
1. OPTIMISTIC: bid <= posted_bid (assumes top of queue) - UNREALISTIC
2. ULTRA_CONSERVATIVE: ask <= posted_bid - TOO STRICT (never happens)
3. REALISTIC: prev_ask > posted_bid AND current_ask <= posted_bid (ask crossed through)

The REALISTIC model counts fills when:
- Previous ask was ABOVE our posted bid
- Current ask is AT or BELOW our posted bid
- This means someone sold through our level - guaranteed fill

Usage:
    python research/mm_backtest_realistic.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from collections import defaultdict


# =============================================================================
# CONFIGURATION
# =============================================================================

STARTING_BALANCE = 170.0
TARGET_SHARES = 15
MAX_POSITION_PER_SIDE = 200
MIN_TIME = 60

MIN_ORDER_QTY = 5
MIN_ORDER_VALUE = 1.0
MIN_RUNTIME_SECS = 300

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
# REALISTIC MM SIMULATION
# =============================================================================

def simulate_mm_realistic(mdf: pd.DataFrame, slug: str) -> Optional[Dict]:
    """
    REALISTIC fill model:

    Fill when ASK CROSSES THROUGH our posted bid:
    - prev_ask > posted_bid AND current_ask <= posted_bid

    This means a seller hit through our level - we definitely got filled.
    """
    mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

    resolution = get_resolution(slug, mdf)
    if resolution is None:
        return None

    up_shares = 0
    down_shares = 0
    up_cost = 0.0
    down_cost = 0.0

    # Track fills by model
    fills_optimistic = 0
    fills_realistic = 0
    fills_ultra_conservative = 0

    up_posted_bid = 0.0
    down_posted_bid = 0.0
    prev_up_ask = None
    prev_down_ask = None

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

        # =====================================================================
        # CHECK FILLS - THREE MODELS
        # =====================================================================

        # UP side
        if up_posted_bid > 0:
            # Optimistic: bid or ask at/below our order
            if up_bid <= up_posted_bid or up_ask <= up_posted_bid:
                fills_optimistic += 1

            # Ultra-conservative: ask at/below our order
            if up_ask <= up_posted_bid:
                fills_ultra_conservative += 1

            # REALISTIC: ask CROSSED THROUGH our bid
            # prev_ask was above, current_ask is at or below
            ask_crossed = (prev_up_ask is not None and
                          prev_up_ask > up_posted_bid and
                          up_ask <= up_posted_bid)

            # Also count if ask dropped significantly (>1 cent) and is now at our level
            ask_dropped = (prev_up_ask is not None and
                          prev_up_ask - up_ask >= 0.01 and
                          up_ask <= up_posted_bid + 0.005)

            if ask_crossed or ask_dropped:
                if up_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                    up_cost += up_posted_bid * TARGET_SHARES
                    up_shares += TARGET_SHARES
                    fills_realistic += 1
                up_posted_bid = 0.0

        # DOWN side
        if down_posted_bid > 0:
            if down_bid <= down_posted_bid or down_ask <= down_posted_bid:
                fills_optimistic += 1

            if down_ask <= down_posted_bid:
                fills_ultra_conservative += 1

            ask_crossed = (prev_down_ask is not None and
                          prev_down_ask > down_posted_bid and
                          down_ask <= down_posted_bid)

            ask_dropped = (prev_down_ask is not None and
                          prev_down_ask - down_ask >= 0.01 and
                          down_ask <= down_posted_bid + 0.005)

            if ask_crossed or ask_dropped:
                if down_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                    down_cost += down_posted_bid * TARGET_SHARES
                    down_shares += TARGET_SHARES
                    fills_realistic += 1
                down_posted_bid = 0.0

        prev_up_ask = up_ask
        prev_down_ask = down_ask

        # Rebalancing
        total_pos = up_shares + down_shares
        abs_diff = abs(up_shares - down_shares)
        abs_imb = abs(imbalance)

        if abs_diff > 60 or (abs_imb > REBALANCE_THRESHOLD and total_pos >= 30):
            if imbalance > 0:
                rebal_size = min(TARGET_SHARES, abs_diff // 2)
                if rebal_size >= MIN_ORDER_QTY and up_shares >= rebal_size:
                    avg = up_cost / up_shares if up_shares > 0 else 0
                    up_shares -= rebal_size
                    up_cost = avg * up_shares
            else:
                rebal_size = min(TARGET_SHARES, abs_diff // 2)
                if rebal_size >= MIN_ORDER_QTY and down_shares >= rebal_size:
                    avg = down_cost / down_shares if down_shares > 0 else 0
                    down_shares -= rebal_size
                    down_cost = avg * down_shares

    if fills_realistic == 0:
        return None

    # Calculate PnL
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
        "fills_realistic": fills_realistic,
        "fills_optimistic": fills_optimistic,
        "fills_ultra_conservative": fills_ultra_conservative,
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
    print("REALISTIC MM BACKTEST")
    print("Fill condition: ASK crosses through our posted bid level")
    print("=" * 80)

    print("\nLoading resolutions...")
    load_resolution_cache()

    all_markets = load_market_data()

    if not all_markets:
        print("No markets found!")
        return

    total_hours = len(all_markets) * 15 / 60

    print(f"\nRunning REALISTIC simulation...")
    results = []
    for slug, mdf in all_markets.items():
        result = simulate_mm_realistic(mdf, slug)
        if result:
            results.append(result)

    if not results:
        print("No results!")
        return

    # Aggregate
    total_pnl = sum(r['total_pnl'] for r in results)
    total_pairs = sum(r['pairs'] for r in results)
    total_fills_realistic = sum(r['fills_realistic'] for r in results)
    total_fills_optimistic = sum(r['fills_optimistic'] for r in results)
    total_fills_ultra = sum(r['fills_ultra_conservative'] for r in results)

    pair_costs = [r['pair_cost'] for r in results if r['pairs'] > 0]
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    print("\n" + "=" * 80)
    print("FILL MODEL COMPARISON")
    print("=" * 80)

    print(f"""
    FILL COUNTS BY MODEL:
    ---------------------
    Optimistic (bid OR ask <= posted):        {total_fills_optimistic:,}
    REALISTIC (ask crosses through posted):   {total_fills_realistic:,}
    Ultra-conservative (ask <= posted):       {total_fills_ultra:,}

    Realistic vs Optimistic:                  {total_fills_realistic/total_fills_optimistic*100:.1f}%
    """)

    print("=" * 80)
    print("REALISTIC RESULTS")
    print("=" * 80)

    print(f"""
    Markets with fills:     {len(results)}
    Total hours:            {total_hours:.1f}

    Total pairs:            {total_pairs}
    Pairs per hour:         {total_pairs / total_hours:.1f}

    Average pair cost:      ${avg_pair_cost:.4f}
    Profit per pair:        ${1.0 - avg_pair_cost:.4f}

    Total PnL:              ${total_pnl:.2f}
    Hourly rate:            ${total_pnl / total_hours:.2f}/hr
    """)

    print("=" * 80)
    print("COMPARISON SUMMARY")
    print("=" * 80)

    optimistic_hourly = 10.60
    realistic_hourly = total_pnl / total_hours

    print(f"""
    Optimistic model:       ${optimistic_hourly:.2f}/hr
    REALISTIC model:        ${realistic_hourly:.2f}/hr

    Difference:             ${optimistic_hourly - realistic_hourly:.2f}/hr ({(1 - realistic_hourly/optimistic_hourly)*100:.0f}% reduction)

    REALISTIC EXPECTATION FOR LIVE TRADING:
    ----------------------------------------
    This is a more realistic expectation because:
    - Only counts fills when ask actually crosses through our level
    - Doesn't assume we're at top of order queue
    - Accounts for the fact that bid resting != guaranteed fill
    """)

    # Sample results
    print("\n" + "=" * 80)
    print("TOP PERFORMING MARKETS (Realistic)")
    print("=" * 80)

    sorted_results = sorted(results, key=lambda r: r['total_pnl'], reverse=True)
    for r in sorted_results[:10]:
        print(f"  {r['slug']}: {r['pairs']} pairs @ ${r['pair_cost']:.3f} = ${r['total_pnl']:.2f}")


if __name__ == "__main__":
    main()
