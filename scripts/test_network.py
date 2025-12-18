#!/usr/bin/env python3
"""
Network Monitor Test Script

Tests the network monitoring and failover system.
Run this to verify your WiFi configuration is correct.

Usage:
    source venv/bin/activate
    python scripts/test_network.py
"""

import sys
import asyncio
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.layout import Layout

from src.config import Config
from src.utils.network_monitor import NetworkMonitor, NetworkStatus


console = Console()


def display_header():
    """Display test header."""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Network Monitor Test[/bold cyan]\n"
        "Testing WiFi connectivity and failover system",
        border_style="cyan"
    ))
    console.print()


def display_config(config: Config):
    """Display network configuration."""
    table = Table(title="Network Configuration", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")
    table.add_column("Status", style="green")

    # Primary WiFi
    primary_status = "[green]Set[/green]" if config.primary_wifi else "[red]Not Set[/red]"
    table.add_row(
        "Primary WiFi",
        config.primary_wifi or "(not configured)",
        primary_status
    )

    # Backup WiFi 1
    backup1_status = "[green]Set[/green]" if config.backup_wifi_1 else "[yellow]Optional[/yellow]"
    table.add_row(
        "Backup WiFi 1",
        config.backup_wifi_1 or "(not configured)",
        backup1_status
    )

    # Backup WiFi 2
    backup2_status = "[green]Set[/green]" if config.backup_wifi_2 else "[yellow]Optional[/yellow]"
    table.add_row(
        "Backup WiFi 2",
        config.backup_wifi_2 or "(not configured)",
        backup2_status
    )

    # Poll interval
    table.add_row(
        "Poll Interval",
        f"{config.network_poll_interval} seconds",
        "[green]OK[/green]"
    )

    console.print(table)
    console.print()


def display_status(status: NetworkStatus):
    """Display current network status."""
    table = Table(title="Current Network Status", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    # Current network
    table.add_row("Current Network", status.current_network or "(not connected)")

    # Connection status
    if status.is_connected:
        conn_str = f"[green]Connected[/green] ({status.latency_ms:.0f}ms latency)"
    else:
        conn_str = "[red]Disconnected[/red]"
    table.add_row("Internet", conn_str)

    # Backup status
    backup_str = "[yellow]On Backup[/yellow]" if status.is_on_backup else "[green]On Primary[/green]"
    table.add_row("Network Type", backup_str)

    # Last check
    table.add_row("Last Check", status.last_check.strftime("%H:%M:%S"))

    console.print(table)
    console.print()


def test_get_current_network(monitor: NetworkMonitor):
    """Test getting current WiFi network."""
    console.print("[bold]Test 1: Get Current Network[/bold]")

    network = monitor.get_current_network()

    if network:
        console.print(f"  [green]\u2713[/green] Current network: [cyan]{network}[/cyan]")
    else:
        console.print("  [yellow]\u26a0[/yellow] Not connected to any WiFi network")

    console.print()
    return network


def test_internet_connectivity(monitor: NetworkMonitor):
    """Test internet connectivity."""
    console.print("[bold]Test 2: Internet Connectivity[/bold]")

    connected, latency = monitor.check_internet()

    if connected:
        console.print(f"  [green]\u2713[/green] Internet connected (latency: {latency:.0f}ms)")
    else:
        console.print("  [red]\u2717[/red] No internet connection")

    console.print()
    return connected


def test_scan_networks(monitor: NetworkMonitor):
    """Test scanning for available networks."""
    console.print("[bold]Test 3: Scan Available Networks[/bold]")

    networks = monitor.get_available_networks()

    if networks:
        console.print(f"  [green]\u2713[/green] Found {len(networks)} networks:")
        for i, net in enumerate(networks[:10]):  # Show first 10
            console.print(f"      {i+1}. {net}")
        if len(networks) > 10:
            console.print(f"      ... and {len(networks) - 10} more")
    else:
        console.print("  [yellow]\u26a0[/yellow] No networks found (scan may require permissions)")

    console.print()
    return networks


def test_primary_available(monitor: NetworkMonitor, config: Config):
    """Test if primary network is available."""
    console.print("[bold]Test 4: Primary Network Availability[/bold]")

    if not config.primary_wifi:
        console.print("  [yellow]\u26a0[/yellow] PRIMARY_WIFI not configured in .env")
        console.print()
        return False

    available = monitor.is_primary_available()

    if available:
        console.print(f"  [green]\u2713[/green] Primary network '{config.primary_wifi}' is in range")
    else:
        console.print(f"  [yellow]\u26a0[/yellow] Primary network '{config.primary_wifi}' not found")

    console.print()
    return available


def display_summary(results: dict):
    """Display test summary."""
    console.print()
    console.print(Panel.fit(
        "[bold]Test Summary[/bold]",
        border_style="cyan"
    ))

    all_passed = all([
        results.get("current_network"),
        results.get("internet"),
    ])

    if all_passed:
        console.print("[green]\u2713 All critical tests passed![/green]")
        console.print()
        console.print("Network monitor is ready to use.")
    else:
        console.print("[yellow]\u26a0 Some tests need attention:[/yellow]")

        if not results.get("current_network"):
            console.print("  - Not connected to WiFi")

        if not results.get("internet"):
            console.print("  - No internet connection")

    console.print()


def display_next_steps(config: Config):
    """Display configuration tips."""
    missing = []

    if not config.primary_wifi:
        missing.append("PRIMARY_WIFI")
    if not config.backup_wifi_1:
        missing.append("BACKUP_WIFI_1 (optional)")
    if not config.backup_wifi_2:
        missing.append("BACKUP_WIFI_2 (optional)")

    if missing:
        console.print(Panel.fit(
            "[bold yellow]Configuration Tips[/bold yellow]\n\n"
            "Add these to your .env file for full failover support:\n\n" +
            "\n".join([f"  {var}=YourNetworkName" for var in missing]),
            border_style="yellow"
        ))
        console.print()


def run_tests():
    """Run all network tests."""
    display_header()

    # Load configuration
    try:
        config = Config()
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        console.print("Make sure .env file exists with WALLET_PRIVATE_KEY")
        return

    # Display config
    display_config(config)

    # Create monitor
    monitor = NetworkMonitor(config)

    # Run tests
    results = {}

    results["current_network"] = test_get_current_network(monitor)
    results["internet"] = test_internet_connectivity(monitor)
    results["networks"] = test_scan_networks(monitor)
    results["primary_available"] = test_primary_available(monitor, config)

    # Display overall status
    status = monitor.get_status()
    display_status(status)

    # Summary
    display_summary(results)

    # Next steps
    display_next_steps(config)


async def demo_monitoring():
    """Demo the monitoring loop (runs for 30 seconds)."""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Monitoring Demo[/bold cyan]\n"
        "Watching network status for 30 seconds...\n"
        "Press Ctrl+C to stop",
        border_style="cyan"
    ))
    console.print()

    config = Config()
    monitor = NetworkMonitor(config)

    def on_change(event: str, network: str):
        """Handle network change events."""
        if event == "failover":
            console.print(f"[yellow]\u26a0 FAILOVER: Switched to {network}[/yellow]")
        elif event == "restored":
            console.print(f"[green]\u2713 RESTORED: Back on {network}[/green]")
        elif event == "disconnected":
            console.print(f"[red]\u2717 DISCONNECTED from {network}[/red]")

    # Run monitoring for 30 seconds
    try:
        task = asyncio.create_task(monitor.start_monitoring(callback=on_change))
        await asyncio.sleep(30)
        monitor.stop_monitoring()
        await task
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        console.print("\n[yellow]Monitoring stopped[/yellow]")
        monitor.stop_monitoring()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Test network monitor")
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Run monitoring demo for 30 seconds"
    )

    args = parser.parse_args()

    if args.monitor:
        asyncio.run(demo_monitoring())
    else:
        run_tests()


if __name__ == "__main__":
    main()
