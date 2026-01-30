#!/usr/bin/env python3
"""
SCORING FORMULA REDESIGN

Based on SIGNAL_ACCURACY_FINDINGS.md:
1. Current composite_score has R²=0.003, p=0.85 (USELESS)
2. spike × velocity interaction is significant (p=0.001)
3. time_remaining is the strongest predictor
4. Combined filters achieve 87.8% resolution accuracy

This script:
1. Explores additional features from raw observer/Binance data
2. Tests multiple new scoring approaches
3. Cross-validates to avoid overfitting
4. Compares all approaches
"""

import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.model_selection import cross_val_score, StratifiedKFold
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import accuracy_score, roc_auc_score, classification_report
import warnings
warnings.filterwarnings('ignore')

# =============================================================================
# CONSTANTS
# =============================================================================
OPTIMAL_TIME_WINDOW = (300, 600)  # Best accuracy window
PATH1_LOOKBACKS = [800, 1000, 1200]
PATH2_LOOKBACKS = [300, 400, 500]


def load_signal_data():
    """Load signal analysis results."""
    path1 = pd.read_csv("research/signal_path1_v2.csv")
    path2 = pd.read_csv("research/signal_path2_v2.csv")

    print(f"Path 1 signals: {len(path1)}")
    print(f"Path 2 signals: {len(path2)}")

    return path1, path2


def load_observer_data():
    """Load observer data with additional features."""
    obs_dir = Path("research/observer")
    obs_dfs = []

    for f in sorted(obs_dir.glob("grid_obs_*.csv")):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        obs_dfs.append(df)
        print(f"  Observer: {len(df):,} rows ({f.name})")

    obs_df = pd.concat(obs_dfs, ignore_index=True)
    return obs_df


def load_binance_data():
    """Load Binance HF data."""
    btc_dir = Path("research/binance_hf")
    btc_dfs = []

    for f in sorted(btc_dir.glob("btc_prices_*.csv")):
        df = pd.read_csv(f)
        btc_dfs.append(df)
        print(f"  Binance: {len(df):,} rows ({f.name})")

    btc_df = pd.concat(btc_dfs, ignore_index=True)
    btc_df = btc_df.drop_duplicates(subset=['timestamp_ms']).sort_values('timestamp_ms')
    return btc_df


def engineer_features(signals_df):
    """
    Engineer new features based on findings:
    1. spike × velocity interaction
    2. time window features
    3. regime-adjusted features
    """
    df = signals_df.copy()

    # 1. INTERACTION FEATURES (p=0.001)
    df['spike_x_velocity'] = df['spike_magnitude'] * df['velocity_bps'].abs()
    df['spike_x_time'] = df['spike_magnitude'] * df['time_remaining']
    df['velocity_x_time'] = df['velocity_bps'].abs() * df['time_remaining']

    # 2. TIME WINDOW FEATURES
    df['in_optimal_window'] = ((df['time_remaining'] >= 300) &
                               (df['time_remaining'] <= 600)).astype(int)
    df['time_urgency'] = 1 - np.clip(df['time_remaining'] / 900, 0, 1)
    df['time_squared'] = df['time_remaining'] ** 2

    # 3. REGIME FEATURES
    df['is_low_regime'] = (df['regime'] == 'LOW').astype(int)
    df['is_high_regime'] = (df['regime'] == 'HIGH').astype(int)
    df['is_medium_regime'] = (df['regime'] == 'MEDIUM').astype(int)

    # 4. VELOCITY DIRECTION FEATURES
    df['velocity_confirms'] = (
        ((df['predicted_side'] == 'UP') & (df['velocity_bps'] > 0)) |
        ((df['predicted_side'] == 'DOWN') & (df['velocity_bps'] < 0))
    ).astype(int)

    # 5. MAGNITUDE FEATURES
    df['spike_squared'] = df['spike_magnitude'] ** 2
    df['velocity_squared'] = df['velocity_bps'] ** 2

    # 6. NORMALIZED FEATURES
    df['spike_normalized'] = df['spike_magnitude'] / df['spike_magnitude'].quantile(0.95)
    df['velocity_normalized'] = df['velocity_bps'].abs() / df['velocity_bps'].abs().quantile(0.95)

    return df


