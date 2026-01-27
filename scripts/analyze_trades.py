#!/usr/bin/env python3
"""
Daily Trade Analyzer - Automated analysis of paper trading results

Implements the TRADE_ANALYSIS_PLAYBOOK.md methodology:
- Win/loss rates and P&L metrics
- Statistical analysis (Sharpe, Sortino, CV)
- Pair cost analysis
- Unhedged shares analysis
- Strategy comparison

Usage:
    python scripts/analyze_trades.py                    # Today's trades
    python scripts/analyze_trades.py --date 2026-01-09  # Specific date
    python scripts/analyze_trades.py --all              # All available data
    python scripts/analyze_trades.py --strategy AGGRESSIVE
    python scripts/analyze_trades.py --compare          # Compare all strategies
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class StrategyMetrics:
    """Comprehensive metrics for a strategy."""
    name: str
    markets_resolved: int
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    avg_pnl: float
    max_win: float
    max_loss: float
    avg_win: float
    avg_loss: float
    # Statistical
    std_dev: float
    variance: float
    cv_pct: float  # Coefficient of variation
    skewness: float
    kurtosis: float
    sharpe: float
    sortino: float
    # Percentiles
    p5: float
    p25: float
    p50: float
    p75: float
    p95: float
    # Position metrics
    avg_pair_cost: float
    pair_cost_below_1: int
    pair_cost_total: int
    avg_unhedged: float
    max_unhedged: float
    fully_hedged_count: int
    # Time
    hours_analyzed: float


def load_trade_files(
    date: Optional[str] = None,
    strategy: Optional[str] = None,
    all_data: bool = False
) -> pd.DataFrame:
    """Load paper trade CSV files based on filters."""

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Find all CSV files
    patterns = [
        os.path.join(base_dir, "paper_trades_*.csv"),
        os.path.join(base_dir, "web", "paper_trades_*.csv"),
    ]

    all_files = []
    for pattern in patterns:
        all_files.extend(glob.glob(pattern))

    # Filter by date if specified
    if date and not all_data:
        all_files = [f for f in all_files if date in f]
    elif not all_data:
        # Default to today
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Also check for recent files (last 3 days)
        recent_dates = [
            (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(3)
        ]
        all_files = [f for f in all_files if any(d in f for d in recent_dates)]

    # Filter by strategy if specified
    if strategy:
        strategy_upper = strategy.upper()
        all_files = [f for f in all_files if strategy_upper in f.upper()]

    if not all_files:
        print(f"No trade files found matching criteria")
        print(f"  Date filter: {date if date else 'recent'}")
        print(f"  Strategy filter: {strategy if strategy else 'all'}")
        return pd.DataFrame()

    # Load and concatenate
    dfs = []
    for f in all_files:
        try:
            df = pd.read_csv(f)
            df['source_file'] = os.path.basename(f)
            dfs.append(df)
            print(f"  Loaded: {os.path.basename(f)} ({len(df)} rows)")
        except Exception as e:
            print(f"  Error loading {f}: {e}")

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)

    # Parse timestamps
    if 'timestamp' in combined.columns:
        combined['timestamp'] = pd.to_datetime(combined['timestamp'], utc=True)

    return combined


def calculate_statistics(pnls: pd.Series) -> Dict:
    """Calculate comprehensive statistics for P&L series."""
    if len(pnls) == 0:
        return {}

    # Handle edge cases
    mean = pnls.mean()
    std = pnls.std() if len(pnls) > 1 else 0

    # Downside std for Sortino
    negative_pnls = pnls[pnls < 0]
    downside_std = negative_pnls.std() if len(negative_pnls) > 1 else 0

    return {
        # Central tendency
        'mean': mean,
        'median': pnls.median(),

        # Dispersion
        'std': std,
        'variance': pnls.var() if len(pnls) > 1 else 0,
        'cv_pct': (std / abs(mean) * 100) if mean != 0 else 0,

        # Distribution shape
        'skewness': pnls.skew() if len(pnls) > 2 else 0,
        'kurtosis': pnls.kurtosis() if len(pnls) > 3 else 0,

        # Range
        'min': pnls.min(),
        'max': pnls.max(),
        'range': pnls.max() - pnls.min(),

        # Percentiles
        'p5': pnls.quantile(0.05) if len(pnls) > 0 else 0,
        'p25': pnls.quantile(0.25) if len(pnls) > 0 else 0,
        'p50': pnls.quantile(0.50) if len(pnls) > 0 else 0,
        'p75': pnls.quantile(0.75) if len(pnls) > 0 else 0,
        'p95': pnls.quantile(0.95) if len(pnls) > 0 else 0,

        # Risk-adjusted
        'sharpe': mean / std if std != 0 else 0,
        'sortino': mean / downside_std if downside_std != 0 else float('inf'),
    }


def analyze_strategy(df: pd.DataFrame, strategy_name: str) -> Optional[StrategyMetrics]:
    """Analyze a single strategy's performance."""

    if df.empty:
        return None

    # Filter to resolutions only for P&L
    resolutions = df[df['event_type'] == 'RESOLUTION']
    trades = df[df['event_type'] == 'TRADE']

    if resolutions.empty:
        # If no resolutions, use all data for what we can
        pnls = pd.Series([0])
        markets_resolved = 0
    else:
        pnls = resolutions['pnl_realized'].dropna()
        markets_resolved = len(resolutions)

    total_trades = len(trades)

    # Win/loss
    wins = (pnls > 0).sum()
    losses = (pnls < 0).sum()
    win_rate = (wins / len(pnls) * 100) if len(pnls) > 0 else 0

    # P&L metrics
    total_pnl = pnls.sum()
    avg_pnl = pnls.mean() if len(pnls) > 0 else 0
    max_win = pnls.max() if len(pnls) > 0 else 0
    max_loss = pnls.min() if len(pnls) > 0 else 0
    avg_win = pnls[pnls > 0].mean() if (pnls > 0).sum() > 0 else 0
    avg_loss = pnls[pnls < 0].mean() if (pnls < 0).sum() > 0 else 0

    # Statistical metrics
    stats = calculate_statistics(pnls)

    # Pair cost analysis (from resolutions or trades)
    pair_costs = pd.Series(dtype=float)
    if 'pos_pair_cost' in df.columns:
        if not resolutions.empty:
            pair_costs = resolutions['pos_pair_cost'].dropna()
        else:
            pair_costs = trades['pos_pair_cost'].dropna()
        pair_costs = pair_costs[pair_costs > 0]

    avg_pair_cost = pair_costs.mean() if len(pair_costs) > 0 else 0
    pair_cost_below_1 = (pair_costs < 1.0).sum() if len(pair_costs) > 0 else 0
    pair_cost_total = len(pair_costs)

    # Unhedged shares analysis
    unhedged = pd.Series(dtype=float)
    if 'pos_up_size' in df.columns and 'pos_down_size' in df.columns:
        source = resolutions if not resolutions.empty else trades
        unhedged = abs(source['pos_up_size'] - source['pos_down_size'])

    avg_unhedged = unhedged.mean() if len(unhedged) > 0 else 0
    max_unhedged = unhedged.max() if len(unhedged) > 0 else 0
    fully_hedged_count = (unhedged == 0).sum() if len(unhedged) > 0 else 0

    # Time analysis
    hours_analyzed = 0
    if 'timestamp' in df.columns and len(df) > 1:
        time_range = df['timestamp'].max() - df['timestamp'].min()
        hours_analyzed = time_range.total_seconds() / 3600

    return StrategyMetrics(
        name=strategy_name,
        markets_resolved=markets_resolved,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_pnl=avg_pnl,
        max_win=max_win,
        max_loss=max_loss,
        avg_win=avg_win,
        avg_loss=avg_loss,
        std_dev=stats.get('std', 0),
        variance=stats.get('variance', 0),
        cv_pct=stats.get('cv_pct', 0),
        skewness=stats.get('skewness', 0),
        kurtosis=stats.get('kurtosis', 0),
        sharpe=stats.get('sharpe', 0),
        sortino=stats.get('sortino', 0),
        p5=stats.get('p5', 0),
        p25=stats.get('p25', 0),
        p50=stats.get('p50', 0),
        p75=stats.get('p75', 0),
        p95=stats.get('p95', 0),
        avg_pair_cost=avg_pair_cost,
        pair_cost_below_1=pair_cost_below_1,
        pair_cost_total=pair_cost_total,
        avg_unhedged=avg_unhedged,
        max_unhedged=max_unhedged,
        fully_hedged_count=fully_hedged_count,
        hours_analyzed=hours_analyzed,
    )


