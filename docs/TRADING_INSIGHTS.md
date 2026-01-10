# Trading Insights & Empirical Findings

Documented observations from live paper trading sessions.

---

## Polymarket Wallet Architecture

### Wallet Types on Polymarket

1. **EOA (Externally Owned Account)**
   - Your private key → 0x5a84...2c
   - Direct wallet you control
   - YOU pay gas for every transaction

2. **Safe (Smart Contract Wallet)**
   - Derived from EOA → 0xAa26...6f
   - Contract deployed on Polygon
   - RELAYER pays gas on your behalf
   - You sign, they submit

### How Gas-Free Transactions Work

```
YOU                    RELAYER                 BLOCKCHAIN
 |                        |                        |
 | 1. Sign transaction    |                        |
 |----------------------->|                        |
 |                        | 2. Submit to Safe      |
 |                        |    (Relayer pays gas)  |
 |                        |----------------------->|
 |                        |                        |
 |                        | 3. Safe executes       |
 |<-----------------------|<-----------------------|
 | 4. Done (you paid $0)  |                        |
```

### Why Deploy the Safe First?

| Without Safe | With Safe |
|--------------|-----------|
| You pay gas (~$0.01-0.10) | Relayer pays gas ($0) |
| Direct EOA transactions | Transactions via Safe contract |
| Works immediately | Requires one-time deployment |

### Your Current Setup

| Component | Address | Type |
|-----------|---------|------|
| EOA (Signer) | `0xc22edB57ef0eB97B3fa7baC7B440e8C9FfA2D299` | Private key holder |
| Magic Proxy | `0x1404341D718bbd4e5683877fa57f1249016B8989` | Trading wallet (holds funds) |
| Builder Safe | `0xeCf99c5f646dEe86B4Bca1C33F013a8ACe6c0dbB` | Gnosis Safe (for Builder Relayer) |

---

---

## 2025-12-28

### Grid Maker Gabagool-Style Opportunistic Accumulation

**Market**: `btc-updown-15m-1766902500`
**Observation Time**: 06:20 UTC (11:50 AM IST)
**Outcome**: Resolved UP, +$1.08 PNL

**Key Finding**: Grid Maker's flexible accumulation captured a spike mispricing that rigid threshold strategies would miss.

**The Trade**:
```
06:20:57 UTC | DOWN | 8 shares @ $0.14 | Pair cost dropped to $0.83
```

**Context**:
- Prior pair cost was hovering around $0.98-1.01
- A sudden 8-share DOWN fill at $0.14 (vs typical $0.35-0.58 range) appeared
- Grid Maker grabbed it instantly, dropping running pair cost from ~$1.00 to $0.83
- This single opportunistic trade contributed ~$1.36 to final PNL

**Why This Matters**:
1. **Calculus MAKER** would have been bidding at `best_bid - threshold` and likely missed this flash
2. **Grid Maker's Gabagool heritage** - always ready to accumulate when price is favorable, regardless of "expected" levels
3. **Liquidity pockets** exist in these markets; flexible strategies capture them

**Recommendation**:
Consider adding an "opportunistic grab" mode to Calculus that bypasses threshold checks when pair_cost drops significantly below recent average (e.g., >10% improvement).

---

## Template for Future Entries

### [Title]

**Market**: `slug`
**Observation Time**: UTC (IST)
**Outcome**:

**Key Finding**:

**The Trade(s)**:
```
timestamp | side | shares @ price | notes
```

**Why This Matters**:

**Recommendation**:

---
