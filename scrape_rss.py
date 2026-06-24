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

RSS_URL = (
    "https://zoek.officielebekendmakingen.nl/rss"
    "?q=(c.product-area%3D%3D%22officielepublicaties%22)"
    "and((((w.organisatietype%3D%3D%22gemeente%22)"
    "and((dt.creator%3D%3D%22Zaanstad%22)"
    "or(dt.creator%3D%3D%22gemeente%20Zaanstad%22)))))"
    "and(((w.publicatienaam%3D%3D%22Tractatenblad%22))"
    "or((w.publicatienaam%3D%3D%22Staatsblad%22))"
    "or((w.publicatienaam%3D%3D%22Staatscourant%22))"
    "or((w.publicatienaam%3D%3D%22Gemeenteblad%22))"
    "or((w.publicatienaam%3D%3D%22Provinciaal%20blad%22))"
    "or((w.publicatienaam%3D%3D%22Waterschapsblad%22))"
    "or((w.publicatienaam%3D%3D%22Blad%20gemeenschappelijke%20regeling%22)))"
)

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

# Regex om straat + huisnummer (+ evt. postcode) uit een titel te vissen.
# Zoekt naar een typisch Nederlands adrespatroon:
#   straatnaam (begint met hoofdletter, bevat letters, kan 'straat', 'weg', etc. zijn)
#   gevolgd door één of meer cijfers en eventueel een letter (huisnummer)
#   optioneel gevolgd door postcode en plaats
ADRES_REGEX = re.compile(
    r"([A-Z][a-z]+(?:straat|weg|laan|singel|kade|gracht|plein|dijk|pad|baan|steeg|hof|plantsoen|werf|kade|oord|meen|donk|akker|brink|erf|hofje|park|zoom)\s+\d+[a-zA-Z]?)"
)

def categoriseer(titel):
    """Geeft categorie terug als titel een trefwoord bevat, anders None (weggooien)."""
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
    # Probeer eerst in de titel
    m = ADRES_REGEX.search(titel)
    if m:
        return m.group(1)
    # Anders in omschrijving (indien aanwezig)
    if omschrijving:
        m = ADRES_REGEX.search(omschrijving)
        if m:
            return m.group(1)
    return None

def fetch_feed():
    print("Feed ophalen...", end=" ", flush=True)
    headers = {
        "User-Agent": "Zaanstad-Raad-Monitor/1.0",
        "Accept":     "application/rss+xml, application/xml, text/xml",
    }
    req = urllib.request.Request(RSS_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    print(f"OK ({len(data)} bytes)")
    return data

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
    vandaag      = datetime.now()
    week_geleden = vandaag - timedelta(days=7)
    grens_datum  = week_geleden.strftime("%Y-%m-%d")

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
