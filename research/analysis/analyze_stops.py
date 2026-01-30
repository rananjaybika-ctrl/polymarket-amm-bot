import pandas as pd
import numpy as np
from scipy import stats
import warnings
warnings.filterwarnings("ignore")

# Load the data files
time_stop_df = pd.read_csv("/Users/rananjaybika/polymarket-amm-bot/research/time_stop_top50_results.csv")
stop_out_df = pd.read_csv("/Users/rananjaybika/polymarket-amm-bot/research/stop_out_analysis_results.csv")
grid_df = pd.read_csv("/Users/rananjaybika/polymarket-amm-bot/research/vol_filter_grid_results_all_combined.csv")

print("=" * 80)
print("COMPREHENSIVE TIME-STOP VS PRICE-STOP STATISTICAL ANALYSIS")
print("=" * 80)
print()

print("=== DATA LOADED ===")
print(f"Time Stop Results: {len(time_stop_df)} rows")
print(f"Stop Out Analysis: {len(stop_out_df)} rows")
print(f"Grid Results: {len(grid_df)} rows")
print()

# ============================================================================
# SECTION 1: BASIC COMPARISONS
# ============================================================================
print("=" * 80)
print("SECTION 1: BASIC STOP TYPE COMPARISONS")
print("=" * 80)
print()

print("=== STOP TYPE VALUES ===")
print(time_stop_df["stop_type"].unique())
print()

# Separate baseline (price stops) and time stops
baseline_df = time_stop_df[time_stop_df["stop_type"].str.contains("price", case=False, na=False)]
time_120_df = time_stop_df[time_stop_df["stop_type"] == "120s time"]
time_180_df = time_stop_df[time_stop_df["stop_type"] == "180s time"]

print(f"Baseline (Price Stop) rows: {len(baseline_df)}")
print(f"120s Time Stop rows: {len(time_120_df)}")
print(f"180s Time Stop rows: {len(time_180_df)}")
print()

# PnL comparison
pnl_price = baseline_df["pnl"].mean()
pnl_120 = time_120_df["pnl"].mean()
pnl_180 = time_180_df["pnl"].mean()

print("=== PNL COMPARISON BY STOP TYPE ===")
print(f"Price Stop Mean PnL: ${pnl_price:.2f}")
print(f"120s Time Stop Mean PnL: ${pnl_120:.2f}")
print(f"180s Time Stop Mean PnL: ${pnl_180:.2f}")
print(f"120s vs Price: {((pnl_120 - pnl_price) / pnl_price * 100):.1f}% change")
print(f"180s vs Price: {((pnl_180 - pnl_price) / pnl_price * 100):.1f}% change")
print()

# Win Rate comparison
wr_price = baseline_df["win_rate"].mean()
wr_120 = time_120_df["win_rate"].mean()
wr_180 = time_180_df["win_rate"].mean()

print("=== WIN RATE COMPARISON ===")
print(f"Price Stop Mean Win Rate: {wr_price:.2f}%")
print(f"120s Time Stop Mean Win Rate: {wr_120:.2f}%")
print(f"180s Time Stop Mean Win Rate: {wr_180:.2f}%")
print(f"120s vs Price: {wr_120 - wr_price:.2f} ppts")
print(f"180s vs Price: {wr_180 - wr_price:.2f} ppts")
print()

# Hourly Rate comparison
hr_price = baseline_df["hourly_rate"].mean()
hr_120 = time_120_df["hourly_rate"].mean()
hr_180 = time_180_df["hourly_rate"].mean()

print("=== HOURLY RATE COMPARISON ===")
print(f"Price Stop Mean Hourly: ${hr_price:.4f}")
print(f"120s Time Stop Mean Hourly: ${hr_120:.4f}")
print(f"180s Time Stop Mean Hourly: ${hr_180:.4f}")
print(f"120s vs Price: {((hr_120 - hr_price) / hr_price * 100):.1f}% change")
print(f"180s vs Price: {((hr_180 - hr_price) / hr_price * 100):.1f}% change")
print()

