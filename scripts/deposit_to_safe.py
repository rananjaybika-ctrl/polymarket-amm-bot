#!/usr/bin/env python3
"""
Deposit USDC.e from EOA to Gnosis Safe (non-interactive).

Usage:
    python scripts/deposit_to_safe.py <amount_usdc>
    python scripts/deposit_to_safe.py all

Examples:
    python scripts/deposit_to_safe.py 50      # Send $50 USDC.e
    python scripts/deposit_to_safe.py all     # Send entire balance
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
load_dotenv(project_root / ".env")


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/deposit_to_safe.py <amount_usdc|all>")
        print("Example: python scripts/deposit_to_safe.py 50")
        sys.exit(1)

    amount_arg = sys.argv[1].lower()

    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    # Config
    private_key = os.getenv("WALLET_PRIVATE_KEY")
    safe_address = os.getenv("SAFE_ADDRESS")
    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

    if not private_key or not safe_address:
        print("ERROR: Missing WALLET_PRIVATE_KEY or SAFE_ADDRESS in .env")
        sys.exit(1)

    # Connect
    w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    account = w3.eth.account.from_key(private_key)
    eoa_address = account.address

    # Check balances
    usdc_abi = [
        {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
        {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"}
    ]
    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=usdc_abi)

    eoa_balance_wei = usdc.functions.balanceOf(eoa_address).call()
    eoa_balance = eoa_balance_wei / 1_000_000

    if eoa_balance < 0.01:
        print(f"EOA has insufficient USDC.e: ${eoa_balance:.2f}")
        sys.exit(1)

    # Determine amount
    if amount_arg == "all":
        amount_usdc = eoa_balance
    else:
        amount_usdc = float(amount_arg)
        if amount_usdc > eoa_balance:
            print(f"ERROR: Requested ${amount_usdc:.2f} but EOA only has ${eoa_balance:.2f}")
            sys.exit(1)

    amount_wei = int(amount_usdc * 1_000_000)

    print(f"Depositing ${amount_usdc:.2f} USDC.e to Safe...")
    print(f"  From: {eoa_address}")
    print(f"  To:   {safe_address}")

    # Build and send transaction
    tx = usdc.functions.transfer(
        Web3.to_checksum_address(safe_address),
        amount_wei
    ).build_transaction({
        'from': eoa_address,
        'gas': 100000,
        'gasPrice': int(w3.eth.gas_price * 1.2),
        'nonce': w3.eth.get_transaction_count(eoa_address),
        'chainId': 137
    })

    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

    print(f"Tx sent: {tx_hash.hex()}")

    # Wait for confirmation
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

    if receipt['status'] == 1:
        print(f"SUCCESS! ${amount_usdc:.2f} deposited to Safe")
        print(f"https://polygonscan.com/tx/{tx_hash.hex()}")
    else:
        print(f"FAILED! Transaction reverted")
        sys.exit(1)


if __name__ == "__main__":
    main()
