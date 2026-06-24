#!/usr/bin/env python3
"""
Haalt alle moties en amendementen op uit iBabs Zaanstad
en schrijft ze weg als data/moties.json

Gebruik:
    python3 scrape_moties.py
"""

import json
import time
import sys
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

BASE_URL  = "https://zaanstad.bestuurlijkeinformatie.nl"
PAGE_URL  = f"{BASE_URL}/Reports/Details/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
DATA_URL  = f"{BASE_URL}/Reports/GetReportData/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
PAGE_SIZE = 100
OUTPUT    = "data/moties.json"

HEADERS = {
    "User-Agent":        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept":            "application/json, text/javascript, */*; q=0.01",
    "Content-Type":      "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With":  "XMLHttpRequest",
    "Referer":           PAGE_URL,
    "Origin":            BASE_URL,
}

# DataTables POST-body (vaste kolomdefinities uit de browser)
COLUMNS = [
    ("typeselectie",            False),
    ("title",                   False),
    ("datummotie",              True),
    ("raadsledenselectie",      True),
    ("fractieselectie",         True),
    ("medeondertekenaarsselectie", True),
    ("registrationdate",        True),
]

def build_body(start: int, draw: int) -> bytes:
    params = []
    params.append(("draw", str(draw)))
    for i, (name, searchable) in enumerate(COLUMNS):
        params.append((f"columns[{i}][data]",      name))
        params.append((f"columns[{i}][name]",      name))
        params.append((f"columns[{i}][searchable]", "true" if searchable else "false"))
        params.append((f"columns[{i}][orderable]",  "true"))
        val = "|" if name in ("datummotie", "registrationdate") else ""
        params.append((f"columns[{i}][search][value]", val))
        params.append((f"columns[{i}][search][regex]", "false"))
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


def parse_datum(s):
    """DD-MM-YYYY → YYYY-MM-DD, anders None"""
    if not s:
        return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return None


def parse_motie(row: dict) -> dict:
    titel = row.get("title", "").strip()

    # Type afleiden uit titel of typeselectie-veld
    type_raw = row.get("typeselectie", "").strip()
    if not type_raw:
        if titel.startswith("26A") or "Amendement" in titel:
            type_raw = "Amendement"
        else:
            type_raw = "Motie"

    # Fractie: eerste fractie als primaire partij
    fracties_raw = row.get("fractieselectie", "") or ""
    fracties = [f.strip() for f in fracties_raw.split("\r\n") if f.strip()]
    partij = fracties[0] if fracties else None

    # Indiener
    indiener = (row.get("raadsledenselectie") or "").strip() or None

    # Medeondertekenaars
    mede_raw = row.get("medeondertekenaarsselectie", "") or ""
    medeondertekenaars = [m.strip() for m in mede_raw.split("\r\n") if m.strip()]

    # Agendapunt / vergadering
    registratie = (row.get("registrationdate") or "").strip()

    return {
        "id":                row.get("DT_RowId"),
        "titel":             titel,
        "type":              type_raw,
        "partij":            partij,
        "fracties":          fracties,
        "indiener":          indiener,
        "medeondertekenaars": medeondertekenaars,
        "datum":             parse_datum(row.get("datummotie")),
        "agendapunt":        registratie,
        "status":            None,   # niet beschikbaar in dit endpoint
    }


def main():
    # Stap 1: sessie ophalen via GET
    print("Sessie ophalen...", end=" ", flush=True)
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request(PAGE_URL, headers={"User-Agent": HEADERS["User-Agent"]})
        opener.open(req, timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e})")
        print("Doorgaan zonder sessiecookie...")

    # Stap 2: eerste pagina ophalen om totaal te weten
    print("Eerste pagina ophalen...", end=" ", flush=True)
    try:
        req = urllib.request.Request(DATA_URL, data=build_body(0, 1), headers=HEADERS)
        with opener.open(req, timeout=30) as resp:
            first = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"\nFout: {e}")
        print("\nDe sessiecookie is verlopen. Haal een verse cookie op:")
        print("1. Open de motie-pagina in Chrome")
        print("2. DevTools → Network → zoek de XHR-call → Copy as cURL")
        print("3. Kopieer de -b '...' cookie-string en plak die in dit script bij COOKIE_OVERRIDE")
        sys.exit(1)

    total = first.get("recordsTotal", 0)
    print(f"OK — {total} moties gevonden")

    # Stap 3: alle pagina's ophalen
    all_rows = list(first.get("data", []))
    draw = 2
    start = PAGE_SIZE

    while start < total:
        remaining = total - start
        print(f"  Ophalen {start}–{min(start + PAGE_SIZE, total)} van {total}...", end=" ", flush=True)
        try:
            req = urllib.request.Request(DATA_URL, data=build_body(start, draw), headers=HEADERS)
            with opener.open(req, timeout=30) as resp:
                page = json.loads(resp.read().decode("utf-8"))
            rows = page.get("data", [])
            all_rows.extend(rows)
            print(f"{len(rows)} rijen")
        except Exception as e:
            print(f"FOUT: {e} — even wachten en opnieuw...")
            time.sleep(3)
            continue

        draw += 1
        start += PAGE_SIZE
        time.sleep(0.3)  # beleefd wachten

    # Stap 4: parsen en wegschrijven
    print(f"\n{len(all_rows)} rijen opgehaald, parsen...")
    moties = [parse_motie(r) for r in all_rows]

    import os
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(moties, f, ensure_ascii=False, indent=2)

    print(f"✓ Weggeschreven naar {OUTPUT}")
    print(f"  {sum(1 for m in moties if m['type'] == 'Motie')} moties")
    print(f"  {sum(1 for m in moties if m['type'] == 'Amendement')} amendementen")
    print(f"  Status: altijd None (niet beschikbaar in dit iBabs-endpoint)")


if __name__ == "__main__":
    main()
