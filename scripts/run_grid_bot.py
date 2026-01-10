#!/usr/bin/env python3
"""
Grid Bot - Gabagool-style Two-Sided Grid Market Maker

Simplified entry point for running the grid maker strategy.
Uses run_paper_bot.py with grid_maker mode and sensible defaults.

Scaling Phases (from Gabagool analysis):
  Phase 1 (test):     10 shares, 500 max pos, $0.15-$0.85 range
  Phase 2 (validate): 12 shares, 800 max pos, $0.10-$0.90 range
  Phase 3 (scale):    15 shares, 1200 max pos, $0.08-$0.92 range
  Phase 4 (full):     20 shares, 2000 max pos, $0.05-$0.95 range

Usage:
    # Start with Phase 1 (conservative testing)
    python scripts/run_grid_bot.py --phase 1

    # Phase 4 (full Gabagool-style)
    python scripts/run_grid_bot.py --phase 4

    # Custom parameters
    python scripts/run_grid_bot.py --order-size 15 --max-position 1000

    # Live trading (CAUTION!)
    python scripts/run_grid_bot.py --phase 2 --live
"""

import argparse
import subprocess
import sys
import os

# Phase presets based on Gabagool Week 1-4 scaling
PHASE_PRESETS = {
    1: {
        "name": "Week 1 (Conservative)",
        "order_size": 5,           # Gabagool Week 1: 5-10 shares
        "max_position": 30,        # Conservative: 30 per side
        "max_imbalance": 15,       # Conservative: 15 max imbalance
        "min_price": 0.15,
        "max_price": 0.85,
        "description": "Week 1 Gabagool - 5 shares, tight limits"
    },
    2: {
        "name": "Week 2 (Validation)",
        "order_size": 8,           # Increase to 8 shares
        "max_position": 400,
        "max_imbalance": 200,
        "min_price": 0.12,
        "max_price": 0.88,
        "description": "Week 2 - proving profitability"
    },
    3: {
        "name": "Week 3 (Scaling)",
        "order_size": 12,
        "max_position": 800,
        "max_imbalance": 400,
        "min_price": 0.10,
        "max_price": 0.90,
        "description": "Week 3 - increasing volume"
    },
    4: {
        "name": "Week 4+ (Full Scale)",
        "order_size": 20,
        "max_position": 2000,
        "max_imbalance": 1000,
        "min_price": 0.05,
        "max_price": 0.95,
        "description": "Full Gabagool-style - maximum volume"
    },
}


def print_phase_info():
    """Print information about all phases."""
    print("\n" + "=" * 60)
    print("GRID BOT SCALING PHASES (from Gabagool analysis)")
    print("=" * 60)

    for phase, config in PHASE_PRESETS.items():
        print(f"\nPhase {phase}: {config['name']}")
        print(f"  {config['description']}")
        print(f"  Order size: {config['order_size']} shares")
        print(f"  Max position: {config['max_position']} shares/side")
        print(f"  Max imbalance: {config['max_imbalance']} shares")
        print(f"  Price range: ${config['min_price']:.2f} - ${config['max_price']:.2f}")
        num_levels = int((config['max_price'] - config['min_price']) / 0.01) + 1
        print(f"  Grid levels: ~{num_levels}")

    print("\n" + "=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description='Grid Bot - Gabagool-style Two-Sided Grid Market Maker',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_grid_bot.py --phase 1           # Phase 1 testing
  python scripts/run_grid_bot.py --phase 4           # Full Gabagool-style
  python scripts/run_grid_bot.py --phase 2 --live    # Live trading (CAUTION!)
  python scripts/run_grid_bot.py --phases            # Show all phase details
        """
    )

    # Phase selection
    parser.add_argument(
        '--phase', '-p',
        type=int,
        choices=[1, 2, 3, 4],
        default=1,
        help='Scaling phase (1=test, 2=validate, 3=scale, 4=full). Default: 1',
    )
    parser.add_argument(
        '--phases',
        action='store_true',
        help='Show detailed information about all phases and exit',
    )

    # Override phase parameters
    parser.add_argument(
        '--order-size',
        type=int,
        default=None,
        help='Override order size (shares per level)',
    )
    parser.add_argument(
        '--max-position',
        type=float,
        default=None,
        help='Override max position per side',
    )
    parser.add_argument(
        '--max-imbalance',
        type=float,
        default=None,
        help='Override max imbalance',
    )

    # Trading mode
    parser.add_argument(
        '--live',
        action='store_true',
        help='Enable LIVE trading (real money!). Default: paper trading',
    )

    # Duration
    parser.add_argument(
        '--duration', '-d',
        type=float,
        default=60,
        help='Duration in minutes (default: 60)',
    )
    parser.add_argument(
        '--end-time', '-e',
        type=str,
        default=None,
        help='End time (e.g., "14:00 EST")',
    )

    # Display
    parser.add_argument(
        '--live-display', '-l',
        action='store_true',
        help='Enable live terminal display',
    )
    parser.add_argument(
        '--quiet', '-q',
        action='store_true',
        help='Quiet mode - reduce log noise',
    )

    args = parser.parse_args()

    # Show phases info if requested
    if args.phases:
        print_phase_info()
        return

    # Get phase preset
    preset = PHASE_PRESETS[args.phase]

    # Apply overrides
    order_size = args.order_size if args.order_size else preset['order_size']
    max_position = args.max_position if args.max_position else preset['max_position']
    max_imbalance = args.max_imbalance if args.max_imbalance else preset['max_imbalance']

    # Build command
    cmd = [
        sys.executable,
        'scripts/run_paper_bot.py',
        '--accum-mode', 'grid_maker',
        '--grid-phase', str(args.phase),
    ]

    # Add overrides if specified
    if args.order_size:
        cmd.extend(['--grid-order-size', str(args.order_size)])
    if args.max_position:
        cmd.extend(['--grid-max-position', str(args.max_position)])
    if args.max_imbalance:
        cmd.extend(['--grid-max-imbalance', str(args.max_imbalance)])

    # Trading mode
    if args.live:
        cmd.extend(['--trading-mode', 'live'])

    # Duration
    if args.end_time:
        cmd.extend(['--end-time', args.end_time])
    else:
        cmd.extend(['--duration', str(args.duration)])

    # Display options
    if args.live_display:
        cmd.append('--live-display')
    if args.quiet:
        cmd.append('--quiet')

    # Print startup info
    print("\n" + "=" * 60)
    print("GRID BOT - Gabagool-style Market Maker")
    print("=" * 60)
    print(f"\nPhase: {args.phase} ({preset['name']})")
    print(f"Mode: {'LIVE TRADING' if args.live else 'Paper Trading'}")
    print(f"\nConfiguration:")
    print(f"  Order size: {order_size} shares")
    print(f"  Max position: {max_position} shares/side")
    print(f"  Max imbalance: {max_imbalance} shares")
    print(f"  Price range: ${preset['min_price']:.2f} - ${preset['max_price']:.2f}")
    num_levels = int((preset['max_price'] - preset['min_price']) / 0.01) + 1
    print(f"  Grid levels: ~{num_levels}")

    if args.live:
        print("\n" + "!" * 60)
        print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK!")
        print("!" * 60)
        response = input("\nType 'YES' to confirm live trading: ")
        if response != 'YES':
            print("Aborted.")
            return

    print(f"\nStarting bot...")
    print("-" * 60)

    # Run the bot
    try:
        subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    except KeyboardInterrupt:
        print("\nBot stopped by user")


if __name__ == '__main__':
    main()
