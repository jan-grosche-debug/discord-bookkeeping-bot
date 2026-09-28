"""
Einmalige Migration: bringt jeden Eintrag in bot.data["euer"] auf eine einheitliche
Feld-Reihenfolge/-Menge, damit business_data.json von Hand lesbar bleibt, egal
welcher (alte oder neue) Befehl den Eintrag erzeugt hat.

Nach diesem Lauf schreibt Business.py (siehe make_journal_entry) neue Eintraege
automatisch schon in genau dieser Form - die Migration muss nur einmal laufen.
"""
import json
import os
import shutil
from datetime import datetime

DATA_FILE = 'business_data.json'
FIELD_ORDER = [
    "journal_id", "datum", "typ", "produkt", "menge", "betrag",
    "kategorie", "zahlungsart", "herkunft", "gebuehr", "versand_ausgabe", "netto_gewinn",
    "rechnungsnummer", "quittungsnummer", "beleg_pfad",
]
DEFAULTS = {
    "journal_id": "", "datum": "", "typ": "", "produkt": "", "menge": 0, "betrag": 0.0,
    "kategorie": "", "zahlungsart": "", "herkunft": "", "gebuehr": 0.0,
    "versand_ausgabe": 0.0, "netto_gewinn": 0.0,
    "rechnungsnummer": "", "quittungsnummer": "", "beleg_pfad": "",
}


def normalize_entry(entry):
    normalized = {}
    for key in FIELD_ORDER:
        value = entry.get(key, DEFAULTS[key])
        if key == "netto_gewinn" and "netto_gewinn" not in entry:
            # Gleiche Formel wie in Business.py's make_journal_entry().
            value = entry.get("betrag", 0.0) - entry.get("gebuehr", 0.0) - entry.get("versand_ausgabe", 0.0)
        if key in ("menge",) and value is None:
            value = 0
        if key in ("betrag", "gebuehr", "versand_ausgabe", "netto_gewinn") and value is None:
            value = 0.0
        if key in ("rechnungsnummer", "quittungsnummer", "beleg_pfad") and value is None:
            value = ""
        normalized[key] = value
    return normalized


def migrate():
    if not os.path.exists(DATA_FILE):
        print(f"Datei {DATA_FILE} nicht gefunden!")
        return

    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    backup_dir = "Backups"
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, f"business_data_vor_normalisierung_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.json")
    shutil.copy2(DATA_FILE, backup_path)
    print(f"Sicherheitskopie erstellt: {backup_path}")

    euer = data.get("euer", [])
    extra_keys_found = set()
    for entry in euer:
        extra_keys_found.update(k for k in entry.keys() if k not in FIELD_ORDER)

    data["euer"] = [normalize_entry(e) for e in euer]

    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, DATA_FILE)

    print(f"Normalisierung abgeschlossen: {len(euer)} Eintraege auf einheitliches Schema gebracht.")
    if extra_keys_found:
        print(f"Hinweis: folgende bisherige Zusatzfelder wurden entfernt (waren nirgends im Code ausgewertet): {sorted(extra_keys_found)}")


if __name__ == '__main__':
    migrate()
