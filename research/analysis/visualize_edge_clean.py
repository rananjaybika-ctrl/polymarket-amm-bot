#!/usr/bin/env python3
"""
Clean visualization of the velocity edge.
"""

import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# Read the data
timeseries = pd.read_csv('calc_velocity_timeseries_20260110_143519.csv')
cycles = pd.read_csv('calc_velocity_sim_20260110_143519.csv')

# Convert timestamps
timeseries['datetime'] = pd.to_datetime(timeseries['timestamp'], unit='s')
cycles['datetime'] = pd.to_datetime(cycles['timestamp'], unit='s')

# Use market where BTC crosses strike MULTIPLE times for better visualization
target_market = 'btc-updown-15m-1768068000'  # 18 crossings! 21 above/45 below, 2 cycles
market_ts = timeseries[timeseries['market_slug'] == target_market].copy()
market_cycles = cycles[cycles['market_slug'] == target_market].copy()

print(f"Market: {target_market}")
print(f"Samples: {len(market_ts)}, Cycles: {len(market_cycles)}")
print(f"Max velocity: {market_ts['velocity_bps'].abs().max():.4f} bps")

# Reset index for plotting
market_ts = market_ts.reset_index(drop=True)
x = range(len(market_ts))
time_labels = market_ts['datetime'].dt.strftime('%H:%M:%S')

# Create figure
fig, axes = plt.subplots(4, 1, figsize=(16, 12))
fig.suptitle('THE VELOCITY EDGE EXPLAINED\nMarket: btc-updown-15m-1768086000',
             fontsize=16, fontweight='bold', y=0.98)

# ===== Panel 1: BTC Price =====
ax1 = axes[0]
btc = market_ts['btc_price'].values
strike = btc[0]

ax1.plot(x, btc, 'b-', linewidth=2, label='BTC Price')
ax1.axhline(y=strike, color='black', linestyle='--', linewidth=2, label=f'Strike: ${strike:,.0f}')
ax1.fill_between(x, strike, btc, where=btc > strike, alpha=0.3, color='green')
ax1.fill_between(x, strike, btc, where=btc < strike, alpha=0.3, color='red')

ax1.set_ylabel('BTC Price ($)', fontsize=11)
ax1.set_title('① BTC Price vs Strike - Determines which side wins', fontsize=11, loc='left')
ax1.legend(loc='upper right')
ax1.grid(True, alpha=0.3)
ax1.set_xlim(0, len(x)-1)

# Add annotations
ax1.annotate('BTC ABOVE strike\n→ UP wins', xy=(10, btc.max()-20), fontsize=10,
             color='darkgreen', fontweight='bold')
ax1.annotate('BTC BELOW strike\n→ DOWN wins', xy=(50, btc.min()+20), fontsize=10,
             color='darkred', fontweight='bold')

# ===== Panel 2: Velocity Signal =====
ax2 = axes[1]
vel = market_ts['velocity_bps'].values

# Color bars by direction
colors = ['green' if v > 0 else 'red' for v in vel]
ax2.bar(x, vel, color=colors, alpha=0.7, width=1)
ax2.axhline(y=0.05, color='orange', linestyle='--', linewidth=2, label='Threshold +0.05 bps')
ax2.axhline(y=-0.05, color='orange', linestyle='--', linewidth=2)
ax2.axhline(y=0, color='gray', linestyle='-', linewidth=1)

# Highlight signal zones
signal_mask = np.abs(vel) > 0.05
for i in range(len(x)):
    if signal_mask[i]:
        ax2.axvspan(i-0.5, i+0.5, alpha=0.3, color='yellow')

ax2.set_ylabel('Velocity (bps/sec)', fontsize=11)
ax2.set_title('② Velocity Signal - Rate of BTC price change (our edge!)', fontsize=11, loc='left')
ax2.legend(loc='upper right')
ax2.grid(True, alpha=0.3)
ax2.set_ylim(-0.15, 0.15)
ax2.set_xlim(0, len(x)-1)

# Annotations
ax2.annotate('⚡ SIGNAL!\nVelocity > threshold\n→ Price moving fast\n→ Pull/Wait',
             xy=(35, 0.10), fontsize=9, ha='center',
             bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.8))
ax2.annotate('+velocity = BTC rising\n(good for UP)', xy=(5, 0.08), fontsize=9, color='green')
ax2.annotate('-velocity = BTC falling\n(good for DOWN)', xy=(5, -0.12), fontsize=9, color='red')

# ===== Panel 3: UP/DOWN Prices =====
ax3 = axes[2]
ax3.plot(x, market_ts['up_ask'], 'g-', linewidth=2.5, label='UP Price', marker='o', markersize=3)
ax3.plot(x, market_ts['down_ask'], 'r-', linewidth=2.5, label='DOWN Price', marker='o', markersize=3)
ax3.axhline(y=0.5, color='gray', linestyle=':', alpha=0.5)

ax3.set_ylabel('Option Price ($)', fontsize=11)
ax3.set_title('③ Polymarket Prices - Follow BTC with lag (our opportunity!)', fontsize=11, loc='left')
ax3.legend(loc='upper right')
ax3.grid(True, alpha=0.3)
ax3.set_ylim(0, 1)
ax3.set_xlim(0, len(x)-1)

