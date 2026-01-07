#!/usr/bin/env python3
"""
Correct historical CSV data for wrong winners.

This script:
1. Corrects 9 markets in paper_trades_accumulation.csv where wrong winners were recorded
2. Verifies paper_trades_directional.csv has correct winners
3. Recalculates PNL and cascades balance_after corrections
"""

import pandas as pd
from datetime import datetime

# Actual winners from Polymarket API (verified via outcomePrices)
ACTUAL_WINNERS = {
    "1766340900": "UP",
    "1766341800": "UP",
    "1766343600": "DOWN",
    "1766344500": "DOWN",
    "1766350800": "UP",
    "1766375100": "UP",
    "1766378700": "DOWN",
    "1766379600": "DOWN",
    "1766380500": "DOWN",
}


def calc_pnl(up_size: float, up_avg: float, down_size: float, down_avg: float, winner: str) -> float:
    """Calculate PNL given position and winner."""
    total_cost = (up_size * up_avg) + (down_size * down_avg)
    payout = up_size if winner == "UP" else down_size
    return payout - total_cost


def correct_csv(filepath: str, csv_name: str) -> dict:
    """
    Correct a CSV file with wrong winners.

    Returns dict with correction stats.
    """
    print(f"\n{'='*60}")
    print(f"  {csv_name.upper()} CSV CORRECTIONS")
    print(f"{'='*60}")

    # Load CSV
    df = pd.read_csv(filepath)
    original_len = len(df)

    # Get original final balance
    original_final_balance = df.iloc[-1]['balance_after']

    # Find resolution rows for our markets
    corrections_made = []
    total_pnl_delta = 0.0

    for market_ts, actual_winner in ACTUAL_WINNERS.items():
        # Find RESOLUTION row for this market
        mask = (
            df['market_slug'].str.contains(market_ts, na=False) &
            (df['event_type'] == 'RESOLUTION')
        )

        matching_rows = df[mask]

        if len(matching_rows) == 0:
            print(f"  {market_ts}: Not found in {csv_name}")
            continue

        idx = matching_rows.index[0]
        row = df.loc[idx]

        recorded_winner = row['trade_side']
        recorded_pnl = row['pnl_realized']

        # Get position data
        up_size = row['pos_up_size']
        up_avg = row['pos_up_avg_price']
        down_size = row['pos_down_size']
        down_avg = row['pos_down_avg_price']

        # Calculate correct PNL
        correct_pnl = calc_pnl(up_size, up_avg, down_size, down_avg, actual_winner)
        pnl_delta = correct_pnl - recorded_pnl

        # Check if correction needed
        winner_wrong = recorded_winner != actual_winner
        pnl_wrong = abs(pnl_delta) > 0.01

        if winner_wrong or pnl_wrong:
            # Apply correction
            df.loc[idx, 'trade_side'] = actual_winner
            df.loc[idx, 'pnl_realized'] = round(correct_pnl, 4)

            corrections_made.append({
                'market': market_ts,
                'idx': idx,
                'old_winner': recorded_winner,
                'new_winner': actual_winner,
                'old_pnl': recorded_pnl,
                'new_pnl': correct_pnl,
                'delta': pnl_delta,
            })

            total_pnl_delta += pnl_delta

            status = "WINNER FIXED" if winner_wrong else "PNL ADJUSTED"
            print(f"  {market_ts}: {recorded_winner} -> {actual_winner}, "
                  f"PNL: ${recorded_pnl:+.2f} -> ${correct_pnl:+.2f} (delta: ${pnl_delta:+.2f}) [{status}]")
        else:
            print(f"  {market_ts}: {actual_winner} (correct)")

    # Cascade balance_after if corrections were made
    if corrections_made:
        print(f"\nCascading balance_after corrections...")

        # Sort corrections by index to process in order
        corrections_made.sort(key=lambda x: x['idx'])
        first_correction_idx = corrections_made[0]['idx']

        # Calculate cumulative delta and apply to all subsequent rows
        cumulative_delta = 0.0
        correction_map = {c['idx']: c['delta'] for c in corrections_made}

        for i in range(first_correction_idx, len(df)):
            # Add any new delta at this index
            if i in correction_map:
                cumulative_delta += correction_map[i]

            # Apply cumulative delta to balance_after
            if cumulative_delta != 0:
                old_balance = df.loc[i, 'balance_after']
                new_balance = old_balance + cumulative_delta
                df.loc[i, 'balance_after'] = round(new_balance, 2)

        rows_updated = len(df) - first_correction_idx
        print(f"  Updated {rows_updated} rows with cumulative delta: ${cumulative_delta:+.2f}")

    # Get new final balance
    new_final_balance = df.iloc[-1]['balance_after']

    # Save corrected CSV
    df.to_csv(filepath, index=False)

    print(f"\n  Summary:")
    print(f"    Corrections made: {len(corrections_made)}")
    print(f"    Total PNL delta: ${total_pnl_delta:+.2f}")
    print(f"    Final balance: ${original_final_balance:.2f} -> ${new_final_balance:.2f}")
    print(f"    CSV saved: {filepath}")

    return {
        'corrections': len(corrections_made),
        'pnl_delta': total_pnl_delta,
        'old_balance': original_final_balance,
        'new_balance': new_final_balance,
    }


def main():
    print("\n" + "="*60)
    print("  CSV WINNER CORRECTION SCRIPT")
    print("="*60)
    print(f"  Timestamp: {datetime.now().isoformat()}")
    print(f"  Markets to verify: {len(ACTUAL_WINNERS)}")

    # Correct accumulation CSV
    accum_stats = correct_csv(
        'paper_trades_accumulation.csv',
        'Accumulation'
    )

    # Verify/correct directional CSV
    dir_stats = correct_csv(
        'paper_trades_directional.csv',
        'Directional'
    )

    # Final summary
    print("\n" + "="*60)
    print("  FINAL SUMMARY")
    print("="*60)
    print(f"\n  Accumulation:")
    print(f"    Corrections: {accum_stats['corrections']}")
    print(f"    PNL change: ${accum_stats['pnl_delta']:+.2f}")
    print(f"    Balance: ${accum_stats['old_balance']:.2f} -> ${accum_stats['new_balance']:.2f}")

    print(f"\n  Directional:")
    print(f"    Corrections: {dir_stats['corrections']}")
    print(f"    PNL change: ${dir_stats['pnl_delta']:+.2f}")
    print(f"    Balance: ${dir_stats['old_balance']:.2f} -> ${dir_stats['new_balance']:.2f}")

    print("\n" + "="*60)
    print("  DONE")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