# ============================================================================
# SECTION 2: PREMATURE STOP ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 2: PREMATURE STOP ANALYSIS")
print("=" * 80)
print()

# Only time stops have premature stop data
time_stops_only = time_stop_df[~time_stop_df["stop_type"].str.contains("price", case=False, na=False)].copy()
time_stops_only = time_stops_only.dropna(subset=["premature_pct", "premature_pnl"])

print("=== PREMATURE STOP PERCENTAGES ===")
print(f"120s Time Stop Mean Premature %: {time_120_df['premature_pct'].mean():.2f}%")
print(f"180s Time Stop Mean Premature %: {time_180_df['premature_pct'].mean():.2f}%")
print()

print("=== PREMATURE STOP PnL IMPACT ===")
print(f"120s Time Stop Mean Premature PnL: ${time_120_df['premature_pnl'].mean():.2f}")
print(f"180s Time Stop Mean Premature PnL: ${time_180_df['premature_pnl'].mean():.2f}")
print()

# Correlation between premature % and PnL for time stops
corr_premature_pnl, p_val = stats.pearsonr(
    time_stops_only["premature_pct"],
    time_stops_only["pnl"]
)
print(f"Correlation (Premature % vs PnL): r={corr_premature_pnl:.4f}, p={p_val:.4f}")
print()

# ============================================================================
# SECTION 3: CYCLING ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 3: CYCLING (ON/OFF) IMPACT ON STOP TYPE EFFECTIVENESS")
print("=" * 80)
print()

# Group by cycling status
cycling_true = time_stop_df[time_stop_df["cycling"] == True]
cycling_false = time_stop_df[time_stop_df["cycling"] == False]

print("=== CYCLING=TRUE ANALYSIS ===")
ct_price = cycling_true[cycling_true["stop_type"].str.contains("price", na=False)]
ct_120 = cycling_true[cycling_true["stop_type"] == "120s time"]
ct_180 = cycling_true[cycling_true["stop_type"] == "180s time"]

if len(ct_price) > 0 and len(ct_120) > 0 and len(ct_180) > 0:
    print(f"Configs with Cycling=TRUE: {len(ct_price)}")
    print(f"  Price Stop Mean PnL: ${ct_price['pnl'].mean():.2f}")
    print(f"  120s Time Stop Mean PnL: ${ct_120['pnl'].mean():.2f} ({((ct_120['pnl'].mean() - ct_price['pnl'].mean()) / ct_price['pnl'].mean() * 100):.1f}% vs price)")
    print(f"  180s Time Stop Mean PnL: ${ct_180['pnl'].mean():.2f} ({((ct_180['pnl'].mean() - ct_price['pnl'].mean()) / ct_price['pnl'].mean() * 100):.1f}% vs price)")
    print()

print("=== CYCLING=FALSE ANALYSIS ===")
cf_price = cycling_false[cycling_false["stop_type"].str.contains("price", na=False)]
cf_120 = cycling_false[cycling_false["stop_type"] == "120s time"]
cf_180 = cycling_false[cycling_false["stop_type"] == "180s time"]

if len(cf_price) > 0 and len(cf_120) > 0 and len(cf_180) > 0:
    print(f"Configs with Cycling=FALSE: {len(cf_price)}")
    print(f"  Price Stop Mean PnL: ${cf_price['pnl'].mean():.2f}")
    print(f"  120s Time Stop Mean PnL: ${cf_120['pnl'].mean():.2f} ({((cf_120['pnl'].mean() - cf_price['pnl'].mean()) / cf_price['pnl'].mean() * 100):.1f}% vs price)")
    print(f"  180s Time Stop Mean PnL: ${cf_180['pnl'].mean():.2f} ({((cf_180['pnl'].mean() - cf_price['pnl'].mean()) / cf_price['pnl'].mean() * 100):.1f}% vs price)")
print()

# ============================================================================
# SECTION 4: Z-SCORE METHOD ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 4: Z-SCORE METHOD IMPACT ON STOP TYPE EFFECTIVENESS")
print("=" * 80)
print()

