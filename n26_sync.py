"""
N26-Kontoabgleich ueber GoCardless Bank Account Data (ex-Nordigen).

N26 selbst vergibt keine API-Zugaenge an Privatpersonen (die PSD2-Schnittstelle
verlangt ein QWAC-Zertifikat als lizenzierter Zahlungsdienstleister). GoCardless
Bank Account Data ist ein lizenzierter Aggregator mit kostenlosem Tarif fuer
genau diesen Lese-Anwendungsfall.

EINMALIGES SETUP (muss der Kontoinhaber selbst machen, nicht automatisierbar):
  1. Kostenlosen Account anlegen: https://bankaccountdata.gocardless.com/
  2. Im Dashboard unter "User secrets" einen Secret ID + Secret Key erzeugen.
  3. In dieses Verzeichnis eine Datei ".env" anlegen mit:
       GOCARDLESS_SECRET_ID=...
       GOCARDLESS_SECRET_KEY=...
  4. `python n26_sync.py setup` ausfuehren -> gibt einen Consent-Link aus.
  5. Diesen Link im Browser oeffnen und einmalig per N26-Login bestaetigen.
  6. Die dabei ausgegebene REQUISITION_ID ebenfalls in die .env eintragen:
       GOCARDLESS_REQUISITION_ID=...

DANACH:
  `python n26_sync.py sync` (oder der Discord-Befehl /kontoabgleich) ruft
  read-only die letzten Kontobewegungen ab und gleicht sie grob gegen die
  Journalbuchungen mit zahlungsart == "N26" ab.

Es werden ausschliesslich Lesezugriffe (Kontoumsaetze) genutzt - keine
Ueberweisungen, keine Aenderungen am Konto.
"""
import os
import sys
import json
from datetime import datetime, timedelta

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_BASE = "https://bankaccountdata.gocardless.com/api/v2"
SECRET_ID = os.environ.get("GOCARDLESS_SECRET_ID")
SECRET_KEY = os.environ.get("GOCARDLESS_SECRET_KEY")
REQUISITION_ID = os.environ.get("GOCARDLESS_REQUISITION_ID")
REDIRECT_URL = os.environ.get("GOCARDLESS_REDIRECT_URL", "https://example.com/gocardless-callback")

DATA_FILE = "business_data.json"


class GoCardlessNotConfigured(Exception):
    pass


def _require_credentials():
    if not SECRET_ID or not SECRET_KEY:
        raise GoCardlessNotConfigured(
            "GOCARDLESS_SECRET_ID / GOCARDLESS_SECRET_KEY fehlen. "
            "Siehe Docstring oben in n26_sync.py fuer das einmalige Setup."
        )


