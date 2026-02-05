# Volatility Gate Backtest Implementation Plan

## Objective
Add volatility gating to aggressive_grid_search.py to validate that skipping LOW volatility periods improves win rate.

## Implementation Steps

### Step 1: Calculate Rolling Volatility in Spike Detection

In the `detect_spikes_*` functions (around line 250-350), add rolling volatility calculation:

```python
def calculate_rolling_volatility(prices: np.ndarray, window: int = 72) -> np.ndarray:
    """
    Calculate rolling volatility (std of returns) over window.

    Args:
        prices: Array of BTC prices
        window: Lookback window (72 ticks = 1200ms at 60Hz)

    Returns:
        Array of rolling volatility values
    """
    returns = np.diff(prices) / prices[:-1] * 100  # % returns

    # Pad with NaN for first window
    vol = np.full(len(prices), np.nan)

    for i in range(window, len(prices)):
        vol[i] = np.std(returns[i-window:i])

    return vol
```

### Step 2: Add Volatility Column to btc_spikes DataFrame

In the spike detection function, add:
```python
btc_df['rolling_vol'] = calculate_rolling_volatility(btc_df['price'].values, window=72)
```

### Step 3: Add Volatility Gate Parameter to TestConfig

```python
@dataclass
class TestConfig:
    # ... existing fields ...
    min_volatility: Optional[float] = None  # None = disabled, e.g., 0.005 = require vol >= 0.005%
```

### Step 4: Add Volatility Gate Check in simulate_market_single

Around line 986 (after enhanced score check, before entry):

```python
# VOLATILITY GATE (Feb 4, 2026) - Skip low volatility periods
if config.min_volatility is not None:
    spike_vol = spike_row.get('rolling_vol', None)
    if spike_vol is not None and spike_vol < config.min_volatility:
        spike_idx += 1
        continue
```

### Step 5: Add Grid Dimension for Volatility Threshold

```python
# Volatility thresholds to test
MIN_VOLATILITIES = [None, 0.003, 0.005, 0.008, 0.010]
```

## Expected Results

Based on Feb 4 analysis:
- LOW volatility (vol < 0.005%): ~33% win rate
- MEDIUM volatility (0.005-0.01%): ~45% win rate
- HIGH volatility (> 0.01%): ~55-65% win rate

The volatility gate should:
1. Filter out ~30-40% of signals (low vol periods)
2. Increase overall win rate from ~50% to ~55-60%
3. Reduce total trades but improve profit factor

## Files to Modify

1. `research/optimizers/aggressive_grid_search.py`:
   - Add `calculate_rolling_volatility()` function
   - Add `rolling_vol` column in spike detection
   - Add `min_volatility` to TestConfig
   - Add volatility gate check in simulate_market_single
   - Add MIN_VOLATILITIES to grid parameters

2. `src/strategies/enhanced_spike.py`:
   - Add volatility gate check using ou_adaptive_threshold.current_regime

## Validation

After implementation:
1. Run grid search with MIN_VOLATILITIES = [None, 0.005, 0.008, 0.010]
2. Compare win rates across volatility thresholds
3. Find optimal min_volatility that maximizes $/hr
4. Update TRADING_CONFIGS.py with winner value