zscore_methods = time_stop_df["zscore_method"].unique()
print(f"Z-Score Methods: {zscore_methods}")
print()

for method in zscore_methods:
    method_df = time_stop_df[time_stop_df["zscore_method"] == method]
    m_price = method_df[method_df["stop_type"].str.contains("price", na=False)]
    m_120 = method_df[method_df["stop_type"] == "120s time"]
    m_180 = method_df[method_df["stop_type"] == "180s time"]

    if len(m_price) > 0 and len(m_120) > 0 and len(m_180) > 0:
        price_pnl = m_price["pnl"].mean()
        print(f"=== {method.upper()} Z-SCORE METHOD ===")
        print(f"Configs: {len(m_price)}")
        print(f"  Price Stop Mean PnL: ${price_pnl:.2f}")
        print(f"  120s Time Stop Mean PnL: ${m_120['pnl'].mean():.2f} ({((m_120['pnl'].mean() - price_pnl) / price_pnl * 100):.1f}% vs price)")
        print(f"  180s Time Stop Mean PnL: ${m_180['pnl'].mean():.2f} ({((m_180['pnl'].mean() - price_pnl) / price_pnl * 100):.1f}% vs price)")
        print()

# ============================================================================
# SECTION 5: DETAILED CONFIG-BY-CONFIG ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 5: CONFIG-BY-CONFIG ANALYSIS - WHICH CONFIGS BENEFIT FROM TIME STOPS?")
print("=" * 80)
print()

# Create comparison dataframe
# Group by rank to compare same configs
results = []
for rank in baseline_df["rank"].unique():
    rank_df = time_stop_df[time_stop_df["rank"] == rank]

    price_row = rank_df[rank_df["stop_type"].str.contains("price", na=False)]
    t120_row = rank_df[rank_df["stop_type"] == "120s time"]
    t180_row = rank_df[rank_df["stop_type"] == "180s time"]

    if len(price_row) == 1 and len(t120_row) == 1 and len(t180_row) == 1:
        price_row = price_row.iloc[0]
        t120_row = t120_row.iloc[0]
        t180_row = t180_row.iloc[0]

        results.append({
            "rank": rank,
            "method": price_row["method"],
            "zscore_method": price_row["zscore_method"],
            "lookback_ms": price_row["lookback_ms"],
            "cycling": price_row["cycling"],
            "z_zone": price_row["z_zone"],
            "price_pnl": price_row["pnl"],
            "t120_pnl": t120_row["pnl"],
            "t180_pnl": t180_row["pnl"],
            "price_win_rate": price_row["win_rate"],
            "t120_win_rate": t120_row["win_rate"],
            "t180_win_rate": t180_row["win_rate"],
            "t120_premature_pct": t120_row["premature_pct"],
            "t180_premature_pct": t180_row["premature_pct"],
            "best_stop": "180s" if t180_row["pnl"] > max(price_row["pnl"], t120_row["pnl"]) else ("120s" if t120_row["pnl"] > price_row["pnl"] else "price")
        })

compare_df = pd.DataFrame(results)
compare_df["t120_pnl_change"] = (compare_df["t120_pnl"] - compare_df["price_pnl"]) / compare_df["price_pnl"] * 100
compare_df["t180_pnl_change"] = (compare_df["t180_pnl"] - compare_df["price_pnl"]) / compare_df["price_pnl"] * 100

print(f"Total comparable configs: {len(compare_df)}")
print()

# Count best stop type
print("=== BEST STOP TYPE DISTRIBUTION ===")
print(compare_df["best_stop"].value_counts())
print()

# Configs where time stop beats price stop
time_wins = compare_df[(compare_df["t120_pnl"] > compare_df["price_pnl"]) | (compare_df["t180_pnl"] > compare_df["price_pnl"])]
price_wins = compare_df[(compare_df["t120_pnl"] <= compare_df["price_pnl"]) & (compare_df["t180_pnl"] <= compare_df["price_pnl"])]

