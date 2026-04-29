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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
TIMEOUT = 25
DELAY_RANGE = (2.0, 4.5)   # délai aléatoire entre requêtes (politesse)
MAX_RETRIES = 2
PRICE_DROP_THRESHOLD = 0.97  # notifie si le prix tombe sous 97% de l'ancien
DEFAULT_COOLDOWN_HOURS = 24  # ne pas re-notifier le même produit dans X heures

# ════ MARQUEURS DE STATUT — détection sur cartes produit en page catégorie ════

# Mots-clés qui indiquent une RUPTURE DE STOCK
OUT_OF_STOCK_MARKERS = [
    "rupture", "épuisé", "epuisé", "epuise", "épuise",
    "indisponible", "non disponible", "non-disponible",
    "out of stock", "sold out", "soldout", "sold-out",
    "plus en stock", "plus disponible", "non dispo",
    "vendu", "stock épuisé", "stock epuise",
    "produit épuisé", "produit epuise",
    "victim of its success", "more available",
]
# Classes CSS / data-attributes qui indiquent une rupture
# Plus exhaustif : on couvre Shopify, PrestaShop, WooCommerce, Wix
OUT_OF_STOCK_CSS_HINTS = [
    "out-of-stock", "outofstock", "out_of_stock",
    "sold-out", "soldout", "sold_out",
    "unavailable", "no-stock", "nostock",
    "product--sold-out", "product-sold-out",
    "is-sold-out", "is-out-of-stock",
    "data-sold-out", "data-out-of-stock",
    "btn-sold-out", "button--sold-out",
    "stock-out", "no-stock-availability",
]

# Mots-clés de PRÉCOMMANDE (le produit est suivi mais marqué "preorder")
PREORDER_MARKERS = [
    # Français
    "précommande", "precommande", "pré-commande", "pre-commande",
    "précommandez", "precommandez", "précommander", "precommander",
    # Anglais
    "preorder", "pre-order", "pre order",
    "pre-orders open", "preorders open",
    # Annonces de sortie / disponibilité future
    "à paraître", "a paraitre", "à venir", "a venir",
    "sortie le", "sortie prévue", "sortie prevue",
    "disponible le", "disponible sur", "disponible à partir",
    "dispo le", "dispo à partir",
    "available on", "available from",
    "release date", "release on", "releases on",
    "shipping", "ship date", "expédition à partir", "expedition a partir",
    # Réservation
    "réservation", "reservation", "réserver maintenant", "reserver maintenant",
    # Indicateurs de date future
    "livraison entre", "delivery between",
    "in stock from", "en stock à partir", "en stock a partir",
]
# Classes CSS / data-attributes de précommande
PREORDER_CSS_HINTS = [
    "preorder", "pre-order", "pre_order",
    "precommande", "pré-commande",
    "btn-preorder", "button--preorder",
    "data-preorder", "is-preorder",
    "product-preorder", "preorder-button",
    "countdown",  # Tofopolis & co. utilisent un compte à rebours pour les précos
]

