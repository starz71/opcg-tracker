#!/usr/bin/env python3
"""
OPCG Tracker — Surveillance gratuite des sorties de displays One Piece TCG.

Lit alerts.yaml, vérifie chaque alerte sur les sites configurés, envoie des
notifications via ntfy / Telegram / email pour les nouveautés et baisses de prix.

Usage : python tracker.py
"""

import os
import sys
import json
import re
import time
import random
import hashlib
import smtplib
import traceback
from pathlib import Path
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import quote_plus, urljoin

import requests
import yaml
import feedparser
from bs4 import BeautifulSoup

# cloudscraper contourne la plupart des protections Cloudflare anti-bot.
# Si non installé, on retombe sur requests classique.
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False

# ───────────────────────────── Constantes ─────────────────────────────
ROOT         = Path(__file__).resolve().parent
ALERTS_FILE  = ROOT / "alerts.yaml"
STATE_FILE   = ROOT / "state.json"
HISTORY_FILE = ROOT / "history.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 25
DELAY_RANGE = (2.0, 4.5)   # délai aléatoire entre requêtes (politesse)
MAX_RETRIES = 2
PRICE_DROP_THRESHOLD = 0.97  # notifie si le prix tombe sous 97% de l'ancien
DEFAULT_COOLDOWN_HOURS = 24  # ne pas re-notifier le même produit dans X heures

# Mots-clés qui indiquent une RUPTURE DE STOCK (le produit est ignoré).
OUT_OF_STOCK_MARKERS = [
    "rupture", "épuisé", "epuise", "indisponible", "non disponible",
    "out of stock", "sold out", "soldout", "sold-out",
    "plus en stock", "plus disponible", "non dispo",
    "vendu", "stock épuisé",
]
OUT_OF_STOCK_CSS_HINTS = [
    "out-of-stock", "outofstock", "sold-out", "soldout",
    "unavailable", "no-stock", "nostock",
]
# Marqueurs de précommande (le produit est suivi mais marqué "preorder")
PREORDER_MARKERS = [
    "précommande", "precommande", "pré-commande", "pre-commande",
    "preorder", "pre-order", "pre order",
    "à paraître", "a paraitre", "à venir", "a venir",
    "sortie le", "sortie prévue", "disponible le", "dispo le",
    "available on", "release date", "release on",
    "réservation", "reservation",
]
PREORDER_CSS_HINTS = [
    "preorder", "pre-order", "precommande", "pré-commande",
]

# Sélecteurs CSS par plateforme — utilisés en fallback automatique quand un
# site ne définit pas explicitement ses selectors.
PLATFORM_SELECTORS = {
    "prestashop": {
        "product": "article.js-product-miniature, article.product-miniature, .product-miniature",
        "title": ".product-title a, .product-name a, h2.product-title, .product-title",
        "link": ".product-title a, .product-name a, .thumbnail-container a",
        # Plus exhaustif : on essaie span.price puis .product-price-and-shipping,
        # puis [content] (microdata Prestashop), puis fallback générique
        "price": "span.price, .product-price-and-shipping .price, .product-price, [itemprop=price], .regular-price",
        "availability": ".product-availability, .product-flags, .out-of-stock",
    },
    "shopify": {
        "product": ".product-card, .product-item, .grid__item, .grid-product, [class*=ProductCard], .card-wrapper",
        "title": ".product-card__title, .product-item__title, .product-card__name, h3, .product-title, .card__heading",
        "link": "a.product-card__link, a.product-item__image-wrapper, .grid-product__link, .product-card__media, a.full-unstyled-link, a",
        "price": ".price__current, .price-item--regular, .product-card__price, .price, .money, .price-item, [class*=price]",
        "availability": ".badge--bottom-left, .product-card__sold-out, .sold-out, [class*=sold-out]",
    },
    "woocommerce": {
        "product": "li.product, .product, .wc-block-grid__product",
        "title": ".woocommerce-loop-product__title, h2.woocommerce-loop-product__title, .wc-block-grid__product-title",
        "link": "a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link, a.wc-block-grid__product-link",
        "price": ".price ins .amount, .price > .amount, .price .amount, .woocommerce-Price-amount",
        "availability": ".out-of-stock, .stock, .outofstock",
    },
}

# ───────────────────────────── Logging ────────────────────────────────
def log(msg, indent=0):
    print(("  " * indent) + msg, flush=True)

# ───────────────────────────── Persistence ────────────────────────────
def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log(f"⚠️  Corruption détectée dans {path.name}, réinitialisation.")
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ───────────────────────────── HTTP ───────────────────────────────────
def make_session():
    """Crée une session HTTP. Utilise cloudscraper si disponible (contourne
    la plupart des protections Cloudflare), sinon requests classique."""
    if HAS_CLOUDSCRAPER:
        s = cloudscraper.create_scraper(
            browser={"browser": "firefox", "platform": "windows", "mobile": False}
        )
    else:
        s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        "DNT": "1",
        "Cache-Control": "no-cache",
    })
    return s