def compute_btc_momentum_features(btc_df, window_seconds=[5, 10, 30]):
    """
    Compute momentum features from Binance data.
    These can be joined to signals to add BTC momentum context.
    """
    btc_df = btc_df.copy()
    btc_df['timestamp_s'] = btc_df['timestamp_ms'] // 1000

    # Price changes over different windows
    for w in window_seconds:
        ticks = w * 60  # 60Hz data
        btc_df[f'btc_momentum_{w}s'] = btc_df['price'].diff(ticks) / btc_df['price'].shift(ticks) * 100

    # Volatility (rolling std)
    btc_df['btc_volatility_5s'] = btc_df['price'].rolling(300).std() / btc_df['price'].rolling(300).mean() * 100
    btc_df['btc_volatility_30s'] = btc_df['price'].rolling(1800).std() / btc_df['price'].rolling(1800).mean() * 100

    # Trend strength (absolute momentum)
    btc_df['btc_trend_strength'] = btc_df['btc_momentum_10s'].abs()

    return btc_df


def compute_new_scores(df):
    """
    Compute multiple new scoring formulas to test.
    """
    df = df.copy()

    # SCORE 1: Interaction-based (main finding)
    df['score_interaction'] = (
        df['spike_magnitude'] * df['velocity_bps'].abs() *
        np.where(df['in_optimal_window'], 2.0, 1.0) *
        np.where(df['is_low_regime'], 0.3, 1.0)  # Penalize LOW regime
    )

    # SCORE 2: Time-weighted (time_remaining is strongest predictor)
    time_weight = 1 - np.abs(df['time_remaining'] - 450) / 450  # Peak at 450s
    time_weight = np.clip(time_weight, 0.1, 1.0)
    df['score_time_weighted'] = df['spike_magnitude'] * time_weight * (1 + df['velocity_bps'].abs())

    # SCORE 3: Binary threshold (simplest approach from findings)
    df['score_threshold'] = (
        (df['regime'] != 'LOW').astype(int) *
        df['in_optimal_window'] *
        (df['spike_magnitude'] >= 0.02).astype(int) *
        (df['spike_x_velocity'] >= df['spike_x_velocity'].median()).astype(int)
    )

    # SCORE 4: Logistic coefficients (from regression)
    # Using coefficients from analysis: time_remaining=-0.267 (strongest)
    df['score_logistic'] = (
        0.064 * df['spike_normalized'] +
        0.046 * df['velocity_normalized'] -
        0.267 * (df['time_remaining'] / 900) +  # Normalized
        0.5  # Intercept
    )

    # SCORE 5: Multiplicative with regime penalty
    df['score_multiplicative'] = (
        np.sqrt(df['spike_magnitude'] * df['velocity_bps'].abs()) *
        (1 - 0.5 * df['is_low_regime']) *
        df['time_urgency']
    )

    # SCORE 6: Enhanced interaction with confirmation
    df['score_enhanced'] = (
        df['spike_x_velocity'] *
        (1 + 0.3 * df['velocity_confirms']) *
        np.where(df['in_optimal_window'], 1.5, 0.7) *
        np.where(df['is_low_regime'], 0.2, 1.0)
    )

    return df


def evaluate_scores(df, score_columns, target='direction_resolution'):
    """
    Evaluate each score's predictive power.
    """
    results = []

    # Filter valid signals
    valid = df[df[target].notna()].copy()
    valid['target'] = valid[target].astype(int)

    print(f"\nEvaluating on {len(valid)} signals with resolution data")
    print("=" * 70)

    for score_col in score_columns:
        if score_col not in valid.columns:
            continue

        # Handle NaN scores
        score_valid = valid[valid[score_col].notna()].copy()
        if len(score_valid) < 10:
            continue

        # Compute thresholds
        median_score = score_valid[score_col].median()
        high_score = score_valid[score_valid[score_col] >= median_score]

        # Accuracy for high-score signals
        if len(high_score) > 0:
            high_score_accuracy = high_score['target'].mean()
            n_signals = len(high_score)
        else:
            high_score_accuracy = 0
            n_signals = 0

        # Correlation with target
        correlation = score_valid[score_col].corr(score_valid['target'])

        # AUC
        try:
            auc = roc_auc_score(score_valid['target'], score_valid[score_col])
        except:
            auc = 0.5

        results.append({
            'score': score_col,
            'correlation': correlation,
            'auc': auc,
            'high_score_accuracy': high_score_accuracy,
            'n_high_score': n_signals,
            'total_signals': len(score_valid)
        })

        print(f"{score_col:25s} | Corr: {correlation:+.3f} | AUC: {auc:.3f} | "
              f"High Score Acc: {high_score_accuracy:.1%} ({n_signals}/{len(score_valid)})")

    return pd.DataFrame(results)