# Sélecteurs CSS par plateforme — utilisés en fallback automatique quand un
# site ne définit pas explicitement ses selectors.
PLATFORM_SELECTORS = {
    "prestashop": {
        # Élargi : v1.6 utilise <li class="ajax_block_product">, v1.7+ utilise <article class="js-product-miniature">,
        # Philibert/Goupiya peuvent avoir des structures custom .product-thumbnail / .product-card
        "product": ("article.js-product-miniature, article.product-miniature, "
                    ".product-miniature, li.ajax_block_product, .product-thumbnail, "
                    ".product-card, li.product, .item-product"),
        "title": (".product-title a, .product-name a, h2.product-title, .product-title, "
                  ".product-name, h3.product-name a, a.product-name, .name a, h2 a, h3 a"),
        "link": (".product-title a, .product-name a, .thumbnail-container a, "
                 "a.product_img_link, a.product-name, .product-image a, "
                 "a.product-card__link, .name a, h2 a, h3 a"),
        "price": ("span.price, .product-price-and-shipping .price, .product-price, "
                  "[itemprop=price], .regular-price, .content_price .price, "
                  ".product-card__price, .price__current, span.amount"),
        "availability": (".product-availability, .product-flags, .out-of-stock, "
                         ".availability, .stock-info, .product-availability-list, "
                         ".product-flag, span.online_only"),
    },
    "shopify": {
        "product": (".product-card, .product-item, .grid__item, .grid-product, "
                    "[class*=ProductCard], .card-wrapper, .product-grid-item, "
                    ".collection-product-card, li.collection__products-item, "
                    ".grid__item--collection-template, .productitem"),
        "title": (".product-card__title, .product-item__title, .product-card__name, "
                  "h3, .product-title, .card__heading, .productitem--title, "
                  ".product-grid-item__title, .product-card__name, .product-name"),
        "link": ("a.product-card__link, a.product-item__image-wrapper, "
                 ".grid-product__link, .product-card__media, a.full-unstyled-link, "
                 ".productitem--image-link, .product-grid-item__link, a.card-link, "
                 ".product-card__link-wrapper, a"),
        "price": (".price__current, .price-item--regular, .product-card__price, "
                  ".price, .money, .price-item, [class*=price], .productitem--price, "
                  ".product-grid-item__price, .product-price"),
        "availability": (".badge--bottom-left, .product-card__sold-out, .sold-out, "
                         "[class*=sold-out], .product-label, .productitem--badge, "
                         ".price--sold-out, .stock-out"),
    },
    "woocommerce": {
        "product": ("li.product, .product, .wc-block-grid__product, "
                    ".product-grid .product, .products .product, ul.products li"),
        "title": (".woocommerce-loop-product__title, h2.woocommerce-loop-product__title, "
                  ".wc-block-grid__product-title, h2.product-title, h3.product-title"),
        "link": ("a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link, "
                 "a.wc-block-grid__product-link, .product-link, h2 a, h3 a"),
        "price": (".price ins .amount, .price > .amount, .price .amount, "
                  ".woocommerce-Price-amount, span.price, .product-price"),
        "availability": (".out-of-stock, .stock, .outofstock, .availability"),
    },
    # Fallback générique : tente de trouver tout produit qui ressemble à une fiche
    # commerciale. Très permissif, à utiliser en dernier recours.
    "generic": {
        "product": ("[class*=product-item], [class*=product-card], "
                    "[class*=ProductCard], [class*=product_item], li.product, "
                    "[itemtype*=Product], article[class*=product]"),
        "title": ("h2 a, h3 a, .title a, .name a, [itemprop=name], a[title]"),
        "link": ("a[href]"),
        "price": ("[class*=price], [itemprop=price], .amount, .money"),
        "availability": ("[class*=stock], [class*=availability], [class*=sold-out], "
                         "[class*=out-of-stock], [class*=epuise]"),
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
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cache-Control": "no-cache",
    })
    return s

def fetch(session, url, referer=None):
    """GET avec retry et backoff léger.
    Ajoute automatiquement un Referer (Google) pour réduire les 403 anti-bot."""
    last_err = None
    headers_extra = {}
    if referer:
        headers_extra["Referer"] = referer
    else:
        # Simule un clic depuis Google (les sites font moins de 403 sur ce trafic)
        headers_extra["Referer"] = "https://www.google.com/"

    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT, headers=headers_extra)
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

    # Stratégie 3 : fallback regex sur tout le texte du conteneur
    # Pour les sites sans classe CSS standard sur le prix (ex: Ultrajeux qui
    # met juste "<span>189,90 €</span>" sans classe). On capture le PREMIER
    # prix qui ressemble à NN,NN € ou NN.NN € ou € NN,NN.
    full_text = card.get_text(" ", strip=True)
    if full_text:
        # Patterns prix : "189,90 €", "189.90€", "€189,90", "1 234,56 €"
        # On cherche un nombre suivi (ou précédé) de € ou EUR
        m = re.search(r"(\d{1,4}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*€", full_text)
        if not m:
            m = re.search(r"€\s*(\d{1,4}(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,2})?)", full_text)
        if m:
            p = parse_price(m.group(0))
            if p:
                return p

    return None

