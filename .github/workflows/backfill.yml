#!/usr/bin/env python3
"""
EENMALIGE BACKFILL: haalt moties, vergaderingen en bekendmakingen op
vanaf 2025-01-01 en vult de bestaande JSON-bestanden aan.

Na gebruik mag dit script worden verwijderd.
"""

import json, re, time, os, urllib.request, urllib.parse, http.cookiejar, xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# ------------------------------------------------------------
# 1. MOTIES
# ------------------------------------------------------------
BASE_URL = "https://zaanstad.bestuurlijkeinformatie.nl"
MOTIES_PAGE_URL     = f"{BASE_URL}/Reports/Details/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
MOTIES_DATA_URL     = f"{BASE_URL}/Reports/GetReportData/4b5dcb7b-adc3-4253-bad3-7bfd16341021"
STEMMINGEN_PAGE_URL = f"{BASE_URL}/Reports/Details/8e7af291-79d7-457f-88ca-e3c780df6eb2"
STEMMINGEN_DATA_URL = f"{BASE_URL}/Reports/GetReportData/8e7af291-79d7-457f-88ca-e3c780df6eb2"

MOTIES_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
}

MOTIES_COLUMNS = [
    ("typeselectie", False), ("title", False), ("datummotie", True),
    ("raadsledenselectie", True), ("fractieselectie", True),
    ("medeondertekenaarsselectie", True), ("registrationdate", True),
]

STEMMINGEN_COLUMNS = [
    ("identity", False), ("datum", True), ("title", False), ("status", False), ("registrationdate", True),
]

def build_body(columns, order_col, order_name, start, draw):
    params = [("draw", str(draw))]
    for i, (name, has_pipe) in enumerate(columns):
        params += [
            (f"columns[{i}][data]", name),
            (f"columns[{i}][name]", name),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", "|" if has_pipe else ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    params += [
        ("order[0][column]", str(order_col)),
        ("order[0][dir]", "desc"),
        ("order[0][name]", order_name),
        ("start", str(start)),
        ("length", "100"),
        ("search[value]", ""),
        ("search[regex]", "false"),
    ]
    return urllib.parse.urlencode(params).encode("utf-8")

