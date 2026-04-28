"""Collecte les annonces officielles One Piece TCG depuis les 3 sites :
- fr.onepiece-cardgame.com  (français natif)
- en.onepiece-cardgame.com  (couvre US/EU exclusifs comme Best Selection)
- www.onepiece-cardgame.com  (japonais — souvent en avance, traduit)

Filtre par catégorie (PRODUITS, CARTES, ANNONCES — exclut ÉVÉNEMENTS).
Déduplication par code de set + hash du titre normalisé.
Persistance dans news_history.json (rolling window 30 jours).
"""
from __future__ import annotations
import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ─────────────────── Constantes ───────────────────
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8,ja;q=0.7",
}

# Catégories à GARDER (en majuscules pour matcher les badges du site)
KEEP_CATEGORIES_FR = {"PRODUITS", "CARTES", "ANNONCES"}
KEEP_CATEGORIES_EN = {"PRODUCTS", "CARDS", "NEWS"}
KEEP_CATEGORIES_JP = {"商品", "カード", "ニュース"}  # produits, cartes, news

# Mots-clés à EXCLURE même dans les bonnes catégories (banlist surtout)
EXCLUDE_TITLE_KEYWORDS = [
    # FR
    "bannies", "bannie", "limitées", "limitée", "tournoi", "championship",
    "treasure cup", "store tournament", "campagne", "événement", "evenement",
    "tour", "festa", "regional", "nationals",
    # EN
    "banned", "restricted", "ban list", "banlist", "tournament", "store championship",
    "event", "fest", "regional", "world championship", "playoff",
    # JP
    "禁止", "制限", "大会", "イベント", "選手権", "ストアバトル", "公認",
]

# Sources
SOURCES = [
    {
        "lang": "FR",
        "label": "🇫🇷",
        "name": "Site officiel FR",
        "topics_url": "https://fr.onepiece-cardgame.com/topics/",
        "products_url": "https://fr.onepiece-cardgame.com/products/",
        "base": "https://fr.onepiece-cardgame.com",
    },
    {
        "lang": "EN",
        "label": "🇺🇸",
        "name": "Site officiel EN",
        "topics_url": "https://en.onepiece-cardgame.com/topics/",
        "products_url": "https://en.onepiece-cardgame.com/products/",
        "base": "https://en.onepiece-cardgame.com",
    },
    {
        "lang": "JP",
        "label": "🇯🇵",
        "name": "Site officiel JP",
        "topics_url": "https://www.onepiece-cardgame.com/topics/",
        "products_url": "https://www.onepiece-cardgame.com/products/",
        "base": "https://www.onepiece-cardgame.com",
    },
]

# Regex code de set (canonique)
SET_RE = re.compile(r"\b(OP|EB|PRB|ST|PRD|DP|TS|IB|DF|PRC)\s*[-_]?\s*(\d{1,2})\b", re.I)

HISTORY_FILE = Path("news_history.json")
ROLLING_WINDOW_DAYS = 30  # on garde 30 jours d'historique


# ─────────────────── Utils ───────────────────
def log(msg, indent=0):
    print("  " * indent + msg, flush=True)


def fetch_html(url: str, timeout: int = 15) -> Optional[str]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.encoding = r.apparent_encoding or "utf-8"
        if r.status_code == 200:
            return r.text
        log(f"⚠️  HTTP {r.status_code} pour {url}", indent=2)
    except Exception as ex:
        log(f"⚠️  fetch {url}: {ex}", indent=2)
    return None


def detect_set_code(title: str) -> Optional[str]:
    if not title:
        return None
    m = SET_RE.search(title)
    if m:
        return f"{m.group(1).upper()}-{m.group(2).zfill(2)}"
    return None


def normalize_title(title: str) -> str:
    """Retire ponctuation / casse / espaces multiples pour comparaison."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_hash(title: str) -> str:
    """Hash court d'un titre normalisé pour dédoublonnage cross-langue."""
    norm = normalize_title(title)
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]


