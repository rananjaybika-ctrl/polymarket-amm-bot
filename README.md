# Polymarket AMM Bot

> **LEGACY** -- This project is no longer actively maintained.

Automated market-making bot for Polymarket's BTC binary options markets. Designed to exploit pricing inefficiencies using real-time crypto price feeds, statistical signal detection, and whale order flow analysis.

## Key Results

- 3 quantitative strategies validated across 167 hours of out-of-sample backtest data
- 0.90 Sharpe ratio | +$15.20/hr expected return at 50-share size
- Whale order flow reverse-engineering achieving 57--82% directional accuracy

## Strategies

1. **EWMA Spike Detection** -- Identifies statistically significant price movements using exponentially weighted moving averages against Binance 60Hz feeds
2. **Whale Order Flow** -- Reverse-engineers large participant positioning from Polymarket CLOB data for directional signals
3. **Mean Reversion** -- Captures pricing dislocations between spot crypto prices and binary option implied probabilities

## Tech Stack

- Python | WebSockets | Polymarket CLOB API | Polygon L2
- Binance real-time price feeds (60Hz)
- Statistical analysis and backtesting framework

## Development Approach

Built using AI-assisted development (Claude) -- strategy design, signal logic, and research direction by author; implementation via AI pair programming. The author provided all domain expertise, trading hypotheses, and quantitative specifications while leveraging AI as the engineering layer.

## Status

This project is no longer actively maintained. The author transitioned to direct discretionary/prop trading, where applying market knowledge directly proved more capital-efficient than maintaining automated edge cases.
