"""
Pair cost and fill dynamics analysis for multi-phase accumulation strategy.
Analyzes OOS7 observer data (Jan 29-30, 2026).
Self-contained, no project module imports.
"""

import pandas as pd
import numpy as np

# ── 1. LOAD DATA ─────────────────────────────────────────────────────────────

print("Loading data...")

obs_29 = pd.read_csv("research/observer/grid_obs_20260129.csv",
                     usecols=["timestamp_ms", "market_slug", "time_remaining_secs",
                               "up_ask", "down_ask", "up_bid", "down_bid"])
obs_30 = pd.read_csv("research/observer/grid_obs_20260130.csv",
                     usecols=["timestamp_ms", "market_slug", "time_remaining_secs",
                               "up_ask", "down_ask", "up_bid", "down_bid"])

res_29 = pd.read_csv("research/observer/resolutions_20260129.csv")
res_30 = pd.read_csv("research/observer/resolutions_20260130.csv")

obs = pd.concat([obs_29, obs_30], ignore_index=True)
res = pd.concat([res_29, res_30], ignore_index=True)

print(f"  Observer rows : {len(obs):,}")
print(f"  Markets in obs: {obs['market_slug'].nunique()}")
print(f"  Resolutions   : {len(res)}")
print()

# ── 2. BUILD RESOLUTION MAP ──────────────────────────────────────────────────

res_map = dict(zip(res["market_slug"], res["resolution"]))

# Keep only markets with known resolution
markets = [m for m in obs["market_slug"].unique() if m in res_map]
print(f"  Resolved markets in obs: {len(markets)}")
print()

# ── 3. HELPER: closest row to a target time_remaining ─────────────────────────

def get_snapshot(df_market, target_secs, tolerance=30):
    """
    Return the row closest to target_secs remaining,
    within ±tolerance. Returns None if no row is close enough.
    """
    diff = (df_market["time_remaining_secs"] - target_secs).abs()
    idx = diff.idxmin()
    if diff[idx] <= tolerance:
        return df_market.loc[idx]
    return None


# ── 4. ANALYSIS SECTION 1: PAIR COST AT KEY TIME POINTS ─────────────────────

print("=" * 70)
print("SECTION 1: PAIR COST AT KEY TIME POINTS (TAKER)")
print("=" * 70)

checkpoints = [800, 600, 400, 200]
taker_pair_costs = {t: [] for t in checkpoints}
maker_pair_costs = {t: [] for t in checkpoints}   # ask-3c each side

for mkt in markets:
    df_m = obs[obs["market_slug"] == mkt].copy()
    df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)
    for t in checkpoints:
        snap = get_snapshot(df_m, t)
        if snap is not None:
            up_a  = snap["up_ask"]
            dn_a  = snap["down_ask"]
            taker = round(up_a + dn_a, 4)
            maker = round((up_a - 0.03) + (dn_a - 0.03), 4)
            taker_pair_costs[t].append(taker)
            maker_pair_costs[t].append(maker)

print(f"\n{'Time Remaining':>16} | {'N':>5} | {'Taker Pair Cost':>16} | {'Maker Pair Cost':>16}")
print("-" * 60)
for t in checkpoints:
    tp = taker_pair_costs[t]
    mp = maker_pair_costs[t]
    if tp:
        print(f"{t:>14}s   | {len(tp):>5} | "
              f"  {np.mean(tp):.4f} ± {np.std(tp):.4f}  | "
              f"  {np.mean(mp):.4f} ± {np.std(mp):.4f}")

print()
print("Taker pair cost distribution at 600s (percentiles):")
tp600 = taker_pair_costs[600]
if tp600:
    for pct in [10, 25, 50, 75, 90]:
        print(f"  p{pct:2d}: {np.percentile(tp600, pct):.4f}")


# ── 5. ANALYSIS SECTION 2: MAKER PAIR COST BREAKDOWN ────────────────────────

print()
print("=" * 70)
print("SECTION 2: MAKER PAIR COST DETAIL (ask-3c per side)")
print("=" * 70)

for t in checkpoints:
    costs = maker_pair_costs[t]
    if costs:
        below_100 = sum(1 for c in costs if c < 1.00)
        below_97  = sum(1 for c in costs if c < 0.97)
        n = len(costs)
        print(f"\nAt T={t}s: mean={np.mean(costs):.4f}, "
              f"<1.00={below_100/n*100:.1f}%, <0.97={below_97/n*100:.1f}%")


# ── 6. ANALYSIS SECTION 3: FILL DYNAMICS ─────────────────────────────────────
# "If we place a maker bid at up_ask(T) - 0.03, does up_ask later
#  drop to that level within the same market?"

print()
print("=" * 70)
print("SECTION 3: FILL DYNAMICS — ask-3c maker bid placed at T")
print("(Does ask drop to bid level at any point AFTER entry?)")
print("=" * 70)

OFFSET = 0.03

