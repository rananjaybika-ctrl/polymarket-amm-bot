#!/usr/bin/env python3
"""
SIGNAL-BASED MARKET MAKING ANALYSIS

This script explores mathematical formulas to improve MM fills by using:
1. Velocity signals (1st derivative of price)
2. Acceleration signals (2nd derivative of price)
3. Spike detection (sudden price moves)
4. Cross-market momentum (Binance -> Polymarket)

Key Questions:
- Can we detect windows where both sides fill for <$1 combined?
- Can we use signals to adjust offsets for better fills?
- Can order pulling reduce adverse selection?

Usage:
    python research/signal_based_mm_analysis.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')


# =============================================================================
# CONFIGURATION
# =============================================================================

TARGET_SHARES = 15
MIN_ORDER_QTY = 5
MIN_ORDER_VALUE = 1.0
MIN_TIME = 60
MAX_POSITION_PER_SIDE = 200

# Signal parameters
VELOCITY_THRESHOLD_BPS = 0.10    # Consider velocity significant above this
ACCEL_THRESHOLD = 0.05           # Acceleration threshold
SPIKE_THRESHOLD_PCT = 0.02       # 2% spike detection

# Offset formula parameters
BASE_OFFSET = 0.01
MAX_OFFSET = 0.05
SIGNAL_WEIGHT = 0.02             # How much signals adjust offset


# =============================================================================
# MATHEMATICAL FORMULAS FOR SIGNAL-BASED OFFSETS
# =============================================================================

@dataclass
class SignalState:
    """Current market signal state"""
    velocity_bps: float = 0.0
    acceleration: float = 0.0
    jerk: float = 0.0  # 3rd derivative
    spike_detected: bool = False
    spike_direction: str = ""
    spike_magnitude: float = 0.0
    binance_velocity: float = 0.0  # From HF price data
    emva_20: float = 0.0  # Exponential moving velocity average
    time_remaining: float = 900.0


def formula_1_velocity_adjusted(signal: SignalState, side: str) -> float:
    """
    FORMULA 1: Simple Velocity Adjustment

    Offset = base_offset + velocity_factor * sign(velocity)

    - If velocity is positive (UP trending), widen UP bid, tighten DOWN bid
    - If velocity is negative (DOWN trending), tighten UP bid, widen DOWN bid
    """
    vel = signal.velocity_bps
    vel_factor = min(abs(vel), 0.50) * SIGNAL_WEIGHT  # Cap at 0.50 bps

    if side == "UP":
        # If trending UP, we want to BUY UP cheaper (widen offset = more aggressive)
        # But that means we're more likely to get filled when UP drops
        # Actually: if trending UP, widen offset to avoid buying high
        if vel > VELOCITY_THRESHOLD_BPS:
            return BASE_OFFSET + vel_factor  # Widen when trending up
        elif vel < -VELOCITY_THRESHOLD_BPS:
            return max(0.005, BASE_OFFSET - vel_factor)  # Tighten when trending down
    else:  # DOWN
        if vel > VELOCITY_THRESHOLD_BPS:
            return max(0.005, BASE_OFFSET - vel_factor)  # Tighten when UP trending
        elif vel < -VELOCITY_THRESHOLD_BPS:
            return BASE_OFFSET + vel_factor  # Widen when DOWN trending

    return BASE_OFFSET


def formula_2_acceleration_adjusted(signal: SignalState, side: str) -> float:
    """
    FORMULA 2: Acceleration-Based Adjustment

    Offset = base_offset + accel_factor * acceleration

    Acceleration tells us if momentum is BUILDING or FADING:
    - Positive velocity + positive accel = momentum building (widen both)
    - Positive velocity + negative accel = momentum fading (tighten both)
    """
    vel = signal.velocity_bps
    accel = signal.acceleration

    # Momentum building: accel same sign as velocity
    momentum_building = (vel > 0 and accel > 0) or (vel < 0 and accel < 0)
    momentum_fading = (vel > 0 and accel < 0) or (vel < 0 and accel > 0)

    accel_factor = min(abs(accel), 0.10) * SIGNAL_WEIGHT

    if momentum_building:
        # Momentum building - be more patient (widen offsets)
        return BASE_OFFSET + accel_factor
    elif momentum_fading:
        # Momentum fading - be more aggressive (tighten offsets)
        return max(0.005, BASE_OFFSET - accel_factor)

    return BASE_OFFSET


def formula_3_spike_reactive(signal: SignalState, side: str) -> float:
    """
    FORMULA 3: Spike-Reactive Adjustment

    When spike detected:
    - Pull bid on winner side (avoid buying high)
    - Tighten bid on loser side (capitalize on drop)
    """
    if not signal.spike_detected:
        return BASE_OFFSET

    spike_factor = min(signal.spike_magnitude, 0.05) * 2  # 2x weight for spikes

    if signal.spike_direction == "UP":
        if side == "UP":
            return BASE_OFFSET + spike_factor  # Widen UP (don't buy high)
        else:
            return max(0.005, BASE_OFFSET - spike_factor)  # Tighten DOWN
    elif signal.spike_direction == "DOWN":
        if side == "DOWN":
            return BASE_OFFSET + spike_factor  # Widen DOWN
        else:
            return max(0.005, BASE_OFFSET - spike_factor)  # Tighten UP

    return BASE_OFFSET


def formula_4_binance_lead(signal: SignalState, side: str, binance_change: float) -> float:
    """
    FORMULA 4: Binance Price Lead

    Use Binance price movement as leading indicator.
    Binance moves ~0.6-2.35 seconds before Polymarket.

    binance_change = (current_price - price_2sec_ago) / price_2sec_ago
    """
    if abs(binance_change) < 0.0001:  # 0.01% threshold
        return BASE_OFFSET

    lead_factor = min(abs(binance_change) * 100, 0.03)  # Max 3 cents adjustment

    if binance_change > 0:  # Binance going UP
        if side == "UP":
            return BASE_OFFSET + lead_factor  # Widen UP offset
        else:
            return max(0.005, BASE_OFFSET - lead_factor)  # Tighten DOWN
    else:  # Binance going DOWN
        if side == "DOWN":
            return BASE_OFFSET + lead_factor  # Widen DOWN
        else:
            return max(0.005, BASE_OFFSET - lead_factor)  # Tighten UP

    return BASE_OFFSET


def formula_5_composite(signal: SignalState, side: str, binance_change: float = 0) -> float:
    """
    FORMULA 5: Composite Signal

    Combines velocity, acceleration, spike, and Binance signals with weights:

    total_adjustment =
        0.30 * velocity_factor +
        0.20 * accel_factor +
        0.30 * spike_factor +
        0.20 * binance_factor
    """
    vel = signal.velocity_bps
    accel = signal.acceleration

    # Velocity component (0.30 weight)
    vel_direction = 1 if vel > VELOCITY_THRESHOLD_BPS else (-1 if vel < -VELOCITY_THRESHOLD_BPS else 0)
    vel_magnitude = min(abs(vel), 0.50)
    vel_adjustment = vel_direction * vel_magnitude * 0.02

    # Acceleration component (0.20 weight)
    momentum_aligned = (vel > 0 and accel > 0) or (vel < 0 and accel < 0)
    accel_adjustment = 0.01 if momentum_aligned else -0.01 if not momentum_aligned and abs(accel) > ACCEL_THRESHOLD else 0

    # Spike component (0.30 weight)
    spike_adjustment = 0
    if signal.spike_detected:
        spike_dir = 1 if signal.spike_direction == "UP" else -1
        spike_adjustment = spike_dir * min(signal.spike_magnitude, 0.05) * 0.5

    # Binance component (0.20 weight)
    binance_adjustment = 0
    if abs(binance_change) >= 0.0001:
        binance_dir = 1 if binance_change > 0 else -1
        binance_adjustment = binance_dir * min(abs(binance_change) * 100, 0.02)

    # Combine with weights
    total = (
        0.30 * vel_adjustment +
        0.20 * accel_adjustment +
        0.30 * spike_adjustment +
        0.20 * binance_adjustment
    )

    # Apply side-specific logic
    if side == "UP":
        # Positive total = UP momentum = widen UP offset
        return max(0.005, min(MAX_OFFSET, BASE_OFFSET + total))
    else:
        # Positive total = UP momentum = tighten DOWN offset
        return max(0.005, min(MAX_OFFSET, BASE_OFFSET - total))


def formula_6_time_weighted(signal: SignalState, side: str) -> float:
    """
    FORMULA 6: Time-Weighted Offset

    Offset decreases as time runs out (more aggressive late):

    offset = base_offset * (time_remaining / 900) + min_offset

    At 900s: offset = base_offset + min_offset
    At 60s: offset = base_offset * (60/900) + min_offset = very small
    """
    t = signal.time_remaining
    min_offset = 0.005

    time_factor = max(0.1, t / 900)  # Floor at 0.1 to not go too aggressive

    return min_offset + (BASE_OFFSET - min_offset) * time_factor


def formula_7_order_pull_threshold(signal: SignalState, side: str) -> Optional[float]:
    """
    FORMULA 7: Order Pull Decision

    Returns None if we should PULL the order (not post).
    Returns offset if we should POST.

    Pull conditions:
    1. Spike in same direction as our bid (would buy high)
    2. Strong velocity against our position (adverse selection)
    3. Acceleration building against us
    """
    vel = signal.velocity_bps
    accel = signal.acceleration

    if side == "UP":
        # Pull UP bid if strong UP movement (would buy at top)
        if signal.spike_detected and signal.spike_direction == "UP":
            return None  # PULL
        if vel > 0.30 and accel > 0:  # Strong UP momentum building
            return None  # PULL
    else:  # DOWN
        if signal.spike_detected and signal.spike_direction == "DOWN":
            return None  # PULL
        if vel < -0.30 and accel < 0:  # Strong DOWN momentum building
            return None  # PULL

    return BASE_OFFSET  # POST with base offset


# =============================================================================
# DATA LOADING AND SIGNAL CALCULATION
# =============================================================================

def load_observer_data() -> pd.DataFrame:
    """Load and merge observer data files"""
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')

    dfs = []
    for pattern in ['grid_obs_*.csv', 'spread_capture_obs_*.csv']:
        for filepath in sorted(observer_dir.glob(pattern)):
            if 'old' in str(filepath) or 'fixed' in str(filepath):
                continue
            try:
                df = pd.read_csv(filepath, on_bad_lines='skip', low_memory=False)
                if not df.empty:
                    dfs.append(df)
            except:
                continue

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.sort_values('timestamp_ms').drop_duplicates()
    return combined


def load_binance_hf_data() -> pd.DataFrame:
    """Load high-frequency Binance price data (60Hz)"""
    binance_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/binance_hf')

    dfs = []
    for filepath in sorted(binance_dir.glob('*.csv')):
        try:
            df = pd.read_csv(filepath)
            if not df.empty:
                dfs.append(df)
        except:
            continue

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    combined = combined.sort_values('timestamp_ms').drop_duplicates()
    return combined


def calculate_advanced_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate acceleration, jerk, and EMVA from observer data"""

    if df.empty:
        return df

    df = df.copy()

    # Sort by market and time
    df = df.sort_values(['market_slug', 'timestamp_ms'])

    # Calculate acceleration (change in velocity)
    df['prev_velocity'] = df.groupby('market_slug')['velocity_bps'].shift(1)
    df['acceleration'] = df['velocity_bps'] - df['prev_velocity'].fillna(df['velocity_bps'])

    # Calculate jerk (change in acceleration)
    df['prev_accel'] = df.groupby('market_slug')['acceleration'].shift(1)
    df['jerk'] = df['acceleration'] - df['prev_accel'].fillna(df['acceleration'])

    # Calculate EMVA (Exponential Moving Velocity Average)
    # Using span of 20 observations (~4 seconds at 5Hz)
    df['emva_20'] = df.groupby('market_slug')['velocity_bps'].transform(
        lambda x: x.ewm(span=20, adjust=False).mean()
    )

    # Calculate velocity momentum (velocity vs EMVA)
    df['velocity_momentum'] = df['velocity_bps'] - df['emva_20']

    return df