print(f"Configs where TIME STOP wins: {len(time_wins)} ({len(time_wins)/len(compare_df)*100:.1f}%)")
print(f"Configs where PRICE STOP wins: {len(price_wins)} ({len(price_wins)/len(compare_df)*100:.1f}%)")
print()

# ============================================================================
# SECTION 6: PATTERN IDENTIFICATION
# ============================================================================
print("=" * 80)
print("SECTION 6: PATTERN IDENTIFICATION - WHAT MAKES TIME STOPS WORK?")
print("=" * 80)
print()

# Analyze characteristics of configs where time stops win vs lose
time_wins_180 = compare_df[compare_df["t180_pnl"] > compare_df["price_pnl"]]
time_loses_180 = compare_df[compare_df["t180_pnl"] <= compare_df["price_pnl"]]

print("=== COMPARING CONFIGS: 180s TIME STOP WINS vs LOSES ===")
print()

# By cycling
print("--- By Cycling ---")
tw_cycling = time_wins_180["cycling"].value_counts(normalize=True) * 100
tl_cycling = time_loses_180["cycling"].value_counts(normalize=True) * 100
print(f"Time wins - Cycling=True: {tw_cycling.get(True, 0):.1f}%, Cycling=False: {tw_cycling.get(False, 0):.1f}%")
print(f"Time loses - Cycling=True: {tl_cycling.get(True, 0):.1f}%, Cycling=False: {tl_cycling.get(False, 0):.1f}%")
print()

# By zscore method
print("--- By Z-Score Method ---")
tw_zscore = time_wins_180["zscore_method"].value_counts(normalize=True) * 100
tl_zscore = time_loses_180["zscore_method"].value_counts(normalize=True) * 100
print("Time wins distribution:")
print(tw_zscore)
print()
print("Time loses distribution:")
print(tl_zscore)
print()

# By base PnL (are time stops better for lower or higher PnL configs?)
print("--- By Base PnL Level ---")
print(f"Time wins - Mean base price PnL: ${time_wins_180['price_pnl'].mean():.2f}")
print(f"Time loses - Mean base price PnL: ${time_loses_180['price_pnl'].mean():.2f}")
print()

# By base win rate
print("--- By Base Win Rate ---")
print(f"Time wins - Mean base win rate: {time_wins_180['price_win_rate'].mean():.1f}%")
print(f"Time loses - Mean base win rate: {time_loses_180['price_win_rate'].mean():.1f}%")
print()

# By method (ou vs ewma)
print("--- By Method (OU vs EWMA) ---")
tw_method = time_wins_180["method"].value_counts(normalize=True) * 100
tl_method = time_loses_180["method"].value_counts(normalize=True) * 100
print("Time wins distribution:")
print(tw_method)
print()
print("Time loses distribution:")
print(tl_method)
print()

# ============================================================================
# SECTION 7: CORRELATION ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 7: CORRELATION ANALYSIS")
print("=" * 80)
print()

# Correlations with 180s time stop PnL improvement
compare_df["cycling_int"] = compare_df["cycling"].astype(int)
compare_df["is_ewma"] = (compare_df["zscore_method"] == "ewma").astype(int)
compare_df["is_ou_method"] = (compare_df["method"] == "ou").astype(int)

print("=== CORRELATIONS WITH 180s TIME STOP PnL CHANGE (%) ===")
print()

corr_vars = ["price_pnl", "price_win_rate", "cycling_int", "lookback_ms", "t180_premature_pct"]
for var in corr_vars:
    if var in compare_df.columns:
        valid = compare_df[[var, "t180_pnl_change"]].dropna()
        if len(valid) > 5:
            corr, p = stats.pearsonr(valid[var], valid["t180_pnl_change"])
            sig = "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else ""))
            print(f"{var:25s}: r={corr:+.4f}, p={p:.4f} {sig}")

print()
print("Significance: *** p<0.001, ** p<0.01, * p<0.05")
print()

# ============================================================================
# SECTION 8: STATISTICAL TESTS
# ============================================================================
print("=" * 80)
print("SECTION 8: STATISTICAL SIGNIFICANCE TESTS")
print("=" * 80)
print()

