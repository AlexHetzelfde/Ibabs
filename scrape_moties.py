#!/usr/bin/env python3
"""
Haalt moties en amendementen op uit iBabs Zaanstad,
haalt per motie de uitslag + stemresultaten op via de detailpagina,
en voegt ze toe aan data/moties.json

Gebruik:
    python3 scrape_moties.py
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

BASE_URL = "https://zaanstad.bestuurlijkeinformatie.nl"

MOTIES_PAGE_URL = f"{BASE_URL}/Reports/Details/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
MOTIES_DATA_URL = f"{BASE_URL}/Reports/GetReportData/4b5dcb7b-adc3-4253-bad3-7bfd16341021"

PAGE_SIZE = 100
OUTPUT    = "data/moties.json"

HEADERS = {
    "User-Agent":       (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":           "application/json, text/javascript, */*; q=0.01",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE_URL,
}

MOTIES_COLUMNS = [
    ("typeselectie",               False),
    ("title",                      False),
    ("datummotie",                 True),
    ("raadsledenselectie",         True),
    ("fractieselectie",            True),
    ("medeondertekenaarsselectie", True),
    ("registrationdate",           True),
]


def build_moties_body(start, draw):
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(MOTIES_COLUMNS):
        params += [
            (f"columns[{i}][data]",          name),
            (f"columns[{i}][name]",          name),
            (f"columns[{i}][searchable]",    "true"),
            (f"columns[{i}][orderable]",     "true"),
            (f"columns[{i}][search][value]", "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", "6"),
        ("order[0][dir]",    "desc"),
        ("order[0][name]",   "registrationdate"),
        ("start",            str(start)),
        ("length",           str(PAGE_SIZE)),
        ("search[value]",    ""),
        ("search[regex]",    "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")


def fetch_stemming_detail(opener, motie_id):
    """
    Haalt uitslag, voor/tegen percentages en fractielijsten op
    via de motie-detailpagina: /Reports/Item/{motie_id}

    De DT_RowId van de motie IS de UUID van de detailpagina —
    geen aparte stemmingen-koppeling nodig.
    """
    url = f"{BASE_URL}/Reports/Item/{motie_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        return {}

    result = {}

    # Uitslag / status
    # HTML: <dt class="col-sm-3">Uitslag</dt><dd class="col-sm-9">Aangenomen</dd>
    m = re.search(
        r'<dt[^>]*>\s*Uitslag\s*</dt>\s*<dd[^>]*>\s*([^<]+)\s*</dd>',
        html
    )
    if m:
        result["status"] = m.group(1).strip().lower()

    # Voor-percentage: class="vote-summary-bar-in-favour w-59 d-flex"
    m = re.search(r'vote-summary-bar-in-favour\s+w-(\d+)', html)
    if m:
        result["voor_pct"]  = int(m.group(1))
        result["tegen_pct"] = 100 - int(m.group(1))

    # Fracties voor: <div class="vote-summary-legend-in-favour ..."><div class="text">...</div>
    m = re.search(
        r'vote-summary-legend-in-favour[^>]*>.*?<div class="text">\s*([^<]+?)\s*</div>',
        html, re.DOTALL
    )
    if m:
        result["fracties_voor"] = m.group(1).strip()

    # Fracties tegen: <div class="vote-summary-legend-against ..."><div class="text">...</div>
    m = re.search(
        r'vote-summary-legend-against[^>]*>.*?<div class="text">\s*([^<]+?)\s*</div>',
        html, re.DOTALL
    )
    if m:
        result["fracties_tegen"] = m.group(1).strip()

    return result


def parse_datum(s):
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def parse_motie(row):
    titel    = row.get("title", "").strip()
    type_raw = row.get("typeselectie", "").strip()
    if not type_raw:
        type_raw = "Amendement" if ("26A" in titel or "Amendement" in titel) else "Motie"

    fracties_raw = row.get("fractieselectie", "") or ""
    fracties     = [f.strip() for f in fracties_raw.split("\r\n") if f.strip()]
    mede_raw     = row.get("medeondertekenaarsselectie", "") or ""

    return {
        "id":                 row.get("DT_RowId"),
        "titel":              titel,
        "type":               type_raw,
        "partij":             fracties[0] if fracties else None,
        "fracties":           fracties,
        "indiener":           (row.get("raadsledenselectie") or "").strip() or None,
        "medeondertekenaars": [m.strip() for m in mede_raw.split("\r\n") if m.strip()],
        "datum":              parse_datum(row.get("datummotie")),
        "agendapunt":         (row.get("registrationdate") or "").strip(),
        "status":             None,
        "voor_pct":           None,
        "tegen_pct":          None,
        "fracties_voor":      None,
        "fracties_tegen":     None,
    }


def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {m["id"]: m for m in data}


def main():
    # Datumbereik: via env var SCRAPE_VANAF of standaard afgelopen 7 dagen
    vandaag     = datetime.now()
    vanaf_env   = os.environ.get("SCRAPE_VANAF", "").strip()
    grens_datum = vanaf_env if vanaf_env else (vandaag - timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"Alleen moties vanaf: {grens_datum}")

    # Sessie
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(
            MOTIES_PAGE_URL, headers={"User-Agent": HEADERS["User-Agent"]}
        ), timeout=15)
    except Exception:
        pass

    # Moties ophalen
    moties_headers = {**HEADERS, "Referer": MOTIES_PAGE_URL}
    print("Moties ophalen...", end=" ", flush=True)
    try:
        req = urllib.request.Request(
            MOTIES_DATA_URL, data=build_moties_body(0, 1), headers=moties_headers
        )
        with opener.open(req, timeout=30) as resp:
            first = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"\nFout: {e}")
        sys.exit(1)

    total    = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    print(f"OK — {total} totaal")

    draw, start = 2, PAGE_SIZE
    while start < total:
        req = urllib.request.Request(
            MOTIES_DATA_URL, data=build_moties_body(start, draw), headers=moties_headers
        )
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren op datum
    recente_rows = [
        r for r in all_rows
        if (parse_datum(r.get("datummotie")) or "") >= grens_datum
    ]
    print(f"{len(recente_rows)} moties vanaf {grens_datum}")

    # Bestaande data inladen
    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} moties")

    # Per motie detailpagina ophalen voor uitslag + stemresultaten
    print("Detailpagina's ophalen...")
    for i, row in enumerate(recente_rows):
        m = parse_motie(row)
        print(f"  [{i+1}/{len(recente_rows)}] {m['datum']} {m['titel'][:50]}", end=" ", flush=True)
        detail = fetch_stemming_detail(opener, m["id"])
        m.update(detail)
        status_label = m.get("status") or "geen uitslag"
        print(f"→ {status_label}")
        bestaand[m["id"]] = m
        time.sleep(0.35)

    # Opslaan: nieuwste eerst
    resultaat = sorted(
        bestaand.values(),
        key=lambda x: x.get("datum") or "",
        reverse=True,
    )
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {len(recente_rows)} moties verwerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    met_status = sum(1 for m in resultaat if m.get("status"))
    print(f"  {met_status}/{len(resultaat)} met stemuitslag")


if __name__ == "__main__":
    main()
