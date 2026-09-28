import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from datetime import datetime
import json
import os
import csv
import logging
import shutil
import hashlib

try:
    import n26_sync  # optional bank sync
except ImportError:
    n26_sync = None

# --- DEINE DATEN (aus .env, nie im Code) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MEIN_NAME = os.environ.get("BUSINESS_NAME", "Max Mustermann")
MEINE_STRASSE = os.environ.get("BUSINESS_STREET", "Musterstr. 1")
MEINE_STADT = os.environ.get("BUSINESS_CITY", "12345 Musterstadt")
MEINE_MAIL = os.environ.get("BUSINESS_EMAIL", "mail@example.com")
MEINE_STEUERNUMMER = os.environ.get("BUSINESS_TAX_NUMBER", "")
MEINE_IBAN = os.environ.get("BUSINESS_IBAN", "")
MEINE_BIC = os.environ.get("BUSINESS_BIC", "")
PAYPAL_ME = os.environ.get("BUSINESS_PAYPAL_ME", "")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("DISCORD_BOT_TOKEN fehlt – bitte .env anlegen (Vorlage: .env.example).")

DATA_FILE = 'business_data.json'
RECHNUNG_ORDNER = 'Rechnungen'
QUITTUNG_ORDNER = 'Eigenbelege'
STORNO_ORDNER = 'Storno'
BELEG_ORDNER = 'Belege'

# Erlaubte Dateitypen fuer Beleg-Anhaenge (Lieferantenrechnungen etc.)
ERLAUBTE_BELEG_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif"}

# Zusaetzliches Backup-Ziel auf Google Drive (Ordner "Buchhaltung2026_Backups" in "Meine Ablage").
# Wird uebersprungen, falls der Drive-Client auf diesem Rechner nicht laeuft/gemountet ist.
GDRIVE_BACKUP_DIR = os.environ.get("BACKUP_DIR", "")

# Ordner erstellen falls nicht vorhanden
for folder in [RECHNUNG_ORDNER, QUITTUNG_ORDNER, STORNO_ORDNER, BELEG_ORDNER]:
    if not os.path.exists(folder):
        os.makedirs(folder)

def gdrive_mounted():
    """True, wenn der konkrete Google-Drive-Ordner 'Meine Ablage' tatsaechlich gemountet ist
    (nicht nur irgendein Laufwerk auf G:)."""
    return os.path.isdir(os.path.dirname(GDRIVE_BACKUP_DIR))

def gdrive_mirror(local_path, unterordner=""):
    """Spiegelt eine lokale Datei ins Google-Drive-Backup (optional in einen Unterordner).
    Wird still uebersprungen, wenn Drive nicht gemountet ist oder die Datei fehlt."""
    if not gdrive_mounted() or not os.path.exists(local_path):
        return
    try:
        ziel_dir = os.path.join(GDRIVE_BACKUP_DIR, unterordner) if unterordner else GDRIVE_BACKUP_DIR
        os.makedirs(ziel_dir, exist_ok=True)
        shutil.copy2(local_path, os.path.join(ziel_dir, os.path.basename(local_path)))
    except OSError as e:
        print(f"⚠️ Google-Drive-Spiegelung uebersprungen: {e}")

def gdrive_remove_mirror(local_path, unterordner=""):
    """Entfernt die gespiegelte Kopie einer lokalen Datei aus dem Google-Drive-Backup."""
    if not gdrive_mounted():
        return
    try:
        ziel_dir = os.path.join(GDRIVE_BACKUP_DIR, unterordner) if unterordner else GDRIVE_BACKUP_DIR
        ziel = os.path.join(ziel_dir, os.path.basename(local_path))
        if os.path.exists(ziel):
            os.remove(ziel)
    except OSError:
        pass

class BusinessBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True 
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.data = self.load_data()

    def load_data(self):
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Fehler beim Laden der Datei: {e}")
        return {
            "config": {"next_invoice_number": 106, "next_receipt_number": 1001},
            "lager": [],
            "euer": []
        }

    def save_data(self):
        backup_dir = "Backups"
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)

        backup_name = os.path.join(backup_dir, f"business_data_{datetime.now().strftime('%Y-%m-%d')}.json")
        if os.path.exists(DATA_FILE) and not os.path.exists(backup_name):
            shutil.copy2(DATA_FILE, backup_name)

        # Atomar schreiben: erst in Temp-Datei, dann austauschen. So bleibt bei einem
        # Absturz mitten im Schreibvorgang immer noch die alte, intakte Datei erhalten.
        tmp_path = DATA_FILE + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, DATA_FILE)

        # Zusaetzliche Kopie des Tages-Backups auf Google Drive, falls gemountet.
        # (gdrive_mirror achtet selbst darauf, nur den echten "Meine Ablage"-Ordner zu nutzen.)
        if os.path.exists(backup_name):
            gdrive_target = os.path.join(GDRIVE_BACKUP_DIR, os.path.basename(backup_name))
            if not os.path.exists(gdrive_target):
                gdrive_mirror(backup_name)

    async def setup_hook(self):
        await self.tree.sync()
        print(f"Eingeloggt als {self.user} - Bereit.")

bot = BusinessBot()

# --- GLOBALE CHOICES ---
ZAHLUNGSARTEN = [
    app_commands.Choice(name="Bar", value="Bar"),
    app_commands.Choice(name="PayPal", value="PayPal"),
    app_commands.Choice(name="Vivid Konto", value="Vivid"),
    app_commands.Choice(name="Privat Konto", value="Privat"),
    app_commands.Choice(name="N26-Konto", value="N26")
]

PLATTFORMEN = [
    app_commands.Choice(name="B2B", value="B2B"),
    app_commands.Choice(name="Bar (mit Quittung)", value="Bar (mit Quittung)"),
    app_commands.Choice(name="eBay", value="eBay"),
    app_commands.Choice(name="Kleinanzeigen", value="Kleinanzeigen"),
    app_commands.Choice(name="Vinted", value="Vinted"),
    app_commands.Choice(name="Sonstiges", value="Sonstiges")
]

KATEGORIEN = [
    app_commands.Choice(name="Wareneinkauf", value="Wareneinkauf"),
    app_commands.Choice(name="Porto / Versand", value="Porto"),
    app_commands.Choice(name="Büromaterial / Verpackung", value="Material"),
    app_commands.Choice(name="Gebühren (Plattform/Bank)", value="Gebühren"),
    app_commands.Choice(name="Sonstiges", value="Sonstiges")
]

# --- HELFER FUNKTIONEN ---
# Discord erlaubt fuer Autocomplete-Optionen max. 100 Zeichen in name UND value.
# Lange Lagernamen (z.B. Amazon-Titel mit ASIN) werden deshalb als kurzer Schluessel
# uebergeben und im Befehl per resolve_produkt() zurueck in den vollen Namen uebersetzt.
DISCORD_CHOICE_MAX = 100
LAGER_KEY_PREFIX = "lager#"

def lager_key(name: str) -> str:
    return LAGER_KEY_PREFIX + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]

def resolve_produkt(value):
    """Uebersetzt einen Autocomplete-Schluessel zurueck in den vollen Lagernamen.
    Normale Namen/Freitext werden unveraendert zurueckgegeben."""
    if isinstance(value, str) and value.startswith(LAGER_KEY_PREFIX):
        for item in bot.data.get("lager", []):
            if lager_key(str(item.get("name", ""))) == value:
                return item["name"]
    return value

