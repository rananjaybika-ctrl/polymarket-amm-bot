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
- [ ] Discord notifications working (PNL summaries, loss alerts, outage alerts)
- [ ] Network failover successfully switches to backup WiFi on disconnect
- [ ] Market rotation works (auto-advance through up to 4 consecutive 15-min markets)
- [ ] Auto-claim successfully claims winning positions after market resolution
- [ ] WebSocket connection provides real-time orderbook updates

## Constraints

- **Developer skill**: Python beginner (code must be clean, well-documented)
- **Capital**: $50-100 learning budget (conservative risk parameters)
- **Deployment**: Local Mac machine (no cloud initially)
- **API**: Polymarket credentials not yet set up (Phase 1 includes setup)
- **Strategy focus**: Balanced inventory first, pair cost optimization second
- **Wallet design**: Wallet-agnostic config (easy switch from test to main account)
- **Test account**: Separate $100 Polygon wallet for learning phase, switch to main later

## Discord Integration

Server channels for notifications:
- `#pnl-summary` - Trade completions, daily P&L summaries (no mention)
- `#losses` - Loss events, losing trades (@mention for alerts)
- `#outages` - Network disconnects, API errors, website issues (@mention for alerts)

## Network Resilience

- Primary WiFi network (main connection)
- Backup network 1 (first fallback)
- Backup network 2 (second fallback)
- Poll every 15 seconds to return to primary when available

## Market Rotation

- Automatically advance to next 15-minute market when current market closes
- Maximum 60-minute trading window (4 consecutive 15-min markets)
- Smooth transition between markets without manual intervention
- Respects time window limits from Gabagool strategy

## Auto-Claim Winnings

- Check for resolved markets every 5 minutes
- Automatically claim winning positions (Up or Down tokens → USDC)
- Log claimed amounts to Discord #pnl-summary
- Handle partial claims and network errors gracefully

## Real-Time Data (WebSockets)

- WebSocket connection to Polymarket for live orderbook updates
- Instant fill notifications for faster response
- Auto-reconnect on connection drops
- Fallback to HTTP polling if WebSocket unavailable

## Out of Scope

What we're NOT building in v1.0:

- Directional trading strategies (betting on outcome)
- Multi-market concurrent trading (deferred to v1.1)
- Complex grid systems (keep it simple: 1 level)
- Cloud deployment or 24/7 operation
- GUI/dashboard (terminal dashboard only)
- Backtesting framework
