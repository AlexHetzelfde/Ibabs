#!/usr/bin/env python3
"""
Haalt bekendmakingen op van afgelopen 7 dagen uit officielebekendmakingen.nl
en voegt ze toe aan data/bekendmakingen.json

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

CATEGORIE_REGELS = {
    "woningbouw": [
        "woningbouw", "woningen", "appartementen", "woongebouw",
        "wooncomplex", "sociale huur", "huurwoningen", "nieuwbouw",
        "transformatie", "woonbestemming",
    ],
    "omgevingsplan": [
        "omgevingsplan", "bestemmingsplan", "omgevingsvergunning",
        "ruimtelijk", "bouwplan", "bouwvergunning", "wijziging bestemmings",
    ],
    "verkeer": [
        "verkeersbesluit", "verkeersmaatregelen", "snelheidsbegrenzing",
        "parkeerverbod", "parkeervergunning", "wegafsluiting",
        "omleidingsroute", "rijbaan",
    ],
    "sloop": [
        "sloopvergunning", "sloopmelding", "sloop",
    ],
}

FILTER_WEG = [
    "dakkapel", "erfafscheiding", "schutting", "zonnepanelen",
    "airconditioning", "kozijn", "gevelwijziging", "tuinmuur",
    "berging", "fietsenstalling",
]


def categoriseer(titel):
    t = titel.lower()
    for woord in FILTER_WEG:
        if woord in t:
            return None
    for cat, trefwoorden in CATEGORIE_REGELS.items():
        for tw in trefwoorden:
            if tw in t:
                return cat
    return "overig"


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
            link  = link_el.get("href", "") if link_el is not None else ""
            datum = parse_datum(
                entry.findtext("{http://www.w3.org/2005/Atom}published") or
                entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            )
            desc = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
            items.append((titel, link, datum, desc))
    return items


def load_existing():
    """Laad bestaande bekendmakingen.json als die bestaat."""
    if not os.path.exists(OUTPUT):
        return {}
    with open(OUTPUT, encoding="utf-8") as f:
        data = json.load(f)
    # Dedup op link-URL
    return {b["link"]: b for b in data}


def main():
    import os
    vandaag   = datetime.now()
    vanaf_env = os.environ.get("SCRAPE_VANAF", "").strip()
    grens_datum = vanaf_env if vanaf_env else (vandaag - timedelta(days=7)).strftime("%Y-%m-%d")

    print(f"Alleen bekendmakingen vanaf: {grens_datum}")

    data      = fetch_feed()
    raw_items = parse_feed(data)
    print(f"{len(raw_items)} items in feed")

    # Bestaande data inladen
    bestaand = load_existing()
    print(f"Bestaande JSON: {len(bestaand)} bekendmakingen")

    nieuw = 0
    overgeslagen = 0

    for titel, link, datum, desc in raw_items:
        # Alleen afgelopen week
        if (datum or "") < grens_datum:
            continue

        cat = categoriseer(titel)
        if cat is None:
            overgeslagen += 1
            continue

        omschrijving = re.sub(r"<[^>]+>", "", desc).strip()
        if len(omschrijving) > 300:
            omschrijving = omschrijving[:300] + "…"

        bestaand[link] = {
            "titel":        titel,
            "link":         link,
            "datum":        datum,
            "categorie":    cat,
            "omschrijving": omschrijving or None,
        }
        nieuw += 1

    # Opslaan: nieuwste eerst
    resultaat = sorted(bestaand.values(), key=lambda x: x.get("datum") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {nieuw} nieuwe bekendmakingen toegevoegd")
    print(f"  {overgeslagen} weggefilterd (dakkapellen e.d.)")
    print(f"  {len(resultaat)} totaal in JSON")


if __name__ == "__main__":
    main()