def format_metrics_table(metrics: StrategyMetrics) -> str:
    """Format metrics as a readable table."""

    lines = [
        f"\n{'='*60}",
        f"  STRATEGY: {metrics.name}",
        f"{'='*60}",
        "",
        "  PERFORMANCE SUMMARY",
        "  -------------------",
        f"  Markets Resolved:     {metrics.markets_resolved}",
        f"  Total Trades:         {metrics.total_trades}",
        f"  Win Rate:             {metrics.win_rate:.1f}% ({metrics.wins}W / {metrics.losses}L)",
        f"  Total P&L:            ${metrics.total_pnl:.2f}",
        f"  Avg P&L/Market:       ${metrics.avg_pnl:.2f}",
        f"  Hours Analyzed:       {metrics.hours_analyzed:.1f}",
        "",
        "  P&L DISTRIBUTION",
        "  -----------------",
        f"  Max Win:              ${metrics.max_win:.2f}",
        f"  Max Loss:             ${metrics.max_loss:.2f}",
        f"  Avg Win:              ${metrics.avg_win:.2f}",
        f"  Avg Loss:             ${metrics.avg_loss:.2f}",
        "",
        "  STATISTICAL ANALYSIS",
        "  --------------------",
        f"  Std Deviation:        ${metrics.std_dev:.2f}",
        f"  Volatility (CV%):     {metrics.cv_pct:.1f}%",
        f"  Skewness:             {metrics.skewness:.2f}",
        f"  Kurtosis:             {metrics.kurtosis:.2f}",
        "",
        "  RISK-ADJUSTED RETURNS",
        "  ---------------------",
        f"  Sharpe Ratio:         {metrics.sharpe:.2f}",
        f"  Sortino Ratio:        {metrics.sortino:.2f}",
        "",
        "  PERCENTILES",
        "  -----------",
        f"  5th (worst case):     ${metrics.p5:.2f}",
        f"  25th (Q1):            ${metrics.p25:.2f}",
        f"  50th (median):        ${metrics.p50:.2f}",
        f"  75th (Q3):            ${metrics.p75:.2f}",
        f"  95th (best case):     ${metrics.p95:.2f}",
    ]

    # Add pair cost if available
    if metrics.pair_cost_total > 0:
        lines.extend([
            "",
            "  PAIR COST ANALYSIS",
            "  ------------------",
            f"  Avg Pair Cost:        ${metrics.avg_pair_cost:.4f}",
            f"  Below $1.00:          {metrics.pair_cost_below_1}/{metrics.pair_cost_total}",
        ])

    # Add hedging analysis
    if metrics.total_trades > 0:
        lines.extend([
            "",
            "  HEDGING ANALYSIS",
            "  ----------------",
            f"  Avg Unhedged Shares:  {metrics.avg_unhedged:.1f}",
            f"  Max Unhedged Shares:  {metrics.max_unhedged:.0f}",
            f"  Fully Hedged:         {metrics.fully_hedged_count}",
        ])

    lines.append("")
    return "\n".join(lines)


