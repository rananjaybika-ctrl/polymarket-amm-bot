#!/usr/bin/env python3
"""
Daily Report Generator - Automated daily P&L report with Telegram notification

Generates a daily summary and optionally sends to Telegram.

Usage:
    python scripts/daily_report.py                  # Generate report for today
    python scripts/daily_report.py --date 2026-01-09
    python scripts/daily_report.py --telegram       # Send to Telegram
    python scripts/daily_report.py --save           # Save to reports/
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_trades import (
    load_trade_files,
    analyze_strategy,
    StrategyMetrics,
)


def generate_telegram_message(metrics_list: list, date_str: str) -> str:
    """Generate a concise Telegram message."""

    if not metrics_list:
        return f"*Daily Report - {date_str}*\n\nNo trades found."

    lines = [
        f"*Daily Report - {date_str}*",
        "",
    ]

    total_pnl = sum(m.total_pnl for m in metrics_list)
    total_markets = sum(m.markets_resolved for m in metrics_list)
    total_trades = sum(m.total_trades for m in metrics_list)

    # Summary
    emoji = "+" if total_pnl >= 0 else ""
    lines.append(f"Total P&L: `{emoji}${total_pnl:.2f}`")
    lines.append(f"Markets: `{total_markets}` | Trades: `{total_trades}`")
    lines.append("")

    # Per-strategy breakdown
    for m in metrics_list:
        emoji = "+" if m.total_pnl >= 0 else ""
        win_indicator = "W" if m.win_rate >= 50 else "L"
        lines.append(
            f"{m.name}: `{emoji}${m.total_pnl:.2f}` "
            f"({m.win_rate:.0f}% WR, {m.markets_resolved} mkts)"
        )

    # Key metrics
    lines.append("")
    best = max(metrics_list, key=lambda m: m.sharpe) if metrics_list else None
    if best and best.sharpe > 0:
        lines.append(f"Best Sharpe: `{best.name}` ({best.sharpe:.2f})")

    # Warnings
    warnings = []
    for m in metrics_list:
        if m.avg_pair_cost > 1.0 and m.pair_cost_total > 0:
            warnings.append(f"{m.name}: Avg pair cost ${m.avg_pair_cost:.4f} > $1.00")
        if m.max_unhedged > 10:
            warnings.append(f"{m.name}: Max unhedged {m.max_unhedged:.0f} shares")
        if m.win_rate < 40 and m.markets_resolved > 3:
            warnings.append(f"{m.name}: Low win rate {m.win_rate:.0f}%")

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  {w}")

    return "\n".join(lines)


def generate_full_report(metrics_list: list, date_str: str) -> str:
    """Generate a full markdown report."""

    lines = [
        f"# Daily Trading Report - {date_str}",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "---",
        "",
        "## Summary",
        "",
    ]

    if not metrics_list:
        lines.append("No trades found for this date.")
        return "\n".join(lines)

    total_pnl = sum(m.total_pnl for m in metrics_list)
    total_markets = sum(m.markets_resolved for m in metrics_list)
    total_trades = sum(m.total_trades for m in metrics_list)

    lines.extend([
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total P&L | ${total_pnl:.2f} |",
        f"| Markets Resolved | {total_markets} |",
        f"| Total Trades | {total_trades} |",
        f"| Strategies Active | {len(metrics_list)} |",
        "",
        "---",
        "",
        "## Strategy Breakdown",
        "",
    ])

    # Per-strategy details
    for m in metrics_list:
        lines.extend([
            f"### {m.name}",
            "",
            f"| Metric | Value |",
            f"|--------|-------|",
            f"| P&L | ${m.total_pnl:.2f} |",
            f"| Win Rate | {m.win_rate:.1f}% ({m.wins}W/{m.losses}L) |",
            f"| Avg P&L/Market | ${m.avg_pnl:.2f} |",
            f"| Sharpe Ratio | {m.sharpe:.2f} |",
            f"| Max Win | ${m.max_win:.2f} |",
            f"| Max Loss | ${m.max_loss:.2f} |",
        ])

        if m.pair_cost_total > 0:
            lines.append(f"| Avg Pair Cost | ${m.avg_pair_cost:.4f} |")
            lines.append(f"| Pairs < $1.00 | {m.pair_cost_below_1}/{m.pair_cost_total} |")

        lines.extend([
            f"| Avg Unhedged | {m.avg_unhedged:.1f} |",
            "",
        ])

    # Recommendations
    lines.extend([
        "---",
        "",
        "## Recommendations",
        "",
    ])

    recommendations = []

    for m in metrics_list:
        if m.sharpe > 1.5:
            recommendations.append(f"- **{m.name}**: Excellent Sharpe ({m.sharpe:.2f}). Consider increasing size.")
        elif m.sharpe < 0.5 and m.markets_resolved > 3:
            recommendations.append(f"- **{m.name}**: Poor Sharpe ({m.sharpe:.2f}). Review parameters or pause.")

        if m.avg_pair_cost > 0.995 and m.pair_cost_total > 0:
            recommendations.append(f"- **{m.name}**: Pair cost ${m.avg_pair_cost:.4f} is marginal. Tighten entry criteria.")

        if m.max_unhedged > 10:
            recommendations.append(f"- **{m.name}**: High unhedged exposure ({m.max_unhedged:.0f}). Review hedging logic.")

    if recommendations:
        lines.extend(recommendations)
    else:
        lines.append("- No specific recommendations. Performance within normal parameters.")

    lines.append("")
    return "\n".join(lines)


async def send_telegram(message: str) -> bool:
    """Send message to Telegram."""
    try:
        from src.config import Config
        from src.utils.telegram_notifier import TelegramNotifier

        config = Config()
        notifier = TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )
        await notifier.send_message(message, parse_mode="Markdown")
        return True
    except Exception as e:
        print(f"Failed to send Telegram: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily Report Generator")
    parser.add_argument("--date", help="Date to analyze (YYYY-MM-DD)")
    parser.add_argument("--telegram", action="store_true", help="Send to Telegram")
    parser.add_argument("--save", action="store_true", help="Save to reports/")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")

    args = parser.parse_args()

    # Determine date
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not args.quiet:
        print(f"\nGenerating daily report for {date_str}...")

    # Load data
    df = load_trade_files(date=date_str, all_data=False)

    if df.empty:
        print(f"No trades found for {date_str}")
        return

    # Analyze strategies
    strategies = []
    if 'trade_mode' in df.columns:
        modes = df['trade_mode'].dropna().unique()
        strategies = [m for m in modes if isinstance(m, str) and len(m) > 2]

    if not strategies:
        strategies = ['ALL']

    metrics_list = []
    for strategy in strategies:
        if strategy == 'ALL':
            strategy_df = df
        else:
            strategy_df = df[df['trade_mode'] == strategy]

        if strategy_df.empty:
            continue

        metrics = analyze_strategy(strategy_df, strategy)
        if metrics:
            metrics_list.append(metrics)

    # Generate reports
    telegram_msg = generate_telegram_message(metrics_list, date_str)
    full_report = generate_full_report(metrics_list, date_str)

    # Print Telegram message
    if not args.quiet:
        print("\n--- Telegram Message ---")
        print(telegram_msg)
        print("------------------------\n")

    # Send to Telegram if requested
    if args.telegram:
        import asyncio
        success = asyncio.run(send_telegram(telegram_msg))
        if success:
            print("Sent to Telegram!")
        else:
            print("Failed to send to Telegram")

    # Save report if requested
    if args.save:
        reports_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "reports"
        )
        os.makedirs(reports_dir, exist_ok=True)

        report_path = os.path.join(reports_dir, f"daily_report_{date_str}.md")
        with open(report_path, 'w') as f:
            f.write(full_report)
        print(f"Report saved to: {report_path}")


if __name__ == "__main__":
    main()
