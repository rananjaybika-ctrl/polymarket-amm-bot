"""
Price dynamics analysis within 15-minute Polymarket BTC binary markets.
Uses OOS7 (Jan 29-30) + OOS8 (Jan 31) datasets.
No project module imports — fully self-contained.
"""

import csv
from collections import defaultdict

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
OBS_FILES = [
    "/Users/rananjaybika/polymarket-amm-bot/research/observer/grid_obs_20260129.csv",
    "/Users/rananjaybika/polymarket-amm-bot/research/observer/grid_obs_20260131.csv",
]
RES_FILES = [
    "/Users/rananjaybika/polymarket-amm-bot/research/observer/resolutions_20260129.csv",
    "/Users/rananjaybika/polymarket-amm-bot/research/observer/resolutions_20260130.csv",
    "/Users/rananjaybika/polymarket-amm-bot/research/observer/resolutions_20260131.csv",
]

TIME_POINTS = [800, 600, 400, 300, 200, 100]   # seconds remaining
MAKER_OFFSET = 0.03                              # bid = expensive_ask - 0.03

# ---------------------------------------------------------------------------
# LOAD RESOLUTIONS
# ---------------------------------------------------------------------------
resolutions = {}   # market_slug -> "UP" | "DOWN"
for path in RES_FILES:
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            resolutions[row["market_slug"]] = row["resolution"]

print(f"Loaded {len(resolutions)} resolutions from {len(RES_FILES)} files.")

# ---------------------------------------------------------------------------
# LOAD OBSERVER DATA  (downsample: keep one row per market per ~1s bucket)
# ---------------------------------------------------------------------------
# Structure:  market_slug -> list of (time_remaining_secs, up_ask, down_ask)
# sorted descending by time_remaining (we only keep one sample per integer second)
market_ticks = defaultdict(list)

for path in OBS_FILES:
    with open(path) as f:
        reader = csv.DictReader(f)
        prev_key = {}   # market_slug -> last integer-second bucket stored
        for row in reader:
            slug = row["market_slug"]
            if slug not in resolutions:
                continue  # skip markets with no resolution
            try:
                tr = float(row["time_remaining_secs"])
                up_ask = float(row["up_ask"])
                down_ask = float(row["down_ask"])
            except (ValueError, KeyError):
                continue

            bucket = int(tr)   # integer-second bucket for downsampling
            if prev_key.get(slug) == bucket:
                continue       # already stored a row for this second
            prev_key[slug] = bucket
            market_ticks[slug].append((tr, up_ask, down_ask))

# Sort each market's ticks in descending order of time_remaining
for slug in market_ticks:
    market_ticks[slug].sort(key=lambda x: x[0], reverse=True)

print(f"Loaded ticks for {len(market_ticks)} markets (downsampled to ~1 sample/sec).")
print()

# ---------------------------------------------------------------------------
# HELPER: find the closest tick to a target time_remaining
# Returns None if no tick within 30s of target
# ---------------------------------------------------------------------------
def find_closest_tick(ticks, target_secs, tolerance=30):
    """ticks is sorted descending by time_remaining."""
    best = None
    best_dist = float("inf")
    for (tr, ua, da) in ticks:
        dist = abs(tr - target_secs)
        if dist < best_dist:
            best_dist = dist
            best = (tr, ua, da)
        if tr < target_secs - tolerance:
            break   # list is sorted descending, no point continuing
    if best_dist <= tolerance:
        return best
    return None

# ---------------------------------------------------------------------------
# ANALYSIS STRUCTURES
# ---------------------------------------------------------------------------
# Per time-point stats
stats = {T: {
    "total": 0,
    "expensive_correct": 0,      # expensive side == actual winner
    "expensive_ask_sum": 0.0,
    "cnt_ge65": 0,
    "cnt_ge70": 0,
    "cnt_ge80": 0,
    "expensive_side": [],        # list of "UP"/"DOWN" labels
    "slugs": [],                 # market slugs seen at this T
} for T in TIME_POINTS}

# Flip analysis: per market, sequence of expensive sides at each time point
market_expensive_seq = defaultdict(dict)   # slug -> {T: "UP"/"DOWN"}

# Maker fill analysis: per market and time point
# Did the ask ever touch expensive_ask - 0.03 at any point AFTER time T?
# We'll build this from the full tick data.

maker_fill = {T: {"total": 0, "filled": 0} for T in TIME_POINTS}

# ---------------------------------------------------------------------------
# MAIN LOOP
# ---------------------------------------------------------------------------
all_slugs = set(market_ticks.keys()) & set(resolutions.keys())
print(f"Markets with both ticks and resolution: {len(all_slugs)}")
print()

for slug in all_slugs:
    ticks = market_ticks[slug]   # sorted descending by time_remaining
    resolution = resolutions[slug]   # "UP" or "DOWN"

    # Build a fast lookup: time_remaining -> (up_ask, down_ask)
    # Also keep full list for later-asks scan
    all_tr    = [t[0] for t in ticks]
    all_ua    = [t[1] for t in ticks]
    all_da    = [t[2] for t in ticks]

    for T in TIME_POINTS:
        tick = find_closest_tick(ticks, T, tolerance=30)
        if tick is None:
            continue

        _, up_ask, down_ask = tick

        # Determine expensive side
        if up_ask > down_ask:
            exp_side = "UP"
            exp_ask  = up_ask
        elif down_ask > up_ask:
            exp_side = "DOWN"
            exp_ask  = down_ask
        else:
            # Tied — skip
            continue

        s = stats[T]
        s["total"] += 1
        s["expensive_ask_sum"] += exp_ask
        if exp_ask >= 0.65:
            s["cnt_ge65"] += 1
        if exp_ask >= 0.70:
            s["cnt_ge70"] += 1
        if exp_ask >= 0.80:
            s["cnt_ge80"] += 1

        if exp_side == resolution:
            s["expensive_correct"] += 1

        market_expensive_seq[slug][T] = exp_side

        # Maker fill: bid = exp_ask - MAKER_OFFSET
        # Fill condition: down the timeline (lower time_remaining),
        # the expensive side's ask drops to <= our bid
        our_bid = exp_ask - MAKER_OFFSET

        # Collect all ticks AFTER this time point (lower time_remaining)
        later_exp_asks = []
        for (tr, ua, da) in ticks:
            if tr >= T:
                continue   # same or earlier — skip
            later_exp_asks.append(ua if exp_side == "UP" else da)

        maker_fill[T]["total"] += 1
        if any(a <= our_bid for a in later_exp_asks):
            maker_fill[T]["filled"] += 1

