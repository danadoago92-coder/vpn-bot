# Telegram VPN Sales Bot

[فارسی](README.fa.md) | **English**

A Persian Telegram bot for selling VPN subscriptions. Locations, protocols, plans, prices and delivery methods are managed directly from the Telegram admin panel.

## Features

- Custom countries and multi-location services
- Standard subscription links, WireGuard and OpenVPN
- One-month and two-month plans
- Card-to-card payments and wallet balance
- Rotating bank cards
- Manual delivery, ready inventory and optional 3x-ui delivery
- Admin, customer, receipt and tutorial management
- Custom brand name and messages

## Quick installation

Requirements: Linux, Python 3.11 or newer, and internet access to Telegram and PyPI.

```bash
git clone https://github.com/danadoago92-coder/vpn-bot.git
cd vpn-bot
bash install.sh
bash run.sh
```

The installer asks for the bot token, numeric admin ID, brand name, payment details, support username, Telegram proxy and optional 3x-ui settings.

## Run with systemd

```bash
sudo bash install.sh --systemd --target /opt/vpn-bot
```

Useful commands:

```bash
sudo systemctl status vpn-bot
sudo systemctl restart vpn-bot
sudo journalctl -u vpn-bot -n 50 --no-pager
```

## First setup

Send `/admin` to the bot, then:

1. Open **Locations & Protocols**.
2. Add a country or a multi-location service with any name and emoji.
3. Enable the protocols available for that location.
4. Open **Plan Management** and create your prices, traffic limits and durations.
5. Choose manual, inventory or automatic delivery.

A fresh installation contains no sample country, plan or customer data.

## Delivery methods

- **Manual:** after payment approval, the admin sends the subscription link or configuration file.
- **Inventory:** prepared subscription links are delivered one at a time.
- **Automatic:** the bot creates a standard subscription through 3x-ui.

WireGuard uses `.conf` files and OpenVPN uses `.ovpn` files. Automatic 3x-ui delivery currently applies to standard subscription plans.

## Generated private files

The installer or first run creates:

- `.env` — tokens and private settings
- `admins.json` — Telegram admin IDs
- `data/bot.sqlite3` — users, orders, plans and services
- `backups/` — database backups

These files are ignored by Git and must not be uploaded to GitHub.
