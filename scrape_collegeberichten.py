#!/usr/bin/env python3
"""
Haalt raadsinformatiebrieven en kennisgevingen op uit iBabs Zaanstad,
downloadt de bijbehorende PDFs, extraheert de tekst, en detecteert
checkwaardige claims via twee lagen:

  1. CODE — altijd actief, regex-patronen op geldbedragen, percentages,
             beloftetaal, datumdeadlines, vage beweringen
  2. AI   — optioneel, Gemini 1.5 Flash voor diepere claimanalyse

Claims krijgen een "bron"-veld: "code" of "ai".
Resultaat wordt opgeslagen in data/collegeberichten.json

Gebruik:
    python3 scrape_collegeberichten.py

Vereiste omgevingsvariabelen:
    GEMINI_API_KEY  — Gemini API key (optioneel, alleen voor AI-laag)

Optionele omgevingsvariabelen:
    SCRAPE_VANAF    — datum YYYY-MM-DD (standaard: afgelopen 7 dagen)
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

COLUMNS = [
    ("title",                      False),
    ("datumbericht",               True),
    ("portefeuillehouderselectie", True),
    ("typeselectie",               True),
    ("afhandelingselectie",        True),
    ("registrationdate",           True),
]

# ── PRIORITEITSSCORES ─────────────────────────────────────────────────────────
PRIO_SCORE = {"HOOG": 75, "MIDDEL": 45, "LAAG": 20}

# ── CODE-GEBASEERDE CLAIMDETECTIE ─────────────────────────────────────────────
#
# Elk patroon is een tuple van:
#   (regex, prioriteit, verificatietip)
#
# De detectie werkt per zin: als een zin matcht op een patroon
# wordt die zin als claim opgeslagen. Eén match per zin volstaat.
#
CODE_PATRONEN = [
    # Geldbedragen — altijd HOOG, altijd controleerbaar
    (
        r'€\s*[\d.,]+(?:\s*(?:miljoen|mln|duizend|k))?',
        "HOOG",
        "Controleer bedrag via gemeentebegroting, raadsstuk of jaarverslag"
    ),
    # Percentages
    (
        r'\b\d+[,.]\d+\s*%|\b\d+\s*%',
        "HOOG",
        "Controleer percentage via bron, onderzoeksrapport of CBS-data"
    ),
    # Concrete aantallen mensen/woningen
    (
        r'\b\d+\s*(?:woningen|appartementen|inwoners|huishoudens|bewoners|'
        r'vluchtelingen|statushouders|leerlingen|medewerkers|fte|arbeidsplaatsen)',
        "HOOG",
        "Controleer aantal via CBS, gemeentelijke rapportage of aanbieder"
    ),
    # Concrete aantallen incidenten/meldingen
    (
        r'\b\d+\s*(?:meldingen|klachten|incidenten|overtredingen|'
        r'aanvragen|bezwaren|vergunningen)',
        "MIDDEL",
        "Controleer via jaarrapportage handhaving of gemeentelijke registratie"
    ),
    # Datumdeadlines en tijdskaders
    (
        r'(?:voor|eind|in|per|uiterlijk)\s+'
        r'(?:20\d\d|dit jaar|volgend jaar|begin \d{4}|medio \d{4}|Q[1-4]\s*20\d\d)',
        "MIDDEL",
        "Controleer deadline via eerder raadsstuk, motie of collegebrief"
    ),
    # Expliciete beloftes en toezeggingen
    (
        r'(?:zal worden|gaan we|wordt gerealiseerd|is toegezegd|'
        r'hebben wij toegezegd|wordt opgeleverd|zullen wij|'
        r'nemen wij|doen wij|streven wij|is onze inzet)',
        "MIDDEL",
        "Controleer toezegging via eerdere collegebrieven, moties of raadsvragen"
    ),
    # Vergelijkingen met eerdere periodes
    (
        r'(?:ten opzichte van|vergeleken met|meer dan vorig jaar|'
        r'minder dan vorig jaar|stijging van|daling van|'
        r'toegenomen met|afgenomen met|hoger dan|lager dan)',
        "MIDDEL",
        "Controleer vergelijking via jaarrapportage, CBS of vorige collegebrief"
    ),
    # Wetsartikelen en beleidsreferenties
    (
        r'(?:artikel\s+\d+[a-z]?|wet\s+[A-Z][a-z]+|'
        r'besluit\s+[A-Z][a-z]+|verordening\s+[a-z])',
        "LAAG",
        "Controleer wetsartikel of beleidsdocument via overheid.nl of gemeentearchief"
    ),
    # Vage superlatieven zonder onderbouwing
    (
        r'\b(?:structureel|aanzienlijk|fors|significant|'
        r'sterk gestegen|sterk gedaald|substantieel|'
        r'groot aantal|veel meer|veel minder|hoog risico)\b',
        "LAAG",
        "Vage bewering zonder getal of bron — vraag om kwantificering"
    ),
]


def detecteer_code_claims(tekst):
    """
    Detecteert checkwaardige claims via regex-patronen.
    Werkt altijd, ook zonder Gemini API key.
    Geeft maximaal 10 claims terug met bron='code'.
    """
    if not tekst:
        return []

    # Splits op zinsgrenzen
    zinnen = re.split(r'(?<=[.!?])\s+|\n', tekst)

    claims = []
    gezien = set()  # voorkom duplicaten

    for zin in zinnen:
        zin = zin.strip()
        # Te kort of te lang om zinvol te zijn
        if len(zin) < 25 or len(zin) > 500:
            continue

        for patroon, prioriteit, verificatie in CODE_PATRONEN:
            if re.search(patroon, zin, re.IGNORECASE):
                # Dedupliceer op basis van eerste 50 tekens
                sleutel = zin[:50].lower()
                if sleutel in gezien:
                    break
                gezien.add(sleutel)

                claims.append({
                    "claim":      zin[:250],
                    "verificatie": verificatie,
                    "prioriteit": prioriteit,
                    "score":      PRIO_SCORE[prioriteit],
                    "bron":       "code",
                    "kruischeck": None,
                })
                break  # één match per zin is genoeg

        if len(claims) >= 10:
            break

    # Sorteer: HOOG eerst
    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    claims.sort(key=lambda c: volgorde.get(c["prioriteit"], 9))

    return claims


# ── AI-GEBASEERDE CLAIMANALYSE ────────────────────────────────────────────────
def analyseer_ai_claims(tekst, titel, portefeuillehouder, api_key):
    if not api_key or not tekst:
        return []

    tekst_kort = tekst[:8000]
    prompt = f"""..."""  # ongewijzigd
    body = json.dumps({
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}
}).encode("utf-8")

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("error"):
                print(f"  ({model} mislukt: {data['error'].get('message')})")
                continue
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            match = re.search(r"\[[\s\S]*\]", raw)
            if not match:
                return []
            ai_claims = json.loads(match.group(0))
            for c in ai_claims:
                c["bron"] = "ai"
                c["kruischeck"] = c.get("kruischeck") or None
            return ai_claims
        except Exception as e:
            print(f"  ({model} mislukt: {e})")
            continue

    return []

    tekst_kort = tekst[:8000]

    prompt = f"""Je bent een factcheck-assistent voor een journalist die collegebrieven van de gemeente Zaanstad analyseert.

