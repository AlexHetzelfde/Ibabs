#!/usr/bin/env python3
"""
Haalt stemmingen op uit iBabs Zaanstad,
inclusief per-raadslid stemgedrag per motie/besluit.

Gebruik:
    python3 scrape_stemmingen.py

Optionele omgevingsvariabelen:
    SCRAPE_VANAF    — datum YYYY-MM-DD (standaard: afgelopen 30 dagen)
"""

import json
import re
import time
import sys
import os
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime, timedelta

# ── CONFIGURATIE ──────────────────────────────────────────────────────────────
BASE_URL       = "https://zaanstad.bestuurlijkeinformatie.nl"
LIJST_PAGE_URL = f"{BASE_URL}/Reports/Details/8e7af291-79d7-457f-88ca-e3c780df6eb2"
LIJST_DATA_URL = f"{BASE_URL}/Reports/GetReportData/8e7af291-79d7-457f-88ca-e3c780df6eb2"
PAGE_SIZE      = 100
OUTPUT         = "data/stemmingen.json"

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
    ("title",              False),
    ("datum",              True),
    ("uitslag",            True),
    ("registrationdate",   True),
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
        ("order[0][column]", "3"),
        ("order[0][dir]",    "desc"),
        ("order[0][name]",   "registrationdate"),
        ("start",            str(start)),
        ("length",           str(PAGE_SIZE)),
        ("search[value]",    ""),
        ("search[regex]",    "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")


# ── DETAIL OPHALEN ────────────────────────────────────────────────────────────
def _vind_buitenste_ul(html, zoek_vanaf):
    """
    Zoekt de eerste <ul> na positie zoek_vanaf en geeft de inhoud terug
    van de complete buitenste ul (geneste uls inbegrepen) via depth-counting.
    Retourneert de tekst TUSSEN <ul> en </ul>, of None als niet gevonden.
    """
    ul_start = html.find("<ul>", zoek_vanaf)
    if ul_start == -1:
        return None

    depth = 0
    i = ul_start
    ul_end = -1

    while i < len(html):
        if html[i:i+4] == "<ul>":
            depth += 1
            i += 4
        elif html[i:i+5] == "</ul>":
            depth -= 1
            if depth == 0:
                ul_end = i
                break
            i += 5
        else:
            i += 1

    if ul_end == -1:
        return None

    # Geef de inhoud terug TUSSEN <ul> en </ul> (dus niet de tags zelf)
    return html[ul_start + 4 : ul_end]


def _splits_namen(namen_tekst):
    """
    Splitst een iBabs namenstring in losse raadsleden.

    iBabs-formaat: 'Achternaam, Voornaam [tussenvoegsel] , Achternaam2, Voornaam2 , ...'
    De scheiding tussen personen is ' , ' (spatie-komma-spatie).
    Binnen een naam scheidt de komma achternaam van voornaam (geen spatie VOOR de komma).

    Fallback: als ' , ' niets oplevert, probeer dan splitsen op ', ' gevolgd door
    een hoofdletter (voor namen zonder tussenvoegsel en zonder spatie voor de komma).
    """
    # Normaliseer witruimte
    tekst = re.sub(r"\s+", " ", namen_tekst).strip()

    # Primaire split: spatie-komma-spatie (= scheiding tussen personen)
    delen = [d.strip() for d in tekst.split(" , ") if d.strip()]

    if len(delen) > 1:
        return [d.rstrip(",").strip() for d in delen if d]

    # Fallback: sommige namen hebben geen spatie voor de komma
    # Splits op ', ' gevolgd door een hoofdletter (begin nieuw achternaam)
    delen_fb = [d.strip() for d in re.split(r",\s+(?=[A-Z])", tekst) if d.strip()]

    # Plak achternaam en voornaam weer samen: elk even element is voornaam
    # bij het patroon Achternaam, Voornaam → na split krijg je [Acht, Vnm, Acht2, Vnm2]
    # Hergroepeer per twee
    if len(delen_fb) >= 2 and len(delen_fb) % 2 == 0:
        namen = []
        for j in range(0, len(delen_fb), 2):
            naam = f"{delen_fb[j]}, {delen_fb[j+1]}"
            namen.append(naam.strip())
        return namen

    # Laatste redmiddel: geef de hele tekst terug als één naam
    return [tekst] if tekst else []


