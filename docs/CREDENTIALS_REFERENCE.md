# Credentials Reference (Test Wallet)

**Purpose**: Reference for all addresses and API keys used with the TEST wallet.
**When switching to main wallet**: Update `.env` with new values.

---

## Wallet Credentials

### Private Key & Addresses
| Item | Value |
|------|-------|
| **Private Key** | `0x83d19e0038476ba6b2dc1dbdbb2315d268fb7e5b7891179b305632b41bc8c26b` |
| **Wallet Type** | `magic` |
| **Funder Address (Magic Proxy)** | `0x1404341D718bbd4e5683877fa57f1249016B8989` |
| **EOA Signer (derived)** | `0xc22edB57ef0eB97B3fa7baC7B440e8C9FfA2D299` |
| **Builder Safe (deployed)** | `0xeCf99c5f646dEe86B4Bca1C33F013a8ACe6c0dbB` |

---

## Builder Relayer API (Gasless Redemptions)
*Get from: https://polymarket.com/settings?tab=builder*

| Item | Value |
|------|-------|
| **API Key** | `019b63c2-92e1-7538-ac72-a9ac60aaefc7` |
| **Secret** | `LUnnUbmL7Y9HcZQm-5F8jUnWt-VO67rQbud8XUNR3Wc=` |
| **Passphrase** | `5b71f3b502ef941a3d751b6d6020ff533db5e00e17dba81e4997858270425325` |

---

## Discord Notifications

| Channel | Webhook URL |
|---------|-------------|
| **PNL Summary** | `https://discord.com/api/webhooks/1451251845660934299/fJ16p16SrlABNbO_9uL_l0kxxsLL4PPx-l1qGPmAbL3OKQ-TaZqn8YafEFSz19OsQFVc` |
| **Losses** | `https://discord.com/api/webhooks/1451252208665493686/UVLnFh2tHkFW2N5bFHkmzG1o_jmQGh_0vg26-8lpx60S9NRnSEh0gmQfgZsJ_RFQt6lE` |
| **Outages** | `https://discord.com/api/webhooks/1451252352706416710/2f2t1xBuXQIGa_ZtjHGGI-uUMf-GR1aplpVhbhmFed6TVafMtyfAwkWEvxVHhCRiOvQu` |
| **User ID** | `396707931845951498` |

---

## Telegram Notifications

| Item | Value |
|------|-------|
| **Bot Token** | `8547380856:AAHrDS4zKnWLI_0vR90bCiCe4BXnA0oooiQ` |
| **Chat ID** | `5910701388` |

---

## WiFi Configuration

| Network | Password |
|---------|----------|
| **PRIMARY**: RSBIKA_5G | BIKA@112 |
| **BACKUP 1**: BIKA | BIKA@112 |
| **BACKUP 2**: RSBIKA | BIKA@112 |

---

## What to Change for Main Wallet

When switching to your main Polymarket account, update these in `.env`:

1. **Required**:
   - `WALLET_PRIVATE_KEY` - Your main wallet's private key
   - `FUNDER_ADDRESS` - Your main wallet's proxy address (from Polymarket)

2. **Required for Builder Relayer** (get new ones from polymarket.com/settings?tab=builder):
   - `BUILDER_API_KEY`
   - `BUILDER_SECRET`
   - `BUILDER_PASSPHRASE`

3. **Optional** (can keep same if notifications go to same place):
   - Discord webhooks
   - Telegram bot

4. **After switching**:
   - Deploy new Builder Safe: `python -c "from scripts.redeem_winnings import *; ..."`
   - Or just use auto-redemption (Polymarket auto-redeems after ~5 min)

---

*Last updated: 2025-12-28*