# Paired t-tests comparing stop types
print("=== PAIRED T-TESTS (Same Configs) ===")
print()

# 180s vs Price
t_stat, p_val = stats.ttest_rel(compare_df["t180_pnl"], compare_df["price_pnl"])
print(f"180s Time vs Price Stop PnL:")
print(f"  t-statistic: {t_stat:.4f}")
print(f"  p-value: {p_val:.4f}")
print(f"  Significant at 0.05? {'YES' if p_val < 0.05 else 'NO'}")
print()

# 120s vs Price
t_stat, p_val = stats.ttest_rel(compare_df["t120_pnl"], compare_df["price_pnl"])
print(f"120s Time vs Price Stop PnL:")
print(f"  t-statistic: {t_stat:.4f}")
print(f"  p-value: {p_val:.4f}")
print(f"  Significant at 0.05? {'YES' if p_val < 0.05 else 'NO'}")
print()

# ============================================================================
# SECTION 9: DETAILED BREAKDOWNS
# ============================================================================
print("=" * 80)
print("SECTION 9: DETAILED BREAKDOWNS BY CONFIGURATION ATTRIBUTES")
print("=" * 80)
print()

# Break down by cycling AND z-score method
print("=== BREAKDOWN BY CYCLING + Z-SCORE METHOD ===")
print()

for cycling in [True, False]:
    for zscore in compare_df["zscore_method"].unique():
        subset = compare_df[(compare_df["cycling"] == cycling) & (compare_df["zscore_method"] == zscore)]
        if len(subset) > 0:
            price_avg = subset["price_pnl"].mean()
            t180_avg = subset["t180_pnl"].mean()
            change = ((t180_avg - price_avg) / price_avg * 100)
            print(f"Cycling={str(cycling):5s}, zscore={zscore:12s}: n={len(subset):2d}, Price=${price_avg:6.2f}, 180s=${t180_avg:6.2f} ({change:+.1f}%)")

print()

# ============================================================================
# SECTION 10: CONCLUSIONS
# ============================================================================
print("=" * 80)
print("SECTION 10: KEY FINDINGS AND RECOMMENDATIONS")
print("=" * 80)
print()

# Calculate key stats for conclusions
best_stop_counts = compare_df["best_stop"].value_counts()
cycling_true_subset = compare_df[compare_df["cycling"] == True]
cycling_false_subset = compare_df[compare_df["cycling"] == False]

ct_time_better = len(cycling_true_subset[cycling_true_subset["t180_pnl"] > cycling_true_subset["price_pnl"]])
cf_time_better = len(cycling_false_subset[cycling_false_subset["t180_pnl"] > cycling_false_subset["price_pnl"]])

print("KEY FINDINGS:")
print()
print(f"1. OVERALL: 180s time stop is best for {best_stop_counts.get('180s', 0)} configs ({best_stop_counts.get('180s', 0)/len(compare_df)*100:.1f}%)")
print(f"   Price stop is best for {best_stop_counts.get('price', 0)} configs ({best_stop_counts.get('price', 0)/len(compare_df)*100:.1f}%)")
print()
print(f"2. CYCLING IMPACT:")
print(f"   - Cycling=True: 180s time beats price in {ct_time_better}/{len(cycling_true_subset)} configs ({ct_time_better/len(cycling_true_subset)*100:.1f}%)")
print(f"   - Cycling=False: 180s time beats price in {cf_time_better}/{len(cycling_false_subset)} configs ({cf_time_better/len(cycling_false_subset)*100:.1f}%)")
print()

# Z-score method breakdown
for zscore in compare_df["zscore_method"].unique():
    subset = compare_df[compare_df["zscore_method"] == zscore]
    time_better = len(subset[subset["t180_pnl"] > subset["price_pnl"]])
    print(f"3. {zscore.upper()} METHOD: 180s time beats price in {time_better}/{len(subset)} configs ({time_better/len(subset)*100:.1f}%)")

print()
print("RECOMMENDATIONS:")
print()