def fetch_stemming_detail(opener, item_id):
    """
    Haalt de detailpagina op en parseert:
    - voor_pct / tegen_pct / onthouding_pct  (via w-XX klasse)
    - uitslag_tekst                           (via bar-text span)
    - fracties_voor / fracties_tegen / fracties_onthouding (via .text div)
    - raadsleden_voor / raadsleden_tegen / raadsleden_onthouding
      (via vote-summary-legend-details)

    FIX t.o.v. vorige versie:
    1. Nested-<ul> bug: gebruikt depth-counting i.p.v. non-greedy regex
       zodat de volledige buitenste <ul> wordt geparsed, niet alleen de eerste
       fractie.
    2. Naamssplitsing: splitst op ' , ' (spatie voor de komma = persoonscheiding)
       in plaats van op komma-hoofdletter (wat binnen een naam ook voorkomt).
    """
    url = f"{BASE_URL}/Reports/Item/{item_id}"
    req = urllib.request.Request(
        url,
        headers={**HEADERS, "Accept": "text/html"}
    )
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"(detailpagina mislukt: {e})")
        return {}

    result = {}

    # ── Percentages via w-XX klasse ───────────────────────────────────────
    m = re.search(r"vote-summary-bar-in-favour[\w\s-]*\bw-(\d+)\b", html)
    if m:
        result["voor_pct"] = int(m.group(1))

    m = re.search(r"vote-summary-bar-against[\w\s-]*\bw-(\d+)\b", html)
    if m:
        result["tegen_pct"] = int(m.group(1))

    m = re.search(r"vote-summary-bar-abstain[\w\s-]*\bw-(\d+)\b", html)
    if m:
        result["onthouding_pct"] = int(m.group(1))

    # Vul ontbrekende percentages aan
    voor  = result.get("voor_pct",  0)
    tegen = result.get("tegen_pct", 0)
    onth  = result.get("onthouding_pct", 0)
    if voor and not tegen and not onth:
        result["tegen_pct"]      = 0
        result["onthouding_pct"] = 0
    elif voor and tegen and not onth:
        result["onthouding_pct"] = max(0, 100 - voor - tegen)

    # ── Uitslag tekst ─────────────────────────────────────────────────────
    m = re.search(r"vote-summary-bar-in-favour-text[^>]*>\s*([^<]+)", html)
    if m:
        result["uitslag_tekst"] = m.group(1).strip()

    # ── Fractie-samenvattingen per categorie ──────────────────────────────
    for cat, css in [("voor", "in-favour"), ("tegen", "against"), ("onthouding", "abstain")]:
        m = re.search(
            rf"vote-summary-legend-{css}[^>]*>.*?<div class=\"text\">\s*(.*?)\s*</div>",
            html, re.DOTALL
        )
        if m:
            result[f"fracties_{cat}"] = re.sub(r"\s+", " ", m.group(1)).strip()

    # ── Individuele raadsleden uit details-div ────────────────────────────
    #
    # FIX: de details-div heeft geneste <ul>-tags:
    #   <div class="vote-summary-legend-details ...">
    #     <ul>                         ← buitenste ul
    #       <li>Fractie (n)
    #         <ul><li>Namen...</li></ul>  ← geneste ul
    #       </li>
    #       ...
    #     </ul>
    #   </div>
    #
    # De oude regex r'<ul>(.*?)</ul>' stopte bij de EERSTE </ul> (de geneste),
    # waardoor alleen fractie 1 werd geparsed.
    # Oplossing: zoek de positie van de details-div en gebruik depth-counting
    # om de complete buitenste <ul> te vinden.

    details_pos = html.find("vote-summary-legend-details")
    if details_pos != -1:
        details_html = _vind_buitenste_ul(html, details_pos)
    else:
        details_html = None

    if details_html:
        # Elke <li> in de buitenste ul is één fractie met een geneste <ul><li>namen</li></ul>
        fractie_blokken = re.findall(
            r"<li>\s*(.*?)\s*\(\d+\)\s*<ul>\s*<li>(.*?)</li>\s*</ul>\s*</li>",
            details_html, re.DOTALL
        )

        # Bouw mapping: fractienaam → lijst raadsleden
        fractie_namen: dict = {}
        for fractie_raw, namen_raw in fractie_blokken:
            fractie = re.sub(r"\s+", " ", fractie_raw).strip()
            namen_tekst = re.sub(r"\s+", " ", namen_raw).strip()

            # FIX: gebruik _splits_namen() in plaats van de kapotte regex-split
            namen = _splits_namen(namen_tekst)
            fractie_namen[fractie] = namen

        # Categoriseer fracties via de summary-teksten
        voor_tekst  = result.get("fracties_voor",       "")
        tegen_tekst = result.get("fracties_tegen",       "")
        onth_tekst  = result.get("fracties_onthouding",  "")

        raadsleden_voor  = []
        raadsleden_tegen = []
        raadsleden_onth  = []

        for fractie, namen in fractie_namen.items():
            if fractie in tegen_tekst:
                doellijst = raadsleden_tegen
            elif fractie in onth_tekst:
                doellijst = raadsleden_onth
            else:
                # Default: voor (ook als er helemaal geen opsplitsing is)
                doellijst = raadsleden_voor

            for naam in namen:
                doellijst.append({"naam": naam, "fractie": fractie})

        if raadsleden_voor:
            result["raadsleden_voor"]       = raadsleden_voor
        if raadsleden_tegen:
            result["raadsleden_tegen"]      = raadsleden_tegen
        if raadsleden_onth:
            result["raadsleden_onthouding"] = raadsleden_onth

    return result