for entry_t in [800, 600, 400]:
    up_filled   = 0
    down_filled = 0
    both_filled = 0
    either_filled = 0
    n_valid     = 0

    for mkt in markets:
        df_m = obs[obs["market_slug"] == mkt].copy()
        df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)

        snap = get_snapshot(df_m, entry_t)
        if snap is None:
            continue

        up_bid_target  = snap["up_ask"]  - OFFSET
        dn_bid_target  = snap["down_ask"] - OFFSET

        # Look at all rows AFTER entry (lower time_remaining)
        entry_tr = snap["time_remaining_secs"]
        later = df_m[df_m["time_remaining_secs"] < entry_tr]

        if len(later) == 0:
            continue

        n_valid += 1
        up_hit  = (later["up_ask"]   <= up_bid_target).any()
        dn_hit  = (later["down_ask"] <= dn_bid_target).any()

        if up_hit:  up_filled   += 1
        if dn_hit:  down_filled += 1
        if up_hit and dn_hit: both_filled += 1
        if up_hit or dn_hit:  either_filled += 1

    if n_valid > 0:
        print(f"\nEntry at T={entry_t}s (N={n_valid}):")
        print(f"  UP  side fill rate   : {up_filled}/{n_valid}  = {up_filled/n_valid*100:5.1f}%")
        print(f"  DOWN side fill rate  : {down_filled}/{n_valid}  = {down_filled/n_valid*100:5.1f}%")
        print(f"  BOTH sides filled    : {both_filled}/{n_valid}  = {both_filled/n_valid*100:5.1f}%")
        print(f"  EITHER side filled   : {either_filled}/{n_valid}  = {either_filled/n_valid*100:5.1f}%")


# ── 7. ANALYSIS SECTION 4: CHEAP (LOSING) SIDE FILL RATE ─────────────────────

print()
print("=" * 70)
print("SECTION 4: CHEAP SIDE (LOSING/WRONG SIDE) MAKER FILL RATE")
print("(ask-3c bid placed at entry; ask-3c applied to loser side)")
print("=" * 70)

for entry_t in [800, 600, 400]:
    filled_count = 0
    n_valid = 0

    for mkt in markets:
        resolution = res_map[mkt]   # UP or DOWN
        df_m = obs[obs["market_slug"] == mkt].copy()
        df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)

        snap = get_snapshot(df_m, entry_t)
        if snap is None:
            continue

        # Cheap side = losing side
        if resolution == "UP":
            # UP won → DOWN is cheap (loser)
            cheap_ask_at_entry = snap["down_ask"]
        else:
            # DOWN won → UP is cheap (loser)
            cheap_ask_at_entry = snap["up_ask"]

        bid_target = cheap_ask_at_entry - OFFSET

        entry_tr = snap["time_remaining_secs"]
        later = df_m[df_m["time_remaining_secs"] < entry_tr]
        if len(later) == 0:
            continue

        n_valid += 1

        if resolution == "UP":
            hit = (later["down_ask"] <= bid_target).any()
        else:
            hit = (later["up_ask"] <= bid_target).any()

        if hit:
            filled_count += 1

    if n_valid > 0:
        print(f"\nEntry at T={entry_t}s (N={n_valid}):")
        print(f"  Cheap (loser) side fill rate at ask-3c: "
              f"{filled_count}/{n_valid} = {filled_count/n_valid*100:.1f}%")


# ── 8. ANALYSIS SECTION 5: EXPENSIVE (WINNING) SIDE FILL RATE ────────────────

print()
print("=" * 70)
print("SECTION 5: EXPENSIVE SIDE (WINNING SIDE) MAKER FILL RATE")
print("(ask-3c bid placed at entry)")
print("=" * 70)

for entry_t in [800, 600, 400]:
    filled_count = 0
    n_valid = 0

    for mkt in markets:
        resolution = res_map[mkt]
        df_m = obs[obs["market_slug"] == mkt].copy()
        df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)

        snap = get_snapshot(df_m, entry_t)
        if snap is None:
            continue

        # Expensive side = winning side
        if resolution == "UP":
            exp_ask_at_entry = snap["up_ask"]
        else:
            exp_ask_at_entry = snap["down_ask"]

        bid_target = exp_ask_at_entry - OFFSET

        entry_tr = snap["time_remaining_secs"]
        later = df_m[df_m["time_remaining_secs"] < entry_tr]
        if len(later) == 0:
            continue

        n_valid += 1

        if resolution == "UP":
            hit = (later["up_ask"] <= bid_target).any()
        else:
            hit = (later["down_ask"] <= bid_target).any()

        if hit:
            filled_count += 1

    if n_valid > 0:
        print(f"\nEntry at T={entry_t}s (N={n_valid}):")
        print(f"  Expensive (winner) side fill rate at ask-3c: "
              f"{filled_count}/{n_valid} = {filled_count/n_valid*100:.1f}%")


# ── 9. ANALYSIS SECTION 6: MIXED APPROACH ────────────────────────────────────
# Expensive side: ask-1c (aggressive maker)
# Cheap side: ask-3c (passive maker)

print()
print("=" * 70)
print("SECTION 6: MIXED APPROACH — ask-1c (expensive) + ask-3c (cheap)")
print("Both fills required for a complete pair")
print("=" * 70)

