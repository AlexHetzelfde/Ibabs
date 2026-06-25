#!/usr/bin/env python3
"""
Haalt raadsinformatiebrieven en kennisgevingen op uit iBabs Zaanstad,
downloadt de bijbehorende PDFs, extraheert de tekst, en laat Gemini
checkwaardige claims identificeren.

Resultaat wordt opgeslagen in data/collegeberichten.json

Gebruik:
    python3 scrape_collegeberichten.py

Vereiste omgevingsvariabelen:
    GEMINI_API_KEY  — Gemini API key voor claimanalyse

Optionele omgevingsvariabelen:
    SCRAPE_VANAF    — datum in YYYY-MM-DD formaat (standaard: afgelopen 7 dagen)
"""

import json
import re
import time
import sys
import os
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from datetime import datetime, timedelta

# ── CONFIGURATIE ──────────────────────────────────────────────────────────────
BASE_URL       = "https://zaanstad.bestuurlijkeinformatie.nl"
LIJST_PAGE_URL = f"{BASE_URL}/Reports/Details/8ea04074-52e6-4284-bd1a-66e378b40ec1"
LIJST_DATA_URL = f"{BASE_URL}/Reports/GetReportData/8ea04074-52e6-4284-bd1a-66e378b40ec1"
PAGE_SIZE      = 100
OUTPUT         = "data/collegeberichten.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)

# Alleen deze typen zijn journalistiek relevant
RELEVANTE_TYPEN = {"Raadsinformatiebrief", "Kennisgeving"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE_URL,
}

# Kolomdefinities op basis van de response-structuur
COLUMNS = [
    ("title",                    False),
    ("datumbericht",             True),
    ("portefeuillehouderselectie", True),
    ("typeselectie",             True),
    ("afhandelingselectie",      True),
    ("registrationdate",         True),
]


# ── HELPERS ───────────────────────────────────────────────────────────────────
def parse_datum(s):
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def build_lijst_body(start, draw):
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(COLUMNS):
        params += [
            (f"columns[{i}][data]",          name),
            (f"columns[{i}][name]",          name),
            (f"columns[{i}][searchable]",    "true"),
            (f"columns[{i}][orderable]",     "true"),
            (f"columns[{i}][search][value]", "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", "5"),
        ("order[0][dir]",    "desc"),
        ("order[0][name]",   "registrationdate"),
        ("start",            str(start)),
        ("length",           str(PAGE_SIZE)),
        ("search[value]",    ""),
        ("search[regex]",    "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")


# ── PDF OPHALEN & TEKST EXTRAHEREN ────────────────────────────────────────────
def haal_document_id(opener, item_id):
    """
    Haalt de documentId op via de detailpagina van een bericht.
    Patroon: /Reports/Document/{item_id}?documentId={doc_id}
    """
    url = f"{BASE_URL}/Reports/Item/{item_id}"
    req = urllib.request.Request(
        url, headers={**HEADERS, "Accept": "text/html"}
    )
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"(detailpagina mislukt: {e})")
        return None

    # Zoek naar documentId in links
    m = re.search(
        r"/Reports/Document/" + re.escape(item_id) +
        r"\?documentId=([a-f0-9\-]{36})",
        html
    )
    if m:
        return m.group(1)

    # Fallback: zoek breder naar documentId=
    m = re.search(r"documentId=([a-f0-9\-]{36})", html)
    return m.group(1) if m else None


def download_pdf(opener, item_id, document_id):
    """Download de PDF en geef de raw bytes terug."""
    url = f"{BASE_URL}/Reports/Document/{item_id}?documentId={document_id}"
    req = urllib.request.Request(
        url, headers={**HEADERS, "Accept": "application/pdf,*/*"}
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.read()
    except Exception as e:
        print(f"(PDF download mislukt: {e})")
        return None


def extraheer_pdf_tekst(pdf_bytes):
    """
    Extraheert tekst uit PDF bytes via pypdf.
    Installeer met: pip install pypdf --break-system-packages
    """
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        tekst_delen = []
        for pagina in reader.pages:
            tekst = pagina.extract_text()
            if tekst:
                tekst_delen.append(tekst)
        return "\n".join(tekst_delen).strip()
    except ImportError:
        print("  ⚠ pypdf niet geïnstalleerd. Installeer met: pip install pypdf --break-system-packages")
        return None
    except Exception as e:
        print(f"  (PDF-tekst extractie mislukt: {e})")
        return None


# ── GEMINI CLAIMANALYSE ───────────────────────────────────────────────────────
def analyseer_claims(tekst, titel, portefeuillehouder, api_key):
    """
    Laat Gemini checkwaardige claims identificeren in de brieftekst.
    Geeft een lijst van claim-objecten terug.
    """
    if not api_key or not tekst:
        return []

    # Tekst beperken tot 8000 tekens voor de API
    tekst_kort = tekst[:8000]

    prompt = f"""Je bent een factcheck-assistent voor een journalist die collegebrieven van de gemeente Zaanstad analyseert.

Document: "{titel}"
Portefeuillehouder: {portefeuillehouder or "onbekend"}

Analyseer de onderstaande tekst en identificeer alle feitelijke claims die verifieerbaar zijn.
Denk aan: getallen, percentages, datums, tijdlijnen, beloftes van het college, budgetten, aantallen woningen of inwoners, vergelijkingen met eerdere jaren, statusupdates op moties of eerdere beloftes.

Geef voor elke claim:
- De exacte claim (kort en precies)
- Hoe een journalist dit kan controleren (welke bron, welk document)
- Prioriteit: HOOG / MIDDEL / LAAG
- Score: 0-100 (hoe checkwaardig)

Maximaal 8 claims, HOOG eerst.

Antwoord ALLEEN met een JSON-array, geen markdown, geen uitleg:
[{{"claim":"...","verificatie":"...","prioriteit":"HOOG","score":85}}]

Tekst:
{tekst_kort}"""

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
    }).encode("utf-8")

    url = f"{GEMINI_URL}?key={api_key}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw   = data["candidates"][0]["content"]["parts"][0]["text"]
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return []
        return json.loads(match.group(0))

    except Exception as e:
        print(f"  (Gemini mislukt: {e})")
        return []


