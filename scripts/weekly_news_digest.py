"""Envoie le digest hebdomadaire des annonces officielles One Piece TCG sur Telegram.
- Lit news_history.json
- Garde les annonces des 7 derniers jours
- Traduit le japonais en français via deep-translator (Google gratuit)
- Envoie un message header + un message photo par annonce
"""
from __future__ import annotations
import json
import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# Import du collector pour récupérer les paramètres communs
sys.path.insert(0, str(Path(__file__).parent))
from news_collector import (
    HISTORY_FILE, fetch_html, log
)

DIGEST_WINDOW_DAYS = 80  # Annonces des 80 derniers jours (cycle complet précommande → sortie)

# ─────────────────── Telegram ───────────────────
def tg_send_message(token: str, chat_id: str, text: str, parse_mode: str = None,
                    thread_id: int = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log(f"[DEBUG sendMessage HTTP {r.status_code}] body={r.text[:300]}", indent=2)
        return r.json() if r.text else {}
    except Exception as ex:
        log(f"[DEBUG sendMessage exception] {ex}", indent=2)
        return {"ok": False, "description": str(ex)}


def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str = None,
                  parse_mode: str = None, thread_id: int = None) -> dict:
    """Envoie une photo via URL distante (Telegram télécharge l'image)."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {"chat_id": str(chat_id), "photo": photo_url}
    if caption:
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        payload["caption"] = caption
        if parse_mode:
            payload["parse_mode"] = parse_mode
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log(f"[DEBUG sendPhoto HTTP {r.status_code}] body={r.text[:300]}", indent=2)
        return r.json() if r.text else {}
    except Exception as ex:
        log(f"[DEBUG sendPhoto exception] {ex}", indent=2)
        return {"ok": False, "description": str(ex)}


# ─────────────────── Traduction ───────────────────
_TRANSLATOR = None

def get_translator():
    """Lazy-load deep-translator."""
    global _TRANSLATOR
    if _TRANSLATOR is None:
        try:
            from deep_translator import GoogleTranslator
            _TRANSLATOR = GoogleTranslator(source="auto", target="fr")
        except ImportError:
            log("⚠️  deep-translator non installé — pas de traduction du JP", indent=1)
            _TRANSLATOR = False
    return _TRANSLATOR


def translate_to_fr(text: str, source_lang: str = "auto") -> str:
    """Traduit en français. Renvoie le texte original en cas d'échec."""
    if not text or not text.strip():
        return text
    translator = get_translator()
    if not translator:
        return text
    try:
        return translator.translate(text)
    except Exception as ex:
        log(f"⚠️  traduction échouée : {ex}", indent=2)
        return text


# ─────────────────── Sélection / formatage ───────────────────
_DATE_ONLY_PATTERNS = [
    re.compile(r"^\d{1,2}\s+\w+\s+\d{4}$", re.UNICODE),  # "19 mars 2026"
    re.compile(r"^[A-Za-z]+\s+\d{1,2},?\s+\d{4}$"),       # "March 19, 2026"
    re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$"),           # "2026-03-19"
    re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"),               # "2026年3月19日"
]

def _is_valid_title(title: str) -> bool:
    """Rejette les titres dégénérés (juste une date, vide, trop court)."""
    if not title or len(title.strip()) < 8:
        return False
    t = title.strip()
    for pat in _DATE_ONLY_PATTERNS:
        if pat.match(t):
            return False
    return True


