# Polymarket AMM Bot

**One-liner**: An automated market-making bot that trades BTC 15-minute Up/Down markets on Polymarket, profiting from pair_cost < $1.00 arbitrage.

## Problem

Manual trading on Polymarket 15-min markets is time-intensive and prone to "legging risk" (getting filled on one side but not the other). This bot automates balanced pair acquisition where `Up_price + Down_price < $1.00`, guaranteeing profit on resolution regardless of outcome.

## Success Criteria

How we know it worked:

- [ ] Bot connects to Polymarket API and fetches live market data
- [ ] Executes balanced trades (equal Up/Down shares) with pair_cost < $0.98
- [ ] Achieves 3+ profitable market cycles in dry-run mode
- [ ] Completes 1 successful live trade with $5-10 capital
- [ ] Maintains position imbalance < 20% throughout operation

## Constraints

- **Developer skill**: Python beginner (code must be clean, well-documented)
- **Capital**: $50-100 learning budget (conservative risk parameters)
- **Deployment**: Local Mac machine (no cloud initially)
- **API**: Polymarket credentials not yet set up (Phase 1 includes setup)
- **Strategy focus**: Balanced inventory first, pair cost optimization second

## Out of Scope

What we're NOT building in v1.0:

- Directional trading strategies (betting on outcome)
- Multi-market concurrent trading
- Complex grid systems (keep it simple: 1 level)
- Cloud deployment or 24/7 operation
- GUI/dashboard (CLI monitoring only)
- Backtesting framework
