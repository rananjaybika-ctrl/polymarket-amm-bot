# Roadmap: Polymarket AMM Bot

## Overview

Build an automated market-making bot for Polymarket BTC 15-minute Up/Down markets. Starting from environment setup through live trading, each phase delivers testable functionality. The bot will execute balanced pair trades where pair_cost < $1.00, guaranteeing profit regardless of outcome.

## Phases

- [ ] **Phase 1: Foundation** - Environment, API credentials, wallet setup, network failover
- [ ] **Phase 2: Market Intelligence** - Data fetching, orderbook parsing, opportunity detection
- [ ] **Phase 3: Trading Core** - Order execution, position tracking, balance management
- [ ] **Phase 4: Dry Run** - Paper trading simulation, strategy validation
- [ ] **Phase 5: Live Trading** - Real execution with safety controls

## Phase Details

### Phase 1: Foundation
**Goal**: Working Python environment connected to Polymarket API with network resilience
**Depends on**: Nothing (first phase)
**Plans**: 3 plans

Plans:
- [ ] 01-01: Python environment setup (venv, dependencies, project structure)
- [ ] 01-02: Polymarket API authentication (wallet, credentials, connection test)
- [ ] 01-03: Network failover (monitor WiFi, auto-switch to backups, 15s polling to return to primary)

### Phase 2: Market Intelligence
**Goal**: Fetch and analyze BTC Up/Down markets, identify trading opportunities
**Depends on**: Phase 1
**Plans**: 2 plans

Plans:
- [ ] 02-01: Market data fetching (list markets, filter 15-min BTC, parse responses)
- [ ] 02-02: Orderbook analysis (best bid/ask, spread calculation, pair cost detection)

### Phase 3: Trading Core
**Goal**: Execute orders and track positions with balance management
**Depends on**: Phase 2
**Plans**: 3 plans

Plans:
- [ ] 03-01: Order placement (limit orders, parallel execution for Up/Down)
- [ ] 03-02: Position tracking (inventory, average prices, P&L calculation)
- [ ] 03-03: Balance management (imbalance detection, recovery logic)

### Phase 4: Dry Run
**Goal**: Paper trading mode to validate strategy without risking capital
**Depends on**: Phase 3
**Plans**: 2 plans

Plans:
- [ ] 04-01: Simulation engine (mock fills, realistic conditions)
- [ ] 04-02: Strategy validation (run 3+ market cycles, verify profitability)

### Phase 5: Live Trading
**Goal**: Production-ready bot with real money execution and safety controls
**Depends on**: Phase 4
**Plans**: 2 plans

Plans:
- [ ] 05-01: Live execution (real orders, position sync, error handling)
- [ ] 05-02: Monitoring & safety (logging, pair cost limits, emergency stops)

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 0/3 | Not started | - |
| 2. Market Intelligence | 0/2 | Not started | - |
| 3. Trading Core | 0/3 | Not started | - |
| 4. Dry Run | 0/2 | Not started | - |
| 5. Live Trading | 0/2 | Not started | - |
