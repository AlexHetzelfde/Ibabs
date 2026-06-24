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
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime, timedelta

BASE_URL    = "https://zaanstad.bestuurlijkeinformatie.nl"
CALENDAR_URL = f"{BASE_URL}/Calendar"
OUTPUT      = "data/vergaderingen.json"

# Alleen raadsvergaderingen
RAAD_CLASS  = "agendatype-100491844"

# Jaar ophalen: vorig jaar + huidig jaar
JAAR_START  = 2025
JAAR_EIND   = 2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept":     "*/*",
    "Referer":    CALENDAR_URL,
}


def fetch_agenda_range(opener, start_dt, end_dt):
    """Haalt vergaderingen op voor een datumbereik."""
    start_str = urllib.parse.quote(start_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    end_str   = urllib.parse.quote(end_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    url = f"{BASE_URL}/Calendar/GetAgendasForCalendar?start={start_str}&end={end_str}"
    req = urllib.request.Request(url, headers=HEADERS)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_agendapunten(opener, agenda_id):
    """
    Haalt agendapunten op voor één vergadering.
    Scrapet de HTML van /Agenda/Index/{id} want er is geen JSON-endpoint voor.
    """
    url = f"{BASE_URL}/Agenda/Index/{agenda_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
    except Exception:
        return []

    # Agendapunten staan als <li> items met een nummer en titel
    punten = []
    # Zoek naar patronen als "1.1 Titel van agendapunt"
    matches = re.findall(
        r'<span[^>]*class="[^"]*agenda-item-number[^"]*"[^>]*>([^<]+)</span>'
        r'.*?<span[^>]*class="[^"]*agenda-item-title[^"]*"[^>]*>([^<]+)</span>',
        html, re.DOTALL
    )
    for nummer, titel in matches:
        punten.append({
            "nummer": nummer.strip(),
            "titel":  titel.strip(),
        })

    # Fallback: zoek op li-items met nummering
    if not punten:
        matches = re.findall(
            r'<li[^>]*>.*?(\d+[\.\d]*)\s+([A-Z][^<]{5,80})</li>',
            html, re.DOTALL
        )
        for nummer, titel in matches[:20]:
            punten.append({"nummer": nummer.strip(), "titel": titel.strip()})

    return punten


def fetch_video_link(opener, agenda_id):
    """Zoekt naar een video/stream-link op de vergaderingspagina."""
    url = f"{BASE_URL}/Agenda/Index/{agenda_id}"
    req = urllib.request.Request(url, headers={**HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=15) as resp:
            html = resp.read().decode("utf-8")
    except Exception:
        return None

    # CompanyWebcast / iBabs video links
    patterns = [
        r'(https?://[^\s"\']+companywebcast[^\s"\']+)',
        r'(https?://[^\s"\']+ibabs[^\s"\']*stream[^\s"\']+)',
        r'(https?://[^\s"\']+video[^\s"\']+\.m3u8)',
        r'href="(https?://[^\s"\']+(?:webcast|stream|video)[^\s"\']*)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def main():
    # Sessie ophalen
    print("Sessie ophalen...", end=" ", flush=True)
    jar = http.cookiejar.CookieJar()
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

    # Sorteren: nieuwste eerst
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

        agendapunten = fetch_agendapunten(opener, agenda_id)
        video_link   = fetch_video_link(opener, agenda_id)

        print(f"— {len(agendapunten)} punten {'· video ✓' if video_link else ''}")

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

        time.sleep(0.4)

    import os
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(vergaderingen, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {len(vergaderingen)} raadsvergaderingen")
    met_video = sum(1 for v in vergaderingen if v["video_link"])
    met_punten = sum(1 for v in vergaderingen if v["agendapunten"])
    print(f"  {met_video} met video-link")
    print(f"  {met_punten} met agendapunten")


if __name__ == "__main__":
    main()