def text_of(el):
    return el.get_text(" ", strip=True) if el else ""

def is_out_of_stock(card, availability_text=""):
    """Détecte si un produit est en rupture de stock.
    PRÉCOMMANDES = en stock (renvoie False) — la détection précommande est faite
    séparément dans is_preorder() et a la priorité dans scrape_category()."""
    # 1. Texte d'availability extrait
    if availability_text:
        low = availability_text.lower()
        for marker in OUT_OF_STOCK_MARKERS:
            if marker in low:
                return True

    # 2. Classes CSS de la carte et de ses descendants (pas que enfants directs)
    # Beaucoup de sites mettent le marqueur OOS plus profond dans le DOM
    card_html_low = str(card).lower()
    for hint in OUT_OF_STOCK_CSS_HINTS:
        if hint in card_html_low:
            return True

    # 3. Texte global de la carte (dernier recours)
    # On cherche les markers OOS dans le texte complet du conteneur. Cas typique :
    # Ultrajeux affiche juste "<strong>Indisponible</strong>" sans classe CSS.
    card_text = card.get_text(" ", strip=True).lower()
    # Avant tout : vérifier qu'on n'est PAS en précommande déguisée
    # (les précommandes contiennent souvent "indisponible" mais sont gérées
    # par is_preorder, pas ici)
    is_preorder_text = any(
        m in card_text for m in ["précommande", "precommande", "préco.", "preco.",
                                  "preorder", "pre-order", "à venir", "a venir"]
    )
    if is_preorder_text:
        return False

    for marker in OUT_OF_STOCK_MARKERS:
        if marker in card_text:
            return True

    return False

def is_preorder(card, availability_text="", title="", url=""):
    """Détecte si un produit est en précommande.
    Vérifie URL, cartes (texte + CSS), titre, et indicateurs de date future.
    Renvoie True si précommande détectée."""
    # 0. URL : si l'URL contient explicitement /precommande(s)/, c'est sûr
    # Cas Nippon TCG : /precommandes/one-piece-chopper-s-book...
    if url:
        url_low = url.lower()
        for url_marker in ["/precommande", "/precommandes", "/pre-commande",
                           "/preorder", "/pre-order", "/precommander"]:
            if url_marker in url_low:
                return True

    # 1. Texte d'availability
    if availability_text:
        low = availability_text.lower()
        for marker in PREORDER_MARKERS:
            if marker in low:
                return True

    # 2. Classes CSS / data-attributes (toute la carte, descendants inclus)
    card_html_low = str(card).lower()
    for hint in PREORDER_CSS_HINTS:
        if hint in card_html_low:
            return True

    # 3. Titre du produit
    if title:
        title_low = title.lower()
        for marker in ["précommande", "precommande", "preorder", "pre-order"]:
            if marker in title_low:
                return True

    # 4. Texte global de la carte (dernier filet de sécurité)
    # On évite les faux positifs en exigeant des phrases assez spécifiques
    card_text = card.get_text(" ", strip=True).lower()
    strong_preorder_phrases = [
        "ce produit est en précommande", "ce produit est en precommande",
        "précommandez maintenant", "precommandez maintenant",
        "this product is a pre-order", "this is a pre-order",
        "available for pre-order", "preorder now",
        "disponible sur :", "disponible sur:", "disponible le :", "disponible le:",
        "sortie le :", "sortie le:", "sortie prévue :", "sortie prevue :",
        "release date:", "release date :", "release on:", "release on :",
        "available on:", "available on :", "available from:", "available from :",
    ]
    for phrase in strong_preorder_phrases:
        if phrase in card_text:
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


