#!/usr/bin/env python3
"""
Test script for Trading Schedule.

Demonstrates the TradingSchedule model that controls when
the bot actively trades - by hours and/or date range.

Usage:
    python scripts/test_schedule.py
"""

import asyncio
import sys
from pathlib import Path
from datetime import time, date, timedelta

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.models.schedule import TradingSchedule, ET
from src.services.market_finder import MarketFinder
from src.services.market_rotator import MarketRotator


console = Console()


def format_timedelta(td: timedelta) -> str:
    """Format timedelta as human-readable string."""
    if td is None:
        return "N/A"
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


async def main():
    """Test the TradingSchedule feature."""
    console.print(Panel.fit(
        "[bold cyan]Trading Schedule Test[/]",
        subtitle="Control Bot Operating Hours"
    ))

    # Show current time in ET
    now_et = TradingSchedule().now_in_tz()
    console.print(f"\n[yellow]Current Time (ET):[/] {now_et.strftime('%Y-%m-%d %I:%M:%S %p %Z')}")

    # Test 1: 24/7 Schedule (default)
    console.print("\n[bold]═══ Test 1: 24/7 Schedule (Default) ═══[/]")
    schedule_247 = TradingSchedule()
    console.print(f"  Schedule: {schedule_247}")
    console.print(f"  Is 24/7: {schedule_247.is_24_7}")
    console.print(f"  Is Active: {schedule_247.is_active()}")

    # Test 2: Trading Hours Only
    console.print("\n[bold]═══ Test 2: Trading Hours Only ═══[/]")
    schedule_hours = TradingSchedule(
        start_time=time(9, 0),   # 9:00 AM
        end_time=time(17, 0),    # 5:00 PM
    )
    console.print(f"  Schedule: {schedule_hours}")
    console.print(f"  Is Active: {schedule_hours.is_active()}")
    console.print(f"  Within Hours: {schedule_hours.is_within_hours()}")

    until_active = schedule_hours.time_until_active()
    until_inactive = schedule_hours.time_until_inactive()
    console.print(f"  Until Active: {format_timedelta(until_active) if until_active else 'Already active'}")
    console.print(f"  Until Inactive: {format_timedelta(until_inactive) if until_inactive else 'N/A'}")

    # Test 3: Date Range Only
    console.print("\n[bold]═══ Test 3: Date Range Only ═══[/]")
    today = date.today()
    schedule_dates = TradingSchedule(
        start_date=today,
        end_date=today + timedelta(days=7),
    )
    console.print(f"  Schedule: {schedule_dates}")
    console.print(f"  Is Active: {schedule_dates.is_active()}")
    console.print(f"  Within Dates: {schedule_dates.is_within_dates()}")

    # Test 4: Combined Hours + Dates
    console.print("\n[bold]═══ Test 4: Combined Hours + Dates ═══[/]")
    schedule_combined = TradingSchedule(
        start_time=time(9, 30),   # 9:30 AM
        end_time=time(16, 0),     # 4:00 PM
        start_date=today,
        end_date=today + timedelta(days=14),
    )
    console.print(f"  Schedule: {schedule_combined}")
    console.print(f"  Is Active: {schedule_combined.is_active()}")

    # Show detailed status
    status = schedule_combined.get_status()
    console.print("\n  [cyan]Detailed Status:[/]")
    for key, value in status.items():
        if isinstance(value, dict):
            console.print(f"    {key}:")
            for k, v in value.items():
                console.print(f"      {k}: {v}")
        else:
            console.print(f"    {key}: {value}")

    # Test 5: Integration with MarketRotator
    console.print("\n[bold]═══ Test 5: MarketRotator Integration ═══[/]")

    finder = MarketFinder()

    try:
        # Create rotator with schedule
        rotator = MarketRotator(
            finder=finder,
            continuous=True,
            schedule=schedule_hours,  # 9 AM - 5 PM ET
        )

        console.print(f"\n  Rotator: {rotator}")
        console.print(f"  Trading Allowed: {rotator.is_trading_allowed()}")
        console.print(f"  Within Hours: {rotator.is_within_trading_hours()}")
        console.print(f"  Within Dates: {rotator.is_within_trading_dates()}")

        schedule_status = rotator.get_schedule_status()
        if schedule_status:
            console.print(f"\n  [cyan]Schedule Status:[/]")
            console.print(f"    Active: {schedule_status.get('is_active')}")
            if 'trading_hours' in schedule_status:
                hours = schedule_status['trading_hours']
                console.print(f"    Hours: {hours['start']} - {hours['end']}")

        # Show what happens when outside schedule
        console.print("\n  [yellow]Session Completion Check:[/]")
        if not rotator.is_trading_allowed():
            end_reason = rotator.get_session_end_reason()
            console.print(f"    Outside schedule - would end with: {end_reason}")
        else:
            console.print("    Within schedule - trading allowed")

    finally:
        await finder.close()

    # Test 6: Example Schedules
    console.print("\n[bold]═══ Example Schedules ═══[/]")

    examples = [
        ("Market Hours (9:30 AM - 4:00 PM)", TradingSchedule(
            start_time=time(9, 30),
            end_time=time(16, 0),
        )),
        ("Evening Trading (6 PM - 10 PM)", TradingSchedule(
            start_time=time(18, 0),
            end_time=time(22, 0),
        )),
        ("This Week Only", TradingSchedule(
            start_date=today,
            end_date=today + timedelta(days=7),
        )),
        ("Christmas Week 9-5", TradingSchedule(
            start_time=time(9, 0),
            end_time=time(17, 0),
            start_date=date(2025, 12, 23),
            end_date=date(2025, 12, 27),
        )),
    ]

    table = Table(title="Schedule Examples")
    table.add_column("Name", style="cyan")
    table.add_column("Hours")
    table.add_column("Dates")
    table.add_column("Active Now")

    for name, sched in examples:
        hours = "24/7"
        if sched.start_time or sched.end_time:
            start = sched.start_time.strftime("%I:%M %p") if sched.start_time else "12:00 AM"
            end = sched.end_time.strftime("%I:%M %p") if sched.end_time else "11:59 PM"
            hours = f"{start} - {end}"

        dates = "Any"
        if sched.start_date or sched.end_date:
            start = sched.start_date.strftime("%b %d") if sched.start_date else "..."
            end = sched.end_date.strftime("%b %d") if sched.end_date else "..."
            dates = f"{start} to {end}"

        active = "[green]Yes[/]" if sched.is_active() else "[red]No[/]"
        table.add_row(name, hours, dates, active)

    console.print(table)

    # Usage summary
    console.print("\n[bold]Usage in Code:[/]")
    console.print("""
    from datetime import time, date
    from src.models.schedule import TradingSchedule
    from src.services.market_rotator import MarketRotator

    # Create schedule
    schedule = TradingSchedule(
        start_time=time(9, 0),      # 9:00 AM ET
        end_time=time(17, 0),       # 5:00 PM ET
        start_date=date(2025, 12, 19),
        end_date=date(2025, 12, 31),
    )

    # Use with rotator
    rotator = MarketRotator(
        finder=finder,
        continuous=True,
        schedule=schedule,
    )

    # Check if trading allowed
    if rotator.is_trading_allowed():
        await rotator.start_session()
    """)


if __name__ == "__main__":
    asyncio.run(main())
