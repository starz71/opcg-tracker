#!/usr/bin/env python3
"""
update_lookups.py — Met à jour la section cardmarket_lookups de alerts.yaml
à partir d'un export JSON Cardmarket.

Usage :
  python scripts/update_lookups.py path/to/products.json

Comportement :
  - Lit le JSON CM (format : {"version":1, "products":[{idProduct, name, categoryName, ...}]})
  - Filtre les Booster Boxes simples (pas Case, pas Pre-Errata, pas Display)
  - Mappe chaque produit à un (set_code, lang) via NAME_TO_CODE + détection JP
  - Génère l'URL CM via slugification du nom
  - Met à jour SEULEMENT les entrées vides de alerts.yaml (ne touche pas
    aux URLs déjà remplies à la main)
  - Préserve l'ordre des sets, les commentaires, et tout le reste du fichier
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALERTS_FILE = ROOT / "alerts.yaml"

# Mapping nom CM → code de set. À étendre quand un nouveau set sort avec
# un nouveau nom inconnu de cette table.
NAME_TO_CODE = {
    "Romance Dawn":               "OP-01",
    "Paramount War":              "OP-02",
    "Pillars of Strength":        "OP-03",
    "Kingdoms of Intrigue":       "OP-04",
    "Awakening of the New Era":   "OP-05",
    "Wings of the Captain":       "OP-06",
    "500 Years into the Future":  "OP-07",
    "Two Legends":                "OP-08",
    "Emperors in the New World":  "OP-09",
    "Royal Blood":                "OP-10",
    "A Fist of Divine Speed":     "OP-11",
    "Legacy of the Master":       "OP-12",
    "Carrying on his Will":       "OP-13",
    "The Azure Sea's Seven":      "OP-14",
    "Heroines Edition":           "OP-15",
    # Placeholders avant noms officiels
    "OP15":                       "OP-15",
    "OP16":                       "OP-16",
    "OP17":                       "OP-17",
    "OP18":                       "OP-18",
    "OP19":                       "OP-19",
    "OP20":                       "OP-20",
    "OP21":                       "OP-21",
    "OP22":                       "OP-22",
    "OP23":                       "OP-23",
    "OP24":                       "OP-24",
    "OP25":                       "OP-25",
    # Mappings JP probables (à corriger si erreur)
    "Egghead Crisis":             "OP-17",
    "Adventure on Kami's Island": "OP-18",
    # Extra Boosters
    "Memorial Collection":        "EB-01",
    "Anime 25th Collection":      "EB-02",
    # Premium / Best
    "The Best":                   "PRB-01",
    "The Best Vol.2":             "PRB-02",
    "The Best Vol.3":             "PRB-03",
}

JP_MARKERS = ["(Non-English)", "(Asia Region Legal)", "(Asia Region Lega)"]


def slugify(name):
    """Reproduit la règle de slug Cardmarket."""
    s = name.replace("(", "").replace(")", "")
    s = s.replace("'", "")
    s = re.sub(r"\s+", "-", s.strip())
    return s


def url_for(name):
    return f"https://www.cardmarket.com/fr/OnePiece/Products/Booster-Boxes/{slugify(name)}"


def parse_cm_export(json_path):
    """Renvoie {set_code: {fr, en, jp}} depuis l'export Cardmarket."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    boxes = [
        p for p in data.get("products", [])
        if p.get("categoryName") == "One Piece Booster Boxes"
        and "Case" not in p["name"]
        and "Pre-Errata" not in p["name"]
    ]
    result = {}
    unknown = []
    for b in boxes:
        name = b["name"]
        is_jp = any(m in name for m in JP_MARKERS)
        lang = "jp" if is_jp else "en"
        # Retire les marqueurs de langue pour matcher avec NAME_TO_CODE
        base = name
        for m in JP_MARKERS:
            base = base.replace(" " + m, "")
        set_name = base.replace(" Booster Box", "").strip()
        code = NAME_TO_CODE.get(set_name)
        if not code:
            unknown.append(name)
            continue
        result.setdefault(code, {"fr": "", "en": "", "jp": ""})
        url = url_for(name)
        if lang == "en":
            if not result[code]["fr"]:
                result[code]["fr"] = url
            if not result[code]["en"]:
                result[code]["en"] = url
        else:
            if not result[code]["jp"]:
                result[code]["jp"] = url
    return result, unknown