def fetch(session, url):
    """GET avec retry et backoff léger."""
    last_err = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as ex:
            last_err = ex
            if attempt < MAX_RETRIES:
                time.sleep(2 + attempt * 2)
    raise last_err

# ───────────────────────────── Parsing prix ───────────────────────────
def parse_price(text):
    """Extrait un float depuis '12,90 €' ou '€12.90' ou '1 234,56 €'."""
    if not text:
        return None
    t = str(text).replace("\xa0", " ").replace("&nbsp;", " ").strip()
    m = re.search(r"(\d{1,4}(?:[\.,\s]\d{2,3})?)\s*(?:€|EUR)", t)
    if not m:
        m = re.search(r"(\d+[\.,]?\d*)", t)
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(",", ".")
    if raw.count(".") > 1:                  # "1.234.56" → "1234.56"
        parts = raw.split(".")
        raw = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(raw)
    except ValueError:
        return None

def extract_price(card, price_selector):
    """Essaie d'extraire un prix depuis une carte produit en testant plusieurs
    stratégies : sélecteur fourni, attribut content/data-price (microdata),
    puis recherche élargie."""
    if not price_selector:
        return None

    # Stratégie 1 : on essaie chaque sélecteur de la liste séparément
    for sel in [s.strip() for s in price_selector.split(",")]:
        try:
            els = card.select(sel)
        except Exception:
            continue
        for el in els:
            # Tenter d'abord les attributs (microdata)
            for attr in ("content", "data-price", "data-product-price"):
                v = el.get(attr)
                if v:
                    p = parse_price(v)
                    if p:
                        return p
            # Puis le texte visible
            p = parse_price(text_of(el))
            if p:
                return p

    # Stratégie 2 : balayer tous les éléments avec un attribut content="prix"
    for el in card.select("[itemprop=price], [content]"):
        v = el.get("content")
        if v:
            p = parse_price(v)
            if p:
                return p

    return None

def text_of(el):
    return el.get_text(" ", strip=True) if el else ""

def is_out_of_stock(card, availability_text=""):
    """Détecte si un produit est en rupture de stock à partir de sa carte HTML
    et du texte d'availability extrait. PRÉCOMMANDES = en stock (renvoie False).
    Renvoie True si rupture détectée."""
    # 1. Vérifier le texte d'availability extrait
    if availability_text:
        low = availability_text.lower()
        for marker in OUT_OF_STOCK_MARKERS:
            if marker in low:
                return True

    # 2. Vérifier les classes CSS de la carte (et de ses enfants directs)
    card_html = str(card.attrs) + " " + " ".join(
        " ".join(c.get("class") or []) for c in card.find_all(class_=True, recursive=False)
    )
    card_html_low = card_html.lower()
    for hint in OUT_OF_STOCK_CSS_HINTS:
        if hint in card_html_low:
            return True

    # 3. Vérifier le texte global de la carte (dernier recours, prudent)
    card_text = card.get_text(" ", strip=True).lower()
    if "sold out" in card_text or "soldout" in card_text:
        return True
    if "rupture de stock" in card_text or "stock épuisé" in card_text:
        return True

    return False

def is_preorder(card, availability_text="", title=""):
    """Détecte si un produit est en précommande (et non en stock immédiat)."""
    # 1. Texte d'availability
    if availability_text:
        low = availability_text.lower()
        for marker in PREORDER_MARKERS:
            if marker in low:
                return True
    # 2. Classes CSS
    card_html = str(card.attrs) + " " + " ".join(
        " ".join(c.get("class") or []) for c in card.find_all(class_=True, recursive=False)
    )
    card_html_low = card_html.lower()
    for hint in PREORDER_CSS_HINTS:
        if hint in card_html_low:
            return True
    # 3. Titre du produit
    if title:
        title_low = title.lower()
        for marker in ["précommande", "precommande", "preorder", "pre-order"]:
            if marker in title_low:
                return True
    return False

# ───── Détection du type de produit (Display, Booster, Starter, etc.) ─────
PRODUCT_TYPE_RULES = [
    # (label,        liste de patterns regex à tester sur le titre lower-case,
    #                liste de mots-clés DISQUALIFIANTS qui empêchent de matcher)
    # ⚠ ORDRE IMPORTANT : Display avant Booster (Booster Box = Display, pas Booster)
    ("case",     [r"\bcase\b", r"\bcarton\b",
                  r"12\s*x?\s*(display|booster\s*box)", r"lot de 12"], []),
    ("display",  [r"\bdisplay\b", r"booster\s*box", r"bo[iî]te\s+de\s+booster", r"\bbb\b"], []),
    ("starter",  [r"\bstarter\b", r"\bdeck de d[ée]marrage\b", r"structure\s*deck",
                  r"\bsd\b"], []),
    ("box",      [r"\bgift\s*box\b", r"\bcoffret\b", r"\btournament\s*pack\b",
                  r"\bbox\b"], ["booster"]),  # "Booster Box" → exclut box
    ("booster",  [r"\bbooster\b", r"\bsachet\b", r"\bpack\b"],
                 ["box", "display"]),
]

