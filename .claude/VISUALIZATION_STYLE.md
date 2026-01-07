# Visualization Style Guide for Explanations

Use this format when explaining trading concepts, order flow, or market mechanics.

## Key Elements

### 1. Section Headers with Box Drawing
```
SECTION TITLE
═══════════════════════════════════════════════════════════════════════
```

### 2. Orderbook Format
```
  UP SHARES                          DOWN SHARES
  ─────────────────────────          ─────────────────────────
  Asks:                              Asks:
    $0.53 - 100 shares                 $0.52 - 100 shares
    $0.51 - 200 shares  ← best ask     $0.50 - 200 shares  ← best ask
  ─────────────────────────          ─────────────────────────
    $0.50 - 150 shares  ← best bid     $0.49 - 150 shares  ← best bid
    $0.48 - 50 shares                  $0.47 - 50 shares
  Bids:                              Bids:
```

### 3. Info Boxes
```
  ┌─────────────────────────────────────────────────────────────────┐
  │  Important information goes here                                │
  │  - Bullet point 1                                              │
  │  - Bullet point 2                                              │
  └─────────────────────────────────────────────────────────────────┘
```

### 4. Timeline Format
```
  T=0s:   Event 1 happens
  T=5s:   Event 2 happens
  T=10s:  Event 3 happens
```

### 5. Side-by-Side Comparison
```
  WITHOUT FEATURE (BAD)              WITH FEATURE (GOOD)
  ─────────────────────────          ─────────────────────────
  Step 1                             Step 1
  Step 2 (problem)                   Step 2 (avoided)
  Result: LOSS                       Result: SAFE
```

### 6. Comparison Tables
```
  ┌────────────────────┬──────────────────┬──────────────────┐
  │                    │ Option A         │ Option B         │
  ├────────────────────┼──────────────────┼──────────────────┤
  │ Metric 1           │ Value            │ Value            │
  │ Metric 2           │ Value            │ Value            │
  │ Result             │ Outcome          │ Outcome          │
  └────────────────────┴──────────────────┴──────────────────┘
```

### 7. Flow Diagrams
```
  Event A
      │
      ▼
  ┌─────────────────────────────────────┐
  │ Process/Decision                    │
  └─────────────────────────────────────┘
      │
      ▼
  Event B
```

### 8. Key-Value Pairs
```
  Best bid:    $0.50
  Our price:   $0.47 (bid - $0.03)
  Depth:       $0.03
  Timeout:     30s (deep in book)
```

## Example Structure

```
SETUP: Context and Initial State
═══════════════════════════════════════════════════════════════════════

  [Describe initial conditions, prices, positions]


EVENT HAPPENS (T=Xs)
═══════════════════════════════════════════════════════════════════════

  [Describe what changed and why it matters]

  ┌─────────────────────────────────────────────────────────────────┐
  │  Key insight or implication                                     │
  └─────────────────────────────────────────────────────────────────┘


SCENARIO A: Without Feature
═══════════════════════════════════════════════════════════════════════

  T=Xs: Bad thing happens
  Result: LOSS


SCENARIO B: With Feature
═══════════════════════════════════════════════════════════════════════

  T=Xs: Feature prevents bad thing
  Result: SAFE


SUMMARY
═══════════════════════════════════════════════════════════════════════

  ┌────────────────────┬──────────────────┬──────────────────┐
  │                    │ Without          │ With             │
  ├────────────────────┼──────────────────┼──────────────────┤
  │ Outcome            │ Bad              │ Good             │
  └────────────────────┴──────────────────┴──────────────────┘
```

## When to Use

- Explaining order flow and market mechanics
- Comparing strategies or approaches
- Showing timeline of events
- Demonstrating why a feature helps/hurts
- Visualizing orderbook states