def format_comparison_table(metrics_list: List[StrategyMetrics]) -> str:
    """Format multiple strategies as a comparison table."""

    if not metrics_list:
        return "No strategies to compare."

    # Header
    header = f"{'METRIC':<25}"
    for m in metrics_list:
        name_str = str(m.name)[:15] if m.name else "UNKNOWN"
        header += f" | {name_str:<15}"

    sep = "-" * len(header)

    rows = [
        "",
        "=" * len(header),
        "  STRATEGY COMPARISON",
        "=" * len(header),
        "",
        header,
        sep,
    ]

    # Data rows
    def add_row(label, attr, fmt=".2f", prefix="$"):
        row = f"{label:<25}"
        for m in metrics_list:
            val = getattr(m, attr)
            if prefix == "$":
                row += f" | {prefix}{val:{fmt}}"[:17].ljust(17)
            elif prefix == "%":
                row += f" | {val:{fmt}}{prefix}"[:17].ljust(17)
            else:
                row += f" | {val:{fmt}}"[:17].ljust(17)
        rows.append(row)

    add_row("Markets Resolved", "markets_resolved", ".0f", "")
    add_row("Total Trades", "total_trades", ".0f", "")
    add_row("Win Rate", "win_rate", ".1f", "%")
    add_row("Total P&L", "total_pnl", ".2f", "$")
    add_row("Avg P&L/Market", "avg_pnl", ".2f", "$")
    rows.append(sep)
    add_row("Max Win", "max_win", ".2f", "$")
    add_row("Max Loss", "max_loss", ".2f", "$")
    add_row("Std Deviation", "std_dev", ".2f", "$")
    rows.append(sep)
    add_row("Sharpe Ratio", "sharpe", ".2f", "")
    add_row("Sortino Ratio", "sortino", ".2f", "")
    add_row("Volatility (CV%)", "cv_pct", ".1f", "%")
    rows.append(sep)
    add_row("Avg Pair Cost", "avg_pair_cost", ".4f", "$")
    add_row("Avg Unhedged", "avg_unhedged", ".1f", "")

    rows.append("")

    # Determine winner
    if len(metrics_list) > 1:
        best = max(metrics_list, key=lambda m: m.total_pnl)
        rows.append(f"  WINNER: {best.name} (${best.total_pnl:.2f} total P&L)")

    rows.append("")
    return "\n".join(rows)


