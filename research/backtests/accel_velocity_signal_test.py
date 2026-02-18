#!/usr/bin/env python3
"""
Signal Test: Acceleration, Jerk, and Velocity Reformulations for PHOENIX

Copied from obi_contrarian_test.py (validated execution engine).
Same data loading, resolution merging, entry window filtering.

Tests:
1. Acceleration sign reversal count → does it predict winner?
2. Acceleration direction (toward/away from expensive side)
3. Jerk direction (toward/away from expensive side)
4. Velocity reformulations:
   a. Velocity direction relative to expensive side
   b. Velocity magnitude (high vs low)
   c. Deceleration (velocity magnitude drops in entry window)
5. Spike + Deceleration combo vs spike alone

Output: p-values and accuracy comparisons per threshold.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from scipy import stats
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

PROJECT_ROOT = Path(__file__).parent.parent.parent
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"
RESOLUTIONS_FILE = OBSERVER_DIR / "market_resolutions_verified.csv"

# Datasets with acceleration/jerk/velocity columns
DATASETS = {
    "OOS7": [
        OBSERVER_DIR / "grid_obs_20260129.csv",
        OBSERVER_DIR / "grid_obs_20260130.csv",
    ],
    "OOS8": [OBSERVER_DIR / "grid_obs_20260131.csv"],
    "OOS9": [OBSERVER_DIR / "grid_obs_oos9.csv"],
}

COLS = [
    "timestamp_ms", "market_slug", "time_remaining_secs",
    "up_bid", "up_ask", "down_bid", "down_ask",
    "velocity_bps", "acceleration_bps2", "jerk_bps3",
    "spike_detected", "spike_direction", "spike_magnitude",
]


def load_dataset(name, files):
    """Load dataset with minimal columns."""
    dfs = []
    for f in files:
        if not f.exists():
            print(f"  SKIP: {f.name} not found")
            continue
        try:
            df = pd.read_csv(f, usecols=lambda c: c in COLS)
            dfs.append(df)
        except Exception as e:
            print(f"  ERROR loading {f.name}: {e}")
    if not dfs:
        return None
    return pd.concat(dfs, ignore_index=True)


def chi2_test(group_a_wins, group_a_total, group_b_wins, group_b_total):
    """Run chi-squared test between two groups. Returns p-value."""
    if group_a_total < 5 or group_b_total < 5:
        return np.nan
    table = np.array([
        [group_a_wins, group_a_total - group_a_wins],
        [group_b_wins, group_b_total - group_b_wins],
    ])
    if table.min() < 0:
        return np.nan
    try:
        _, p, _, _ = stats.chi2_contingency(table)
        return p
    except ValueError:
        return np.nan


def compute_market_features(df, resolutions, time_window=(300, 600)):
    """
    For each market, compute signal features in the entry window.
    Returns one row per market with features + outcome.
    """
    mask = (df["time_remaining_secs"] >= time_window[0]) & (df["time_remaining_secs"] <= time_window[1])
    windowed = df[mask].copy()
    if windowed.empty:
        return pd.DataFrame()

    # Core price features
    windowed["expensive_ask"] = np.where(
        windowed["up_ask"] > windowed["down_ask"],
        windowed["up_ask"], windowed["down_ask"]
    )
    windowed["expensive_side"] = (windowed["up_ask"] > windowed["down_ask"]).astype(int)

    # Velocity direction relative to expensive side
    windowed["velocity_toward_expensive"] = np.where(
        windowed["expensive_side"] == 1,
        windowed["velocity_bps"],
        -windowed["velocity_bps"]
    )

    # Acceleration/jerk relative to expensive side
    if "acceleration_bps2" in windowed.columns:
        windowed["accel_toward_expensive"] = np.where(
            windowed["expensive_side"] == 1,
            windowed["acceleration_bps2"],
            -windowed["acceleration_bps2"]
        )
    if "jerk_bps3" in windowed.columns:
        windowed["jerk_toward_expensive"] = np.where(
            windowed["expensive_side"] == 1,
            windowed["jerk_bps3"],
            -windowed["jerk_bps3"]
        )

    windowed["has_spike"] = windowed["spike_detected"].fillna(0).astype(bool)

    # Per-market custom aggregation
    results = []
    for slug, group in windowed.groupby("market_slug"):
        row = {"market_slug": slug}

        row["expensive_ask_median"] = group["expensive_ask"].median()
        row["expensive_side_median"] = group["expensive_side"].median()
        row["n_obs"] = len(group)

        # Velocity features
        row["velocity_median"] = group["velocity_bps"].median()
        row["velocity_abs_median"] = np.abs(group["velocity_bps"]).median()
        row["velocity_toward_exp_median"] = group["velocity_toward_expensive"].median()

        # Acceleration features
        if "acceleration_bps2" in group.columns:
            accel = group["acceleration_bps2"].dropna()
            row["accel_median"] = accel.median() if len(accel) > 0 else np.nan
            row["accel_toward_exp_median"] = group["accel_toward_expensive"].dropna().median() if len(accel) > 0 else np.nan
            # Count sign reversals
            if len(accel) >= 2:
                signs = np.sign(accel.values)
                row["accel_reversals"] = int(np.sum(signs[1:] != signs[:-1]))
            else:
                row["accel_reversals"] = 0
        else:
            row["accel_median"] = np.nan
            row["accel_toward_exp_median"] = np.nan
            row["accel_reversals"] = 0

        # Jerk features
        if "jerk_bps3" in group.columns:
            jerk = group["jerk_toward_expensive"].dropna()
            row["jerk_toward_exp_median"] = jerk.median() if len(jerk) > 0 else np.nan
        else:
            row["jerk_toward_exp_median"] = np.nan

        # Spike
        row["has_spike"] = group["has_spike"].any()

        # Deceleration: velocity magnitude drops from first half to second half
        vel = group["velocity_bps"].dropna()
        if len(vel) >= 6:
            mid = len(vel) // 2
            first_mag = np.abs(vel.iloc[:mid]).mean()
            second_mag = np.abs(vel.iloc[mid:]).mean()
            row["decel_detected"] = second_mag < first_mag * 0.7  # 30%+ drop
            row["vel_first_half"] = first_mag
            row["vel_second_half"] = second_mag
        else:
            row["decel_detected"] = False
            row["vel_first_half"] = np.nan
            row["vel_second_half"] = np.nan

        results.append(row)

    market_df = pd.DataFrame(results)

    # Merge resolutions
    res_renamed = resolutions.rename(columns={"slug": "market_slug"})
    market_df = market_df.merge(res_renamed[["market_slug", "winner"]], on="market_slug", how="inner")
    market_df["winner_binary"] = (market_df["winner"] == "UP").astype(int)

    # Does expensive side win?
    market_df["expensive_wins"] = (
        ((market_df["expensive_side_median"] >= 0.5) & (market_df["winner_binary"] == 1)) |
        ((market_df["expensive_side_median"] < 0.5) & (market_df["winner_binary"] == 0))
    ).astype(int)

    return market_df


def print_split_test(label, subset, split_col, split_val=0, split_type="median"):
    """Print accuracy split test results."""
    baseline_acc = subset["expensive_wins"].mean()

    if split_type == "zero":
        group_a = subset[subset[split_col] > split_val]
        group_b = subset[subset[split_col] <= split_val]
        label_a, label_b = f"{split_col} > 0", f"{split_col} <= 0"
    elif split_type == "median":
        med = subset[split_col].median()
        group_a = subset[subset[split_col] > med]
        group_b = subset[subset[split_col] <= med]
        label_a, label_b = f"> median({med:.3f})", f"<= median({med:.3f})"
    elif split_type == "bool":
        group_a = subset[subset[split_col] == True]
        group_b = subset[subset[split_col] == False]
        label_a, label_b = "True", "False"
    else:
        return

    a_acc = group_a["expensive_wins"].mean() if len(group_a) > 0 else np.nan
    b_acc = group_b["expensive_wins"].mean() if len(group_b) > 0 else np.nan

    p = chi2_test(
        int(group_a["expensive_wins"].sum()), len(group_a),
        int(group_b["expensive_wins"].sum()), len(group_b),
    )

    p_str = f"p={p:.4f}" if not np.isnan(p) else "p=N/A"
    sig = " ***" if not np.isnan(p) and p < 0.05 else ""

    print(f"    {label}: baseline={baseline_acc:.1%}(n={len(subset)})")
    print(f"      {label_a}: {a_acc:.1%}(n={len(group_a)})")
    print(f"      {label_b}: {b_acc:.1%}(n={len(group_b)})")
    print(f"      {p_str}{sig}")

    return {
        "label": label,
        "baseline_acc": baseline_acc, "n": len(subset),
        "a_acc": a_acc, "n_a": len(group_a),
        "b_acc": b_acc, "n_b": len(group_b),
        "p": p,
    }


def run_test():
    print("=" * 70)
    print("  SIGNAL TEST: Acceleration, Jerk, Velocity Reformulations")
    print("  Project PHOENIX — Session 2")
    print("=" * 70)

    resolutions = pd.read_csv(RESOLUTIONS_FILE)
    print(f"\nLoaded {len(resolutions)} resolutions")

    all_market_dfs = []

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

        market_df = compute_market_features(df, resolutions)
        if market_df.empty:
            print("  No markets with resolutions in entry window")
            continue

        market_df["dataset"] = ds_name
        all_market_dfs.append(market_df)
        print(f"  Markets with resolutions: {len(market_df)}")

    if not all_market_dfs:
        print("\nNo data to analyze!")
        return

    combined = pd.concat(all_market_dfs, ignore_index=True)
    print(f"\n{'=' * 70}")
    print(f"  COMBINED: {len(combined)} markets across {combined['dataset'].nunique()} datasets")
    print("=" * 70)

    thresholds = [0.55, 0.65, 0.75, 0.80]
    all_results = []

    # ── TEST 1: Acceleration sign reversal count ───────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 1: ACCELERATION SIGN REVERSALS")
    print("  Q: Do more accel reversals in entry window predict winner?")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "accel_reversals", split_type="median")
        if r:
            r["test"] = "accel_reversals"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 2: Acceleration direction ─────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 2: ACCELERATION DIRECTION (toward vs away from expensive)")
    print("  Q: Does accel TOWARD expensive side predict better?")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        subset = subset[subset["accel_toward_exp_median"].notna()]
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "accel_toward_exp_median", split_type="zero")
        if r:
            r["test"] = "accel_direction"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 3: Jerk direction ─────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 3: JERK DIRECTION (toward vs away from expensive)")
    print("  Q: Does jerk toward expensive side predict inflection?")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        subset = subset[subset["jerk_toward_exp_median"].notna()]
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "jerk_toward_exp_median", split_type="zero")
        if r:
            r["test"] = "jerk_direction"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 4a: Velocity direction relative to expensive ──────────────
    print(f"\n{'=' * 70}")
    print("  TEST 4a: VELOCITY DIRECTION (toward vs away from expensive)")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "velocity_toward_exp_median", split_type="zero")
        if r:
            r["test"] = "velocity_direction"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 4b: Velocity magnitude ────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 4b: VELOCITY MAGNITUDE (high vs low |velocity|)")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "velocity_abs_median", split_type="median")
        if r:
            r["test"] = "velocity_magnitude"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 4c: Deceleration ──────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 4c: DECELERATION (|vel| drops >30% in entry window)")
    print("=" * 70)

    for t in thresholds:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        if len(subset) < 10:
            continue
        r = print_split_test(f"T>=${t}", subset, "decel_detected", split_type="bool")
        if r:
            r["test"] = "deceleration"
            r["threshold"] = t
            all_results.append(r)

    # ── TEST 5: Spike + Deceleration combo ─────────────────────────────
    print(f"\n{'=' * 70}")
    print("  TEST 5: SPIKE + DECELERATION COMBO vs SPIKE ALONE")
    print("=" * 70)

    for t in [0.65, 0.75, 0.80]:
        subset = combined[combined["expensive_ask_median"] >= t].copy()
        if len(subset) < 10:
            continue

        baseline_acc = subset["expensive_wins"].mean()
        spike_markets = subset[subset["has_spike"] == True]
        no_spike = subset[subset["has_spike"] == False]

        spike_acc = spike_markets["expensive_wins"].mean() if len(spike_markets) > 0 else np.nan
        no_spike_acc = no_spike["expensive_wins"].mean() if len(no_spike) > 0 else np.nan

        spike_decel = spike_markets[spike_markets["decel_detected"] == True]
        spike_no_decel = spike_markets[spike_markets["decel_detected"] == False]

        sd_acc = spike_decel["expensive_wins"].mean() if len(spike_decel) > 0 else np.nan
        snd_acc = spike_no_decel["expensive_wins"].mean() if len(spike_no_decel) > 0 else np.nan

        print(f"\n    T>=${t}: baseline={baseline_acc:.1%}(n={len(subset)})")
        print(f"      No spike:              {no_spike_acc:.1%}(n={len(no_spike)})")
        print(f"      Spike:                 {spike_acc:.1%}(n={len(spike_markets)})")
        print(f"      Spike + decel:         {sd_acc:.1%}(n={len(spike_decel)})")
        print(f"      Spike + no decel:      {snd_acc:.1%}(n={len(spike_no_decel)})")

    # ── PER-DATASET BREAKDOWN ──────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  PER-DATASET BREAKDOWN (T >= $0.75)")
    print("=" * 70)

    for ds_name in combined["dataset"].unique():
        ds = combined[(combined["dataset"] == ds_name) & (combined["expensive_ask_median"] >= 0.75)]
        if len(ds) < 5:
            continue
        print(f"\n  {ds_name}: {len(ds)} markets, baseline={ds['expensive_wins'].mean():.1%}")

        for test_col, test_type, test_name in [
            ("accel_toward_exp_median", "zero", "accel_dir"),
            ("jerk_toward_exp_median", "zero", "jerk_dir"),
            ("velocity_toward_exp_median", "zero", "vel_dir"),
            ("decel_detected", "bool", "decel"),
        ]:
            valid = ds[ds[test_col].notna()] if test_type != "bool" else ds
            if len(valid) < 5:
                continue

            if test_type == "zero":
                a = valid[valid[test_col] > 0]
                b = valid[valid[test_col] <= 0]
            else:
                a = valid[valid[test_col] == True]
                b = valid[valid[test_col] == False]

            a_acc = a["expensive_wins"].mean() if len(a) > 0 else np.nan
            b_acc = b["expensive_wins"].mean() if len(b) > 0 else np.nan
            p = chi2_test(int(a["expensive_wins"].sum()), len(a), int(b["expensive_wins"].sum()), len(b))
            sig = " *" if not np.isnan(p) and p < 0.05 else ""
            print(f"    {test_name}: +={a_acc:.0%}(n={len(a)}) -={b_acc:.0%}(n={len(b)}) p={p:.3f}{sig}" if not np.isnan(p) else f"    {test_name}: +={a_acc:.0%}(n={len(a)}) -={b_acc:.0%}(n={len(b)}) p=N/A")

    # ── FINAL SUMMARY ──────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  FINAL SUMMARY")
    print("=" * 70)

    results_df = pd.DataFrame(all_results)
    if not results_df.empty:
        sig_results = results_df[results_df["p"] < 0.05]
        print(f"\n  Total tests run: {len(results_df)}")
        print(f"  Significant (p<0.05): {len(sig_results)}")

        if len(sig_results) > 0:
            print(f"\n  Significant results:")
            for _, r in sig_results.iterrows():
                print(f"    {r['test']} @ T>={r['threshold']}: "
                      f"a={r['a_acc']:.1%}(n={r['n_a']}) b={r['b_acc']:.1%}(n={r['n_b']}) p={r['p']:.4f}")
        else:
            print(f"\n  NO significant results found.")
            print(f"  VERDICT: Acceleration, jerk, and velocity reformulations")
            print(f"  add NO predictive value over expensive_ask heuristic alone.")

    # Save
    output_file = PROJECT_ROOT / "research" / "findings" / "data" / "accel_velocity_test_results.csv"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(output_file, index=False)
    print(f"\n  Saved market-level data: {output_file}")


if __name__ == "__main__":
    run_test()
