# Discord Bookkeeping Bot

Complete bookkeeping for a German sole proprietorship (small business under the *Kleinunternehmerregelung*, §19 UStG), operated entirely through **Discord slash commands**. I built it for my own reselling business and use it every day. It holds 300+ journal entries for the 2026 tax year.

> This is a sanitized public copy. All personal and business data (token, address, tax number, bank details, journal, receipts) has been removed. Configuration lives in a local `.env` file (see `.env.example`).

## Features

- **Journal with sequential IDs.** Income, expenses, private withdrawals, platform fees and shipping costs are recorded, and the net profit per sale is calculated automatically.
- **GoBD-compliant corrections.** Entries are never edited or deleted. Mistakes are fixed with reversal bookings (`/storno_*`), as German bookkeeping rules require.
- **Inventory management.** Stock is tracked with autocomplete in all commands. Sales reduce stock automatically, including multi-item sales of up to 5 products.
- **PDF documents.** The bot generates invoices, receipts and cancellation documents with fpdf2, including the legally required small-business notice. It can also create them retroactively for existing entries.
- **Receipt handling.** Photos and PDFs can be attached to any entry, and `/beleg_check` lists expenses that still have no receipt.
- **Tax preparation.** The bot offers an EÜR (income statement) summary grouped by tax-form line, exports for the cash book and private withdrawals, and an ELSTER preparation report.
- **Small-business threshold dashboard.** `/stats` tracks revenue against the §19 UStG limits and warns early before they are reached.
- **Optional bank reconciliation.** `n26_sync.py` reads transactions via the GoCardless Bank Account Data API (read-only) and matches them against journal entries.
- **Backups.** Every change is written atomically, and daily backups go to a local folder plus an optional second (cloud) location.

## Commands

`einnahme` · `einnahme_multi` · `ausgabe` · `privatentnahme` · `storno_einnahme` · `storno_einnahme_multi` · `storno_ausgabe` · `lager` · `lager_add` · `lager_change` · `last_sales` · `last_expenses` · `journal_suche` · `stats` · `rechnung_nachtraeglich` · `quittung_nachtraeglich` · `beleg_nachtraeglich` · `beleg_check` · `export_euer` · `euer_summary` · `elster_vorbereitung` · `export_kassenbuch` · `export_privatentnahmen` · `kontoabgleich`

The command names and the user interface are in German, because the tool maps German tax concepts.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in the bot token and business details
python bookkeeping_bot.py
```

Tech: Python 3, discord.py 2 (app commands, autocomplete, UI buttons and modals), fpdf2, python-dotenv, GoCardless API.

## Disclaimer

This bot helps with record-keeping. It is not tax advice.

## License

MIT
