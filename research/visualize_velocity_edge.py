#!/usr/bin/env python3
"""
Visualize the velocity edge - BTC price, velocity, UP/DOWN prices, and trading signals.
"""

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import numpy as np

# Read the data
timeseries = pd.read_csv('calc_velocity_timeseries_20260110_143519.csv')
cycles = pd.read_csv('calc_velocity_sim_20260110_143519.csv')

# Convert timestamps
timeseries['datetime'] = pd.to_datetime(timeseries['timestamp'], unit='s')
cycles['datetime'] = pd.to_datetime(cycles['timestamp'], unit='s')

# Pick an interesting market with good price movement and completed cycles
# Find markets with completed cycles
markets_with_cycles = cycles['market_slug'].unique()
print(f"Markets with completed cycles: {len(markets_with_cycles)}")

# Pick a market that has multiple cycles and price movement
# Let's find one with interesting velocity
for market in markets_with_cycles[:5]:
    market_data = timeseries[timeseries['market_slug'] == market]
    market_cycles = cycles[cycles['market_slug'] == market]
    max_vel = market_data['velocity_bps'].abs().max()
    print(f"{market}: {len(market_data)} samples, {len(market_cycles)} cycles, max_vel={max_vel:.4f}")

# Select a market with good data - let's pick one with multiple cycles
target_market = 'btc-updown-15m-1768086000'  # Has 5 cycles based on earlier data

# Filter data for this market
market_ts = timeseries[timeseries['market_slug'] == target_market].copy()
market_cycles = cycles[cycles['market_slug'] == target_market].copy()

if len(market_ts) < 10:
    # Pick another market
    target_market = markets_with_cycles[3]
    market_ts = timeseries[timeseries['market_slug'] == target_market].copy()
    market_cycles = cycles[cycles['market_slug'] == target_market].copy()

print(f"\nVisualing market: {target_market}")
print(f"  Samples: {len(market_ts)}")
print(f"  Cycles: {len(market_cycles)}")

# Create figure with subplots
fig, axes = plt.subplots(4, 1, figsize=(16, 14), sharex=True)
fig.suptitle(f'Velocity Edge Visualization\n{target_market}', fontsize=14, fontweight='bold')

# Plot 1: BTC Price
ax1 = axes[0]
ax1.plot(market_ts['datetime'], market_ts['btc_price'], 'b-', linewidth=1.5, label='BTC Price')
ax1.set_ylabel('BTC Price ($)', fontsize=10)
ax1.legend(loc='upper right')
ax1.grid(True, alpha=0.3)

# Add strike price line (approximate - using first price as strike)
strike = market_ts['btc_price'].iloc[0]
ax1.axhline(y=strike, color='red', linestyle='--', alpha=0.7, label=f'Strike: ${strike:,.2f}')
ax1.fill_between(market_ts['datetime'], strike, market_ts['btc_price'],
                  where=market_ts['btc_price'] > strike, alpha=0.3, color='green', label='Above strike (UP wins)')
ax1.fill_between(market_ts['datetime'], strike, market_ts['btc_price'],
                  where=market_ts['btc_price'] < strike, alpha=0.3, color='red', label='Below strike (DOWN wins)')
ax1.legend(loc='upper right', fontsize=8)

# Plot 2: Velocity with threshold lines
ax2 = axes[1]
ax2.plot(market_ts['datetime'], market_ts['velocity_bps'], 'purple', linewidth=1, label='Velocity (bps/sec)')
ax2.axhline(y=0.05, color='orange', linestyle='--', alpha=0.8, label='Threshold +0.05')
ax2.axhline(y=-0.05, color='orange', linestyle='--', alpha=0.8, label='Threshold -0.05')
ax2.axhline(y=0.02, color='green', linestyle=':', alpha=0.8, label='Lower threshold ±0.02')
ax2.axhline(y=-0.02, color='green', linestyle=':', alpha=0.8)
ax2.axhline(y=0, color='gray', linestyle='-', alpha=0.5)
ax2.set_ylabel('Velocity (bps/sec)', fontsize=10)
ax2.set_ylim(-0.2, 0.2)
ax2.legend(loc='upper right', fontsize=8)
ax2.grid(True, alpha=0.3)

# Highlight when velocity exceeds threshold
vel_exceeds = market_ts['velocity_bps'].abs() > 0.05
ax2.fill_between(market_ts['datetime'], -0.2, 0.2, where=vel_exceeds, alpha=0.2, color='red', label='Signal!')

# Plot 3: UP and DOWN prices
ax3 = axes[2]
ax3.plot(market_ts['datetime'], market_ts['up_ask'], 'g-', linewidth=1.5, label='UP Ask', alpha=0.8)
ax3.plot(market_ts['datetime'], market_ts['down_ask'], 'r-', linewidth=1.5, label='DOWN Ask', alpha=0.8)
ax3.plot(market_ts['datetime'], market_ts['up_bid'], 'g--', linewidth=1, label='UP Bid', alpha=0.5)
ax3.plot(market_ts['datetime'], market_ts['down_bid'], 'r--', linewidth=1, label='DOWN Bid', alpha=0.5)
ax3.set_ylabel('Option Prices ($)', fontsize=10)
ax3.set_ylim(0, 1)
ax3.legend(loc='upper right', fontsize=8)
ax3.grid(True, alpha=0.3)