def main():
    parser = argparse.ArgumentParser(description="Daily Trade Analyzer")
    parser.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--strategy", help="Filter by strategy name")
    parser.add_argument("--all", action="store_true", help="Analyze all available data")
    parser.add_argument("--compare", action="store_true", help="Compare all strategies")
    parser.add_argument("--output", help="Output file path (optional)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  DAILY TRADE ANALYZER")
    print("=" * 60)
    print("")

    # Load data
    print("Loading trade files...")
    df = load_trade_files(
        date=args.date,
        strategy=args.strategy,
        all_data=args.all
    )

    if df.empty:
        print("\nNo data to analyze.")
        return

    print(f"\nTotal rows loaded: {len(df)}")

    # Determine strategies to analyze
    strategies_to_analyze = []

    if 'trade_mode' in df.columns:
        modes = df['trade_mode'].dropna().unique()
        # Filter to string modes only (exclude numeric values)
        for m in modes:
            if isinstance(m, str) and len(m) > 2:
                strategies_to_analyze.append(m)

    if 'source_file' in df.columns:
        # Extract strategy names from filenames
        for f in df['source_file'].unique():
            # paper_trades_AGGRESSIVE_2026-01-03.csv -> AGGRESSIVE
            parts = f.replace('.csv', '').split('_')
            for p in parts:
                if isinstance(p, str) and p.isupper() and len(p) > 3 and p not in strategies_to_analyze:
                    strategies_to_analyze.append(p)

    if not strategies_to_analyze:
        strategies_to_analyze = ['ALL']

    print(f"Strategies found: {strategies_to_analyze}")

    # Analyze each strategy
    all_metrics = []

    for strategy in strategies_to_analyze:
        if strategy == 'ALL':
            strategy_df = df
        elif 'trade_mode' in df.columns:
            strategy_df = df[df['trade_mode'] == strategy]
        else:
            strategy_df = df

        if strategy_df.empty:
            continue

        metrics = analyze_strategy(strategy_df, strategy)
        if metrics:
            all_metrics.append(metrics)

    # Output results
    output_lines = []

    if args.compare and len(all_metrics) > 1:
        output_lines.append(format_comparison_table(all_metrics))

    for metrics in all_metrics:
        output_lines.append(format_metrics_table(metrics))

    output = "\n".join(output_lines)
    print(output)

    # Save to file if requested
    if args.output:
        with open(args.output, 'w') as f:
            f.write(output)
        print(f"\nResults saved to: {args.output}")

    # JSON output
    if args.json:
        import json
        json_data = [
            {k: v for k, v in m.__dict__.items()}
            for m in all_metrics
        ]
        print("\n--- JSON OUTPUT ---")
        print(json.dumps(json_data, indent=2))

    print("\n" + "=" * 60)
    print("  Analysis complete.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
