"""
Market Resolution Loader

Aggregates market resolution data (which side won: UP or DOWN) from multiple sources.
This is the ground truth for winner prediction models.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Optional, List
from dataclasses import dataclass
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
OBSERVER_DIR = PROJECT_ROOT / "research" / "observer"
FINDINGS_DIR = PROJECT_ROOT / "research" / "findings"
FINDINGS_DATA_DIR = FINDINGS_DIR / "data"


@dataclass
class ResolutionData:
    """Container for market resolution data."""
    resolutions: Dict[str, str]  # market_slug -> 'UP' | 'DOWN'
    metadata: Dict[str, Dict]  # market_slug -> additional info
    source_files: List[str]

    def __len__(self):
        return len(self.resolutions)

    def get(self, market_slug: str) -> Optional[str]:
        return self.resolutions.get(market_slug)


def load_resolution_csv(filepath: Path) -> Dict[str, str]:
    """
    Load resolutions from a single CSV file.

    Handles various column naming conventions.
    """
    if not filepath.exists():
        return {}

    df = pd.read_csv(filepath)
    resolutions = {}

    # Handle different column names
    slug_col = None
    winner_col = None

    for col in df.columns:
        col_lower = col.lower()
        if col_lower in ['market_slug', 'slug']:
            slug_col = col
        elif col_lower in ['winner', 'resolution', 'outcome']:
            winner_col = col

    if slug_col is None or winner_col is None:
        logger.warning(f"  Could not find slug/winner columns in {filepath.name}")
        return {}

    for _, row in df.iterrows():
        slug = row[slug_col]
        winner = str(row[winner_col]).upper().strip()

        if winner in ['UP', 'DOWN']:
            resolutions[slug] = winner

    return resolutions


def load_all_resolutions() -> ResolutionData:
    """
    Load market resolutions from all available sources.

    Sources (in priority order):
    1. market_resolutions_verified.csv - manually verified
    2. market_resolutions.csv - auto-detected
    3. oos*_resolutions.csv - period-specific files

    Returns:
        ResolutionData with aggregated resolutions
    """
    resolutions = {}
    metadata = {}
    source_files = []

    # Define resolution file paths in priority order
    resolution_files = [
        OBSERVER_DIR / "market_resolutions_verified.csv",
        OBSERVER_DIR / "market_resolutions.csv",
        FINDINGS_DIR / "oos6_resolutions.csv",
        FINDINGS_DIR / "oos6_resolutions_jan28.csv",
        FINDINGS_DIR / "oos6_resolutions_jan29.csv",
        FINDINGS_DATA_DIR / "oos6_resolutions.csv",
    ]

    for filepath in resolution_files:
        if filepath.exists():
            logger.info(f"Loading resolutions from {filepath.name}")
            file_resolutions = load_resolution_csv(filepath)

            if file_resolutions:
                # Update (later files don't overwrite earlier verified ones)
                for slug, winner in file_resolutions.items():
                    if slug not in resolutions:
                        resolutions[slug] = winner
                        metadata[slug] = {'source': filepath.name}

                source_files.append(filepath.name)
                logger.info(f"  Added {len(file_resolutions)} resolutions")

    # Also try to infer from observer data (final prices)
    observer_inferred = infer_resolutions_from_observer()
    for slug, winner in observer_inferred.items():
        if slug not in resolutions:
            resolutions[slug] = winner
            metadata[slug] = {'source': 'observer_inferred'}

    if observer_inferred:
        logger.info(f"  Inferred {len(observer_inferred)} additional resolutions from observer data")

    logger.info(f"Total resolutions loaded: {len(resolutions)}")

    return ResolutionData(
        resolutions=resolutions,
        metadata=metadata,
        source_files=source_files,
    )


def infer_resolutions_from_observer(
    observer_dir: Path = None
) -> Dict[str, str]:
    """
    Infer market resolutions from final observer prices.

    Logic: At market close, the winning side trades near $1.00, loser near $0.00.
    If final_up_bid > 0.90, UP won. If final_down_bid > 0.90, DOWN won.

    Returns:
        Dict of market_slug -> 'UP' | 'DOWN'
    """
    if observer_dir is None:
        observer_dir = OBSERVER_DIR

    inferred = {}

    # Find observer files
    observer_files = list(observer_dir.glob("grid_obs_*.csv"))

    for filepath in observer_files:
        try:
            # Read with minimal columns for efficiency
            df = pd.read_csv(
                filepath,
                usecols=['market_slug', 'time_remaining_secs', 'up_bid', 'down_bid'],
                on_bad_lines='skip',
                low_memory=False
            )

            # Get final rows per market (lowest time_remaining_secs)
            for slug, mdf in df.groupby('market_slug'):
                if slug in inferred:
                    continue

                final_row = mdf.loc[mdf['time_remaining_secs'].idxmin()]

                # Only infer if very close to end
                if final_row['time_remaining_secs'] > 60:
                    continue

                up_bid = final_row['up_bid']
                down_bid = final_row['down_bid']

                # Infer winner from final prices
                if pd.notna(up_bid) and up_bid > 0.90:
                    inferred[slug] = 'UP'
                elif pd.notna(down_bid) and down_bid > 0.90:
                    inferred[slug] = 'DOWN'

        except Exception as e:
            logger.debug(f"Could not process {filepath.name}: {e}")

    return inferred


def get_resolution_statistics(resolution_data: ResolutionData) -> Dict:
    """
    Get statistics about market resolutions.

    Returns:
        Dict with counts and ratios
    """
    resolutions = resolution_data.resolutions

    up_count = sum(1 for v in resolutions.values() if v == 'UP')
    down_count = sum(1 for v in resolutions.values() if v == 'DOWN')
    total = len(resolutions)

    return {
        'total_markets': total,
        'up_wins': up_count,
        'down_wins': down_count,
        'up_ratio': up_count / total if total > 0 else 0,
        'down_ratio': down_count / total if total > 0 else 0,
        'balanced': abs(up_count - down_count) / total < 0.1 if total > 0 else False,
    }


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Resolution Loader")
    print("=" * 60)

    resolution_data = load_all_resolutions()

    print(f"\nTotal resolutions: {len(resolution_data)}")
    print(f"Sources: {resolution_data.source_files}")

    stats = get_resolution_statistics(resolution_data)
    print(f"\nStatistics:")
    print(f"  UP wins: {stats['up_wins']} ({stats['up_ratio']*100:.1f}%)")
    print(f"  DOWN wins: {stats['down_wins']} ({stats['down_ratio']*100:.1f}%)")
    print(f"  Balanced: {stats['balanced']}")

    # Show sample
    print("\nSample resolutions:")
    for i, (slug, winner) in enumerate(resolution_data.resolutions.items()):
        if i >= 5:
            break
        print(f"  {slug}: {winner}")