# ── BESTAANDE DATA LADEN ──────────────────────────────────────────────────────
def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {b["id"]: b for b in data}


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────
def main():
    # Gemini API key
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("⚠  GEMINI_API_KEY niet ingesteld — claims worden niet geanalyseerd")

    # Datumbereik
    vandaag   = datetime.now()
    vanaf_env = os.environ.get("SCRAPE_VANAF", "").strip()
    grens     = vanaf_env if vanaf_env else (vandaag - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Collegeberichten vanaf: {grens}")

    # Sessie opbouwen
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    print("Sessie ophalen...", end=" ", flush=True)
    try:
        opener.open(urllib.request.Request(
            LIJST_PAGE_URL,
            headers={"User-Agent": HEADERS["User-Agent"]}
        ), timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e}) — doorgaan zonder sessie")

    # Eerste pagina ophalen
    lijst_headers = {**HEADERS, "Referer": LIJST_PAGE_URL}
    print("Lijst ophalen...", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            LIJST_DATA_URL,
            data=build_lijst_body(0, 1),
            headers=lijst_headers
        )
        with opener.open(req, timeout=30) as resp:
            first = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"\nFout bij ophalen lijst: {e}")
        sys.exit(1)

    total    = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    print(f"OK — {total} items totaal")

    # Resterende pagina's
    draw, start = 2, PAGE_SIZE
    while start < total:
        req = urllib.request.Request(
            LIJST_DATA_URL,
            data=build_lijst_body(start, draw),
            headers=lijst_headers
        )
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1
        start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren: alleen relevante typen en recente datums
    relevante_rows = [
        r for r in all_rows
        if r.get("typeselectie", "") in RELEVANTE_TYPEN
        and (parse_datum(r.get("datumbericht")) or "") >= grens
    ]
    print(f"{len(relevante_rows)} relevante brieven vanaf {grens}")

    # Bestaande data
    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} brieven")

    # Per brief: PDF downloaden, tekst extraheren, claims analyseren
    print("Brieven verwerken...")
    verwerkt = 0
    for i, row in enumerate(relevante_rows):
        item_id = row.get("DT_RowId")
        titel   = row.get("title", "").strip()
        datum   = parse_datum(row.get("datumbericht"))
        type_   = row.get("typeselectie", "")
        ph_raw  = row.get("portefeuillehouderselectie", "") or ""
        ph      = ", ".join([p.strip() for p in ph_raw.split("\r\n") if p.strip()])

        print(f"  [{i+1}/{len(relevante_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        # Sla over als al verwerkt én al claims heeft
        if item_id in bestaand and bestaand[item_id].get("claims"):
            print("→ al verwerkt, overgeslagen")
            continue

        # DocumentId ophalen
        doc_id = haal_document_id(opener, item_id)
        if not doc_id:
            print("→ geen documentId gevonden")
            bestaand[item_id] = {
                "id":                item_id,
                "titel":             titel,
                "type":              type_,
                "datum":             datum,
                "portefeuillehouder": ph,
                "url":               f"{BASE_URL}/Reports/Item/{item_id}",
                "tekst":             None,
                "claims":            [],
                "bijgewerkt":        vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # PDF downloaden
        time.sleep(0.3)
        pdf_bytes = download_pdf(opener, item_id, doc_id)
        if not pdf_bytes:
            print("→ PDF niet beschikbaar")
            bestaand[item_id] = {
                "id":                item_id,
                "titel":             titel,
                "type":              type_,
                "datum":             datum,
                "portefeuillehouder": ph,
                "url":               f"{BASE_URL}/Reports/Item/{item_id}",
                "pdf_url":           f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
                "tekst":             None,
                "claims":            [],
                "bijgewerkt":        vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # Tekst extraheren
        tekst = extraheer_pdf_tekst(pdf_bytes)
        tekst_preview = f"{len(tekst)} tekens" if tekst else "geen tekst"

        # Claims analyseren
        claims = []
        if tekst and api_key:
            claims = analyseer_claims(tekst, titel, ph, api_key)

        print(f"→ {tekst_preview} · {len(claims)} claims")

        bestaand[item_id] = {
            "id":                item_id,
            "titel":             titel,
            "type":              type_,
            "datum":             datum,
            "portefeuillehouder": ph,
            "url":               f"{BASE_URL}/Reports/Item/{item_id}",
            "pdf_url":           f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
            "tekst":             tekst[:5000] if tekst else None,  # bewaar eerste 5000 tekens
            "claims":            claims,
            "bijgewerkt":        vandaag.strftime("%Y-%m-%d"),
        }
        verwerkt += 1
        time.sleep(0.5)

    # Opslaan — nieuwste eerst
    resultaat = sorted(
        bestaand.values(),
        key=lambda x: x.get("datum") or "",
        reverse=True,
    )
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    # Samenvatting
    totaal_claims = sum(len(b.get("claims") or []) for b in resultaat)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {verwerkt} brieven nieuw verwerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    print(f"  {totaal_claims} claims geïdentificeerd")


if __name__ == "__main__":
    main()