def detect_product_type(title):
    """Renvoie 'display', 'booster', 'starter', 'box', 'case' ou 'other'."""
    if not title:
        return "other"
    t = title.lower()
    for label, patterns, disq in PRODUCT_TYPE_RULES:
        # Disqualifiants : si présents, on saute ce label
        if any(d in t for d in disq):
            continue
        for p in patterns:
            if re.search(p, t):
                return label
    return "other"

# ───────────────────────────── Recherche site ─────────────────────────
def search_site(session, site, query):
    """Cherche `query` sur un site, renvoie la liste des produits trouvés."""
    if not site.get("search_url"):
        return []
    url = site["search_url"].replace("{query}", quote_plus(query))
    try:
        r = fetch(session, url)
    except Exception as ex:
        log(f"⚠️  {site['name']}: {ex}", indent=2)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    sel = site.get("selectors", {})
    product_sel = sel.get("product")

    if not product_sel:
        # Fallbacks usuels
        for fb in ["article.product", "li.product", ".product-item",
                   ".product-miniature", "[data-product]"]:
            if soup.select(fb):
                product_sel = fb
                break
    if not product_sel:
        log(f"⚠️  {site['name']}: aucun sélecteur produit trouvé", indent=2)
        return []

    results = []
    for card in soup.select(product_sel)[:25]:
        title = text_of(card.select_one(sel.get("title", "h2, h3, .title, a")))
        link_el = card.select_one(sel.get("link", "a[href]"))
        href = link_el.get("href") if link_el else None
        price = extract_price(card, sel.get("price", ".price, [class*=price]"))
        avail = text_of(card.select_one(sel.get("availability"))) if sel.get("availability") else ""
        if not (title and href):
            continue
        results.append({
            "title": title,
            "url": urljoin(url, href),
            "price": price,
            "site": site["name"],
            "availability": avail,
        })
    return results

# ───────────────────────── Page catégorie ─────────────────────────────
def scrape_category(session, site):
    """Scrape la page catégorie One Piece d'un site. Renvoie tous les
    produits visibles (titre, URL, prix, dispo)."""
    url = site.get("category_url")
    if not url:
        return []
    try:
        r = fetch(session, url)
    except Exception as ex:
        log(f"⚠️  {site['name']}: {ex}", indent=2)
        return []

    soup = BeautifulSoup(r.text, "lxml")

    # Priorité : sélecteurs explicites > plateforme déclarée > auto-détection
    sel = (site.get("selectors") or {}).copy()
    if not sel.get("product"):
        platform = site.get("platform")
        if platform in PLATFORM_SELECTORS:
            sel = {**PLATFORM_SELECTORS[platform], **sel}
        else:
            # Auto-détection : essaie chaque plateforme
            for p in ["prestashop", "shopify", "woocommerce"]:
                test = PLATFORM_SELECTORS[p]["product"]
                if soup.select(test):
                    sel = {**PLATFORM_SELECTORS[p], **sel}
                    site["_detected_platform"] = p
                    break

    product_sel = sel.get("product")
    if not product_sel or not soup.select(product_sel):
        log(f"⚠️  {site['name']}: aucun produit détecté ({url})", indent=2)
        return []

    results = []
    skipped_oos = 0
    for card in soup.select(product_sel)[:60]:
        title = text_of(card.select_one(sel.get("title", "h2, h3, .title, a")))
        link_el = card.select_one(sel.get("link", "a[href]"))
        href = link_el.get("href") if link_el else None
        price = extract_price(card, sel.get("price", ".price"))
        avail = text_of(card.select_one(sel.get("availability"))) if sel.get("availability") else ""
        if not (title and href):
            continue
        # Calcul du statut : out / preorder / in
        oos = is_out_of_stock(card, avail)
        if oos:
            status = "out"
            skipped_oos += 1
        elif is_preorder(card, avail, title):
            status = "preorder"
        else:
            status = "in"
        ptype = detect_product_type(title)
        results.append({
            "title": title,
            "url": urljoin(url, href),
            "price": price,
            "site": site["name"],
            "availability": avail,
            "is_oos": oos,            # legacy : encore utilisé par les transitions
            "status": status,         # nouveau : in / preorder / out
            "product_type": ptype,    # display / booster / starter / box / case / other
        })
    if skipped_oos:
        log(f"({skipped_oos} produits en rupture détectés et suivis pour transitions)", indent=2)
    return results