# Mots-clés d'accessoires à EXCLURE complètement de la surveillance.
# Ces produits ne sont pas l'intérêt principal d'un acheteur de displays/boosters.
ACCESSORY_EXCLUDE_KEYWORDS = [
    # Tapis de jeu — toutes variantes FR/EN
    "tapis de jeu", "tapis officiel", "tapis officiels",
    "playmat", "play mat", "play-mat", "playmats",
    "playing mat", "rubber mat", "rubber play mat",
    # Pochettes / protège-cartes — traductions FR/EN, singulier ET pluriel
    "sleeve", "sleeves", "card sleeve", "card sleeves",
    "pochette de carte", "pochettes de carte", "pochettes de cartes",
    "pochette protege", "pochettes protege",
    "pochette protège", "pochettes protège",
    "protège-carte", "protège-cartes", "protege-carte", "protege-cartes",
    "protège carte", "protège cartes", "protege carte", "protege cartes",
    "protections de carte", "protections de cartes", "protection de carte",
    "protection de cartes",
    # Classeurs / range-cartes / porte-cartes
    "binder", "binders", "9-pocket binder", "9 pocket binder",
    "classeur", "classeurs", "classeur officiel",
    "range-cartes", "range cartes", "range-carte", "range carte",
    "porte-cartes", "porte cartes", "porte-carte", "porte carte",
    "card binder", "album de cartes", "album cartes",
    # Boîtes de rangement / deck box
    "card case", "card cases", "deck box", "deck-box", "deckbox",
    "deck case", "deck cases",
    "boite de rangement", "boîte de rangement", "boites de rangement",
    "boîtes de rangement", "storage box", "storage boxes", "rangement",
    # Goodies divers (pas le jeu lui-même)
    "card holder", "card holders", "présentoir", "presentoir",
    "tin box",  # /!\ "Mini-Tin" reste toléré (ce sont des produits jeu)
]


# Mots-clés supplémentaires pour le filtre des alertes (alerts.yaml)
# On exclut aussi les decks débutants/initiation par défaut.


