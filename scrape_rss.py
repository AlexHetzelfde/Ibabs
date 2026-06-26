#!/usr/bin/env python3
"""
Haalt alleen relevante bekendmakingen op van officielebekendmakingen.nl:
  - Cameratoezicht
  - Woningsluiting
  - Handhaving / dwangsommen

Elk item krijgt een 'adres'-veld (straat + huisnummer) zodat later
per wijk kan worden gegroepeerd.

Gebruik:
    python3 scrape_rss.py
"""

import json
import re
import os
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# RSS-feed: Zaanstad Gemeenteblad — meerdere URL-varianten als fallback
RSS_URLS = [
    (
        "https://zoek.officielebekendmakingen.nl/rss"
        "?q=(c.product-area==%22officielepublicaties%22)"
        "and((((w.organisatietype==%22gemeente%22)"
        "and((dt.creator==%22Zaanstad%22)"
        "or(dt.creator==%22gemeente%20Zaanstad%22)))))"
        "and(((w.publicatienaam==%22Gemeenteblad%22))"
        "or((w.publicatienaam==%22Staatscourant%22))"
        "or((w.publicatienaam==%22Provinciaal%20blad%22)))"
        "&rows=200"
    ),
]
OUTPUT = "data/bekendmakingen.json"

# Alleen deze categorieën worden bewaard.
CATEGORIE_TREFWOORDEN = {
    "cameratoezicht": [
        "cameratoezicht", "bewakingscamera", "camerasysteem", "cameragebied",
    ],
    "woningsluiting": [
        "woningsluiting", "pand gesloten", "sluiting woning",
        "drugspand", "artikel 13b", "bestuurlijke sluiting",
    ],
    "dwangsom": [
        "dwangsom", "last onder dwangsom", "bestuursdwang", "sanctiebesluit",
        "handhaving",
    ],
}

# Regex om straat + huisnummer uit een titel of omschrijving te vissen.
ADRES_REGEX = re.compile(
    r"([A-Z][a-z]+(?:straat|weg|laan|singel|kade|gracht|plein|dijk|pad|baan|steeg|hof|plantsoen|werf|oord|meen|donk|akker|brink|erf|hofje|park|zoom)\s+\d+[a-zA-Z]?)"
)


def categoriseer(titel):
    """Geeft categorie terug als titel een trefwoord bevat, anders None."""
    t = titel.lower()
    for cat, woorden in CATEGORIE_TREFWOORDEN.items():
        for w in woorden:
            if w in t:
                return cat
    return None


def parse_datum(s):
    if not s:
        return None
    formaten = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ]
    for fmt in formaten:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def extract_adres(titel, omschrijving=""):
    """Haalt straat + huisnummer uit titel of omschrijving."""
    m = ADRES_REGEX.search(titel)
    if m:
        return m.group(1)
    if omschrijving:
        m = ADRES_REGEX.search(omschrijving)
        if m:
            return m.group(1)
    return None


def fetch_feed():
    """
    Probeert meerdere RSS-URL-varianten. Bij elke variant 2 pogingen.
    Geeft de data terug van de eerste URL die werkt én items bevat.
    """
    import xml.etree.ElementTree as ET2
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":     "application/rss+xml, application/xml, text/xml, */*",
    }
    for vi, url in enumerate(RSS_URLS, 1):
        for poging in range(1, 3):
            print(f"URL-variant {vi}, poging {poging}...", end=" ", flush=True)
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                # Check of er items in zitten
                root = ET2.fromstring(data)
                items = list(root.iter("item"))
                print(f"OK — {len(items)} items ({len(data)} bytes)")
                if items:
                    return data
                else:
                    print(f"  (lege feed, volgende variant proberen)")
                    break
            except Exception as e:
                print(f"FOUT: {e}")
                if poging < 2:
                    import time; time.sleep(4)
    raise RuntimeError("Alle RSS-URL-varianten mislukt of geven lege feed")


def parse_feed(data):
    root  = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        datum = parse_datum(item.findtext("pubDate") or "")
        desc  = (item.findtext("description") or "").strip()
        items.append((titel, link, datum, desc))
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            titel = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            datum = parse_datum(
                entry.findtext("{http://www.w3.org/2005/Atom}published") or
                entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            )
            desc = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
            items.append((titel, link, datum, desc))
    return items


def load_existing():
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    return {b["link"]: b for b in data}


def main():
    # Datumbereik: via env var SCRAPE_VANAF of standaard afgelopen 7 dagen
    vandaag     = datetime.now()
    vanaf_env   = os.environ.get("SCRAPE_VANAF", "").strip()
    grens_datum = vanaf_env if vanaf_env else (vandaag - timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"Alleen bekendmakingen vanaf: {grens_datum}")

    data      = fetch_feed()
    raw_items = parse_feed(data)
    print(f"{len(raw_items)} items in feed")

    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} bekendmakingen")

    nieuw = 0
    overgeslagen = 0

    for titel, link, datum, desc in raw_items:
        if (datum or "") < grens_datum:
            continue

        cat = categoriseer(titel)
        if cat is None:
            overgeslagen += 1
            continue

        omschrijving = re.sub(r"<[^>]+>", "", desc).strip()
        if len(omschrijving) > 300:
            omschrijving = omschrijving[:300] + "…"

        adres = extract_adres(titel, omschrijving)

        bestaand[link] = {
            "titel":        titel,
            "link":         link,
            "datum":        datum,
            "categorie":    cat,
            "omschrijving": omschrijving or None,
            "adres":        adres,
        }
        nieuw += 1

    resultaat = sorted(bestaand.values(), key=lambda x: x.get("datum") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} nieuwe bekendmakingen toegevoegd")
    print(f"  {overgeslagen} weggefilterd (niet relevant)")
    print(f"  {len(resultaat)} totaal in JSON")


if __name__ == "__main__":
    main()