# ─────────────────── Cardmarket : référence prix ──────────────────────
def fetch_cardmarket_ref(session, ref):
    """Récupère le prix le plus bas d'une URL Cardmarket pour comparaison.
    `ref` = {url, label}. Renvoie {price, label, url} ou None."""
    if not ref or not ref.get("url"):
        return None
    try:
        r = fetch(session, ref["url"])
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    price = None
    for sel in [".article-row .color-primary",
                ".price-container .color-primary",
                "[class*=lowestPrice]",
                ".info-list-container dd"]:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            p = parse_price(text_of(el))
            if p:
                price = p
                break
    if price is None:
        return None
    return {"price": price, "label": ref.get("label", "Cardmarket"),
            "url": ref["url"]}

# ──────────── Détection automatique du code de set + langue ────────────
# Patterns pour détecter le code de set dans un titre :
# OP-12, OP12, OP 12 → "OP-12"
# EB-02, EB02 → "EB-02"
# PRB-01, PRB01 → "PRB-01"
# ST-25, ST25 → "ST-25"
SET_CODE_RE = re.compile(
    r"\b(OP|EB|PRB|ST)\s*[-_]?\s*(\d{1,2})\b",
    re.IGNORECASE,
)

# Mots-clés indiquant la langue d'un produit dans son titre
LANG_MARKERS = {
    "jp": ["japonais", "japonaise", "japon", "japan", "jp ", " jp", "(jp)",
           "version japonaise", "version jp", "jap "],
    "en": ["english", "anglais", "anglaise", "(en)", " en ", "en/", "/en",
           "version anglaise", "version english"],
    # FR par défaut, on le met en dernier (reconnu si rien d'autre ne match)
    "fr": ["français", "francais", "française", "francaise", "(fr)", " fr ",
           "version française", "version francaise"],
}

def detect_set_and_language(title, site_priority=None):
    """Détecte le code de set + la langue à partir du titre d'un produit.
    Renvoie (set_code, lang) où set_code = 'OP-12'/'EB-02'/... ou None,
    et lang = 'fr' / 'en' / 'jp'.
    Si site_priority='JP', on assume JP par défaut quand la langue n'est pas
    explicitée dans le titre (parce que les sites JP vendent du JP). Idem
    si le site est FR, on assume FR par défaut."""
    if not title:
        return None, None

    # 1) Code de set
    m = SET_CODE_RE.search(title)
    set_code = None
    if m:
        prefix = m.group(1).upper()
        num = m.group(2).zfill(2)
        set_code = f"{prefix}-{num}"

    # 2) Langue : on cherche les marqueurs explicites
    title_lower = title.lower()
    detected_lang = None
    for lang in ("jp", "en", "fr"):
        for marker in LANG_MARKERS[lang]:
            if marker in title_lower:
                detected_lang = lang
                break
        if detected_lang:
            break

    # 3) Si rien de détecté, on devine selon la priorité du site
    if not detected_lang:
        if site_priority == "JP":
            detected_lang = "jp"
        else:
            # Par défaut sur sites européens, on assume FR (le marché principal)
            detected_lang = "fr"

    return set_code, detected_lang

def resolve_cm_ref(alert, listing, lookups, site_priority=None):
    """Renvoie un cardmarket_ref ({url, label}) à utiliser pour cette notif.
    Priorité :
      1) alert.cardmarket_ref s'il est défini explicitement
      2) lookup automatique via cardmarket_lookups[set_code][lang]
      3) None (pas de comparaison CM dans la notif)"""
    # Priorité 1 : ref explicite dans l'alerte
    if alert.get("cardmarket_ref"):
        return alert["cardmarket_ref"]

    # Priorité 2 : lookup auto
    if not lookups:
        return None
    set_code, lang = detect_set_and_language(listing.get("title", ""), site_priority)
    if not set_code:
        return None
    entry = lookups.get(set_code)
    if not entry:
        return None
    url = entry.get(lang) or entry.get("fr") or entry.get("en") or entry.get("jp")
    if not url:
        return None
    label = f"CM {lang.upper()}"
    return {"url": url, "label": label}