def is_excluded_accessory(title):
    """Renvoie True si le titre indique un accessoire à exclure de la surveillance
    (playmat, sleeve, classeur, etc.)."""
    if not title:
        return False
    t = title.lower()
    for kw in ACCESSORY_EXCLUDE_KEYWORDS:
        if kw in t:
            return True
    return False

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
def _scrape_by_url_pattern(soup, base_url, site):
    """Fallback ultime : extrait les produits en cherchant les liens qui
    correspondent à des URLs de fiches produit, peu importe les classes CSS.

    Détecte automatiquement le pattern d'URL (Shopify /products/, WooCommerce
    /produit/ ou /product/, Prestashop /XXX-slug). Pour chaque lien trouvé,
    on remonte au conteneur parent qui inclut titre + prix + image."""
    from urllib.parse import urlparse

    # Normalisation : on travaille avec le path du domaine du site
    base_parsed = urlparse(base_url)
    base_host = base_parsed.netloc

    # Patterns de fiches produit dans l'URL (insensible à la casse)
    PATTERNS = [
        re.compile(r"/products/[a-z0-9_\-]+", re.I),       # Shopify
        re.compile(r"/produit/\d+/[a-z0-9_\-]+", re.I),     # Play-In FR
        re.compile(r"/produit/[a-z0-9_\-]+", re.I),         # WooCommerce FR
        re.compile(r"/product/[a-z0-9_\-]+", re.I),         # WooCommerce EN
        re.compile(r"/produit-\d+-[a-z0-9_\-]+", re.I),     # Prestashop custom (Ultrajeux)
        re.compile(r"/fr/[^/]+/\d+-[a-z0-9_\-]+", re.I),    # Prestashop FR (Philibert)
        re.compile(r"/[a-z]{2}/\d+-[a-z0-9_\-]+", re.I),    # Prestashop multilingue
        re.compile(r"^/\d+-[a-z0-9_\-]+\.html?$", re.I),    # Prestashop simple
        re.compile(r"\.html?$.*[a-z0-9_\-]+\.html?$", re.I), # autres .html (Mystic Ambre, etc.)
    ]

    # ━━━ Restreindre la zone de recherche au contenu principal ━━━
    # Beaucoup de pages ont des liens produits dans le menu/sidebar/footer
    # qui ne sont PAS dans la catégorie en cours (ex : "best-sellers", "à voir
    # aussi", "menu nav"). On cherche d'abord dans une zone de contenu
    # principal, et on retombe sur soup entier si rien n'est trouvé.
    main_zones = []
    for sel in ["main", "[role=main]", "section.products", "section.product-list",
                ".collection", ".collection__products", "div.products",
                "#main-content", "#content", "#products", ".category-products",
                "[class*=collection-grid]", "[class*=product-list]",
                "[class*=products-grid]"]:
        try:
            zones = soup.select(sel)
        except Exception:
            continue
        for z in zones:
            # On ignore les zones très petites (probablement pas des listes produits)
            text_len = len(z.get_text(strip=True))
            if text_len > 50:
                main_zones.append(z)
    # Si on a au moins 1 zone de contenu, on cherche dans ces zones
    # (la plus grande). Sinon, on cherche dans tout le document.
    search_root = soup
    if main_zones:
        # On prend la zone qui contient le plus de liens produits matchant les
        # patterns (ça filtre les sidebars qui ont aussi quelques produits)
        best_zone, best_count = None, 0
        for zone in main_zones:
            count = sum(
                1 for a in zone.find_all("a", href=True)
                if any(p.search(urlparse(urljoin(base_url, a.get("href", ""))).path)
                       for p in PATTERNS)
            )
            if count > best_count:
                best_count = count
                best_zone = zone
        if best_zone and best_count >= 3:
            search_root = best_zone

    # Collecte des liens candidats (uniques par URL) — quand 2 <a> pointent
    # vers la même URL (cas fréquent : un <a> autour de l'image + un <a>
    # autour du titre), on garde celui qui a le titre le plus pertinent.
    candidates = {}  # url → balise <a>
    for a in search_root.find_all("a", href=True):
        href = a.get("href", "")
        if not href:
            continue
        # On ignore les ancres et les javascript:
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        # Normalise le path
        try:
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            # Ne garde que les liens du même domaine
            if parsed.netloc and parsed.netloc != base_host:
                continue
            path = parsed.path
        except Exception:
            continue
        # Vérifie si le path matche un pattern produit
        if not any(p.search(path) for p in PATTERNS):
            continue
        # Anti-doublon par URL canonique (sans query string ni fragment)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{path}" if parsed.netloc else full

        # Si déjà vu : on remplace seulement si le nouveau <a> a un titre meilleur
        if clean_url in candidates:
            existing = candidates[clean_url]
            new_text = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
            old_text = (existing.get("title") or existing.get_text(" ", strip=True) or "").strip()
            # On garde le nouveau s'il a un texte ET (l'ancien n'en a pas OU le nouveau est plus long)
            if new_text and (not old_text or len(new_text) > len(old_text)):
                candidates[clean_url] = a
        else:
            candidates[clean_url] = a

    # ━━━ Filtre One Piece : ne garder que les URLs qui ressemblent à des
    # produits One Piece. Cela évite de capturer les liens du menu ou des
    # widgets "à voir aussi" qui pointent vers d'autres TCG.
    OP_URL_HINTS = [
        "one-piece", "onepiece", "one_piece",
        "op-01", "op-02", "op-03", "op-04", "op-05", "op-06", "op-07",
        "op-08", "op-09", "op-10", "op-11", "op-12", "op-13", "op-14",
        "op-15", "op-16", "op-17", "op-18", "op-19", "op-20",
        "op01", "op02", "op03", "op04", "op05", "op06", "op07", "op08",
        "op09", "op10", "op11", "op12", "op13", "op14", "op15", "op16",
        "op17", "op18", "op19", "op20",
        "eb-01", "eb-02", "eb-03", "eb-04", "eb-05",
        "eb01", "eb02", "eb03", "eb04", "eb05",
        "prb-01", "prb-02", "prb01", "prb02",
        "df-01", "df-02", "df-03", "df01", "df02", "df03",
        "luffy", "zoro", "nami", "sanji", "chopper", "robin",
        "ace", "shanks", "kaido", "newgate",
        "premium-card-collection", "ichiban-kuji",
        "memorial-collection", "kami-s-island", "kami-island",
    ]

    # Filtre les candidats : on garde ceux dont l'URL contient un indice OP
    op_candidates = {}
    for url, a in candidates.items():
        url_low = url.lower()
        if any(hint in url_low for hint in OP_URL_HINTS):
            op_candidates[url] = a
    # Si on a trouvé au moins 3 candidats OP, on ne garde QUE ceux-là.
    # Sinon, on garde tous les candidats (le site n'a peut-être pas le
    # mot-clé OP dans l'URL même pour les produits OP).
    if len(op_candidates) >= 3:
        candidates = op_candidates

    if len(candidates) < 3:
        return []

    # Extraction : pour chaque lien, remonter au plus petit conteneur "produit"
    # (li, article, ou div qui contient image + texte + prix éventuel)
    results = []
    seen_titles = set()
    for full_url, anchor in list(candidates.items())[:60]:
        # Trouve le conteneur parent : li, article, ou div significatif.
        # Critère d'arrêt : le conteneur ne doit PAS contenir plusieurs
        # liens distincts vers d'autres fiches produit (sinon on englobe
        # plusieurs cards = faux positif statut/prix sur le mauvais produit)
        container = anchor
        previous = anchor
        for _ in range(8):  # max 8 remontées
            parent = container.parent
            if not parent or parent.name in ("body", "html", None):
                break

            # Compter combien de liens produits DIFFÉRENTS sont dans ce parent
            distinct_product_urls = set()
            for a in parent.find_all("a", href=True):
                ah = a.get("href", "")
                try:
                    p_full = urljoin(base_url, ah)
                    p_path = urlparse(p_full).path
                except Exception:
                    continue
                if any(p.search(p_path) for p in PATTERNS):
                    p_clean = f"{urlparse(p_full).scheme}://{urlparse(p_full).netloc}{p_path}"
                    distinct_product_urls.add(p_clean)

            if len(distinct_product_urls) > 1:
                # Trop large : on a englobé plusieurs produits
                # On revient au container précédent (qui n'avait qu'un seul produit)
                break

            previous = container
            container = parent

            # Critère d'arrêt positif : on est dans un li ou article
            if container.name in ("li", "article"):
                break

        # Extraction du titre : title attribute du <a> > texte du <a> > h1/h2/h3 dans container
        title = (anchor.get("title") or "").strip()
        if not title:
            title = anchor.get_text(" ", strip=True)
        if not title or len(title) < 3:
            for h in container.find_all(["h1", "h2", "h3", "h4"]):
                t = h.get_text(" ", strip=True)
                if t and len(t) > 3:
                    title = t
                    break
        if not title:
            # Texte alt de l'image
            img = container.find("img")
            if img and img.get("alt"):
                title = img.get("alt").strip()

        if not title or len(title) < 3:
            continue
        # Limite la longueur (évite les blocs descriptifs)
        if len(title) > 250:
            title = title[:250].rsplit(" ", 1)[0]

        # Anti-doublon par titre normalisé
        title_key = re.sub(r"\s+", " ", title.lower()).strip()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)

        # Extraction prix (cherche dans le container)
        price = extract_price(container, "[class*=price], [itemprop=price], .amount, .money, span.price")

        # Texte de disponibilité (rupture / précommande)
        avail_el = container.select_one(
            "[class*=stock], [class*=sold-out], [class*=out-of-stock], "
            "[class*=epuise], [class*=availability], [class*=preorder], "
            "[class*=precommande]"
        )
        avail = text_of(avail_el) if avail_el else ""

        # Détection statut
        is_pre = is_preorder(container, avail, title, url=full_url)
        is_oos = is_out_of_stock(container, avail)
        if is_pre:
            status = "preorder"
        elif is_oos:
            status = "out"
        else:
            status = "in"
        # Exclusion accessoires
        if is_excluded_accessory(title):
            continue

        ptype = detect_product_type(title)
        results.append({
            "title": title,
            "url": full_url,
            "price": price,
            "availability": avail,
            "site": site.get("name", ""),
            "in_stock": status == "in",
            "out_of_stock": status == "out",
            "is_oos": status == "out",
            "status": status,
            "product_type": ptype,
        })

    return results


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
            # Auto-détection : essaie chaque plateforme dans l'ordre
            # On choisit celle qui retourne le PLUS de produits (≥ 3 minimum
            # pour éviter de matcher 1-2 éléments parasites comme un menu)
            best_platform = None
            best_count = 0
            for p in ["prestashop", "shopify", "woocommerce"]:
                test = PLATFORM_SELECTORS[p]["product"]
                count = len(soup.select(test))
                if count >= 3 and count > best_count:
                    best_count = count
                    best_platform = p
            if best_platform:
                sel = {**PLATFORM_SELECTORS[best_platform], **sel}
                site["_detected_platform"] = best_platform
            else:
                # Dernier recours : fallback générique
                generic_count = len(soup.select(PLATFORM_SELECTORS["generic"]["product"]))
                if generic_count >= 3:
                    sel = {**PLATFORM_SELECTORS["generic"], **sel}
                    site["_detected_platform"] = "generic"

    product_sel = sel.get("product")
    if not product_sel or not soup.select(product_sel):
        # ━━━ Fallback ultime : extraction par URLs distinctives ━━━
        # Shopify : /products/, WooCommerce : /produit/, /product/
        # Prestashop : URL avec ID-slug en path
        results = _scrape_by_url_pattern(soup, url, site)
        if results:
            site["_detected_platform"] = "url-pattern"
            log(f"  ↳ {site['name']}: extraction par URL pattern ({len(results)} candidat(s))", indent=2)
            return results
        log(f"⚠️  {site['name']}: aucun produit détecté ({url})", indent=2)
        return []

    results = []
    skipped_oos = 0
    skipped_accessory = 0
    for card in soup.select(product_sel)[:60]:
        title = text_of(card.select_one(sel.get("title", "h2, h3, .title, a")))
        link_el = card.select_one(sel.get("link", "a[href]"))
        href = link_el.get("href") if link_el else None
        price = extract_price(card, sel.get("price", ".price"))
        avail = text_of(card.select_one(sel.get("availability"))) if sel.get("availability") else ""
        if not (title and href):
            continue
        # Exclusion des accessoires (playmats, sleeves, classeurs, etc.)
        # Ces produits ne sont pas l'intérêt principal du tracker.
        if is_excluded_accessory(title):
            skipped_accessory += 1
            continue
        # Calcul du statut avec priorité : preorder > out > in
        # Un produit en précommande peut avoir un bouton "Épuisé" sur la liste
        # (parce qu'il n'est pas encore dispo immédiatement), mais le badge
        # "Précommande" ou "Disponible le X" doit primer.
        is_pre = is_preorder(card, avail, title, url=href or "")
        is_oos = is_out_of_stock(card, avail)
        if is_pre:
            status = "preorder"
            oos = False  # is_oos legacy : précommande ≠ rupture
        elif is_oos:
            status = "out"
            oos = True
            skipped_oos += 1
        else:
            status = "in"
            oos = False
        ptype = detect_product_type(title)
        results.append({
            "title": title,
            "url": urljoin(url, href),
            "price": price,
            "site": site["name"],
            "availability": avail,
            "is_oos": oos,            # legacy : encore utilisé par les transitions
            "status": status,         # in / preorder / out
            "product_type": ptype,    # display / booster / starter / box / case / other
        })
    if skipped_oos:
        log(f"({skipped_oos} produits en rupture détectés et suivis pour transitions)", indent=2)
    if skipped_accessory:
        log(f"({skipped_accessory} accessoires exclus : playmats/sleeves/classeurs)", indent=2)

    # ━━━ Stratégie hybride : on essaie aussi le fallback URL-pattern et on
    # garde les résultats les plus nombreux. Cela permet de fixer les sites
    # où nos sélecteurs CSS matchent du parasite (sous-éléments d'une vraie
    # card), mais où l'extraction par URL distinctive trouve les vrais
    # produits. Compromis pour la stabilité : on n'écrase les résultats CSS
    # que si l'URL-pattern en trouve nettement plus (>1.5x plus).
    url_pattern_results = _scrape_by_url_pattern(soup, url, site)
    if url_pattern_results and len(url_pattern_results) > len(results) * 1.5:
        site["_detected_platform"] = "url-pattern"
        log(f"  ↳ {site['name']}: extraction par URL pattern ({len(url_pattern_results)} candidat(s), au lieu de {len(results)} via CSS)", indent=2)
        return url_pattern_results

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

