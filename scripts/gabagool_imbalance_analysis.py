#!/usr/bin/env python3
"""
Gabagool22 Imbalance vs Binance Price Correlation Analysis

Analyzes whether Gabagool's position imbalances correlate with Binance BTC price movement.

Time Ranges:
- Range 1: Jan 9 02:45 to Jan 10 01:45 EST
- Range 2: Jan 7 02:30 to Jan 8 03:15 EST
"""

import requests
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import defaultdict
import statistics
import csv

# API endpoints
TRADES_URL = "https://data-api.polymarket.com/trades"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Gabagool22's wallet
WALLET = "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d"

ET = ZoneInfo('America/New_York')
UTC = ZoneInfo('UTC')


@dataclass
class MarketAnalysis:
    """Analysis for a single 15-min market."""
    slug: str
    market_start: datetime
    market_end: datetime

    # Position data
    up_shares: float = 0
    down_shares: float = 0
    up_cost: float = 0
    down_cost: float = 0
    trade_count: int = 0

    # Binance data
    btc_open: float = 0
    btc_close: float = 0
    btc_high: float = 0
    btc_low: float = 0
    btc_change_pct: float = 0
    btc_direction: str = ""  # "UP" or "DOWN"

    # Derived
    @property
    def imbalance(self) -> float:
        return self.up_shares - self.down_shares

    @property
    def imbalance_pct(self) -> float:
        total = self.up_shares + self.down_shares
        if total == 0:
            return 0
        return (self.imbalance / total) * 100

    @property
    def imbalance_direction(self) -> str:
        if self.imbalance > 5:
            return "UP_HEAVY"
        elif self.imbalance < -5:
            return "DOWN_HEAVY"
        return "BALANCED"

    @property
    def pair_cost(self) -> float:
        if self.up_shares > 0 and self.down_shares > 0:
            avg_up = self.up_cost / self.up_shares
            avg_down = self.down_cost / self.down_shares
            return avg_up + avg_down
        return 0

    @property
    def imbalance_matches_direction(self) -> bool:
        """Does imbalance direction match BTC price direction?"""
        if self.imbalance > 10:  # Significantly UP heavy
            return self.btc_direction == "UP"
        elif self.imbalance < -10:  # Significantly DOWN heavy
            return self.btc_direction == "DOWN"
        return True  # Balanced = neutral match

    @property
    def expected_winner(self) -> str:
        """Which side wins based on BTC close vs open?"""
        return self.btc_direction