def merge_binance_signals(observer_df: pd.DataFrame, binance_df: pd.DataFrame) -> pd.DataFrame:
    """Merge Binance HF data with observer data for cross-market signals"""

    if observer_df.empty or binance_df.empty:
        return observer_df

    observer_df = observer_df.copy()
    binance_df = binance_df.copy()

    # Calculate Binance velocity (2-second change)
    # At 60Hz, 120 ticks = 2 seconds
    binance_df['price_2s_ago'] = binance_df['price'].shift(120)
    binance_df['binance_change_2s'] = (
        (binance_df['price'] - binance_df['price_2s_ago']) /
        binance_df['price_2s_ago'].replace(0, np.nan)
    )

    # Also calculate 500ms change for faster signals
    binance_df['price_500ms_ago'] = binance_df['price'].shift(30)
    binance_df['binance_change_500ms'] = (
        (binance_df['price'] - binance_df['price_500ms_ago']) /
        binance_df['price_500ms_ago'].replace(0, np.nan)
    )

    # Merge using timestamp (asof merge - closest Binance time before observer time)
    observer_df = observer_df.sort_values('timestamp_ms')
    binance_df = binance_df.sort_values('timestamp_ms')

    observer_df = pd.merge_asof(
        observer_df,
        binance_df[['timestamp_ms', 'price', 'binance_change_2s', 'binance_change_500ms']].rename(
            columns={'price': 'binance_hf_price'}
        ),
        on='timestamp_ms',
        direction='backward',
        tolerance=5000  # 5 second tolerance
    )

    return observer_df


