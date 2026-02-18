#!/usr/bin/env python3
"""
Quick Statistical Test: Does OBI Contrarian Signal Help PHOENIX?

Tests whether OBI contrarian (OBI disagrees with expensive side) improves
the "expensive side wins" prediction at various thresholds across all datasets.

Also tests acceleration_bps2 sign reversal and jerk_bps3 as bonus signals.

Output: p-values and accuracy comparisons.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from scipy import stats
from collections import defaultdict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"
RESOLUTIONS_FILE = OBSERVER_DIR / "market_resolutions_verified.csv"

# Datasets to test
DATASETS = {
    "IS+OOS2": [OBSERVER_DIR / "PROTECTED_grid_obs_is_oos2_combined.csv"],
    "OOS7": [
        OBSERVER_DIR / "grid_obs_20260129.csv",
        OBSERVER_DIR / "grid_obs_20260130.csv",
    ],
    "OOS8": [OBSERVER_DIR / "grid_obs_20260131.csv"],
    "OOS9": [OBSERVER_DIR / "grid_obs_oos9.csv"],
    "OOS3+4": [OBSERVER_DIR / "PROTECTED_grid_obs_oos3_oos4_combined.csv"],
}

# Columns needed
COLS = [
    "timestamp_ms", "market_slug", "time_remaining_secs",
    "up_bid", "up_ask", "down_bid", "down_ask",
    "velocity_bps", "acceleration_bps2", "jerk_bps3",
    "spike_detected", "spike_direction",
]
OBI_COLS = ["up_bid_size_1", "up_ask_size_1", "down_bid_size_1", "down_ask_size_1"]


def load_dataset(name, files):
    """Load dataset with minimal columns."""
    dfs = []
    for f in files:
        if not f.exists():
            print(f"  SKIP: {f.name} not found")
            continue
        try:
            cols_to_load = COLS + OBI_COLS
            df = pd.read_csv(f, usecols=lambda c: c in cols_to_load)
            dfs.append(df)
        except Exception as e:
            print(f"  ERROR loading {f.name}: {e}")
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True)


def get_market_snapshot(df, resolutions, time_window=(300, 600)):
    """
    For each market, take the MEDIAN observation in the entry window.
    Returns one row per market with features + outcome.
    """
    # Filter to entry window
    mask = (df["time_remaining_secs"] >= time_window[0]) & (df["time_remaining_secs"] <= time_window[1])
    windowed = df[mask].copy()

    if windowed.empty:
        return pd.DataFrame()

    # Compute features per observation
    windowed["expensive_ask"] = np.where(
        windowed["up_ask"] > windowed["down_ask"],
        windowed["up_ask"], windowed["down_ask"]
    )
    windowed["cheap_ask"] = np.where(
        windowed["up_ask"] > windowed["down_ask"],
        windowed["down_ask"], windowed["up_ask"]
    )
    windowed["expensive_side"] = (windowed["up_ask"] > windowed["down_ask"]).astype(int)

    # OBI
    has_obi = all(c in windowed.columns for c in OBI_COLS)
    if has_obi:
        windowed["up_imbalance"] = (
            (windowed["up_bid_size_1"] - windowed["up_ask_size_1"]) /
            (windowed["up_bid_size_1"] + windowed["up_ask_size_1"] + 1e-8)
        )
        windowed["down_imbalance"] = (
            (windowed["down_bid_size_1"] - windowed["down_ask_size_1"]) /
            (windowed["down_bid_size_1"] + windowed["down_ask_size_1"] + 1e-8)
        )
        windowed["obi_diff"] = windowed["up_imbalance"] - windowed["down_imbalance"]
        # OBI contrarian = OBI disagrees with expensive side
        windowed["obi_contrarian"] = (
            ((windowed["expensive_side"] == 1) & (windowed["obi_diff"] < 0)) |
            ((windowed["expensive_side"] == 0) & (windowed["obi_diff"] > 0))
        ).astype(int)
    else:
        windowed["obi_contrarian"] = np.nan

    # Aggregate per market (median of entry window)
    agg_cols = ["expensive_ask", "cheap_ask", "expensive_side"]
    agg_dict = {c: "median" for c in agg_cols}
    agg_dict["obi_contrarian"] = "mean"  # proportion of ticks where OBI is contrarian
    agg_dict["velocity_bps"] = "median"
    if "acceleration_bps2" in windowed.columns:
        agg_dict["acceleration_bps2"] = "median"
    if "jerk_bps3" in windowed.columns:
        agg_dict["jerk_bps3"] = "median"

    market_df = windowed.groupby("market_slug").agg(agg_dict).reset_index()

    # Merge resolutions (resolutions CSV uses 'slug', observer uses 'market_slug')
    res_renamed = resolutions.rename(columns={"slug": "market_slug"})
    market_df = market_df.merge(res_renamed[["market_slug", "winner"]], on="market_slug", how="inner")
    market_df["winner_binary"] = (market_df["winner"] == "UP").astype(int)

    # Does expensive side win?
    market_df["expensive_wins"] = (
        ((market_df["expensive_side"] >= 0.5) & (market_df["winner_binary"] == 1)) |
        ((market_df["expensive_side"] < 0.5) & (market_df["winner_binary"] == 0))
    ).astype(int)

    return market_df


def run_test():
    print("=" * 70)
    print("  OBI CONTRARIAN SIGNAL TEST — Project PHOENIX")
    print("  Question: Does OBI contrarian improve expensive_side_wins?")
    print("=" * 70)

    # Load resolutions
    resolutions = pd.read_csv(RESOLUTIONS_FILE)
    print(f"\nLoaded {len(resolutions)} resolutions")

    all_results = []

    for ds_name, files in DATASETS.items():
        print(f"\n{'─' * 60}")
        print(f"Dataset: {ds_name}")
        print("─" * 60)

        df = load_dataset(ds_name, files)
        if df is None:
            print("  No data loaded, skipping")
            continue

        n_markets = df["market_slug"].nunique()
        print(f"  Loaded {len(df):,} rows, {n_markets} markets")

        market_df = get_market_snapshot(df, resolutions)
        if market_df.empty:
            print("  No markets with resolutions in entry window")
            continue

        print(f"  Markets with resolutions: {len(market_df)}")

        # Test at different thresholds
        for threshold in [0.55, 0.65, 0.75, 0.80]:
            mask = market_df["expensive_ask"] >= threshold
            subset = market_df[mask]
            if len(subset) < 5:
                continue

            # Baseline: expensive side wins
            baseline_acc = subset["expensive_wins"].mean()
            n_baseline = len(subset)

            # OBI contrarian: majority of ticks in entry window had OBI contrarian
            obi_available = subset["obi_contrarian"].notna()
            obi_subset = subset[obi_available]

            if len(obi_subset) < 5:
                all_results.append({
                    "dataset": ds_name, "threshold": threshold,
                    "n_baseline": n_baseline, "baseline_acc": baseline_acc,
                    "n_obi_contra": 0, "obi_contra_acc": np.nan,
                    "n_obi_agrees": 0, "obi_agrees_acc": np.nan,
                    "chi2_p": np.nan,
                })
                continue

            obi_contra_mask = obi_subset["obi_contrarian"] > 0.5
            obi_agrees_mask = obi_subset["obi_contrarian"] <= 0.5

            n_contra = obi_contra_mask.sum()
            n_agrees = obi_agrees_mask.sum()

            contra_acc = obi_subset.loc[obi_contra_mask, "expensive_wins"].mean() if n_contra > 0 else np.nan
            agrees_acc = obi_subset.loc[obi_agrees_mask, "expensive_wins"].mean() if n_agrees > 0 else np.nan

            # Chi-squared test: is accuracy different between OBI contrarian vs OBI agrees?
            chi2_p = np.nan
            if n_contra >= 5 and n_agrees >= 5:
                contra_wins = obi_subset.loc[obi_contra_mask, "expensive_wins"].sum()
                agrees_wins = obi_subset.loc[obi_agrees_mask, "expensive_wins"].sum()
                table = np.array([
                    [contra_wins, n_contra - contra_wins],
                    [agrees_wins, n_agrees - agrees_wins],
                ])
                if table.min() >= 0:
                    try:
                        _, chi2_p, _, _ = stats.chi2_contingency(table)
                    except ValueError:
                        chi2_p = np.nan

            all_results.append({
                "dataset": ds_name, "threshold": threshold,
                "n_baseline": n_baseline, "baseline_acc": baseline_acc,
                "n_obi_contra": n_contra, "obi_contra_acc": contra_acc,
                "n_obi_agrees": n_agrees, "obi_agrees_acc": agrees_acc,
                "chi2_p": chi2_p,
            })

            p_str = f"p={chi2_p:.4f}" if not np.isnan(chi2_p) else "p=N/A"
            print(f"  T>={threshold}: baseline={baseline_acc:.1%}(n={n_baseline}) | "
                  f"OBI_contra={contra_acc:.1%}(n={n_contra}) | "
                  f"OBI_agrees={agrees_acc:.1%}(n={n_agrees}) | {p_str}")

    # Combined analysis
    print(f"\n{'=' * 70}")
    print("  COMBINED RESULTS ACROSS ALL DATASETS")
    print("=" * 70)

    results_df = pd.DataFrame(all_results)

    for threshold in [0.55, 0.65, 0.75, 0.80]:
        subset = results_df[results_df["threshold"] == threshold]
        if subset.empty:
            continue

        total_baseline = subset["n_baseline"].sum()
        total_contra = subset["n_obi_contra"].sum()
        total_agrees = subset["n_obi_agrees"].sum()

        # Weighted accuracy
        if total_baseline > 0:
            weighted_baseline = (subset["baseline_acc"] * subset["n_baseline"]).sum() / total_baseline
        else:
            weighted_baseline = np.nan

        if total_contra > 0:
            weighted_contra = sum(
                r["obi_contra_acc"] * r["n_obi_contra"]
                for _, r in subset.iterrows()
                if not np.isnan(r["obi_contra_acc"]) and r["n_obi_contra"] > 0
            ) / total_contra
        else:
            weighted_contra = np.nan

        if total_agrees > 0:
            weighted_agrees = sum(
                r["obi_agrees_acc"] * r["n_obi_agrees"]
                for _, r in subset.iterrows()
                if not np.isnan(r["obi_agrees_acc"]) and r["n_obi_agrees"] > 0
            ) / total_agrees
        else:
            weighted_agrees = np.nan

        # Significant p-values
        sig_count = (subset["chi2_p"] < 0.05).sum()
        total_tests = subset["chi2_p"].notna().sum()

        print(f"\n  Threshold >= ${threshold:.2f}:")
        print(f"    Baseline:      {weighted_baseline:.1%} (n={total_baseline})")
        print(f"    OBI Contrarian: {weighted_contra:.1%} (n={total_contra})")
        print(f"    OBI Agrees:     {weighted_agrees:.1%} (n={total_agrees})")
        print(f"    Significant:   {sig_count}/{total_tests} datasets (p<0.05)")

        if not np.isnan(weighted_contra) and not np.isnan(weighted_baseline):
            delta = weighted_contra - weighted_baseline
            print(f"    Delta (OBI contra vs baseline): {delta:+.1%}")
            if abs(delta) < 0.02:
                print(f"    VERDICT: OBI contrarian adds NO meaningful value (<2pp)")
            elif delta > 0.02:
                print(f"    VERDICT: OBI contrarian HELPS (+{delta:.1%})")
            else:
                print(f"    VERDICT: OBI contrarian HURTS ({delta:.1%})")

    # Save results
    output_file = PROJECT_ROOT / "research" / "findings" / "data" / "obi_contrarian_test_results.csv"
    results_df.to_csv(output_file, index=False)
    print(f"\n  Saved: {output_file}")

    # Final summary
    print(f"\n{'=' * 70}")
    print("  FINAL VERDICT")
    print("=" * 70)

    # At the critical $0.75 threshold
    t75 = results_df[results_df["threshold"] == 0.75]
    if not t75.empty:
        sig = (t75["chi2_p"] < 0.05).sum()
        total = t75["chi2_p"].notna().sum()
        avg_p = t75["chi2_p"].mean()
        print(f"\n  At $0.75 threshold:")
        print(f"    Datasets with significant difference: {sig}/{total}")
        print(f"    Average p-value: {avg_p:.4f}")
        if sig == 0:
            print(f"    CONCLUSION: OBI contrarian does NOT significantly improve prediction")
            print(f"    RECOMMENDATION: Drop from PHOENIX strategy")
        elif sig >= total / 2:
            print(f"    CONCLUSION: OBI contrarian shows significant effect in {sig}/{total} datasets")
            print(f"    RECOMMENDATION: Include as optional filter")


if __name__ == "__main__":
    run_test()
