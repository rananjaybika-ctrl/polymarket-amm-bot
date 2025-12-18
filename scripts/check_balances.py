#!/usr/bin/env python3
"""
Check all balances - wallet and Polymarket.

This helps debug why funds might not be showing up.
"""

import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from rich.console import Console
from rich.panel import Panel
from web3 import Web3
from eth_account import Account

from src.config import Config

console = Console()

# USDC contract on Polygon
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]


async def check_balances():
    """Check all balance sources."""
    console.print(Panel.fit(
        "[bold blue]Balance Diagnostic Tool[/bold blue]",
        border_style="blue"
    ))
    console.print()

    # Load config
    try:
        config = Config()
        config.validate()
    except Exception as e:
        console.print(f"[red]Config error: {e}[/red]")
        return

    # Get wallet address
    account = Account.from_key(config.wallet_private_key)
    wallet_address = account.address
    console.print(f"[bold]Wallet Address:[/bold] {wallet_address}")
    console.print()

    # Check 1: Raw USDC balance on Polygon (in your wallet)
    console.print("[bold]1. USDC in Wallet (Polygon Network)[/bold]")
    try:
        w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
        if not w3.is_connected():
            console.print("[red]   Cannot connect to Polygon RPC[/red]")
        else:
            usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_CONTRACT), abi=USDC_ABI)
            raw_balance = usdc.functions.balanceOf(Web3.to_checksum_address(wallet_address)).call()
            usdc_balance = raw_balance / 1_000_000  # USDC has 6 decimals
            console.print(f"   [green]USDC Balance: ${usdc_balance:.2f}[/green]")

            if usdc_balance == 0:
                console.print("   [yellow]No USDC found in wallet on Polygon[/yellow]")
                console.print("   [dim]Make sure you have USDC on Polygon network, not Ethereum[/dim]")
    except Exception as e:
        console.print(f"   [red]Error checking wallet: {e}[/red]")
    console.print()

    # Check 2: MATIC balance (for gas)
    console.print("[bold]2. MATIC Balance (for gas fees)[/bold]")
    try:
        w3 = Web3(Web3.HTTPProvider(config.polygon_rpc_url))
        matic_balance = w3.eth.get_balance(Web3.to_checksum_address(wallet_address))
        matic = matic_balance / 1e18
        console.print(f"   [green]MATIC Balance: {matic:.4f}[/green]")

        if matic < 0.1:
            console.print("   [yellow]Low MATIC - you need some for gas fees[/yellow]")
    except Exception as e:
        console.print(f"   [red]Error: {e}[/red]")
    console.print()

    # Check 3: Polymarket trading balance
    console.print("[bold]3. Polymarket Trading Balance[/bold]")
    console.print("   [dim]This is USDC deposited INTO Polymarket for trading[/dim]")
    try:
        from src.api.polymarket_client import PolymarketClient
        client = PolymarketClient(config)
        await client.connect()
        pm_balance = await client.get_balance()
        console.print(f"   [green]Polymarket Balance: ${pm_balance:.2f}[/green]")

        if pm_balance == 0:
            console.print()
            console.print("   [yellow]No funds deposited to Polymarket yet![/yellow]")
            console.print()
            console.print("   [bold]To deposit funds:[/bold]")
            console.print("   1. Go to https://polymarket.com")
            console.print("   2. Connect your wallet")
            console.print("   3. Click 'Deposit' and transfer USDC")
            console.print("   4. Wait for transaction to confirm")
            console.print("   5. Run this script again")

        await client.disconnect()
    except Exception as e:
        console.print(f"   [red]Error: {e}[/red]")
    console.print()

    # Summary
    console.print(Panel.fit(
        "[bold]Summary[/bold]\n\n"
        "• Wallet USDC = funds in your MetaMask on Polygon\n"
        "• Polymarket Balance = funds deposited for trading\n\n"
        "You need to deposit USDC into Polymarket before trading.",
        border_style="blue"
    ))


def main():
    asyncio.run(check_balances())


if __name__ == "__main__":
    main()