for entry_t in [800, 600, 400]:
    exp_filled  = 0
    cheap_filled= 0
    both_filled = 0
    n_valid     = 0

    for mkt in markets:
        resolution = res_map[mkt]
        df_m = obs[obs["market_slug"] == mkt].copy()
        df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)

        snap = get_snapshot(df_m, entry_t)
        if snap is None:
            continue

        if resolution == "UP":
            exp_ask   = snap["up_ask"]
            cheap_ask = snap["down_ask"]
        else:
            exp_ask   = snap["down_ask"]
            cheap_ask = snap["up_ask"]

        exp_target   = exp_ask   - 0.01   # aggressive
        cheap_target = cheap_ask - 0.03   # passive

        entry_tr = snap["time_remaining_secs"]
        later = df_m[df_m["time_remaining_secs"] < entry_tr]
        if len(later) == 0:
            continue

        n_valid += 1

        if resolution == "UP":
            exp_hit   = (later["up_ask"]   <= exp_target).any()
            cheap_hit = (later["down_ask"] <= cheap_target).any()
        else:
            exp_hit   = (later["down_ask"] <= exp_target).any()
            cheap_hit = (later["up_ask"]   <= cheap_target).any()

        if exp_hit:   exp_filled   += 1
        if cheap_hit: cheap_filled += 1
        if exp_hit and cheap_hit: both_filled += 1

    if n_valid > 0:
        print(f"\nEntry at T={entry_t}s (N={n_valid}):")
        print(f"  Expensive side (ask-1c) fill rate : "
              f"{exp_filled}/{n_valid} = {exp_filled/n_valid*100:.1f}%")
        print(f"  Cheap side (ask-3c) fill rate     : "
              f"{cheap_filled}/{n_valid} = {cheap_filled/n_valid*100:.1f}%")
        print(f"  BOTH filled (complete pair)        : "
              f"{both_filled}/{n_valid} = {both_filled/n_valid*100:.1f}%")


# ── 10. BONUS: OFFSET SWEEP ───────────────────────────────────────────────────

print()
print("=" * 70)
print("BONUS: FILL RATE SWEEP vs OFFSET (both sides, entry at T=600s)")
print("=" * 70)

entry_t = 600
results_by_offset = []
for offset in [0.01, 0.02, 0.03, 0.04, 0.05]:
    up_filled = down_filled = both = n_valid = 0
    for mkt in markets:
        df_m = obs[obs["market_slug"] == mkt].copy()
        df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)
        snap = get_snapshot(df_m, entry_t)
        if snap is None:
            continue
        up_target  = snap["up_ask"]   - offset
        dn_target  = snap["down_ask"] - offset
        entry_tr   = snap["time_remaining_secs"]
        later = df_m[df_m["time_remaining_secs"] < entry_tr]
        if len(later) == 0:
            continue
        n_valid += 1
        u = (later["up_ask"]   <= up_target).any()
        d = (later["down_ask"] <= dn_target).any()
        if u: up_filled  += 1
        if d: down_filled += 1
        if u and d: both += 1
    results_by_offset.append((offset, n_valid, up_filled, down_filled, both))

print(f"\n{'Offset':>8} | {'N':>5} | {'UP fill%':>10} | {'DN fill%':>10} | {'BOTH%':>8}")
print("-" * 50)
for offset, n, u, d, b in results_by_offset:
    if n > 0:
        print(f"  ask-{int(offset*100):02d}c  | {n:>5} | "
              f"{u/n*100:>9.1f}% | {d/n*100:>9.1f}% | {b/n*100:>7.1f}%")


# ── 11. WINNER vs LOSER price distribution at entry ──────────────────────────

print()
print("=" * 70)
print("BONUS 2: WINNER vs LOSER PRICE AT ENTRY (T=600s)")
print("(Winner = expensive side, Loser = cheap side)")
print("=" * 70)

winner_prices = []
loser_prices  = []

for mkt in markets:
    resolution = res_map[mkt]
    df_m = obs[obs["market_slug"] == mkt].copy()
    df_m = df_m.sort_values("time_remaining_secs", ascending=False).reset_index(drop=True)
    snap = get_snapshot(df_m, 600)
    if snap is None:
        continue
    if resolution == "UP":
        winner_prices.append(snap["up_ask"])
        loser_prices.append(snap["down_ask"])
    else:
        winner_prices.append(snap["down_ask"])
        loser_prices.append(snap["up_ask"])

print(f"\n{'Stat':>10} | {'Winner ask':>12} | {'Loser ask':>12}")
print("-" * 40)
for pct in [10, 25, 50, 75, 90]:
    wp = np.percentile(winner_prices, pct)
    lp = np.percentile(loser_prices, pct)
    print(f"  p{pct:2d}     | {wp:>12.4f} | {lp:>12.4f}")
print(f"  mean    | {np.mean(winner_prices):>12.4f} | {np.mean(loser_prices):>12.4f}")
print(f"  std     | {np.std(winner_prices):>12.4f} | {np.std(loser_prices):>12.4f}")

print()
print("Done.")
