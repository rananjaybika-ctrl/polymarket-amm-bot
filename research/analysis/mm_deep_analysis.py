#!/usr/bin/env python3
"""
Deep Analysis of Opportunistic MM Strategy

Answers: Why is MM so much better than velocity?
- Breakdown by market type (trending vs ranging)
- Trade-by-trade examples
- Comparison with velocity strategy
"""

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple
import json


# =============================================================================
# CONFIGURATION (matching mm_backtest.py)
# =============================================================================

TARGET_SHARES = 15
MAX_POSITION_PER_SIDE = 200
MIN_TIME = 60
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
# LOAD DATA
# =============================================================================

def load_resolutions() -> Dict[str, str]:
    """Load verified resolutions."""
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')
    resolutions = {}
    if resolution_file.exists():
        df = pd.read_csv(resolution_file)
        for _, row in df.iterrows():
            if row['winner'] in ('UP', 'DOWN'):
                resolutions[row['market']] = row['winner']
    return resolutions


def load_markets() -> Dict[str, pd.DataFrame]:
    """Load all valid market data."""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('grid_obs_*.csv'))

    all_markets = {}
    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
            for slug in df['market_slug'].unique():
                mdf = df[df['market_slug'] == slug].copy()
                mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

                if len(mdf) < 25:
                    continue
                first = mdf.iloc[0]['time_remaining_secs']
                last = mdf.iloc[-1]['time_remaining_secs']
                if first < 800 or last > 60:
                    continue

                if slug not in all_markets or len(mdf) > len(all_markets[slug]):
                    all_markets[slug] = mdf
        except:
            continue

    return all_markets


# =============================================================================
# MARKET CLASSIFICATION
# =============================================================================

def classify_market(mdf: pd.DataFrame, resolution: str) -> Dict:
    """
    Classify market as trending or ranging based on price action.

    Trending: UP or DOWN prices move consistently in one direction
    Ranging: Prices oscillate back and forth
    """
    up_prices = mdf['up_bid'].values
    down_prices = mdf['down_bid'].values

    # Calculate direction changes
    up_changes = np.diff(up_prices)
    down_changes = np.diff(down_prices)

    # Count sign changes (reversals)
    up_reversals = np.sum(np.diff(np.sign(up_changes)) != 0)
    down_reversals = np.sum(np.diff(np.sign(down_changes)) != 0)

    total_samples = len(mdf)
    reversal_rate = (up_reversals + down_reversals) / (2 * total_samples)

    # Price range
    up_range = up_prices.max() - up_prices.min()
    down_range = down_prices.max() - down_prices.min()

    # Final price vs start
    up_start, up_end = up_prices[0], up_prices[-1]
    down_start, down_end = down_prices[0], down_prices[-1]

    up_net_move = up_end - up_start
    down_net_move = down_end - down_start

    # Trending if net move is >50% of range
    is_trending = abs(up_net_move) > 0.5 * up_range or abs(down_net_move) > 0.5 * down_range

    # Velocity analysis
    velocities = mdf['velocity_bps'].abs().values
    avg_velocity = np.mean(velocities)
    max_velocity = np.max(velocities)

    # Trend direction
    if is_trending:
        if up_net_move > 0:
            trend_dir = "UP"
        else:
            trend_dir = "DOWN"
    else:
        trend_dir = "NONE"

    return {
        "type": "TRENDING" if is_trending else "RANGING",
        "trend_direction": trend_dir,
        "resolution": resolution,
        "trend_matches_resolution": trend_dir == resolution,
        "reversal_rate": reversal_rate,
        "up_range": up_range,
        "down_range": down_range,
        "up_net_move": up_net_move,
        "down_net_move": down_net_move,
        "avg_velocity": avg_velocity,
        "max_velocity": max_velocity,
        "samples": total_samples,
    }


# =============================================================================
# MM SIMULATION WITH DETAILED TRACKING
# =============================================================================

@dataclass
class DetailedMMResult:
    slug: str
    classification: Dict
    fills: List[Dict]
    final_position: Dict
    pnl_breakdown: Dict