def filter_recent(items: dict, days: int = DIGEST_WINDOW_DAYS) -> list[dict]:
    """Garde les annonces dont la published_date est dans la fenêtre.
    Filtre aussi les titres dégénérés (juste une date, parsing raté).
    Si pas de published_date, on ne la retient PAS (trop risqué de remonter
    des vieilles annonces qu'on découvre tardivement)."""
    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=days)
    selected = []
    skipped_bad_title = 0
    for key, item in items.items():
        pub_str = item.get("published_date")
        if not pub_str:
            continue
        try:
            pub_date = datetime.strptime(pub_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if pub_date < cutoff_date:
            continue
        if not _is_valid_title(item.get("title", "")):
            skipped_bad_title += 1
            continue
        selected.append(item)
    if skipped_bad_title:
        log(f"⏭️  {skipped_bad_title} annonce(s) ignorée(s) (titre dégénéré)", indent=1)
    selected.sort(key=lambda it: it.get("published_date", ""), reverse=True)
    return selected


# Phrases qu\'on rejette comme étant la description générique du site (pas du produit)
GENERIC_DESC_MARKERS = [
    "site officiel du jeu de cartes one piece",
    "the official one piece card game website",
    "official site for the popular trading card game",
    "site officiel",
    "embarquez pour une nouvelle ère",
    "set sail for the new era",
    "find out about the latest cards",
    "retrouvez toutes les informations du jeu",
]

def _is_generic_text(text: str) -> bool:
    """Détecte si un texte est une description générique du site, pas du produit."""
    if not text:
        return True
    low = text.lower()
    return any(marker in low for marker in GENERIC_DESC_MARKERS)


def _is_generic_image(url: str) -> bool:
    """Détecte si une URL d\'image est un placeholder générique du site."""
    if not url:
        return True
    low = url.lower()
    bad_patterns = [
        "img_thumbnail",      # placeholder Bandai
        "logo_op",            # logo générique
        "no-image",
        "noimage",
        "placeholder",
        "default",
        "common/",            # icônes communes
        "/footer_",           # icônes footer
        "ico_",               # icônes
    ]
    return any(p in low for p in bad_patterns)


def fetch_article_data(url: str, lang: str, max_chars: int = 280) -> tuple[str, str, str]:
    """Récupère (title, description, image_url) depuis une fiche article/produit.
    Renvoie ("", "", "") si rien d'utilisable.

    Le titre est cherché dans (par priorité) : og:title -> <h1> -> <h2> dans le contenu
    principal -> balise <title> nettoyée du suffixe site."""
    html = fetch_html(url, timeout=10)
    if not html:
        return "", "", ""
    soup = BeautifulSoup(html, "lxml")

    # ── TITRE ──
    title = ""

    # Priorité 1 : og:title
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content"):
        candidate = og_title["content"].strip()
        if candidate:
            title = _clean_page_title(candidate)

    # Priorité 2 : <h1> (mais pas si c'est le logo "ONE PIECE CARD GAME")
    if not title:
        h1 = soup.find("h1")
        if h1:
            t = h1.get_text(" ", strip=True)
            if t and len(t) > 5 and not _looks_like_logo_title(t):
                title = t

    # Priorité 3 : premier <h2> dans <main>/<article> (souvent le nom du produit)
    if not title:
        for container_sel in ["main", "article", ".product-info", "#main"]:
            try:
                container = soup.select_one(container_sel)
            except Exception:
                container = None
            if container:
                h2 = container.find("h2")
                if h2:
                    t = h2.get_text(" ", strip=True)
                    if t and len(t) > 5:
                        title = t
                        break

    # Priorité 4 : <title> du document, nettoyé
    if not title:
        page_title = soup.find("title")
        if page_title:
            t = _clean_page_title(page_title.get_text(strip=True))
            if t and len(t) > 5:
                title = t

    if title and len(title) > 200:
        title = title[:200].rsplit(" ", 1)[0] + "…"

    # ── DESCRIPTION ──
    description = ""

    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        content = og["content"].strip()
        if content and not _is_generic_text(content) and len(content) > 30:
            description = content[:max_chars]

    if not description:
        meta = soup.find("meta", attrs={"name": "description"})
        if meta and meta.get("content"):
            content = meta["content"].strip()
            if content and not _is_generic_text(content) and len(content) > 30:
                description = content[:max_chars]

    if not description:
        candidates = []
        for selector in ["article", "main", "[role=\"main\"]", "#main", ".content",
                          ".article-body", ".product-info", ".product-detail",
                          ".news-content", ".topics-content"]:
            try:
                el = soup.select_one(selector)
                if el:
                    candidates.append(el)
            except Exception:
                continue
        if not candidates:
            candidates = [soup]
        for container in candidates:
            for p in container.find_all(["p", "h3"]):
                text = p.get_text(" ", strip=True)
                if not text or len(text) < 50:
                    continue
                if "©" in text or "cookie" in text.lower():
                    continue
                if _is_generic_text(text):
                    continue
                description = text[:max_chars]
                break
            if description:
                break

    # ── IMAGE ──
    image_url = ""

    # Priorité 1 : photo officielle du produit (img_item01.webp / .png est le standard Bandai)
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        if any(pat in src.lower() for pat in ["img_item01", "img_item02", "img_item03"]):
            image_url = urljoin(url, src)
            break

    # Priorité 2 : og:image
    if not image_url:
        og_img = soup.find("meta", attrs={"property": "og:image"})
        if og_img and og_img.get("content"):
            candidate = og_img["content"].strip()
            if candidate and not _is_generic_image(candidate):
                image_url = urljoin(url, candidate)

    # Priorité 3 : grosse image dans le contenu (fallback)
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if not src or _is_generic_image(src):
                continue
            try:
                w = int(img.get("width", "0") or "0")
                h = int(img.get("height", "0") or "0")
                if w and h and (w < 200 or h < 200):
                    continue
            except ValueError:
                pass
            image_url = urljoin(url, src)
            break

    return title, description, image_url


def _clean_page_title(raw: str) -> str:
    """Nettoie un titre de page Bandai en retirant les suffixes du site.
    Ex: 'XYZ - PRODUITS|ONE PIECE CARD GAME - Site Officiel' -> 'XYZ'."""
    if not raw:
        return ""
    t = raw.strip()
    # Coupe au premier separateur connu de fin de titre
    for sep in ["｜", " | ", " − ", " - ONE PIECE", "│", " — ", " ｜"]:
        idx = t.find(sep)
        if idx > 5:  # garde au moins 6 chars de titre
            t = t[:idx].strip()
    return t


def _looks_like_logo_title(text: str) -> bool:
    """Détecte un h1 qui est juste 'ONE PIECE CARD GAME' (logo en h1)."""
    if not text:
        return True
    low = text.lower().strip()
    return low in {"one piece card game", "one piece tcg", "ワンピースカードゲーム"}


def _looks_like_url_segment(text: str) -> bool:
    """Détecte un titre qui ressemble à un segment d'URL technique :
    'Sleeve033', 'Playmat010', 'Cardcollection Bestselection Vol6', etc.
    Critère : au moins 1 mot avec des chiffres collés à des lettres ET pas d'espace
    avant les chiffres → ressemble à un identifiant technique."""
    if not text:
        return False
    t = text.strip()
    # Si déjà long et contenant des espaces normaux, c'est sans doute un vrai titre
    if len(t) > 60:
        return False
    # Pattern type "Sleeve033", "Playmat010" : un mot suivi de chiffres collés
    import re as _re
    # Mot avec lettres + chiffres collés au bout (pas d'espace avant les chiffres)
    if _re.match(r"^[A-Z][a-z]+\d+$", t):
        return True
    # Pattern type "Cardcollection Bestselection Vol6" : capitalisation chelou de segments URL
    # = mots accolés en CamelCase/lower, peu d'espaces, ressemble à du nom-de-fichier
    words = t.split()
    if len(words) <= 4 and all(_re.match(r"^[A-Z][a-z]+\d*$", w) for w in words):
        return True
    return False


def _looks_like_just_a_date(text: str) -> bool:
    """Vrai si le titre est juste une date ('19 mars 2026', 'March 19, 2026')."""
    if not text:
        return True
    t = text.strip()
    import re as _re
    patterns = [
        r"^\d{1,2}\s+\w+\s+\d{4}$",
        r"^[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}$",
        r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$",
        r"^\d{4}年\d{1,2}月\d{1,2}日$",
    ]
    return any(_re.match(p, t) for p in patterns)


_TITLE_STOP_WORDS = {
    # Mots vides FR/EN qu'on ignore pour le calcul de similarité
    "the", "and", "of", "for", "to", "in", "on", "at", "is", "a", "an",
    "le", "la", "les", "de", "du", "des", "et", "ou", "un", "une",
    "vol", "edition", "card", "cards", "products", "produits", "official",
    "one", "piece", "tcg", "game", "carte", "cartes", "boutiques",
    "exclusive", "exclusif", "shops",
}


def _title_keywords(title: str) -> set:
    """Extrait les mots significatifs d'un titre."""
    if not title:
        return set()
    import re as _re
    words = _re.findall(r"[a-z0-9àâäéèêëïîôöùûüÿç]+", title.lower())
    return {w for w in words if len(w) >= 3 and w not in _TITLE_STOP_WORDS}


def _title_similarity(t1: str, t2: str) -> float:
    """Jaccard similarité entre les mots-clés de 2 titres."""
    k1, k2 = _title_keywords(t1), _title_keywords(t2)
    if not k1 or not k2:
        return 0.0
    return len(k1 & k2) / len(k1 | k2)


def fuse_similar_titles(items: list, min_similarity: float = 0.5) -> list:
    """Re-fusionne les items dont les vrais titres ('_resolved_title') sont
    similaires ET qui ont la même date de publication.

    Cas typique : Playmat annoncé le 16/02/2026 sur 2 pages différentes (FR/EN)
    avec des paths URL différents mais le même nom commercial."""
    LANG_PRIORITY = {"FR": 3, "EN": 2, "JP": 1}
    if not items:
        return items

    groups = []
    for item in items:
        item_title = item.get("_resolved_title") or item.get("title", "")
        item_date = item.get("published_date", "")

        # Skip si titre dégénéré ou vide
        if not item_title or _looks_like_just_a_date(item_title):
            groups.append([item])
            continue

        matched_group = None
        for g in groups:
            for existing in g:
                ex_title = existing.get("_resolved_title") or existing.get("title", "")
                ex_date = existing.get("published_date", "")
                if not ex_date or not item_date or ex_date != item_date:
                    continue
                if _looks_like_just_a_date(ex_title):
                    continue
                sim = _title_similarity(item_title, ex_title)
                if sim >= min_similarity:
                    matched_group = g
                    break
            if matched_group:
                break

        if matched_group:
            matched_group.append(item)
        else:
            groups.append([item])

    # Reconstruction
    merged = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        group.sort(key=lambda x: LANG_PRIORITY.get(x.get("lang", ""), 0), reverse=True)
        primary = dict(group[0])
        existing_sources = list(primary.get("sources", []))
        existing_langs = {s.get("lang") for s in existing_sources}
        for g in group[1:]:
            for src in g.get("sources", [{
                "lang": g.get("lang"),
                "label": g.get("source_label"),
                "name": g.get("source_name"),
                "url": g.get("url"),
                "published_date": g.get("published_date"),
            }]):
                if src.get("lang") not in existing_langs:
                    existing_sources.append(src)
                    existing_langs.add(src.get("lang"))
        primary["sources"] = existing_sources
        merged.append(primary)
    return merged


# Compatibilité descendante (renvoie juste la description)
def fetch_excerpt(url: str, lang: str, max_chars: int = 280) -> str:
    _, description, _ = fetch_article_data(url, lang, max_chars)
    return description




def format_digest_header(items: list[dict]) -> str:
    """Message d'introduction du digest (en Markdown)."""
    if not items:
        return ""
    by_lang = {}
    for it in items:
        lang = it.get("lang", "??")
        by_lang[lang] = by_lang.get(lang, 0) + 1
    today = datetime.now()
    week_start = today - timedelta(days=DIGEST_WINDOW_DAYS - 1)
    lines = [
        "🎴 ━━━━━━━━━━━━━━━━━━━",
        "   *ONE PIECE TCG NEWS*",
        "   Récap de la semaine",
        "━━━━━━━━━━━━━━━━━━━",
        "",
        f"📅 Semaine du {week_start.strftime('%d/%m')} → {today.strftime('%d/%m/%Y')}",
        f"📰 *{len(items)} annonce{'s' if len(items) > 1 else ''}* cette semaine",
        "",
    ]
    flag_lines = []
    for lang in ["FR", "EN", "JP"]:
        if lang in by_lang:
            flag = {"FR": "🇫🇷", "EN": "🇺🇸", "JP": "🇯🇵"}[lang]
            flag_lines.append(f"{flag} {by_lang[lang]}")
    if flag_lines:
        lines.append(" · ".join(flag_lines))
    return "\n".join(lines)


def format_announcement(item: dict) -> tuple[str, Optional[str]]:
    """Renvoie (caption_markdown, image_url) pour Telegram sendPhoto.
    Traduit titre + résumé du JP vers FR. FR/EN restent natifs."""
    raw_title = item.get("title", "(sans titre)")
    lang = item.get("lang", "??")
    is_jp = lang == "JP"

    # Récupère titre + résumé + image (depuis le cache si déjà résolu, sinon fetch)
    primary_url = item.get("url", "")
    detail_title = item.get("_resolved_title", "")
    summary = item.get("_resolved_description", "")
    better_image = item.get("_resolved_image", "")
    if not (detail_title or summary or better_image):
        detail_title, summary, better_image = fetch_article_data(primary_url, lang)

    # Logging détaillé pour debug
    log(f"      📄 detail_title='{detail_title[:60]}' (raw='{raw_title[:40]}')", indent=1)
    log(f"      🖼️  better_image={'OUI' if better_image else 'NON'}  (orig={'OUI' if item.get('image_url') else 'NON'})", indent=1)

    # On préfère le titre de la fiche détaillée s'il est plus informatif
    # (le titre de la liste tombe parfois sur "Sleeve033" ou une date)
    override_title = False
    if detail_title and (
        len(detail_title) > len(raw_title) + 5
        or _looks_like_url_segment(raw_title)
        or _looks_like_just_a_date(raw_title)
    ):
        raw_title = detail_title
        override_title = True

    log(f"      ✏️  title_override={'OUI' if override_title else 'NON'}  -> '{raw_title[:60]}'", indent=1)

    # Traduit si JP
    title = translate_to_fr(raw_title) if is_jp else raw_title
    if is_jp and summary:
        summary = translate_to_fr(summary)

    # On override l'image initiale si la fiche détaillée a une meilleure image
    if better_image:
        item = dict(item)
        item["image_url"] = better_image

    # Émoji de catégorie
    cat = item.get("category", "")
    if cat in ("PRODUITS", "PRODUCTS", "商品"):
        cat_icon = "🆕 *NOUVEAU PRODUIT*"
    elif cat in ("CARTES", "CARDS", "カード"):
        cat_icon = "🃏 *REVEAL DE CARTES*"
    else:
        cat_icon = "📢 *ANNONCE*"

    # Code de set s'il existe
    set_badge = f" · `{item['set_code']}`" if item.get("set_code") else ""

    # Date publiée
    pub = item.get("published_date") or item.get("first_seen", "")[:10]
    date_str = ""
    if pub:
        try:
            d = datetime.strptime(pub, "%Y-%m-%d")
            date_str = d.strftime("%d/%m/%Y")
        except ValueError:
            date_str = pub[:10]

    # Sources avec drapeaux et liens
    sources = item.get("sources", [])
    if not sources:
        sources = [{
            "lang": item.get("lang"), "label": item.get("source_label"),
            "url": item.get("url"),
        }]
    flags = " → ".join(s.get("label", "") for s in sources if s.get("label"))

    # Construction de la caption
    parts = [f"{cat_icon}{set_badge}", "", f"*{escape_md(title)}*"]
    if date_str:
        parts.append(f"📅 {date_str}")
    if flags:
        parts.append(f"🌐 Sources : {flags}")
    if summary:
        parts.append("")
        parts.append(f"📝 _{escape_md(summary[:280])}_")
    parts.append("")
    primary_url = sources[0].get("url") if sources else item.get("url")
    if primary_url:
        parts.append(f"🔗 [Lire l'article complet]({primary_url})")

    caption = "\n".join(parts)
    image_url = item.get("image_url")
    return caption, image_url


def escape_md(text: str) -> str:
    """Échappe les caractères Markdown legacy de Telegram."""
    if not text:
        return ""
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, "\\" + ch)
    return text