def kuerzen_fuer_discord(text: str, suffix: str = "") -> str:
    """Kuerzt text so, dass text+suffix <= 100 Zeichen ist. Das Ende des Namens bleibt
    sichtbar (unterscheidet z.B. Dubletten mit/ohne ASIN)."""
    voll = f"{text}{suffix}"
    if len(voll) <= DISCORD_CHOICE_MAX:
        return voll
    platz = DISCORD_CHOICE_MAX - len(suffix) - 1  # 1 Zeichen fuer "…"
    ende = min(20, platz // 3)
    return f"{text[:platz - ende]}…{text[-ende:]}{suffix}"

async def product_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    choices = []
    if current:
        # Freitext >100 Zeichen kann Discord nicht als Option zurueckgeben -> gekuerzt anbieten
        choices.append(app_commands.Choice(
            name=kuerzen_fuer_discord(f'✍️ Freier Text: "{current}"'),
            value=current[:DISCORD_CHOICE_MAX]))

    lager_data = bot.data.get("lager", [])
    if not isinstance(lager_data, list): return choices

    for p in lager_data:
        name = str(p.get('name', 'Unbekannt'))
        qty = p.get('qty', 0)
        if name == 'Unbekannt': continue
        if current.lower() in name.lower():
            display_name = kuerzen_fuer_discord(name, f" ({qty} Stk)")
            value = name if len(name) <= DISCORD_CHOICE_MAX else lager_key(name)
            if name != current:
                choices.append(app_commands.Choice(name=display_name, value=value))
            if len(choices) >= 25: break
    return choices

def clean_text(text):
    if isinstance(text, str):
        return text.encode('latin-1', 'ignore').decode('latin-1')
    return text

def find_lager_item(produkt: str):
    """Sucht einen Lagerartikel case-insensitiv anhand des Namens."""
    for item in bot.data["lager"]:
        if item["name"].lower() == produkt.lower():
            return item
    return None

def lager_aufraeumen():
    """Entfernt Artikel mit Bestand <= 0 automatisch aus dem Lager (Nutzerentscheidung
    2026-07-04: ausverkaufte Artikel verschwinden von selbst, die Verkaufshistorie bleibt
    ja im Journal). Gibt die Namen der entfernten Artikel zurueck."""
    entfernt = [item["name"] for item in bot.data["lager"] if item.get("qty", 0) <= 0]
    if entfernt:
        bot.data["lager"] = [item for item in bot.data["lager"] if item.get("qty", 0) > 0]
    return entfernt

# Zahlungsarten, bei denen der Erloes sofort privat verwendet wird (kein Geld bleibt
# in der Firma) - fuer diese wird automatisch eine spiegelnde Privatentnahme gebucht.
PRIVATENTNAHME_ZAHLUNGSARTEN = {"bar", "privat"}

# Feste Feld-Reihenfolge fuer JEDEN Journaleintrag, damit business_data.json einheitlich
# und von Hand lesbar bleibt, egal welcher Befehl den Eintrag erzeugt hat.
def make_journal_entry(typ, produkt="", menge=0, betrag=0.0, kategorie="", zahlungsart="",
                        herkunft="", gebuehr=0.0, versand_ausgabe=0.0, netto_gewinn=None,
                        journal_id=None, datum=None):
    if journal_id is None:
        journal_id = bot.data.setdefault("config", {}).get("next_journal_number", 2026001)
        bot.data["config"]["next_journal_number"] = journal_id + 1
    if netto_gewinn is None:
        # Gleiche Formel wie frueher (betrag - gebuehr - versand_ausgabe), damit das Feld
        # ueberall dieselbe Bedeutung behaelt - ergibt betrag, solange keine Gebuehren/
        # Versandkosten mehr direkt an der Einnahme haengen (die werden jetzt separat gebucht).
        netto_gewinn = betrag - gebuehr - versand_ausgabe
    entry = {
        "journal_id": f"J-{journal_id}",
        "datum": datum or datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        "typ": typ,
        "produkt": produkt,
        "menge": menge,
        "betrag": betrag,
        "kategorie": kategorie,
        "zahlungsart": zahlungsart,
        "herkunft": herkunft,
        "gebuehr": gebuehr,
        "versand_ausgabe": versand_ausgabe,
        "netto_gewinn": netto_gewinn,
        "rechnungsnummer": "",
        "quittungsnummer": "",
        "beleg_pfad": "",
    }
    bot.data["euer"].append(entry)
    return entry

def find_journal_entry(journal_id: str):
    """Sucht einen Journaleintrag anhand seiner Journal-ID (mit oder ohne 'J-'-Prefix)."""
    gesucht = journal_id.strip().upper()
    if not gesucht.startswith("J-"):
        gesucht = f"J-{gesucht}"
    for entry in bot.data.get("euer", []):
        if entry.get("journal_id", "").upper() == gesucht:
            return entry
    return None

def entry_jahr(entry):
    """Extrahiert das Buchungsjahr aus dem datum-Feld ("DD.MM.YYYY HH:MM:SS").
    Ein echter Jahresvergleich statt Substring-Suche - "2025" irgendwo im String
    (z.B. in einer Uhrzeit oder Produktbezeichnung) zaehlt damit nicht mehr faelschlich."""
    datum = entry.get("datum", "")
    try:
        return int(datum[6:10])
    except (ValueError, IndexError):
        return None

class BelegError(Exception):
    """Wird geworfen, wenn ein Beleg-Anhang nicht gespeichert werden kann (z.B. falscher Dateityp)."""

async def save_beleg(attachment: discord.Attachment, journal_entry: dict) -> str:
    """Speichert einen hochgeladenen Beleg unter Belege/<journal_id>.<ext> und schreibt den
    Pfad in journal_entry['beleg_pfad']. Der Dateiname wird bewusst NICHT aus dem vom Nutzer
    kontrollierten attachment.filename gebildet (Path-Traversal-Schutz), sondern rein aus der
    Journal-ID plus der validierten Extension. Gibt den relativen Pfad zurueck."""
    ext = os.path.splitext(attachment.filename or "")[1].lower()
    if ext not in ERLAUBTE_BELEG_EXTENSIONS:
        erlaubt = ", ".join(sorted(ERLAUBTE_BELEG_EXTENSIONS))
        raise BelegError(f"Dateityp '{ext or '(keiner)'}' nicht erlaubt. Erlaubt: {erlaubt}")

    # Journal-ID ist vom Format J-<int>, enthaelt also keine Pfadtrenner - trotzdem defensiv absichern.
    safe_id = os.path.basename(journal_entry["journal_id"])
    filename = f"{safe_id}{ext}"
    path = os.path.join(BELEG_ORDNER, filename)
    await attachment.save(path)

    # Ersetzt ein alter Beleg mit ANDERER Endung existiert (z.B. .jpg -> .pdf), wuerde die alte
    # Datei sonst verwaisen. Nur loeschen, wenn sie innerhalb von Belege/ liegt und nicht die
    # gerade geschriebene Datei ist. Die gespiegelte Kopie auf Google Drive mit entfernen.
    alt_pfad = journal_entry.get("beleg_pfad")
    if alt_pfad and alt_pfad != path:
        try:
            if os.path.abspath(alt_pfad).startswith(os.path.abspath(BELEG_ORDNER)) and os.path.exists(alt_pfad):
                os.remove(alt_pfad)
        except OSError:
            pass
        gdrive_remove_mirror(alt_pfad, BELEG_ORDNER)

    # Beleg zusaetzlich ins Google-Drive-Backup spiegeln (Belege/-Unterordner), falls gemountet.
    gdrive_mirror(path, BELEG_ORDNER)

    journal_entry["beleg_pfad"] = path
    return path

def maybe_auto_privatentnahme(bezug_entry):
    """Bucht automatisch eine Privatentnahme, wenn eine Einnahme sofort privat war
    (Zahlungsart Bar oder Privat) - das Geld verbleibt dann nie in der Firma."""
    zahlungsart = bezug_entry.get("zahlungsart", "")
    if zahlungsart.lower() not in PRIVATENTNAHME_ZAHLUNGSARTEN:
        return None
    betrag = bezug_entry.get("betrag", 0.0)
    return make_journal_entry(
        typ="Privatentnahme",
        produkt=f"Automatische Privatentnahme zu {bezug_entry['journal_id']} ({bezug_entry.get('produkt', '')})",
        betrag=-abs(betrag),
        zahlungsart=zahlungsart,
        herkunft="Auto (Sofortentnahme bei Verkauf)",
    )

# --- KONTENRAHMEN / EUER-ZUORDNUNG ---
# Zeilennummern = amtliche Anlage EUER 2025 (per Multi-Agenten-Recherche verifiziert,
# siehe ELSTER-Anforderungen.md). Verbindlich ist immer das Live-Formular in Mein ELSTER -
# vor jeder Abgabe kurz gegenpruefen, die Nummerierung kann sich je Steuerjahr verschieben.
# "zeile" ist der maschinenlesbare Schluessel fuer /elster_vorbereitung.
EUER_MAPPING = {
    "Einnahme": {"skr03_konto": "8200", "zeile": "12", "euer_zeile": "Zeile 12 - Betriebseinnahmen als Kleinunternehmer (§19 Abs. 1 UStG), brutto"},
    "STORNO": {"skr03_konto": "8200", "zeile": "12", "euer_zeile": "Zeile 12 - Betriebseinnahmen Kleinunternehmer (Korrektur/Retoure, mindernd)"},
    "Wareneinkauf": {"skr03_konto": "3200", "zeile": "27", "euer_zeile": "Zeile 27 - Waren, Rohstoffe und Hilfsstoffe inkl. Nebenkosten"},
    "Porto": {"skr03_konto": "4910", "zeile": "60", "euer_zeile": "Zeile 60 - Uebrige unbeschr. abziehbare Betriebsausgaben (Porto/Versand)"},
    "Material": {"skr03_konto": "4930", "zeile": "60", "euer_zeile": "Zeile 60 - Uebrige unbeschr. abziehbare Betriebsausgaben (Verpackung/Buero)"},
    "Gebühren": {"skr03_konto": "4970", "zeile": "60", "euer_zeile": "Zeile 60 - Uebrige unbeschr. abziehbare Betriebsausgaben (Plattform-/Bankgebuehren)"},
    "Sonstiges": {"skr03_konto": "4900", "zeile": "60", "euer_zeile": "Zeile 60 - Uebrige unbeschr. abziehbare Betriebsausgaben (Sonstiges)"},
    "Ausgabe-STORNO": {"skr03_konto": "3200", "zeile": "27", "euer_zeile": "Zeile 27 - Wareneingang, Gutschrift vom Lieferanten (mindernd)"},
    "Privatentnahme": {"skr03_konto": "1800", "zeile": "106", "euer_zeile": "Zeile 106 - Entnahmen (nachrichtlich, NICHT gewinnwirksam)"},
}

def euer_zuordnung(entry):
    """Ermittelt die passende SKR03/EUER-Zuordnung: nur bei normalen Ausgaben nach Kategorie
    (Wareneinkauf/Porto/etc.), sonst immer nach Typ - damit z.B. Ausgabe-STORNO nicht ueber
    seine (immer "Wareneinkauf" gesetzte) Kategorie faelschlich wie eine normale Ausgabe
    einsortiert wird."""
    typ = entry.get("typ", "")
    if typ == "Ausgabe":
        kategorie = entry.get("kategorie")
        if kategorie and kategorie in EUER_MAPPING:
            return EUER_MAPPING[kategorie]
    return EUER_MAPPING.get(typ, {"skr03_konto": "-", "euer_zeile": "Manuell pruefen"})

def generate_pdf(type, data, filename):
    pdf = FPDF()
    pdf.add_page()
    
    total_sum = sum(item['qty'] * item['price'] for item in data.get('items', []))
    
    pdf.set_font("helvetica", size=8)
    pdf.cell(0, 4, clean_text(f"{MEIN_NAME} | {MEINE_STRASSE} | {MEINE_STADT}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, clean_text(f"E-Mail: {MEINE_MAIL} | Steuernummer: {MEINE_STEUERNUMMER}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 4, clean_text(f"IBAN: {MEINE_IBAN} | BIC: {MEINE_BIC}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(10)

    # --- PAYPAL ZAHLUNGSLINK ---
    pdf.ln(10)
    pdf.set_font("helvetica", 'B', 10)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 5, "Zahlungsinformation:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("helvetica", size=10, style="U")
    pdf.set_text_color(0, 70, 190)
    if PAYPAL_ME:
        paypal_url = f"https://www.paypal.me/{PAYPAL_ME}"
        pdf.cell(0, 5, f"Jetzt per PayPal bezahlen ({total_sum:.2f} EUR)", link=paypal_url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(5)
    
    pdf.set_font("helvetica", 'B', 16)
    title = "RECHNUNG" if type == "invoice" else "QUITTUNG"
    if "storno" in type: title = "STORNO-BELEG / GUTSCHRIFT"
    pdf.cell(0, 10, clean_text(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("helvetica", size=11)
    pdf.cell(0, 8, clean_text(f"Datum: {datetime.now().strftime('%d.%m.%Y')}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    num_label = "Rechnungsnummer" if type == "invoice" else "Belegnummer"
    if "storno" in type: num_label = "Bezieht sich auf Rechnungsnummer"
    num_val = data.get('inv_num') or data.get('rec_num', 'N/A')
    pdf.cell(0, 8, clean_text(f"{num_label}: {num_val}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    if data.get('customer_info'):
        pdf.ln(5)
        pdf.set_font("helvetica", 'B', 10)
        empf_label = "Kunde:" if type == "receipt" else "Empfänger:"
        pdf.cell(0, 5, clean_text(empf_label), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("helvetica", size=10)
        pdf.multi_cell(0, 5, clean_text(data['customer_info']))

    # NEU: Anmerkungsfeld einbinden
    if data.get('anmerkung'):
        pdf.ln(5)
        pdf.set_font("helvetica", 'B', 10)
        pdf.cell(0, 5, "Anmerkung:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("helvetica", size=10)
        pdf.multi_cell(0, 5, clean_text(data['anmerkung']))
    
    pdf.ln(10)
    pdf.set_font("helvetica", 'B', 11)
    pdf.cell(90, 10, "Beschreibung", border=1)
    pdf.cell(20, 10, "Menge", border=1, align='C')
    pdf.cell(35, 10, "Einzelpreis", border=1, align='R')
    pdf.cell(35, 10, "Gesamt", border=1, align='R')
    pdf.ln()
    
    pdf.set_font("helvetica", size=11)
    
    for item in data.get('items', []):
        name = clean_text(str(item['name']))
        qty = item['qty']
        price = item['price']
        row_total = qty * price
        
        try:
            lines = pdf.multi_cell(90, 10, name, border=0, dry_run=True, output="LINES")
        except TypeError:
            lines = pdf.multi_cell(90, 10, name, border=0, split_only=True)
            
        num_lines = len(lines)
        row_height = num_lines * 10 if num_lines > 0 else 10
        
        x, y = pdf.get_x(), pdf.get_y()
        pdf.multi_cell(90, 10, name, border=1)
        pdf.set_xy(x + 90, y)
        pdf.cell(20, row_height, str(qty), border=1, align='C')
        pdf.cell(35, row_height, f"{price:.2f} EUR", border=1, align='R')
        pdf.cell(35, row_height, f"{row_total:.2f} EUR", border=1, align='R')
        pdf.ln()
    
    pdf.set_font("helvetica", 'B', 11)
    pdf.cell(145, 10, "GESAMTBETRAG", border=1, align='R')
    pdf.cell(35, 10, f"{total_sum:.2f} EUR", border=1, align='R')
    pdf.ln(10)
    
    pdf.set_font("helvetica", 'I', 9)
    pdf.cell(0, 5, "Das Leistungsdatum entspricht dem Rechnungsdatum.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.multi_cell(0, 5, "Hinweis: Als Kleinunternehmer im Sinne von § 19 Abs. 1 UStG wird keine Umsatzsteuer berechnet.")
    
    # NEU: QR Code für Rechnungen einbinden
    qr_path = "Qr_code_einnahmen.png"
    if os.path.exists(qr_path) and type == "invoice":
        pdf.ln(10)
        pdf.set_font("helvetica", 'B', 10)
        pdf.cell(0, 5, "Bequem per N26 QR-Code bezahlen:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        # Fügt das Bild ein (w=50 skaliert das Bild auf 50mm Breite)
        pdf.image(qr_path, x=pdf.get_x(), w=50)

    if "storno" in type:
        ordner = STORNO_ORDNER
    elif "invoice" in type:
        ordner = RECHNUNG_ORDNER
    else:
        ordner = QUITTUNG_ORDNER
    path = os.path.join(ordner, filename)
    pdf.output(path)
    return path

# --- ZENTRALES INVOICE MODAL ---
class InvoiceModal(discord.ui.Modal, title='Rechnungsdaten'):
    customer = discord.ui.TextInput(label='Kunde: Name & Anschrift', style=discord.TextStyle.paragraph)
    
    # NEU: Optionales Anmerkungsfeld hinzugefügt
    anmerkung = discord.ui.TextInput(
        label='Anmerkung (Optional)', 
        style=discord.TextStyle.paragraph, 
        required=False, 
        placeholder='Z.B. Besonderheiten, Versandhinweise...'
    )
    
    def __init__(self, items, total_val, journal_entry=None):
        super().__init__()
        self.items = items
        self.total_val = total_val
        self.journal_entry = journal_entry

    async def on_submit(self, modal_int: discord.Interaction):
        await modal_int.response.defer()

        inv_num = bot.data["config"]["next_invoice_number"]

        # Daten-Dict um anmerkung erweitert
        pdf_data = {
            "inv_num": inv_num,
            "customer_info": self.customer.value,
            "anmerkung": self.anmerkung.value,
            "items": self.items
        }

        path = generate_pdf("invoice", pdf_data, f"Rechnung_{inv_num}.pdf")

        bot.data["config"]["next_invoice_number"] += 1

        hinweis = ""
        if self.journal_entry is not None:
            if not self.journal_entry.get("menge"):
                hinweis += f"\nℹ️ Hinweis: Für {self.journal_entry['journal_id']} war keine Stückzahl hinterlegt - es wurde 1 Posten für den Gesamtbetrag angenommen."
            vorherige = self.journal_entry.get("rechnungsnummer")
            if vorherige:
                hinweis += f"\nℹ️ Hinweis: Für {self.journal_entry['journal_id']} war bereits Rechnung Nr. {vorherige} hinterlegt."
            self.journal_entry["rechnungsnummer"] = str(inv_num)

        bot.save_data()

        await modal_int.followup.send(f"✅ Rechnung #{inv_num} erstellt.{hinweis}", file=discord.File(path))

# --- ZENTRALES QUITTUNGS-MODAL ---
# Analog zum InvoiceModal, aber alle Felder optional: Eine Quittung ist auch ohne
# Kundendaten gueltig (z.B. Barverkauf an Laufkundschaft).
class ReceiptModal(discord.ui.Modal, title='Quittungsdaten'):
    customer = discord.ui.TextInput(
        label='Kunde: Name & Anschrift (Optional)',
        style=discord.TextStyle.paragraph,
        required=False,
        placeholder='Z.B. Max Mustermann, Musterstr. 1, 12345 Musterstadt'
    )

    anmerkung = discord.ui.TextInput(
        label='Anmerkung (Optional)',
        style=discord.TextStyle.paragraph,
        required=False,
        placeholder='Z.B. Betrag dankend erhalten, Abholung, Zahlungsart...'
    )

    def __init__(self, items, total_val, journal_entry=None):
        super().__init__()
        self.items = items
        self.total_val = total_val
        self.journal_entry = journal_entry

    async def on_submit(self, modal_int: discord.Interaction):
        await modal_int.response.defer()

        rec_num = bot.data["config"].get("next_receipt_number", 1001)

        pdf_data = {"rec_num": rec_num, "items": self.items}
        # Leere Felder gar nicht erst uebergeben, damit die PDF wie bisher aussieht,
        # wenn keine Kundendaten eingetragen wurden.
        if self.customer.value and self.customer.value.strip():
            pdf_data["customer_info"] = self.customer.value.strip()
        if self.anmerkung.value and self.anmerkung.value.strip():
            pdf_data["anmerkung"] = self.anmerkung.value.strip()

        path = generate_pdf("receipt", pdf_data, f"Quittung_{rec_num}.pdf")

        bot.data["config"]["next_receipt_number"] = rec_num + 1

        hinweis = ""
        if self.journal_entry is not None:
            if not self.journal_entry.get("menge"):
                hinweis += f"\nℹ️ Hinweis: Für {self.journal_entry['journal_id']} war keine Stückzahl hinterlegt - es wurde 1 Posten für den Gesamtbetrag angenommen."
            vorherige = self.journal_entry.get("quittungsnummer")
            if vorherige:
                hinweis += f"\nℹ️ Hinweis: Für {self.journal_entry['journal_id']} war bereits Quittung Nr. {vorherige} hinterlegt."
            self.journal_entry["quittungsnummer"] = str(rec_num)

        bot.save_data()

        await modal_int.followup.send(f"✅ Quittung #{rec_num} erstellt.{hinweis}", file=discord.File(path))

# --- COMMANDS ---

@bot.tree.command(name="last_sales", description="Zeigt die letzten 5 Einnahmen")
async def last_sales(interaction: discord.Interaction):
    sales = [e for e in bot.data["euer"] if e["typ"] in ["Einnahme", "STORNO"]]
    if not sales:
        return await interaction.response.send_message("❌ Noch keine Einnahmen verzeichnet.", ephemeral=True)
    recent = sales[-5:]
    recent.reverse()
    embed = discord.Embed(title="💰 Letzte 5 Einnahmen/Stornos", color=discord.Color.green())
    for s in recent:
        datum = s.get('datum', '-')
        produkt = s.get('produkt', 'Unbekannt')
        betrag = s.get('betrag', 0.0)
        herkunft = s.get('herkunft', 'N/A')
        embed.add_field(name=f"{datum} | {betrag:.2f} €", value=f"{produkt}\nVia: {herkunft}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="last_expenses", description="Zeigt die letzten 5 Ausgaben")
async def last_expenses(interaction: discord.Interaction):
    expenses = [e for e in bot.data["euer"] if e["typ"] in ["Ausgabe", "Ausgabe-STORNO"]]
    if not expenses:
        return await interaction.response.send_message("❌ Noch keine Ausgaben verzeichnet.", ephemeral=True)
    recent = expenses[-5:]
    recent.reverse()
    embed = discord.Embed(title="💸 Letzte 5 Ausgaben", color=discord.Color.red())
    for e in recent:
        datum = e.get('datum', '-')
        produkt = e.get('produkt', 'Unbekannt')
        betrag = e.get('betrag', 0.0)
        shop = e.get('herkunft', 'N/A')
        kategorie = e.get('kategorie', '-')
        embed.add_field(name=f"{datum} | {betrag:.2f} €", value=f"{produkt}\nShop: {shop} | Kat: {kategorie}", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="lager_add", description="Artikel hinzufügen")
async def lager_add(interaction: discord.Interaction, produkt: str, menge: int, ek_wert_pro_stueck: float):
    item = find_lager_item(produkt)
    if item:
        item["qty"] += menge
    else:
        bot.data["lager"].append({"name": produkt, "qty": menge, "ek": ek_wert_pro_stueck})
    lager_aufraeumen()
    bot.save_data()
    if find_lager_item(produkt) is None:
        await interaction.response.send_message(f"🗑️ **{produkt}** hat durch diese Änderung Bestand ≤ 0 erreicht und wurde aus dem Lager entfernt.")
    else:
        await interaction.response.send_message(f"📦 Lagerbestand erweitert: {menge}x {produkt}.")

@bot.tree.command(name="lager", description="Zeigt das komplette Lager und den Gesamtwert")
async def lager_show(interaction: discord.Interaction):
    bestand = bot.data.get("lager", [])
    if not bestand:
        return await interaction.response.send_message("📦 Dein Lager ist aktuell komplett leer.", ephemeral=True)
    
    embed = discord.Embed(title="📦 Aktueller Lagerbestand", color=discord.Color.gold())
    
    lager_liste = ""
    gesamtwert = 0.0
    
    for item in bestand:
        name = item.get("name", "Unbekannt")
        qty = item.get("qty", 0)
        ek = item.get("ek", 0.0)
        wert = qty * ek
        gesamtwert += wert
        
        zeile = f"**{qty}x** {name} (EK: {ek:.2f} €) = {wert:.2f} €\n"
        if len(lager_liste) + len(zeile) < 4000:
            lager_liste += zeile
            
    embed.description = lager_liste
    embed.add_field(name="Lagerwert (EK) Gesamt", value=f"**{gesamtwert:.2f} €**", inline=False)
    embed.set_footer(text=f"{MEIN_NAME} | Kleinunternehmer gem. § 19 UStG")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="lager_change", description="Die Menge eines Produkts im Lager anpassen (0 = Produkt entfernen)")
@app_commands.autocomplete(produkt=product_autocomplete)
async def lager_change(interaction: discord.Interaction, produkt: str, neue_menge: int):
    produkt = resolve_produkt(produkt)
    item = find_lager_item(produkt)

    if item:
        alte_menge = item["qty"]
        item["qty"] = neue_menge
        lager_aufraeumen()
        bot.save_data()
        # Entscheidend ist die gesetzte Menge, nicht die Rueckgabe von lager_aufraeumen() -
        # die koennte theoretisch auch andere Altlasten enthalten.
        if neue_menge <= 0:
            await interaction.response.send_message(f"🗑️ **{produkt}** auf 0 gesetzt und automatisch aus dem Lager entfernt (vorher {alte_menge} Stück).")
        else:
            await interaction.response.send_message(f"🔄 Menge für **{produkt}** aktualisiert: {alte_menge} ➔ {neue_menge} Stück.")
    else:
        await interaction.response.send_message(f"❌ Produkt **{produkt}** nicht im Lager gefunden. Nutze `/lager_add` um es anzulegen.", ephemeral=True)

# /lager_delete ist seit 2026-07-04 deaktiviert (Nutzerentscheidung): Produkte verschwinden
# jetzt automatisch aus dem Lager, sobald ihr Bestand 0 erreicht (siehe lager_aufraeumen()),
# und /lager_change mit neue_menge=0 entfernt gezielt. Code bleibt fuer den Fall einer
# spaeteren Reaktivierung als Kommentar erhalten:
#
# @bot.tree.command(name="lager_delete", description="Ein Produkt komplett aus dem Lager löschen")
# @app_commands.autocomplete(produkt=product_autocomplete)
# async def lager_delete(interaction: discord.Interaction, produkt: str):
#     original_len = len(bot.data["lager"])
#     bot.data["lager"] = [item for item in bot.data["lager"] if item["name"].lower() != produkt.lower()]
#
#     if len(bot.data["lager"]) < original_len:
#         bot.save_data()
#         await interaction.response.send_message(f"🗑️ Produkt **{produkt}** wurde komplett aus dem Lager gelöscht.")
#     else:
#         await interaction.response.send_message(f"❌ Produkt **{produkt}** wurde nicht gefunden.", ephemeral=True)

@bot.tree.command(name="ausgabe", description="Ausgabe erfassen (optional mit Beleg-Anhang)")
@app_commands.choices(zahlungsart=ZAHLUNGSARTEN, kategorie=KATEGORIEN)
async def ausgabe(interaction: discord.Interaction, shop: str, produkt: str, kategorie: str, menge: int, ek_brutto_gesamt: float, zahlungsart: str, beleg: discord.Attachment = None):
    # Defer, da der Download eines angehaengten Belegs laenger als die 3-Sekunden-Frist dauern kann.
    await interaction.response.defer()

    entry = make_journal_entry(
        typ="Ausgabe",
        produkt=produkt,
        menge=menge,
        betrag=ek_brutto_gesamt,
        kategorie=kategorie,
        zahlungsart=zahlungsart,
        herkunft=shop,
    )

    # Nur echter Wareneinkauf fuellt das Lager - Porto, Gebuehren, Material etc. sind
    # Betriebsausgaben ohne Lagerbezug (Nutzerentscheidung 2026-07-04; frueher landete
    # z.B. auch ein Versandlabel als "Artikel" im Lager).
    lager_hinweis = ""
    if kategorie == "Wareneinkauf":
        item = find_lager_item(produkt)
        if item:
            item["qty"] += menge
        else:
            bot.data["lager"].append({"name": produkt, "qty": menge, "ek": ek_brutto_gesamt/menge if menge > 0 else 0})
        lager_aufraeumen()
        lager_hinweis = f"\n📦 Lager aktualisiert: +{menge}x {produkt}" if menge > 0 else ""

    beleg_hinweis = ""
    if beleg is not None:
        try:
            await save_beleg(beleg, entry)
            beleg_hinweis = "\n📎 Beleg gespeichert."
        except BelegError as e:
            beleg_hinweis = f"\n⚠️ Beleg NICHT gespeichert: {e}"
        except Exception as e:
            beleg_hinweis = f"\n⚠️ Beleg konnte nicht gespeichert werden: {e}"

    bot.save_data()
    await interaction.followup.send(f"✅ Ausgabe erfasst: {menge}x {produkt}. (Journal-ID: {entry['journal_id']}){lager_hinweis}{beleg_hinweis}")

@bot.tree.command(name="einnahme", description="Verkauf erfassen (Einzelnes Produkt)")
@app_commands.autocomplete(produkt_bestand=product_autocomplete)
@app_commands.choices(zahlungsart=ZAHLUNGSARTEN, nachweis=PLATTFORMEN)
async def einnahme(interaction: discord.Interaction, menge: int, vk_brutto_inkl_versand: float, zahlungsart: str, nachweis: str, produkt_bestand: str):
    produkt_bestand = resolve_produkt(produkt_bestand)
    entry = make_journal_entry(
        typ="Einnahme",
        produkt=produkt_bestand,
        menge=menge,
        betrag=vk_brutto_inkl_versand,
        zahlungsart=zahlungsart,
        herkunft=nachweis,
    )
    maybe_auto_privatentnahme(entry)

    item = find_lager_item(produkt_bestand)
    if item:
        item["qty"] = max(0, item["qty"] - menge)
    entfernt = lager_aufraeumen()
    bot.save_data()

    lager_hinweis = f"\n📦 Ausverkauft - aus dem Lager entfernt: {', '.join(entfernt)}" if entfernt else ""
    items_for_pdf = [{'name': produkt_bestand, 'qty': menge, 'price': vk_brutto_inkl_versand/menge if menge > 0 else 0}]

    class SaleView(discord.ui.View):
        def __init__(self, items, total, entry):
            super().__init__()
            self.items = items
            self.total = total
            self.entry = entry

        @discord.ui.button(label="Rechnung", style=discord.ButtonStyle.primary)
        async def inv(self, btn_int, btn):
            await btn_int.response.send_modal(InvoiceModal(self.items, self.total, self.entry))

        @discord.ui.button(label="Quittung", style=discord.ButtonStyle.secondary)
        async def rec(self, btn_int, btn):
            await btn_int.response.send_modal(ReceiptModal(self.items, self.total, self.entry))

    await interaction.response.send_message(f"💰 Verkauf erfasst: {produkt_bestand}\n(Journal-ID: {entry['journal_id']}){lager_hinweis}", view=SaleView(items_for_pdf, vk_brutto_inkl_versand, entry))

@bot.tree.command(name="einnahme_multi", description="Verkauf von bis zu 5 Produkten gleichzeitig erfassen")
@app_commands.autocomplete(p1=product_autocomplete, p2=product_autocomplete, p3=product_autocomplete, p4=product_autocomplete, p5=product_autocomplete)
@app_commands.choices(zahlungsart=ZAHLUNGSARTEN, nachweis=PLATTFORMEN)
async def einnahme_multi(
    interaction: discord.Interaction,
    vk_brutto_gesamt: float,
    zahlungsart: str,
    nachweis: str,
    p1: str, m1: int, ep1: float,
    p2: str = None, m2: int = None, ep2: float = None,
    p3: str = None, m3: int = None, ep3: float = None,
    p4: str = None, m4: int = None, ep4: float = None,
    p5: str = None, m5: int = None, ep5: float = None
):
    p1, p2, p3, p4, p5 = (resolve_produkt(x) for x in (p1, p2, p3, p4, p5))
    products_input = [
        (p1, m1, ep1),
        (p2, m2, ep2),
        (p3, m3, ep3),
        (p4, m4, ep4),
        (p5, m5, ep5)
    ]
    
    valid_products = []
    items_for_pdf = []
    sum_products = 0.0

    for p, m, ep in products_input:
        if p:
            m_val = m if m is not None else 1
            ep_val = ep if ep is not None else 0.0
            
            valid_products.append({"name": p, "qty": m_val, "ep": ep_val})
            items_for_pdf.append({"name": p, "qty": m_val, "price": ep_val})
            sum_products += (m_val * ep_val)

    differenz = vk_brutto_gesamt - sum_products
    if differenz > 0.01:
        items_for_pdf.append({"name": "Versandkosten / Aufschlag", "qty": 1, "price": differenz})
    elif differenz < -0.01:
        items_for_pdf.append({"name": "Rabatt", "qty": 1, "price": differenz})

    produkt_namen = ", ".join([f"{vp['qty']}x {vp['name']}" for vp in valid_products])

    entry = make_journal_entry(
        typ="Einnahme",
        produkt=produkt_namen,
        betrag=vk_brutto_gesamt,
        zahlungsart=zahlungsart,
        herkunft=nachweis,
    )
    maybe_auto_privatentnahme(entry)

    for vp in valid_products:
        item = find_lager_item(vp["name"])
        if item:
            item["qty"] = max(0, item["qty"] - vp["qty"])
    entfernt = lager_aufraeumen()
    bot.save_data()
    lager_hinweis = f"\n📦 Ausverkauft - aus dem Lager entfernt: {', '.join(entfernt)}" if entfernt else ""

    class MultiSaleView(discord.ui.View):
        def __init__(self, items, total, entry):
            super().__init__()
            self.items = items
            self.total = total
            self.entry = entry

        @discord.ui.button(label="Rechnung", style=discord.ButtonStyle.primary)
        async def inv(self, btn_int, btn):
            await btn_int.response.send_modal(InvoiceModal(self.items, self.total, self.entry))

        @discord.ui.button(label="Quittung", style=discord.ButtonStyle.secondary)
        async def rec(self, btn_int, btn):
            await btn_int.response.send_modal(ReceiptModal(self.items, self.total, self.entry))

    await interaction.response.send_message(f"🛒 Multi-Verkauf erfasst:\n**{produkt_namen}**\n(Journal-ID: {entry['journal_id']}){lager_hinweis}", view=MultiSaleView(items_for_pdf, vk_brutto_gesamt, entry))

@bot.tree.command(name="storno_einnahme", description="Einnahme stornieren")
@app_commands.autocomplete(produkt_bestand=product_autocomplete)
@app_commands.choices(plattform=PLATTFORMEN)
async def storno_einnahme(interaction: discord.Interaction, journal_id_referenz: str, produkt_bestand: str, menge_retour: int, betrag: float, grund: str, plattform: str):
    produkt_bestand = resolve_produkt(produkt_bestand)
    storno_betrag = -abs(betrag)
    entry = make_journal_entry(
        typ="STORNO",
        produkt=f"STORNO zu {journal_id_referenz} - {produkt_bestand} ({grund})",
        menge=menge_retour,
        betrag=storno_betrag,
        zahlungsart="Retoure",
        herkunft=plattform,
    )

    retour_hinweis = ""
    item = find_lager_item(produkt_bestand)
    if item:
        item["qty"] += menge_retour
    elif menge_retour > 0:
        # Produkt wurde beim Abverkauf automatisch aus dem Lager entfernt - fuer die
        # Retoure neu anlegen. EK ist dann unbekannt (0.0) und muss ggf. per
        # /lager_change bzw. /lager_add korrigiert werden. (Nur bei echter Menge > 0,
        # sonst entstuende ein 0-Bestand-Eintrag.)
        bot.data["lager"].append({"name": produkt_bestand, "qty": menge_retour, "ek": 0.0})
        retour_hinweis = f"\n📦 {produkt_bestand} war nicht mehr im Lager - neu angelegt mit EK 0,00 € (bitte ggf. korrigieren)."
    lager_aufraeumen()
    bot.save_data()
    path = generate_pdf("storno_invoice", {
        "inv_num": f"Ref {journal_id_referenz}",
        "customer_info": "Kundeninfo manuell ergänzen.",
        "items": [{"name": f"STORNO: {produkt_bestand} ({grund})", "qty": menge_retour, "price": storno_betrag/menge_retour if menge_retour > 0 else 0}]
    }, f"Storno_{entry['journal_id'].split('-', 1)[1]}.pdf")
    embed = discord.Embed(title="🔴 Einnahme storniert", color=discord.Color.red())
    embed.add_field(name="Buchung", value=f"{storno_betrag:.2f} €", inline=True)
    embed.add_field(name="Journal-ID", value=entry['journal_id'], inline=True)
    if retour_hinweis:
        embed.add_field(name="📦 Lager", value=retour_hinweis.strip(), inline=False)
    embed.set_footer(text=f"{MEIN_NAME}")
    await interaction.response.send_message(embed=embed, file=discord.File(path))

@bot.tree.command(name="storno_einnahme_multi", description="Multi-Einnahme stornieren (bis zu 5 Produkte)")
@app_commands.autocomplete(p1=product_autocomplete, p2=product_autocomplete, p3=product_autocomplete, p4=product_autocomplete, p5=product_autocomplete)
@app_commands.choices(plattform=PLATTFORMEN)
async def storno_einnahme_multi(
    interaction: discord.Interaction,
    journal_id_referenz: str,
    betrag: float,
    grund: str,
    plattform: str,
    p1: str, m1: int,
    p2: str = None, m2: int = None,
    p3: str = None, m3: int = None,
    p4: str = None, m4: int = None,
    p5: str = None, m5: int = None
):
    storno_betrag = -abs(betrag)
    p1, p2, p3, p4, p5 = (resolve_produkt(x) for x in (p1, p2, p3, p4, p5))
    products_input = [(p1, m1), (p2, m2), (p3, m3), (p4, m4), (p5, m5)]
    valid_products = []

    for p, m in products_input:
        if p:
            m_val = m if m is not None else 1
            valid_products.append({"name": p, "qty": m_val})

    produkt_namen = ", ".join([f"{vp['qty']}x {vp['name']}" for vp in valid_products])

    entry = make_journal_entry(
        typ="STORNO",
        produkt=f"STORNO zu {journal_id_referenz} - Multi: {produkt_namen} ({grund})",
        betrag=storno_betrag,
        zahlungsart="Retoure",
        herkunft=plattform,
    )

    neu_angelegt = []
    for vp in valid_products:
        item = find_lager_item(vp["name"])
        if item:
            item["qty"] += vp["qty"]
        elif vp["qty"] > 0:
            # Beim Abverkauf automatisch entfernt - fuer die Retoure neu anlegen (EK unbekannt).
            bot.data["lager"].append({"name": vp["name"], "qty": vp["qty"], "ek": 0.0})
            neu_angelegt.append(vp["name"])

    lager_aufraeumen()
    bot.save_data()

    items_for_pdf = [{"name": f"STORNO: {vp['name']} ({grund})", "qty": vp['qty'], "price": storno_betrag/len(valid_products) if len(valid_products) > 0 else 0} for vp in valid_products]

    path = generate_pdf("storno_invoice", {
        "inv_num": f"Ref {journal_id_referenz}",
        "customer_info": "Kundeninfo manuell ergänzen.",
        "items": items_for_pdf
    }, f"Storno_{entry['journal_id'].split('-', 1)[1]}.pdf")

    embed = discord.Embed(title="🔴 Multi-Einnahme storniert", color=discord.Color.red())
    embed.add_field(name="Produkte", value=produkt_namen, inline=False)
    embed.add_field(name="Buchung", value=f"{storno_betrag:.2f} €", inline=True)
    embed.add_field(name="Journal-ID", value=entry['journal_id'], inline=True)
    if neu_angelegt:
        embed.add_field(name="📦 Neu im Lager angelegt (EK 0,00 € - bitte ggf. korrigieren)",
                        value=", ".join(neu_angelegt), inline=False)
    embed.set_footer(text=f"{MEIN_NAME} - Kleinunternehmer gem. § 19 UStG")
    
    await interaction.response.send_message(embed=embed, file=discord.File(path))

KLEINUNTERNEHMER_GRENZE_LAUFENDES_JAHR = 100000.0
KLEINUNTERNEHMER_GRENZE_VORJAHR = 25000.0

@bot.tree.command(name="stats", description="Finanz-Dashboard inkl. Kleinunternehmer-Grenzen")
async def stats(interaction: discord.Interaction, jahr: int = None):
    jahr = jahr or datetime.now().year
    einnahmen = [e for e in bot.data["euer"] if entry_jahr(e) == jahr and e["typ"] in ["Einnahme", "STORNO"]]
    ausgaben = [e for e in bot.data["euer"] if entry_jahr(e) == jahr and e["typ"] in ["Ausgabe", "Ausgabe-STORNO"]]
    umsatz = sum(e["betrag"] for e in einnahmen)
    kosten = sum(e["betrag"] for e in ausgaben)
    gewinn = umsatz - kosten
    lager_wert = sum(item.get("qty", 0) * item.get("ek", 0) for item in bot.data["lager"])

    vorjahr_einnahmen = [e for e in bot.data["euer"] if entry_jahr(e) == jahr - 1 and e["typ"] in ["Einnahme", "STORNO"]]
    vorjahr_umsatz = sum(e["betrag"] for e in vorjahr_einnahmen)

    prozent_laufend = (umsatz / KLEINUNTERNEHMER_GRENZE_LAUFENDES_JAHR) * 100
    prozent_vorjahr = (vorjahr_umsatz / KLEINUNTERNEHMER_GRENZE_VORJAHR) * 100
    kritisch = prozent_laufend >= 100 or prozent_vorjahr >= 100
    warnung = prozent_laufend >= 80 or prozent_vorjahr >= 80

    farbe = discord.Color.red() if kritisch else (discord.Color.orange() if warnung else discord.Color.blue())

    embed = discord.Embed(title=f"📊 Status {jahr}", color=farbe)
    embed.add_field(name="Umsatz", value=f"{umsatz:.2f} €", inline=True)
    embed.add_field(name="Gewinn", value=f"{gewinn:.2f} €", inline=True)
    embed.add_field(name="Lagerwert (EK)", value=f"{lager_wert:.2f} €", inline=True)
    embed.add_field(
        name=f"Grenze laufendes Jahr ({jahr})",
        value=(
            f"{umsatz:.2f} € / {KLEINUNTERNEHMER_GRENZE_LAUFENDES_JAHR:.0f} € ({prozent_laufend:.1f}%)\n"
            "Bei Überschreiten: sofortiger Wechsel zur Regelbesteuerung (seit Reform 2025)."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"Grenze Vorjahr ({jahr - 1}, entscheidet über Berechtigung {jahr})",
        value=f"{vorjahr_umsatz:.2f} € / {KLEINUNTERNEHMER_GRENZE_VORJAHR:.0f} € ({prozent_vorjahr:.1f}%)",
        inline=False,
    )
    if warnung:
        embed.add_field(
            name="⚠️ Hinweis",
            value=(
                "Du näherst dich der Kleinunternehmergrenze. Falls du zur Regelbesteuerung wechselst: "
                "als Wiederverkäufer gebrauchter Ware kann die Differenzbesteuerung (§25a UStG) - Umsatzsteuer "
                "nur auf die Marge statt auf den vollen Verkaufspreis - deutlich günstiger sein. "
                "Bitte rechtzeitig mit einem Steuerberater klären."
            ),
            inline=False,
        )
    embed.set_footer(text="Richtwerte, keine Steuerberatung - bitte Zahlen vor der Erklärung gegenchecken.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="storno_ausgabe", description="Storniert eine Ausgabe (Gutschrift vom Lieferanten)")
@app_commands.autocomplete(produkt=product_autocomplete)
async def storno_ausgabe(interaction: discord.Interaction, journal_id_referenz: str, produkt: str, menge_retour: int, betrag_gutschrift: float, grund: str):
    produkt = resolve_produkt(produkt)
    storno_betrag = -abs(betrag_gutschrift)
    entry = make_journal_entry(
        typ="Ausgabe-STORNO",
        produkt=f"RETOUR zu {journal_id_referenz}: {produkt} ({grund})",
        menge=menge_retour,
        betrag=storno_betrag,
        kategorie="Wareneinkauf",
        zahlungsart="Gutschrift",
        herkunft="Lieferant",
    )

    item = find_lager_item(produkt)
    if item:
        item["qty"] = max(0, item["qty"] - menge_retour)
    entfernt = lager_aufraeumen()

    bot.save_data()

    embed = discord.Embed(title="reverso ↩️ Ausgabe storniert", color=discord.Color.orange())
    if entfernt:
        embed.add_field(name="📦 Aus dem Lager entfernt (Bestand 0)", value=", ".join(entfernt), inline=False)
    embed.add_field(name="Produkt", value=produkt, inline=True)
    embed.add_field(name="Menge", value=f"-{menge_retour} Stk.", inline=True)
    embed.add_field(name="Journal-ID", value=entry['journal_id'], inline=True)
    embed.add_field(name="Gutschrift", value=f"{betrag_gutschrift:.2f} €", inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="privatentnahme", description="Manuelle Privatentnahme buchen (z.B. angesammeltes Bargeld aus der Kasse nehmen)")
@app_commands.choices(zahlungsart=ZAHLUNGSARTEN)
async def privatentnahme(interaction: discord.Interaction, betrag: float, grund: str, zahlungsart: str):
    entry = make_journal_entry(
        typ="Privatentnahme",
        produkt=f"Privatentnahme: {grund}",
        betrag=-abs(betrag),
        zahlungsart=zahlungsart,
        herkunft="Manuell",
    )
    bot.save_data()

    embed = discord.Embed(title="🏧 Privatentnahme gebucht", color=discord.Color.orange())
    embed.add_field(name="Betrag", value=f"-{abs(betrag):.2f} €", inline=True)
    embed.add_field(name="Zahlungsart", value=zahlungsart, inline=True)
    embed.add_field(name="Journal-ID", value=entry['journal_id'], inline=True)
    embed.add_field(name="Grund", value=grund, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="rechnung_nachtraeglich", description="Erstellt nachträglich eine Rechnung zu einer bestehenden Einnahme-Buchung")
async def rechnung_nachtraeglich(interaction: discord.Interaction, journal_id: str):
    entry = find_journal_entry(journal_id)
    if entry is None:
        return await interaction.response.send_message(f"❌ Keine Buchung mit Journal-ID {journal_id} gefunden.", ephemeral=True)
    if entry.get("typ") != "Einnahme":
        return await interaction.response.send_message(
            f"❌ {entry['journal_id']} ist keine Einnahme (Typ: {entry.get('typ')}) - dafür kann keine Rechnung erstellt werden.",
            ephemeral=True,
        )

    # Fehlt eine Stueckzahl (menge=0, z.B. bei alten oder aus /einnahme_multi stammenden
    # Eintraegen), wird 1 Sammelposten fuer den Gesamtbetrag angenommen statt eine falsche
    # Stueckzahl vorzutaeuschen - der Gesamtbetrag bleibt so in jedem Fall korrekt.
    menge = entry.get("menge") or 1
    preis = entry.get("betrag", 0.0) / menge
    items = [{"name": entry.get("produkt", "Artikel"), "qty": menge, "price": preis}]

    await interaction.response.send_modal(InvoiceModal(items, entry.get("betrag", 0.0), journal_entry=entry))

@bot.tree.command(name="quittung_nachtraeglich", description="Erstellt nachträglich eine Quittung zu einer bestehenden Einnahme-Buchung")
async def quittung_nachtraeglich(interaction: discord.Interaction, journal_id: str):
    entry = find_journal_entry(journal_id)
    if entry is None:
        return await interaction.response.send_message(f"❌ Keine Buchung mit Journal-ID {journal_id} gefunden.", ephemeral=True)
    if entry.get("typ") != "Einnahme":
        return await interaction.response.send_message(
            f"❌ {entry['journal_id']} ist keine Einnahme (Typ: {entry.get('typ')}) - dafür kann keine Quittung erstellt werden.",
            ephemeral=True,
        )

    # Fehlt eine Stueckzahl (menge=0, z.B. bei alten oder aus /einnahme_multi stammenden
    # Eintraegen), wird 1 Sammelposten fuer den Gesamtbetrag angenommen statt eine falsche
    # Stueckzahl vorzutaeuschen - der Gesamtbetrag bleibt so in jedem Fall korrekt.
    menge = entry.get("menge") or 1
    preis = entry.get("betrag", 0.0) / menge
    items = [{"name": entry.get("produkt", "Artikel"), "qty": menge, "price": preis}]

    await interaction.response.send_modal(ReceiptModal(items, entry.get("betrag", 0.0), journal_entry=entry))

@bot.tree.command(name="beleg_nachtraeglich", description="Hängt nachträglich einen Beleg (Foto/PDF) an eine bestehende Buchung an")
async def beleg_nachtraeglich(interaction: discord.Interaction, journal_id: str, beleg: discord.Attachment):
    entry = find_journal_entry(journal_id)
    if entry is None:
        return await interaction.response.send_message(f"❌ Keine Buchung mit Journal-ID {journal_id} gefunden.", ephemeral=True)

    await interaction.response.defer()

    hinweis = ""
    if entry.get("typ") != "Ausgabe":
        hinweis += f"\nℹ️ Hinweis: {entry['journal_id']} ist keine Ausgabe (Typ: {entry.get('typ')}) - Beleg wurde trotzdem angehängt."
    if entry.get("beleg_pfad"):
        hinweis += f"\nℹ️ Hinweis: Für {entry['journal_id']} war bereits ein Beleg hinterlegt ({entry['beleg_pfad']}) - er wird ersetzt."

    try:
        path = await save_beleg(beleg, entry)
    except BelegError as e:
        return await interaction.followup.send(f"⚠️ Beleg NICHT gespeichert: {e}")
    except Exception as e:
        return await interaction.followup.send(f"⚠️ Beleg konnte nicht gespeichert werden: {e}")

    bot.save_data()
    await interaction.followup.send(f"📎 Beleg zu {entry['journal_id']} gespeichert ({path}).{hinweis}")

@bot.tree.command(name="beleg_check", description="Zeigt Ausgaben ohne hinterlegten Beleg")
async def beleg_check(interaction: discord.Interaction, jahr: int = None):
    jahr = jahr or datetime.now().year
    ausgaben = [e for e in bot.data.get("euer", []) if entry_jahr(e) == jahr and e.get("typ") == "Ausgabe"]
    ohne_beleg = [e for e in ausgaben if not e.get("beleg_pfad")]

    if not ausgaben:
        return await interaction.response.send_message(f"❌ Keine Ausgaben für {jahr} gefunden.", ephemeral=True)

    embed = discord.Embed(
        title=f"🧾 Beleg-Check {jahr}",
        color=discord.Color.orange() if ohne_beleg else discord.Color.green(),
        description=f"{len(ohne_beleg)} von {len(ausgaben)} Ausgaben ohne hinterlegten Beleg.",
    )
    if ohne_beleg:
        text = "\n".join(
            f"{e.get('datum', '-')[:10]} | {e.get('betrag', 0.0):.2f} € | {e.get('produkt', '-')} ({e.get('journal_id', '-')})"
            for e in ohne_beleg[:20]
        )
        embed.add_field(name="Fehlende Belege (max. 20 angezeigt)", value=text[:1024] or "-", inline=False)

    # GoBD-Einzelaufzeichnungspflicht: Bar-Einnahmen sollten eine Quittung oder Rechnung haben.
    bar_einnahmen = [e for e in bot.data.get("euer", [])
                     if entry_jahr(e) == jahr and e.get("typ") == "Einnahme"
                     and e.get("zahlungsart", "").lower() == "bar"]
    bar_ohne_beleg = [e for e in bar_einnahmen if not e.get("quittungsnummer") and not e.get("rechnungsnummer")]
    if bar_ohne_beleg:
        text = "\n".join(
            f"{e.get('datum', '-')[:10]} | {e.get('betrag', 0.0):.2f} € | {e.get('produkt', '-')} ({e.get('journal_id', '-')})"
            for e in bar_ohne_beleg[:10]
        )
        embed.add_field(
            name=f"⚠️ {len(bar_ohne_beleg)} Bar-Einnahmen ohne Quittung/Rechnung",
            value=(text[:950] + "\nNachtraeglich erzeugen: /quittung_nachtraeglich bzw. /rechnung_nachtraeglich."),
            inline=False,
        )

    embed.set_footer(text="Belege anhängen: direkt bei /ausgabe oder nachträglich mit /beleg_nachtraeglich.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="journal_suche", description="Durchsucht das Journal nach Produktname (optional nach Jahr gefiltert)")
async def journal_suche(interaction: discord.Interaction, suchbegriff: str, jahr: int = None):
    treffer = [
        e for e in bot.data.get("euer", [])
        if suchbegriff.lower() in e.get("produkt", "").lower()
        and (jahr is None or entry_jahr(e) == jahr)
    ]

    if not treffer:
        return await interaction.response.send_message(f"❌ Keine Treffer für \"{suchbegriff}\".", ephemeral=True)

    gesamt = len(treffer)
    treffer = treffer[-15:]
    treffer.reverse()

    embed = discord.Embed(title=f"🔍 Suche: \"{suchbegriff}\"", color=discord.Color.blue())
    for e in treffer:
        embed.add_field(
            name=f"{e.get('journal_id', '-')} | {e.get('datum', '-')}",
            value=f"{e.get('typ', '-')} | {e.get('betrag', 0.0):.2f} € | {e.get('produkt', '-')}",
            inline=False,
        )
    embed.set_footer(text=f"{len(treffer)} von {gesamt} Treffern angezeigt (neueste zuerst).")
    await interaction.response.send_message(embed=embed)

# --- EXPORT BEFEHLE ---

@bot.tree.command(name="export_euer", description="Exportiert alle Buchungen als CSV")
async def export_euer(interaction: discord.Interaction):
    filename = f"EUER_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile, delimiter=';')
        writer.writerow(['Journal-ID', 'Datum', 'Typ', 'Kategorie', 'Produkt', 'Betrag (EUR)', 'Zahlungsart',
                          'Herkunft/Plattform', 'SKR03-Konto (Richtwert)', 'EUER-Zeile (Richtwert)'])
        for e in bot.data.get("euer", []):
            jid = e.get("journal_id", "-")
            datum = e.get("datum", "")
            typ = e.get("typ", "")
            kat = e.get("kategorie", "")
            prod = e.get("produkt", "")
            betrag = f"{e.get('betrag', 0.0):.2f}".replace('.', ',')
            zart = e.get("zahlungsart", "")
            herk = e.get("herkunft", "")
            zuordnung = euer_zuordnung(e)
            writer.writerow([jid, datum, typ, kat, prod, betrag, zart, herk,
                              zuordnung["skr03_konto"], zuordnung["euer_zeile"]])

    await interaction.response.send_message("📄 Hier ist der vollständige EÜR CSV-Export:", file=discord.File(filename))
    os.remove(filename)

@bot.tree.command(name="euer_summary", description="Summiert Buchungen gruppiert nach EÜR-Zeile (grobe Orientierung, ersetzt keinen Steuerberater)")
async def euer_summary(interaction: discord.Interaction, jahr: int = None):
    jahr = jahr or datetime.now().year
    relevante = [e for e in bot.data.get("euer", []) if entry_jahr(e) == jahr and e.get("typ") != "Privatentnahme"]

    summen = {}
    for e in relevante:
        zuordnung = euer_zuordnung(e)
        zeile = zuordnung["euer_zeile"]
        summen[zeile] = summen.get(zeile, 0.0) + e.get("betrag", 0.0)

    if not summen:
        return await interaction.response.send_message(f"❌ Keine Buchungen für {jahr} gefunden.", ephemeral=True)

    embed = discord.Embed(title=f"📊 EÜR-Übersicht {jahr} (grobe Orientierung)", color=discord.Color.blue())
    for zeile, summe in sorted(summen.items(), key=lambda x: -x[1]):
        embed.add_field(name=zeile, value=f"{summe:.2f} €", inline=False)

    # Entnahmen sind Pflichtangabe in Zeile 106 (nachrichtlich) - getrennt vom Gewinnteil zeigen.
    entnahmen = sum(abs(e.get("betrag", 0.0)) for e in bot.data.get("euer", [])
                    if entry_jahr(e) == jahr and e.get("typ") == "Privatentnahme")
    if entnahmen:
        embed.add_field(name="Zeile 106 - Entnahmen (nachrichtlich, nicht gewinnwirksam)",
                        value=f"{entnahmen:.2f} €", inline=False)

    embed.set_footer(text="Zeilen der Anlage EÜR 2025 - bitte vor der Steuererklärung mit dem Live-Formular in Mein ELSTER abgleichen.")
    await interaction.response.send_message(embed=embed)

GEWST_FREIBETRAG = 24500.0
EST_GRUNDFREIBETRAG_2025 = 12096.0

@bot.tree.command(name="elster_vorbereitung", description="Summen pro Anlage-EÜR-Zeile zum direkten Übertragen in ELSTER")
async def elster_vorbereitung(interaction: discord.Interaction, jahr: int = None):
    jahr = jahr or datetime.now().year
    eintraege = [e for e in bot.data.get("euer", []) if entry_jahr(e) == jahr]
    if not eintraege:
        return await interaction.response.send_message(f"❌ Keine Buchungen für {jahr} gefunden.", ephemeral=True)

    zeile12 = sum(e["betrag"] for e in eintraege if e.get("typ") in ("Einnahme", "STORNO"))
    zeile27 = sum(e["betrag"] for e in eintraege
                  if (e.get("typ") == "Ausgabe" and e.get("kategorie") == "Wareneinkauf")
                  or e.get("typ") == "Ausgabe-STORNO")
    zeile60 = sum(e["betrag"] for e in eintraege
                  if e.get("typ") == "Ausgabe" and e.get("kategorie") in ("Porto", "Material", "Gebühren", "Sonstiges"))
    # Ausgaben ohne Kategorie (Alt-Eintraege) nicht stillschweigend verlieren:
    unzugeordnet = sum(e["betrag"] for e in eintraege
                       if e.get("typ") == "Ausgabe"
                       and e.get("kategorie") not in ("Wareneinkauf", "Porto", "Material", "Gebühren", "Sonstiges"))
    zeile106 = sum(abs(e["betrag"]) for e in eintraege if e.get("typ") == "Privatentnahme")
    gewinn = zeile12 - zeile27 - zeile60 - unzugeordnet

    embed = discord.Embed(
        title=f"📋 ELSTER-Vorbereitung {jahr} (Anlage EÜR)",
        description="Eine Summe pro Formularzeile - zum direkten Übertragen in Mein ELSTER.",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Zeile 12 - Betriebseinnahmen Kleinunternehmer (§19 UStG, brutto)",
                    value=f"**{zeile12:.2f} €**", inline=False)
    embed.add_field(name="Zeile 27 - Waren/Rohstoffe/Hilfsstoffe inkl. Nebenkosten",
                    value=f"**{zeile27:.2f} €**", inline=False)
    embed.add_field(name="Zeile 60 - Übrige Betriebsausgaben (Porto/Verpackung/Gebühren/Sonstiges)",
                    value=f"**{zeile60:.2f} €**", inline=False)
    if abs(unzugeordnet) > 0.005:
        embed.add_field(name="⚠️ Ausgaben ohne Kategorie (manuell zuordnen!)",
                        value=f"{unzugeordnet:.2f} €", inline=False)
    embed.add_field(name="Zeile 106 - Entnahmen (nachrichtlich, nicht gewinnwirksam)",
                    value=f"{zeile106:.2f} €", inline=False)
    embed.add_field(name="➡️ Gewinn (in Anlage G zu übernehmen)",
                    value=f"**{gewinn:.2f} €**", inline=False)

    # Gewinn-Schwellen (alle gewinn-, nicht umsatzbezogen)
    schwellen = []
    schwellen.append(f"{'✅' if gewinn <= GEWST_FREIBETRAG else '🔴'} GewSt-Freibetrag 24.500 €: {gewinn/GEWST_FREIBETRAG*100:.0f}%")
    schwellen.append(f"{'✅' if gewinn <= EST_GRUNDFREIBETRAG_2025 else 'ℹ️'} ESt-Grundfreibetrag 12.096 €: {gewinn/EST_GRUNDFREIBETRAG_2025*100:.0f}% (zzgl. weiterer Einkünfte)")
    embed.add_field(name="Gewinn-Schwellen", value="\n".join(schwellen), inline=False)

    # DAC7: Plattformen melden ab ~30 Verkaeufen oder 2.000 EUR/Jahr ans BZSt -
    # die erklaerten Einnahmen sollten je Plattform nachvollziehbar sein.
    plattformen = {}
    for e in eintraege:
        if e.get("typ") in ("Einnahme", "STORNO"):
            herkunft = e.get("herkunft") or "Unbekannt"
            plattformen[herkunft] = plattformen.get(herkunft, 0.0) + e["betrag"]
    if plattformen:
        text = "\n".join(f"{name}: {summe:.2f} €" for name, summe in sorted(plattformen.items(), key=lambda x: -x[1]))
        embed.add_field(name="Einnahmen je Plattform (DAC7-Abgleich)", value=text[:1024], inline=False)

    offene_belege = sum(1 for e in eintraege if e.get("typ") == "Ausgabe" and not e.get("beleg_pfad"))
    if offene_belege:
        embed.add_field(name="🧾 Belege", value=f"{offene_belege} Ausgaben ohne Beleg-Anhang (siehe /beleg_check).", inline=False)

    embed.set_footer(text="Zeilennummern: Anlage EÜR 2025. Verbindlich ist das Live-Formular in Mein ELSTER. Keine Steuerberatung - Details in ELSTER-Anforderungen.md.")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="export_kassenbuch", description="Exportiert das Kassenbuch (Bar-Zahlungen)")
async def export_kassenbuch(interaction: discord.Interaction):
    filename = f"Kassenbuch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    kassenbestand = 0.0
    
    with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile, delimiter=';')
        writer.writerow(['Journal-ID', 'Datum', 'Typ', 'Produkt', 'Einnahme (EUR)', 'Ausgabe (EUR)', 'Aktueller Bestand (EUR)'])
        
        bar_eintraege = [e for e in bot.data.get("euer", []) if e.get("zahlungsart", "").lower() == "bar"]
        for e in bar_eintraege:
            jid = e.get("journal_id", "-")
            datum = e.get("datum", "")
            typ = e.get("typ", "")
            prod = e.get("produkt", "")
            betrag = e.get("betrag", 0.0)
            
            einnahme, ausgabe = 0.0, 0.0
            if "Einnahme" in typ or typ == "Privateinlage" or (typ == "STORNO" and betrag > 0):
                einnahme = betrag
                kassenbestand += betrag
            elif "Ausgabe" in typ or typ == "Privatentnahme" or (typ == "STORNO" and betrag < 0):
                ausgabe = abs(betrag)
                kassenbestand -= ausgabe
            elif betrag > 0:
                einnahme = betrag
                kassenbestand += betrag
            else:
                ausgabe = abs(betrag)
                kassenbestand -= ausgabe

            e_str = f"{einnahme:.2f}".replace('.', ',') if einnahme > 0 else ""
            a_str = f"{ausgabe:.2f}".replace('.', ',') if ausgabe > 0 else ""
            b_str = f"{kassenbestand:.2f}".replace('.', ',')
            
            writer.writerow([jid, datum, typ, prod, e_str, a_str, b_str])
            
    await interaction.response.send_message("💶 Hier ist das Kassenbuch:", file=discord.File(filename))
    os.remove(filename)

@bot.tree.command(name="export_privatentnahmen", description="Summiert Einnahmen auf Privat und Bar")
async def export_privat(interaction: discord.Interaction):
    filename = f"Privat_und_Bar_Einnahmen_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    gesamtsumme = 0.0
    
    with open(filename, 'w', newline='', encoding='utf-8-sig') as csvfile:
        writer = csv.writer(csvfile, delimiter=';')
        writer.writerow(['Journal-ID', 'Datum', 'Typ', 'Konto', 'Produkt', 'Betrag (EUR)'])
        
        eintraege = [e for e in bot.data.get("euer", []) 
                     if ("privat" in e.get("zahlungsart", "").lower() or "bar" in e.get("zahlungsart", "").lower())
                     and (e.get("typ") in ["Einnahme", "STORNO"])]
        
        for e in eintraege:
            jid = e.get("journal_id", "-")
            datum = e.get("datum", "")
            typ = e.get("typ", "")
            konto = e.get("zahlungsart", "")
            prod = e.get("produkt", "")
            betrag = e.get("betrag", 0.0)
            
            if typ == "STORNO" and betrag < 0:
                gesamtsumme += betrag
            elif typ == "Einnahme":
                gesamtsumme += betrag
            elif betrag > 0:
                gesamtsumme += betrag
                
            betrag_str = f"{betrag:.2f}".replace('.', ',')
            writer.writerow([jid, datum, typ, konto, prod, betrag_str])
            
        writer.writerow(['', '', '', '', 'GESAMT (EUR)', f"{gesamtsumme:.2f}".replace('.', ',')])
        
    await interaction.response.send_message(f"💰 Gesamtsumme auf Privat & Bar: **{gesamtsumme:.2f} EUR**", file=discord.File(filename))
    os.remove(filename)

@bot.tree.command(name="kontoabgleich", description="Gleicht N26-Kontobewegungen gegen die Journalbuchungen ab (GoCardless)")
async def kontoabgleich(interaction: discord.Interaction, tage: int = 30):
    if n26_sync is None:
        return await interaction.response.send_message(
            "❌ n26_sync.py konnte nicht geladen werden (fehlende Abhaengigkeit?).", ephemeral=True
        )
    if not n26_sync.SECRET_ID or not n26_sync.SECRET_KEY:
        return await interaction.response.send_message(
            "❌ N26-Anbindung ist noch nicht eingerichtet. Setup-Schritte stehen im Kopf von `n26_sync.py`.",
            ephemeral=True,
        )

    await interaction.response.defer()
    try:
        unmatched_bank, unmatched_journal = await asyncio.to_thread(n26_sync.sync_flow, tage)
    except n26_sync.GoCardlessNotConfigured as e:
        return await interaction.followup.send(f"❌ {e}")
    except Exception as e:
        return await interaction.followup.send(f"❌ Fehler beim Abgleich: {e}")

    embed = discord.Embed(title=f"🏦 N26-Kontoabgleich (letzte {tage} Tage)", color=discord.Color.blue())
    if unmatched_bank:
        text = "\n".join(
            f"{t['bookingDate']} | {t['transactionAmount']['amount']} € | {t.get('remittanceInformationUnstructured', '-')}"
            for t in unmatched_bank[:15]
        )
        embed.add_field(name=f"⚠️ {len(unmatched_bank)} Kontobewegungen ohne Buchung", value=text[:1024] or "-", inline=False)
    else:
        embed.add_field(name="✅ Kontobewegungen", value="Alle gefunden Buchungen passen.", inline=False)

    if unmatched_journal:
        text = "\n".join(f"{e['datum']} | {e['betrag']} € | {e['produkt']}" for e in unmatched_journal[:15])
        embed.add_field(name=f"⚠️ {len(unmatched_journal)} Buchungen ohne Kontobewegung", value=text[:1024] or "-", inline=False)

    await interaction.followup.send(embed=embed)

# --- BOT START ---
logging.basicConfig(level=logging.INFO)

@bot.event
async def on_ready():
    print(f"✅ Eingeloggt als {bot.user.name}")
    try:
        synced = await bot.tree.sync()
        print(f"🔄 {len(synced)} Commands synchronisiert.")
    except Exception as e:
        print(f"❌ Sync-Fehler: {e}")

if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Start-Fehler: {e}")