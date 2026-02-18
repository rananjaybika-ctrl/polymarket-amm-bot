#!/usr/bin/env python3
"""
PHOENIX V1 — Spike Density Analysis

Counts qualifying entry signals per market across training datasets.
Key question: Is there enough signal density to justify multiple entries (cycling)?

Approach: Import data loading and spike detection directly from phoenix_v1_grid_search.py.
Analyzes:
  - Total EWMA spikes per market
  - Spikes in entry window (300-180s, expensive_ask >= 0.80)
  - Spikes with deceleration detected
  - Time gaps between consecutive qualifying spikes
  - Potential extra entries under the best config
"""

import sys
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# ---- Import everything from the grid search — no re-implementation ----
from research.backtests.phoenix_v1_grid_search import (
    load_dataset,
    precompute_spikes_ewma,
    precompute_markets,
    compute_deceleration,
    TRAIN_DATASETS,
    DATASETS,
    SKIP_UTC_HOURS,
    EWMA_HALFLIFE_MS,
    DECEL_WINDOWS,
)

# ============================================================
# ANALYSIS CONFIG — best config from grid search results
# ============================================================
ENTRY_START_SECS = 300.0   # from T80_W300-180_O3_DC_PC96_S15_DD
ENTRY_END_SECS   = 180.0
EXPENSIVE_THRESHOLD = 0.80
COOLDOWN_SECONDS = 10

