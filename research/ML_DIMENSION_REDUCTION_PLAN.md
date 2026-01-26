# ML Dimension Reduction Analysis Plan

**Goal:** Identify which parameters actually matter before exhaustive grid search.

---

## Data Sources

1. **Historical Grid Results:**
   - `vol_filter_grid_results_all_combined.csv` (1440 configs)
   - `vol_filter_grid_results_ou.csv`
   - `vol_filter_grid_results_ewma.csv`
   - `vol_filter_grid_results_percentile.csv`

2. **New Test Results (from zone grid scripts):**
   - `velocity_options_results.csv` (when generated)
   - `acceleration_signal_results.csv`
   - `regime_adaptive_results.csv`
   - `multi_signal_results.csv`
   - `kalman_signal_results.csv`

3. **Partial Results:**
   - `results_t2.txt` (multi_signal partial)
   - `kalman_partial_results.txt`

---

## Parameters to Analyze

| Parameter | In Original Grid? | In New Scripts? | Current Setting |
|-----------|-------------------|-----------------|-----------------|
| Threshold method | Yes (searched) | Fixed (OU) | OU adaptive |
| Z-score method | Yes (searched) | Fixed (EWMA) | EWMA volatility |
| Lookback | Yes (searched) | Fixed (72) | 72 ticks (1.2s) |
| Stop-loss | Yes (searched) | Fixed (None) | No stop-loss |
| Time-stop | NOT searched | Fixed (180s) | 180s time stop |
| Cycling | Yes (searched) | Fixed (ON) | Cycling enabled |
| Z-zone bounds | Yes (searched) | Fixed (0<z<1.5) | Low-medium vol |
| Velocity zone | NOT SEARCHED | Yes (5 options) | Grid search |
| Signal method | NOT SEARCHED | Yes (7+ options) | Grid search |
| Kalman params | N/A | Fixed | Default |

---

## ML Analysis Approach

### Phase 1: Feature Importance (Random Forest)

```python
# Load all historical results
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import LabelEncoder

# Load data
df = pd.read_csv('vol_filter_grid_results_all_combined.csv')

# Encode categorical features
for col in ['threshold_method', 'zscore_method', 'cycling', 'stop_loss']:
    if col in df.columns:
        le = LabelEncoder()
        df[col + '_enc'] = le.fit_transform(df[col].astype(str))

# Feature matrix
features = ['lookback', 'threshold_method_enc', 'zscore_method_enc',
            'cycling_enc', 'zscore_lo', 'zscore_hi']
X = df[features].fillna(0)
y = df['direction_accuracy']  # Target: accuracy (not $/hr)

# Train Random Forest
rf = RandomForestRegressor(n_estimators=100, random_state=42)
rf.fit(X, y)

# Feature importance
importance = dict(zip(features, rf.feature_importances_))
sorted_importance = sorted(importance.items(), key=lambda x: -x[1])
```

**Expected Output:**
- Ranked list of parameter importance
- Identify top 3-4 parameters that explain most variance

### Phase 2: Cluster Analysis

```python
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

# Standardize features
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)

# Find clusters
kmeans = KMeans(n_clusters=5, random_state=42)
df['cluster'] = kmeans.fit_predict(X_scaled)

# Analyze cluster performance
cluster_stats = df.groupby('cluster').agg({
    'direction_accuracy': ['mean', 'std'],
    'hourly_rate': ['mean', 'std'],
    'total_trades': 'mean'
})
```

**Expected Output:**
- Groups of configs that perform similarly
- Identify high-performing clusters
- Understand which parameter combinations define each cluster

### Phase 3: Interaction Effects

```python
# Check if parameters interact
from itertools import combinations

for p1, p2 in combinations(features, 2):
    # Group by both parameters
    interaction = df.groupby([p1, p2])['direction_accuracy'].mean()

    # Check if effect of p1 changes based on p2 value
    # High variance indicates interaction
```

**Key Interactions to Check:**
- Lookback + Cycling: Does lookback matter more when cycling=ON?
- Threshold method + Z-score range: Do methods perform differently in different vol zones?
- Velocity zone + Signal method: Do acceleration methods need higher velocity zones?

---

## Hypotheses to Test

1. **Cycling dominates**: Most performance variance comes from cycling ON vs OFF
2. **Z-zone bounds matter more than method**: Where you trade matters more than how you filter
3. **Velocity zone is underutilized**: Adding velocity zone filter will improve all methods
4. **Signal method has diminishing returns**: After top 2-3 methods, others are noise

---

## Expected Deliverables

1. **Parameter Ranking Table:**
   ```
   Rank | Parameter       | Importance | Notes
   1    | cycling         | 0.35       | Most impactful
   2    | zscore_hi       | 0.22       | Upper bound matters
   3    | velocity_zone   | 0.18       | New - highly impactful
   4    | signal_method   | 0.12       | Some methods better
   5    | threshold_method| 0.08       | Minor impact
   6    | lookback        | 0.05       | Can fix to 72
   ```

2. **Simplified Model:**
   - Drop irrelevant parameters (e.g., fix lookback=72, threshold=OU)
   - Focus search space on: cycling, z-zone, velocity zone, signal method

3. **Recommended Focused Grid Search:**
   ```python
   FOCUSED_GRID = {
       'cycling': [True],  # Keep ON
       'zscore_bounds': [(0, 1.0), (0, 1.5), (0.5, 1.5)],
       'velocity_zone': ['ALL', 'Z2_6', 'Z3_6', 'Z4_6'],
       'signal_method': ['BASELINE', 'CONSERVATIVE', 'ACCEL_ALIGNED', 'KALMAN_VEL'],
   }
   # = 1 * 3 * 4 * 4 = 48 configs instead of 1440
   ```

---

## Implementation Steps

1. **Collect Results (PREREQUISITE)**
   - Run zone grid scripts with `--all --grid-zones`
   - Combine CSV outputs into single analysis DataFrame

2. **Run Feature Importance Analysis**
   - Create `research/analyze_parameter_importance.py`
   - Load all CSV results
   - Train Random Forest on accuracy
   - Output importance ranking

3. **Run Cluster Analysis**
   - Add clustering to analysis script
   - Visualize with PCA
   - Identify high-performing clusters

4. **Test Interaction Hypotheses**
   - Add interaction analysis
   - Create interaction heatmaps
   - Document findings

5. **Create Focused Grid Search**
   - Based on findings, define reduced search space
   - Create `research/focused_grid_search.py`
   - Run reduced search across all periods

---

## Timeline

1. **Data Collection**: Run scripts with `--all --grid-zones`
2. **Analysis Script**: Create `analyze_parameter_importance.py`
3. **Run Analysis**: Execute on collected data
4. **Interpret Results**: Document findings
5. **Create Focused Search**: Implement reduced grid search

---

## Success Criteria

- [ ] Identify top 3-4 parameters that explain >70% of accuracy variance
- [ ] Reduce search space from 1440+ configs to <100
- [ ] Find parameter combinations that are robust across periods
- [ ] Document any interaction effects discovered