def train_ml_models(df, feature_cols, target='direction_resolution'):
    """
    Train ML models to find optimal feature weights.
    """
    valid = df[df[target].notna()].copy()
    valid['target'] = valid[target].astype(int)

    # Filter to available features
    available_features = [f for f in feature_cols if f in valid.columns]
    X = valid[available_features].fillna(0)
    y = valid['target']

    print(f"\nTraining ML models on {len(X)} samples, {len(available_features)} features")
    print("=" * 70)

    # Standardize
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Cross-validation
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)

    models = {
        'Logistic Regression': LogisticRegression(max_iter=1000, random_state=42),
        'Random Forest': RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42),
        'Gradient Boosting': GradientBoostingClassifier(n_estimators=100, max_depth=3, random_state=42)
    }

    results = {}
    for name, model in models.items():
        # Cross-val accuracy
        cv_scores = cross_val_score(model, X_scaled, y, cv=cv, scoring='accuracy')
        cv_auc = cross_val_score(model, X_scaled, y, cv=cv, scoring='roc_auc')

        # Fit on all data for feature importance
        model.fit(X_scaled, y)

        results[name] = {
            'cv_accuracy': cv_scores.mean(),
            'cv_accuracy_std': cv_scores.std(),
            'cv_auc': cv_auc.mean(),
            'cv_auc_std': cv_auc.std(),
            'model': model
        }

        print(f"{name:25s} | CV Accuracy: {cv_scores.mean():.1%} ± {cv_scores.std():.1%} | "
              f"CV AUC: {cv_auc.mean():.3f} ± {cv_auc.std():.3f}")

    # Extract logistic regression coefficients
    if 'Logistic Regression' in results:
        lr_model = results['Logistic Regression']['model']
        coef_df = pd.DataFrame({
            'feature': available_features,
            'coefficient': lr_model.coef_[0]
        }).sort_values('coefficient', key=abs, ascending=False)

        print("\nLogistic Regression Coefficients (for new formula):")
        print("-" * 50)
        for _, row in coef_df.head(10).iterrows():
            print(f"  {row['feature']:30s} {row['coefficient']:+.4f}")

        results['coefficients'] = coef_df

    # Extract Random Forest importance
    if 'Random Forest' in results:
        rf_model = results['Random Forest']['model']
        importance_df = pd.DataFrame({
            'feature': available_features,
            'importance': rf_model.feature_importances_
        }).sort_values('importance', ascending=False)

        print("\nRandom Forest Feature Importance:")
        print("-" * 50)
        for _, row in importance_df.head(10).iterrows():
            print(f"  {row['feature']:30s} {row['importance']:.4f}")

        results['importance'] = importance_df

    return results


def analyze_by_regime_and_time(df, target='direction_resolution'):
    """
    Detailed analysis by regime and time window combinations.
    """
    valid = df[df[target].notna()].copy()
    valid['target'] = valid[target].astype(int)

    # Create time bins
    valid['time_bin'] = pd.cut(valid['time_remaining'],
                               bins=[0, 180, 300, 450, 600, 900],
                               labels=['0-180s', '180-300s', '300-450s', '450-600s', '600-900s'])

    print("\nAccuracy by Regime × Time Window:")
    print("=" * 70)

    pivot = valid.groupby(['regime', 'time_bin']).agg({
        'target': ['mean', 'count']
    }).round(3)
    pivot.columns = ['accuracy', 'count']
    print(pivot.to_string())

    # Find best combinations
    print("\nBest Combinations (>65% accuracy, >10 signals):")
    print("-" * 50)

    for regime in ['LOW', 'MEDIUM', 'HIGH']:
        for time_bin in ['0-180s', '180-300s', '300-450s', '450-600s', '600-900s']:
            subset = valid[(valid['regime'] == regime) & (valid['time_bin'] == time_bin)]
            if len(subset) >= 10:
                acc = subset['target'].mean()
                if acc >= 0.65:
                    print(f"  {regime:6s} × {time_bin:10s}: {acc:.1%} ({len(subset)} signals)")

    return valid