# ── BESTAANDE DATA ────────────────────────────────────────────────────────────
def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {s["id"]: s for s in data}


# ── HOOFDPROGRAMMA ────────────────────────────────────────────────────────────
def main():
    vandaag   = datetime.now()
    vanaf_env = os.environ.get("SCRAPE_VANAF", "").strip()
    grens     = vanaf_env if vanaf_env else (vandaag - timedelta(days=30)).strftime("%Y-%m-%d")
    print(f"Stemmingen vanaf: {grens}")

    # Sessie
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

    # Eerste pagina
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
    print(f"OK — {total} stemmingen totaal")

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
        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren op datum
    recente_rows = [
        r for r in all_rows
        if (parse_datum(r.get("datum")) or "") >= grens
    ]
    print(f"{len(recente_rows)} stemmingen vanaf {grens}")

    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} stemmingen")

    # Per stemming detailpagina ophalen
    print("Detailpagina's ophalen...")
    nieuw = 0

    for i, row in enumerate(recente_rows):
        item_id  = row.get("DT_RowId")
        titel    = row.get("title", "").strip()
        datum    = parse_datum(row.get("datum"))
        uitslag  = (row.get("uitslag") or "").strip()

        print(f"  [{i+1}/{len(recente_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        # FIX: alleen overslaan als er écht raadsledendata aanwezig is.
        # [] is niet None, dus de oude check liet lege arrays altijd door als "al verwerkt".
        al_verwerkt = (
            item_id in bestaand
            and len(bestaand[item_id].get("raadsleden_voor") or []) > 0
        )
        if al_verwerkt:
            print("→ al verwerkt, overgeslagen")
            continue

        detail = fetch_stemming_detail(opener, item_id)

        raadsleden_voor  = detail.get("raadsleden_voor",       [])
        raadsleden_tegen = detail.get("raadsleden_tegen",      [])
        raadsleden_onth  = detail.get("raadsleden_onthouding", [])

        print(
            f"→ {detail.get('voor_pct', '?')}% voor · "
            f"{len(raadsleden_voor)} voor · "
            f"{len(raadsleden_tegen)} tegen · "
            f"{len(raadsleden_onth)} onth."
        )

        bestaand[item_id] = {
            "id":                    item_id,
            "titel":                 titel,
            "datum":                 datum,
            "uitslag":               uitslag,
            "uitslag_tekst":         detail.get("uitslag_tekst"),
            "voor_pct":              detail.get("voor_pct"),
            "tegen_pct":             detail.get("tegen_pct"),
            "onthouding_pct":        detail.get("onthouding_pct"),
            "fracties_voor":         detail.get("fracties_voor"),
            "fracties_tegen":        detail.get("fracties_tegen"),
            "fracties_onthouding":   detail.get("fracties_onthouding"),
            "raadsleden_voor":       raadsleden_voor,
            "raadsleden_tegen":      raadsleden_tegen,
            "raadsleden_onthouding": raadsleden_onth,
            "url":                   f"{BASE_URL}/Reports/Item/{item_id}",
            "bijgewerkt":            vandaag.strftime("%Y-%m-%d"),
        }
        nieuw += 1
        time.sleep(0.4)

    # Opslaan
    resultaat = sorted(
        bestaand.values(),
        key=lambda x: x.get("datum") or "",
        reverse=True,
    )
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    totaal_voor  = sum(len(s.get("raadsleden_voor",  []) or []) for s in resultaat)
    totaal_tegen = sum(len(s.get("raadsleden_tegen", []) or []) for s in resultaat)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} stemmingen nieuw verwerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    print(f"  {totaal_voor} voor-stemmen · {totaal_tegen} tegen-stemmen geregistreerd")


if __name__ == "__main__":
    main()
