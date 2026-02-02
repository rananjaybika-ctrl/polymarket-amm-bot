"""
Compute Gabagool's Market Bias

Gabagool's trading pattern reveals which side (UP/DOWN) he predicts will win:
- He pays a 2.6% adverse selection premium
- When UP-biased: pays $0.572 for UP vs $0.454 for DOWN (pays MORE for predicted winner)
- When DOWN-biased: pays $0.455 for UP vs $0.571 for DOWN

This module extracts his bias per market by comparing VWAP of UP vs DOWN purchases.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Tuple, Optional, List
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
FINDINGS_DIR = PROJECT_ROOT / "research" / "findings" / "data"


def compute_gabagool_bias(
    trades_df: pd.DataFrame,
    market_slug: str
) -> Tuple[str, float, Dict[str, float]]:
    """
    Compute which side Gabagool favored in a market.

    Gabagool pays MORE for the side he thinks will win (adverse selection).
    So his "bias" is the side with higher VWAP.

    Args:
        trades_df: DataFrame with trades (columns: market_slug, outcome, price, size)
        market_slug: Market identifier

    Returns:
        Tuple of (bias: 'UP'|'DOWN', premium: float, details: dict)
    """
    # Filter to this market
    market_trades = trades_df[trades_df['market_slug'] == market_slug].copy()

    if len(market_trades) == 0:
        return None, 0.0, {}

    # Standardize outcome names
    market_trades['outcome_std'] = market_trades['outcome'].str.upper().str.strip()
    market_trades.loc[market_trades['outcome_std'] == 'DOWN', 'outcome_std'] = 'DOWN'
    market_trades.loc[market_trades['outcome_std'] == 'UP', 'outcome_std'] = 'UP'

    # Separate UP and DOWN trades
    up_trades = market_trades[market_trades['outcome_std'] == 'UP']
    down_trades = market_trades[market_trades['outcome_std'] == 'DOWN']

    # Compute VWAP for each side
    def vwap(df):
        if len(df) == 0 or df['size'].sum() == 0:
            return np.nan
        return (df['price'] * df['size']).sum() / df['size'].sum()

    up_vwap = vwap(up_trades)
    down_vwap = vwap(down_trades)

    # Determine bias
    if pd.isna(up_vwap) and pd.isna(down_vwap):
        return None, 0.0, {}
    elif pd.isna(up_vwap):
        bias = 'DOWN'
        premium = 0.0
    elif pd.isna(down_vwap):
        bias = 'UP'
        premium = 0.0
    else:
        bias = 'UP' if up_vwap > down_vwap else 'DOWN'
        premium = abs(up_vwap - down_vwap)

    details = {
        'up_vwap': up_vwap,
        'down_vwap': down_vwap,
        'up_trades': len(up_trades),
        'down_trades': len(down_trades),
        'up_volume': up_trades['size'].sum(),
        'down_volume': down_trades['size'].sum(),
        'total_trades': len(market_trades),
    }

    return bias, premium, details


def compute_all_market_biases(
    trades_df: pd.DataFrame,
    min_trades: int = 10
) -> pd.DataFrame:
    """
    Compute Gabagool's bias for all markets.

    Args:
        trades_df: DataFrame with all trades
        min_trades: Minimum trades to compute bias (default 10)

    Returns:
        DataFrame with columns: market_slug, bias, premium, up_vwap, down_vwap, ...
    """
    logger.info(f"Computing bias for markets with >= {min_trades} trades")

    markets = trades_df['market_slug'].unique()
    results = []

    for slug in markets:
        bias, premium, details = compute_gabagool_bias(trades_df, slug)

        if bias is None:
            continue

        if details.get('total_trades', 0) < min_trades:
            continue

        results.append({
            'market_slug': slug,
            'bias': bias,
            'premium': premium,
            **details,
        })

    df = pd.DataFrame(results)
    logger.info(f"Computed bias for {len(df)} markets")

    return df


def validate_bias_against_resolutions(
    bias_df: pd.DataFrame,
    resolutions: Dict[str, str]
) -> Dict[str, any]:
    """
    Validate Gabagool's bias predictions against actual market outcomes.

    His bias should match resolution ~70% of time (his known accuracy).

    Args:
        bias_df: DataFrame with market_slug and bias columns
        resolutions: Dict mapping market_slug -> 'UP'|'DOWN'

    Returns:
        Dict with accuracy metrics
    """
    # Add resolution to bias_df
    bias_df = bias_df.copy()
    bias_df['resolution'] = bias_df['market_slug'].map(resolutions)

    # Filter to markets with known resolution
    valid = bias_df[bias_df['resolution'].isin(['UP', 'DOWN'])]

    if len(valid) == 0:
        return {'error': 'No markets with known resolution'}

    # Compute accuracy
    correct = (valid['bias'] == valid['resolution']).sum()
    total = len(valid)
    accuracy = correct / total

    # Breakdown by bias
    up_bias = valid[valid['bias'] == 'UP']
    down_bias = valid[valid['bias'] == 'DOWN']

    up_accuracy = (up_bias['resolution'] == 'UP').sum() / len(up_bias) if len(up_bias) > 0 else 0
    down_accuracy = (down_bias['resolution'] == 'DOWN').sum() / len(down_bias) if len(down_bias) > 0 else 0

    results = {
        'total_markets': total,
        'correct_predictions': correct,
        'accuracy': accuracy,
        'up_bias_count': len(up_bias),
        'up_bias_accuracy': up_accuracy,
        'down_bias_count': len(down_bias),
        'down_bias_accuracy': down_accuracy,
        'expected_accuracy': 0.70,  # Gabagool's known accuracy
        'matches_expected': abs(accuracy - 0.70) < 0.05,  # Within 5%
    }

    logger.info("\nBias Validation Results:")
    logger.info(f"  Overall accuracy: {accuracy*100:.1f}% ({correct}/{total})")
    logger.info(f"  UP-bias accuracy: {up_accuracy*100:.1f}% ({len(up_bias)} markets)")
    logger.info(f"  DOWN-bias accuracy: {down_accuracy*100:.1f}% ({len(down_bias)} markets)")
    logger.info(f"  Expected: ~70%, Matches: {results['matches_expected']}")

    return results


def get_gabagool_bias_labels(
    trades_df: pd.DataFrame,
    matched_df: pd.DataFrame,
    min_trades: int = 10
) -> pd.DataFrame:
    """
    Generate training labels based on Gabagool's market-level bias.

    For each observer row in matched_df, add Gabagool's bias for that market.
    This is for Approach A: learning to predict what Gabagool would do.

    Args:
        trades_df: All Gabagool trades
        matched_df: Cross-referenced trades with observer data
        min_trades: Minimum trades for reliable bias

    Returns:
        DataFrame with gabagool_bias column added
    """
    # Compute market-level biases
    bias_df = compute_all_market_biases(trades_df, min_trades=min_trades)

    # Create bias lookup
    bias_map = dict(zip(bias_df['market_slug'], bias_df['bias']))

    # Add to matched_df
    result = matched_df.copy()
    result['gabagool_bias'] = result['market_slug'].map(bias_map)

    # Encode: UP=1, DOWN=0
    result['gabagool_bias_encoded'] = (result['gabagool_bias'] == 'UP').astype(float)

    logger.info(f"Added gabagool_bias to {len(result)} rows")
    logger.info(f"  UP bias: {(result['gabagool_bias'] == 'UP').sum()}")
    logger.info(f"  DOWN bias: {(result['gabagool_bias'] == 'DOWN').sum()}")

    return result


if __name__ == "__main__":
    from .cross_reference import load_gabagool_trades
    from .resolution_loader import load_all_resolutions

    print("=" * 60)
    print("Testing Gabagool Bias Computation")
    print("=" * 60)

    try:
        # Load trades
        trades_df = load_gabagool_trades()
        print(f"\nLoaded {len(trades_df):,} trades")

        # Compute biases
        bias_df = compute_all_market_biases(trades_df)
        print(f"\nComputed bias for {len(bias_df)} markets")

        # Show distribution
        up_count = (bias_df['bias'] == 'UP').sum()
        down_count = (bias_df['bias'] == 'DOWN').sum()
        print(f"  UP-biased: {up_count} markets ({up_count/len(bias_df)*100:.1f}%)")
        print(f"  DOWN-biased: {down_count} markets ({down_count/len(bias_df)*100:.1f}%)")

        # Average premium
        print(f"\nAverage premium: ${bias_df['premium'].mean():.4f}")
        print(f"  UP VWAP (mean): ${bias_df['up_vwap'].mean():.3f}")
        print(f"  DOWN VWAP (mean): ${bias_df['down_vwap'].mean():.3f}")

        # Validate against resolutions
        resolutions = load_all_resolutions()
        if resolutions:
            validation = validate_bias_against_resolutions(bias_df, resolutions.resolutions)

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