# =============================================================================
# FILL OPPORTUNITY DETECTION
# =============================================================================

@dataclass
class FillOpportunity:
    """Represents an opportunity to fill both sides for <$1"""
    timestamp_ms: int
    market_slug: str
    time_remaining: float
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float
    pair_cost: float
    signal: SignalState
    formula_up_offset: Dict[str, float]
    formula_down_offset: Dict[str, float]
    would_fill_up: Dict[str, bool]
    would_fill_down: Dict[str, bool]
    combined_cost: Dict[str, float]


def detect_fill_opportunities(df: pd.DataFrame) -> List[FillOpportunity]:
    """
    Detect windows where both sides could be filled for <$1.

    For each timestamp, test each formula to see:
    1. What offset it would use
    2. Whether that bid would have been filled (ask crosses through)
    3. What the combined cost would be
    """

    opportunities = []

    df = df.sort_values(['market_slug', 'timestamp_ms']).reset_index(drop=True)

    for market_slug in df['market_slug'].unique():
        mdf = df[df['market_slug'] == market_slug].copy()

        if len(mdf) < 10:
            continue

        # Track posted bids and look for fills
        prev_up_ask = None
        prev_down_ask = None

        for i in range(1, len(mdf)):
            row = mdf.iloc[i]
            prev = mdf.iloc[i-1]

            time_rem = row.get('time_remaining_secs', 900)
            if time_rem < MIN_TIME:
                continue

            # Build signal state
            signal = SignalState(
                velocity_bps=row.get('velocity_bps', 0),
                acceleration=row.get('acceleration', 0),
                jerk=row.get('jerk', 0),
                spike_detected=row.get('spike_detected', False),
                spike_direction=row.get('spike_direction', ''),
                spike_magnitude=row.get('spike_magnitude', 0),
                binance_velocity=row.get('binance_change_500ms', 0) or 0,
                emva_20=row.get('emva_20', 0),
                time_remaining=time_rem
            )

            binance_change = row.get('binance_change_2s', 0) or 0

            # Calculate offsets for each formula
            formulas = {
                'baseline': (BASE_OFFSET, BASE_OFFSET),
                'f1_velocity': (
                    formula_1_velocity_adjusted(signal, "UP"),
                    formula_1_velocity_adjusted(signal, "DOWN")
                ),
                'f2_acceleration': (
                    formula_2_acceleration_adjusted(signal, "UP"),
                    formula_2_acceleration_adjusted(signal, "DOWN")
                ),
                'f3_spike': (
                    formula_3_spike_reactive(signal, "UP"),
                    formula_3_spike_reactive(signal, "DOWN")
                ),
                'f4_binance': (
                    formula_4_binance_lead(signal, "UP", binance_change),
                    formula_4_binance_lead(signal, "DOWN", binance_change)
                ),
                'f5_composite': (
                    formula_5_composite(signal, "UP", binance_change),
                    formula_5_composite(signal, "DOWN", binance_change)
                ),
                'f6_time': (
                    formula_6_time_weighted(signal, "UP"),
                    formula_6_time_weighted(signal, "DOWN")
                ),
            }

            # Check for order pull
            f7_up = formula_7_order_pull_threshold(signal, "UP")
            f7_down = formula_7_order_pull_threshold(signal, "DOWN")
            if f7_up is not None and f7_down is not None:
                formulas['f7_pull'] = (f7_up, f7_down)

            up_bid_price = row['up_bid']
            up_ask = row['up_ask']
            down_bid_price = row['down_bid']
            down_ask = row['down_ask']

            prev_up_ask_val = prev['up_ask']
            prev_down_ask_val = prev['down_ask']

            # For each formula, check if we would have been filled
            formula_up_offsets = {}
            formula_down_offsets = {}
            would_fill_up = {}
            would_fill_down = {}
            combined_costs = {}

            for name, (up_offset, down_offset) in formulas.items():
                formula_up_offsets[name] = up_offset
                formula_down_offsets[name] = down_offset

                # Posted bid prices
                posted_up_bid = max(0.01, min(0.95, up_bid_price - up_offset))
                posted_down_bid = max(0.01, min(0.95, down_bid_price - down_offset))

                # Check if ask crossed through our bid (realistic fill)
                up_filled = (
                    prev_up_ask_val > posted_up_bid and
                    up_ask <= posted_up_bid
                )
                down_filled = (
                    prev_down_ask_val > posted_down_bid and
                    down_ask <= posted_down_bid
                )

                would_fill_up[name] = up_filled
                would_fill_down[name] = down_filled

                # Combined cost if both filled
                combined_costs[name] = posted_up_bid + posted_down_bid

            # Record opportunity if any formula would fill both for <$1
            any_good_fill = any(
                would_fill_up.get(name, False) and
                would_fill_down.get(name, False) and
                combined_costs.get(name, 2.0) < 1.0
                for name in formulas
            )

            # Also record if baseline doesn't fill but advanced formulas do
            advanced_better = (
                not (would_fill_up.get('baseline', False) and would_fill_down.get('baseline', False)) and
                any(
                    would_fill_up.get(name, False) and would_fill_down.get(name, False)
                    for name in formulas if name != 'baseline'
                )
            )

            if any_good_fill or advanced_better:
                opp = FillOpportunity(
                    timestamp_ms=row['timestamp_ms'],
                    market_slug=market_slug,
                    time_remaining=time_rem,
                    up_bid=up_bid_price,
                    up_ask=up_ask,
                    down_bid=down_bid_price,
                    down_ask=down_ask,
                    pair_cost=row.get('pair_cost', up_ask + down_ask),
                    signal=signal,
                    formula_up_offset=formula_up_offsets,
                    formula_down_offset=formula_down_offsets,
                    would_fill_up=would_fill_up,
                    would_fill_down=would_fill_down,
                    combined_cost=combined_costs
                )
                opportunities.append(opp)

            prev_up_ask = up_ask
            prev_down_ask = down_ask

    return opportunities


