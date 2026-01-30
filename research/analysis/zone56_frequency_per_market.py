#!/usr/bin/env python3
"""
Zone 5-6 Frequency Per Market

Question: Are there multiple Zone 5-6 signals in a single market?
If yes, cycling could multiply profits.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

MIN_VELOCITY = 0.50  # Zone 5-6
MIN_TIME = 120  # Only count signals with >2 min remaining


def analyze_market_signals(mdf):
    """Count Zone 5-6 signals in a single market."""

    # Filter to tradeable time (>120s remaining)
    tradeable = mdf[mdf['time_remaining_secs'] >= MIN_TIME]

    if len(tradeable) == 0:
        return None

    # Find all Zone 5-6 samples
    zone56_samples = tradeable[abs(tradeable['velocity_bps']) >= MIN_VELOCITY]

    # Count distinct "events" - a new event starts after velocity drops below threshold
    # Group consecutive Zone 5-6 samples into events
    events = []
    in_event = False
    event_start = None
    event_max_vel = 0
    event_samples = 0

    for idx, row in tradeable.iterrows():
        vel = abs(row['velocity_bps'])
        time_rem = row['time_remaining_secs']

        if vel >= MIN_VELOCITY:
            if not in_event:
                # Start new event
                in_event = True
                event_start = time_rem
                event_max_vel = vel
                event_samples = 1
            else:
                # Continue event
                event_max_vel = max(event_max_vel, vel)
                event_samples += 1
        else:
            if in_event:
                # End event
                events.append({
                    'start_time': event_start,
                    'end_time': time_rem,
                    'duration': event_start - time_rem,
                    'max_velocity': event_max_vel,
                    'samples': event_samples,
                })
                in_event = False
                event_max_vel = 0
                event_samples = 0

    # Don't forget last event if still in one
    if in_event:
        events.append({
            'start_time': event_start,
            'end_time': tradeable.iloc[-1]['time_remaining_secs'],
            'duration': event_start - tradeable.iloc[-1]['time_remaining_secs'],
            'max_velocity': event_max_vel,
            'samples': event_samples,
        })

    return {
        'total_samples': len(tradeable),
        'zone56_samples': len(zone56_samples),
        'zone56_pct': len(zone56_samples) / len(tradeable) * 100 if len(tradeable) > 0 else 0,
        'num_events': len(events),
        'events': events,
    }


def main():
    print("=" * 80)
    print("ZONE 5-6 FREQUENCY PER MARKET")
    print("=" * 80)

    # Load ALL observer data
    observer_dir = Path('/Users/rananjaybika/polymarket-amm-bot/research/observer')
    csv_files = sorted(observer_dir.glob('spread_capture_obs_*.csv'))

    print(f"\nLoading data from {len(csv_files)} files...")

    market_results = []

    for filepath in csv_files:
        try:
            df = pd.read_csv(filepath, on_bad_lines='skip')
            if df.empty:
                continue

            markets = df['market_slug'].unique()
            for slug in markets:
                mdf = df[df['market_slug'] == slug]
                if len(mdf) >= 2:
                    first = mdf.iloc[0]['time_remaining_secs']
                    last = mdf.iloc[-1]['time_remaining_secs']
                    if first >= 800 and last <= 60:
                        result = analyze_market_signals(mdf)
                        if result:
                            result['slug'] = slug
                            market_results.append(result)
        except Exception as e:
            continue

    print(f"Complete markets analyzed: {len(market_results)}")

    # Distribution of events per market
    print(f"\n{'='*80}")
    print("ZONE 5-6 EVENTS PER MARKET")
    print("=" * 80)

    event_counts = [r['num_events'] for r in market_results]

    print(f"\n  Distribution:")
    for n in range(0, max(event_counts) + 1):
        count = sum(1 for e in event_counts if e == n)
        pct = count / len(event_counts) * 100
        bar = "█" * int(pct / 2)
        print(f"    {n} events: {count:3} markets ({pct:5.1f}%) {bar}")

    print(f"\n  Summary:")
    print(f"    Markets with 0 events: {sum(1 for e in event_counts if e == 0)}")
    print(f"    Markets with 1 event:  {sum(1 for e in event_counts if e == 1)}")
    print(f"    Markets with 2+ events: {sum(1 for e in event_counts if e >= 2)}")
    print(f"    Markets with 3+ events: {sum(1 for e in event_counts if e >= 3)}")
    print(f"\n    Average events/market: {np.mean(event_counts):.2f}")
    print(f"    Max events in a market: {max(event_counts)}")

    # Analyze markets with multiple events
    multi_event_markets = [r for r in market_results if r['num_events'] >= 2]

    if multi_event_markets:
        print(f"\n{'='*80}")
        print(f"MARKETS WITH 2+ ZONE 5-6 EVENTS ({len(multi_event_markets)} markets)")
        print("=" * 80)

        # Show a few examples
        print(f"\n  Examples (first 10):")
        for r in multi_event_markets[:10]:
            print(f"\n    {r['slug'][:40]}...")
            print(f"      Events: {r['num_events']}")
            for i, e in enumerate(r['events']):
                print(f"        #{i+1}: {e['start_time']:.0f}s→{e['end_time']:.0f}s ({e['duration']:.0f}s) max_vel={e['max_velocity']:.2f}")

        # Gap analysis between events
        gaps = []
        for r in multi_event_markets:
            events = r['events']
            for i in range(1, len(events)):
                gap = events[i-1]['end_time'] - events[i]['start_time']
                gaps.append(gap)

        if gaps:
            print(f"\n  Gap between Zone 5-6 events:")
            print(f"    Average gap: {np.mean(gaps):.0f}s ({np.mean(gaps)/60:.1f} min)")
            print(f"    Min gap: {min(gaps):.0f}s")
            print(f"    Max gap: {max(gaps):.0f}s")

    # Event duration analysis
    print(f"\n{'='*80}")
    print("ZONE 5-6 EVENT DURATION")
    print("=" * 80)

    all_events = []
    for r in market_results:
        all_events.extend(r['events'])

    if all_events:
        durations = [e['duration'] for e in all_events]
        max_vels = [e['max_velocity'] for e in all_events]

        print(f"\n  Total Zone 5-6 events: {len(all_events)}")
        print(f"\n  Duration:")
        print(f"    Average: {np.mean(durations):.0f}s ({np.mean(durations)/60:.1f} min)")
        print(f"    Median: {np.median(durations):.0f}s")
        print(f"    Min: {min(durations):.0f}s")
        print(f"    Max: {max(durations):.0f}s")

        print(f"\n  Max velocity during event:")
        print(f"    Average: {np.mean(max_vels):.2f} bps")
        print(f"    Max: {max(max_vels):.2f} bps")

    # Cycling potential
    print(f"\n{'='*80}")
    print("CYCLING POTENTIAL")
    print("=" * 80)

    markets_1_event = sum(1 for e in event_counts if e == 1)
    markets_2plus = sum(1 for e in event_counts if e >= 2)
    total_extra_events = sum(max(0, e - 1) for e in event_counts)

    print(f"""
  Current strategy (1 entry per market):
    - Trades: {sum(1 for e in event_counts if e >= 1)} markets

  With cycling (multiple entries per market):
    - Markets with 2+ events: {markets_2plus} ({markets_2plus/len(market_results)*100:.0f}%)
    - Extra trades possible: {total_extra_events}
    - Potential trade increase: {total_extra_events / sum(1 for e in event_counts if e >= 1) * 100:.0f}%

  Recommendation:
""")

    if markets_2plus / len(market_results) > 0.3:
        print("    YES - Enable cycling! 30%+ markets have multiple opportunities.")
    elif markets_2plus / len(market_results) > 0.15:
        print("    MAYBE - Some markets have multiple opportunities, could help.")
    else:
        print("    NO - Few markets have multiple opportunities, cycling won't help much.")


if __name__ == "__main__":
    main()