def check_url(session, url, site_name):
    """Récupère titre + prix d'une fiche produit unique (OG / JSON-LD / fallback)."""
    try:
        r = fetch(session, url)
    except Exception as ex:
        log(f"⚠️  {site_name}: {ex}", indent=2)
        return None
    soup = BeautifulSoup(r.text, "lxml")
    title = price = None

    og_title = soup.find("meta", {"property": "og:title"})
    if og_title:
        title = og_title.get("content")

    og_price = (soup.find("meta", {"property": "product:price:amount"})
                or soup.find("meta", {"itemprop": "price"}))
    if og_price:
        price = parse_price(og_price.get("content"))

    # JSON-LD
    if not price:
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for it in items:
                    offers = it.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    for k in ("price", "lowPrice"):
                        if k in offers:
                            price = parse_price(str(offers[k]))
                            break
                    if price:
                        break
            except Exception:
                continue
            if price:
                break

    # Fallback DOM visible
    if not price:
        for s in [".price", "[class*=price]", "[itemprop=price]"]:
            el = soup.select_one(s)
            if el:
                price = parse_price(text_of(el))
                if price:
                    break

    if not title:
        h1 = soup.find("h1")
        title = text_of(h1) if h1 else url

    return {"title": title, "url": url, "price": price,
            "site": site_name, "availability": ""}

# ───────────────────────────── Cardmarket ─────────────────────────────
def check_cardmarket(session, alert):
    """Récupère le prix le plus bas d'une fiche produit Cardmarket."""
    url = alert.get("url")
    if not url:
        return None
    try:
        r = fetch(session, url)
    except Exception as ex:
        log(f"⚠️  Cardmarket: {ex}", indent=2)
        return None

    soup = BeautifulSoup(r.text, "lxml")
    title = text_of(soup.find("h1")) or alert.get("name", "Cardmarket")

    price = None
    candidates = [
        ".article-row .color-primary",
        ".price-container .color-primary",
        "[class*=lowestPrice]",
        ".info-list-container dd",
    ]
    for sel in candidates:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el:
            p = parse_price(text_of(el))
            if p:
                price = p
                break

    return {"title": title, "url": url, "price": price,
            "site": "Cardmarket", "availability": ""}

# ───────────────────────────── RSS / Atom ─────────────────────────────
def check_rss(alert):
    """Lit un flux RSS/Atom. Renvoie une liste d'items {title, url, content, site}."""
    url = alert.get("url")
    if not url:
        return []
    try:
        feed = feedparser.parse(url, request_headers={"User-Agent": UA})
    except Exception as ex:
        log(f"⚠️  RSS {url}: {ex}", indent=2)
        return []

    if feed.bozo and not feed.entries:
        log(f"⚠️  RSS {url}: flux invalide ({feed.get('bozo_exception')})", indent=2)
        return []

    site_name = alert.get("site_name") or feed.feed.get("title") or alert.get("name", "RSS")
    items = []
    for entry in feed.entries[:30]:
        title = entry.get("title", "") or ""
        link = entry.get("link", "") or url
        # Contenu : on essaie summary, content, description
        content = ""
        if "summary" in entry:
            content = entry.summary
        if "content" in entry and entry.content:
            try:
                content = entry.content[0].get("value", content)
            except Exception:
                pass
        # On nettoie le HTML éventuel
        if content and "<" in content:
            content = BeautifulSoup(content, "lxml").get_text(" ", strip=True)
        items.append({
            "title": title,
            "url": link,
            "content": content,
            "site": site_name,
            "price": None,
            "availability": "",
        })
    return items

# ───────────────────────────── Page news HTML ─────────────────────────
def check_news_page(session, alert):
    """Scrape une page de news/blog. Renvoie liste d'items {title, url, content, site}."""
    url = alert.get("url")
    if not url:
        return []
    try:
        r = fetch(session, url)
    except Exception as ex:
        log(f"⚠️  News {url}: {ex}", indent=2)
        return []

    soup = BeautifulSoup(r.text, "lxml")
    sel = alert.get("selectors", {}) or {}
    item_sel = sel.get("item")

    if not item_sel:
        for fb in ["article", ".news-item", ".post", ".entry", "li.news",
                   ".article-item", "[class*=news]"]:
            if soup.select(fb):
                item_sel = fb
                break
    if not item_sel:
        log(f"⚠️  News {url}: aucun sélecteur d'item trouvé", indent=2)
        return []

    site_name = alert.get("site_name") or alert.get("name", "Site")
    items = []
    for it in soup.select(item_sel)[:30]:
        title_el = it.select_one(sel.get("title", "h2, h3, .title, a"))
        link_el = it.select_one(sel.get("link", "a[href]"))
        if not (title_el and link_el):
            continue
        href = link_el.get("href", "")
        items.append({
            "title": text_of(title_el),
            "url": urljoin(url, href),
            "content": text_of(it)[:400],
            "site": site_name,
            "price": None,
            "availability": "",
        })
    return items

