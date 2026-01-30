# Short-Term Signal Discovery Results

Generated: 2026-01-29 07:48:19


## Summary

- Total events analyzed: 255
- Condition combinations tested: 3,682
- Combinations with >= 70% accuracy: 113

## Top Conditions at 5s Horizon

| Condition | n | Accuracy | Mean PnL | p-value |
|-----------|---|----------|----------|---------|
| vel_aligned+time_gt_300 | 169 | 81.1% | $4.60 | 0.0000 |
| vel_aligned+spike_mag_low+time_gt_300 | 169 | 81.1% | $4.60 | 0.0000 |
| vel_aligned+zone_not_neutral+time_gt_300 | 164 | 80.5% | $4.63 | 0.0000 |
| spike_down+vel_negative | 124 | 79.0% | $5.08 | 0.0000 |
| spike_down+vel_aligned | 124 | 79.0% | $5.08 | 0.0000 |
| vel_negative+vel_aligned | 124 | 79.0% | $5.08 | 0.0000 |
| spike_down+vel_negative+vel_aligned | 124 | 79.0% | $5.08 | 0.0000 |
| spike_down+vel_negative+spike_mag_low | 124 | 79.0% | $5.08 | 0.0000 |
| spike_down+vel_aligned+spike_mag_low | 124 | 79.0% | $5.08 | 0.0000 |
| vel_negative+vel_aligned+spike_mag_low | 124 | 79.0% | $5.08 | 0.0000 |

## Top Conditions at 10s Horizon

| Condition | n | Accuracy | Mean PnL | p-value |
|-----------|---|----------|----------|---------|
| vel_aligned+time_gt_300 | 169 | 84.6% | $5.10 | 0.0000 |
| vel_aligned+spike_mag_low+time_gt_300 | 169 | 84.6% | $5.10 | 0.0000 |
| vel_aligned+zone_not_neutral+time_gt_300 | 164 | 84.1% | $5.12 | 0.0000 |
| vel_aligned+time_gt_300+spread_tight | 142 | 82.4% | $5.05 | 0.0000 |
| time_gt_300 | 187 | 81.8% | $4.82 | 0.0000 |
| zone_not_neutral+time_gt_300 | 176 | 81.8% | $4.88 | 0.0000 |
| spike_mag_low+time_gt_300 | 187 | 81.8% | $4.82 | 0.0000 |
| zone_not_neutral+spike_mag_low+time_gt_300 | 176 | 81.8% | $4.88 | 0.0000 |
| spike_down+vel_negative+zone_not_neutral | 119 | 81.5% | $5.62 | 0.0000 |
| spike_down+vel_aligned+zone_not_neutral | 119 | 81.5% | $5.62 | 0.0000 |

## Top Conditions at 15s Horizon

| Condition | n | Accuracy | Mean PnL | p-value |
|-----------|---|----------|----------|---------|
| vel_aligned+time_gt_300 | 169 | 87.0% | $5.54 | 0.0000 |
| vel_aligned+spike_mag_low+time_gt_300 | 169 | 87.0% | $5.54 | 0.0000 |
| vel_aligned+zone_not_neutral+time_gt_300 | 164 | 86.6% | $5.52 | 0.0000 |
| spike_down+vel_negative+zone_not_neutral | 119 | 84.9% | $6.00 | 0.0000 |
| spike_down+vel_aligned+zone_not_neutral | 119 | 84.9% | $6.00 | 0.0000 |
| vel_negative+vel_aligned+zone_not_neutral | 119 | 84.9% | $6.00 | 0.0000 |
| spike_down+vel_negative | 124 | 84.7% | $5.94 | 0.0000 |
| spike_down+vel_aligned | 124 | 84.7% | $5.94 | 0.0000 |
| vel_negative+vel_aligned | 124 | 84.7% | $5.94 | 0.0000 |
| spike_down+vel_negative+vel_aligned | 124 | 84.7% | $5.94 | 0.0000 |

## Top Conditions at 30s Horizon

| Condition | n | Accuracy | Mean PnL | p-value |
|-----------|---|----------|----------|---------|
| vel_aligned+time_gt_300 | 169 | 87.0% | $5.84 | 0.0000 |
| vel_aligned+spike_mag_low+time_gt_300 | 169 | 87.0% | $5.84 | 0.0000 |
| time_gt_300 | 187 | 86.6% | $5.63 | 0.0000 |
| spike_mag_low+time_gt_300 | 187 | 86.6% | $5.63 | 0.0000 |
| vel_aligned+zone_not_neutral+time_gt_300 | 164 | 86.6% | $5.78 | 0.0000 |
| spike_down | 136 | 86.0% | $5.93 | 0.0000 |
| spike_down+spike_mag_low | 136 | 86.0% | $5.93 | 0.0000 |
| zone_not_neutral+time_gt_300 | 176 | 85.8% | $5.59 | 0.0000 |
| zone_not_neutral+spike_mag_low+time_gt_300 | 176 | 85.8% | $5.59 | 0.0000 |
| spike_down+zone_not_neutral | 126 | 85.7% | $5.99 | 0.0000 |

## Implementation Code Snippets

```python
# Top conditions for each horizon
# Copy these into your trading strategy

# spike_down: 86.0% at 30s
# Conditions: spike_down

# vel_positive: 73.2% at 30s
# Conditions: vel_positive

# vel_negative: 81.8% at 15s
# Conditions: vel_negative

# vel_aligned: 77.9% at 15s
# Conditions: vel_aligned

# vel_strong: 74.0% at 30s
# Conditions: vel_strong

```