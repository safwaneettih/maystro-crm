#!/usr/bin/env python3
"""
Pipeline quotidien de leads fulfillment
Meta Ad Library (Tunisie) -> score -> dédoublonnage (SQLite) -> enrichissement -> Twenty

Usage :
    python leads_daily.py run                 # exécution normale (à planifier chaque jour)
    python leads_daily.py run --mode full     # balayage complet (sans filtre de date)
    python leads_daily.py run --dry-run       # collecte + score, sans écrire ni pousser
    python leads_daily.py run --no-enrich     # sans visite des pages Facebook
    python leads_daily.py run --no-push       # collecte seulement, rien envoyé au CRM
    python leads_daily.py stats               # état de la base
    python leads_daily.py selftest            # test hors ligne du dédoublonnage
"""
import argparse
import json
import os
import re
import sqlite3
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from contextlib import closing

from twenty_import import TwentyClient, TwentyRequestError

BASE = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "country": "TN",
    "keywords": ["توصيل", "توصيل مجاني", "الدفع عند الاستلام", "livraison gratuite"],
    "lookback_days": 3,
    "max_ads_per_keyword": 600,
    "scroll_pause_seconds": 2.5,
    "min_score": 3,
    "enrich_per_run": 15,
    "enrich_delay_seconds": 6,
    "push_requires_enrichment": False,
    "headless": True,
    "browser_profile_dir": "browser_profile",
    "db_path": "leads.db",
    "output_dir": "out",
    "allow_page_keys": [],
    "exclude_page_keys": [],
    "competitor_name_regex": r"(delivery|livraison|express|colis|transport|cargo|voyage|travel|tours|rent ?a? ?car|location de voiture)",
    "competitor_text_regex": r"(شركة توصيل|société de livraison|service de livraison|خدمة التوصيل|livraison express)",
}

GENERIC_DOMAINS = {
    "fb.com", "fb.me", "facebook.com", "instagram.com", "play.google.com", "api.whatsapp.com",
    "wa.me", "youtube.com", "tiktok.com", "linktr.ee", "apps.apple.com",
}
TURNKEY = re.compile(r"converty|myshopify|youcan|tiktak-preview|\.shop$|\.store$|\.online$|\.xyz$|\.site$|\.live$|\.pro$", re.I)
COD_RE = re.compile(r"الدفع عند الاستلام|عند الاستلام|paiement à la livraison|cash on delivery", re.I)
FREE_RE = re.compile(r"توصيل مجاني|التوصيل مجاني|توصيل مجانا|livraison gratuite|free delivery", re.I)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?216[\s.\-]?)?([2459]\d[\s.\-]?\d{3}[\s.\-]?\d{3})(?!\d)")
LIB_ID_RE = re.compile(r"(?:ID dans la biblioth[èe]que|Library ID)\s*:?\s*(\d+)", re.I)
DOMAIN_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})(?:/\S*)?$", re.M)

FR_MONTHS = {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
             "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
             "décembre": 12, "decembre": 12}


# ----------------------------------------------------------------------------- utilitaires
def log(msg, cfg=None):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        out = Path((cfg or {}).get("output_dir", "out"))
        if not out.is_absolute():
            out = BASE / out
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "run.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw and not raw.startswith("#") and "=" in raw:
                k, v = raw.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = Path(os.environ.get("LEADS_CONFIG_PATH", BASE / "config.json"))
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    if os.environ.get("LEADS_CONFIG_JSON"):
        cfg.update(json.loads(os.environ["LEADS_CONFIG_JSON"]))
    if os.environ.get("LEADS_DATA_DIR"):
        data_dir = Path(os.environ["LEADS_DATA_DIR"])
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg.update(db_path=data_dir / "leads.db", browser_profile_dir=data_dir / "browser_profile",
                   output_dir=data_dir / "out", headless=True)
    return cfg


def abs_path(cfg, key):
    p = Path(cfg[key])
    return p if p.is_absolute() else BASE / p


def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 8:
        return "+216" + d
    if len(d) == 11 and d.startswith("216"):
        return "+" + d
    return ("+" + d) if d else ""


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d).split("/")[0]
    return "" if (not d or d in GENERIC_DOMAINS) else d


def page_key_from_url(url):
    """Clé stable d'une page Facebook : id numérique si présent, sinon identifiant texte en minuscules."""
    u = urllib.parse.urlparse(url)
    if u.path.strip("/") == "profile.php":
        q = urllib.parse.parse_qs(u.query)
        return (q.get("id") or [""])[0]
    segs = [s for s in u.path.split("/") if s]
    for s in reversed(segs):
        if s.isdigit() and len(s) >= 8:
            return s
    return (segs[0] if segs else "").lower()