# =============================================================================
# SIMULATION: SIGNAL-BASED MM
# =============================================================================

def simulate_signal_based_mm(
    df: pd.DataFrame,
    formula_name: str,
    resolution_cache: Dict[str, str]
) -> Dict:
    """
    Simulate MM strategy using a specific offset formula.

    Returns performance metrics for comparison.
    """

    results = []

    for market_slug in df['market_slug'].unique():
        mdf = df[df['market_slug'] == market_slug].copy()
        mdf = mdf.sort_values('time_remaining_secs', ascending=False).reset_index(drop=True)

        if len(mdf) < 25:
            continue

        resolution = resolution_cache.get(market_slug)
        if not resolution:
            final = mdf.iloc[-1]
            resolution = 'UP' if final['up_bid'] > final['down_bid'] else 'DOWN'

        up_shares = 0
        down_shares = 0
        up_cost = 0.0
        down_cost = 0.0
        fills = 0
        fills_up = 0
        fills_down = 0
        orders_pulled = 0

        prev_up_ask = None
        prev_down_ask = None

        for i in range(len(mdf)):
            row = mdf.iloc[i]
            time_rem = row.get('time_remaining_secs', 900)

            if time_rem < MIN_TIME:
                continue

            # Build signal
            signal = SignalState(
                velocity_bps=row.get('velocity_bps', 0),
                acceleration=row.get('acceleration', 0),
                jerk=row.get('jerk', 0),
                spike_detected=row.get('spike_detected', False),
                spike_direction=row.get('spike_direction', ''),
                spike_magnitude=row.get('spike_magnitude', 0),
                binance_velocity=row.get('binance_change_500ms', 0) or 0,
                emva_20=row.get('emva_20', 0),
                time_remaining=time_rem
            )

            binance_change = row.get('binance_change_2s', 0) or 0

            # Get offsets based on formula
            if formula_name == 'baseline':
                up_offset = BASE_OFFSET
                down_offset = BASE_OFFSET
            elif formula_name == 'f1_velocity':
                up_offset = formula_1_velocity_adjusted(signal, "UP")
                down_offset = formula_1_velocity_adjusted(signal, "DOWN")
            elif formula_name == 'f2_acceleration':
                up_offset = formula_2_acceleration_adjusted(signal, "UP")
                down_offset = formula_2_acceleration_adjusted(signal, "DOWN")
            elif formula_name == 'f3_spike':
                up_offset = formula_3_spike_reactive(signal, "UP")
                down_offset = formula_3_spike_reactive(signal, "DOWN")
            elif formula_name == 'f4_binance':
                up_offset = formula_4_binance_lead(signal, "UP", binance_change)
                down_offset = formula_4_binance_lead(signal, "DOWN", binance_change)
            elif formula_name == 'f5_composite':
                up_offset = formula_5_composite(signal, "UP", binance_change)
                down_offset = formula_5_composite(signal, "DOWN", binance_change)
            elif formula_name == 'f6_time':
                up_offset = formula_6_time_weighted(signal, "UP")
                down_offset = formula_6_time_weighted(signal, "DOWN")
            elif formula_name == 'f7_pull':
                up_offset_result = formula_7_order_pull_threshold(signal, "UP")
                down_offset_result = formula_7_order_pull_threshold(signal, "DOWN")
                if up_offset_result is None:
                    orders_pulled += 1
                    up_offset = None
                else:
                    up_offset = up_offset_result
                if down_offset_result is None:
                    orders_pulled += 1
                    down_offset = None
                else:
                    down_offset = down_offset_result
            else:
                up_offset = BASE_OFFSET
                down_offset = BASE_OFFSET

            up_bid = row['up_bid']
            up_ask = row['up_ask']
            down_bid = row['down_bid']
            down_ask = row['down_ask']

            # Post bids (if not pulled)
            posted_up_bid = 0
            posted_down_bid = 0

            if up_offset is not None and up_shares < MAX_POSITION_PER_SIDE:
                posted_up_bid = max(0.01, min(0.95, up_bid - up_offset))
            if down_offset is not None and down_shares < MAX_POSITION_PER_SIDE:
                posted_down_bid = max(0.01, min(0.95, down_bid - down_offset))

            # Check fills (realistic model)
            if prev_up_ask is not None and posted_up_bid > 0:
                ask_crossed = prev_up_ask > posted_up_bid and up_ask <= posted_up_bid
                ask_dropped = (prev_up_ask - up_ask >= 0.01 and up_ask <= posted_up_bid + 0.005)

                if ask_crossed or ask_dropped:
                    if up_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                        up_cost += posted_up_bid * TARGET_SHARES
                        up_shares += TARGET_SHARES
                        fills += 1
                        fills_up += 1

            if prev_down_ask is not None and posted_down_bid > 0:
                ask_crossed = prev_down_ask > posted_down_bid and down_ask <= posted_down_bid
                ask_dropped = (prev_down_ask - down_ask >= 0.01 and down_ask <= posted_down_bid + 0.005)

                if ask_crossed or ask_dropped:
                    if down_shares + TARGET_SHARES <= MAX_POSITION_PER_SIDE:
                        down_cost += posted_down_bid * TARGET_SHARES
                        down_shares += TARGET_SHARES
                        fills += 1
                        fills_down += 1

            prev_up_ask = up_ask
            prev_down_ask = down_ask

        if fills == 0:
            continue

        # Calculate PnL
        pairs = min(up_shares, down_shares)
        unmatched_up = up_shares - pairs
        unmatched_down = down_shares - pairs

        total_cost = up_cost + down_cost
        pair_payout = pairs * 1.0
        unmatched_up_payout = unmatched_up * (1.0 if resolution == "UP" else 0.0)
        unmatched_down_payout = unmatched_down * (1.0 if resolution == "DOWN" else 0.0)
        total_payout = pair_payout + unmatched_up_payout + unmatched_down_payout

        total_pnl = total_payout - total_cost

        up_avg = up_cost / up_shares if up_shares > 0 else 0
        down_avg = down_cost / down_shares if down_shares > 0 else 0
        pair_cost = up_avg + down_avg if pairs > 0 else 0

        results.append({
            'slug': market_slug,
            'resolution': resolution,
            'pairs': pairs,
            'pair_cost': pair_cost,
            'fills': fills,
            'fills_up': fills_up,
            'fills_down': fills_down,
            'orders_pulled': orders_pulled,
            'total_pnl': total_pnl,
            'up_shares': up_shares,
            'down_shares': down_shares,
        })

    return {
        'formula': formula_name,
        'markets': len(results),
        'total_pairs': sum(r['pairs'] for r in results),
        'total_fills': sum(r['fills'] for r in results),
        'total_pnl': sum(r['total_pnl'] for r in results),
        'avg_pair_cost': np.mean([r['pair_cost'] for r in results if r['pairs'] > 0]) if results else 0,
        'orders_pulled': sum(r['orders_pulled'] for r in results),
        'results': results,
    }


