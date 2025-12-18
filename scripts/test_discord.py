#!/usr/bin/env python3
"""
Discord Webhook Test Script

Tests the Discord notification system by sending test messages
to each configured channel.

Usage:
    source venv/bin/activate
    python scripts/test_discord.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.config import Config
from src.utils.discord_notifier import DiscordNotifier


console = Console()


def display_header():
    """Display test header."""
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Discord Webhook Test[/bold cyan]\n"
        "Testing Discord notification channels",
        border_style="cyan"
    ))
    console.print()


def display_config(config: Config):
    """Display Discord configuration."""
    table = Table(title="Discord Configuration", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")
    table.add_column("Status", style="green")

    # PNL Webhook
    pnl_value = config.discord_webhook_pnl[:50] + "..." if len(config.discord_webhook_pnl) > 50 else config.discord_webhook_pnl
    pnl_valid = is_valid_webhook(config.discord_webhook_pnl)
    pnl_status = "[green]Valid[/green]" if pnl_valid else "[red]Invalid/Placeholder[/red]"
    table.add_row("PNL Webhook", pnl_value or "(not set)", pnl_status)

    # Losses Webhook
    losses_value = config.discord_webhook_losses[:50] + "..." if len(config.discord_webhook_losses) > 50 else config.discord_webhook_losses
    losses_valid = is_valid_webhook(config.discord_webhook_losses)
    losses_status = "[green]Valid[/green]" if losses_valid else "[red]Invalid/Placeholder[/red]"
    table.add_row("Losses Webhook", losses_value or "(not set)", losses_status)

    # Outages Webhook
    outages_value = config.discord_webhook_outages[:50] + "..." if len(config.discord_webhook_outages) > 50 else config.discord_webhook_outages
    outages_valid = is_valid_webhook(config.discord_webhook_outages)
    outages_status = "[green]Valid[/green]" if outages_valid else "[red]Invalid/Placeholder[/red]"
    table.add_row("Outages Webhook", outages_value or "(not set)", outages_status)

    # User ID
    user_id_valid = config.discord_user_id and not any(p in config.discord_user_id.lower() for p in ["your_", "xxx", "123456789"])
    user_status = "[green]Set[/green]" if user_id_valid else "[yellow]Placeholder[/yellow]"
    table.add_row("User ID (@mentions)", config.discord_user_id or "(not set)", user_status)

    console.print(table)
    console.print()


def is_valid_webhook(url: str) -> bool:
    """Check if webhook URL looks valid."""
    if not url:
        return False
    if not url.startswith("https://discord.com/api/webhooks/"):
        return False
    placeholders = ["your_", "xxx", "placeholder", "example"]
    return not any(p in url.lower() for p in placeholders)


def test_pnl_notification(notifier: DiscordNotifier) -> bool:
    """Test PNL notification channel."""
    console.print("[bold]Test 1: PNL Channel[/bold]")

    if not is_valid_webhook(notifier.webhook_pnl):
        console.print("  [yellow]\u26a0[/yellow] Skipped - webhook not configured")
        return False

    success = notifier.send_pnl(
        "Test Trade: +$1.50 locked profit",
        {
            "Market": "BTC 15-min Up (TEST)",
            "Entry": "$0.48",
            "Exit": "$0.52",
            "Shares": "10"
        }
    )

    if success:
        console.print("  [green]\u2713[/green] PNL notification sent! Check #pnl-summary channel")
    else:
        console.print("  [red]\u2717[/red] Failed to send PNL notification")

    console.print()
    return success


def test_loss_notification(notifier: DiscordNotifier) -> bool:
    """Test loss notification channel."""
    console.print("[bold]Test 2: Losses Channel (@mention)[/bold]")

    if not is_valid_webhook(notifier.webhook_losses):
        console.print("  [yellow]\u26a0[/yellow] Skipped - webhook not configured")
        return False

    success = notifier.send_loss(
        "Test Loss Alert: -$2.00 on trade (TEST)",
        {
            "Market": "BTC 15-min Down (TEST)",
            "Loss": "-$2.00",
            "Action": "Position closed"
        }
    )

    if success:
        console.print("  [green]\u2713[/green] Loss notification sent! Check #losses channel")
        if notifier.user_id and not any(p in notifier.user_id.lower() for p in ["your_", "xxx", "123"]):
            console.print(f"      You should be @mentioned")
    else:
        console.print("  [red]\u2717[/red] Failed to send loss notification")

    console.print()
    return success


def test_outage_notification(notifier: DiscordNotifier) -> bool:
    """Test outage notification channel."""
    console.print("[bold]Test 3: Outages Channel (@mention)[/bold]")

    if not is_valid_webhook(notifier.webhook_outages):
        console.print("  [yellow]\u26a0[/yellow] Skipped - webhook not configured")
        return False

    success = notifier.send_outage(
        "Test Outage: Network connection simulated failure (TEST)",
        outage_type="network",
        details={
            "Status": "Testing",
            "Duration": "0 seconds"
        }
    )

    if success:
        console.print("  [green]\u2713[/green] Outage notification sent! Check #outages channel")
        if notifier.user_id and not any(p in notifier.user_id.lower() for p in ["your_", "xxx", "123"]):
            console.print(f"      You should be @mentioned")
    else:
        console.print("  [red]\u2717[/red] Failed to send outage notification")

    console.print()
    return success


def display_summary(results: dict):
    """Display test summary."""
    console.print()
    console.print(Panel.fit(
        "[bold]Test Summary[/bold]",
        border_style="cyan"
    ))

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    if passed == total:
        console.print(f"[green]\u2713 All {total} tests passed![/green]")
    elif passed > 0:
        console.print(f"[yellow]\u26a0 {passed}/{total} tests passed[/yellow]")
    else:
        console.print(f"[red]\u2717 No tests passed[/red]")

    console.print()


def display_setup_instructions():
    """Display webhook setup instructions."""
    console.print(Panel.fit(
        "[bold yellow]Discord Webhook Setup[/bold yellow]\n\n"
        "To enable Discord notifications:\n\n"
        "1. Create 3 text channels in your Discord server:\n"
        "   - #pnl-summary (trade updates)\n"
        "   - #losses (loss alerts)\n"
        "   - #outages (network/API issues)\n\n"
        "2. For EACH channel, create a webhook:\n"
        "   - Right-click channel -> Edit Channel -> Integrations -> Webhooks\n"
        "   - Click 'New Webhook' and copy the URL\n\n"
        "3. Get your Discord User ID:\n"
        "   - Enable Developer Mode: User Settings -> Advanced -> Developer Mode\n"
        "   - Right-click your name -> Copy User ID\n\n"
        "4. Update your .env file:\n"
        "   DISCORD_WEBHOOK_PNL=https://discord.com/api/webhooks/...\n"
        "   DISCORD_WEBHOOK_LOSSES=https://discord.com/api/webhooks/...\n"
        "   DISCORD_WEBHOOK_OUTAGES=https://discord.com/api/webhooks/...\n"
        "   DISCORD_USER_ID=123456789012345678",
        border_style="yellow"
    ))
    console.print()


def main():
    """Main test function."""
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

    # Create notifier
    notifier = DiscordNotifier(config)

    # Check if any webhooks are configured
    any_valid = any([
        is_valid_webhook(config.discord_webhook_pnl),
        is_valid_webhook(config.discord_webhook_losses),
        is_valid_webhook(config.discord_webhook_outages),
    ])

    if not any_valid:
        console.print("[yellow]No valid Discord webhooks configured.[/yellow]")
        console.print()
        display_setup_instructions()
        return

    # Run tests
    console.print("[bold]Sending test notifications...[/bold]")
    console.print()

    results = {
        "pnl": test_pnl_notification(notifier),
        "losses": test_loss_notification(notifier),
        "outages": test_outage_notification(notifier),
    }

    # Display summary
    display_summary(results)

    # Show setup instructions if any failed
    if not all(results.values()):
        console.print("[dim]Some webhooks not configured. See setup instructions below.[/dim]")
        console.print()
        display_setup_instructions()


if __name__ == "__main__":
    main()
