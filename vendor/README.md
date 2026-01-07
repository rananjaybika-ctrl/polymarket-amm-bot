# Vendored Dependencies

## SECURITY WARNING

**DO NOT UPDATE `polymarket_apis` without auditing the new code first!**

This directory contains a frozen copy of `polymarket-apis==0.4.3` that was:
- Verified safe on 2024-12-23
- Copied from PyPI to disconnect from remote updates
- Reviewed for private key handling (keys stay local, never transmitted)

## Before Updating

If you ever need to update this package:

1. **Download new version to temp location:**
   ```bash
   pip download polymarket-apis -d /tmp/audit
   unzip /tmp/audit/polymarket_apis-*.whl -d /tmp/audit/extracted
   ```

2. **Audit the code for:**
   ```bash
   # Check for suspicious network calls with private key
   grep -rn "private_key" /tmp/audit/extracted/ | grep -v "from_key\|sign"

   # Check what data is sent over network
   grep -rn "\.post\|\.get\|requests\." /tmp/audit/extracted/
   ```

3. **Compare with current version:**
   ```bash
   diff -r vendor/polymarket_apis /tmp/audit/extracted/polymarket_apis
   ```

4. **Only then copy to vendor:**
   ```bash
   rm -rf vendor/polymarket_apis
   cp -r /tmp/audit/extracted/polymarket_apis vendor/
   ```

## Current Version

- Package: `polymarket-apis`
- Version: `0.4.3`
- Source: https://github.com/qualiaenjoyer/polymarket-apis
- Vendored: 2024-12-23
- Audited by: User + Claude Code review

## What Was Verified

- Private key used only for local signing (never transmitted)
- HTTP calls send only: signatures, public addresses, signed tx data
- No suspicious exfiltration of secrets