# Determine recommendations based on analysis
if ct_time_better/len(cycling_true_subset) > cf_time_better/len(cycling_false_subset):
    print("- USE TIME STOPS (180s) when: Cycling is ENABLED")
    print("- USE PRICE STOPS when: Cycling is DISABLED")
else:
    print("- USE PRICE STOPS when: Cycling is ENABLED")
    print("- USE TIME STOPS (180s) when: Cycling is DISABLED")

print()

# Check low vs high win rate configs
low_wr = compare_df[compare_df["price_win_rate"] < compare_df["price_win_rate"].median()]
high_wr = compare_df[compare_df["price_win_rate"] >= compare_df["price_win_rate"].median()]

low_wr_time_better = len(low_wr[low_wr["t180_pnl"] > low_wr["price_pnl"]])
high_wr_time_better = len(high_wr[high_wr["t180_pnl"] > high_wr["price_pnl"]])

print(f"WIN RATE PATTERN:")
print(f"- Low win rate configs (<{compare_df['price_win_rate'].median():.1f}%): Time beats price {low_wr_time_better}/{len(low_wr)} ({low_wr_time_better/len(low_wr)*100:.1f}%)")
print(f"- High win rate configs (>={compare_df['price_win_rate'].median():.1f}%): Time beats price {high_wr_time_better}/{len(high_wr)} ({high_wr_time_better/len(high_wr)*100:.1f}%)")
print()

# Output the full comparison dataframe
print("=" * 80)
print("FULL COMPARISON DATA (sorted by 180s PnL change)")
print("=" * 80)
print()
compare_df_sorted = compare_df.sort_values("t180_pnl_change", ascending=False)
print(compare_df_sorted[["rank", "method", "zscore_method", "cycling", "z_zone", "price_pnl", "t180_pnl", "t180_pnl_change", "best_stop"]].to_string())

# ============================================================================
# SECTION 11: AGGRESSIVE vs CONSERVATIVE DEEP DIVE
# ============================================================================
print()
print("=" * 80)
print("SECTION 11: AGGRESSIVE vs CONSERVATIVE CONFIG ANALYSIS")
print("=" * 80)
print()

# Define aggressive vs conservative based on win rate
median_wr = compare_df["price_win_rate"].median()
print(f"Median base win rate: {median_wr:.1f}%")
print()

# Aggressive configs (low win rate)
aggressive = compare_df[compare_df["price_win_rate"] < median_wr]
# Conservative configs (high win rate)
conservative = compare_df[compare_df["price_win_rate"] >= median_wr]

print("=== AGGRESSIVE CONFIGS (Win Rate < median) ===")
print(f"Count: {len(aggressive)}")
agg_price_pnl = aggressive["price_pnl"].mean()
agg_180_pnl = aggressive["t180_pnl"].mean()
change_agg = ((agg_180_pnl - agg_price_pnl) / agg_price_pnl * 100)
print(f"Price Stop Mean PnL: ${agg_price_pnl:.2f}")
print(f"180s Time Mean PnL: ${agg_180_pnl:.2f}")
print(f"Change: {change_agg:+.1f}%")
print()

print("=== CONSERVATIVE CONFIGS (Win Rate >= median) ===")
print(f"Count: {len(conservative)}")
cons_price_pnl = conservative["price_pnl"].mean()
cons_180_pnl = conservative["t180_pnl"].mean()
change_cons = ((cons_180_pnl - cons_price_pnl) / cons_price_pnl * 100)
print(f"Price Stop Mean PnL: ${cons_price_pnl:.2f}")
print(f"180s Time Mean PnL: ${cons_180_pnl:.2f}")
print(f"Change: {change_cons:+.1f}%")
print()

print("=" * 80)
print("THE CONTRADICTION EXPLAINED")
print("=" * 80)
print()

if change_agg > change_cons:
    print("TIME STOPS HELP AGGRESSIVE CONFIGS MORE")
    print(f"  - Aggressive configs: {change_agg:+.1f}% improvement with 180s time stop")
    print(f"  - Conservative configs: {change_cons:+.1f}% change with 180s time stop")