def parse_date_french(text: str) -> Optional[str]:
    """Parse '19 mars 2026' / '19/03/2026' / '2026-03-19' → ISO yyyy-mm-dd."""
    if not text:
        return None
    text = text.strip()
    months_fr = {
        "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
        "jan": 1, "fév": 2, "fev": 2, "avr": 4, "juil": 7, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12,
    }
    months_en = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    # Format ISO direct
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except ValueError:
            pass
    # Format "19 mars 2026" (FR)
    m = re.search(r"(\d{1,2})\s+([a-zéèûôîàç]+)\s+(\d{4})", text, re.I)
    if m:
        day, month_name, year = m.group(1), m.group(2).lower(), m.group(3)
        if month_name in months_fr:
            return f"{int(year):04d}-{months_fr[month_name]:02d}-{int(day):02d}"
    # Format "March 19, 2026" / "March 19 2026" (EN)
    m = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", text, re.I)
    if m:
        month_name, day, year = m.group(1).lower(), m.group(2), m.group(3)
        if month_name in months_en:
            return f"{int(year):04d}-{months_en[month_name]:02d}-{int(day):02d}"
    # Format "DD/MM/YYYY" ou "DD-MM-YYYY"
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text)
    if m:
        try:
            return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
        except ValueError:
            pass
    # Format JP "2026年3月19日"
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def title_excluded(title: str) -> bool:
    if not title:
        return True
    low = title.lower()
    for kw in EXCLUDE_TITLE_KEYWORDS:
        if kw.lower() in low:
            return True
    return False


