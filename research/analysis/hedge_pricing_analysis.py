#!/usr/bin/env python3
"""
Hedge Pricing Analysis: Linear vs Multiple Regression

Compares the current simple linear hedge pricing model:
    expected_drop = 0.68 * spike_mag + 0.01

Against multiple regression models that include:
- spike_magnitude
- velocity_bps
- time_remaining
- regime
- interaction terms (spike × velocity)

Goal: Determine if multiple regression improves R² significantly (>50% improvement)
to justify updating the calc_loser_bid() function.
"""

import argparse
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
import warnings
warnings.filterwarnings('ignore')

# Regression
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import cross_val_score, KFold
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import r2_score, mean_squared_error
import statsmodels.api as sm


# =============================================================================
# CONSTANTS
# =============================================================================

RESEARCH_DIR = Path("/Users/rananjaybika/polymarket-amm-bot/research")
OBSERVER_DIR = RESEARCH_DIR / "observer"
BINANCE_DIR = RESEARCH_DIR / "binance_hf"

# Current linear model parameters (from spike_param_optimizer.py)
DROP_MULTIPLIER = 0.68
DROP_INTERCEPT = 0.01


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class RegressionResult:
    """Result from a regression model."""
    name: str
    r2: float
    adj_r2: float
    rmse: float
    cv_r2_mean: float
    cv_r2_std: float
    coefficients: Dict[str, float]
    p_values: Dict[str, float]
    n_samples: int


# =============================================================================
# DATA LOADING
# =============================================================================

def load_signal_data() -> pd.DataFrame:
    """Load signal data from both paths."""
    print("Loading signal data...")

    dfs = []
    for path_name in ["signal_path1_v2.csv", "signal_path2_v2.csv"]:
        path = RESEARCH_DIR / path_name
        if path.exists():
            df = pd.read_csv(path)
            df['path'] = path_name.replace("signal_", "").replace("_v2.csv", "")
            dfs.append(df)
            print(f"  {path_name}: {len(df)} signals")

    if not dfs:
        raise FileNotFoundError("No signal data found")

    combined = pd.concat(dfs, ignore_index=True)
    print(f"  Total signals: {len(combined)}")
    return combined


def load_observer_data() -> pd.DataFrame:
    """Load all observer grid data."""
    print("Loading observer data...")

    obs_files = list(OBSERVER_DIR.glob("grid_obs_*.csv"))
    if not obs_files:
        raise FileNotFoundError("No observer data found")

    dfs = []
    for f in sorted(obs_files):
        df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
        dfs.append(df)
        print(f"  {f.name}: {len(df)} rows")

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=['timestamp_ms', 'market_slug'])
    combined = combined.sort_values(['market_slug', 'timestamp_ms'])
    print(f"  Total observer rows: {len(combined)}")
    return combined


def load_resolutions() -> Dict[str, str]:
    """Load market resolutions."""
    print("Loading resolutions...")

    res_path = OBSERVER_DIR / "market_resolutions_verified.csv"
    if not res_path.exists():
        raise FileNotFoundError("market_resolutions_verified.csv not found")

    res_df = pd.read_csv(res_path)
    res_map = dict(zip(res_df['slug'], res_df['winner']))
    print(f"  {len(res_map)} resolved markets")
    return res_map


# =============================================================================
# FEATURE ENGINEERING
# =============================================================================

