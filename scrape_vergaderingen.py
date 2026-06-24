#!/usr/bin/env python3
"""
Haalt alle raadsvergaderingen op uit iBabs Zaanstad
en schrijft ze weg als data/vergaderingen.json

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
from datetime import datetime

BASE_URL     = "https://zaanstad.bestuurlijkeinformatie.nl"
CALENDAR_URL = f"{BASE_URL}/Calendar"
OUTPUT       = "data/vergaderingen.json"

# Alleen raadsvergaderingen — classname uit de Calendar-API
RAAD_CLASS   = "agendatype-100491844"

# Jaren ophalen
JAAR_START   = 2025
JAAR_EIND    = 2026

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept":  "*/*",
    "Referer": CALENDAR_URL,
}


# ── CALENDAR API ───────────────────────────────────────────

def fetch_agenda_range(opener, start_dt, end_dt):
    """Haalt vergaderingen op voor een datumbereik via de Calendar-API."""
    start_str = urllib.parse.quote(start_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    end_str   = urllib.parse.quote(end_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    url = (
        f"{BASE_URL}/Calendar/GetAgendasForCalendar"
        f"?start={start_str}&end={end_str}"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── VERGADERPAGINA (agendapunten + video in één request) ───

def fetch_vergadering_details(opener, agenda_id):
    """
    Haalt agendapunten en video-link op uit /Agenda/Index/{id}.
    Één HTTP-request per vergadering.

    Geeft terug: (agendapunten: list, video_link: str|None)
    """
    url = f"{BASE_URL}/Agenda/Index/{agenda_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        print(f"(HTML-fetch mislukt: {e})", end=" ")
        return [], None

    agendapunten = parse_agendapunten(html)
    video_link   = parse_video_link(html)
    return agendapunten, video_link


def parse_agendapunten(html: str) -> list:
    """
    Parst agendapunten uit iBabs HTML.

    Structuur op de pagina:
        <div class="panel-id">6.1</div>
        ...
        <span class="panel-title-label">Voorjaarsnota 2026-2030</span>
    """
    punten = []

    # Elke combinatie van panel-id + panel-title-label binnen dezelfde sectie
    matches = re.findall(
        r'<div[^>]*class="[^"]*\bpanel-id\b[^"]*"[^>]*>\s*([^<]+)\s*</div>'
        r'.{0,500}?'
        r'<span[^>]*class="[^"]*\bpanel-title-label\b[^"]*"[^>]*>\s*([^<]+)\s*</span>',
        html,
        re.DOTALL,
    )
    for nummer, titel in matches:
        nummer = nummer.strip()
        titel  = titel.strip()
        if nummer and titel:
            punten.append({"nummer": nummer, "titel": titel})

    return punten


def parse_video_link(html: str) -> str | None:
    """
    Zoekt het CompanyWebcast video-ID op in de HTML.

    Structuur op de pagina:
        <div class="cwc" data-video-id="zaanstad/20260611_1" ...>

    De player-URL wordt:
        https://player.companywebcast.com/player/?id=zaanstad_20260611_1&display=126
    (Slash in het ID wordt underscore — zo werkt de player-embed.)

    Deze URL werkt direct met yt-dlp:
        yt-dlp "https://player.companywebcast.com/player/?id=zaanstad_20260611_1&display=126"
    """
    m = re.search(r'data-video-id="([^"]+)"', html)
    if not m:
        return None

    raw_id   = m.group(1).strip()          # bijv. "zaanstad/20260611_1"
    player_id = raw_id.replace("/", "_")   # → "zaanstad_20260611_1"
    return (
        f"https://player.companywebcast.com/player/"
        f"?id={player_id}&display=126&customBtnColor=006a81"
    )


# ── MAIN ───────────────────────────────────────────────────

def main():
    # Sessie ophalen
    print("Sessie ophalen...", end=" ", flush=True)
    jar    = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        req = urllib.request.Request(CALENDAR_URL, headers=HEADERS)
        opener.open(req, timeout=15)
        print("OK")
    except Exception as e:
        print(f"MISLUKT ({e}) — doorgaan zonder sessie")

    # Vergaderingen ophalen per jaar
    alle_items = []
    for jaar in range(JAAR_START, JAAR_EIND + 1):
        start = datetime(jaar, 1, 1)
        eind  = datetime(jaar, 12, 31)
        print(f"Jaar {jaar} ophalen...", end=" ", flush=True)
        try:
            items = fetch_agenda_range(opener, start, eind)
            raad  = [i for i in items if RAAD_CLASS in i.get("classNames", [])]
            print(f"{len(raad)} raadsvergaderingen")
            alle_items.extend(raad)
        except Exception as e:
            print(f"FOUT: {e}")

    # Nieuwste eerst
    alle_items.sort(key=lambda x: x.get("start", ""), reverse=True)

    print(f"\n{len(alle_items)} raadsvergaderingen gevonden")
    print("Agendapunten en video-links ophalen (dit duurt even)...")

    vergaderingen = []
    for i, item in enumerate(alle_items):
        agenda_id = item["id"]
        titel     = item.get("title", "").strip()
        start_str = item.get("start", "")
        datum     = start_str[:10] if start_str else None

        print(f"  [{i+1}/{len(alle_items)}] {datum} {titel}", end=" ", flush=True)

        agendapunten, video_link = fetch_vergadering_details(opener, agenda_id)

        print(
            f"— {len(agendapunten)} punten"
            f"{' · video ✓' if video_link else ''}"
        )

        vergaderingen.append({
            "id":           agenda_id,
            "titel":        titel,
            "type":         "Raadsvergadering",
            "datum":        datum,
            "start":        start_str,
            "eind":         item.get("end", ""),
            "locatie":      item.get("location"),
            "url":          f"{BASE_URL}{item.get('url', '')}",
            "video_link":   video_link,
            "agendapunten": agendapunten,
            "bijgewerkt":   datetime.now().strftime("%d-%m-%Y"),
        })

        time.sleep(0.4)   # beleefd wachten

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(vergaderingen, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {len(vergaderingen)} raadsvergaderingen")
    met_video  = sum(1 for v in vergaderingen if v["video_link"])
    met_punten = sum(1 for v in vergaderingen if v["agendapunten"])
    print(f"  {met_video} met video-link")
    print(f"  {met_punten} met agendapunten")


if __name__ == "__main__":
    main()
