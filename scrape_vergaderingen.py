#!/usr/bin/env python3
"""
Haalt raadsvergaderingen op van de afgelopen 7 dagen (+ komende 30 dagen)
en voegt ze toe aan data/vergaderingen.json

Gebruik:
    python3 scrape_vergaderingen.py
"""

import json
import time
import re
import os
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime, timedelta

BASE_URL     = "https://zaanstad.bestuurlijkeinformatie.nl"
CALENDAR_URL = f"{BASE_URL}/Calendar"
OUTPUT       = "data/vergaderingen.json"
RAAD_CLASS   = "agendatype-100491844"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":  "*/*",
    "Referer": CALENDAR_URL,
}


def fetch_agenda_range(opener, start_dt, end_dt):
    start_str = urllib.parse.quote(start_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    end_str   = urllib.parse.quote(end_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    url = f"{BASE_URL}/Calendar/GetAgendasForCalendar?start={start_str}&end={end_str}"
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_vergadering_details(opener, agenda_id):
    url = f"{BASE_URL}/Agenda/Index/{agenda_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"(HTML-fetch mislukt: {e})", end=" ")
        return [], None

    # Agendapunten
    punten = []
    matches = re.findall(
        r'<div[^>]*class="[^"]*\bpanel-id\b[^"]*"[^>]*>\s*([^<]+)\s*</div>'
        r'.{0,500}?'
        r'<span[^>]*class="[^"]*\bpanel-title-label\b[^"]*"[^>]*>\s*([^<]+)\s*</span>',
        html, re.DOTALL,
    )
    for nummer, titel in matches:
        if nummer.strip() and titel.strip():
            punten.append({"nummer": nummer.strip(), "titel": titel.strip()})

    # Video
    video_id   = None
    video_link = None
        

    return punten, video_link, video_id

def load_existing():
    """Laad bestaande vergaderingen.json als die bestaat."""
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    # Dict op ID voor snelle dedup
    return {v["id"]: v for v in data}


def main():
    # Datumbereik: via env var SCRAPE_VANAF of standaard afgelopen 7 dagen
    import os
    vandaag       = datetime.now()
    over_30_dagen = vandaag + timedelta(days=30)
    vanaf_env     = os.environ.get("SCRAPE_VANAF", "").strip()
    week_geleden  = datetime.strptime(vanaf_env, "%Y-%m-%d") if vanaf_env else vandaag - timedelta(days=7)

    print(f"Bereik: {week_geleden.strftime('%Y-%m-%d')} t/m {over_30_dagen.strftime('%Y-%m-%d')}")

    # Sessie
    print("Sessie ophalen...", end=" ", flush=True)
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(CALENDAR_URL, headers=HEADERS), timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e})")

    # Vergaderingen ophalen voor dit bereik
    print("Vergaderingen ophalen...", end=" ", flush=True)
    try:
        items = fetch_agenda_range(opener, week_geleden, over_30_dagen)
        raad  = [i for i in items if RAAD_CLASS in i.get("classNames", [])]
        print(f"{len(raad)} raadsvergaderingen")
    except Exception as e:
        print(f"FOUT: {e}")
        return

    # Bestaande data inladen
    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} vergaderingen")

    # Nieuwe/gewijzigde vergaderingen verwerken
    nieuw = 0
    for i, item in enumerate(raad):
        agenda_id = item["id"]
        titel     = item.get("title", "").strip()
        start_str = item.get("start", "")
        datum     = start_str[:10] if start_str else None

        print(f"  [{i+1}/{len(raad)}] {datum} {titel}", end=" ", flush=True)

        agendapunten, video_link, video_id = fetch_vergadering_details(opener, agenda_id)
        print(f"— {len(agendapunten)} punten{' · video ✓' if video_link else ''}")

        bestaand[agenda_id] = {
            "id":           agenda_id,
            "titel":        titel,
            "type":         "Raadsvergadering",
            "datum":        datum,
            "start":        start_str,
            "eind":         item.get("end", ""),
            "locatie":      item.get("location"),
            "url":          f"{BASE_URL}{item.get('url', '')}",
            "video_id":     video_id,
            "video_link":   video_link,
            "heeft_video":  True,
            "agendapunten": agendapunten,
            "bijgewerkt":   vandaag.strftime("%d-%m-%Y"),
        }
        nieuw += 1
        time.sleep(0.4)

    # Opslaan: nieuwste eerst
    resultaat = sorted(bestaand.values(), key=lambda x: x.get("start", ""), reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} vergaderingen toegevoegd/bijgewerkt")
    print(f"  {len(resultaat)} totaal in JSON")


if __name__ == "__main__":
    main()