# =============================================================================
# RESOLUTION LOADING
# =============================================================================

def load_resolution_cache() -> Dict[str, str]:
    """Load verified market resolutions"""
    cache = {}
    resolution_file = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer/market_resolutions.csv')

    if resolution_file.exists():
        df = pd.read_csv(resolution_file)
        for _, row in df.iterrows():
            if row['winner'] in ('UP', 'DOWN'):
                cache[row['market']] = row['winner']

    return cache


# =============================================================================
# MAIN ANALYSIS
# =============================================================================

def main():
    print("=" * 80)
    print("SIGNAL-BASED MARKET MAKING ANALYSIS")
    print("=" * 80)

    # Load data
    print("\n1. Loading observer data...")
    observer_df = load_observer_data()
    print(f"   Loaded {len(observer_df):,} observer rows")

    print("\n2. Loading Binance HF data...")
    binance_df = load_binance_hf_data()
    print(f"   Loaded {len(binance_df):,} Binance rows")

    print("\n3. Loading resolution cache...")
    resolution_cache = load_resolution_cache()
    print(f"   Loaded {len(resolution_cache)} resolutions")

    print("\n4. Calculating advanced signals (acceleration, jerk, EMVA)...")
    observer_df = calculate_advanced_signals(observer_df)

    print("\n5. Merging Binance cross-market signals...")
    observer_df = merge_binance_signals(observer_df, binance_df)

    # Show signal statistics
    print("\n" + "=" * 80)
    print("SIGNAL STATISTICS")
    print("=" * 80)

    if 'acceleration' in observer_df.columns:
        print(f"""
    Velocity (bps):
        Mean: {observer_df['velocity_bps'].mean():.4f}
        Std:  {observer_df['velocity_bps'].std():.4f}
        Min:  {observer_df['velocity_bps'].min():.4f}
        Max:  {observer_df['velocity_bps'].max():.4f}

    Acceleration:
        Mean: {observer_df['acceleration'].mean():.4f}
        Std:  {observer_df['acceleration'].std():.4f}

    Jerk:
        Mean: {observer_df['jerk'].mean():.4f}
        Std:  {observer_df['jerk'].std():.4f}
        """)

    if 'binance_change_2s' in observer_df.columns:
        bc = observer_df['binance_change_2s'].dropna()
        if len(bc) > 0:
            print(f"""
    Binance 2-second change:
        Mean: {bc.mean()*100:.4f}%
        Std:  {bc.std()*100:.4f}%
        Max:  {bc.abs().max()*100:.4f}%
            """)

    # Detect fill opportunities
    print("\n" + "=" * 80)
    print("FILL OPPORTUNITY DETECTION")
    print("=" * 80)

    print("\n6. Detecting <$1 paired fill opportunities...")
    opportunities = detect_fill_opportunities(observer_df)
    print(f"   Found {len(opportunities)} potential opportunities")

    if opportunities:
        # Analyze by formula
        formula_stats = defaultdict(lambda: {'count': 0, 'avg_cost': [], 'exclusive': 0})

        for opp in opportunities:
            for name in opp.combined_cost:
                if opp.would_fill_up.get(name) and opp.would_fill_down.get(name):
                    formula_stats[name]['count'] += 1
                    formula_stats[name]['avg_cost'].append(opp.combined_cost[name])

                    # Check if exclusive (baseline wouldn't fill)
                    if name != 'baseline' and not (
                        opp.would_fill_up.get('baseline') and opp.would_fill_down.get('baseline')
                    ):
                        formula_stats[name]['exclusive'] += 1

        print(f"""
    FORMULA COMPARISON (opportunities where both sides fill):
    ---------------------------------------------------------
    {'Formula':<20} {'Count':<10} {'Avg Cost':<12} {'Exclusive':<10}
    {'-'*52}""")

        for name in sorted(formula_stats.keys()):
            stats = formula_stats[name]
            avg = np.mean(stats['avg_cost']) if stats['avg_cost'] else 0
            print(f"    {name:<20} {stats['count']:<10} ${avg:.4f}      {stats['exclusive']:<10}")

    # Run simulations for each formula
    print("\n" + "=" * 80)
    print("FORMULA BACKTEST COMPARISON")
    print("=" * 80)

    formulas_to_test = [
        'baseline', 'f1_velocity', 'f2_acceleration', 'f3_spike',
        'f4_binance', 'f5_composite', 'f6_time', 'f7_pull'
    ]

    total_markets = observer_df['market_slug'].nunique()
    total_hours = total_markets * 15 / 60  # 15 min per market

    print(f"\n   Total markets: {total_markets}")
    print(f"   Total hours: {total_hours:.1f}")

    print(f"\n   Running backtests for each formula...")

    results_by_formula = {}
    for formula in formulas_to_test:
        result = simulate_signal_based_mm(observer_df, formula, resolution_cache)
        results_by_formula[formula] = result

    print(f"""
    BACKTEST RESULTS:
    -----------------
    {'Formula':<20} {'Fills':<10} {'Pairs':<10} {'Avg Cost':<12} {'PnL':<12} {'$/hr':<10}
    {'-'*74}""")

    for formula in formulas_to_test:
        r = results_by_formula[formula]
        hourly = r['total_pnl'] / total_hours if total_hours > 0 else 0
        pulled = f" (pulled: {r['orders_pulled']})" if r['orders_pulled'] > 0 else ""
        print(f"    {formula:<20} {r['total_fills']:<10} {r['total_pairs']:<10} ${r['avg_pair_cost']:.4f}      ${r['total_pnl']:>8.2f}    ${hourly:>6.2f}{pulled}")

    # Find best formula
    best = max(results_by_formula.items(), key=lambda x: x[1]['total_pnl'])
    print(f"\n   BEST FORMULA: {best[0]} (${best[1]['total_pnl']:.2f} total, ${best[1]['total_pnl']/total_hours:.2f}/hr)")

    # Analyze specific opportunities
    print("\n" + "=" * 80)
    print("EXAMPLE OPPORTUNITIES")
    print("=" * 80)

    if opportunities:
        # Show 5 best opportunities
        good_opps = [
            opp for opp in opportunities
            if any(opp.would_fill_up.get(n) and opp.would_fill_down.get(n) and opp.combined_cost.get(n, 2) < 0.98
                   for n in opp.combined_cost)
        ]

        print(f"\n   Top {min(5, len(good_opps))} opportunities with <$0.98 combined cost:\n")

        for opp in sorted(good_opps, key=lambda x: min(x.combined_cost.values()))[:5]:
            best_formula = min(opp.combined_cost, key=lambda k: opp.combined_cost[k] if opp.would_fill_up.get(k) and opp.would_fill_down.get(k) else 99)

            print(f"""   Market: {opp.market_slug}
      Time remaining: {opp.time_remaining:.0f}s
      Pair cost (ask): ${opp.pair_cost:.4f}
      Best formula: {best_formula} -> ${opp.combined_cost.get(best_formula, 0):.4f}
      Signal: vel={opp.signal.velocity_bps:.3f}, accel={opp.signal.acceleration:.3f}
      Spike: {opp.signal.spike_detected} {opp.signal.spike_direction}
      """)

    # Summary and recommendations
    print("\n" + "=" * 80)
    print("SUMMARY AND RECOMMENDATIONS")
    print("=" * 80)

    baseline_pnl = results_by_formula['baseline']['total_pnl']

    improvements = []
    for formula, result in results_by_formula.items():
        if formula != 'baseline':
            diff = result['total_pnl'] - baseline_pnl
            pct = (diff / abs(baseline_pnl) * 100) if baseline_pnl != 0 else 0
            improvements.append((formula, diff, pct, result))

    improvements.sort(key=lambda x: x[1], reverse=True)

    print(f"""
    IMPROVEMENT OVER BASELINE:
    --------------------------
    Baseline PnL: ${baseline_pnl:.2f}
    """)

    for formula, diff, pct, result in improvements:
        sign = "+" if diff > 0 else ""
        print(f"    {formula:<20}: {sign}${diff:.2f} ({sign}{pct:.1f}%)")

    print(f"""
    KEY FINDINGS:
    -------------
    1. Realistic fill model shows very few fills regardless of formula
       (This is expected - fills only happen when ask crosses through bid)

    2. Signal-based adjustments can:
       - Improve fill prices by widening/tightening at right times
       - Reduce adverse selection via order pulling
       - But cannot create fills that wouldn't happen

    3. To get more fills, we need:
       - Better fill detection (maybe optimistic model is closer to reality)
       - Tighter offsets (more aggressive pricing)
       - Or switch to TAKER strategy (sweep asks)

    RECOMMENDATIONS:
    ----------------
    1. Test with optimistic fill model to see signal impact
    2. If signals show improvement, implement f5_composite or best performer
    3. For live trading: start with low capital to measure actual fill rates
    4. Order pulling (f7) can prevent some adverse selection
    """)


if __name__ == "__main__":
    main()