# ---------------------------------------------------------------------------
# FLIP ANALYSIS
# ---------------------------------------------------------------------------
# For each market, count how many TIME_POINTS have a different expensive
# side from the first (earliest in sequence = highest T available)
flip_counts = []   # per market: number of flips among observed time points

for slug, seq in market_expensive_seq.items():
    present_Ts = sorted([T for T in TIME_POINTS if T in seq], reverse=True)
    if len(present_Ts) < 2:
        continue
    first_side = seq[present_Ts[0]]
    flips = sum(1 for T in present_Ts[1:] if seq[T] != first_side)
    flip_counts.append(flips)

total_with_multiple = len(flip_counts)
at_least_one_flip   = sum(1 for f in flip_counts if f > 0)

# ---------------------------------------------------------------------------
# PRINT RESULTS
# ---------------------------------------------------------------------------

print("=" * 70)
print("SECTION 1: EXPENSIVE-SIDE PREDICTION ACCURACY PER TIME POINT")
print("=" * 70)
print(f"{'Time Rem':>10} {'N Mkts':>8} {'Accuracy':>10} {'Avg Exp Ask':>13}")
print("-" * 45)
for T in TIME_POINTS:
    s = stats[T]
    n = s["total"]
    if n == 0:
        print(f"{T:>10}s {'0':>8} {'N/A':>10} {'N/A':>13}")
        continue
    acc = s["expensive_correct"] / n * 100
    avg_ask = s["expensive_ask_sum"] / n
    print(f"{T:>9}s {n:>8} {acc:>9.1f}% {avg_ask:>13.4f}")

print()
print("=" * 70)
print("SECTION 2: EXPENSIVE_ASK DISTRIBUTION PER TIME POINT")
print("=" * 70)
print(f"{'Time Rem':>10} {'N':>6} {'>=0.65':>10} {'>=0.70':>10} {'>=0.80':>10}")
print("-" * 50)
for T in TIME_POINTS:
    s = stats[T]
    n = s["total"]
    if n == 0:
        print(f"{T:>9}s {'0':>6} {'N/A':>10} {'N/A':>10} {'N/A':>10}")
        continue
    p65 = s["cnt_ge65"] / n * 100
    p70 = s["cnt_ge70"] / n * 100
    p80 = s["cnt_ge80"] / n * 100
    print(f"{T:>9}s {n:>6} {p65:>9.1f}% {p70:>9.1f}% {p80:>9.1f}%")

print()
print("=" * 70)
print("SECTION 3: EXPENSIVE-SIDE FLIP FREQUENCY")
print("=" * 70)
print(f"Markets with >=2 time points observed : {total_with_multiple}")
print(f"Markets with >=1 flip in expensive side: {at_least_one_flip}  "
      f"({at_least_one_flip/total_with_multiple*100:.1f}%)" if total_with_multiple else "N/A")

if flip_counts:
    avg_flips = sum(flip_counts) / len(flip_counts)
    print(f"Average flips per market              : {avg_flips:.2f}")

    from collections import Counter
    dist = Counter(flip_counts)
    print(f"\nFlip count distribution:")
    print(f"  {'Flips':>6} {'Markets':>8} {'Pct':>8}")
    print(f"  {'-'*24}")
    for k in sorted(dist.keys()):
        pct = dist[k] / total_with_multiple * 100
        print(f"  {k:>6} {dist[k]:>8} {pct:>7.1f}%")

print()
print("=" * 70)
print("SECTION 4: MAKER FILL PROBABILITY")
print(f"  Bid = expensive_ask - {MAKER_OFFSET:.2f}  (fill when ask later drops to bid)")
print("=" * 70)
print(f"{'Time Rem':>10} {'N':>6} {'Filled':>8} {'Fill Rate':>10}")
print("-" * 38)
for T in TIME_POINTS:
    mf = maker_fill[T]
    n = mf["total"]
    if n == 0:
        print(f"{T:>9}s {'0':>6} {'N/A':>8} {'N/A':>10}")
        continue
    rate = mf["filled"] / n * 100
    print(f"{T:>9}s {n:>6} {mf['filled']:>8} {rate:>9.1f}%")

print()
print("=" * 70)
print("SECTION 5: COMBINED — ACCURACY × FILL RATE (expected edge proxy)")
print("  (expensive side correct AND would have been filled as maker)")
print("=" * 70)
print(f"{'Time Rem':>10} {'Accuracy':>10} {'Fill%':>8} {'Joint%':>10}")
print("-" * 42)
for T in TIME_POINTS:
    s = stats[T]
    mf = maker_fill[T]
    n  = s["total"]
    nf = mf["total"]
    if n == 0 or nf == 0:
        continue
    acc  = s["expensive_correct"] / n * 100
    fill = mf["filled"] / nf * 100
    joint = acc * fill / 100
    print(f"{T:>9}s {acc:>9.1f}% {fill:>7.1f}% {joint:>9.1f}%")

print()
print("Done.")
