# Analyze Paper Trades

Run the Daily Trade Analyzer to analyze paper trading results.

## Arguments

- `$ARGUMENTS` - Optional flags: `--date YYYY-MM-DD`, `--strategy NAME`, `--all`, `--compare`

## Instructions

Run the trade analyzer script with the provided arguments:

```bash
python scripts/analyze_trades.py $ARGUMENTS
```

After running, provide a brief summary of:
1. Total P&L and win rate
2. Best/worst performing strategy (if comparing)
3. Key insights or concerns (e.g., high volatility, low Sharpe)
4. Recommendations based on the metrics

### Common Usage Examples

- `/analyze-trades` - Analyze today's/recent trades
- `/analyze-trades --date 2026-01-09` - Specific date
- `/analyze-trades --strategy AGGRESSIVE` - Single strategy
- `/analyze-trades --all --compare` - Compare all strategies
- `/analyze-trades --all --json` - Output as JSON for further processing

### Key Metrics to Highlight

- **Sharpe Ratio**: >1.0 is good, >2.0 is excellent
- **Win Rate**: 50%+ for hedged strategies, 70%+ for directional
- **Pair Cost**: Should be < $1.00 for profitable hedging
- **Unhedged Shares**: Lower is better, 0 is ideal
- **Sortino Ratio**: Higher is better (only penalizes downside)
