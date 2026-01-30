#!/usr/bin/env python3
"""
Quick parameter sweep for OU threshold tuning.
Tests different base_threshold, steepness, and min_threshold values.
"""

import subprocess
import re

# Test configurations
CONFIGS = [
    # (base_threshold, steepness, min_threshold, description)
    (0.02, 1.5, 0.005, "baseline"),           # Current
    (0.025, 1.5, 0.005, "base=0.025"),         # Raised base
    (0.03, 1.5, 0.005, "base=0.03"),           # Higher base
    (0.02, 2.5, 0.005, "steep=2.5"),           # Sharper sigmoid
    (0.02, 3.0, 0.005, "steep=3.0"),           # Even sharper
    (0.02, 1.5, 0.015, "min=0.015"),           # Higher floor
    (0.025, 2.0, 0.015, "combined"),           # Combined best guess
]

# Path to backtest file
BACKTEST_FILE = "research/enhanced_spike_backtest.py"

def update_params(base, steepness, min_thresh):
    """Update OU parameters in backtest file."""
    with open(BACKTEST_FILE, 'r') as f:
        content = f.read()

    # Replace parameters
    content = re.sub(r'OU_BASE_THRESHOLD = [\d.]+', f'OU_BASE_THRESHOLD = {base}', content)
    content = re.sub(r'OU_SIGMOID_STEEPNESS = [\d.]+', f'OU_SIGMOID_STEEPNESS = {steepness}', content)
    content = re.sub(r'OU_MIN_THRESHOLD = [\d.]+', f'OU_MIN_THRESHOLD = {min_thresh}', content)

    with open(BACKTEST_FILE, 'w') as f:
        f.write(content)

def run_backtest():
    """Run backtest and extract key results."""
    result = subprocess.run(
        ['python', BACKTEST_FILE, '--threshold-method', 'ou'],
        capture_output=True, text=True, timeout=900
    )
    output = result.stdout + result.stderr

    # Extract spike count
    spikes_match = re.search(r'Found ([\d,]+) spikes', output)
    spikes = int(spikes_match.group(1).replace(',', '')) if spikes_match else 0

    # Extract best spike result (look for spike rows in results)
    best_pnl_hr = -999
    best_config = ""
    for line in output.split('\n'):
        if line.startswith('spike') and '$/hr' not in line:
            parts = line.split()
            if len(parts) >= 8:
                try:
                    # Parse: spike  7%  OFF  149  $  51.28  $  0.74
                    pnl_hr_idx = line.find('$/hr=')
                    if pnl_hr_idx > 0:
                        pnl_str = line[pnl_hr_idx+5:].split()[0].replace('$', '')
                        pnl_hr = float(pnl_str)
                        if pnl_hr > best_pnl_hr:
                            best_pnl_hr = pnl_hr
                            sl = parts[1]
                            cyc = parts[3]
                            best_config = f"SL={sl},Cyc={cyc}"
                except:
                    pass

    # Parse from summary section
    for line in output.split('\n'):
        if 'spike' in line and '$' in line and 'Strategy' not in line:
            parts = line.split()
            try:
                # Format: spike  7%  OFF  226  $  128.00  $  1.84  ...
                if len(parts) >= 7:
                    pnl_hr = float(parts[6])
                    if pnl_hr > best_pnl_hr:
                        best_pnl_hr = pnl_hr
                        best_config = f"SL={parts[1]},Cyc={parts[2]}"
            except:
                pass

    return spikes, best_pnl_hr, best_config

def main():
    print("=" * 80)
    print("OU PARAMETER SWEEP")
    print("=" * 80)
    print()
    print(f"{'Config':<20} {'Base':>8} {'Steep':>8} {'Min':>8} {'Spikes':>10} {'$/hr':>8} {'Best Config':<20}")
    print("-" * 90)

    results = []
    for base, steep, min_t, desc in CONFIGS:
        print(f"Testing {desc}...", end=" ", flush=True)
        update_params(base, steep, min_t)

        try:
            spikes, pnl_hr, config = run_backtest()
            results.append((desc, base, steep, min_t, spikes, pnl_hr, config))
            print(f"Done: {spikes:,} spikes, ${pnl_hr:.2f}/hr")
        except Exception as e:
            print(f"Error: {e}")
            results.append((desc, base, steep, min_t, 0, -999, "ERROR"))

    # Print summary
    print()
    print("=" * 90)
    print("SUMMARY")
    print("=" * 90)
    print(f"{'Config':<20} {'Base':>8} {'Steep':>8} {'Min':>8} {'Spikes':>10} {'$/hr':>8} {'Best Config':<20}")
    print("-" * 90)

    for desc, base, steep, min_t, spikes, pnl_hr, config in sorted(results, key=lambda x: -x[5]):
        print(f"{desc:<20} {base:>8.3f} {steep:>8.1f} {min_t:>8.3f} {spikes:>10,} {pnl_hr:>8.2f} {config:<20}")

    # Restore baseline
    update_params(0.02, 1.5, 0.005)
    print()
    print("Restored baseline parameters.")

if __name__ == "__main__":
    main()