def compute_actual_loser_drops(signals_df: pd.DataFrame,
                                obs_df: pd.DataFrame,
                                res_map: Dict[str, str]) -> pd.DataFrame:
    """
    Compute actual loser price drop for each signal.

    For each signal:
    1. entry_loser_ask: loser ask price at signal time
    2. min_loser_ask: minimum loser ask from signal to resolution
    3. actual_drop: entry_loser_ask - min_loser_ask

    This tells us how much the loser side actually dropped after the signal,
    which we use to train the drop prediction model.
    """
    print("\nComputing actual loser drops...")

    results = []
    skipped_no_obs = 0
    skipped_no_resolution = 0

    # Group observer data by market for faster lookup
    obs_by_market = {slug: group for slug, group in obs_df.groupby('market_slug')}

    for idx, signal in signals_df.iterrows():
        market_slug = signal['market_slug']
        signal_ts = signal['timestamp_ms']
        predicted_side = signal['predicted_side']

        # Get resolution
        resolution = res_map.get(market_slug)
        if resolution not in ['UP', 'DOWN']:
            skipped_no_resolution += 1
            continue

        # Get observer data for this market
        if market_slug not in obs_by_market:
            skipped_no_obs += 1
            continue

        mdf = obs_by_market[market_slug]

        # Filter to rows from signal time onwards
        future_obs = mdf[mdf['timestamp_ms'] >= signal_ts].copy()
        if len(future_obs) == 0:
            skipped_no_obs += 1
            continue

        # Determine loser side (opposite of predicted winner)
        loser_side = 'DOWN' if predicted_side == 'UP' else 'UP'

        # Get loser ask column
        loser_ask_col = 'down_ask' if loser_side == 'DOWN' else 'up_ask'

        # Entry loser ask (at signal time)
        entry_obs = future_obs.iloc[0]
        entry_loser_ask = entry_obs[loser_ask_col]

        # Minimum loser ask from signal to resolution
        # Convert to numeric and handle any bad data
        loser_asks = pd.to_numeric(future_obs[loser_ask_col], errors='coerce')
        min_loser_ask = loser_asks.min()

        if pd.isna(entry_loser_ask) or pd.isna(min_loser_ask):
            skipped_no_obs += 1
            continue

        # Actual drop
        actual_drop = entry_loser_ask - min_loser_ask

        # Was direction correct?
        direction_correct = predicted_side == resolution

        results.append({
            'market_slug': market_slug,
            'timestamp_ms': signal_ts,
            'predicted_side': predicted_side,
            'resolution': resolution,
            'direction_correct': direction_correct,
            'spike_magnitude': signal['spike_magnitude'],
            'velocity_bps': signal['velocity_bps'],
            'time_remaining': signal['time_remaining'],
            'regime': signal['regime'],
            'entry_loser_ask': entry_loser_ask,
            'min_loser_ask': min_loser_ask,
            'actual_drop': actual_drop,
            'path': signal.get('path', 'unknown'),
        })

    print(f"  Computed drops for {len(results)} signals")
    print(f"  Skipped (no obs data): {skipped_no_obs}")
    print(f"  Skipped (no resolution): {skipped_no_resolution}")

    return pd.DataFrame(results)