# ───────────────────────────── Matching ───────────────────────────────
def matches(alert, listing):
    # Pour les news/RSS, on cherche dans titre + contenu (un mot-clé peut
    # apparaître dans le corps de l'article). Pour le reste, titre seul
    # pour éviter les faux positifs (sidebars, recommandations).
    if alert.get("type") in ("rss", "news_page"):
        haystack = " ".join([
            listing.get("title") or "",
            listing.get("content") or "",
        ]).lower()
    else:
        haystack = (listing.get("title") or "").lower()

    keywords = [k.lower() for k in (alert.get("keywords") or [])]
    if keywords and not any(k in haystack for k in keywords):
        return False

    must = [k.lower() for k in (alert.get("must_include") or [])]
    if must and not all(k in haystack for k in must):
        return False

    bad = [k.lower() for k in (alert.get("exclude") or [])]
    if bad and any(k in haystack for k in bad):
        return False

    if alert.get("max_price") is not None and listing.get("price") is not None:
        if listing["price"] > alert["max_price"]:
            return False

    if alert.get("min_price") is not None and listing.get("price") is not None:
        if listing["price"] < alert["min_price"]:
            return False

    return True

# ───────────────────────────── Notifications ──────────────────────────
def env_or(cfg, key):
    """Lit une crédentielle depuis env (UPPERCASE) sinon depuis cfg."""
    return os.environ.get(key.upper()) or cfg.get(key)

def notify_ntfy(topic, title, body, click_url=None, priority="default", tags=None):
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if click_url:
        # Header Click : ouvre l'URL au tap simple
        headers["Click"] = click_url
        # Header Actions : ajoute un bouton "Voir le produit" visible sur la notif
        # Format : "view, <label>, <url>, clear=true"
        action_label = "Voir le produit"
        headers["Actions"] = f"view, {action_label}, {click_url}, clear=true"
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(f"https://ntfy.sh/{topic}",
                      data=body.encode("utf-8"),
                      headers=headers, timeout=10)
    except Exception as ex:
        log(f"⚠️  ntfy: {ex}", indent=2)

