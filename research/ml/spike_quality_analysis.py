#!/usr/bin/env python3
"""
Spike Quality ML Analysis

Goal: Can we predict which spikes will have good hedge fills (loser drops 12c+)?

Key insight from handover: Only 43.5% of spikes have loser drop >= 12c.
OBI predicts direction but NOT hedge quality. Can ML find patterns?

Features available:
- OBI (up_imbalance, down_imbalance)
- Order book depth (5 levels × 2 sides × bid/ask)
- Velocity, acceleration, jerk
- Spike magnitude
- Time remaining
- Spread width
- Signal quality score

Target:
- good_spike = 1 if max_loser_drop >= 0.12 within 180s
- good_spike = 0 otherwise

Usage:
    python research/ml/spike_quality_analysis.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from tqdm import tqdm
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# CONFIGURATION
# =============================================================================

TIME_WINDOW_SECS = 180  # Look for loser drop within 180s
MIN_LOSER_DROP = 0.12   # 12 cents = "good" spike
SPIKE_LOOKBACK_TICKS = 72  # From TRADING_CONFIGS.py

# OU threshold parameters (from TRADING_CONFIGS.py)
OU_BASE_THRESHOLD = 0.02
OU_K_LOW = 0.5
OU_K_HIGH = 1.75
OU_SIGMOID_STEEPNESS = 1.5
OU_MIN_THRESHOLD = 0.015
OU_MAX_THRESHOLD = 0.10


# =============================================================================
# DATA LOADING
# =============================================================================

def load_oos7_data():
    """Load OOS7 observer + Binance data."""
    base_dir = Path("/Users/rananjaybika/polymarket-amm-bot")

    # Load Binance 60Hz data
    btc_path = base_dir / "research/binance_hf/btc_prices_20260129_160523.csv"
    print(f"Loading Binance HF: {btc_path.name}")
    btc_df = pd.read_csv(btc_path)
    print(f"  Rows: {len(btc_df):,}")

    # Load Observer data
    obs_dir = base_dir / "research/observer"
    obs_files = [
        obs_dir / "grid_obs_20260129.csv",
        obs_dir / "grid_obs_20260130.csv",
    ]

    print("\nLoading Observer data:")
    obs_dfs = []
    for f in obs_files:
        if f.exists():
            df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            obs_dfs.append(df)
            print(f"  {f.name}: {len(df):,} rows")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    obs_df = obs_df.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    print(f"  Combined: {len(obs_df):,} rows")

    # Load resolutions
    res_path = obs_dir / "market_resolutions_verified.csv"
    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    print(f"Resolutions: {len(res_map)} markets")

    return btc_df, obs_df, res_map


# =============================================================================
# SPIKE DETECTION WITH FEATURES
# =============================================================================

@dataclass
class SpikeWithFeatures:
    """Spike event with all features for ML."""
    timestamp_ms: int
    market_slug: str
    spike_direction: str
    spike_magnitude: float
    time_remaining: float

    # Order book imbalance
    obi_winner: float
    obi_loser: float
    obi_confirms: bool

    # Spread features
    winner_spread: float
    loser_spread: float

    # Depth features (sum of sizes)
    winner_bid_depth: float
    winner_ask_depth: float
    loser_bid_depth: float
    loser_ask_depth: float

    # Velocity/momentum
    velocity_bps: float
    acceleration_bps2: float
    jerk_bps3: float
    signal_quality: float
    momentum_5s: float

    # Target variable (computed later)
    max_loser_drop: float = 0.0
    loser_drop_time_ms: int = 0
    is_good_spike: bool = False

    # Resolution outcome
    correct_direction: bool = False


def extract_spike_features(row: pd.Series, spike_dir: str) -> SpikeWithFeatures:
    """Extract all features for a spike event."""
    # Determine winner/loser sides
    if spike_dir == "UP":
        winner_bid = row.get('up_bid', 0.5)
        winner_ask = row.get('up_ask', 0.5)
        loser_bid = row.get('down_bid', 0.5)
        loser_ask = row.get('down_ask', 0.5)
        obi_winner = row.get('up_imbalance', 0.0)
        obi_loser = row.get('down_imbalance', 0.0)

        # Depth sums
        winner_bid_depth = sum(row.get(f'up_bid_size_{i}', 0) or 0 for i in range(1, 6))
        winner_ask_depth = sum(row.get(f'up_ask_size_{i}', 0) or 0 for i in range(1, 6))
        loser_bid_depth = sum(row.get(f'down_bid_size_{i}', 0) or 0 for i in range(1, 6))
        loser_ask_depth = sum(row.get(f'down_ask_size_{i}', 0) or 0 for i in range(1, 6))
    else:
        winner_bid = row.get('down_bid', 0.5)
        winner_ask = row.get('down_ask', 0.5)
        loser_bid = row.get('up_bid', 0.5)
        loser_ask = row.get('up_ask', 0.5)
        obi_winner = row.get('down_imbalance', 0.0)
        obi_loser = row.get('up_imbalance', 0.0)

        # Depth sums
        winner_bid_depth = sum(row.get(f'down_bid_size_{i}', 0) or 0 for i in range(1, 6))
        winner_ask_depth = sum(row.get(f'down_ask_size_{i}', 0) or 0 for i in range(1, 6))
        loser_bid_depth = sum(row.get(f'up_bid_size_{i}', 0) or 0 for i in range(1, 6))
        loser_ask_depth = sum(row.get(f'up_ask_size_{i}', 0) or 0 for i in range(1, 6))

    # Check OBI confirms spike
    obi_confirms = False
    if pd.notna(obi_winner) and pd.notna(obi_loser):
        if spike_dir == "UP":
            obi_confirms = obi_winner > 0  # More buying pressure on winner
        else:
            obi_confirms = obi_winner > 0  # More buying pressure on winner (DOWN side)

    return SpikeWithFeatures(
        timestamp_ms=int(row['timestamp_ms']),
        market_slug=row['market_slug'],
        spike_direction=spike_dir,
        spike_magnitude=float(row.get('spike_magnitude', 0) or 0),
        time_remaining=float(row.get('time_remaining_secs', 0) or 0),

        obi_winner=float(obi_winner) if pd.notna(obi_winner) else 0.0,
        obi_loser=float(obi_loser) if pd.notna(obi_loser) else 0.0,
        obi_confirms=obi_confirms,

        winner_spread=float(winner_ask - winner_bid) if winner_ask and winner_bid else 0.0,
        loser_spread=float(loser_ask - loser_bid) if loser_ask and loser_bid else 0.0,

        winner_bid_depth=winner_bid_depth,
        winner_ask_depth=winner_ask_depth,
        loser_bid_depth=loser_bid_depth,
        loser_ask_depth=loser_ask_depth,

        velocity_bps=float(row.get('velocity_bps', 0) or 0),
        acceleration_bps2=float(row.get('acceleration_bps2', 0) or 0),
        jerk_bps3=float(row.get('jerk_bps3', 0) or 0),
        signal_quality=float(row.get('signal_quality', 0) or 0),
        momentum_5s=float(row.get('momentum_5s', 0) or 0),
    )


def compute_loser_drop(spike: SpikeWithFeatures, obs_df: pd.DataFrame) -> Tuple[float, int]:
    """
    Compute the maximum loser drop within TIME_WINDOW_SECS after spike.

    Returns (max_drop, time_to_drop_ms)
    """
    spike_ts = spike.timestamp_ms
    market = spike.market_slug
    end_ts = spike_ts + TIME_WINDOW_SECS * 1000

    # Get future data for this market
    future_data = obs_df[
        (obs_df['market_slug'] == market) &
        (obs_df['timestamp_ms'] > spike_ts) &
        (obs_df['timestamp_ms'] <= end_ts)
    ].copy()

    if len(future_data) == 0:
        return 0.0, 0

    # Get loser ask at spike time
    if spike.spike_direction == "UP":
        loser_col = 'down_ask'
    else:
        loser_col = 'up_ask'

    # Get initial loser ask (at spike)
    spike_row = obs_df[
        (obs_df['market_slug'] == market) &
        (obs_df['timestamp_ms'] == spike_ts)
    ]
    if len(spike_row) == 0:
        return 0.0, 0

    initial_loser_ask = spike_row[loser_col].iloc[0]
    if pd.isna(initial_loser_ask) or initial_loser_ask <= 0:
        return 0.0, 0

    # Find minimum loser ask in window (max drop)
    min_loser_ask = future_data[loser_col].min()
    if pd.isna(min_loser_ask):
        return 0.0, 0

    drop = initial_loser_ask - min_loser_ask

    # Find time to max drop
    min_idx = future_data[loser_col].idxmin()
    if pd.notna(min_idx):
        time_to_drop = int(future_data.loc[min_idx, 'timestamp_ms'] - spike_ts)
    else:
        time_to_drop = 0

    return max(0.0, drop), time_to_drop


# =============================================================================
# FEATURE ENGINEERING
# =============================================================================

def engineer_features(spikes: List[SpikeWithFeatures]) -> pd.DataFrame:
    """Convert spike list to feature DataFrame for ML."""
    records = []
    for s in spikes:
        records.append({
            # Target
            'is_good_spike': 1 if s.is_good_spike else 0,
            'max_loser_drop': s.max_loser_drop,
            'correct_direction': 1 if s.correct_direction else 0,

            # Core features
            'spike_magnitude': s.spike_magnitude,
            'time_remaining': s.time_remaining,

            # OBI features
            'obi_winner': s.obi_winner,
            'obi_loser': s.obi_loser,
            'obi_diff': s.obi_winner - s.obi_loser,
            'obi_confirms': 1 if s.obi_confirms else 0,

            # Spread features
            'winner_spread': s.winner_spread,
            'loser_spread': s.loser_spread,
            'spread_ratio': s.loser_spread / s.winner_spread if s.winner_spread > 0 else 1.0,

            # Depth features
            'winner_bid_depth': s.winner_bid_depth,
            'winner_ask_depth': s.winner_ask_depth,
            'loser_bid_depth': s.loser_bid_depth,
            'loser_ask_depth': s.loser_ask_depth,
            'winner_depth_imb': (s.winner_bid_depth - s.winner_ask_depth) / (s.winner_bid_depth + s.winner_ask_depth + 1),
            'loser_depth_imb': (s.loser_bid_depth - s.loser_ask_depth) / (s.loser_bid_depth + s.loser_ask_depth + 1),

            # Velocity features
            'velocity_bps': s.velocity_bps,
            'velocity_abs': abs(s.velocity_bps),
            'acceleration_bps2': s.acceleration_bps2,
            'jerk_bps3': s.jerk_bps3,
            'signal_quality': s.signal_quality,
            'momentum_5s': s.momentum_5s,

            # Derived features
            'magnitude_x_velocity': s.spike_magnitude * abs(s.velocity_bps),
            'obi_x_magnitude': s.obi_winner * s.spike_magnitude,
            'depth_ratio': (s.loser_bid_depth + 1) / (s.loser_ask_depth + 1),
        })

    return pd.DataFrame(records)


# =============================================================================
# ANALYSIS
# =============================================================================

def analyze_feature_importance(df: pd.DataFrame):
    """Analyze which features predict good spikes."""
    print("\n" + "=" * 70)
    print("FEATURE ANALYSIS: What Predicts Good Spikes?")
    print("=" * 70)

    target = 'is_good_spike'
    feature_cols = [c for c in df.columns if c not in ['is_good_spike', 'max_loser_drop', 'correct_direction']]

    # Basic statistics
    good_spikes = df[df[target] == 1]
    bad_spikes = df[df[target] == 0]

    print(f"\nTotal spikes: {len(df)}")
    print(f"Good spikes (loser drop >= 12c): {len(good_spikes)} ({100*len(good_spikes)/len(df):.1f}%)")
    print(f"Bad spikes: {len(bad_spikes)} ({100*len(bad_spikes)/len(df):.1f}%)")

    # Feature comparison
    print("\n" + "-" * 70)
    print("FEATURE COMPARISON: Good vs Bad Spikes")
    print("-" * 70)
    print(f"{'Feature':<25} {'Good Mean':>12} {'Bad Mean':>12} {'Diff':>10} {'Sig?':>6}")
    print("-" * 70)

    significant_features = []
    for col in feature_cols:
        good_mean = good_spikes[col].mean()
        bad_mean = bad_spikes[col].mean()
        diff_pct = 100 * (good_mean - bad_mean) / (abs(bad_mean) + 0.001)

        # Simple significance test: >20% difference
        is_sig = abs(diff_pct) > 20
        sig_marker = "***" if is_sig else ""

        if is_sig:
            significant_features.append((col, diff_pct))

        print(f"{col:<25} {good_mean:>12.4f} {bad_mean:>12.4f} {diff_pct:>+9.1f}% {sig_marker:>6}")

    print("\n" + "-" * 70)
    print("MOST PREDICTIVE FEATURES (>20% difference)")
    print("-" * 70)

    significant_features.sort(key=lambda x: abs(x[1]), reverse=True)
    for feat, diff in significant_features[:10]:
        direction = "HIGHER in good" if diff > 0 else "LOWER in good"
        print(f"  {feat}: {direction} ({diff:+.1f}%)")

    return significant_features


def analyze_correlations(df: pd.DataFrame):
    """Analyze correlations with target."""
    print("\n" + "=" * 70)
    print("CORRELATION ANALYSIS")
    print("=" * 70)

    target = 'is_good_spike'
    feature_cols = [c for c in df.columns if c not in ['is_good_spike', 'max_loser_drop', 'correct_direction']]

    correlations = []
    for col in feature_cols:
        corr = df[col].corr(df[target])
        if pd.notna(corr):
            correlations.append((col, corr))

    correlations.sort(key=lambda x: abs(x[1]), reverse=True)

    print(f"\n{'Feature':<30} {'Correlation':>12}")
    print("-" * 45)
    for feat, corr in correlations[:15]:
        stars = "***" if abs(corr) > 0.1 else "**" if abs(corr) > 0.05 else "*" if abs(corr) > 0.02 else ""
        print(f"{feat:<30} {corr:>+.4f} {stars}")

    return correlations


def try_simple_models(df: pd.DataFrame):
    """Try simple ML models to predict good spikes."""
    print("\n" + "=" * 70)
    print("SIMPLE ML MODEL EVALUATION")
    print("=" * 70)

    from sklearn.model_selection import cross_val_score, StratifiedKFold
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    target = 'is_good_spike'
    # Exclude non-numeric columns
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    feature_cols = [c for c in numeric_cols if c not in ['is_good_spike', 'max_loser_drop', 'correct_direction']]

    X = df[feature_cols].fillna(0)
    y = df[target]

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    models = {
        'Logistic Regression': LogisticRegression(max_iter=1000),
        'Random Forest': RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42),
        'Gradient Boosting': GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42),
    }

    print(f"\nBaseline (always predict majority class): {max(y.mean(), 1-y.mean()):.1%}")
    print("\nModel performance (5-fold CV accuracy):")
    print("-" * 50)

    best_model = None
    best_score = 0

    for name, model in models.items():
        try:
            scores = cross_val_score(model, X_scaled, y, cv=cv, scoring='accuracy')
            mean_score = scores.mean()
            std_score = scores.std()
            print(f"  {name:<25}: {mean_score:.1%} (+/- {std_score:.1%})")

            if mean_score > best_score:
                best_score = mean_score
                best_model = (name, model)
        except Exception as e:
            print(f"  {name:<25}: Error - {e}")

    # Feature importance from best tree model
    if best_model and 'Forest' in best_model[0] or 'Boosting' in best_model[0]:
        print("\n" + "-" * 50)
        print("Feature Importance (Random Forest)")
        print("-" * 50)

        rf = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
        rf.fit(X_scaled, y)

        importances = list(zip(feature_cols, rf.feature_importances_))
        importances.sort(key=lambda x: x[1], reverse=True)

        for feat, imp in importances[:10]:
            print(f"  {feat:<30}: {imp:.4f}")

    return best_model, best_score


def analyze_obi_specifically(df: pd.DataFrame):
    """Deep dive on OBI's predictive power."""
    print("\n" + "=" * 70)
    print("OBI DEEP DIVE")
    print("=" * 70)

    # Check if OBI predicts direction
    obi_correct = df['obi_confirms'] == df['correct_direction']
    print(f"\nOBI confirms direction: {df['obi_confirms'].sum()} ({100*df['obi_confirms'].mean():.1f}%)")
    print(f"Direction accuracy when OBI confirms: {100*df[df['obi_confirms']==1]['correct_direction'].mean():.1f}%")
    print(f"Direction accuracy when OBI disagrees: {100*df[df['obi_confirms']==0]['correct_direction'].mean():.1f}%")

    # Check if OBI predicts hedge quality
    print(f"\nGood spike rate when OBI confirms: {100*df[df['obi_confirms']==1]['is_good_spike'].mean():.1f}%")
    print(f"Good spike rate when OBI disagrees: {100*df[df['obi_confirms']==0]['is_good_spike'].mean():.1f}%")

    # OBI magnitude analysis
    print("\n" + "-" * 50)
    print("OBI Magnitude Bins")
    print("-" * 50)

    bins = [-1, -0.3, -0.1, 0.1, 0.3, 1]
    labels = ['Strong Sell', 'Mild Sell', 'Neutral', 'Mild Buy', 'Strong Buy']
    df['obi_bin'] = pd.cut(df['obi_winner'], bins=bins, labels=labels)

    for label in labels:
        subset = df[df['obi_bin'] == label]
        if len(subset) > 0:
            good_rate = 100 * subset['is_good_spike'].mean()
            count = len(subset)
            print(f"  {label:<15}: {good_rate:>5.1f}% good spikes (n={count})")


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("SPIKE QUALITY ML ANALYSIS")
    print("Can we predict which spikes will have good hedge fills?")
    print("=" * 70)

    # Load data
    btc_df, obs_df, res_map = load_oos7_data()

    # Find all spikes
    print("\n" + "=" * 70)
    print("EXTRACTING SPIKES WITH FEATURES")
    print("=" * 70)

    spike_rows = obs_df[obs_df['spike_detected'] == True].copy()
    print(f"\nSpikes detected: {len(spike_rows)}")

    # Extract features for each spike
    spikes = []
    for idx, row in tqdm(spike_rows.iterrows(), total=len(spike_rows), desc="Extracting features"):
        spike_dir = row.get('spike_direction', 'UP')
        if pd.isna(spike_dir):
            continue

        spike = extract_spike_features(row, spike_dir)

        # Compute max loser drop
        max_drop, drop_time = compute_loser_drop(spike, obs_df)
        spike.max_loser_drop = max_drop
        spike.loser_drop_time_ms = drop_time
        spike.is_good_spike = max_drop >= MIN_LOSER_DROP

        # Check resolution
        if spike.market_slug in res_map:
            resolution = res_map[spike.market_slug]
            spike.correct_direction = (
                (spike.spike_direction == "UP" and resolution == "UP") or
                (spike.spike_direction == "DOWN" and resolution == "DOWN")
            )

        spikes.append(spike)

    print(f"\nSpikes with features: {len(spikes)}")

    # Convert to DataFrame
    df = engineer_features(spikes)
    print(f"Feature DataFrame: {df.shape}")

    # Run analyses
    significant_features = analyze_feature_importance(df)
    correlations = analyze_correlations(df)
    analyze_obi_specifically(df)

    try:
        best_model, best_score = try_simple_models(df)
        print(f"\n{'=' * 70}")
        print("CONCLUSION")
        print("=" * 70)
        print(f"Best model: {best_model[0]} with {best_score:.1%} accuracy")
        print(f"Baseline: {max(df['is_good_spike'].mean(), 1-df['is_good_spike'].mean()):.1%}")
        improvement = best_score - max(df['is_good_spike'].mean(), 1-df['is_good_spike'].mean())
        print(f"Improvement over baseline: {improvement:+.1%}")

        if improvement < 0.05:
            print("\n⚠️  ML provides minimal improvement over baseline.")
            print("   Loser drop may be genuinely unpredictable from available features.")
        else:
            print(f"\n✓ ML can provide {improvement:.1%} improvement in spike selection.")
            print("  Top features to focus on:")
            for feat, _ in correlations[:5]:
                print(f"    - {feat}")
    except ImportError:
        print("\n⚠️  sklearn not available. Install with: pip install scikit-learn")

    # Save results
    output_path = Path("/Users/rananjaybika/polymarket-amm-bot/research/ml/spike_quality_features.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"\nFeature data saved to: {output_path}")


if __name__ == "__main__":
    main()