def _telegram_escape_md(text):
    """Échappe les caractères Markdown spéciaux pour Telegram (mode legacy 'Markdown').
    Telegram interprète *, _, `, [ et certains autres comme balises actives.
    On préfixe avec un backslash pour les neutraliser."""
    if not text:
        return text
    # Caractères critiques en Markdown legacy Telegram
    for char in ['_', '*', '`', '[']:
        text = text.replace(char, '\\' + char)
    return text

def notify_telegram(token, chat_id, text, thread_id=None):
    """Envoie un message Telegram avec gestion d'erreurs visible dans les logs.
    Tente d'abord en Markdown ; si Telegram refuse à cause d'une syntaxe
    Markdown invalide, retente en mode plain text (sans formatage).
    Si thread_id est fourni, le message est envoyé dans ce topic du supergroupe."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _build_payload(content, parse_mode=None):
        p = {"chat_id": str(chat_id), "text": content}
        if parse_mode:
            p["parse_mode"] = parse_mode
        if thread_id:
            p["message_thread_id"] = int(thread_id)
        return p

    # Tentative 1 : Markdown (avec échappement des caractères critiques)
    safe_text = _telegram_escape_md(text)
    try:
        r = requests.post(url, json=_build_payload(safe_text, "Markdown"), timeout=10)
        if r.status_code == 200:
            return  # OK
        log(f"⚠️  telegram Markdown failed ({r.status_code}): {r.text[:200]}", indent=2)
    except Exception as ex:
        log(f"⚠️  telegram exception (Markdown): {ex}", indent=2)

    # Tentative 2 : plain text sans parse_mode
    try:
        r = requests.post(url, json=_build_payload(text), timeout=10)
        if r.status_code == 200:
            log(f"✓ telegram envoyé en plain text", indent=2)
            return
        log(f"❌ telegram plain failed ({r.status_code}): {r.text[:200]}", indent=2)
    except Exception as ex:
        log(f"❌ telegram exception (plain): {ex}", indent=2)

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
        # Routage par topic selon le type de notification
        # - back_in_stock → Topic "Retour en stock" (priorité critique, séparé)
        # - new / price_drop → Topic "Notifs produits"
        if kind == "back_in_stock":
            tg_thread = env_or(n, "telegram_topic_restocks")
        else:
            tg_thread = env_or(n, "telegram_topic_products")
        md = f"*{title}*\n\n{body}\n\n[👉 Ouvrir]({listing['url']})"
        notify_telegram(tg_token, tg_chat, md, thread_id=tg_thread)

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
        # Skip si site désactivé (enabled: false ou disabled: true)
        if not site.get("enabled", True):
            log(f"⏸️  {site.get('name', site_id)} (désactivé)")
            continue
        if site.get("disabled"):
            log(f"⏸️  {site.get('name', site_id)} (désactivé : {site.get('disabled_reason', 'manuel')})")
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
