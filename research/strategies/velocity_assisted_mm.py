#!/usr/bin/env python3
"""
Velocity-Assisted Market Making

Use velocity to HELP hedging, not to pick a direction:
- Winner side (moving up): bid MORE aggressive to fill before price rises
- Loser side (moving down): bid LESS aggressive, price will come to us
"""

import pandas as pd
import numpy as np
from pathlib import Path

SHARES_PER_SIDE = 10
MIN_TIME_REMAINING = 120
MIN_VELOCITY_BPS = 0.10  # Lower threshold to use velocity assist

# Asymmetric offsets based on velocity
# Winner needs aggressive bid (price rising)
# Loser can be passive (price falling)
CONFIGS = {
    'symmetric': {'winner_off': -0.03, 'loser_off': -0.03},
    'slight_asym': {'winner_off': -0.02, 'loser_off': -0.04},
    'moderate_asym': {'winner_off': -0.01, 'loser_off': -0.05},
    'strong_asym': {'winner_off': +0.00, 'loser_off': -0.06},
    'very_strong_asym': {'winner_off': +0.01, 'loser_off': -0.07},
}


def test_config(df, complete, config):
    """Test a specific configuration."""
    winner_off = config['winner_off']
    loser_off = config['loser_off']

    total_pnl = 0
    hedged_count = 0
    pair_costs = []

    for slug in complete:
        mdf = df[df['market_slug'] == slug].copy()

        entry_row = None
        entry_idx = None
        for i, (idx, row) in enumerate(mdf.iterrows()):
            if row['time_remaining_secs'] >= MIN_TIME_REMAINING:
                entry_idx = i
                entry_row = row
                break

        if entry_row is None:
            continue

        vel = entry_row['velocity_bps']

        # Determine winner/loser based on velocity
        if abs(vel) >= MIN_VELOCITY_BPS:
            if vel > 0:  # UP is winner
                up_offset = winner_off
                down_offset = loser_off
            else:  # DOWN is winner
                up_offset = loser_off
                down_offset = winner_off
        else:
            # No velocity signal - use symmetric
            up_offset = (winner_off + loser_off) / 2
            down_offset = up_offset

        up_bid = round(entry_row['up_bid'] + up_offset, 2)
        down_bid = round(entry_row['down_bid'] + down_offset, 2)
        up_bid = max(0.01, min(0.95, up_bid))
        down_bid = max(0.01, min(0.95, down_bid))

        post_entry = mdf.iloc[entry_idx:]
        up_min_ask = post_entry['up_ask'].min()
        down_min_ask = post_entry['down_ask'].min()

        up_filled = up_min_ask <= up_bid
        down_filled = down_min_ask <= down_bid

        if up_filled and down_filled:
            pair_cost = up_bid + down_bid
            pnl = (1.0 - pair_cost) * SHARES_PER_SIDE
            total_pnl += pnl
            hedged_count += 1
            pair_costs.append(pair_cost)

    hedge_rate = hedged_count / len(complete) if complete else 0
    avg_pair_cost = np.mean(pair_costs) if pair_costs else 0

    return {
        'hedged': hedged_count,
        'hedge_rate': hedge_rate,
        'avg_pair_cost': avg_pair_cost,
        'total_pnl': total_pnl,
        'pnl_per_market': total_pnl / len(complete) if complete else 0,
    }


def main():
    print("="*80)
    print("VELOCITY-ASSISTED MARKET MAKING")
    print("="*80)
    print("\nIdea: Use velocity to HELP hedging")
    print("  - Winner side (rising): bid aggressive to fill before price goes up")
    print("  - Loser side (falling): bid passive, price will drop to us")

    main_file = '/Users/rananjaybika/polymarket-amm-bot/research/observer/spread_capture_obs_20260115.csv'

    df = pd.read_csv(main_file, on_bad_lines='skip')
    markets = df['market_slug'].unique()
    complete = [s for s in markets
                if df[df['market_slug']==s].iloc[0]['time_remaining_secs'] >= 800
                and df[df['market_slug']==s].iloc[-1]['time_remaining_secs'] <= 60]

    print(f"\nComplete markets: {len(complete)}")

    # Test configurations
    print(f"\n{'Config':<20} {'W_Off':>6} {'L_Off':>6} {'Hedge':>6} {'AvgCost':>10} {'PnL':>10}")
    print("-" * 65)

    # Baseline: symmetric -0.05
    baseline = test_config(df, complete, {'winner_off': -0.05, 'loser_off': -0.05})
    print(f"{'baseline (-0.05)':<20} {-0.05:>+6.2f} {-0.05:>+6.2f} "
          f"{100*baseline['hedge_rate']:>5.1f}% ${baseline['avg_pair_cost']:>8.4f} ${baseline['total_pnl']:>8.2f}")

    for name, config in CONFIGS.items():
        r = test_config(df, complete, config)
        print(f"{name:<20} {config['winner_off']:>+6.2f} {config['loser_off']:>+6.2f} "
              f"{100*r['hedge_rate']:>5.1f}% ${r['avg_pair_cost']:>8.4f} ${r['total_pnl']:>8.2f}")

    # Find best
    all_results = [(name, test_config(df, complete, config)) for name, config in CONFIGS.items()]
    all_results.append(('baseline', baseline))
    best_name, best = max(all_results, key=lambda x: x[1]['total_pnl'])

    print(f"\n{'='*65}")
    print(f"BEST: {best_name}")
    print(f"  Hedge rate: {100*best['hedge_rate']:.1f}%")
    print(f"  Avg pair cost: ${best['avg_pair_cost']:.4f}")
    print(f"  Total PnL: ${best['total_pnl']:.2f}")
    print(f"  Per market: ${best['pnl_per_market']:.2f}")

    hours = len(complete) * 15 / 60
    print(f"  Estimated hourly: ${best['total_pnl']/hours:.2f}/hr")


if __name__ == "__main__":
    main()
