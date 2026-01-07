#!/usr/bin/env python3
"""
Test script for Dry Run Simulation.

Runs the complete trading loop in paper trading mode
to validate strategy before going live.

Usage:
    python scripts/test_dry_run.py
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.services.dry_run import DryRunSimulator, SimulationReport
from src.services.paper_trading import SimulationConfig


console = Console()


def print_report(report: SimulationReport) -> None:
    """Print detailed simulation report."""

    # Header
    console.print("\n[bold cyan]═══════════════════════════════════════[/]")
    console.print("[bold cyan]         DRY RUN SIMULATION REPORT       [/]")
    console.print("[bold cyan]═══════════════════════════════════════[/]\n")

    # Summary panel
    roi_color = "green" if report.roi_percent >= 0 else "red"
    console.print(Panel.fit(
        f"[bold]Duration:[/] {report.duration_minutes:.1f} minutes\n"
        f"[bold]Markets Analyzed:[/] {report.markets_analyzed}\n"
        f"[bold]Markets Traded:[/] {report.markets_traded}\n"
        f"[bold]Initial Balance:[/] ${report.initial_balance:.2f}\n"
        f"[bold]Final Balance:[/] ${report.final_balance:.2f}\n"
        f"[bold]ROI:[/] [{roi_color}]{report.roi_percent:.2f}%[/{roi_color}]",
        title="Summary",
        border_style="cyan"
    ))

    # Opportunity Analysis
    console.print("\n[bold]Opportunity Analysis[/]")
    opp_table = Table(show_header=False)
    opp_table.add_column("Metric", style="cyan")
    opp_table.add_column("Value", justify="right")

    opp_table.add_row("Total Opportunities Checked", str(report.total_opportunities))
    opp_table.add_row("Profitable Opportunities", str(report.profitable_opportunities))
    if report.total_opportunities > 0:
        pct = report.profitable_opportunities / report.total_opportunities * 100
        opp_table.add_row("Profitable %", f"{pct:.1f}%")

    console.print(opp_table)

    # Trade Statistics
    console.print("\n[bold]Trade Statistics[/]")
    trade_table = Table(show_header=False)
    trade_table.add_column("Metric", style="cyan")
    trade_table.add_column("Value", justify="right")

    trade_table.add_row("Trades Attempted", str(report.trades_attempted))
    trade_table.add_row("Trades Successful", str(report.trades_successful))
    trade_table.add_row("Partial Fills", str(report.trades_partial))
    trade_table.add_row("Failed Fills", str(report.trades_failed))
    trade_table.add_row("Win Rate", f"{report.win_rate:.1%}")

    console.print(trade_table)

    # Financial Results
    console.print("\n[bold]Financial Results[/]")
    fin_table = Table(show_header=False)
    fin_table.add_column("Metric", style="cyan")
    fin_table.add_column("Value", justify="right")

    fin_table.add_row("Total Cost", f"${report.total_cost:.4f}")
    fin_table.add_row("Expected Profit", f"${report.total_profit:.4f}")
    fin_table.add_row("Realized P&L", f"${report.realized_pnl:.4f}")
    fin_table.add_row("Unrealized P&L", f"${report.unrealized_pnl:.4f}")
    if report.trades_successful > 0:
        fin_table.add_row("Avg Profit/Trade", f"${report.avg_profit_per_pair:.4f}")

    console.print(fin_table)

    # Per-Market Results
    if report.market_results:
        console.print("\n[bold]Per-Market Results[/]")
        market_table = Table()
        market_table.add_column("Market", style="cyan", max_width=30)
        market_table.add_column("Opps", justify="right")
        market_table.add_column("Trades", justify="right")
        market_table.add_column("Pairs", justify="right")
        market_table.add_column("Cost", justify="right")
        market_table.add_column("P&L", justify="right")

        for mr in report.market_results:
            pnl_str = f"${mr.realized_pnl:.4f}" if mr.realized_pnl != 0 else "-"
            market_table.add_row(
                mr.market_slug[:28],
                str(mr.opportunities_found),
                f"{mr.trades_successful}/{mr.trades_attempted}",
                str(mr.pairs_traded),
                f"${mr.total_cost:.2f}" if mr.total_cost > 0 else "-",
                pnl_str,
            )

        console.print(market_table)

    # Final verdict
    console.print()
    if report.roi_percent > 0:
        console.print(Panel.fit(
            "[bold green]STRATEGY VALIDATION: PASSED[/]\n"
            f"The strategy generated a positive return of {report.roi_percent:.2f}%",
            border_style="green"
        ))
    elif report.trades_attempted == 0:
        console.print(Panel.fit(
            "[bold yellow]STRATEGY VALIDATION: INCONCLUSIVE[/]\n"
            "No trades were executed (no profitable opportunities found)",
            border_style="yellow"
        ))
    else:
        console.print(Panel.fit(
            "[bold red]STRATEGY VALIDATION: NEEDS REVIEW[/]\n"
            f"The strategy generated a loss of {abs(report.roi_percent):.2f}%",
            border_style="red"
        ))


async def main():
    """Run the dry run simulation."""
    console.print(Panel.fit(
        "[bold cyan]Dry Run Simulation[/]",
        subtitle="Phase 4: Strategy Validation"
    ))

    # Configuration
    duration = 1.0  # 1 minute for quick test
    initial_balance = 100.0

    console.print("\n[yellow]Configuration:[/]")
    console.print(f"  Duration: {duration} minute(s)")
    console.print(f"  Initial Balance: ${initial_balance:.2f}")
    console.print(f"  Fill Probability: 90%")
    console.print(f"  Max Pairs/Trade: 10")

    # Create simulator
    sim_config = SimulationConfig(
        fill_probability=0.90,
        partial_fill_rate=0.10,
        slippage_bps=5.0,
    )

    simulator = DryRunSimulator(
        initial_balance=initial_balance,
        max_pairs_per_trade=10,
        min_profit_threshold=0.001,  # $0.001 minimum profit per pair
        sim_config=sim_config,
    )

    # Run simulation
    console.print("\n[yellow]Running simulation...[/]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Simulating...", total=None)

        try:
            report = await simulator.run(
                duration_minutes=duration,
                check_interval=2.0,  # Check every 2 seconds
                continuous=False,  # Session mode for testing
                max_markets=5,
            )
            progress.update(task, description="Complete!")

        except Exception as e:
            progress.update(task, description=f"Error: {e}")
            console.print(f"\n[red]Simulation failed: {e}[/]")
            raise

    # Print report
    print_report(report)


if __name__ == "__main__":
    asyncio.run(main())
