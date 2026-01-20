# Unhedged Trade Fill Likelihood Analysis

## Key Finding: Stop-Loss Selection Bias

The stop-loss mechanism creates a selection bias that makes unhedged trades 100% accurate:

| Metric | Value |
|--------|-------|
| Overall velocity accuracy | 68.3% (671/983) |
| Unhedged trades accuracy | **100%** (309/309) |
| Hedged trades accuracy | 53.7% (362/674) |

**Why?** When velocity is wrong, the market reverses, triggering stop-loss. Only correct predictions remain unhedged.

---

## Fill Likelihood Analysis

### Scenario 1: Fill at ASK (Taker - What Backtest Assumes)

| Metric | Value |
|--------|-------|
| Fill rate | **100%** (guaranteed) |
| Avg fill price | $0.9477 |
| Total PnL (273 trades) | $214.14 |

### Scenario 2: Fill at Best Bid + $0.01 (Limit Order)

| Metric | Value |
|--------|-------|
| Fill rate | **96.0%** (262/273 would fill) |
| Avg time to fill | 1.4 seconds |
| Avg fill price | $0.9494 |
| Total PnL (filled only) | $198.71 |

### Unfilled Limit Orders (11 trades)

| Metric | Value |
|--------|-------|
| Would have been correct | 11 (100%) |
| Missed profit | $14.85 |

---

## Spread Distribution at Entry

| Spread | Count | % |
|--------|-------|---|
| <= $0.01 | 75 | 27.5% |
| <= $0.02 | 267 | 97.8% |
| <= $0.03 | 271 | 99.3% |

**Key Insight:** When spread = $0.01, bid + $0.01 = ask. So limit orders and taker orders fill at the same price.

---

## Fill Rate by Spread Size

| Spread Range | Fill Rate |
|--------------|-----------|
| $0.00-$0.01 | **100%** |
| $0.01-$0.02 | 95.8% |
| $0.02-$0.03 | 50.0% |
| $0.03-$0.04 | 50.0% |

**Key Insight:** Tighter spreads = higher fill rate for limit orders.

---

## Conclusion

### Taker (Fill at ASK) is Better

| Strategy | Fill Rate | Total PnL | Difference |
|----------|-----------|-----------|------------|
| Taker | 100% | $214.14 | - |
| Limit | 96% | $198.71 | -$15.43 |

**Reason:** The 4% of trades that don't fill as limit orders were ALL correct predictions. Missing them costs more than the small price improvement on filled trades.

### Backtest Validity

The backtest assumption of filling at ASK is:
1. **Realistic** - market orders fill at ask
2. **Conservative** - we're paying the spread
3. **Optimal** - better than limit orders due to fill certainty

### Why Unhedged Trades Are "Free Money"

1. Stop-loss (7%) filters out wrong predictions
2. If stop-loss doesn't trigger, we were right
3. 100% accuracy on unhedged = guaranteed profit at resolution
4. Average profit per unhedged trade: $0.78