def simulate_mm_detailed(mdf: pd.DataFrame, slug: str, resolution: str) -> DetailedMMResult:
    """Simulate MM with detailed fill tracking."""

    classification = classify_market(mdf, resolution)

    up_shares = 0
    down_shares = 0
    up_cost = 0.0
    down_cost = 0.0

    fills = []
    up_posted_bid = 0.0
    down_posted_bid = 0.0
    prev_up_bid = None
    prev_down_bid = None

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

        # Get base offset from velocity
        abs_vel = abs(velocity_bps)
        base_offset = 0.01
        for zone_name, zone in VELOCITY_ZONES.items():
            if zone['vel_min'] <= abs_vel < zone['vel_max']:
                base_offset = zone['base_offset']
                break

        # Inventory adjustment
        adjustment = imbalance * INVENTORY_ADJUSTMENT_FACTOR
        up_offset = max(0.005, min(MAX_OFFSET, base_offset + adjustment))
        down_offset = max(0.005, min(MAX_OFFSET, base_offset - adjustment))

        # Post bids
        if up_shares < MAX_POSITION_PER_SIDE:
            up_posted_bid = max(0.01, min(0.95, up_bid - up_offset))
        if down_shares < MAX_POSITION_PER_SIDE:
            down_posted_bid = max(0.01, min(0.95, down_bid - down_offset))

        # Check UP fill
        up_filled = False
        if up_posted_bid > 0:
            if up_bid <= up_posted_bid or up_ask <= up_posted_bid:
                up_filled = True
            elif prev_up_bid is not None and prev_up_bid - up_bid >= 0.01:
                if up_bid <= up_posted_bid + 0.005:
                    up_filled = True

        if up_filled:
            up_cost += up_posted_bid * TARGET_SHARES
            up_shares += TARGET_SHARES
            fills.append({
                "time_remaining": time_rem,
                "side": "UP",
                "price": up_posted_bid,
                "size": TARGET_SHARES,
                "type": "BUY",
                "velocity": velocity_bps,
                "up_bid": up_bid,
                "down_bid": down_bid,
            })
            up_posted_bid = 0.0

        # Check DOWN fill
        down_filled = False
        if down_posted_bid > 0:
            if down_bid <= down_posted_bid or down_ask <= down_posted_bid:
                down_filled = True
            elif prev_down_bid is not None and prev_down_bid - down_bid >= 0.01:
                if down_bid <= down_posted_bid + 0.005:
                    down_filled = True

        if down_filled:
            down_cost += down_posted_bid * TARGET_SHARES
            down_shares += TARGET_SHARES
            fills.append({
                "time_remaining": time_rem,
                "side": "DOWN",
                "price": down_posted_bid,
                "size": TARGET_SHARES,
                "type": "BUY",
                "velocity": velocity_bps,
                "up_bid": up_bid,
                "down_bid": down_bid,
            })
            down_posted_bid = 0.0

        prev_up_bid = up_bid
        prev_down_bid = down_bid

        # Rebalancing
        total_pos = up_shares + down_shares
        abs_diff = abs(up_shares - down_shares)
        abs_imbalance = abs(imbalance)

        if abs_diff > 60 or (abs_imbalance > REBALANCE_THRESHOLD and total_pos >= 30):
            if imbalance > 0:
                rebal_side = "UP"
                rebal_price = up_bid
            else:
                rebal_side = "DOWN"
                rebal_price = down_bid

            rebal_size = min(TARGET_SHARES, abs_diff // 2)
            if rebal_size >= 5:
                if rebal_side == "UP":
                    up_shares -= rebal_size
                    up_cost -= (up_cost / (up_shares + rebal_size)) * rebal_size
                else:
                    down_shares -= rebal_size
                    down_cost -= (down_cost / (down_shares + rebal_size)) * rebal_size

                fills.append({
                    "time_remaining": time_rem,
                    "side": rebal_side,
                    "price": rebal_price,
                    "size": rebal_size,
                    "type": "REBALANCE_SELL",
                    "velocity": velocity_bps,
                    "up_bid": up_bid,
                    "down_bid": down_bid,
                })

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

    # Average prices
    up_avg = up_cost / up_shares if up_shares > 0 else 0
    down_avg = down_cost / down_shares if down_shares > 0 else 0
    pair_cost = up_avg + down_avg if pairs > 0 else 0

    pnl_breakdown = {
        "pairs": pairs,
        "pair_cost": pair_cost,
        "pair_profit": pairs * (1.0 - pair_cost) if pairs > 0 else 0,
        "unmatched_up": unmatched_up,
        "unmatched_down": unmatched_down,
        "unmatched_pnl": unmatched_up_payout + unmatched_down_payout - (unmatched_up * up_avg + unmatched_down * down_avg),
        "total_pnl": total_pnl,
    }

    final_position = {
        "up_shares": up_shares,
        "down_shares": down_shares,
        "up_avg_price": up_avg,
        "down_avg_price": down_avg,
    }

    return DetailedMMResult(
        slug=slug,
        classification=classification,
        fills=fills,
        final_position=final_position,
        pnl_breakdown=pnl_breakdown,
    )


# =============================================================================
# MAIN ANALYSIS
# =============================================================================

def main():
    print("=" * 80)
    print("DEEP ANALYSIS: WHY IS MM SO MUCH BETTER?")
    print("=" * 80)

    resolutions = load_resolutions()
    markets = load_markets()

    # Filter to markets with resolutions
    valid_markets = {k: v for k, v in markets.items() if k in resolutions}
    print(f"\nAnalyzing {len(valid_markets)} markets with verified resolutions")

    # Run detailed simulation
    results = []
    for slug, mdf in valid_markets.items():
        result = simulate_mm_detailed(mdf, slug, resolutions[slug])
        results.append(result)

    # ==========================================================================
    # SECTION 1: PARAMETERS
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 1: MM STRATEGY PARAMETERS")
    print("=" * 80)

    print(f"""
    TARGET_SHARES:              {TARGET_SHARES} shares per fill
    MAX_POSITION_PER_SIDE:      {MAX_POSITION_PER_SIDE} shares
    MIN_TIME:                   {MIN_TIME} seconds (entry cutoff)

    OFFSETS BY VELOCITY ZONE:
      neutral (0-0.10 bps):     $0.01 offset from best bid
      moderate (0.10-0.30 bps): $0.01 offset
      strong (0.30-0.50 bps):   $0.02 offset
      very_strong (>0.50 bps):  $0.03 offset

    INVENTORY ADJUSTMENT:       ±$0.02 based on imbalance
      - If UP heavy: widen UP offset, tighten DOWN offset
      - If DOWN heavy: widen DOWN offset, tighten UP offset

    REBALANCING:
      - Trigger at 30% imbalance OR >60 share difference
      - Sell excess side at current bid
    """)

    # ==========================================================================
    # SECTION 2: OVERALL RESULTS
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 2: OVERALL RESULTS")
    print("=" * 80)

    total_pnl = sum(r.pnl_breakdown['total_pnl'] for r in results)
    total_pairs = sum(r.pnl_breakdown['pairs'] for r in results)
    total_hours = len(valid_markets) * 15 / 60

    pair_costs = [r.pnl_breakdown['pair_cost'] for r in results if r.pnl_breakdown['pairs'] > 0]
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    print(f"""
    Total Markets:          {len(results)}
    Total Hours:            {total_hours:.1f}
    Total Pairs:            {total_pairs}
    Pairs per Hour:         {total_pairs / total_hours:.1f}

    Total PnL:              ${total_pnl:.2f}
    Hourly Rate:            ${total_pnl / total_hours:.2f}/hr

    Average Pair Cost:      ${avg_pair_cost:.4f}
    Average Pair Profit:    ${1.0 - avg_pair_cost:.4f} per pair
    """)

    # ==========================================================================
    # SECTION 3: BREAKDOWN BY MARKET TYPE
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 3: TRENDING vs RANGING MARKETS")
    print("=" * 80)

    trending = [r for r in results if r.classification['type'] == 'TRENDING']
    ranging = [r for r in results if r.classification['type'] == 'RANGING']

    trending_pnl = sum(r.pnl_breakdown['total_pnl'] for r in trending)
    ranging_pnl = sum(r.pnl_breakdown['total_pnl'] for r in ranging)

    trending_pairs = sum(r.pnl_breakdown['pairs'] for r in trending)
    ranging_pairs = sum(r.pnl_breakdown['pairs'] for r in ranging)

    print(f"""
    TRENDING MARKETS ({len(trending)} markets):
      Total Pairs:          {trending_pairs}
      Total PnL:            ${trending_pnl:.2f}
      Avg PnL/Market:       ${trending_pnl/len(trending):.2f}

    RANGING MARKETS ({len(ranging)} markets):
      Total Pairs:          {ranging_pairs}
      Total PnL:            ${ranging_pnl:.2f}
      Avg PnL/Market:       ${ranging_pnl/len(ranging):.2f}
    """)

    # Sub-breakdown for trending
    trend_up = [r for r in trending if r.classification['trend_direction'] == 'UP']
    trend_down = [r for r in trending if r.classification['trend_direction'] == 'DOWN']

    print(f"""
    TRENDING BREAKDOWN:
      UP trends:   {len(trend_up)} markets, ${sum(r.pnl_breakdown['total_pnl'] for r in trend_up):.2f}
      DOWN trends: {len(trend_down)} markets, ${sum(r.pnl_breakdown['total_pnl'] for r in trend_down):.2f}
    """)

    # ==========================================================================
    # SECTION 4: WHY MM WORKS - THE MATH
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 4: WHY MM WORKS - THE MATH")
    print("=" * 80)

    print(f"""
    THE KEY INSIGHT:
    ================

    MM strategy buys BOTH sides at below-market prices.

    Example in a balanced market (UP=0.50, DOWN=0.50):
      - Post UP bid at $0.49 (offset $0.01)
      - Post DOWN bid at $0.49 (offset $0.01)
      - Total pair cost: $0.98
      - Pair payout at resolution: $1.00
      - PROFIT: $0.02 per pair, GUARANTEED regardless of winner

    Velocity Strategy Comparison:
      - Velocity enters at ASK ($0.51) for winner
      - Posts loser bid at $0.37-0.40 (offset $0.12)
      - If passive fill: pair cost ~$0.88, profit ~$0.12
      - If stop-loss: pair cost ~$1.00+, LOSS

    WHY MM BEATS VELOCITY:
    =====================

    1. NO PREDICTION RISK
       - MM doesn't care who wins - pairs always pay $1
       - Velocity must predict correctly OR get passive hedge

    2. LOWER ENTRY COST
       - MM buys at BID - offset (passive fills)
       - Velocity buys winner at ASK (market order)

    3. MORE FILLS
       - MM posts on BOTH sides, 2x fill opportunities
       - Velocity only posts on loser side after entry

    4. COMPOUND EFFECT
       - {total_pairs} pairs × ${1.0 - avg_pair_cost:.4f} profit = ${total_pairs * (1.0 - avg_pair_cost):.2f}
       - Velocity: fewer trades, higher per-trade variance
    """)

    # ==========================================================================
    # SECTION 5: EXAMPLE MARKETS
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 5: EXAMPLE TRADES")
    print("=" * 80)

    # Find best and worst markets
    sorted_results = sorted(results, key=lambda r: r.pnl_breakdown['total_pnl'], reverse=True)

    # Show top 3 and bottom 3
    print("\n--- TOP 3 PROFITABLE MARKETS ---")
    for r in sorted_results[:3]:
        c = r.classification
        p = r.pnl_breakdown
        print(f"""
    Market: {r.slug}
    Type: {c['type']} ({c['trend_direction']})
    Resolution: {c['resolution']}

    Pairs: {p['pairs']}, Pair Cost: ${p['pair_cost']:.4f}
    Pair Profit: ${p['pair_profit']:.2f}
    Unmatched: UP={p['unmatched_up']}, DOWN={p['unmatched_down']} (${p['unmatched_pnl']:.2f})
    TOTAL PnL: ${p['total_pnl']:.2f}

    Fills: {len(r.fills)}
    """)

    print("\n--- BOTTOM 3 MARKETS (Losses) ---")
    for r in sorted_results[-3:]:
        c = r.classification
        p = r.pnl_breakdown
        print(f"""
    Market: {r.slug}
    Type: {c['type']} ({c['trend_direction']})
    Resolution: {c['resolution']}

    Pairs: {p['pairs']}, Pair Cost: ${p['pair_cost']:.4f}
    Pair Profit: ${p['pair_profit']:.2f}
    Unmatched: UP={p['unmatched_up']}, DOWN={p['unmatched_down']} (${p['unmatched_pnl']:.2f})
    TOTAL PnL: ${p['total_pnl']:.2f}

    Fills: {len(r.fills)}
    """)

    # ==========================================================================
    # SECTION 6: DETAILED EXAMPLE - TRENDING MARKET
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 6: DETAILED EXAMPLE - TRENDING MARKET")
    print("=" * 80)

    # Find a good trending example
    trending_profitable = [r for r in trending if r.pnl_breakdown['total_pnl'] > 2]
    if trending_profitable:
        example = trending_profitable[0]
        c = example.classification
        p = example.pnl_breakdown

        print(f"""
    Market: {example.slug}
    Classification: {c['type']}, trending {c['trend_direction']}
    Resolution: {c['resolution']}
    Avg Velocity: {c['avg_velocity']:.3f} bps

    FILL SEQUENCE (first 10):
    """)
        for i, fill in enumerate(example.fills[:10]):
            print(f"    {i+1}. t={fill['time_remaining']:.0f}s | {fill['side']:5} @ ${fill['price']:.2f} | "
                  f"vel={fill['velocity']:.3f} | up_bid=${fill['up_bid']:.2f} down_bid=${fill['down_bid']:.2f}")

        print(f"""

    FINAL POSITION:
      UP shares: {example.final_position['up_shares']} @ avg ${example.final_position['up_avg_price']:.4f}
      DOWN shares: {example.final_position['down_shares']} @ avg ${example.final_position['down_avg_price']:.4f}

    PnL BREAKDOWN:
      Pairs: {p['pairs']} × (1.00 - {p['pair_cost']:.4f}) = ${p['pair_profit']:.2f}
      Unmatched: ${p['unmatched_pnl']:.2f}
      TOTAL: ${p['total_pnl']:.2f}
    """)

    # ==========================================================================
    # SECTION 7: DETAILED EXAMPLE - RANGING MARKET
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 7: DETAILED EXAMPLE - RANGING MARKET")
    print("=" * 80)

    ranging_profitable = [r for r in ranging if r.pnl_breakdown['total_pnl'] > 2]
    if ranging_profitable:
        example = ranging_profitable[0]
        c = example.classification
        p = example.pnl_breakdown

        print(f"""
    Market: {example.slug}
    Classification: {c['type']}
    Resolution: {c['resolution']}
    Reversal Rate: {c['reversal_rate']:.3f}
    Avg Velocity: {c['avg_velocity']:.3f} bps

    FILL SEQUENCE (first 10):
    """)
        for i, fill in enumerate(example.fills[:10]):
            print(f"    {i+1}. t={fill['time_remaining']:.0f}s | {fill['side']:5} @ ${fill['price']:.2f} | "
                  f"vel={fill['velocity']:.3f} | up_bid=${fill['up_bid']:.2f} down_bid=${fill['down_bid']:.2f}")

        print(f"""

    FINAL POSITION:
      UP shares: {example.final_position['up_shares']} @ avg ${example.final_position['up_avg_price']:.4f}
      DOWN shares: {example.final_position['down_shares']} @ avg ${example.final_position['down_avg_price']:.4f}

    PnL BREAKDOWN:
      Pairs: {p['pairs']} × (1.00 - {p['pair_cost']:.4f}) = ${p['pair_profit']:.2f}
      Unmatched: ${p['unmatched_pnl']:.2f}
      TOTAL: ${p['total_pnl']:.2f}

    WHY RANGING WORKS WELL:
      - More price oscillations = more fill opportunities
      - Both sides get filled frequently
      - Pairs accumulate faster than trending markets
    """)

    # ==========================================================================
    # SECTION 8: RISK ANALYSIS
    # ==========================================================================
    print("\n" + "=" * 80)
    print("SECTION 8: RISK ANALYSIS - WHERE MM LOSES")
    print("=" * 80)

    losing_markets = [r for r in results if r.pnl_breakdown['total_pnl'] < 0]

    print(f"""
    Losing Markets: {len(losing_markets)} / {len(results)} ({len(losing_markets)/len(results)*100:.1f}%)
    Total Loss: ${sum(r.pnl_breakdown['total_pnl'] for r in losing_markets):.2f}

    COMMON LOSS PATTERNS:
    """)

    # Analyze losing markets
    for r in losing_markets[:5]:
        c = r.classification
        p = r.pnl_breakdown
        print(f"""
      {r.slug}:
        Type: {c['type']}, Resolution: {c['resolution']}
        Pairs: {p['pairs']}, Pair Cost: ${p['pair_cost']:.4f}
        Unmatched: UP={p['unmatched_up']}, DOWN={p['unmatched_down']}
        Loss: ${p['total_pnl']:.2f}

        Issue: {"Unbalanced position" if abs(p['unmatched_up'] - p['unmatched_down']) > 30 else "High pair cost"}
    """)

    print("""
    MM LOSES WHEN:
    1. Pair cost > $1.00 (rare with proper offsets)
    2. Heavy unmatched position on wrong side
    3. One-sided fills during strong trend

    MITIGATION:
    - Rebalancing sells excess inventory
    - Wider offsets during high velocity
    - Position limits prevent over-exposure
    """)

    print("\n" + "=" * 80)
    print("ANALYSIS COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
