#!/usr/bin/env python3
"""
Haalt moties en amendementen op van de afgelopen 7 dagen,
koppelt stemstatus via het stemmingen-endpoint,
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

MOTIES_PAGE_URL     = f"{BASE_URL}/Reports/Details/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
MOTIES_DATA_URL     = f"{BASE_URL}/Reports/GetReportData/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
STEMMINGEN_PAGE_URL = f"{BASE_URL}/Reports/Details/8e7af291-79d7-457f-88ca-e3c780df6eb2"
STEMMINGEN_DATA_URL = f"{BASE_URL}/Reports/GetReportData/8e7af291-79d7-457f-88ca-e3c780df6eb2"

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

STEMMINGEN_COLUMNS = [
    ("identity",         False),
    ("datum",            True),
    ("title",            False),
    ("status",           False),
    ("registrationdate", True),
]


def _build_body(columns, order_col, order_name, start, draw):
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(columns):
        params += [
            (f"columns[{i}][data]",          name),
            (f"columns[{i}][name]",          name),
            (f"columns[{i}][searchable]",    "true"),
            (f"columns[{i}][orderable]",     "true"),
            (f"columns[{i}][search][value]", "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", str(order_col)),
        ("order[0][dir]",    "desc"),
        ("order[0][name]",   order_name),
        ("start",            str(start)),
        ("length",           str(PAGE_SIZE)),
        ("search[value]",    ""),
        ("search[regex]",    "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")


def build_moties_body(start, draw):
    return _build_body(MOTIES_COLUMNS, 6, "registrationdate", start, draw)

def build_stemmingen_body(start, draw):
    return _build_body(STEMMINGEN_COLUMNS, 0, "identity", start, draw)


def parse_datum(s):
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def normalize(s):
    return re.sub(r"\s+", " ", (s or "").lower().strip())


def fetch_stemmingen(opener):
    headers = {**HEADERS, "Referer": STEMMINGEN_PAGE_URL}
    try:
        opener.open(urllib.request.Request(
            STEMMINGEN_PAGE_URL,
            headers={"User-Agent": HEADERS["User-Agent"]}
        ), timeout=15)
    except Exception:
        pass

    req = urllib.request.Request(
        STEMMINGEN_DATA_URL,
        data=build_stemmingen_body(0, 1),
        headers=headers,
    )
    with opener.open(req, timeout=30) as resp:
        first = json.loads(resp.read().decode("utf-8"))

    total    = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    draw, start = 2, PAGE_SIZE

    while start < total:
        req = urllib.request.Request(
            STEMMINGEN_DATA_URL,
            data=build_stemmingen_body(start, draw),
            headers=headers,
        )
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    result = {}
    for row in all_rows:
        titel  = normalize(row.get("title", ""))
        status = (row.get("status") or "").strip().lower()
        if titel and status:
            result[titel] = status
    return result


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
    }


def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {m["id"]: m for m in data}


def main():
    vandaag      = datetime.now()
    week_geleden = vandaag - timedelta(days=7)
    grens_datum  = week_geleden.strftime("%Y-%m-%d")

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

    # Moties ophalen (altijd alle pagina's — we filteren daarna op datum)
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
        rows = page.get("data", [])
        all_rows.extend(rows)
        draw += 1; start += PAGE_SIZE
        time.sleep(0.3)

    # Filteren op afgelopen week
    recente_rows = [
        r for r in all_rows
        if (parse_datum(r.get("datummotie")) or "") >= grens_datum
    ]
    print(f"{len(recente_rows)} moties van afgelopen week")

    # Stemmingen ophalen
    print("Stemmingen ophalen...", end=" ", flush=True)
    try:
        stemmingen = fetch_stemmingen(opener)
        print(f"OK — {len(stemmingen)} stemmingen")
    except Exception as e:
        print(f"MISLUKT ({e})")
        stemmingen = {}

    # Bestaande data inladen
    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} moties")

    # Nieuwe moties toevoegen / bestaande updaten
    for row in recente_rows:
        m = parse_motie(row)
        m["status"] = stemmingen.get(normalize(m["titel"]))
        bestaand[m["id"]] = m

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
    print(f"  {len(recente_rows)} moties toegevoegd/bijgewerkt")
    print(f"  {len(resultaat)} totaal in JSON")
    met_status = sum(1 for m in resultaat if m["status"])
    print(f"  {met_status}/{len(resultaat)} met status")


if __name__ == "__main__":
    main()