# ============================================================
# HELPER: count qualifying spikes in one market
# ============================================================
def analyze_market_spikes(slug, md):
    """
    Returns dict with spike counts and time-gap stats for one market.
    md: precomputed market dict from precompute_markets()
    """
    ts        = md['ts']
    up_ask    = md['up_ask']
    down_ask  = md['down_ask']
    time_rem  = md['time_rem']
    hours     = md['hours']
    spike_ts  = md['spike_ts']
    spike_mag = md['spike_mag']
    spike_obs = md['spike_obs_idx']

    total_spikes = len(spike_ts)

    # Deceleration for the best-config window
    window_key = (int(ENTRY_START_SECS), int(ENTRY_END_SECS))
    decel_flag = md['decel'].get(window_key, False)

    qualifying_basic = []     # In-window + bias threshold (no decel filter)
    qualifying_decel = []     # In-window + bias threshold + decel detected

    cooldown_ms = COOLDOWN_SECONDS * 1000
    last_signal_ts = 0

    for si in range(total_spikes):
        oi = spike_obs[si]
        tr = time_rem[oi]

        # Entry window
        if tr > ENTRY_START_SECS or tr < ENTRY_END_SECS:
            continue

        # Hour filter (same as grid search)
        if hours[oi] in SKIP_UTC_HOURS:
            continue

        # Price validity
        ua, da = up_ask[oi], down_ask[oi]
        if np.isnan(ua) or np.isnan(da) or ua <= 0 or da <= 0:
            continue

        # Expensive side & bias threshold
        exp_ask = max(ua, da)
        if exp_ask < EXPENSIVE_THRESHOLD:
            continue

        # Cooldown
        if spike_ts[si] - last_signal_ts < cooldown_ms:
            continue

        last_signal_ts = spike_ts[si]
        t_ms = spike_ts[si]

        qualifying_basic.append(t_ms)

        if decel_flag:
            qualifying_decel.append(t_ms)

    # Time gaps between consecutive qualifying spikes (basic filter)
    gaps_secs = []
    if len(qualifying_basic) >= 2:
        arr = np.array(qualifying_basic)
        gaps_secs = ((arr[1:] - arr[:-1]) / 1000.0).tolist()

    return {
        'total_spikes': total_spikes,
        'qualifying_basic': len(qualifying_basic),
        'qualifying_decel': len(qualifying_decel),
        'gaps_secs': gaps_secs,
        'decel_present': decel_flag,
    }


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("PHOENIX V1 — SPIKE DENSITY ANALYSIS")
    print(f"Entry window: {int(ENTRY_START_SECS)}-{int(ENTRY_END_SECS)}s remaining")
    print(f"Bias threshold: expensive_ask >= {EXPENSIVE_THRESHOLD}")
    print(f"Training datasets: {TRAIN_DATASETS}")
    print("=" * 70)

    all_stats = []   # one row per market per dataset

    for ds_key in TRAIN_DATASETS:
        print(f"\n{'='*60}")
        print(f"DATASET: {ds_key}")
        print(f"{'='*60}")

        obs_df, btc_df, resolutions, duration_h = load_dataset(ds_key)
        if obs_df is None:
            print(f"  SKIP — could not load {ds_key}")
            continue

        # ---- Spike detection (same as grid search) ----
        print("  Running EWMA spike detection on Binance HF data...")
        spike_df = precompute_spikes_ewma(btc_df, halflife_ms=EWMA_HALFLIFE_MS)
        spk = spike_df[spike_df['spike_detected']]
        spike_ts_all  = spk['timestamp_ms'].values.astype(np.int64)
        spike_mag_all = spk['spike_magnitude'].values.astype(float)
        if len(spike_ts_all) > 0 and not np.all(np.diff(spike_ts_all) >= 0):
            order = np.argsort(spike_ts_all)
            spike_ts_all  = spike_ts_all[order]
            spike_mag_all = spike_mag_all[order]

        print(f"  Total Binance spikes detected: {len(spike_ts_all):,}")

        # ---- Market precomputation ----
        print("  Precomputing market arrays...")
        market_data = precompute_markets(obs_df, spike_ts_all, spike_mag_all, resolutions)
        print(f"  Markets with resolutions: {len(market_data)}")

        # ---- Per-market analysis ----
        for slug, md in market_data.items():
            stats = analyze_market_spikes(slug, md)
            stats['slug'] = slug
            stats['dataset'] = ds_key
            all_stats.append(stats)

    # ============================================================
    # AGGREGATE STATISTICS
    # ============================================================
    if not all_stats:
        print("\nNo data collected.")
        return

    df = pd.DataFrame(all_stats)

    print("\n" + "=" * 70)
    print("AGGREGATE RESULTS ACROSS ALL TRAINING DATASETS")
    print("=" * 70)

    total_markets = len(df)
    print(f"\nTotal markets analysed: {total_markets}")

    # --- Total spikes ---
    print("\n--- TOTAL EWMA SPIKES PER MARKET ---")
    ts_desc = df['total_spikes'].describe(percentiles=[0.25, 0.5, 0.75])
    print(f"  Mean   : {ts_desc['mean']:.1f}")
    print(f"  Median : {ts_desc['50%']:.1f}")
    print(f"  p25    : {ts_desc['25%']:.1f}")
    print(f"  p75    : {ts_desc['75%']:.1f}")
    print(f"  Max    : {ts_desc['max']:.0f}")

    # --- Qualifying spikes (basic: window + threshold) ---
    print("\n--- QUALIFYING SPIKES PER MARKET (window + bias threshold, no decel) ---")
    qb_desc = df['qualifying_basic'].describe(percentiles=[0.25, 0.5, 0.75])
    print(f"  Mean   : {qb_desc['mean']:.2f}")
    print(f"  Median : {qb_desc['50%']:.2f}")
    print(f"  p25    : {qb_desc['25%']:.2f}")
    print(f"  p75    : {qb_desc['75%']:.2f}")
    print(f"  Max    : {qb_desc['max']:.0f}")

    n_zero   = (df['qualifying_basic'] == 0).sum()
    n_one    = (df['qualifying_basic'] == 1).sum()
    n_two_p  = (df['qualifying_basic'] >= 2).sum()
    n_three_p = (df['qualifying_basic'] >= 3).sum()
    n_five_p = (df['qualifying_basic'] >= 5).sum()

    print(f"\n  Markets with 0 qualifying spikes : {n_zero}  ({100*n_zero/total_markets:.1f}%)")
    print(f"  Markets with 1 qualifying spike  : {n_one}  ({100*n_one/total_markets:.1f}%)")
    print(f"  Markets with 2+ qualifying spikes: {n_two_p}  ({100*n_two_p/total_markets:.1f}%)")
    print(f"  Markets with 3+ qualifying spikes: {n_three_p}  ({100*n_three_p/total_markets:.1f}%)")
    print(f"  Markets with 5+ qualifying spikes: {n_five_p}  ({100*n_five_p/total_markets:.1f}%)")

    # --- Qualifying spikes with decel ---
    print("\n--- QUALIFYING SPIKES WITH DECELERATION FILTER ---")
    qd_desc = df['qualifying_decel'].describe(percentiles=[0.25, 0.5, 0.75])
    print(f"  Mean   : {qd_desc['mean']:.2f}")
    print(f"  Median : {qd_desc['50%']:.2f}")
    print(f"  p25    : {qd_desc['25%']:.2f}")
    print(f"  p75    : {qd_desc['75%']:.2f}")
    print(f"  Max    : {qd_desc['max']:.0f}")

    n_decel_two_p  = (df['qualifying_decel'] >= 2).sum()
    n_decel_three_p = (df['qualifying_decel'] >= 3).sum()
    print(f"\n  Markets with 2+ (with decel): {n_decel_two_p}  ({100*n_decel_two_p/total_markets:.1f}%)")
    print(f"  Markets with 3+ (with decel): {n_decel_three_p}  ({100*n_decel_three_p/total_markets:.1f}%)")

    # --- Time gaps between consecutive qualifying spikes ---
    all_gaps = []
    for row in all_stats:
        all_gaps.extend(row['gaps_secs'])

    print("\n--- TIME GAPS BETWEEN CONSECUTIVE QUALIFYING SPIKES (seconds) ---")
    if all_gaps:
        gaps_arr = np.array(all_gaps)
        print(f"  Total gap measurements: {len(gaps_arr)}")
        print(f"  Mean gap   : {gaps_arr.mean():.1f}s")
        print(f"  Median gap : {np.median(gaps_arr):.1f}s")
        print(f"  p25        : {np.percentile(gaps_arr, 25):.1f}s")
        print(f"  p75        : {np.percentile(gaps_arr, 75):.1f}s")
        print(f"  Min        : {gaps_arr.min():.1f}s")
        print(f"  Max        : {gaps_arr.max():.1f}s")

        entry_window_secs = ENTRY_START_SECS - ENTRY_END_SECS  # 120s
        pct_within_window = (gaps_arr < entry_window_secs).mean() * 100
        print(f"\n  Gaps < {entry_window_secs:.0f}s (within entry window): {pct_within_window:.1f}%")
        print(f"  => These spikes are close enough to both be in the same 120s entry window")
    else:
        print("  No gaps to measure (no markets had 2+ qualifying spikes)")

    # --- Cycling extra entries potential (best config) ---
    print("\n--- EXTRA ENTRIES POTENTIAL UNDER BEST CONFIG (T80_W300-180_O3_DC_PC96_S15_DD) ---")
    # Current: max 1 entry per market (double_down counts as 2 but same spike)
    # Cycling: each qualifying spike is a fresh entry
    # Extra entries = qualifying_basic - 1 (capped at 0 min)
    df['extra_entries'] = (df['qualifying_basic'] - 1).clip(lower=0)
    total_extra = df['extra_entries'].sum()
    total_base  = (df['qualifying_basic'] >= 1).sum()
    print(f"\n  Markets with at least 1 qualifying spike : {total_base}")
    print(f"  Total extra entries possible via cycling : {int(total_extra)}")
    if total_base > 0:
        print(f"  Avg extra entries per entered market     : {total_extra / total_base:.2f}")

    # Distribution of qualifying spikes for markets that DO get at least 1
    active = df[df['qualifying_basic'] >= 1]
    print(f"\n  Among markets with >= 1 qualifying spike:")
    print(f"    Avg qualifying spikes : {active['qualifying_basic'].mean():.2f}")
    print(f"    Median                : {active['qualifying_basic'].median():.2f}")
    print(f"    p75                   : {active['qualifying_basic'].quantile(0.75):.2f}")
    print(f"    Max                   : {active['qualifying_basic'].max():.0f}")

    # --- Per-dataset breakdown ---
    print("\n--- PER-DATASET BREAKDOWN ---")
    for ds_key in TRAIN_DATASETS:
        sub = df[df['dataset'] == ds_key]
        if sub.empty:
            continue
        print(f"\n  {ds_key}:")
        print(f"    Markets: {len(sub)}")
        print(f"    Avg qualifying spikes (basic): {sub['qualifying_basic'].mean():.2f}")
        print(f"    Markets with 2+ spikes       : {(sub['qualifying_basic'] >= 2).sum()}")
        print(f"    Markets with 3+ spikes       : {(sub['qualifying_basic'] >= 3).sum()}")

    # --- Top markets (most qualifying spikes) ---
    print("\n--- TOP 20 MARKETS BY QUALIFYING SPIKES ---")
    top = df.nlargest(20, 'qualifying_basic')[['slug', 'dataset', 'qualifying_basic', 'qualifying_decel', 'total_spikes']].reset_index(drop=True)
    print(top.to_string(index=False))

    # --- Conclusion ---
    print("\n" + "=" * 70)
    print("CONCLUSION: IS CYCLING FEASIBLE?")
    print("=" * 70)
    pct_multi = 100 * n_two_p / total_markets if total_markets > 0 else 0
    avg_extra = total_extra / total_base if total_base > 0 else 0
    print(f"\n  {pct_multi:.1f}% of markets have 2+ qualifying spikes (without decel filter)")
    print(f"  Average extra entries per active market: {avg_extra:.2f}")
    if pct_multi < 20:
        print("\n  => SIGNAL DENSITY IS LOW. Most markets have at most 1 qualifying spike.")
        print("     Cycling would add very few extra entries and risk over-trading.")
    elif pct_multi < 40:
        print("\n  => MODERATE signal density. Cycling adds some entries but coverage is patchy.")
        print("     Cycling may be viable with tight controls (same-market cap, cooldown).")
    else:
        print("\n  => HIGH signal density. Cycling is feasible; multiple entries per market common.")

    print()


if __name__ == "__main__":
    main()