def get_access_token():
    _require_credentials()
    resp = requests.post(f"{API_BASE}/token/new/", json={
        "secret_id": SECRET_ID,
        "secret_key": SECRET_KEY,
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["access"]


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def find_n26_institution(token, country="DE"):
    resp = requests.get(
        f"{API_BASE}/institutions/",
        params={"country": country},
        headers=_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    for inst in resp.json():
        if "n26" in inst["id"].lower() or "n26" in inst["name"].lower():
            return inst
    return None


def create_requisition(token, institution_id):
    resp = requests.post(
        f"{API_BASE}/requisitions/",
        json={
            "redirect": REDIRECT_URL,
            "institution_id": institution_id,
            "reference": f"buchhaltung2026-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        },
        headers=_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def setup_flow():
    print("Hole Access-Token...")
    token = get_access_token()

    print("Suche N26 in der Institutionsliste (DE)...")
    institution = find_n26_institution(token)
    if not institution:
        print("N26 wurde nicht gefunden - bitte pruefen, ob GoCardless N26 aktuell unterstuetzt.")
        return
    print(f"Gefunden: {institution['name']} ({institution['id']})")

    print("Erstelle Requisition (Consent-Anfrage)...")
    requisition = create_requisition(token, institution["id"])

    print()
    print("=" * 70)
    print("NAECHSTER SCHRITT (einmalig, muss der Kontoinhaber selbst tun):")
    print(f"1. Diesen Link im Browser oeffnen und mit N26-Login bestaetigen:\n")
    print(f"   {requisition['link']}\n")
    print("2. Danach diese Zeile in die .env-Datei eintragen:")
    print(f"   GOCARDLESS_REQUISITION_ID={requisition['id']}")
    print("=" * 70)


def get_account_ids(token):
    if not REQUISITION_ID:
        raise GoCardlessNotConfigured(
            "GOCARDLESS_REQUISITION_ID fehlt. Erst `python n26_sync.py setup` ausfuehren "
            "und den Consent-Link bestaetigen."
        )
    resp = requests.get(
        f"{API_BASE}/requisitions/{REQUISITION_ID}/",
        headers=_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "LN":
        raise RuntimeError(
            f"Zugriff noch nicht bestaetigt (Status: {data.get('status')}). "
            "Bitte erst den Consent-Link aus `setup` im Browser abschliessen."
        )
    return data["accounts"]


def fetch_transactions(days=30):
    token = get_access_token()
    account_ids = get_account_ids(token)

    all_transactions = []
    for account_id in account_ids:
        resp = requests.get(
            f"{API_BASE}/accounts/{account_id}/transactions/",
            headers=_headers(token),
            timeout=30,
        )
        resp.raise_for_status()
        booked = resp.json().get("transactions", {}).get("booked", [])
        all_transactions.extend(booked)

    cutoff = datetime.now() - timedelta(days=days)
    recent = [
        t for t in all_transactions
        if datetime.strptime(t["bookingDate"], "%Y-%m-%d") >= cutoff
    ]
    return recent


def load_journal_n26_entries():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data.get("euer", []) if e.get("zahlungsart", "").lower() == "n26"]


# Vorzeichen, mit dem ein Buchungstyp tatsaechlich auf dem Kontoauszug erscheinen muss.
# Im Journal ist das Vorzeichen von "betrag" uneinheitlich (Einnahme/Ausgabe positiv,
# STORNO/Ausgabe-STORNO/Privatentnahme negativ) - fuer den Bankabgleich zaehlt aber nur,
# ob das Geld tatsaechlich rein- oder rausgeht.
EXPECTED_BANK_SIGN = {
    "Einnahme": 1,          # Geld kommt rein
    "Ausgabe": -1,          # Geld geht raus
    "STORNO": -1,           # Rueckzahlung an Kunde - Geld geht raus
    "Ausgabe-STORNO": 1,    # Gutschrift vom Lieferanten - Geld kommt rein
    "Privatentnahme": -1,   # Geld geht raus (auf das Privatkonto)
}


def expected_bank_amount(entry):
    sign = EXPECTED_BANK_SIGN.get(entry.get("typ", ""), 1)
    return sign * abs(entry.get("betrag", 0.0))


def match_transactions(bank_transactions, journal_entries, tolerance_days=3):
    """Grober Abgleich per (vorzeichenrichtigem) Betrag + Datumsnaehe. Kein
    Buchhaltungsersatz, nur ein Hinweis auf moeglicherweise fehlende/doppelte Buchungen."""
    unmatched_bank = []
    used_journal_ids = set()

    for tx in bank_transactions:
        amount = float(tx["transactionAmount"]["amount"])
        tx_date = datetime.strptime(tx["bookingDate"], "%Y-%m-%d")

        match = None
        for entry in journal_entries:
            if entry["journal_id"] in used_journal_ids:
                continue
            if abs(expected_bank_amount(entry) - amount) > 0.01:
                continue
            try:
                entry_date = datetime.strptime(entry["datum"][:10], "%d.%m.%Y")
            except ValueError:
                continue
            if abs((entry_date - tx_date).days) <= tolerance_days:
                match = entry
                break

        if match:
            used_journal_ids.add(match["journal_id"])
        else:
            unmatched_bank.append(tx)

    unmatched_journal = [e for e in journal_entries if e["journal_id"] not in used_journal_ids]
    return unmatched_bank, unmatched_journal


def sync_flow(days=30):
    print(f"Hole N26-Kontobewegungen der letzten {days} Tage...")
    transactions = fetch_transactions(days=days)
    journal_entries = load_journal_n26_entries()

    unmatched_bank, unmatched_journal = match_transactions(transactions, journal_entries)

    print(f"\n{len(transactions)} Kontobewegungen, {len(journal_entries)} N26-Journalbuchungen.")
    print(f"\n⚠️ {len(unmatched_bank)} Kontobewegungen ohne passende Buchung:")
    for tx in unmatched_bank:
        amount = tx["transactionAmount"]["amount"]
        info = tx.get("remittanceInformationUnstructured", "-")
        print(f"  {tx['bookingDate']}  {amount} EUR  {info}")

    print(f"\n⚠️ {len(unmatched_journal)} Journalbuchungen ohne passende Kontobewegung:")
    for e in unmatched_journal:
        print(f"  {e['datum']}  {e['betrag']} EUR  {e['produkt']}  ({e['journal_id']})")

    return unmatched_bank, unmatched_journal


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("setup", "sync"):
        print("Nutzung: python n26_sync.py [setup|sync]")
        sys.exit(1)

    try:
        if sys.argv[1] == "setup":
            setup_flow()
        else:
            sync_flow()
    except (GoCardlessNotConfigured, RuntimeError) as e:
        print(f"❌ {e}")
    except requests.HTTPError as e:
        print(f"❌ GoCardless-API-Fehler: {e.response.status_code} {e.response.text}")