def generate_market_slugs(start_time: datetime, end_time: datetime) -> List[Tuple[str, datetime, datetime]]:
    """Generate BTC 15-min market slugs for a time range."""
    slugs = []
    current = start_time

    # Round down to nearest 15 minutes
    minute = (current.minute // 15) * 15
    current = current.replace(minute=minute, second=0, microsecond=0)

    while current < end_time:
        unix_ts = int(current.timestamp())
        slug = f"btc-updown-15m-{unix_ts}"
        market_end = current + timedelta(minutes=15)
        slugs.append((slug, current, market_end))
        current += timedelta(minutes=15)

    return slugs


def fetch_market_info(slug: str) -> Optional[Dict]:
    """Fetch market metadata from gamma API."""
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if data and len(data) > 0:
            event = data[0]
            markets = event.get("markets", [])
            if markets:
                return markets[0]
        return None
    except Exception as e:
        return None


def fetch_trades_for_market(condition_id: str) -> List[Dict]:
    """Fetch all trades for a market/wallet."""
    all_trades = []
    offset = 0
    page_limit = 1000

    while True:
        params = {
            "limit": page_limit,
            "offset": offset,
            "market": condition_id,
            "user": WALLET,
        }
        try:
            resp = requests.get(TRADES_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            break

        if isinstance(data, dict):
            batch = data.get("trades", [])
        elif isinstance(data, list):
            batch = data
        else:
            batch = []

        all_trades.extend(batch)

        if len(batch) < page_limit:
            break
        offset += page_limit

    return all_trades


def fetch_binance_kline(start_time: datetime, end_time: datetime) -> Dict:
    """Fetch Binance 15-min kline for the time period."""
    try:
        # Convert to milliseconds
        start_ms = int(start_time.timestamp() * 1000)
        end_ms = int(end_time.timestamp() * 1000)

        params = {
            "symbol": "BTCUSDT",
            "interval": "15m",
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1
        }

        resp = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data and len(data) > 0:
            kline = data[0]
            open_price = float(kline[1])
            high_price = float(kline[2])
            low_price = float(kline[3])
            close_price = float(kline[4])

            change_pct = ((close_price - open_price) / open_price) * 100
            direction = "UP" if close_price >= open_price else "DOWN"

            return {
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "change_pct": change_pct,
                "direction": direction
            }
    except Exception as e:
        pass

    return {}


def analyze_range(start_time: datetime, end_time: datetime, range_name: str) -> List[MarketAnalysis]:
    """Analyze all markets in a time range."""

    print(f"\n{'='*70}")
    print(f"ANALYZING: {range_name}")
    print(f"{'='*70}")
    print(f"Time: {start_time.strftime('%Y-%m-%d %H:%M ET')} to {end_time.strftime('%Y-%m-%d %H:%M ET')}")

    market_slugs = generate_market_slugs(start_time, end_time)
    print(f"Markets to analyze: {len(market_slugs)}")

    results = []

    for i, (slug, market_start, market_end) in enumerate(market_slugs):
        analysis = MarketAnalysis(
            slug=slug,
            market_start=market_start,
            market_end=market_end
        )

        # Fetch market info
        market_info = fetch_market_info(slug)
        if not market_info:
            print(f"  [{i+1}/{len(market_slugs)}] {slug[-15:]}: Market not found")
            continue

        condition_id = market_info.get("conditionId", "")
        if not condition_id:
            continue

        # Fetch trades
        raw_trades = fetch_trades_for_market(condition_id)
        buys = [t for t in raw_trades if t.get("side", "").upper() == "BUY"]

        if buys:
            analysis.trade_count = len(buys)

            for trade in buys:
                outcome = trade.get("outcome", "").lower()
                price = float(trade.get("price", 0))
                size = float(trade.get("size", 0))

                if outcome == "up":
                    analysis.up_shares += size
                    analysis.up_cost += price * size
                else:
                    analysis.down_shares += size
                    analysis.down_cost += price * size

        # Fetch Binance data
        binance_data = fetch_binance_kline(market_start, market_end)
        if binance_data:
            analysis.btc_open = binance_data["open"]
            analysis.btc_close = binance_data["close"]
            analysis.btc_high = binance_data["high"]
            analysis.btc_low = binance_data["low"]
            analysis.btc_change_pct = binance_data["change_pct"]
            analysis.btc_direction = binance_data["direction"]

        results.append(analysis)

        # Progress
        imb_dir = analysis.imbalance_direction
        btc_dir = analysis.btc_direction
        match = "✓" if analysis.imbalance_matches_direction else "✗"

        print(f"  [{i+1}/{len(market_slugs)}] {slug[-15:]}: "
              f"{analysis.trade_count} trades | "
              f"Imb: {analysis.imbalance:+.0f} ({imb_dir}) | "
              f"BTC: {analysis.btc_change_pct:+.2f}% ({btc_dir}) | "
              f"Match: {match}")

        # Rate limiting
        time.sleep(0.4)

    return results


def print_correlation_analysis(results: List[MarketAnalysis], range_name: str):
    """Print detailed correlation analysis."""

    print(f"\n{'='*70}")
    print(f"CORRELATION ANALYSIS: {range_name}")
    print(f"{'='*70}")

    # Filter to markets with trades
    active = [r for r in results if r.trade_count > 0]

    if not active:
        print("No active markets found.")
        return

    print(f"\nActive markets: {len(active)}")

    # === IMBALANCE DISTRIBUTION ===
    print(f"\n{'─'*70}")
    print("IMBALANCE DISTRIBUTION")
    print(f"{'─'*70}")

    up_heavy = [r for r in active if r.imbalance > 10]
    down_heavy = [r for r in active if r.imbalance < -10]
    balanced = [r for r in active if -10 <= r.imbalance <= 10]

    print(f"  UP Heavy (>10 shares): {len(up_heavy)} markets ({len(up_heavy)/len(active)*100:.1f}%)")
    print(f"  DOWN Heavy (<-10 shares): {len(down_heavy)} markets ({len(down_heavy)/len(active)*100:.1f}%)")
    print(f"  Balanced (-10 to +10): {len(balanced)} markets ({len(balanced)/len(active)*100:.1f}%)")

    if up_heavy:
        avg_up_imb = statistics.mean([r.imbalance for r in up_heavy])
        print(f"    Avg UP imbalance: +{avg_up_imb:.0f} shares")

    if down_heavy:
        avg_down_imb = statistics.mean([r.imbalance for r in down_heavy])
        print(f"    Avg DOWN imbalance: {avg_down_imb:.0f} shares")

    # === BTC DIRECTION DISTRIBUTION ===
    print(f"\n{'─'*70}")
    print("BTC PRICE DIRECTION")
    print(f"{'─'*70}")

    btc_up = [r for r in active if r.btc_direction == "UP"]
    btc_down = [r for r in active if r.btc_direction == "DOWN"]

    print(f"  BTC went UP: {len(btc_up)} markets ({len(btc_up)/len(active)*100:.1f}%)")
    print(f"  BTC went DOWN: {len(btc_down)} markets ({len(btc_down)/len(active)*100:.1f}%)")

    if btc_up:
        avg_up_move = statistics.mean([r.btc_change_pct for r in btc_up])
        print(f"    Avg UP move: +{avg_up_move:.3f}%")

    if btc_down:
        avg_down_move = statistics.mean([r.btc_change_pct for r in btc_down])
        print(f"    Avg DOWN move: {avg_down_move:.3f}%")

    # === CORRELATION ANALYSIS ===
    print(f"\n{'─'*70}")
    print("IMBALANCE vs BTC DIRECTION CORRELATION")
    print(f"{'─'*70}")

    # When BTC went UP, what was Gabagool's imbalance?
    btc_up_imbalances = [r.imbalance for r in btc_up]
    btc_down_imbalances = [r.imbalance for r in btc_down]

    if btc_up_imbalances:
        avg_imb_when_btc_up = statistics.mean(btc_up_imbalances)
        up_heavy_when_btc_up = len([i for i in btc_up_imbalances if i > 10])
        down_heavy_when_btc_up = len([i for i in btc_up_imbalances if i < -10])

        print(f"\n  When BTC went UP ({len(btc_up)} markets):")
        print(f"    Average imbalance: {avg_imb_when_btc_up:+.1f} shares")
        print(f"    UP heavy: {up_heavy_when_btc_up} ({up_heavy_when_btc_up/len(btc_up)*100:.1f}%)")
        print(f"    DOWN heavy: {down_heavy_when_btc_up} ({down_heavy_when_btc_up/len(btc_up)*100:.1f}%)")

    if btc_down_imbalances:
        avg_imb_when_btc_down = statistics.mean(btc_down_imbalances)
        up_heavy_when_btc_down = len([i for i in btc_down_imbalances if i > 10])
        down_heavy_when_btc_down = len([i for i in btc_down_imbalances if i < -10])

        print(f"\n  When BTC went DOWN ({len(btc_down)} markets):")
        print(f"    Average imbalance: {avg_imb_when_btc_down:+.1f} shares")
        print(f"    UP heavy: {up_heavy_when_btc_down} ({up_heavy_when_btc_down/len(btc_down)*100:.1f}%)")
        print(f"    DOWN heavy: {down_heavy_when_btc_down} ({down_heavy_when_btc_down/len(btc_down)*100:.1f}%)")

    # === WIN/LOSS ANALYSIS ===
    print(f"\n{'─'*70}")
    print("IMBALANCE WIN/LOSS ANALYSIS")
    print(f"{'─'*70}")

    # When UP heavy, did BTC go UP?
    if up_heavy:
        up_heavy_wins = len([r for r in up_heavy if r.btc_direction == "UP"])
        print(f"\n  When UP Heavy ({len(up_heavy)} markets):")
        print(f"    BTC went UP (WIN): {up_heavy_wins} ({up_heavy_wins/len(up_heavy)*100:.1f}%)")
        print(f"    BTC went DOWN (LOSS): {len(up_heavy)-up_heavy_wins} ({(len(up_heavy)-up_heavy_wins)/len(up_heavy)*100:.1f}%)")

    if down_heavy:
        down_heavy_wins = len([r for r in down_heavy if r.btc_direction == "DOWN"])
        print(f"\n  When DOWN Heavy ({len(down_heavy)} markets):")
        print(f"    BTC went DOWN (WIN): {down_heavy_wins} ({down_heavy_wins/len(down_heavy)*100:.1f}%)")
        print(f"    BTC went UP (LOSS): {len(down_heavy)-down_heavy_wins} ({(len(down_heavy)-down_heavy_wins)/len(down_heavy)*100:.1f}%)")

    # === PROFIT ESTIMATION ===
    print(f"\n{'─'*70}")
    print("ESTIMATED PROFIT FROM IMBALANCES")
    print(f"{'─'*70}")

    total_profit = 0

    for r in active:
        if r.up_shares == 0 or r.down_shares == 0:
            continue

        avg_up = r.up_cost / r.up_shares if r.up_shares > 0 else 0
        avg_down = r.down_cost / r.down_shares if r.down_shares > 0 else 0

        # Matched pairs profit/loss
        matched = min(r.up_shares, r.down_shares)
        pair_cost = avg_up + avg_down
        matched_pnl = matched * (1.0 - pair_cost)

        # Unmatched position profit/loss
        unmatched = abs(r.imbalance)
        if r.imbalance > 0:  # UP heavy
            # If BTC went UP, unmatched UP wins
            if r.btc_direction == "UP":
                unmatched_pnl = unmatched * (1.0 - avg_up)
            else:
                unmatched_pnl = unmatched * (-avg_up)
        else:  # DOWN heavy
            # If BTC went DOWN, unmatched DOWN wins
            if r.btc_direction == "DOWN":
                unmatched_pnl = unmatched * (1.0 - avg_down)
            else:
                unmatched_pnl = unmatched * (-avg_down)

        market_pnl = matched_pnl + unmatched_pnl
        total_profit += market_pnl

    print(f"\n  Total estimated profit: ${total_profit:+.2f}")
    print(f"  Avg profit per market: ${total_profit/len(active):+.2f}")

    # === PATTERN DETECTION ===
    print(f"\n{'─'*70}")
    print("PATTERN DETECTION")
    print(f"{'─'*70}")

    # Check if imbalances follow a predictable pattern
    # Hypothesis 1: Random (grid fills from both sides)
    # Hypothesis 2: Follows price direction (more fills on trending side)
    # Hypothesis 3: Counter-trend (more fills on cheap side)

    # Calculate correlation coefficient
    if len(active) >= 5:
        btc_changes = [r.btc_change_pct for r in active if r.btc_direction]
        imbalances = [r.imbalance for r in active if r.btc_direction]

        if btc_changes and imbalances:
            mean_btc = statistics.mean(btc_changes)
            mean_imb = statistics.mean(imbalances)

            numerator = sum((b - mean_btc) * (i - mean_imb) for b, i in zip(btc_changes, imbalances))

            std_btc = statistics.stdev(btc_changes) if len(btc_changes) > 1 else 1
            std_imb = statistics.stdev(imbalances) if len(imbalances) > 1 else 1

            if std_btc > 0 and std_imb > 0:
                correlation = numerator / (len(btc_changes) * std_btc * std_imb)
            else:
                correlation = 0

            print(f"\n  Correlation (BTC change % vs Imbalance): {correlation:.3f}")

            if correlation > 0.3:
                print("  → POSITIVE correlation: Imbalances FOLLOW price direction")
                print("    (More UP shares when BTC goes UP, more DOWN when BTC goes DOWN)")
            elif correlation < -0.3:
                print("  → NEGATIVE correlation: Imbalances are COUNTER-TREND")
                print("    (More DOWN shares when BTC goes UP, more UP when BTC goes DOWN)")
            else:
                print("  → WEAK/NO correlation: Imbalances appear RANDOM")
                print("    (Grid fills from both sides regardless of direction)")

    # === TIMING ANALYSIS ===
    print(f"\n{'─'*70}")
    print("TIMING PATTERN (Do imbalances build over time?)")
    print(f"{'─'*70}")

    # Sort by market start time
    sorted_results = sorted(active, key=lambda x: x.market_start)

    # Look at imbalance magnitude over time
    first_half = sorted_results[:len(sorted_results)//2]
    second_half = sorted_results[len(sorted_results)//2:]

    if first_half and second_half:
        avg_imb_first = statistics.mean([abs(r.imbalance) for r in first_half])
        avg_imb_second = statistics.mean([abs(r.imbalance) for r in second_half])

        print(f"\n  First half avg |imbalance|: {avg_imb_first:.1f} shares")
        print(f"  Second half avg |imbalance|: {avg_imb_second:.1f} shares")

        if avg_imb_second > avg_imb_first * 1.2:
            print("  → Imbalances GROW over session (fatigue or intentional)")
        elif avg_imb_first > avg_imb_second * 1.2:
            print("  → Imbalances SHRINK over session (learning or hedging)")
        else:
            print("  → Imbalances STABLE throughout session")


def save_results_csv(all_results: List[MarketAnalysis], filename: str):
    """Save results to CSV for further analysis."""
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'slug', 'market_start', 'market_end',
            'up_shares', 'down_shares', 'imbalance', 'imbalance_pct', 'imbalance_direction',
            'trade_count', 'pair_cost',
            'btc_open', 'btc_close', 'btc_change_pct', 'btc_direction',
            'imbalance_matches_btc'
        ])

        for r in all_results:
            writer.writerow([
                r.slug, r.market_start.isoformat(), r.market_end.isoformat(),
                r.up_shares, r.down_shares, r.imbalance, r.imbalance_pct, r.imbalance_direction,
                r.trade_count, r.pair_cost,
                r.btc_open, r.btc_close, r.btc_change_pct, r.btc_direction,
                r.imbalance_matches_direction
            ])

    print(f"\nSaved to: {filename}")


def main():
    print("=" * 70)
    print("GABAGOOL22 IMBALANCE vs BINANCE PRICE CORRELATION ANALYSIS")
    print("=" * 70)
    print(f"Wallet: {WALLET}")
    print(f"Asset: BTC 15-min markets only")

    # Time ranges (EST)
    ranges = [
        (
            datetime(2026, 1, 9, 2, 45, 0, tzinfo=ET),
            datetime(2026, 1, 10, 1, 45, 0, tzinfo=ET),
            "Range 1: Jan 9 02:45 - Jan 10 01:45 EST"
        ),
        (
            datetime(2026, 1, 7, 2, 30, 0, tzinfo=ET),
            datetime(2026, 1, 8, 3, 15, 0, tzinfo=ET),
            "Range 2: Jan 7 02:30 - Jan 8 03:15 EST"
        ),
    ]

    all_results = []

    for start_time, end_time, range_name in ranges:
        results = analyze_range(start_time, end_time, range_name)
        all_results.extend(results)
        print_correlation_analysis(results, range_name)

    # Combined analysis
    print(f"\n{'#'*70}")
    print("COMBINED ANALYSIS (BOTH RANGES)")
    print(f"{'#'*70}")
    print_correlation_analysis(all_results, "All Markets Combined")

    # Save to CSV
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"research/gabagool_imbalance_analysis_{timestamp}.csv"
    save_results_csv(all_results, csv_filename)

    # Final summary
    print(f"\n{'='*70}")
    print("KEY FINDINGS")
    print(f"{'='*70}")

    active = [r for r in all_results if r.trade_count > 0]

    if active:
        # Calculate overall correlation
        btc_changes = [r.btc_change_pct for r in active if r.btc_direction]
        imbalances = [r.imbalance for r in active if r.btc_direction]

        if btc_changes and imbalances and len(btc_changes) > 1:
            mean_btc = statistics.mean(btc_changes)
            mean_imb = statistics.mean(imbalances)
            numerator = sum((b - mean_btc) * (i - mean_imb) for b, i in zip(btc_changes, imbalances))
            std_btc = statistics.stdev(btc_changes)
            std_imb = statistics.stdev(imbalances)

            if std_btc > 0 and std_imb > 0:
                correlation = numerator / (len(btc_changes) * std_btc * std_imb)
            else:
                correlation = 0

            print(f"\n  1. CORRELATION COEFFICIENT: {correlation:.3f}")

            if abs(correlation) < 0.2:
                print("     → RANDOM: Imbalances are NOT correlated with BTC direction")
                print("     → Grid fills from both sides regardless of price movement")
            elif correlation > 0.2:
                print("     → TREND-FOLLOWING: Imbalances follow BTC price direction")
                print("     → More fills accumulate on the trending side")
            else:
                print("     → COUNTER-TREND: Imbalances are opposite to BTC direction")
                print("     → More fills accumulate on the cheap (counter-trend) side")

        # Imbalance win rate
        up_heavy = [r for r in active if r.imbalance > 10]
        down_heavy = [r for r in active if r.imbalance < -10]

        up_heavy_wins = len([r for r in up_heavy if r.btc_direction == "UP"])
        down_heavy_wins = len([r for r in down_heavy if r.btc_direction == "DOWN"])

        total_imbalanced = len(up_heavy) + len(down_heavy)
        total_wins = up_heavy_wins + down_heavy_wins

        if total_imbalanced > 0:
            win_rate = total_wins / total_imbalanced * 100
            print(f"\n  2. IMBALANCE WIN RATE: {win_rate:.1f}%")
            print(f"     ({total_wins}/{total_imbalanced} imbalanced markets won)")

            if win_rate > 55:
                print("     → Gabagool's imbalances are PREDICTIVE (or lucky)")
            elif win_rate < 45:
                print("     → Gabagool's imbalances are LOSING (random noise)")
            else:
                print("     → Imbalances are near 50/50 (random/grid-driven)")


if __name__ == "__main__":
    main()