def prepare_features(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Prepare feature matrix and target variable.

    Features:
    - spike_magnitude (raw)
    - velocity_bps (absolute value)
    - time_remaining (seconds)
    - regime_HIGH (binary)
    - regime_MEDIUM (binary, reference is LOW)
    - spike_x_velocity (interaction term)

    Target:
    - actual_drop
    """
    # Filter to correct direction signals only (these are the ones we hedge)
    df_correct = df[df['direction_correct'] == True].copy()

    # Remove outliers (drops > 0.5 or negative are likely data issues)
    df_correct = df_correct[(df_correct['actual_drop'] >= 0) &
                             (df_correct['actual_drop'] <= 0.5)]

    # Create features
    X = pd.DataFrame()
    X['spike_magnitude'] = df_correct['spike_magnitude']
    X['velocity_bps_abs'] = df_correct['velocity_bps'].abs()
    X['time_remaining'] = df_correct['time_remaining']

    # Regime dummies (LOW is reference)
    X['regime_HIGH'] = (df_correct['regime'] == 'HIGH').astype(int)
    X['regime_MEDIUM'] = (df_correct['regime'] == 'MEDIUM').astype(int)

    # Interaction term
    X['spike_x_velocity'] = X['spike_magnitude'] * X['velocity_bps_abs']

    # Target
    y = df_correct['actual_drop']

    return X, y


# =============================================================================
# REGRESSION MODELS
# =============================================================================

def run_simple_linear(X: pd.DataFrame, y: pd.Series) -> RegressionResult:
    """
    Model 1: Simple Linear (Baseline)
    drop ~ spike_magnitude
    """
    X_simple = X[['spike_magnitude']].copy()
    X_const = sm.add_constant(X_simple)

    model = sm.OLS(y, X_const).fit()

    # Cross-validation
    cv = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(LinearRegression(), X_simple, y, cv=cv, scoring='r2')

    return RegressionResult(
        name="Simple Linear (spike_mag only)",
        r2=model.rsquared,
        adj_r2=model.rsquared_adj,
        rmse=np.sqrt(mean_squared_error(y, model.predict(X_const))),
        cv_r2_mean=cv_scores.mean(),
        cv_r2_std=cv_scores.std(),
        coefficients={
            'const': model.params['const'],
            'spike_magnitude': model.params['spike_magnitude'],
        },
        p_values={
            'const': model.pvalues['const'],
            'spike_magnitude': model.pvalues['spike_magnitude'],
        },
        n_samples=len(y)
    )


def run_multiple_regression(X: pd.DataFrame, y: pd.Series) -> RegressionResult:
    """
    Model 2: Multiple Regression
    drop ~ spike_mag + velocity_bps + time_remaining + regime
    """
    X_multi = X[['spike_magnitude', 'velocity_bps_abs', 'time_remaining',
                  'regime_HIGH', 'regime_MEDIUM']].copy()
    X_const = sm.add_constant(X_multi)

    model = sm.OLS(y, X_const).fit()

    # Cross-validation
    cv = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(LinearRegression(), X_multi, y, cv=cv, scoring='r2')

    coefficients = {col: model.params[col] for col in X_const.columns}
    p_values = {col: model.pvalues[col] for col in X_const.columns}

    return RegressionResult(
        name="Multiple Regression",
        r2=model.rsquared,
        adj_r2=model.rsquared_adj,
        rmse=np.sqrt(mean_squared_error(y, model.predict(X_const))),
        cv_r2_mean=cv_scores.mean(),
        cv_r2_std=cv_scores.std(),
        coefficients=coefficients,
        p_values=p_values,
        n_samples=len(y)
    )


def run_interaction_model(X: pd.DataFrame, y: pd.Series) -> RegressionResult:
    """
    Model 3: With Interactions
    drop ~ spike_mag + velocity_bps + spike*velocity + regime
    """
    X_inter = X[['spike_magnitude', 'velocity_bps_abs', 'time_remaining',
                  'regime_HIGH', 'regime_MEDIUM', 'spike_x_velocity']].copy()
    X_const = sm.add_constant(X_inter)

    model = sm.OLS(y, X_const).fit()

    # Cross-validation
    cv = KFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(LinearRegression(), X_inter, y, cv=cv, scoring='r2')

    coefficients = {col: model.params[col] for col in X_const.columns}
    p_values = {col: model.pvalues[col] for col in X_const.columns}

    return RegressionResult(
        name="With Interactions",
        r2=model.rsquared,
        adj_r2=model.rsquared_adj,
        rmse=np.sqrt(mean_squared_error(y, model.predict(X_const))),
        cv_r2_mean=cv_scores.mean(),
        cv_r2_std=cv_scores.std(),
        coefficients=coefficients,
        p_values=p_values,
        n_samples=len(y)
    )


def run_path_specific_models(X: pd.DataFrame, y: pd.Series,
                              paths: pd.Series) -> Tuple[Optional[RegressionResult],
                                                          Optional[RegressionResult]]:
    """
    Model 4: Path-Specific Models
    Separate models for Path 1 (800-1200ms) vs Path 2 (300-600ms)
    """
    results = []

    for path_name in ['path1', 'path2']:
        mask = paths == path_name
        if mask.sum() < 30:  # Need minimum samples
            results.append(None)
            continue

        X_path = X[mask][['spike_magnitude', 'velocity_bps_abs', 'time_remaining',
                          'regime_HIGH', 'regime_MEDIUM', 'spike_x_velocity']].copy()
        y_path = y[mask]
        X_const = sm.add_constant(X_path)

        model = sm.OLS(y_path, X_const).fit()

        # Cross-validation
        cv = KFold(n_splits=min(5, len(y_path) // 10), shuffle=True, random_state=42)
        cv_scores = cross_val_score(LinearRegression(), X_path, y_path, cv=cv, scoring='r2')

        coefficients = {col: model.params[col] for col in X_const.columns}
        p_values = {col: model.pvalues[col] for col in X_const.columns}

        results.append(RegressionResult(
            name=f"Path-Specific ({path_name})",
            r2=model.rsquared,
            adj_r2=model.rsquared_adj,
            rmse=np.sqrt(mean_squared_error(y_path, model.predict(X_const))),
            cv_r2_mean=cv_scores.mean(),
            cv_r2_std=cv_scores.std(),
            coefficients=coefficients,
            p_values=p_values,
            n_samples=len(y_path)
        ))

    return tuple(results)


# =============================================================================
# ANALYSIS & OUTPUT
# =============================================================================

def print_model_comparison(results: List[RegressionResult]):
    """Print comparison table of all models."""
    print("\n" + "=" * 90)
    print("MODEL COMPARISON")
    print("=" * 90)
    print()
    print(f"{'Model':<35} {'R²':>8} {'Adj R²':>8} {'RMSE':>8} {'CV R²':>12} {'N':>6}")
    print("-" * 90)

    for r in results:
        if r is None:
            continue
        cv_str = f"{r.cv_r2_mean:.3f}±{r.cv_r2_std:.3f}"
        print(f"{r.name:<35} {r.r2:>8.4f} {r.adj_r2:>8.4f} {r.rmse:>8.4f} {cv_str:>12} {r.n_samples:>6}")

    print()


def print_coefficient_details(result: RegressionResult):
    """Print detailed coefficients for a model."""
    print(f"\n{'='*60}")
    print(f"COEFFICIENTS: {result.name}")
    print(f"{'='*60}")
    print()
    print(f"{'Variable':<25} {'Coefficient':>12} {'p-value':>12} {'Significant':>12}")
    print("-" * 60)

    for var in result.coefficients:
        coef = result.coefficients[var]
        pval = result.p_values[var]
        sig = "***" if pval < 0.001 else "**" if pval < 0.01 else "*" if pval < 0.05 else ""
        print(f"{var:<25} {coef:>12.6f} {pval:>12.4f} {sig:>12}")

    print()


def generate_new_formula(best_result: RegressionResult) -> str:
    """Generate new calc_loser_bid function based on best model."""
    coef = best_result.coefficients

    # Extract coefficients with defaults
    const = coef.get('const', 0.01)
    spike_coef = coef.get('spike_magnitude', 0.68)
    velocity_coef = coef.get('velocity_bps_abs', 0.0)
    time_coef = coef.get('time_remaining', 0.0)
    regime_high = coef.get('regime_HIGH', 0.0)
    interaction = coef.get('spike_x_velocity', 0.0)

    formula = f'''
def calc_loser_bid_v2(winner_entry: float, spike_mag: float,
                       velocity_bps: float = 0.0, time_remaining: float = 500.0,
                       regime: str = "MEDIUM") -> float:
    """
    Multiple regression hedge pricing.

    Based on statistical analysis of actual loser drops.
    R² = {best_result.r2:.4f}, RMSE = {best_result.rmse:.4f}
    """
    # Base intercept
    expected_drop = {const:.6f}

    # Spike magnitude term (primary predictor)
    expected_drop += {spike_coef:.6f} * spike_mag / 100

    # Velocity term (absolute value)
    expected_drop += {velocity_coef:.6f} * abs(velocity_bps)

    # Time remaining term
    expected_drop += {time_coef:.8f} * time_remaining

    # Regime bonus (HIGH regime tends to have larger drops)
    if regime == "HIGH":
        expected_drop += {regime_high:.6f}

    # Interaction term (spike × velocity)
    expected_drop += {interaction:.6f} * spike_mag / 100 * abs(velocity_bps)

    # Clamp to reasonable range
    expected_drop = max(0.005, min(0.15, expected_drop))

    # Calculate loser bid
    TARGET_PAIR_COST = 0.99
    max_loser = TARGET_PAIR_COST - winner_entry
    loser_bid = min((1.0 - winner_entry) - expected_drop, max_loser)

    return max(0.01, min(0.95, loser_bid))
'''
    return formula


def analyze_improvement(simple: RegressionResult, best: RegressionResult) -> Dict:
    """Analyze improvement from simple to best model."""
    r2_improvement = (best.r2 - simple.r2) / simple.r2 * 100 if simple.r2 > 0 else 0
    rmse_improvement = (simple.rmse - best.rmse) / simple.rmse * 100 if simple.rmse > 0 else 0
    cv_improvement = (best.cv_r2_mean - simple.cv_r2_mean) / simple.cv_r2_mean * 100 if simple.cv_r2_mean > 0 else 0

    return {
        'r2_improvement_pct': r2_improvement,
        'rmse_improvement_pct': rmse_improvement,
        'cv_improvement_pct': cv_improvement,
        'recommend_upgrade': r2_improvement > 50 and best.cv_r2_mean > simple.cv_r2_mean,
    }


def save_analysis_results(df: pd.DataFrame, results: List[RegressionResult],
                           improvement: Dict, output_path: str):
    """Save analysis results to CSV."""
    # Model comparison
    rows = []
    for r in results:
        if r is None:
            continue
        rows.append({
            'model': r.name,
            'r2': r.r2,
            'adj_r2': r.adj_r2,
            'rmse': r.rmse,
            'cv_r2_mean': r.cv_r2_mean,
            'cv_r2_std': r.cv_r2_std,
            'n_samples': r.n_samples,
        })

    comparison_df = pd.DataFrame(rows)
    comparison_df.to_csv(output_path, index=False)
    print(f"\nResults saved to: {output_path}")


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Hedge Pricing Analysis")
    parser.add_argument("--output", type=str, default="hedge_analysis_results.csv",
                        help="Output CSV file path")
    args = parser.parse_args()

    print("=" * 90)
    print("HEDGE PRICING ANALYSIS: LINEAR VS MULTIPLE REGRESSION")
    print("=" * 90)
    print()

    # Load data
    signals_df = load_signal_data()
    obs_df = load_observer_data()
    res_map = load_resolutions()

    # Compute actual loser drops
    drops_df = compute_actual_loser_drops(signals_df, obs_df, res_map)

    if len(drops_df) < 30:
        print("\nERROR: Not enough data points for regression analysis")
        return

    # Prepare features
    X, y = prepare_features(drops_df)
    print(f"\nPrepared {len(X)} samples for regression")
    print(f"  Mean actual drop: {y.mean():.4f}")
    print(f"  Std actual drop: {y.std():.4f}")
    print(f"  Min/Max: {y.min():.4f} / {y.max():.4f}")

    # Run all models
    print("\nRunning regression models...")

    model_simple = run_simple_linear(X, y)
    model_multi = run_multiple_regression(X, y)
    model_inter = run_interaction_model(X, y)

    # Path-specific models (if we have path info)
    paths = drops_df.loc[X.index, 'path'] if 'path' in drops_df.columns else None
    if paths is not None:
        model_path1, model_path2 = run_path_specific_models(X, y, paths)
    else:
        model_path1, model_path2 = None, None

    # Collect all results
    all_results = [model_simple, model_multi, model_inter, model_path1, model_path2]
    valid_results = [r for r in all_results if r is not None]

    # Print comparison
    print_model_comparison(valid_results)

    # Find best model (by CV R²)
    best_model = max(valid_results, key=lambda r: r.cv_r2_mean)

    # Print coefficient details for best model
    print_coefficient_details(model_simple)
    print_coefficient_details(best_model)

    # Analyze improvement
    improvement = analyze_improvement(model_simple, best_model)

    print("\n" + "=" * 90)
    print("IMPROVEMENT ANALYSIS")
    print("=" * 90)
    print()
    print(f"Simple Linear R²:     {model_simple.r2:.4f}")
    print(f"Best Model R²:        {best_model.r2:.4f} ({best_model.name})")
    print(f"R² Improvement:       {improvement['r2_improvement_pct']:.1f}%")
    print(f"RMSE Improvement:     {improvement['rmse_improvement_pct']:.1f}%")
    print(f"CV R² Improvement:    {improvement['cv_improvement_pct']:.1f}%")
    print()

    # Recommendation
    print("=" * 90)
    print("RECOMMENDATION")
    print("=" * 90)
    print()

    if improvement['recommend_upgrade']:
        print("✓ RECOMMEND UPGRADE to multiple regression model")
        print(f"  - R² improves by {improvement['r2_improvement_pct']:.1f}% (>50% threshold)")
        print(f"  - Cross-validation confirms improvement")
        print()
        print("NEW FORMULA:")
        print(generate_new_formula(best_model))
    else:
        print("✗ KEEP CURRENT simple linear model")
        if improvement['r2_improvement_pct'] <= 50:
            print(f"  - R² improvement ({improvement['r2_improvement_pct']:.1f}%) does not exceed 50% threshold")
        if best_model.cv_r2_mean <= model_simple.cv_r2_mean:
            print(f"  - Cross-validation does not confirm improvement")
        print()
        print("Current formula remains optimal:")
        print(f"  expected_drop = {DROP_MULTIPLIER} * spike_mag / 100 + {DROP_INTERCEPT}")

    # Save results
    output_path = RESEARCH_DIR / args.output
    save_analysis_results(drops_df, valid_results, improvement, str(output_path))

    # Summary statistics
    print("\n" + "=" * 90)
    print("SUMMARY STATISTICS")
    print("=" * 90)
    print()
    print(f"Total signals analyzed: {len(drops_df)}")
    print(f"Correct direction signals: {drops_df['direction_correct'].sum()}")
    print(f"Samples used in regression: {len(X)}")
    print()

    # By regime
    print("Actual drops by regime:")
    for regime in ['LOW', 'MEDIUM', 'HIGH']:
        mask = drops_df['regime'] == regime
        if mask.sum() > 0:
            mean_drop = drops_df.loc[mask & drops_df['direction_correct'], 'actual_drop'].mean()
            print(f"  {regime}: {mean_drop:.4f} avg drop")

    # By path
    print("\nActual drops by path:")
    for path in drops_df['path'].unique():
        mask = drops_df['path'] == path
        if mask.sum() > 0:
            mean_drop = drops_df.loc[mask & drops_df['direction_correct'], 'actual_drop'].mean()
            print(f"  {path}: {mean_drop:.4f} avg drop")

    print("\n" + "=" * 90)


if __name__ == "__main__":
    main()
