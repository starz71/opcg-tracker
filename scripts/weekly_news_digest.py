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
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# Import du collector pour récupérer les paramètres communs
sys.path.insert(0, str(Path(__file__).parent))
from news_collector import (
    HISTORY_FILE, fetch_html, log
)

DIGEST_WINDOW_DAYS = 7  # Annonces des 7 derniers jours

# ─────────────────── Telegram ───────────────────
def tg_send_message(token: str, chat_id: str, text: str, parse_mode: str = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log(f"[DEBUG sendMessage HTTP {r.status_code}] body={r.text[:300]}", indent=2)
        return r.json() if r.text else {}
    except Exception as ex:
        log(f"[DEBUG sendMessage exception] {ex}", indent=2)
        return {"ok": False, "description": str(ex)}


def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str = None,
                  parse_mode: str = None) -> dict:
    """Envoie une photo via URL distante (Telegram télécharge l'image)."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {"chat_id": str(chat_id), "photo": photo_url}
    if caption:
        # Telegram limite la caption à 1024 caractères
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        payload["caption"] = caption
        if parse_mode:
            payload["parse_mode"] = parse_mode
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
def filter_recent(items: dict, days: int = DIGEST_WINDOW_DAYS) -> list[dict]:
    """Garde les annonces vues dans la fenêtre."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    selected = []
    for key, item in items.items():
        first_seen = item.get("first_seen", "")
        if first_seen >= cutoff:
            selected.append(item)
    # Tri : par date publiée décroissante, sinon par first_seen
    def sort_key(it):
        return it.get("published_date") or it.get("first_seen") or ""
    selected.sort(key=sort_key, reverse=True)
    return selected


def fetch_excerpt(url: str, lang: str, max_chars: int = 280) -> str:
    """Récupère un résumé de l'article. Cherche meta description, sinon début du contenu."""
    html = fetch_html(url, timeout=10)
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    # Priorité 1 : meta description
    for meta_attr in [{"name": "description"}, {"property": "og:description"}]:
        meta = soup.find("meta", attrs=meta_attr)
        if meta and meta.get("content"):
            content = meta["content"].strip()
            if len(content) > 30:
                return content[:max_chars]
    # Priorité 2 : premier <p> significatif
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if text and len(text) > 50 and "©" not in text and "cookie" not in text.lower():
            return text[:max_chars]
    return ""


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
    # Traduit si JP
    title = translate_to_fr(raw_title) if is_jp else raw_title
    # Récupère un résumé depuis l'article
    summary = fetch_excerpt(item.get("url", ""), lang)
    if is_jp and summary:
        summary = translate_to_fr(summary)

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
    if not token or not chat_id:
        log("❌ TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")
        sys.exit(1)
    # Debug : montrer la longueur et le format du chat_id (pas la valeur entière)
    log(f"🔑 Token ok ({len(token)} chars) · Chat ID format: '{chat_id[:4]}...{chat_id[-4:]}' (len={len(chat_id)})")

    if not HISTORY_FILE.exists():
        log("⚠️  news_history.json absent — rien à envoyer")
        sys.exit(0)
    hist = json.loads(HISTORY_FILE.read_text())

    items = filter_recent(hist.get("items", {}), DIGEST_WINDOW_DAYS)
    log(f"📰 {len(items)} annonce(s) sur les {DIGEST_WINDOW_DAYS} derniers jours")

    if not items:
        log("ℹ️  Aucune annonce cette semaine, pas d'envoi.")
        sys.exit(0)

    # 1. Message header
    header = format_digest_header(items)
    log("📤 Envoi du header…", indent=1)
    res = tg_send_message(token, chat_id, header, parse_mode="Markdown")
    if not res.get("ok"):
        log(f"❌ Header échoué : {res}", indent=2)
        # Retente sans Markdown
        res = tg_send_message(token, chat_id, re.sub(r"[*_`]", "", header))
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
                                parse_mode="Markdown")
            if not res.get("ok"):
                log(f"⚠️  Photo échouée ({res.get('description', '?')[:80]}) — fallback texte", indent=2)
                res = tg_send_message(token, chat_id, caption, parse_mode="Markdown")
                if not res.get("ok"):
                    # dernier recours plain text
                    res = tg_send_message(token, chat_id, re.sub(r"[*_`\[\]]", "", caption))
        else:
            res = tg_send_message(token, chat_id, caption, parse_mode="Markdown")
            if not res.get("ok"):
                res = tg_send_message(token, chat_id, re.sub(r"[*_`\[\]]", "", caption))
        if res.get("ok"):
            sent += 1
        # Anti-rate-limit Telegram (30 messages/sec en groupe, on prend large)
        time.sleep(2.0)

    log(f"✅ Digest envoyé : {sent}/{len(items)} annonce(s)")


if __name__ == "__main__":
    main()