def update_alerts_yaml(new_data):
    """Met à jour alerts.yaml en remplaçant SEULEMENT les valeurs vides
    de cardmarket_lookups par les nouvelles URLs trouvées."""
    if not ALERTS_FILE.exists():
        print(f"❌ {ALERTS_FILE} introuvable", file=sys.stderr)
        sys.exit(1)

    text = ALERTS_FILE.read_text(encoding="utf-8")
    # On parse ligne par ligne pour préserver la structure et les commentaires
    lines = text.splitlines(keepends=False)
    out = []
    in_lookups = False
    changes = []

    # Regex pour matcher : "OP-12": { fr: "...", en: "...", jp: "..." }
    line_re = re.compile(
        r'^(\s*)"(?P<code>[A-Z]+-\d+)":\s*\{\s*'
        r'fr:\s*"(?P<fr>[^"]*)",\s*'
        r'en:\s*"(?P<en>[^"]*)",\s*'
        r'jp:\s*"(?P<jp>[^"]*)"\s*\}\s*$'
    )

    for line in lines:
        if line.strip().startswith("cardmarket_lookups:"):
            in_lookups = True
            out.append(line)
            continue
        if in_lookups and line.strip().startswith("# ─── SITES"):
            in_lookups = False

        if in_lookups:
            m = line_re.match(line)
            if m:
                code = m.group("code")
                indent = m.group(1)
                old = {"fr": m.group("fr"), "en": m.group("en"), "jp": m.group("jp")}
                fresh = new_data.get(code, {})
                # On ne remplit QUE les valeurs vides
                final = {
                    "fr": old["fr"] or fresh.get("fr", ""),
                    "en": old["en"] or fresh.get("en", ""),
                    "jp": old["jp"] or fresh.get("jp", ""),
                }
                # Détecter les changements
                added = [k for k in ("fr", "en", "jp") if not old[k] and final[k]]
                if added:
                    changes.append((code, added))
                new_line = (
                    f'{indent}"{code}": {{ '
                    f'fr: "{final["fr"]}", '
                    f'en: "{final["en"]}", '
                    f'jp: "{final["jp"]}" '
                    f'}}'
                )
                out.append(new_line)
                continue
        out.append(line)

    new_text = "\n".join(out)
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text, changes


def main():
    if len(sys.argv) < 2:
        # Recherche un JSON par défaut dans le repo
        candidates = list(ROOT.glob("data/products*.json")) + list(ROOT.glob("products*.json"))
        if not candidates:
            print("Usage: python scripts/update_lookups.py <products.json>", file=sys.stderr)
            sys.exit(1)
        json_path = sorted(candidates)[-1]
        print(f"📂 Pas d'argument fourni, utilisation de : {json_path}")
    else:
        json_path = Path(sys.argv[1])
        if not json_path.exists():
            print(f"❌ Fichier introuvable : {json_path}", file=sys.stderr)
            sys.exit(1)

    print(f"📥 Lecture de l'export CM : {json_path}")
    fresh, unknown = parse_cm_export(json_path)
    print(f"✓ {len(fresh)} sets reconnus depuis l'export")
    if unknown:
        print(f"⚠️  {len(unknown)} produit(s) non mappé(s) (nouveau set ?) :")
        for n in unknown:
            print(f"     - {n}")
        print("    → Si c'est un nouveau set, ajoutez-le dans NAME_TO_CODE en haut de ce script.")

    new_text, changes = update_alerts_yaml(fresh)

    if not changes:
        print("ℹ️  Aucune mise à jour nécessaire — alerts.yaml est déjà à jour.")
        return

    ALERTS_FILE.write_text(new_text, encoding="utf-8")
    print(f"\n✅ alerts.yaml mis à jour avec {len(changes)} changement(s) :")
    for code, added in changes:
        print(f"     {code} : +{', '.join(added)}")


if __name__ == "__main__":
    main()