Document: "{titel}"
Portefeuillehouder: {portefeuillehouder or "onbekend"}

Analyseer de onderstaande tekst en identificeer alle feitelijke claims die verifieerbaar zijn.
Denk aan: getallen, percentages, datums, tijdlijnen, beloftes van het college, budgetten, aantallen woningen of inwoners, vergelijkingen met eerdere jaren, statusupdates op moties of eerdere beloftes.

Geef voor elke claim:
- De exacte claim (kort en precies, max 200 tekens)
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
        url, data=body,
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
        ai_claims = json.loads(match.group(0))
        # Voeg bron-veld toe
        for c in ai_claims:
            c["bron"]       = "ai"
            c["kruischeck"] = c.get("kruischeck") or None
        return ai_claims
    except Exception as e:
        print(f"  (Gemini mislukt: {e})")
        return []


# ── CLAIMS SAMENVOEGEN ────────────────────────────────────────────────────────
def combineer_claims(code_claims, ai_claims):
    """
    Voegt code- en AI-claims samen.
    Verwijdert code-claims die al door AI zijn gevonden
    (op basis van overlap in de eerste 40 tekens).
    AI-claims gaan voor omdat ze rijker zijn.
    """
    if not ai_claims:
        return code_claims

    ai_teksten = {c["claim"][:40].lower() for c in ai_claims}

    unieke_code = [
        c for c in code_claims
        if c["claim"][:40].lower() not in ai_teksten
    ]

    gecombineerd = ai_claims + unieke_code

    # Sorteer: HOOG eerst, dan score
    volgorde = {"HOOG": 0, "MIDDEL": 1, "LAAG": 2}
    gecombineerd.sort(key=lambda c: (
        volgorde.get(c.get("prioriteit", "LAAG"), 9),
        -(c.get("score") or 0)
    ))

    return gecombineerd[:12]  # max 12 gecombineerde claims


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
    url = f"{BASE_URL}/Reports/Item/{item_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"(detailpagina mislukt: {e})")
        return None

    m = re.search(
        r"/Reports/Document/" + re.escape(item_id) +
        r"\?documentId=([a-f0-9\-]{36})", html
    )
    if m:
        return m.group(1)
    m = re.search(r"documentId=([a-f0-9\-]{36})", html)
    return m.group(1) if m else None


