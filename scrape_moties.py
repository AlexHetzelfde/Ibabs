#!/usr/bin/env python3
"""
Haalt alle moties en amendementen op uit iBabs Zaanstad,
koppelt de stemstatus via het stemmingen-endpoint,
en schrijft alles weg als data/moties.json

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
import urllib.error
import http.cookiejar

BASE_URL  = "https://zaanstad.bestuurlijkeinformatie.nl"

# ── Moties-endpoint ────────────────────────────────────────
MOTIES_PAGE_URL = f"{BASE_URL}/Reports/Details/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
MOTIES_DATA_URL = f"{BASE_URL}/Reports/GetReportData/4b5dcb7b-adc3-4253-bad3-7bfd16341021"

# ── Stemmingen-endpoint ────────────────────────────────────
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


# ── POST-body builders ─────────────────────────────────────

# Kolomdefinities moties (volgorde uit iBabs DataTables-call)
MOTIES_COLUMNS = [
    ("typeselectie",               False),
    ("title",                      False),
    ("datummotie",                 True),   # True → search[value]="|"
    ("raadsledenselectie",         True),
    ("fractieselectie",            True),
    ("medeondertekenaarsselectie", True),
    ("registrationdate",           True),
]

# Kolomdefinities stemmingen (uit cURL)
STEMMINGEN_COLUMNS = [
    ("identity",         False),
    ("datum",            True),
    ("title",            False),
    ("status",           False),
    ("registrationdate", True),
]


def _build_body(columns, order_col, order_name, start, draw):
    """Generieke DataTables POST-body builder."""
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(columns):
        params += [
            (f"columns[{i}][data]",            name),
            (f"columns[{i}][name]",            name),
            (f"columns[{i}][searchable]",      "true"),
            (f"columns[{i}][orderable]",       "true"),
            (f"columns[{i}][search][value]",   "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]",   "false"),
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


# ── Helpers ────────────────────────────────────────────────

def parse_datum(s):
    """DD-MM-YYYY → YYYY-MM-DD, anders None."""
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def normalize(s: str) -> str:
    """Titel normaliseren voor fuzzy matching op status."""
    return re.sub(r"\s+", " ", (s or "").lower().strip())


# ── Stemmingen ophalen ─────────────────────────────────────

def fetch_stemmingen(opener) -> dict:
    """
    Haalt alle stemmingen op uit het stemmingen-endpoint.
    Geeft een dict terug: {normalized_title: status_string}

    Stemstatus-waarden zijn zoals iBabs ze levert, bijv.:
        "Aangenomen", "Verworpen", "Ingetrokken", "Aangehouden"
    We slaan ze op in lowercase voor consistentie met de rest van de JSON.
    """
    headers = {**HEADERS, "Referer": STEMMINGEN_PAGE_URL}

    # Sessie voor dit endpoint ophalen
    try:
        req = urllib.request.Request(
            STEMMINGEN_PAGE_URL,
            headers={"User-Agent": HEADERS["User-Agent"]}
        )
        opener.open(req, timeout=15)
    except Exception:
        pass  # Geen sessie nodig als cookies al bestaan

    # Eerste pagina
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
        draw  += 1
        start += PAGE_SIZE
        time.sleep(0.3)

    # Dict: genormaliseerde titel → lowercase status
    result = {}
    for row in all_rows:
        titel      = normalize(row.get("title", ""))
        status_raw = (row.get("status") or "").strip().lower()
        if titel and status_raw:
            result[titel] = status_raw

    return result


# ── Moties parsen ──────────────────────────────────────────

def parse_motie(row: dict) -> dict:
    titel = row.get("title", "").strip()

    type_raw = row.get("typeselectie", "").strip()
    if not type_raw:
        type_raw = "Amendement" if (titel.startswith("26A") or "Amendement" in titel) else "Motie"

    fracties_raw = row.get("fractieselectie", "") or ""
    fracties     = [f.strip() for f in fracties_raw.split("\r\n") if f.strip()]
    partij       = fracties[0] if fracties else None

    indiener  = (row.get("raadsledenselectie") or "").strip() or None
    mede_raw  = row.get("medeondertekenaarsselectie", "") or ""
    medeondertekenaars = [m.strip() for m in mede_raw.split("\r\n") if m.strip()]

    return {
        "id":                  row.get("DT_RowId"),
        "titel":               titel,
        "type":                type_raw,
        "partij":              partij,
        "fracties":            fracties,
        "indiener":            indiener,
        "medeondertekenaars":  medeondertekenaars,
        "datum":               parse_datum(row.get("datummotie")),
        "agendapunt":          (row.get("registrationdate") or "").strip(),
        "status":              None,   # wordt hieronder gevuld vanuit stemmingen
    }


# ── Main ───────────────────────────────────────────────────

def main():
    # Sessie ophalen
    print("Sessie ophalen...", end=" ", flush=True)
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request(
            MOTIES_PAGE_URL,
            headers={"User-Agent": HEADERS["User-Agent"]}
        )
        opener.open(req, timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e}) — doorgaan zonder sessiecookie")

    # Eerste pagina moties ophalen
    print("Eerste pagina moties ophalen...", end=" ", flush=True)
    try:
        moties_headers = {**HEADERS, "Referer": MOTIES_PAGE_URL}
        req = urllib.request.Request(
            MOTIES_DATA_URL,
            data=build_moties_body(0, 1),
            headers=moties_headers,
        )
        with opener.open(req, timeout=30) as resp:
            first = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"\nFout: {e}")
        sys.exit(1)

    total    = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    print(f"OK — {total} moties gevonden")

    # Resterende pagina's moties
    draw, start = 2, PAGE_SIZE
    while start < total:
        print(f"  Ophalen {start}–{min(start + PAGE_SIZE, total)} van {total}...", end=" ", flush=True)
        try:
            req = urllib.request.Request(
                MOTIES_DATA_URL,
                data=build_moties_body(start, draw),
                headers=moties_headers,
            )
            with opener.open(req, timeout=30) as resp:
                page = json.loads(resp.read().decode("utf-8"))
            rows = page.get("data", [])
            all_rows.extend(rows)
            print(f"{len(rows)} rijen")
        except Exception as e:
            print(f"FOUT: {e} — even wachten en opnieuw...")
            time.sleep(3)
            continue
        draw  += 1
        start += PAGE_SIZE
        time.sleep(0.3)

    # Stemmingen ophalen voor statuskoppeling
    print(f"\nStemmingen ophalen voor statuskoppeling...", end=" ", flush=True)
    try:
        stemmingen = fetch_stemmingen(opener)
        print(f"OK — {len(stemmingen)} stemmingen")
    except Exception as e:
        print(f"MISLUKT ({e}) — status blijft None voor alle moties")
        stemmingen = {}

    # Parsen + status koppelen
    print(f"\n{len(all_rows)} rijen parsen...")
    moties = []
    for row in all_rows:
        m = parse_motie(row)
        # Status koppelen op genormaliseerde titel
        key = normalize(m["titel"])
        m["status"] = stemmingen.get(key)
        moties.append(m)

    # Wegschrijven
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(moties, f, ensure_ascii=False, indent=2)

    print(f"✓ Weggeschreven naar {OUTPUT}")
    print(f"  {sum(1 for m in moties if m['type'] == 'Motie')} moties")
    print(f"  {sum(1 for m in moties if m['type'] == 'Amendement')} amendementen")

    met_status = sum(1 for m in moties if m["status"])
    print(f"  {met_status}/{len(moties)} met status gekoppeld")

    # Overzicht statussen
    from collections import Counter
    statussen = Counter(m["status"] for m in moties if m["status"])
    for status, n in statussen.most_common():
        print(f"    {status}: {n}")


if __name__ == "__main__":
    main()