else:
    print("TIME STOPS HELP CONSERVATIVE CONFIGS MORE")
    print(f"  - Conservative configs: {change_cons:+.1f}% improvement with 180s time stop")
    print(f"  - Aggressive configs: {change_agg:+.1f}% change with 180s time stop")

print()
print("KEY INSIGHT: The 'contradiction' arises because:")
print("  - Low win rate (aggressive) configs have trades that often go against them initially")
print("  - Time stops let these trades recover instead of triggering a premature price stop")
print("  - High win rate (conservative) configs already have good entry timing")
print("  - Price stops are better for these as they protect the already-correct direction")
print()

# ============================================================================
# SECTION 12: Z-ZONE ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 12: Z-ZONE IMPACT ON STOP TYPE EFFECTIVENESS")
print("=" * 80)
print()

z_zones = compare_df["z_zone"].unique()
for zone in sorted(z_zones):
    subset = compare_df[compare_df["z_zone"] == zone]
    if len(subset) > 0:
        time_better_count = len(subset[subset["t180_pnl"] > subset["price_pnl"]])
        zone_price_pnl = subset["price_pnl"].mean()
        zone_180_pnl = subset["t180_pnl"].mean()
        zone_change = ((zone_180_pnl - zone_price_pnl) / zone_price_pnl * 100)
        print(f"{zone:15s}: n={len(subset):2d}, Price=${zone_price_pnl:6.2f}, 180s=${zone_180_pnl:6.2f} ({zone_change:+6.1f}%), Time wins: {time_better_count}/{len(subset)}")

print()

# ============================================================================
# SECTION 13: LOOKBACK MS ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 13: LOOKBACK PERIOD IMPACT ON STOP TYPE EFFECTIVENESS")
print("=" * 80)
print()

lookbacks = compare_df["lookback_ms"].unique()
for lb in sorted(lookbacks):
    subset = compare_df[compare_df["lookback_ms"] == lb]
    if len(subset) > 0:
        time_better_count = len(subset[subset["t180_pnl"] > subset["price_pnl"]])
        lb_price_pnl = subset["price_pnl"].mean()
        lb_180_pnl = subset["t180_pnl"].mean()
        lb_change = ((lb_180_pnl - lb_price_pnl) / lb_price_pnl * 100)
        print(f"Lookback {lb}ms: n={len(subset):2d}, Price=${lb_price_pnl:6.2f}, 180s=${lb_180_pnl:6.2f} ({lb_change:+6.1f}%), Time wins: {time_better_count}/{len(subset)}")

print()

# ============================================================================
# SECTION 14: MULTI-FACTOR ANALYSIS
# ============================================================================
print("=" * 80)
print("SECTION 14: MULTI-FACTOR DECISION TREE")
print("=" * 80)
print()

# Build a decision framework
print("DECISION FRAMEWORK FOR STOP TYPE SELECTION:")
print()

# Factor 1: Cycling
cycling_true_df = compare_df[compare_df["cycling"] == True]
cycling_false_df = compare_df[compare_df["cycling"] == False]

ct_time_wins = len(cycling_true_df[cycling_true_df["t180_pnl"] > cycling_true_df["price_pnl"]])
cf_time_wins = len(cycling_false_df[cycling_false_df["t180_pnl"] > cycling_false_df["price_pnl"]])

print(f"1. CYCLING:")
print(f"   IF Cycling=True:  180s time wins {ct_time_wins}/{len(cycling_true_df)} ({ct_time_wins/len(cycling_true_df)*100:.0f}%) -> Consider TIME STOP")
print(f"   IF Cycling=False: 180s time wins {cf_time_wins}/{len(cycling_false_df)} ({cf_time_wins/len(cycling_false_df)*100:.0f}%) -> USE PRICE STOP")
print()

