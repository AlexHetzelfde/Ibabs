#!/usr/bin/env python3
"""
Haalt bekendmakingen op uit officielebekendmakingen.nl (Atom-feed)
en schrijft ze weg als data/bekendmakingen.json

Filtert automatisch op relevante categorieën voor Zaanstad-journalistiek.

Gebruik:
    python3 scrape_rss.py
"""

import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
import os

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

# Trefwoorden per categorie — titel wordt getoetst (lowercase)
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

# Items die we altijd overslaan (te veel ruis)
FILTER_WEG = [
    "dakkapel", "erfafscheiding", "schutting", "zonnepanelen",
    "airconditioning", "kozijn", "gevelwijziging", "tuinmuur",
    "berging", "fietsenstalling",
]


def categoriseer(titel: str) -> str | None:
    """
    Geeft categorie terug als de titel relevant is, anders None (overslaan).
    """
    t = titel.lower()

    # Wegfilteren: te klein, geen journalistieke waarde
    for woord in FILTER_WEG:
        if woord in t:
            return None

    for cat, trefwoorden in CATEGORIE_REGELS.items():
        for tw in trefwoorden:
            if tw in t:
                return cat

    return "overig"


def parse_datum(s: str) -> str | None:
    """Probeert diverse datumformaten te parsen naar YYYY-MM-DD."""
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


def fetch_feed() -> list:
    print(f"Feed ophalen...", end=" ", flush=True)
    headers = {
        "User-Agent": "Zaanstad-Raad-Monitor/1.0 (journalistiek dashboard)",
        "Accept":     "application/rss+xml, application/xml, text/xml",
    }
    req = urllib.request.Request(RSS_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    print(f"OK ({len(data)} bytes)")
    return data


def parse_feed(data: bytes) -> list:
    # RSS/Atom heeft soms een namespace-prefix
    root = ET.fromstring(data)

    # Detecteer Atom vs RSS
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = []

    # Probeer RSS (<item>)
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link  = (item.findtext("link")  or "").strip()
        datum = parse_datum(item.findtext("pubDate") or "")
        desc  = (item.findtext("description") or "").strip()
        items.append((titel, link, datum, desc))

    # Probeer Atom (<entry>) als RSS niets gaf
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            titel = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link  = link_el.get("href", "") if link_el is not None else ""
            datum = parse_datum(
                entry.findtext("{http://www.w3.org/2005/Atom}published") or
                entry.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            )
            desc  = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
            items.append((titel, link, datum, desc))

    return items


def main():
    data = fetch_feed()
    raw_items = parse_feed(data)
    print(f"{len(raw_items)} items in feed")

    resultaten = []
    overgeslagen = 0

    for titel, link, datum, desc in raw_items:
        cat = categoriseer(titel)
        if cat is None:
            overgeslagen += 1
            continue

        # Omschrijving: schoon van HTML-tags
        omschrijving = re.sub(r"<[^>]+>", "", desc).strip()
        if len(omschrijving) > 300:
            omschrijving = omschrijving[:300] + "…"

        resultaten.append({
            "titel":        titel,
            "link":         link,
            "datum":        datum,
            "categorie":    cat,
            "omschrijving": omschrijving or None,
        })

    # Sorteren: nieuwste eerst
    resultaten.sort(key=lambda x: x.get("datum") or "", reverse=True)

    os.makedirs("data", exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(resultaten, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Weggeschreven naar {OUTPUT}")
    print(f"  {len(resultaten)} relevante bekendmakingen")
    print(f"  {overgeslagen} items weggefilterd (dakkapellen e.d.)")

    # Overzicht per categorie
    for cat in ["woningbouw", "omgevingsplan", "verkeer", "sloop", "overig"]:
        n = sum(1 for b in resultaten if b["categorie"] == cat)
        if n:
            print(f"  {cat}: {n}")


if __name__ == "__main__":
    main()
