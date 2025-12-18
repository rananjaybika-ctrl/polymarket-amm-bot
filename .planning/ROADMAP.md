# Roadmap: Polymarket AMM Bot

## Overview

Build an automated market-making bot for Polymarket BTC 15-minute Up/Down markets. Starting from environment setup through live trading, each phase delivers testable functionality. The bot will execute balanced pair trades where pair_cost < $1.00, guaranteeing profit regardless of outcome. Includes Discord notifications, risk management, and comprehensive monitoring.

## Phases

- [x] **Phase 1: Foundation** - Environment, API credentials, network failover, Discord setup
- [x] **Phase 2: Market Intelligence** - Data fetching, orderbook parsing, market rotation
- [x] **Phase 3: Trading Core** - Order execution, position tracking, balance management, WebSockets
- [ ] **Phase 4: Dry Run** - Paper trading simulation, strategy validation
- [ ] **Phase 5: Live Trading** - Real execution, risk management, auto-claim, monitoring

## Phase Details

### Phase 1: Foundation
**Goal**: Working Python environment with API connection, network resilience, and Discord integration
**Depends on**: Nothing (first phase)
**Plans**: 4 plans

Plans:
- [x] 01-01: Python environment + config system (venv, dependencies, wallet-agnostic config for easy switching)
- [x] 01-02: Polymarket API authentication (wallet setup, credentials, connection test)
- [x] 01-03: Network failover (monitor WiFi, auto-switch to 2 backups, 15s polling to return to primary)
- [x] 01-04: Discord bot setup (create 3 channels: #pnl-summary, #losses, #outages with webhook config)

### Phase 2: Market Intelligence
**Goal**: Fetch and analyze BTC Up/Down markets, identify opportunities, handle market rotation
**Depends on**: Phase 1
**Plans**: 3 plans

Plans:
- [x] 02-01: Market data fetching (list markets, filter 15-min BTC, parse responses)
- [x] 02-02: Orderbook analysis (best bid/ask, spread calculation, pair cost detection)
- [x] 02-03: Market rotation (auto-advance to next 15-min market, enforce 60-min window limit)

### Phase 3: Trading Core
**Goal**: Execute orders, track positions, manage balance, real-time data via WebSockets
**Depends on**: Phase 2
**Plans**: 5 plans

Plans:
- [x] 03-01: Order placement (limit orders, parallel execution for Up/Down to prevent legging)
- [x] 03-02: Position tracking (inventory, average prices, P&L calculation)
- [x] 03-03: Balance management (imbalance detection, recovery logic)
- [x] 03-04: Trade logging (CSV export with timestamps, prices, quantities, P&L for analysis)
- [x] 03-05: WebSocket integration (real-time orderbook streaming, instant fills, auto-reconnect)

### Phase 4: Dry Run
**Goal**: Paper trading mode to validate strategy without risking capital
**Depends on**: Phase 3
**Plans**: 2 plans

Plans:
- [ ] 04-01: Simulation engine (mock fills, realistic conditions, queue position uncertainty)
- [ ] 04-02: Strategy validation (run 3+ market cycles, verify profitability, test edge cases)

### Phase 5: Live Trading
**Goal**: Production-ready bot with real money execution, auto-claim, and comprehensive monitoring
**Depends on**: Phase 4
**Plans**: 5 plans

Plans:
- [ ] 05-01: Live execution (real orders, position sync, error handling, graceful recovery)
- [ ] 05-02: Risk management (daily loss limits, position size limits, auto-pause on consecutive losses)
- [ ] 05-03: Discord notifications (PNL summaries to #pnl-summary, losses @mention to #losses, outages @mention to #outages)
- [ ] 05-04: Terminal dashboard + performance metrics (real-time display, ROI tracking, win rate)
- [ ] 05-05: Auto-claim winnings (check resolution every 5 mins, claim winning positions to USDC)

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 4/4 | Complete | Dec 18, 2025 |
| 2. Market Intelligence | 3/3 | Complete | Dec 19, 2025 |
| 3. Trading Core | 5/5 | Complete | Dec 19, 2025 |
| 4. Dry Run | 0/2 | Not started | - |
| 5. Live Trading | 0/5 | Not started | - |

**Total: 19 plans across 5 phases (12 complete)**

## v1.1 (Future)
- Multi-market concurrent trading
- Additional asset support beyond BTC
