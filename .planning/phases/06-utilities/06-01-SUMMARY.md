# Summary: Polywalltrack - Multi-Market Wallet Analyzer

## Completed: 2025-12-22

## What Was Built

Created `scripts/polywalltrack.py` - a CLI tool that analyzes a Polymarket wallet's trading performance across multiple markets.

## Usage

```bash
# Single market by slug
python scripts/polywalltrack.py -w <wallet> -m "btc-updown-15m-1766221200"

# Multiple markets
python scripts/polywalltrack.py -w <wallet> -m "slug1,slug2,slug3"

# Search by market name
python scripts/polywalltrack.py -w <wallet> -m "Bitcoin Up or Down December 20 4:15AM"

# Save to file
python scripts/polywalltrack.py -w <wallet> -m "slug1,slug2" -o report.txt
```

## Sample Output (gabagool22 wallet)

```
============================================================
  POLYWALLTRACK ANALYSIS
============================================================
  Wallet: 0x6031b6ee...51f96d
  Timestamp: 2025-12-22 16:17:07
  Markets: 3 requested, 3 with trades

+-----------------------------------+--------------+
| METRIC                            |        VALUE |
+-----------------------------------+--------------+
| Markets Analyzed                  |            3 |
| Markets Resolved                  |            3 |
| Markets Won                       |            3 |
| Markets Lost                      |            0 |
| Win Rate                          |       100.0% |
+-----------------------------------+--------------+
| Total PNL                         | $    +103.73 |
| Avg PNL/Market                    | $     +34.58 |
| Max Win                           | $     +63.56 |
| Max Loss                          | $      +0.00 |
+-----------------------------------+--------------+
| Total Trades                      |         1500 |
| Total Volume (cost)               | $    8750.42 |
| Total Shares Bought               |      17648.1 |
| Avg Trade Size                    | $       5.83 |
+-----------------------------------+--------------+

PER-MARKET BREAKDOWN:
+----------------------------+--------+--------+-----------+----------+
| MARKET                     | TRADES | WINNER |       PNL |   VOLUME |
+----------------------------+--------+--------+-----------+----------+
| btc-updown-15m-1766221200  |    500 |    YES |   $+26.37 | $2917.99 |
| btc-updown-15m-1766222100  |    500 |    YES |   $+63.56 | $2861.59 |
| btc-updown-15m-1766223000  |    500 |     NO |   $+13.80 | $2970.84 |
+----------------------------+--------+--------+-----------+----------+
```

## Features

- **Multi-market analysis**: Analyze 1 or more markets in a single command
- **Flexible input**: Accepts market slugs or search queries
- **Quantitative summary**: Win rate, total PNL, avg PNL, max win/loss
- **Per-market breakdown**: Individual market performance table
- **Position details**: YES/NO shares bought/sold for each market
- **Winner detection**: Uses Polymarket API outcomePrices or infers from trade prices

## Files Created

- `scripts/polywalltrack.py` - Main CLI tool (340 lines)

## Verification

- [x] `--help` shows usage
- [x] Single market analysis works
- [x] Multi-market analysis works
- [x] Search query input works
- [x] Graceful handling of markets with no trades

## Future Enhancements

- Add date range filtering (`--from`, `--to`)
- Add CSV/JSON export format options
- Add comparison mode (compare two wallets)
- Add chart generation option (reuse chart_generator.py)