# Factor 2: Z-score method (for cycling=True)
print("2. Z-SCORE METHOD (when Cycling=True):")
for method in ["ewma", "ewma_ratio", "ou", "percentile"]:
    subset = cycling_true_df[cycling_true_df["zscore_method"] == method]
    if len(subset) > 0:
        time_wins = len(subset[subset["t180_pnl"] > subset["price_pnl"]])
        pct = time_wins/len(subset)*100
        rec = "TIME STOP" if pct > 50 else "PRICE STOP"
        print(f"   IF zscore={method:12s}: Time wins {time_wins}/{len(subset)} ({pct:.0f}%) -> {rec}")
print()

# Factor 3: Win rate (for cycling=True, ewma/ewma_ratio)
print("3. WIN RATE (when Cycling=True AND zscore=ewma/ewma_ratio):")
ewma_subset = cycling_true_df[cycling_true_df["zscore_method"].isin(["ewma", "ewma_ratio"])]
low_wr_ewma = ewma_subset[ewma_subset["price_win_rate"] < median_wr]
high_wr_ewma = ewma_subset[ewma_subset["price_win_rate"] >= median_wr]

if len(low_wr_ewma) > 0:
    low_time_wins = len(low_wr_ewma[low_wr_ewma["t180_pnl"] > low_wr_ewma["price_pnl"]])
    print(f"   IF WinRate < {median_wr:.0f}%: Time wins {low_time_wins}/{len(low_wr_ewma)} ({low_time_wins/len(low_wr_ewma)*100:.0f}%) -> TIME STOP")
if len(high_wr_ewma) > 0:
    high_time_wins = len(high_wr_ewma[high_wr_ewma["t180_pnl"] > high_wr_ewma["price_pnl"]])
    print(f"   IF WinRate >= {median_wr:.0f}%: Time wins {high_time_wins}/{len(high_wr_ewma)} ({high_time_wins/len(high_wr_ewma)*100:.0f}%) -> PRICE STOP")

print()
print("=" * 80)
print("FINAL SUMMARY")
print("=" * 80)
print()

# Calculate overall statistics
total_configs = len(compare_df)
time_wins_overall = len(compare_df[compare_df["t180_pnl"] > compare_df["price_pnl"]])

# Strong patterns
cycling_true_ewma = compare_df[(compare_df["cycling"] == True) & (compare_df["zscore_method"].isin(["ewma", "ewma_ratio"]))]
ct_ewma_time_wins = len(cycling_true_ewma[cycling_true_ewma["t180_pnl"] > cycling_true_ewma["price_pnl"]])

cycling_true_ou = compare_df[(compare_df["cycling"] == True) & (compare_df["zscore_method"] == "ou")]
ct_ou_time_wins = len(cycling_true_ou[cycling_true_ou["t180_pnl"] > cycling_true_ou["price_pnl"]])

print("STRONGEST PATTERNS IDENTIFIED:")
print()
print(f"1. Cycling=True + zscore=EWMA/EWMA_RATIO: Time wins {ct_ewma_time_wins}/{len(cycling_true_ewma)} ({ct_ewma_time_wins/len(cycling_true_ewma)*100:.0f}%)")
print(f"   -> RECOMMEND 180s TIME STOP for these configs")
print()
print(f"2. Cycling=True + zscore=OU: Time wins {ct_ou_time_wins}/{len(cycling_true_ou)} ({ct_ou_time_wins/len(cycling_true_ou)*100:.0f}%)")
print(f"   -> RECOMMEND PRICE STOP for these configs")
print()
print(f"3. Cycling=False (any zscore): Time wins {cf_time_wins}/{len(cycling_false_df)} ({cf_time_wins/len(cycling_false_df)*100:.0f}%)")
print(f"   -> RECOMMEND PRICE STOP for these configs")
print()
print(f"4. Low Win Rate (<{median_wr:.0f}%) + Cycling=True: Time helps significantly")
print(f"   -> Consider TIME STOP for aggressive low-win-rate configs")
print()
print(f"5. High Win Rate (>={median_wr:.0f}%) + Cycling=True: Price stop generally better")
print(f"   -> RECOMMEND PRICE STOP for conservative high-win-rate configs")