# ─────────────────── Main ───────────────────
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    topic_news = os.environ.get("TELEGRAM_TOPIC_NEWS", "").strip()
    if not token or not chat_id:
        log("❌ TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
        sys.exit(1)
    # Convertir le topic en int si présent et numérique, sinon None
    thread_id = int(topic_news) if topic_news.lstrip("-").isdigit() else None
    log(f"🔑 Token ok ({len(token)} chars) · Chat: '{chat_id[:4]}...{chat_id[-4:]}' (len={len(chat_id)})"
        f" · Topic News: {thread_id if thread_id else '(canal général)'}")

    if not HISTORY_FILE.exists():
        log("⚠️  news_history.json absent — rien à envoyer")
        sys.exit(0)
    hist = json.loads(HISTORY_FILE.read_text())

    items = filter_recent(hist.get("items", {}), DIGEST_WINDOW_DAYS)
    log(f"📰 {len(items)} annonce(s) sur les {DIGEST_WINDOW_DAYS} derniers jours")

    if not items:
        log("ℹ️  Aucune annonce cette semaine, pas d'envoi.")
        sys.exit(0)

    # Pré-résolution des vrais titres + descriptions + images depuis les fiches
    # détaillées. Mis en cache dans _resolved_* pour éviter de refetch dans
    # format_announcement, et utilisé pour la fusion par titre similaire.
    log("🔍 Résolution des titres réels depuis les fiches produit…")
    for item in items:
        url = item.get("url", "")
        if not url:
            continue
        d_title, d_desc, d_image = fetch_article_data(url, item.get("lang", "FR"))
        if d_title:
            item["_resolved_title"] = d_title
        if d_desc:
            item["_resolved_description"] = d_desc
        if d_image:
            item["_resolved_image"] = d_image

    # Fusion supplémentaire : pour les annonces non encore fusionnées, on tente
    # une fusion par "même date + titre similaire". Cas : Playmat 16/02/2026
    # qui sort en FR et EN avec des paths URL différents mais le même nom commercial.
    before_fusion = len(items)
    items = fuse_similar_titles(items, min_similarity=0.5)
    if len(items) < before_fusion:
        log(f"🔗 {before_fusion - len(items)} annonce(s) fusionnée(s) par titre similaire")

    # 1. Message header
    header = format_digest_header(items)
    log("📤 Envoi du header…", indent=1)
    res = tg_send_message(token, chat_id, header, parse_mode="Markdown", thread_id=thread_id)
    if not res.get("ok"):
        log(f"❌ Header échoué : {res}", indent=2)
        # Retente sans Markdown
        res = tg_send_message(token, chat_id, re.sub(r"[*_`]", "", header), thread_id=thread_id)
        if not res.get("ok"):
            log(f"❌ Header échoué même en plain : {res}", indent=2)
            sys.exit(1)
    time.sleep(1.5)

    # 2. Un message par annonce
    sent = 0
    for i, item in enumerate(items, 1):
        log(f"[{i}/{len(items)}] {item.get('title', '?')[:60]}", indent=1)
        caption, img_url = format_announcement(item)
        if img_url:
            res = tg_send_photo(token, chat_id, img_url, caption=caption,
                                parse_mode="Markdown", thread_id=thread_id)
            if not res.get("ok"):
                log(f"⚠️  Photo échouée ({res.get('description', '?')[:80]}) — fallback texte", indent=2)
                res = tg_send_message(token, chat_id, caption, parse_mode="Markdown", thread_id=thread_id)
                if not res.get("ok"):
                    # dernier recours plain text
                    res = tg_send_message(token, chat_id, re.sub(r"[*_`\[\]]", "", caption), thread_id=thread_id)
        else:
            res = tg_send_message(token, chat_id, caption, parse_mode="Markdown", thread_id=thread_id)
            if not res.get("ok"):
                res = tg_send_message(token, chat_id, re.sub(r"[*_`\[\]]", "", caption), thread_id=thread_id)
        if res.get("ok"):
            sent += 1
        # Anti-rate-limit Telegram (30 messages/sec en groupe, on prend large)
        time.sleep(2.0)

    log(f"✅ Digest envoyé : {sent}/{len(items)} annonce(s)")


if __name__ == "__main__":
    main()