def parse_count(s):
    s = (s or "").replace(" ", "").replace("\xa0", "").strip()
    m = re.match(r"([\d][\d\s.,]*)\s*([KkMm])?", s)
    if not m:
        return None
    num = re.sub(r"\s", "", m.group(1)).replace(",", ".")
    try:
        val = float(num)
    except ValueError:
        return None
    mult = {"k": 1_000, "m": 1_000_000}.get((m.group(2) or "").lower(), 1)
    return int(round(val * mult))


# ----------------------------------------------------------------------------- base de données
SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  page_key TEXT PRIMARY KEY, name TEXT, fb_url TEXT, phones TEXT, domain TEXT,
  ads INTEGER, cod INTEGER, free INTEGER, score INTEGER, status TEXT, reason TEXT,
  followers INTEGER, email TEXT, website TEXT, last_post TEXT,
  first_seen TEXT, last_seen TEXT, enriched_at TEXT
);
CREATE TABLE IF NOT EXISTS ids (
  kind TEXT, value TEXT, page_key TEXT, PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS pushes (
  page_key TEXT, adapter TEXT, ref TEXT, pushed_at TEXT, PRIMARY KEY (page_key, adapter)
);
CREATE TABLE IF NOT EXISTS twenty_note_sync (
  page_key TEXT PRIMARY KEY, enriched_at TEXT NOT NULL
);
"""


def open_db(cfg, dry_run=False):
    path = abs_path(cfg, "db_path")
    conn = sqlite3.connect(":memory:" if dry_run else str(path))
    if dry_run and path.is_file():
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as existing:
            existing.backup(conn)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ----------------------------------------------------------------------------- scoring
def score_page(p, cfg):
    """Retourne (statut, score, raison). Statut : new | low | excluded."""
    key = p["page_key"]
    if key in set(k.lower() for k in cfg["exclude_page_keys"]):
        return "excluded", 1, "Exclu manuellement"
    allowed = key in set(k.lower() for k in cfg["allow_page_keys"])
    if not allowed:
        if re.search(cfg["competitor_name_regex"], p["name"], re.I):
            return "excluded", 1, "Nom évoquant un prestataire logistique/voyage"
        if re.search(cfg["competitor_text_regex"], p["text"], re.I):
            return "excluded", 1, "Annonce d'un prestataire de livraison (concurrent)"
    domain = p["domain"]
    site_ok = bool(domain)
    ads = len(p["ad_ids"]) or 1
    pts = (1 if site_ok else 0) + (1 if (site_ok and TURNKEY.search(domain)) else 0) \
        + p["cod"] + p["free"] + (1 if ads >= 2 else 0) + (1 if ads >= 5 else 0)
    score = 5 if pts >= 4 else (4 if pts == 3 else (3 if pts == 2 else (2 if pts == 1 else 1)))
    reasons = []
    if site_ok:
        reasons.append("boutique en ligne" + (" clé en main" if TURNKEY.search(domain) else ""))
    if p["cod"]:
        reasons.append("paiement à la livraison annoncé")
    if p["free"]:
        reasons.append("livraison gratuite annoncée")
    if ads >= 2:
        reasons.append(f"{ads} annonces actives")
    reason = ("Signaux : " + ", ".join(reasons) + ".") if reasons else "Peu de signaux e-commerce."
    return ("new" if score >= cfg["min_score"] else "low"), score, reason


# ----------------------------------------------------------------------------- agrégation des cartes
def aggregate_cards(cards):
    """cards : [{name, url, text}] -> dict page_key -> page agrégée."""
    pages = {}
    for c in cards:
        key = page_key_from_url(c["url"])
        if not key:
            continue
        text = c["text"]
        ids = LIB_ID_RE.findall(text)
        clean = re.sub(r"(?:ID dans la biblioth|Library ID)[^\n]*", "", text, flags=re.I)
        clean = re.sub(r"(?:Début de diffusion|Started running)[^\n]*", "", clean, flags=re.I)
        p = pages.setdefault(key, {
            "page_key": key, "name": c["name"], "fb_url": c["url"].split("?")[0],
            "ad_ids": set(), "phones": set(), "domain": "", "cod": 0, "free": 0, "text": "",
        })
        if ids:
            p["ad_ids"].add(ids[0])
        for m in PHONE_RE.finditer(clean):
            p["phones"].add(norm_phone(m.group(1)))
        if not p["domain"]:
            for m in DOMAIN_LINE_RE.finditer(clean):
                d = norm_domain(m.group(1))
                if d and "." in d and not re.match(r"^\d", d):
                    p["domain"] = d
                    break
        if COD_RE.search(clean):
            p["cod"] = 1
        if FREE_RE.search(clean):
            p["free"] = 1
        if len(p["text"]) < 1500:
            p["text"] += "\n" + clean[:500]
    return pages


# ----------------------------------------------------------------------------- dédoublonnage
def find_existing(conn, identifiers):
    for kind, value in identifiers:
        if not value:
            continue
        row = conn.execute("SELECT page_key FROM ids WHERE kind=? AND value=?", (kind, value)).fetchone()
        if row:
            return row["page_key"]
    return None


def ingest(pages, conn, cfg, dry_run=False):
    """Insère les nouvelles pages, fusionne les doublons (même page, même téléphone ou même domaine)."""
    now = datetime.now().isoformat(timespec="seconds")
    stats = {"seen": len(pages), "new": 0, "duplicates": 0, "excluded": 0, "low": 0}
    for key, p in pages.items():
        identifiers = [("page", key)] + [("phone", ph) for ph in sorted(p["phones"])]
        if p["domain"]:
            identifiers.append(("domain", p["domain"]))
        existing = find_existing(conn, identifiers)
        if existing:
            stats["duplicates"] += 1
            if not dry_run:
                row = conn.execute("SELECT * FROM leads WHERE page_key=?", (existing,)).fetchone()
                phones = sorted(set(filter(None, (row["phones"] or "").split(";"))) | p["phones"])
                status, score, reason = score_page(p, cfg)
                new_status = row["status"]
                if row["status"] == "low" and status == "new":
                    new_status = "new"
                conn.execute(
                    "UPDATE leads SET last_seen=?, phones=?, domain=COALESCE(NULLIF(domain,''),?), "
                    "ads=MAX(ads,?), score=MAX(score,?), status=? WHERE page_key=?",
                    (now, ";".join(phones), p["domain"], len(p["ad_ids"]) or 1,
                     score if row["status"] != "excluded" else row["score"], new_status, existing))
                for kind, value in identifiers:
                    if value:
                        conn.execute("INSERT OR IGNORE INTO ids VALUES (?,?,?)", (kind, value, existing))
            continue
        status, score, reason = score_page(p, cfg)
        stats["new" if status == "new" else status] += 1
        if dry_run:
            continue
        conn.execute(
            "INSERT INTO leads (page_key,name,fb_url,phones,domain,ads,cod,free,score,status,reason,"
            "first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, p["name"], p["fb_url"], ";".join(sorted(p["phones"])), p["domain"],
             len(p["ad_ids"]) or 1, p["cod"], p["free"], score, status, reason, now, now))
        for kind, value in identifiers:
            if value:
                conn.execute("INSERT OR IGNORE INTO ids VALUES (?,?,?)", (kind, value, key))
    if not dry_run:
        conn.commit()
    return stats


# ----------------------------------------------------------------------------- collecte (Playwright)
EXTRACT_JS = """
() => {
  const out = [];
  document.querySelectorAll('a[href*="facebook.com/"]').forEach(a => {
    const h = a.href;
    if (/\\/ads\\/library|l\\.facebook|\\/help|\\/policies|\\/business|\\/privacy|\\/legal/.test(h)) return;
    const name = (a.innerText || '').trim();
    if (!name || name.length > 80) return;
    let el = a, txt = '';
    for (let i = 0; i < 14 && el; i++) {
      el = el.parentElement;
      if (el && el.innerText && /ID dans la biblioth|Library ID/.test(el.innerText)) {
        txt = el.innerText;
        if (txt.length < 6000) break;
      }
    }
    if (!txt) return;
    out.push({name: name, url: h, text: txt.slice(0, 6000)});
  });
  return out;
}
"""

COUNT_ADS_JS = "() => (document.body.innerText.match(/ID dans la biblioth|Library ID/g) || []).length"


def build_url(keyword, cfg, mode):
    params = [
        ("active_status", "active"), ("ad_type", "all"), ("country", cfg["country"]),
        ("is_targeted_country", "false"), ("media_type", "all"),
        ("q", f'"{keyword}"'), ("search_type", "keyword_exact_phrase"),
        ("sort_data[direction]", "desc"), ("sort_data[mode]", "total_impressions"),
    ]
    if mode == "new":
        start = date.today() - timedelta(days=cfg["lookback_days"])
        params += [("start_date[min]", start.isoformat()), ("start_date[max]", date.today().isoformat())]
    return "https://www.facebook.com/ads/library/?" + urllib.parse.urlencode(params)


def dismiss_cookie_banner(page):
    for label in (r"Refuser les cookies facultatifs", r"Decline optional cookies", r"Refuser"):
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if btn.is_visible(timeout=1500):
                btn.click()
                time.sleep(1)
                return
        except Exception:
            continue


def scrape(cfg, mode):
    from playwright.sync_api import sync_playwright  # import tardif : inutile pour selftest/stats

    all_cards = []
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(abs_path(cfg, "browser_profile_dir")), headless=cfg["headless"], locale="fr-FR",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for kw in cfg["keywords"]:
            url = build_url(kw, cfg, mode)
            log(f"Mot-clé « {kw} » ({mode})", cfg)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
            dismiss_cookie_banner(page)
            last_height, stall, collected = 0, 0, []
            for i in range(200):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(cfg["scroll_pause_seconds"])
                height = page.evaluate("document.body.scrollHeight")
                n_ads = page.evaluate(COUNT_ADS_JS)
                stall = stall + 1 if height == last_height else 0
                last_height = height
                if n_ads >= cfg["max_ads_per_keyword"] or stall >= 4:
                    break
            collected = page.evaluate(EXTRACT_JS)
            log(f"  {len(collected)} cartes lues, {page.evaluate(COUNT_ADS_JS)} annonces chargées", cfg)
            all_cards.extend(collected)
        ctx.close()
    return all_cards


# ----------------------------------------------------------------------------- enrichissement des pages
def parse_page_text(text):
    out = {"followers": None, "email": "", "website": "", "phone": "", "last_post": ""}
    m = re.search(r"([\d][\d\s  .,]*\s?[KkMm]?)\s*(?:followers|abonnés)", text, re.I)
    if m:
        out["followers"] = parse_count(m.group(1))
    for e in re.findall(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+", text):
        if not re.search(r"facebook|fbcdn|sentry|example", e, re.I):
            out["email"] = e
            break
    intro = text[text.find("Intro"):text.find("Intro") + 1500] if "Intro" in text else text[:1500]
    for line in intro.splitlines():
        line = line.strip()
        if not out["phone"] and re.fullmatch(r"(?:\+216\s?)?[2459]\d(?:\s?\d{3}){2}", line):
            out["phone"] = norm_phone(line)
        if not out["website"] and re.fullmatch(r"(?:https?://)?[\w\-]+(?:\.[\w\-]+)+(?:/\S*)?", line) \
                and not re.search(r"facebook|instagram|tiktok|youtube|@", line, re.I) and "." in line \
                and not line.startswith("+"):
            out["website"] = line
    foot = text.find("Cookies")
    tail = text[foot:] if foot >= 0 else text
    d = re.search(r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+(\d{1,2})\s+([a-zéûè]+)\s+(\d{4})", tail, re.I)
    if d and d.group(2).lower() in FR_MONTHS:
        out["last_post"] = date(int(d.group(3)), FR_MONTHS[d.group(2).lower()], int(d.group(1))).isoformat()
    return out


def enrich_pending(conn, cfg):
    rows = conn.execute(
        "SELECT page_key, fb_url FROM leads WHERE status IN ('new','pushed') AND enriched_at IS NULL "
        "ORDER BY score DESC, ads DESC LIMIT ?", (cfg["enrich_per_run"],)).fetchall()
    if not rows:
        return 0
    from playwright.sync_api import sync_playwright

    done = 0
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(abs_path(cfg, "browser_profile_dir")), headless=cfg["headless"], locale="fr-FR",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for r in rows:
            try:
                page.goto(r["fb_url"], wait_until="domcontentloaded", timeout=45000)
                time.sleep(4)
                page.evaluate("window.scrollTo(0, 1500)")
                time.sleep(2)
                page.evaluate("window.scrollTo(0, 2500)")
                time.sleep(2.5)
                info = parse_page_text(page.inner_text("body"))
                row = conn.execute("SELECT phones FROM leads WHERE page_key=?", (r["page_key"],)).fetchone()
                phones = set(filter(None, (row["phones"] or "").split(";")))
                if info["phone"]:
                    phones.add(info["phone"])
                conn.execute(
                    "UPDATE leads SET followers=?, email=?, website=?, last_post=?, phones=?, enriched_at=? "
                    "WHERE page_key=?",
                    (info["followers"], info["email"], info["website"], info["last_post"],
                     ";".join(sorted(phones)), datetime.now().isoformat(timespec="seconds"), r["page_key"]))
                for ph in phones:
                    conn.execute("INSERT OR IGNORE INTO ids VALUES ('phone',?,?)", (ph, r["page_key"]))
                conn.commit()
                done += 1
            except Exception as e:
                log(f"  enrichissement impossible pour {r['fb_url']} : {e}", cfg)
            time.sleep(cfg["enrich_delay_seconds"])
        ctx.close()
    return done


# ----------------------------------------------------------------------------- adaptateurs CRM
def lead_dict(row):
    phones = [p for p in (row["phones"] or "").split(";") if p]
    site = row["website"] or row["domain"] or ""
    if site and not site.startswith("http"):
        site = "https://" + site
    return {
        "page_key": row["page_key"], "name": row["name"], "phone1": phones[0] if phones else "",
        "phone2": " / ".join(phones[1:]), "email": row["email"] or "", "website": site,
        "facebook": row["fb_url"], "followers": row["followers"], "last_post": row["last_post"] or "",
        "ads": row["ads"], "cod": bool(row["cod"]), "free_delivery": bool(row["free"]),
        "score": row["score"], "reason": row["reason"], "first_seen": row["first_seen"],
        "channel": "Appel / WhatsApp" if phones else "Messenger",
    }


def push_pending(conn, cfg, client):
    rows = conn.execute(
        "SELECT l.* FROM leads l WHERE l.status IN ('new','pushed') AND l.score >= ? "
        "AND (? = 0 OR l.enriched_at IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM pushes p WHERE p.page_key = l.page_key AND p.adapter = 'twenty') "
        "ORDER BY l.score DESC, l.ads DESC", (cfg["min_score"], cfg["push_requires_enrichment"]),
    ).fetchall()
    failed = False
    for row in rows:
        key = row["page_key"]
        try:
            opportunity_id = client.import_lead(lead_dict(row), cfg["country"])
        except TwentyRequestError as error:
            log(f"[twenty] {key}: {error}. Lead conservé pour un nouvel essai.", cfg)
            failed = True
            if error.status in (None, 401, 403, 429, 502, 503, 504):
                break
            continue
        with conn:
            conn.execute("INSERT OR IGNORE INTO pushes VALUES (?, 'twenty', ?, ?)",
                         (key, opportunity_id, datetime.now().isoformat(timespec="seconds")))
            conn.execute("UPDATE leads SET status='pushed' WHERE page_key=?", (key,))
        log(f"[twenty] {key}: opportunité {opportunity_id}", cfg)
    return int(failed)


def mark_note_synced(conn, row):
    conn.execute("INSERT OR REPLACE INTO twenty_note_sync VALUES (?, ?)",
                 (row["page_key"], row["enriched_at"]))


def sync_enriched_notes(conn, cfg, client):
    rows = conn.execute(
        "SELECT l.* FROM leads l JOIN pushes p ON p.page_key=l.page_key AND p.adapter='twenty' "
        "LEFT JOIN twenty_note_sync s ON s.page_key=l.page_key "
        "WHERE l.enriched_at IS NOT NULL AND s.enriched_at IS NOT l.enriched_at "
        "ORDER BY l.enriched_at, l.page_key"
    ).fetchall()
    failed = False
    for row in rows:
        try:
            client.sync_note(lead_dict(row))
        except TwentyRequestError as error:
            log(f"[twenty] {row['page_key']}: {error}. Note conservée pour un nouvel essai.", cfg)
            failed = True
            if error.status in (None, 401, 403, 429, 502, 503, 504):
                break
            continue
        with conn:
            mark_note_synced(conn, row)
    return int(failed)


def cmd_run(args):
    load_env()
    cfg = load_config()
    client = TwentyClient.from_environment() if not (args.dry_run or args.no_push) else None
    if args.keywords:
        cfg["keywords"] = args.keywords
    log(f"Démarrage ({args.mode}) – mots-clés : {', '.join(cfg['keywords'])}", cfg)
    cards = scrape(cfg, args.mode)
    pages = aggregate_cards(cards)
    with closing(open_db(cfg, dry_run=args.dry_run)) as conn:
        stats = ingest(pages, conn, cfg, dry_run=args.dry_run)
        log(f"Pages lues : {stats['seen']} | nouvelles qualifiées : {stats['new']} | doublons ignorés : "
            f"{stats['duplicates']} | faible potentiel : {stats['low']} | exclues : {stats['excluded']}", cfg)
        if args.dry_run:
            return
        if not args.no_enrich and cfg["enrich_per_run"] > 0:
            log(f"Enrichissement de {cfg['enrich_per_run']} pages maximum…", cfg)
            log(f"  {enrich_pending(conn, cfg)} page(s) enrichie(s)", cfg)
        if client:
            return sync_enriched_notes(conn, cfg, client) | push_pending(conn, cfg, client)


def cmd_push(_):
    load_env()
    cfg = load_config()
    client = TwentyClient.from_environment()
    with closing(open_db(cfg)) as conn:
        return sync_enriched_notes(conn, cfg, client) | push_pending(conn, cfg, client)


def cmd_stats(_):
    cfg = load_config()
    conn = open_db(cfg)
    for r in conn.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status"):
        print(f"{r['status']:10s} {r['n']}")
    print("enrichis :", conn.execute("SELECT COUNT(*) FROM leads WHERE enriched_at IS NOT NULL").fetchone()[0])
    for r in conn.execute("SELECT adapter, COUNT(*) n FROM pushes GROUP BY adapter"):
        print(f"envoyés vers {r['adapter']} : {r['n']}")


def cmd_selftest(_):
    import tempfile
    cfg = dict(DEFAULT_CONFIG)
    tmp = tempfile.mkdtemp()
    cfg["db_path"], cfg["output_dir"] = os.path.join(tmp, "t.db"), os.path.join(tmp, "out")
    conn = open_db(cfg)

    def card(name, url, lib, body, domain=""):
        txt = f"Actif\nID dans la bibliothèque : {lib}\nDébut de diffusion le 2 nov 2026\nSponsorisé\n{name}\n{body}\n{domain.upper()}"
        return {"name": name, "url": url, "text": txt}

    day1 = [
        card("Shop A", "https://www.facebook.com/shopa/", "1111111111111111",
             "توصيل مجاني والدفع عند الاستلام 22 333 444", "shop-a.converty.shop"),
        card("Shop A", "https://www.facebook.com/shopa/", "1111111111111112", "عرض آخر", "shop-a.converty.shop"),
        card("Rapide Delivery", "https://www.facebook.com/rapidedelivery/", "2222222222222222", "توصيل سريع"),
        card("Shop B", "https://www.facebook.com/people/Shop-B/61500000000001/", "3333333333333333",
             "livraison gratuite", "shop-b.tn"),
    ]
    s1 = ingest(aggregate_cards(day1), conn, cfg)
    assert s1["new"] == 2 and s1["excluded"] == 1, s1
    # jour 2 : mêmes pages + même boutique sous un autre identifiant (même domaine) + même téléphone
    day2 = day1 + [
        card("Shop A (2e page)", "https://www.facebook.com/shopa.officiel/", "4444444444444444", "autre", "shop-a.converty.shop"),
        card("Shop C", "https://www.facebook.com/shopc/", "5555555555555555", "appelez 22 333 444", "shop-c.shop"),
        card("Shop D", "https://www.facebook.com/shopd/", "6666666666666666",
             "paiement à la livraison", "shop-d.converty.shop"),
    ]
    s2 = ingest(aggregate_cards(day2), conn, cfg)
    assert s2["new"] == 1, s2  # seul Shop D est nouveau ; Shop A (2e page) et Shop C = doublons
    assert s2["duplicates"] >= 4, s2
    day3 = ingest(aggregate_cards(day2), conn, cfg)
    assert day3["new"] == 0, day3
    info = parse_page_text("Se connecter\nBizerta Deco\n8,8 K followers • 1 suivi(e)s\nIntro\nBizerte, Tunisia\n"
                           "55 500 515\ncontact@bizertadeco.tn\nbizertadeco.tn\nCookies\nMardi 3 novembre 2026 à 22:22")
    assert info["followers"] == 8800 and info["email"] == "contact@bizertadeco.tn" \
        and info["phone"] == "+21655500515" and info["last_post"] == "2026-11-03", info
    print("selftest OK :", s1, s2, day3, info)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["new", "full"], default="new")
    r.add_argument("--keywords", nargs="+")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-enrich", action="store_true")
    r.add_argument("--no-push", action="store_true")
    r.set_defaults(func=cmd_run)
    sub.add_parser("push").set_defaults(func=cmd_push)
    sub.add_parser("stats").set_defaults(func=cmd_stats)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
