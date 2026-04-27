# 🏴‍☠️ OPCG Tracker

Surveillance automatique gratuite des sorties de displays **One Piece TCG** sur les e-shops français + Cardmarket. Notifications push instantanées sur téléphone.

---

## Ce que fait l'outil

- Surveille des sites e-commerce avec des **alertes par mots-clés** — parfait pour les sets pas encore sortis (OP-16, EB-03…). Pas besoin d'URL existante.
- Surveille des **URLs précises** de produits avec seuil de prix.
- Surveille des **cartes Cardmarket** précises avec seuil de prix.
- Notifie **uniquement les nouveautés** (pas de spam) ou les baisses de prix > 3 %.
- Conserve un **historique de prix** dans `history.json`.
- Tourne automatiquement **toutes les 30 minutes** via GitHub Actions (gratuit, illimité sur repo public).

---

## Setup en 5 minutes

### 1. Créez le repo GitHub

1. [Créez un nouveau repo](https://github.com/new) — public conseillé pour des minutes Actions illimitées.
2. Téléchargez le contenu du dossier `opcg-tracker/` et placez-le à la racine du repo.
3. Poussez sur GitHub.

### 2. Configurez ntfy.sh (notif push gratuite)

1. **Choisissez un nom de topic** unique et secret. Toute personne qui connaît le nom peut s'y abonner, donc faites long et aléatoire — par exemple `opcg-mxxx-7k9p2m`.
2. **Installez l'app ntfy** :
   - Android : [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
   - iOS : [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
3. Dans l'app : **Subscribe to topic** → entrez votre nom de topic → OK.

### 3. Personnalisez vos alertes

```bash
cp alerts.example.yaml alerts.yaml
# puis éditez alerts.yaml avec vos alertes
```

Voir la **syntaxe des alertes** plus bas.

### 4. Activez GitHub Actions

1. Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
2. Ajoutez :
   - `NTFY_TOPIC` = votre nom de topic ntfy
   - `TELEGRAM_BOT_TOKEN` (optionnel)
   - `TELEGRAM_CHAT_ID` (optionnel)
3. Onglet **Actions** → activez les workflows si demandé. Le cron tourne maintenant toutes les 30 min.
4. Bouton **Run workflow** disponible pour un test manuel.

### 5. Test en local (recommandé avant de pousser)

```bash
pip install -r requirements.txt
python tracker.py
```

La première exécution crée `state.json` et notifie tout ce qui matche déjà. Les exécutions suivantes ne notifient que les **nouveautés**.

---

## Comment fonctionne le scraping (architecture v1.5)

À chaque exécution (cron toutes les 30 min), le tracker fait ceci :

1. **Étape 1 — scraping mutualisé** : il visite la page catégorie « One Piece » de **chaque site** une fois (URL pré-configurée dans `alerts.yaml`). Pour 42 sites avec 3-4 secondes de délai, ça prend ~3 minutes.
2. **Étape 2 — matching local** : pour chaque alerte, il filtre les produits scrapés selon les keywords/exclusions/seuils. Aucune requête réseau supplémentaire.
3. **Étape 3 — référence Cardmarket** : pour chaque alerte avec `cardmarket_ref`, il récupère le prix le plus bas Cardmarket et l'inclut dans la notification (avec calcul du delta en pourcentage).
4. **Étape 4 — notification** : ntfy / Telegram / email selon ce qui est configuré.

Cette architecture *catégorie-puis-match* est beaucoup plus efficace et polie que l'ancien modèle "une recherche par alerte par site".

### Auto-détection de plateforme

Le tracker connaît les sélecteurs CSS de **PrestaShop, Shopify et WooCommerce** par défaut. Vous précisez juste `platform: prestashop` (ou shopify, ou woocommerce) dans `alerts.yaml` pour chaque site. Si la plateforme est laissée vide, le tracker auto-détecte. Si l'auto-détection échoue, vous pouvez forcer des sélecteurs explicites (voir « Ajouter un nouveau site » ci-dessous).

---

## Référence Cardmarket dans les notifs

Quand une alerte fire, le tracker peut afficher le prix Cardmarket pour comparaison directe :

```
🚨 Display OP-12 FR sous 130€
Display Pillars of Strength FR
📍 Philibert — 124.95€
📦 En stock
📊 CM FR : 138.00€  ✅ (-9%)   ← référence Cardmarket avec écart
```

Pour l'activer, ajoutez à votre alerte :

```yaml
- name: "OP-12 display FR sous 130€"
  keywords: ["OP-12"]
  must_include: ["display"]
  cardmarket_ref:
    url: "https://www.cardmarket.com/fr/OnePiece/Products/Booster-Boxes/..."
    label: "CM FR"
```

---



### A) Mot-clé sur plusieurs sites *(le plus utile)*

C'est le mode pour suivre un set qui n'est pas encore sorti.

```yaml
- name: "Display OP-16 FR"
  keywords: ["OP-16", "OP16"]        # au moins UN doit être dans le titre
  must_include: ["display"]           # TOUS doivent être présents
  exclude: ["booster pack", "single"] # AUCUN ne doit être présent
  queries: ["OP-16 display"]          # ce qui sera tapé dans la barre de recherche
  sites: [philibert, magicbazar, hellogeek]
  max_price: 200                      # optionnel
```

### B) URL fixe avec seuil de prix

Pour un produit existant, quand vous voulez juste être averti d'une baisse.

```yaml
- name: "OP-12 sous 130€"
  type: url
  url: "https://www.philibert.com/.../OP-12.html"
  site_name: "Philibert"
  max_price: 130
```

### C) Carte Cardmarket

```yaml
- name: "Luffy Leader OP01-001 Alt-Art < 200€"
  type: cardmarket
  url: "https://www.cardmarket.com/fr/OnePiece/Products/Singles/..."
  max_price: 200
```

### D) Flux RSS/Atom *(annonces officielles, sites communauté, Twitter via RSSHub)*

```yaml
- name: "OnePiecePlayer — news"
  type: rss
  url: "https://onepieceplayer.com/feed/"
  keywords: ["OP-16"]   # optionnel : filtre dans titre + résumé
```

### E) Page News HTML *(sites sans RSS, ex. site officiel)*

```yaml
- name: "Site officiel ONE PIECE TCG"
  type: news_page
  url: "https://en.onepiece-cardgame.com/news/"
  selectors:
    item: ".news-list li"
    title: ".title"
    link: "a"
```

---

## Ajouter un nouveau site / corriger un site qui ne marche pas

Le tracker auto-détecte PrestaShop / Shopify / WooCommerce. Si pour un site donné le tracker affiche `aucun produit détecté`, c'est qu'il faut soit :

**Option A — préciser la plateforme** :
```yaml
sites:
  monnouveausite:
    name: "Mon Nouveau Site"
    category_url: "https://exemple.com/categorie-one-piece"
    platform: prestashop   # ou shopify, ou woocommerce
```

**Option B — sélecteurs CSS explicites** (pour sites custom) :
1. Ouvrez la page catégorie → F12 → Inspecter un élément sur une carte produit.
2. Repérez les classes CSS de : le bloc produit, le titre, le lien, le prix.
3. Ajoutez :

```yaml
sites:
  sitecustom:
    name: "Site Custom"
    category_url: "https://exemple.com/one-piece"
    selectors:
      product: ".item-card"
      title: ".item-title"
      link: ".item-title a"
      price: ".item-price"
```

Pour debug : lancez `python tracker.py` localement et regardez les logs.

---



**TL;DR honnête** : en 2026, le scraping automatisé de Twitter et Instagram est devenu fragile et risqué (Nitter mort, API X payante 100$/mois, scraping IG → ban du compte). Pour ces deux plateformes, **les notifications natives des apps officielles sont gratuites, instantanées et 100% fiables**. Faites ça en priorité.

### Twitter / X — recommandations par ordre de simplicité

**1. Notifications natives (recommandé)** : dans l'app X, allez sur le compte officiel `@ONEPIECE_tcg_EN`, cliquez sur la cloche 🔔 → "Tous les tweets". Faites pareil pour les comptes communauté qui vous intéressent.

**2. Si vous voulez quand même automatiser** : utilisez **RSSHub**, qui transforme un compte X en flux RSS. Trois variantes :

- **Instance publique gratuite (rapide à tester)** : `https://rsshub.app/twitter/user/ONEPIECE_tcg_EN`. Limite à ~200 requêtes/heure/IP, suffisant pour 5-10 alertes en cron 30 min.
- **RSSHub auto-hébergé sur Railway** (~5 min, gratuit dans la limite des crédits Railway) : [template officiel](https://railway.com/deploy/rsshub). Ensuite votre URL devient `https://votre-rsshub.up.railway.app/twitter/user/...`
- **RSSHub sur Cloudflare Workers** : 100 000 req/jour gratuites, zero maintenance.

Ajoutez ensuite l'alerte dans `alerts.yaml` :
```yaml
- name: "Twitter officiel"
  type: rss
  url: "https://rsshub.app/twitter/user/ONEPIECE_tcg_EN"
  keywords: ["OP-16", "EB-", "release"]   # filtre optionnel
  exclude: ["RT @"]
```

### Instagram — recommandations

**1. Notifications natives (très fortement recommandé)** : suivez les comptes communauté, puis sur leur profil → cloche 🔔 → "Posts".

**2. Si vous voulez automatiser** : c'est techniquement possible avec **RSSHub + credentials Instagram**, mais **votre compte risque d'être suspendu** par les détections anti-bot. À éviter sauf si vous avez un compte secondaire dédié et acceptez le risque. La doc RSSHub Instagram est ici : https://docs.rsshub.app/social-media#instagram

**3. Alternative plus saine** : beaucoup de gros comptes Instagram One Piece TCG ont aussi un Telegram, un Discord ou un Twitter. Privilégiez ces canaux qui sont plus simples à automatiser.

### Telegram — la pépite gratuite

Beaucoup de communautés TCG ont des channels Telegram publics qui mirrorent les news Twitter/Instagram. La page web `https://t.me/s/<nom-du-channel>` est scrapable directement, ou via RSSHub (`/telegram/channel/<nom>`). Reportez-vous à l'exemple 9 dans `alerts.example.yaml`.

---

## Ajouter un nouveau site

1. Allez sur le site, tapez `OP-12` dans la barre de recherche.
2. Copiez l'URL résultante. Remplacez `OP-12` par `{query}`.
3. Sur cette page de résultats, **F12** → **Inspecter un élément** sur une carte produit → repérez les classes CSS de :
   - le bloc produit (ex. `article.product`)
   - le titre (ex. `.product-title`)
   - le lien (ex. `.product-title a`)
   - le prix (ex. `.price`)
4. Ajoutez la config dans `alerts.yaml` :

```yaml
sites:
  monnouveausite:
    name: "Mon Nouveau Site"
    search_url: "https://exemple.com/search?q={query}"
    selectors:
      product: "article.product"
      title: ".product-title"
      link: ".product-title a"
      price: ".price"
```

---

## FAQ

### Pourquoi pas d'auto-achat dans cette v1 ?

L'auto-achat demande de gérer login + captcha + cart + paiement. Une seule étape qui foire et vous achetez le mauvais produit ou laissez un panier ouvert. **Recommandé** : commencez par les notifs (la priorité haute ntfy arrive en ~1 s sur votre téléphone, le clic ouvre direct la fiche produit). Si vous ratez vraiment des drops malgré ça, on passera à une v2 avec Playwright et des scénarios par site.

### Mes sélecteurs CSS ne matchent rien ?

- Lancez `python tracker.py` localement, regardez les logs.
- Le site a peut-être un Cloudflare ou un thème différent. Inspectez la page de résultats avec F12 et ajustez les sélecteurs.
- Pour debug, ajoutez `print(soup.select(product_sel))` temporairement dans `search_site()`.

### Je peux scraper Cardmarket à quel rythme ?

Restez à 30 min minimum entre runs. Cardmarket peut throttler ou demander un captcha si trop agressif. Pour des dizaines de cartes, l'API officielle (avec OAuth) est plus propre — mais elle demande une inscription développeur.

### Combien ça coûte vraiment ?

| Service | Coût |
|---|---|
| GitHub Actions (repo public) | **Gratuit illimité** |
| GitHub Actions (repo privé) | 2 000 min/mois gratuites (~80× ce qui est consommé) |
| ntfy.sh | **Gratuit, sans compte** |
| Telegram bot | Gratuit |
| Gmail SMTP | Gratuit |
| **Total** | **0 €** |

### Et si GitHub Actions est en panne ou trop lent ?

Alternative : exécuter `python tracker.py` sur un PC toujours allumé ou un Raspberry Pi via cron Linux ou Task Scheduler Windows. Même script, même config.

### Comment lire l'historique de prix ?

`history.json` est un JSON simple :
```json
{
  "ab12cd34ef56": {
    "title": "Display OP-12 ...",
    "url": "...",
    "site": "Philibert",
    "prices": [
      {"date": "2026-04-27T10:00:00+00:00", "price": 144.95},
      {"date": "2026-04-29T10:00:00+00:00", "price": 139.95}
    ]
  }
}
```

Vous pouvez le visualiser avec n'importe quel outil JSON ou un petit script Python avec matplotlib.

---

## Limites assumées

- Pas d'auto-achat (voir FAQ).
- Pas de gestion de comptes connectés (vous voyez ce qu'un visiteur anonyme voit).
- Les sélecteurs CSS doivent parfois être ajustés quand un site change son thème.
- Les sites avec Cloudflare anti-bot agressif peuvent bloquer (ajoutez `cloudscraper` à la place de `requests` si nécessaire).

---

## Évolutions possibles (v2)

Dites-moi ce que vous voulez en priorité :
- 🛒 Auto-achat avec Playwright (login + cart + paiement par site)
- 📊 Dashboard web HTML avec graphes de prix
- 📧 Digest hebdomadaire par email avec récap des prix
- 🔄 Comparaison de prix multi-sites pour un même set
- 🌐 Support de sites européens (DE, BE, NL)