# Add cycle markers
for _, cycle in market_cycles.iterrows():
    cycle_time = cycle['datetime']
    entry_side = cycle['entry_side']
    entry_price = cycle['entry_price']
    hedge_price = cycle['hedge_price']
    profit = cycle['profit']

    # Entry marker
    color = 'green' if entry_side == 'UP' else 'red'
    ax3.axvline(x=cycle_time, color=color, linestyle='-', alpha=0.5, linewidth=2)
    ax3.annotate(f"Entry {entry_side}\n${entry_price:.2f}",
                 xy=(cycle_time, entry_price), fontsize=7, ha='right',
                 bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.7))

# Plot 4: Pair Cost / Profit visualization
ax4 = axes[3]
# Calculate running pair cost
market_ts['pair_cost'] = market_ts['up_ask'] + market_ts['down_ask']
market_ts['potential_profit'] = 1.0 - market_ts['pair_cost']

ax4.plot(market_ts['datetime'], market_ts['pair_cost'], 'b-', linewidth=1.5, label='Pair Cost (UP+DOWN)')
ax4.axhline(y=1.0, color='gray', linestyle='-', alpha=0.5)
ax4.axhline(y=0.98, color='green', linestyle='--', alpha=0.7, label='Target: $0.98 (2% profit)')
ax4.fill_between(market_ts['datetime'], 0.98, market_ts['pair_cost'],
                  where=market_ts['pair_cost'] < 0.98, alpha=0.3, color='green', label='Profitable zone')
ax4.set_ylabel('Pair Cost ($)', fontsize=10)
ax4.set_xlabel('Time', fontsize=10)
ax4.set_ylim(0.85, 1.05)
ax4.legend(loc='upper right', fontsize=8)
ax4.grid(True, alpha=0.3)

# Mark completed cycles with profit
for _, cycle in market_cycles.iterrows():
    cycle_time = cycle['datetime']
    pair_cost = cycle['pair_cost']
    profit = cycle['profit']
    ax4.scatter([cycle_time], [pair_cost], s=100, c='gold', edgecolors='black', zorder=5)
    ax4.annotate(f"+${profit:.2f}", xy=(cycle_time, pair_cost),
                 xytext=(5, 10), textcoords='offset points', fontsize=8, fontweight='bold',
                 color='darkgreen')

plt.tight_layout()
plt.savefig('velocity_edge_visualization.png', dpi=150, bbox_inches='tight')
print(f"\nSaved: velocity_edge_visualization.png")

# Also create a zoomed version showing signal detection
fig2, axes2 = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
fig2.suptitle('Velocity Signal Detection - How the Edge Works', fontsize=14, fontweight='bold')

# Find a period with actual velocity signal
high_vel_mask = market_ts['velocity_bps'].abs() > 0.02
if high_vel_mask.any():
    signal_idx = market_ts[high_vel_mask].index[0]
    # Get 60 seconds around the signal
    start_idx = max(0, signal_idx - 30)
    end_idx = min(len(market_ts), signal_idx + 30)
    zoom_data = market_ts.iloc[start_idx:end_idx]
else:
    zoom_data = market_ts.head(60)

ax1 = axes2[0]
ax1.plot(zoom_data['datetime'], zoom_data['btc_price'], 'b-', linewidth=2, marker='o', markersize=3)
ax1.set_ylabel('BTC Price ($)')
ax1.grid(True, alpha=0.3)
ax1.set_title('BTC Price Movement', fontsize=10)

ax2 = axes2[1]
ax2.bar(zoom_data['datetime'], zoom_data['velocity_bps'], width=0.0005,
        color=['green' if v > 0 else 'red' for v in zoom_data['velocity_bps']], alpha=0.7)
ax2.axhline(y=0.05, color='orange', linestyle='--', linewidth=2, label='Threshold 0.05')
ax2.axhline(y=-0.05, color='orange', linestyle='--', linewidth=2)
ax2.axhline(y=0, color='gray', linestyle='-')
ax2.set_ylabel('Velocity (bps/sec)')
ax2.legend()
ax2.grid(True, alpha=0.3)
ax2.set_title('Velocity Signal (rate of BTC price change)', fontsize=10)

ax3 = axes2[2]
ax3.plot(zoom_data['datetime'], zoom_data['up_ask'], 'g-', linewidth=2, label='UP', marker='s', markersize=4)
ax3.plot(zoom_data['datetime'], zoom_data['down_ask'], 'r-', linewidth=2, label='DOWN', marker='s', markersize=4)
ax3.set_ylabel('Option Prices ($)')
ax3.set_xlabel('Time')
ax3.legend()
ax3.grid(True, alpha=0.3)
ax3.set_title('Polymarket UP/DOWN Prices', fontsize=10)

plt.tight_layout()
plt.savefig('velocity_signal_zoom.png', dpi=150, bbox_inches='tight')
print(f"Saved: velocity_signal_zoom.png")

plt.show()
