# Roadmap: Polymarket AMM Bot

## Overview

Build an automated market-making bot for Polymarket BTC 15-minute Up/Down markets. Starting from environment setup through live trading, each phase delivers testable functionality. The bot will execute balanced pair trades where pair_cost < $1.00, guaranteeing profit regardless of outcome. Includes Discord notifications, risk management, and comprehensive monitoring.

## Phases

- [ ] **Phase 1: Foundation** - Environment, API credentials, network failover, Discord setup
- [ ] **Phase 2: Market Intelligence** - Data fetching, orderbook parsing, opportunity detection
- [ ] **Phase 3: Trading Core** - Order execution, position tracking, balance management, logging
- [ ] **Phase 4: Dry Run** - Paper trading simulation, strategy validation
- [ ] **Phase 5: Live Trading** - Real execution, risk management, notifications, monitoring

## Phase Details

### Phase 1: Foundation
**Goal**: Working Python environment with API connection, network resilience, and Discord integration
**Depends on**: Nothing (first phase)
**Plans**: 4 plans

Plans:
- [ ] 01-01: Python environment + config system (venv, dependencies, wallet-agnostic config for easy switching)
- [ ] 01-02: Polymarket API authentication (wallet setup, credentials, connection test)
- [ ] 01-03: Network failover (monitor WiFi, auto-switch to 2 backups, 15s polling to return to primary)
- [ ] 01-04: Discord bot setup (create 3 channels: #pnl-summary, #losses, #outages with webhook config)

### Phase 2: Market Intelligence
**Goal**: Fetch and analyze BTC Up/Down markets, identify trading opportunities
**Depends on**: Phase 1
**Plans**: 2 plans

Plans:
- [ ] 02-01: Market data fetching (list markets, filter 15-min BTC, parse responses)
- [ ] 02-02: Orderbook analysis (best bid/ask, spread calculation, pair cost detection)

### Phase 3: Trading Core
**Goal**: Execute orders, track positions, manage balance, and log all trades
**Depends on**: Phase 2
**Plans**: 4 plans

Plans:
- [ ] 03-01: Order placement (limit orders, parallel execution for Up/Down to prevent legging)
- [ ] 03-02: Position tracking (inventory, average prices, P&L calculation)
- [ ] 03-03: Balance management (imbalance detection, recovery logic)
- [ ] 03-04: Trade logging (CSV export with timestamps, prices, quantities, P&L for analysis)

### Phase 4: Dry Run
**Goal**: Paper trading mode to validate strategy without risking capital
**Depends on**: Phase 3
**Plans**: 2 plans

Plans:
- [ ] 04-01: Simulation engine (mock fills, realistic conditions, queue position uncertainty)
- [ ] 04-02: Strategy validation (run 3+ market cycles, verify profitability, test edge cases)

### Phase 5: Live Trading
**Goal**: Production-ready bot with real money execution, comprehensive safety, and monitoring
**Depends on**: Phase 4
**Plans**: 4 plans

Plans:
- [ ] 05-01: Live execution (real orders, position sync, error handling, graceful recovery)
- [ ] 05-02: Risk management (daily loss limits, position size limits, auto-pause on consecutive losses)
- [ ] 05-03: Discord notifications (PNL summaries to #pnl-summary, losses @mention to #losses, outages @mention to #outages)
- [ ] 05-04: Terminal dashboard + performance metrics (real-time display, ROI tracking, win rate)

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 0/4 | Not started | - |
| 2. Market Intelligence | 0/2 | Not started | - |
| 3. Trading Core | 0/4 | Not started | - |
| 4. Dry Run | 0/2 | Not started | - |
| 5. Live Trading | 0/4 | Not started | - |

**Total: 16 plans across 5 phases**

## v1.1 (Future)
- Multi-market concurrent trading
- Additional asset support beyond BTC