def notify_telegram(token, chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as ex:
        log(f"⚠️  telegram: {ex}", indent=2)

def notify_email(cfg, subject, body):
    try:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = cfg["from"], cfg["to"], subject
        msg.set_content(body)
        with smtplib.SMTP_SSL(cfg.get("smtp", "smtp.gmail.com"),
                              cfg.get("port", 465)) as s:
            s.login(cfg["from"], cfg["password"])
            s.send_message(msg)
    except Exception as ex:
        log(f"⚠️  email: {ex}", indent=2)

def send_notifications(config, alert, listing, kind, previous=None, cm_data=None):
    n = config.get("notifications", {})

    # Titre enrichi : emoji + nom alerte + site + prix
    site = listing.get("site", "")
    price_short = f"{int(listing['price'])}€" if listing.get("price") is not None else ""
    title_parts = []
    if kind == "price_drop":
        title_parts.append("📉")
        tag = "money_with_wings"
    elif kind == "back_in_stock":
        # 🔥 Distinction visuelle forte : retour en stock = la fenêtre d'achat
        title_parts.append("🔥 RETOUR STOCK —")
        tag = "fire"
    else:
        title_parts.append("🚨")
        tag = "rotating_light"
    title_parts.append(alert['name'])
    if site:
        title_parts.append(f"— {site}")
    if price_short:
        title_parts.append(price_short)
    title = " ".join(title_parts)

    price_str = f"{listing['price']}€" if listing.get("price") is not None else "prix non détecté"
    body_lines = [listing["title"], f"📍 {listing['site']} — {price_str}"]
    if kind == "back_in_stock":
        body_lines.append("🔥 Ce produit était en rupture au run précédent — fenêtre d'achat ouverte !")
    if previous and listing.get("price"):
        body_lines.append(f"💰 Avant : {previous}€ → maintenant {listing['price']}€")
    if listing.get("availability"):
        body_lines.append(f"📦 {listing['availability']}")
    # Référence Cardmarket pour comparaison
    if cm_data and cm_data.get("price") is not None:
        cm_label = cm_data.get("label", "Cardmarket")
        cm_price = cm_data["price"]
        cur_price = listing.get("price")
        if cur_price is not None:
            delta_pct = (cur_price - cm_price) / cm_price * 100
            arrow = "✅" if delta_pct < 0 else "⚠️"
            body_lines.append(f"📊 {cm_label} : {cm_price}€  {arrow} ({delta_pct:+.0f}%)")
        else:
            body_lines.append(f"📊 {cm_label} : {cm_price}€")
    # Lien explicite à la fin (en plus du Click header pour ntfy)
    body_lines.append(f"🔗 {listing['url']}")
    body = "\n".join(body_lines)

    topic = env_or(n, "ntfy_topic")
    if topic:
        # Priorité haute pour les nouveautés ET les retours en stock (max=urgence)
        priority = "max" if kind == "back_in_stock" else ("high" if kind == "new" else "default")
        notify_ntfy(topic, title, body, click_url=listing["url"],
                    priority=priority, tags=[tag])

    tg_token = env_or(n, "telegram_bot_token")
    tg_chat = env_or(n, "telegram_chat_id")
    if tg_token and tg_chat:
        md = f"*{title}*\n\n{body}\n\n[👉 Ouvrir]({listing['url']})"
        notify_telegram(tg_token, tg_chat, md)

    email_cfg = n.get("email") or {}
    if email_cfg.get("from") and email_cfg.get("password") and email_cfg.get("to"):
        notify_email(email_cfg, title, f"{body}\n\n{listing['url']}")

    log(f"📨 {title}", indent=2)

# ───────────────────────────── Boucle principale ──────────────────────
def listing_id(listing):
    return hashlib.sha256(f"{listing['site']}::{listing['url']}".encode()).hexdigest()[:14]

def process_alert(config, alert, listings, state, history, now_iso, get_cm_callable=None, sites_config=None):
    """Logique de notification :
       - Produit OOS  : on enregistre l'état mais on ne notifie pas
       - 1re vue en stock                : notif "new" (cooldown actif)
       - Transition OOS → en stock       : notif "back_in_stock" (BYPASSE le cooldown,
                                            c'est l'événement le plus important pour
                                            l'utilisateur — fenêtre d'achat critique)
       - Baisse de prix significative    : notif "price_drop" (cooldown actif)
       - Re-vue en stock même état       : silence (cooldown ou pas)
    """
    seen = state.setdefault("seen", {})
    fired = 0
    # Cooldown : configurable au niveau alerte ou globalement, défaut 24h
    cooldown_h = alert.get("notify_cooldown_hours",
                           config.get("notify_cooldown_hours", DEFAULT_COOLDOWN_HOURS))
    cooldown_seconds = cooldown_h * 3600
    now_dt = datetime.now(timezone.utc)
    lookups = config.get("cardmarket_lookups") or {}

    for listing in listings:
        if not matches(alert, listing):
            continue
        lid = listing_id(listing)
        akey = f"{alert['name']}::{lid}"
        cur_oos = bool(listing.get("is_oos"))
        cur_status = listing.get("status", "unknown")
        cur_ptype = listing.get("product_type", "other")

        # On enregistre TOUJOURS l'entrée history (même pour les OOS) avec
        # last_status / last_seen / product_type pour que le dashboard puisse
        # afficher correctement les pastilles et filtrer par type.
        h = history.setdefault(lid, {
            "title": listing["title"], "url": listing["url"],
            "site": listing["site"], "prices": [],
        })
        h["title"] = listing["title"]
        h["url"] = listing["url"]
        h["site"] = listing["site"]
        h["last_status"] = cur_status
        h["last_seen"] = now_iso
        h["product_type"] = cur_ptype
        # Le PRIX n'est ajouté que si on est en stock ou en pré-co (sinon le
        # prix d'un produit OOS peut être trompeur — c'est un "ancien prix").
        if not cur_oos and listing.get("price") is not None:
            if not h["prices"] or h["prices"][-1]["price"] != listing["price"]:
                h["prices"].append({"date": now_iso, "price": listing["price"]})

        prev = seen.get(akey)
        prev_oos = prev.get("is_oos") if prev else None  # None = jamais vu
        prev_p = prev.get("price") if prev else None
        cur_p = listing.get("price")

        # Calcul du cooldown
        last_notified = prev.get("last_notified") if prev else None
        last_notified_dt = None
        if last_notified:
            try:
                last_notified_dt = datetime.fromisoformat(last_notified)
            except Exception:
                last_notified_dt = None
        in_cooldown = (last_notified_dt is not None
                       and (now_dt - last_notified_dt).total_seconds() < cooldown_seconds)

        # Mise à jour de l'état (toujours, même en cooldown ou OOS)
        seen[akey] = {
            "date": now_iso,
            "price": cur_p,
            "is_oos": cur_oos,
            "last_notified": last_notified,
        }

        # ─── Décider si on notifie ───
        should_notify = False
        kind = "new"
        prev_for_msg = None
        bypass_cooldown = False  # vrai pour les transitions OOS → en stock

        if cur_oos:
            # Produit en rupture : on enregistre, on ne notifie jamais
            pass
        elif prev_oos is True:
            # 🎯 Transition OOS → en stock : événement critique, notif immédiate
            should_notify = True
            kind = "back_in_stock"
            bypass_cooldown = True
        elif prev is None:
            # Première fois qu'on voit ce produit (et il est en stock)
            should_notify = True
            kind = "new"
        elif (prev_p is not None and cur_p is not None
              and cur_p < prev_p * PRICE_DROP_THRESHOLD):
            # Baisse de prix significative
            should_notify = True
            kind = "price_drop"
            prev_for_msg = prev_p
        # Sinon : déjà en stock au run précédent, rien de neuf, on se tait

        # Le cooldown ne s'applique PAS aux retours en stock
        if should_notify and in_cooldown and not bypass_cooldown:
            log(f"⏸  cooldown actif ({cooldown_h}h) — {listing['title'][:60]}", indent=2)
            should_notify = False

        if should_notify:
            # Résolution du Cardmarket ref pour CE listing précis (auto par set+langue)
            cm_data = None
            if get_cm_callable:
                site_priority = None
                if sites_config:
                    for sid, scfg in sites_config.items():
                        if scfg.get("name") == listing.get("site"):
                            site_priority = scfg.get("priority")
                            break
                cm_ref = resolve_cm_ref(alert, listing, lookups, site_priority)
                if cm_ref:
                    cm_data = get_cm_callable(cm_ref)

            send_notifications(config, alert, listing, kind=kind,
                               previous=prev_for_msg, cm_data=cm_data)
            seen[akey]["last_notified"] = now_iso
            fired += 1

    return fired

def main():
    if not ALERTS_FILE.exists():
        log(f"❌ {ALERTS_FILE.name} introuvable. Copiez alerts.example.yaml en alerts.yaml.")
        sys.exit(1)

    config = yaml.safe_load(ALERTS_FILE.read_text(encoding="utf-8")) or {}
    state = load_json(STATE_FILE, {"seen": {}})
    history = load_json(HISTORY_FILE, {})
    session = make_session()

    sites = config.get("sites", {})
    alerts = config.get("alerts", [])
    now_iso = datetime.now(timezone.utc).isoformat()
    total_fired = 0

    log(f"🏴‍☠️ OPCG Tracker — {len(sites)} site(s), {len(alerts)} alerte(s)")

    # ─── Étape 1 : scrape la page catégorie de chaque site UNE FOIS ───
    site_listings = {}
    for site_id, site in sites.items():
        if not site.get("enabled", True):
            continue
        if not site.get("category_url"):
            continue
        log(f"🌐 {site['name']}")
        listings = scrape_category(session, site)
        site_listings[site_id] = listings
        log(f"{len(listings)} produits", indent=1)
        time.sleep(random.uniform(*DELAY_RANGE))

    # Cache pour les références Cardmarket (plusieurs alertes peuvent pointer
    # vers la même URL CM, on ne fetch qu'une fois)
    cm_cache = {}
    def get_cm(ref):
        if not ref or not ref.get("url"):
            return None
        url = ref["url"]
        if url not in cm_cache:
            cm_cache[url] = fetch_cardmarket_ref(session, ref)
            time.sleep(random.uniform(*DELAY_RANGE))
        return cm_cache[url]

    # ─── Étape 2 : pour chaque alerte, matcher contre les listings ───
    for alert in alerts:
        if not alert.get("enabled", True):
            continue
        log(f"🔍 {alert['name']}")
        try:
            atype = alert.get("type")
            listings = []

            if atype == "cardmarket":
                lst = check_cardmarket(session, alert)
                if lst:
                    listings.append(lst)

            elif atype == "url":
                lst = check_url(session, alert["url"],
                                alert.get("site_name", "Site"))
                if lst:
                    listings.append(lst)

            elif atype == "rss":
                listings = check_rss(alert)

            elif atype == "news_page":
                listings = check_news_page(session, alert)

            elif alert.get("queries"):
                # Mode recherche legacy (par mots-clés sur search_url)
                target_sites = alert.get("sites") or list(sites.keys())
                for sid in target_sites:
                    site = sites.get(sid)
                    if not site or not site.get("search_url"):
                        continue
                    for q in alert["queries"]:
                        listings.extend(search_site(session, site, q))
                        time.sleep(random.uniform(*DELAY_RANGE))

            else:
                # Mode catégorie (par défaut) : on tape dans le cache des
                # listings déjà scrapés.
                target_sites = alert.get("sites") or list(site_listings.keys())
                for sid in target_sites:
                    listings.extend(site_listings.get(sid, []))

            cm_data = get_cm(alert.get("cardmarket_ref"))
            fired = process_alert(config, alert, listings, state, history,
                                  now_iso, get_cm_callable=get_cm,
                                  sites_config=sites)
            total_fired += fired
            log(f"✓ {len(listings)} candidats, {fired} notif(s)", indent=1)

        except Exception:
            log(f"❌ Erreur sur l'alerte: {alert['name']}")
            traceback.print_exc()

    # Cap historique
    for lid, h in history.items():
        h["prices"] = h["prices"][-50:]

    save_json(STATE_FILE, state)
    save_json(HISTORY_FILE, history)
    log(f"🏁 Terminé. {total_fired} notification(s) envoyée(s).")

if __name__ == "__main__":
    main()