def generate_optimal_formula(coef_df, importance_df):
    """
    Generate the optimal scoring formula based on ML insights.
    """
    print("\n" + "=" * 70)
    print("RECOMMENDED NEW SCORING FORMULA")
    print("=" * 70)

    # Get top features from logistic regression
    top_lr = coef_df.head(6)

    # Get top features from random forest
    top_rf = importance_df.head(6)

    print("\nBased on statistical analysis, the new formula should:")
    print("1. Use spike × velocity interaction (p=0.001)")
    print("2. Weight heavily on time_remaining (strongest predictor)")
    print("3. Apply regime penalties (LOW → skip or 0.2x)")
    print("4. Prefer 300-600s window (88.9% accuracy)")

    print("\n" + "-" * 50)
    print("PROPOSED FORMULA (Python):")
    print("-" * 50)

    formula = '''
def compute_score_v2(spike_mag, velocity_bps, time_remaining, regime):
    """
    New scoring formula based on statistical analysis.

    Key changes from v1:
    1. Multiplicative interaction (not additive)
    2. Time window gating
    3. Regime penalty
    """
    # Skip LOW regime entirely (48% accuracy = worse than random)
    if regime == 'LOW':
        return 0.0

    # Time window score: peaks in 300-600s window
    if 300 <= time_remaining <= 600:
        time_score = 1.0  # Optimal window
    elif 180 <= time_remaining < 300 or 600 < time_remaining <= 750:
        time_score = 0.6  # Acceptable window
    else:
        time_score = 0.3  # Poor window

    # Interaction effect (the key finding)
    interaction = spike_mag * abs(velocity_bps)

    # Direction confirmation bonus
    # (already filtered by velocity_confirms in signal detection)

    # Final score: multiplicative not additive
    score = interaction * time_score

    # Optional: regime bonus for HIGH volatility
    if regime == 'HIGH':
        score *= 1.2

    return score
'''
    print(formula)

    print("\n" + "-" * 50)
    print("ALTERNATIVE: THRESHOLD-BASED (No Score)")
    print("-" * 50)

    threshold = '''
def should_trade(spike_mag, velocity_bps, time_remaining, regime,
                 median_interaction=0.01):
    """
    Threshold-based approach - simpler, potentially more robust.
    Based on combined filter achieving 87.8% resolution accuracy.
    """
    # Hard filters
    if regime == 'LOW':
        return False

    if time_remaining < 300 or time_remaining > 600:
        return False

    if spike_mag < 0.02:
        return False

    # Interaction threshold
    interaction = spike_mag * abs(velocity_bps)
    if interaction < median_interaction:
        return False

    return True
'''
    print(threshold)

    return formula, threshold


def main():
    print("=" * 70)
    print("SCORING FORMULA REDESIGN ANALYSIS")
    print("=" * 70)

    # Load data
    print("\n[1] Loading Data...")
    path1, path2 = load_signal_data()

    # Combine for analysis
    signals = pd.concat([path1, path2], ignore_index=True)
    print(f"Total signals: {len(signals)}")

    # Engineer features
    print("\n[2] Engineering Features...")
    signals = engineer_features(signals)

    # Compute new scores
    print("\n[3] Computing New Scoring Formulas...")
    signals = compute_new_scores(signals)

    # Evaluate scores
    print("\n[4] Evaluating All Scores...")
    score_columns = [
        'composite_score',  # Current (baseline)
        'score_interaction',
        'score_time_weighted',
        'score_threshold',
        'score_logistic',
        'score_multiplicative',
        'score_enhanced'
    ]
    score_results = evaluate_scores(signals, score_columns)

    # Feature list for ML
    feature_cols = [
        'spike_magnitude', 'velocity_bps', 'time_remaining',
        'spike_x_velocity', 'spike_x_time', 'velocity_x_time',
        'in_optimal_window', 'time_urgency', 'time_squared',
        'is_low_regime', 'is_high_regime', 'is_medium_regime',
        'velocity_confirms', 'spike_squared', 'velocity_squared',
        'spike_normalized', 'velocity_normalized'
    ]

    # Train ML models
    print("\n[5] Training ML Models...")
    ml_results = train_ml_models(signals, feature_cols)

    # Regime × Time analysis
    print("\n[6] Regime × Time Window Analysis...")
    analyze_by_regime_and_time(signals)

    # Generate formula
    print("\n[7] Generating Optimal Formula...")
    if 'coefficients' in ml_results and 'importance' in ml_results:
        generate_optimal_formula(ml_results['coefficients'], ml_results['importance'])

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    print("\nScore Comparison (sorted by AUC):")
    print("-" * 50)
    summary = score_results.sort_values('auc', ascending=False)
    for _, row in summary.iterrows():
        print(f"  {row['score']:25s} AUC={row['auc']:.3f} Corr={row['correlation']:+.3f}")

    best = summary.iloc[0]
    print(f"\n*** BEST SCORE: {best['score']} (AUC={best['auc']:.3f}) ***")

    if 'Logistic Regression' in ml_results:
        lr = ml_results['Logistic Regression']
        print(f"\nML Upper Bound (Logistic): {lr['cv_accuracy']:.1%} ± {lr['cv_accuracy_std']:.1%}")

    # Save results
    signals.to_csv("research/signals_with_new_scores.csv", index=False)
    score_results.to_csv("research/score_comparison.csv", index=False)
    print("\nResults saved to research/signals_with_new_scores.csv")
    print("Comparison saved to research/score_comparison.csv")


if __name__ == "__main__":
    main()