def fetch_stemming_detail(opener, item_id):
    url = f"{BASE_URL}/Reports/Item/{item_id}"
    req = urllib.request.Request(url, headers={**MOTIES_HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
    except:
        return {}
    result = {}
    m = re.search(r'vote-summary-bar-in-favour\s+w-(\d+)', html)
    if m:
        result["voor_pct"] = int(m.group(1))
        result["tegen_pct"] = 100 - int(m.group(1))
    m = re.search(r'vote-summary-legend-in-favour.*?<div class="text">\s*([^<]+)\s*</div>', html, re.DOTALL)
    if m:
        result["fracties_voor"] = m.group(1).strip()
    m = re.search(r'vote-summary-legend-against.*?<div class="text">\s*([^<]+)\s*</div>', html, re.DOTALL)
    if m:
        result["fracties_tegen"] = m.group(1).strip()
    return result

def parse_datum(s):
    if not s: return None
    try:
        d, m, y = s.strip().split("-")
        return f"{y}-{m}-{d}"
    except:
        return None

def normalize(s):
    return re.sub(r"\s+", " ", (s or "").lower().strip())

def fetch_stemmingen(opener):
    headers = {**MOTIES_HEADERS, "Referer": STEMMINGEN_PAGE_URL}
    try:
        opener.open(urllib.request.Request(STEMMINGEN_PAGE_URL, headers={"User-Agent": MOTIES_HEADERS["User-Agent"]}), timeout=15)
    except:
        pass
    req = urllib.request.Request(STEMMINGEN_DATA_URL, data=build_body(STEMMINGEN_COLUMNS, 0, "identity", 0, 1), headers=headers)
    with opener.open(req, timeout=30) as resp:
        first = json.loads(resp.read().decode("utf-8"))
    total = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    draw, start = 2, 100
    while start < total:
        req = urllib.request.Request(STEMMINGEN_DATA_URL, data=build_body(STEMMINGEN_COLUMNS, 0, "identity", start, draw), headers=headers)
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1; start += 100
        time.sleep(0.3)
    result = {}
    for row in all_rows:
        titel = normalize(row.get("title", ""))
        status = (row.get("status") or "").strip().lower()
        identity = (row.get("identity") or "").strip()
        if titel and status:
            result[titel] = {"status": status, "identity": identity}
    return result

def parse_motie(row):
    titel = row.get("title", "").strip()
    type_raw = row.get("typeselectie", "").strip()
    if not type_raw:
        type_raw = "Amendement" if ("26A" in titel or "Amendement" in titel) else "Motie"
    fracties_raw = row.get("fractieselectie", "") or ""
    fracties = [f.strip() for f in fracties_raw.split("\r\n") if f.strip()]
    mede_raw = row.get("medeondertekenaarsselectie", "") or ""
    return {
        "id": row.get("DT_RowId"),
        "titel": titel,
        "type": type_raw,
        "partij": fracties[0] if fracties else None,
        "fracties": fracties,
        "indiener": (row.get("raadsledenselectie") or "").strip() or None,
        "medeondertekenaars": [m.strip() for m in mede_raw.split("\r\n") if m.strip()],
        "datum": parse_datum(row.get("datummotie")),
        "agendapunt": (row.get("registrationdate") or "").strip(),
        "status": None,
        "voor_pct": None,
        "tegen_pct": None,
        "fracties_voor": None,
        "fracties_tegen": None,
    }

def backfill_moties():
    print("\n==== MOTIES BACKFILL ====")
    grens_datum = "2025-01-01"
    print(f"Periode: vanaf {grens_datum}")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(MOTIES_PAGE_URL, headers={"User-Agent": MOTIES_HEADERS["User-Agent"]}), timeout=15)
    except:
        pass

    moties_headers = {**MOTIES_HEADERS, "Referer": MOTIES_PAGE_URL}
    print("Moties ophalen...", end=" ", flush=True)
    req = urllib.request.Request(MOTIES_DATA_URL, data=build_body(MOTIES_COLUMNS, 6, "registrationdate", 0, 1), headers=moties_headers)
    with opener.open(req, timeout=30) as resp:
        first = json.loads(resp.read().decode("utf-8"))
    total = first.get("recordsTotal", 0)
    all_rows = list(first.get("data", []))
    print(f"OK — {total} totaal")
    draw, start = 2, 100
    while start < total:
        req = urllib.request.Request(MOTIES_DATA_URL, data=build_body(MOTIES_COLUMNS, 6, "registrationdate", start, draw), headers=moties_headers)
        with opener.open(req, timeout=30) as resp:
            page = json.loads(resp.read().decode("utf-8"))
        all_rows.extend(page.get("data", []))
        draw += 1; start += 100
        time.sleep(0.3)

    recent = [r for r in all_rows if (parse_datum(r.get("datummotie")) or "") >= grens_datum]
    print(f"{len(recent)} moties vanaf 2025")

    # Stemmingen
    print("Stemmingen ophalen...", end=" ", flush=True)
    try:
        stemmingen = fetch_stemmingen(opener)
        print(f"OK — {len(stemmingen)}")
    except Exception as e:
        print(f"MISLUKT ({e})")
        stemmingen = {}

    # Existing data
    moties_file = "data/moties.json"
    bestaand = {}
    if os.path.exists(moties_file):
        with open(moties_file, encoding="utf-8") as f:
            for m in json.load(f):
                bestaand[m["id"]] = m
    print(f"Bestaande moties: {len(bestaand)}")

    for row in recent:
        m = parse_motie(row)
        stemming = stemmingen.get(normalize(m["titel"]))
        if stemming:
            m["status"] = stemming["status"]
            detail = fetch_stemming_detail(opener, stemming["identity"])
            m.update(detail)
            time.sleep(0.3)
        bestaand[m["id"]] = m

    resultaat = sorted(bestaand.values(), key=lambda x: x.get("datum") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(moties_file, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)
    print(f"✓ {len(recent)} moties toegevoegd, totaal {len(resultaat)} in {moties_file}")

# ------------------------------------------------------------
# 2. VERGADERINGEN
# ------------------------------------------------------------
CALENDAR_URL = f"{BASE_URL}/Calendar"
VERG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": CALENDAR_URL,
}
RAAD_CLASS = "agendatype-100491844"

def fetch_agenda_range(opener, start_dt, end_dt):
    start_str = urllib.parse.quote(start_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    end_str = urllib.parse.quote(end_dt.strftime("%Y-%m-%dT00:00:00+02:00"))
    url = f"{BASE_URL}/Calendar/GetAgendasForCalendar?start={start_str}&end={end_str}"
    req = urllib.request.Request(url, headers=VERG_HEADERS)
    with opener.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_vergadering_details(opener, agenda_id):
    url = f"{BASE_URL}/Agenda/Index/{agenda_id}"
    req = urllib.request.Request(url, headers={**VERG_HEADERS, "Accept": "text/html"})
    try:
        with opener.open(req, timeout=20) as resp:
            html = resp.read().decode("utf-8")
    except:
        return [], None
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
    video_link = None
    m = re.search(r'data-video-id="([^"]+)"', html)
    if m:
        player_id = m.group(1).strip().replace("/", "_")
        video_link = f"https://player.companywebcast.com/player/?id={player_id}&display=126&customBtnColor=006a81"
    return punten, video_link

def backfill_vergaderingen():
    print("\n==== VERGADERINGEN BACKFILL ====")
    start_dt = datetime(2025, 1, 1)
    end_dt = datetime.now()
    print(f"Periode: {start_dt.strftime('%Y-%m-%d')} t/m {end_dt.strftime('%Y-%m-%d')}")

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(CALENDAR_URL, headers=VERG_HEADERS), timeout=15)
        print("Sessie OK")
    except Exception as e:
        print(f"Sessie MISLUKT ({e})")

    print("Vergaderingen ophalen...", end=" ", flush=True)
    try:
        items = fetch_agenda_range(opener, start_dt, end_dt)
        raad = [i for i in items if RAAD_CLASS in i.get("classNames", [])]
        print(f"{len(raad)} raadsvergaderingen")
    except Exception as e:
        print(f"FOUT: {e}")
        return

    verg_file = "data/vergaderingen.json"
    bestaand = {}
    if os.path.exists(verg_file):
        with open(verg_file, encoding="utf-8") as f:
            for v in json.load(f):
                bestaand[v["id"]] = v
    print(f"Bestaande vergaderingen: {len(bestaand)}")

    nieuw = 0
    for i, item in enumerate(raad):
        agenda_id = item["id"]
        titel = item.get("title", "").strip()
        start_str = item.get("start", "")
        datum = start_str[:10] if start_str else None
        print(f"  [{i+1}/{len(raad)}] {datum} {titel}", end=" ", flush=True)
        agendapunten, video_link = fetch_vergadering_details(opener, agenda_id)
        print(f"— {len(agendapunten)} punten{' · video ✓' if video_link else ''}")
        bestaand[agenda_id] = {
            "id": agenda_id,
            "titel": titel,
            "type": "Raadsvergadering",
            "datum": datum,
            "start": start_str,
            "eind": item.get("end", ""),
            "locatie": item.get("location"),
            "url": f"{BASE_URL}{item.get('url', '')}",
            "video_link": video_link,
            "agendapunten": agendapunten,
            "bijgewerkt": datetime.now().strftime("%d-%m-%Y"),
        }
        nieuw += 1
        time.sleep(0.4)

    resultaat = sorted(bestaand.values(), key=lambda x: x.get("start", ""), reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(verg_file, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)
    print(f"✓ {nieuw} vergaderingen toegevoegd, totaal {len(resultaat)} in {verg_file}")

# ------------------------------------------------------------
# 3. BEKENDMAKINGEN (RSS, beperkte historie)
# ------------------------------------------------------------
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

CATEGORIE_TREFWOORDEN = {
    "cameratoezicht": ["cameratoezicht", "bewakingscamera", "camerasysteem", "cameragebied"],
    "woningsluiting": ["woningsluiting", "pand gesloten", "sluiting woning", "drugspand", "artikel 13b", "bestuurlijke sluiting"],
    "dwangsom": ["dwangsom", "last onder dwangsom", "bestuursdwang", "sanctiebesluit", "handhaving"],
}

ADRES_REGEX = re.compile(
    r"([A-Z][a-z]+(?:straat|weg|laan|singel|kade|gracht|plein|dijk|pad|baan|steeg|hof|plantsoen|werf|kade|oord|meen|donk|akker|brink|erf|hofje|park|zoom)\s+\d+[a-zA-Z]?)"
)

def categoriseer(titel):
    t = titel.lower()
    for cat, woorden in CATEGORIE_TREFWOORDEN.items():
        for w in woorden:
            if w in t:
                return cat
    return None

def extract_adres(titel, omschrijving=""):
    m = ADRES_REGEX.search(titel)
    if m: return m.group(1)
    if omschrijving:
        m = ADRES_REGEX.search(omschrijving)
        if m: return m.group(1)
    return None

def backfill_bekendmakingen():
    print("\n==== BEKENDMAKINGEN BACKFILL (RSS) ====")
    grens_datum = "2025-01-01"
    print(f"Filter: alleen cameratoezicht, woningsluiting, dwangsom vanaf {grens_datum} (RSS kan beperkt zijn)")

    headers = {"User-Agent": "Zaanstad-Raad-Monitor/1.0", "Accept": "application/rss+xml, application/xml, text/xml"}
    try:
        req = urllib.request.Request(RSS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
    except Exception as e:
        print(f"Fout bij ophalen RSS: {e}")
        return

    root = ET.fromstring(data)
    items = []
    for item in root.iter("item"):
        titel = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        datum = parse_datum(item.findtext("pubDate") or "")
        desc = (item.findtext("description") or "").strip()
        items.append((titel, link, datum, desc))
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            titel = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href", "") if link_el is not None else ""
            datum = parse_datum(entry.findtext("{http://www.w3.org/2005/Atom}published") or
                                entry.findtext("{http://www.w3.org/2005/Atom}updated") or "")
            desc = (entry.findtext("{http://www.w3.org/2005/Atom}summary") or "").strip()
            items.append((titel, link, datum, desc))

    bk_file = "data/bekendmakingen.json"
    bestaand = {}
    if os.path.exists(bk_file):
        with open(bk_file, encoding="utf-8") as f:
            for b in json.load(f):
                bestaand[b["link"]] = b
    print(f"Bestaande bekendmakingen: {len(bestaand)}")

    nieuw, overgeslagen = 0, 0
    for titel, link, datum, desc in items:
        if not datum or datum < grens_datum:
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
            "titel": titel,
            "link": link,
            "datum": datum,
            "categorie": cat,
            "omschrijving": omschrijving or None,
            "adres": adres,
        }
        nieuw += 1

    resultaat = sorted(bestaand.values(), key=lambda x: x.get("datum") or "", reverse=True)
    os.makedirs("data", exist_ok=True)
    with open(bk_file, "w", encoding="utf-8") as f:
        json.dump(resultaat, f, ensure_ascii=False, indent=2)
    print(f"✓ {nieuw} nieuwe, {overgeslagen} weggefilterd, totaal {len(resultaat)} in {bk_file}")

# ------------------------------------------------------------
# HOOFDPROGRAMMA
# ------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 BACKFILL GESTART (data vanaf 2025-01-01)")
    backfill_moties()
    backfill_vergaderingen()
    backfill_bekendmakingen()
    print("\n✅ ALLE BACKFILLS VOLTOOID. Je kunt dit script nu verwijderen.")