# ─────────────────── Scraping par site ───────────────────
def scrape_fr_topics(source: dict) -> list[dict]:
    """Scrape la page actualités du site français."""
    html = fetch_html(source["topics_url"])
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    # Le site utilise des cards avec date, catégorie en majuscules, titre
    # Structure type observée :
    # <a href="..."><img/><span>19 mars 2026</span><span>PRODUITS</span><h3>...</h3></a>
    seen_urls = set()
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        if not re.search(r"/(topics|products|cardlist)/[^/]+", href):
            continue
        full_url = urljoin(source["base"], href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue
        # EXIGENCE : date présente au format FR "19 mars 2026"
        date_match = re.search(r"\d{1,2}\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+\d{4}", text, re.I)
        if not date_match:
            continue
        date_iso = parse_date_french(date_match.group(0))
        category = None
        for cat in KEEP_CATEGORIES_FR:
            if cat in text:
                category = cat
                break
        if not category:
            continue
        # Titre : ce qui suit la catégorie
        idx = text.find(category)
        title = text[idx + len(category):].strip() if idx >= 0 else text
        title = re.sub(r"^[\s.\-—:]+", "", title)
        title = re.sub(r"a été mis à jour\.?\s*$", "", title, flags=re.I).strip()
        # Coupe les éventuels parasites de menu
        title = re.split(r"\b(VOIR TOUT|LISTE DES|PLUS D)\b", title)[0].strip()
        if not title or len(title) < 10:
            continue
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        if title_excluded(title):
            continue
        img = link.find("img")
        img_url = img.get("src") if img else None
        if img_url:
            img_url = urljoin(source["base"], img_url)
        results.append({
            "title": title,
            "url": full_url,
            "image_url": img_url,
            "category": category,
            "lang": source["lang"],
            "source_label": source["label"],
            "source_name": source["name"],
            "published_date": date_iso,
            "set_code": detect_set_code(title),
        })
    return results


def scrape_en_topics(source: dict) -> list[dict]:
    """Scrape les pages news EN. Structure observée :
    chaque item a une DATE (April 15, 2026), une CATÉGORIE (PRODUCTS/NEWS/EVENTS) et un TITRE.
    On exige la présence d'une date pour valider que c'est une vraie news (pas un menu)."""
    html = fetch_html(source["topics_url"])
    if not html:
        html = fetch_html(source["base"] + "/news/")
        if not html:
            return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_urls = set()
    # Cible UNIQUEMENT les <a> qui pointent vers /news/ ou /products/
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        # Filtre URL : doit pointer vers une fiche news ou produit
        if not re.search(r"/(news|products|topics)/[^/]+", href):
            continue
        # Évite les doublons d'URL
        full_url = urljoin(source["base"], href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        text = link.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue

        # EXIGENCE : une vraie date doit être présente dans le bloc
        date_match = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
        if not date_match:
            continue  # pas de date = c'est probablement un menu/lien navigation
        date_iso = parse_date_french(date_match.group(0))

        # Catégorie
        category = None
        for cat in KEEP_CATEGORIES_EN:
            if re.search(r"\b" + cat + r"\b", text):
                category = cat
                break
        if not category:
            continue

        # Titre : après la catégorie
        idx = text.find(category)
        title = text[idx + len(category):].strip() if idx >= 0 else text
        title = re.sub(r"^[\s.\-—:]+", "", title)
        # Coupe au premier "VIEW ALL" ou "READ MORE" ou date suivante (parasites menus)
        title = re.split(r"\b(VIEW ALL|READ MORE|LATEST INFORMATION)\b", title)[0].strip()
        if not title or len(title) < 10:
            continue
        if len(title) > 200:
            title = title[:200].rsplit(" ", 1)[0] + "…"
        if title_excluded(title):
            continue

        img = link.find("img")
        img_url = img.get("src") if img else None
        if img_url:
            img_url = urljoin(source["base"], img_url)
        results.append({
            "title": title,
            "url": full_url,
            "image_url": img_url,
            "category": category,
            "lang": source["lang"],
            "source_label": source["label"],
            "source_name": source["name"],
            "published_date": date_iso,
            "set_code": detect_set_code(title),
        })
    return results


def scrape_jp_topics(source: dict) -> list[dict]:
    """Scrape le site japonais. Le code latin (OP-XX) est conservé tel quel."""
    html = fetch_html(source["topics_url"])
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    seen_urls = set()
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        if not re.search(r"/(topics|products|cardlist)/[^/]+", href):
            continue
        full_url_check = urljoin(source["base"], href)
        if full_url_check in seen_urls:
            continue
        seen_urls.add(full_url_check)
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue
        # EXIGENCE : date japonaise présente
        if not re.search(r"\d{4}年\d{1,2}月\d{1,2}日", text):
            continue
        category = None
        for cat in KEEP_CATEGORIES_JP:
            if cat in text:
                category = cat
                break
        if not category:
            continue
        date_iso = parse_date_french(text)
        # Titre JP : tout sauf catégorie et date
        title = text
        # On retire les patterns connus
        title = re.sub(r"\d{4}年\d{1,2}月\d{1,2}日", "", title)
        title = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", "", title)
        for cat in KEEP_CATEGORIES_JP:
            title = title.replace(cat, "")
        title = title.strip()
        if not title or len(title) < 6:
            continue
        if title_excluded(title):
            continue
        full_url = urljoin(source["base"], href)
        img = link.find("img")
        img_url = img.get("src") if img else None
        if img_url:
            img_url = urljoin(source["base"], img_url)
        results.append({
            "title": title,
            "url": full_url,
            "image_url": img_url,
            "category": category,
            "lang": source["lang"],
            "source_label": source["label"],
            "source_name": source["name"],
            "published_date": date_iso,
            "set_code": detect_set_code(title),
        })
    return results


def scrape_source(source: dict) -> list[dict]:
    """Dispatcher selon la langue."""
    if source["lang"] == "FR":
        return scrape_fr_topics(source)
    if source["lang"] == "EN":
        return scrape_en_topics(source)
    if source["lang"] == "JP":
        return scrape_jp_topics(source)
    return []


# ─────────────────── Déduplication ───────────────────
def make_dedup_key(item: dict) -> str:
    """Clé qui groupe les annonces identiques entre langues.
    Si on a un set_code → utilise le set_code (très fiable).
    Sinon → hash du titre normalisé (moins précis mais ok)."""
    if item.get("set_code"):
        return f"set:{item['set_code']}"
    return f"title:{title_hash(item.get('title', ''))}"


def dedupe(items: list[dict]) -> list[dict]:
    """Fusionne les doublons cross-langue, en privilégiant FR > EN > JP comme version affichée.
    Le résultat conserve toutes les sources observées dans une liste 'sources'."""
    LANG_PRIORITY = {"FR": 3, "EN": 2, "JP": 1}
    grouped = {}
    for it in items:
        key = make_dedup_key(it)
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(it)
    merged = []
    for key, group in grouped.items():
        # Trie par priorité de langue (FR en premier)
        group.sort(key=lambda x: LANG_PRIORITY.get(x.get("lang", ""), 0), reverse=True)
        primary = dict(group[0])  # copie
        # Liste toutes les sources rencontrées (uniques)
        seen_langs = set()
        sources_list = []
        for g in group:
            if g["lang"] not in seen_langs:
                seen_langs.add(g["lang"])
                sources_list.append({
                    "lang": g["lang"],
                    "label": g["source_label"],
                    "name": g["source_name"],
                    "url": g["url"],
                    "published_date": g.get("published_date"),
                })
        primary["sources"] = sources_list
        merged.append(primary)
    return merged


# ─────────────────── Persistance ───────────────────
def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception:
            log(f"⚠️  news_history.json corrompu, réinitialisation", indent=1)
    return {"items": {}}


def save_history(hist: dict) -> None:
    HISTORY_FILE.write_text(json.dumps(hist, indent=2, ensure_ascii=False))


def prune_old(hist: dict, days: int = ROLLING_WINDOW_DAYS) -> int:
    """Supprime les annonces plus vieilles que `days` jours (basé sur first_seen)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    before = len(hist["items"])
    hist["items"] = {
        k: v for k, v in hist["items"].items()
        if v.get("first_seen", "9999") >= cutoff
    }
    return before - len(hist["items"])


def merge_into_history(hist: dict, items: list[dict]) -> tuple[int, int]:
    """Ajoute les nouvelles annonces, met à jour les sources des anciennes.
    Renvoie (nouveau, mis_a_jour)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    new_count = 0
    upd_count = 0
    for it in items:
        key = make_dedup_key(it)
        if key in hist["items"]:
            existing = hist["items"][key]
            # Met à jour les sources si nouvelle langue détectée
            existing_langs = {s["lang"] for s in existing.get("sources", [])}
            for src in it.get("sources", []):
                if src["lang"] not in existing_langs:
                    existing.setdefault("sources", []).append(src)
                    existing_langs.add(src["lang"])
                    upd_count += 1
            existing["last_seen"] = now_iso
        else:
            it_copy = dict(it)
            it_copy["first_seen"] = now_iso
            it_copy["last_seen"] = now_iso
            hist["items"][key] = it_copy
            new_count += 1
    return new_count, upd_count


# ─────────────────── Main ───────────────────
def main():
    log("📰 Collecte des news officielles One Piece TCG")
    all_items = []
    for source in SOURCES:
        log(f"🌐 {source['name']} ({source['lang']})", indent=1)
        items = scrape_source(source)
        log(f"{len(items)} annonce(s) trouvée(s)", indent=2)
        all_items.extend(items)

    log(f"📊 Total brut : {len(all_items)} annonce(s)")
    deduped = dedupe(all_items)
    log(f"📊 Après dédup : {len(deduped)} annonce(s) unique(s)")

    hist = load_history()
    pruned = prune_old(hist)
    if pruned:
        log(f"🗑️  {pruned} ancienne(s) annonce(s) supprimée(s) (>{ROLLING_WINDOW_DAYS}j)")
    new_count, upd_count = merge_into_history(hist, deduped)
    save_history(hist)
    log(f"✅ {new_count} nouvelle(s) · {upd_count} mise(s) à jour · {len(hist['items'])} en stock")


if __name__ == "__main__":
    main()