def download_pdf(opener, item_id, document_id):
    url = f"{BASE_URL}/Document/View/{document_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":              "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language":         "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer":                 f"{BASE_URL}/Reports/Item//{item_id}",
            "sec-ch-ua":               '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile":        "?0",
            "sec-ch-ua-platform":      '"macOS"',
            "sec-fetch-dest":          "document",
            "sec-fetch-mode":          "navigate",
            "sec-fetch-site":          "same-origin",
            "sec-fetch-user":          "?1",
            "upgrade-insecure-requests": "1",
        }
    )
    try:
        with opener.open(req, timeout=30) as resp:
            data = resp.read()
            if data[:4] != b'%PDF':
                print(f"(geen PDF ontvangen, eerste bytes: {data[:20]})")
                return None
            return data
    except Exception as e:
        print(f"(PDF download mislukt: {e})")
        return None


def extraheer_pdf_tekst(pdf_bytes):
    try:
        import pypdf
        import io
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        delen = []
        for pagina in reader.pages:
            tekst = pagina.extract_text()
            if tekst:
                delen.append(tekst)
        return "\n".join(delen).strip()
    except ImportError:
        print("  ⚠ pypdf niet geïnstalleerd — pip install pypdf --break-system-packages")
        return None
    except Exception as e:
        print(f"  (PDF-tekst extractie mislukt: {e})")
        return None