# Mark entry points from cycles
for _, cycle in market_cycles.iterrows():
    # Find closest timestamp
    cycle_ts = cycle['timestamp']
    idx = (market_ts['timestamp'] - cycle_ts).abs().idxmin()
    if idx < len(x):
        entry_side = cycle['entry_side']
        entry_price = cycle['entry_price']
        hedge_price = cycle['hedge_price']
        profit = cycle['profit']

        color = 'green' if entry_side == 'UP' else 'red'
        ax3.axvline(x=idx, color='gold', linestyle='-', linewidth=3, alpha=0.7)
        ax3.scatter([idx], [entry_price], s=200, c='gold', edgecolors='black',
                    zorder=10, marker='*')
        ax3.annotate(f'ENTRY\n{entry_side} @ ${entry_price:.2f}',
                     xy=(idx, entry_price), xytext=(5, 20), textcoords='offset points',
                     fontsize=8, fontweight='bold',
                     bbox=dict(boxstyle='round', facecolor='yellow', alpha=0.9))

# ===== Panel 4: Pair Cost & Profit =====
ax4 = axes[3]
pair_cost = market_ts['up_ask'] + market_ts['down_ask']
profit_potential = 1.0 - pair_cost

ax4.fill_between(x, 0, profit_potential * 100, alpha=0.3, color='green', label='Profit potential (%)')
ax4.plot(x, profit_potential * 100, 'g-', linewidth=2)
ax4.axhline(y=2, color='blue', linestyle='--', linewidth=2, label='Target: 2% profit')
ax4.axhline(y=0, color='red', linestyle='-', linewidth=1)

ax4.set_ylabel('Profit Potential (%)', fontsize=11)
ax4.set_xlabel('Time (seconds into market)', fontsize=11)
ax4.set_title('④ Profit Opportunity - When pair cost < $1.00', fontsize=11, loc='left')
ax4.legend(loc='upper right')
ax4.grid(True, alpha=0.3)
ax4.set_ylim(-5, 25)
ax4.set_xlim(0, len(x)-1)

# Mark completed cycles
for _, cycle in market_cycles.iterrows():
    cycle_ts = cycle['timestamp']
    idx = (market_ts['timestamp'] - cycle_ts).abs().idxmin()
    if idx < len(x):
        profit = cycle['profit'] * 100
        ax4.scatter([idx], [profit], s=200, c='gold', edgecolors='black', zorder=10, marker='*')
        ax4.annotate(f'+{profit:.1f}%', xy=(idx, profit), xytext=(5, 10),
                     textcoords='offset points', fontsize=10, fontweight='bold', color='darkgreen')

plt.tight_layout()
plt.subplots_adjust(top=0.93)
plt.savefig('velocity_edge_explained.png', dpi=150, bbox_inches='tight', facecolor='white')
print("\nSaved: velocity_edge_explained.png")

# ===== Create How It Works Diagram =====
fig2, ax = plt.subplots(figsize=(14, 8))
ax.set_xlim(0, 10)
ax.set_ylim(0, 10)
ax.axis('off')
ax.set_title('HOW THE VELOCITY EDGE WORKS', fontsize=18, fontweight='bold', pad=20)

# Draw flow
boxes = [
    (1, 8, 'BINANCE\nBTC Price', 'lightblue'),
    (4, 8, 'VELOCITY\nCALCULATION', 'lightyellow'),
    (7, 8, 'SIGNAL\nDETECTION', 'lightgreen'),
    (1, 5, 'POLYMARKET\nUP/DOWN Prices', 'lightcoral'),
    (4, 5, 'SPREAD\nANALYSIS', 'lightyellow'),
    (7, 5, 'TRADE\nEXECUTION', 'lightgreen'),
    (4, 2, 'PROFIT\n$0.02-0.07/cycle', 'gold'),
]

for x, y, text, color in boxes:
    rect = plt.Rectangle((x-0.8, y-0.6), 1.6, 1.2, facecolor=color, edgecolor='black', linewidth=2)
    ax.add_patch(rect)
    ax.text(x, y, text, ha='center', va='center', fontsize=10, fontweight='bold')

# Arrows
arrows = [
    ((1.8, 8), (3.2, 8), 'BTC moves'),
    ((4.8, 8), (6.2, 8), 'velocity > 0.05?'),
    ((1.8, 5), (3.2, 5), 'Prices lag'),
    ((4.8, 5), (6.2, 5), 'pair < $0.98?'),
    ((7, 7.4), (7, 5.6), 'Signal!'),
    ((7, 4.4), (4.8, 2.6), 'Execute'),
    ((4, 4.4), (4, 2.6), 'Check'),
]

for start, end, label in arrows:
    ax.annotate('', xy=end, xytext=start,
                arrowprops=dict(arrowstyle='->', color='black', lw=2))
    mid = ((start[0]+end[0])/2, (start[1]+end[1])/2 + 0.3)
    ax.text(mid[0], mid[1], label, fontsize=8, ha='center', style='italic')

# Add the edge explanation
edge_text = """
THE EDGE:
1. BTC price moves on Binance (fast, liquid)
2. We calculate velocity = rate of price change
3. Polymarket UP/DOWN prices LAG behind by 1-5 seconds
4. When velocity reverses → Polymarket prices will follow
5. We enter BEFORE the lag catches up → better price
6. Entry improvement: ~400 bps, Hedge improvement: ~600 bps
"""
ax.text(0.5, 1, edge_text, fontsize=11, verticalalignment='top',
        bbox=dict(boxstyle='round', facecolor='white', edgecolor='green', linewidth=2))

plt.savefig('velocity_edge_how_it_works.png', dpi=150, bbox_inches='tight', facecolor='white')
print("Saved: velocity_edge_how_it_works.png")

plt.show()
