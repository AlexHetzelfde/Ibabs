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
    ("datumstemming",      True),
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
def fetch_stemming_detail(opener, item_id):
    """
    Haalt de detailpagina op en parseert:
    - voor_pct / tegen_pct / onthouding_pct
    - fracties_voor / fracties_tegen / fracties_onthouding
    - raadsleden_voor / raadsleden_tegen / raadsleden_onthouding
      (elk een lijst van dicts: {naam, fractie})
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

    # ── Percentages uit de balk ───────────────────────────────────────────
    m = re.search(r'vote-summary-bar-in-favour\s+w-(\d+)', html)
    if m:
        result["voor_pct"] = int(m.group(1))

    m = re.search(r'vote-summary-bar-against\s+w-(\d+)', html)
    if m:
        result["tegen_pct"] = int(m.group(1))

    m = re.search(r'vote-summary-bar-abstain\s+w-(\d+)', html)
    if m:
        result["onthouding_pct"] = int(m.group(1))

    # Bereken ontbrekend percentage
    voor   = result.get("voor_pct", 0)
    tegen  = result.get("tegen_pct", 0)
    onth   = result.get("onthouding_pct", 0)
    if voor and not tegen and not onth:
        result["tegen_pct"]      = 0
        result["onthouding_pct"] = 0
    elif voor and tegen and not onth:
        result["onthouding_pct"] = max(0, 100 - voor - tegen)

    # ── Uitslag tekst ─────────────────────────────────────────────────────
    m = re.search(
        r'vote-summary-bar-in-favour[^>]*>.*?<span[^>]*>([^<]+)</span>',
        html, re.DOTALL
    )
    if m:
        result["uitslag_tekst"] = m.group(1).strip()

    # ── Fracties samenvatting per categorie ───────────────────────────────
    for cat, css in [("voor", "in-favour"), ("tegen", "against"), ("onthouding", "abstain")]:
        m = re.search(
            rf'vote-summary-legend-{css}[^>]*>.*?<div class="text">\s*(.*?)\s*</div>',
            html, re.DOTALL
        )
        if m:
            result[f"fracties_{cat}"] = m.group(1).strip()

    # ── Individuele raadsleden per categorie ──────────────────────────────
    # Structuur in HTML:
    # <div class="vote-summary-legend-details hidden votes-{id}">
    #   <ul>
    #     <li>Fractienaam (n)
    #       <ul><li>Naam1 , Naam2</li></ul>
    #     </li>
    #   </ul>
    # </div>
    #
    # Elke categorie heeft zijn eigen legend-sectie.
    # We parsen per categorie het bijbehorende details-blok.

    for cat, css in [("voor", "in-favour"), ("tegen", "against"), ("onthouding", "abstain")]:
        # Vind het legend-blok voor deze categorie
        blok_match = re.search(
            rf'<div class="vote-summary-legend-{css}[^"]*"[^>]*>(.*?)</div>\s*</div>',
            html, re.DOTALL
        )
        if not blok_match:
            continue

        blok = blok_match.group(1)

        # Vind de details-ul in dit blok
        details_match = re.search(
            r'vote-summary-legend-details[^"]*">(.*?)</div>',
            blok, re.DOTALL
        )
        if not details_match:
            # Probeer het direct na het blok te vinden via vote-id
            details_match = re.search(
                r'class="vote-summary-legend-details[^>]+>(.*?)</div>\s*</div>',
                html, re.DOTALL
            )

        raadsleden = []

        # Parse <li>Fractie (n)<ul><li>Naam1 , Naam2</li></ul></li>
        li_matches = re.findall(
            r'<li>\s*([^<\n]+?)\s*\(\d+\)\s*<ul>(.*?)</ul>\s*</li>',
            blok, re.DOTALL
        )
        for fractie_raw, namen_html in li_matches:
            fractie = fractie_raw.strip()
            namen_raw = re.sub(r'<[^>]+>', '', namen_html).strip()
            # Namen staan gescheiden door komma's, achternaam eerst
            for naam in re.split(r'\s*,\s*(?=[A-Z])', namen_raw):
                naam = naam.strip().rstrip(',').strip()
                if naam:
                    raadsleden.append({
                        "naam":    naam,
                        "fractie": fractie
                    })

        if raadsleden:
            result[f"raadsleden_{cat}"] = raadsleden

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
    print("Voorbeeld rij:", json.dumps(all_rows[0], ensure_ascii=False)[:300])
    recente_rows = [
        r for r in all_rows
        if (parse_datum(r.get("datumstemming")) or "") >= grens
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
        datum    = parse_datum(row.get("datumstemming"))
        uitslag  = (row.get("uitslag") or "").strip()

        print(f"  [{i+1}/{len(recente_rows)}] {datum} — {titel[:55]}", end=" ", flush=True)

        # Overslaan als al verwerkt met raadsledendata
        if item_id in bestaand and bestaand[item_id].get("raadsleden_voor") is not None:
            print("→ al verwerkt, overgeslagen")
            continue

        detail = fetch_stemming_detail(opener, item_id)

        raadsleden_voor  = detail.get("raadsleden_voor", [])
        raadsleden_tegen = detail.get("raadsleden_tegen", [])
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