# ── BESTAANDE DATA ────────────────────────────────────────────────────────────
def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {b["id"]: b for b in data}


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────
def main():
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        print("✓ Gemini API key gevonden — AI-laag actief")
    else:
        print("⚠  Geen GEMINI_API_KEY — alleen code-gebaseerde claimdetectie")

    vandaag   = datetime.now()
    vanaf_env = os.environ.get("SCRAPE_VANAF", "").strip()
    grens     = vanaf_env if vanaf_env else (vandaag - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Collegeberichten vanaf: {grens}")

    # Sessie
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    print("Sessie ophalen...", end=" ", flush=True)
    try:
        opener.open(urllib.request.Request(
            LIJST_PAGE_URL, headers={"User-Agent": HEADERS["User-Agent"]}
        ), timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e}) — doorgaan zonder sessie")

    # Eerste pagina
    lijst_headers = {**HEADERS, "Referer": LIJST_PAGE_URL}
    print("Lijst ophalen...", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            LIJST_DATA_URL, data=build_lijst_body(0, 1), headers=lijst_headers
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
            LIJST_DATA_URL, data=build_lijst_body(start, draw), headers=lijst_headers
        )
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren
    relevante_rows = [
        r for r in all_rows
        if r.get("typeselectie", "") in RELEVANTE_TYPEN
        and (parse_datum(r.get("datumbericht")) or "") >= grens
    ]
    print(f"{len(relevante_rows)} relevante brieven vanaf {grens}")

    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} brieven")

    # Per brief verwerken
    print("Brieven verwerken...")
    verwerkt = 0
    code_totaal = 0
    ai_totaal   = 0

    for i, row in enumerate(relevante_rows):
        item_id = row.get("DT_RowId")
        titel   = row.get("title", "").strip()
        datum   = parse_datum(row.get("datumbericht"))
        type_   = row.get("typeselectie", "")
        ph_raw  = row.get("portefeuillehouderselectie", "") or ""
        ph      = ", ".join([p.strip() for p in ph_raw.split("\r\n") if p.strip()])

        print(f"  [{i+1}/{len(relevante_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        # Overslaan als al verwerkt met claims
        if item_id in bestaand and bestaand[item_id].get("claims"):
            print("→ al verwerkt, overgeslagen")
            continue

        # DocumentId ophalen
        doc_id = haal_document_id(opener, item_id)
        if not doc_id:
            print("→ geen documentId gevonden")
            bestaand[item_id] = {
                "id": item_id, "titel": titel, "type": type_,
                "datum": datum, "portefeuillehouder": ph,
                "url": f"{BASE_URL}/Reports/Item/{item_id}",
                "tekst": None, "claims": [],
                "bijgewerkt": vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # PDF downloaden
        time.sleep(0.3)
        pdf_bytes = download_pdf(opener, item_id, doc_id)
        if not pdf_bytes:
            print("→ PDF niet beschikbaar")
            bestaand[item_id] = {
                "id": item_id, "titel": titel, "type": type_,
                "datum": datum, "portefeuillehouder": ph,
                "url": f"{BASE_URL}/Reports/Item/{item_id}",
                "pdf_url": f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
                "tekst": None, "claims": [],
                "bijgewerkt": vandaag.strftime("%Y-%m-%d"),
            }
            time.sleep(0.4)
            continue

        # Tekst extraheren
        tekst = extraheer_pdf_tekst(pdf_bytes)

        # Laag 1: code-gebaseerde claims (altijd)
        code_claims = detecteer_code_claims(tekst) if tekst else []

        # Laag 2: AI-claims (optioneel)
        ai_claims = []
        if tekst and api_key:
            ai_claims = analyseer_ai_claims(tekst, titel, ph, api_key)
            time.sleep(0.3)

        # Combineren
        alle_claims = combineer_claims(code_claims, ai_claims)

        code_totaal += len(code_claims)
        ai_totaal   += len(ai_claims)

        tekst_info = f"{len(tekst)} tekens" if tekst else "geen tekst"
        print(
            f"→ {tekst_info} · "
            f"{len(code_claims)} code-claims · "
            f"{len(ai_claims)} AI-claims · "
            f"{len(alle_claims)} totaal"
        )

        bestaand[item_id] = {
            "id":                item_id,
            "titel":             titel,
            "type":              type_,
            "datum":             datum,
            "portefeuillehouder": ph,
            "url":               f"{BASE_URL}/Reports/Item/{item_id}",
            "pdf_url":           f"{BASE_URL}/Reports/Document/{item_id}?documentId={doc_id}",
            "tekst":             tekst[:5000] if tekst else None,
            "claims":            alle_claims,
            "bijgewerkt":        vandaag.strftime("%Y-%m-%d"),
        }
        verwerkt += 1
        time.sleep(0.5)

    # Opslaan
    resultaat = sorted(
        bestaand.values(),
        key=lambda x: x.get("datum") or "",
        reverse=True,
    )
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    totaal_claims = sum(len(b.get("claims") or []) for b in resultaat)
    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {verwerkt} brieven nieuw verwerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    print(f"  {code_totaal} code-claims gedetecteerd")
    print(f"  {ai_totaal} AI-claims gedetecteerd")
    print(f"  {totaal_claims} claims totaal in JSON")


if __name__ == "__main__":
    main()
