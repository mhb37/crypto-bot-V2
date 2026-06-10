# 🤖 Meme Coin Scanner & Trading Bot

Bot Python de scanning et trading automatisé de meme coins — 100% gratuit, hébergeable sur Railway.

## Fonctionnalités

| Fonctionnalité | Détail |
|---|---|
| **Scanner** | DexScreener + GeckoTerminal (APIs gratuites), scan toutes les 5 min |
| **Filtrage** | Âge >24h, liquidité, volume, tendance haussière, anti-scam |
| **IA/ML** | RandomForest, score 0-100, réentraînement automatique |
| **Paper trading** | Simulation avec slippage réaliste, SL/TP automatiques |
| **Telegram** | Alertes temps réel + commandes interactives |
| **Reporting** | Rapport quotidien, win rate, PnL, leçons apprises |
| **Live trading** | Prêt (Jupiter DEX/Solana) — désactivé par défaut |

---

## 🚀 Déploiement en 5 étapes

### Étape 1 — Créer votre bot Telegram

1. Ouvrez Telegram et cherchez **@BotFather**
2. Envoyez `/newbot`
3. Choisissez un nom (ex: "Mon Meme Bot") et un username (ex: `monmemebot_bot`)
4. Copiez le **token** (format: `123456789:ABCdef...`)
5. Démarrez votre bot en lui envoyant `/start`
6. Pour obtenir votre **Chat ID** :
   - Envoyez un message à votre bot
   - Visitez : `https://api.telegram.org/bot<VOTRE_TOKEN>/getUpdates`
   - Trouvez `"chat":{"id": VOTRE_CHAT_ID}`

### Étape 2 — Créer un compte Railway

1. Allez sur [railway.app](https://railway.app)
2. Cliquez **"Start a New Project"** → **"Deploy from GitHub repo"**
3. Connectez votre compte GitHub
4. Sélectionnez ce repository

### Étape 3 — Configurer les variables d'environnement

Dans Railway, allez dans **Variables** et ajoutez :

```
TELEGRAM_BOT_TOKEN=<votre token BotFather>
TELEGRAM_CHAT_ID=<votre chat ID>
TRADING_MODE=paper
INITIAL_PORTFOLIO=1000
MIN_SCORE_TO_TRADE=70
TARGET_CHAINS=solana
```

### Étape 4 — Déployer

Railway déploie automatiquement après chaque push sur `main`.
Le bot démarrera en **paper trading** — aucun argent réel engagé.

### Étape 5 — Tester

Envoyez `/status` à votre bot Telegram pour confirmer qu'il est actif.

---

## 📱 Commandes Telegram

| Commande | Description |
|---|---|
| `/start` | Message d'accueil |
| `/status` | État du bot, mode, portfolio |
| `/positions` | Positions ouvertes en cours |
| `/pnl` | Performances et statistiques |
| `/pause` | Suspendre les nouveaux trades |
| `/resume` | Reprendre les trades |
| `/help` | Aide complète |

---

## ⚙️ Configuration complète

Voir `.env.example` pour toutes les variables disponibles.

### Paramètres clés

```env
# Scanner
MIN_AGE_HOURS=24          # Tokens d'au moins 24h
MIN_LIQUIDITY_USD=10000   # Liquidité min $10K
MIN_SCORE_TO_TRADE=70     # Score IA minimum (0-100)

# Trading
POSITION_SIZE_PCT=0.05    # 5% du portfolio par trade
STOP_LOSS_PCT=0.15        # Stop loss à -15%
TAKE_PROFIT_PCT=0.50      # Take profit à +50%
MAX_OPEN_POSITIONS=5      # Maximum 5 positions simultanées
```

---

## 🧠 Système IA/ML

### Phase 1 — Scoring heuristique (sans données)
Au démarrage, le bot utilise un scoring basé sur des règles :
- Momentum (25%) — hausse cohérente sur 1h/6h/24h
- Pression acheteuse (20%) — ratio buys/sells
- Accélération du volume (15%) — volume 1h vs moyenne 24h
- Ratio volume/liquidité (15%) — signal d'intérêt
- Consistance (15%) — toutes les périodes positives
- Score d'âge (10%) — optimal entre 24h et 72h

### Phase 2 — Modèle ML (après 20+ trades)
Quand suffisamment de trades sont accumulés :
- Le modèle se réentraîne automatiquement
- RandomForestClassifier avec 200 arbres
- Features normalisées via StandardScaler
- Réentraînement hebdomadaire ou après 20 nouveaux trades

### Apprentissage
- Les trades fermés sont annotés (succès/échec)
- Les patterns d'erreur sont analysés dans le rapport quotidien
- Les leçons actionnables sont suggérées

---

## 💸 Passer en trading réel

⚠️ **Testez au minimum 2-4 semaines en paper trading avant de passer en live.**

1. Créez un **wallet Solana dédié** (ne jamais utiliser le wallet principal)
2. Financez-le avec un petit montant (ex: $50-100 pour commencer)
3. Installez les dépendances supplémentaires :
   ```bash
   pip install solders base58 solana
   ```
4. Ajoutez dans Railway Variables :
   ```
   TRADING_MODE=live
   SOLANA_PRIVATE_KEY=<votre clé privée base58>
   ```
5. ⚠️ Ne jamais committer votre clé privée dans le code !

---

## 📊 Structure du projet

```
meme-coin-bot/
├── main.py              # Point d'entrée
├── config.py            # Configuration centralisée
├── database.py          # SQLite — trades, signaux, modèles
├── requirements.txt     # Dépendances Python
├── railway.toml         # Config Railway
├── Procfile             # Commande de démarrage
├── .env.example         # Template variables d'environnement
│
├── scanner/
│   ├── dexscreener.py   # APIs DexScreener + GeckoTerminal
│   └── filter.py        # Filtres qualité + anti-scam
│
├── analyzer/
│   ├── features.py      # Extraction de features ML
│   └── model.py         # Modèle ML (RandomForest)
│
├── trader/
│   ├── paper_trader.py  # Moteur paper trading
│   └── live_trader.py   # Trading réel Jupiter/Solana
│
├── notifier/
│   └── telegram_bot.py  # Notifications + commandes
│
├── reporter/
│   ├── daily_report.py  # Rapport quotidien
│   └── learner.py       # Réentraînement ML
│
├── models/              # Modèles ML sauvegardés (.pkl)
└── data/                # Base SQLite + logs + rapports
```

---

## 🔧 Développement local

```bash
# Cloner et installer
cd meme-coin-bot
pip install -r requirements.txt

# Copier et configurer
cp .env.example .env
# Éditez .env avec vos valeurs

# Lancer
python main.py
```

---

## ⚠️ Avertissements

- **Le trading de crypto-monnaies est risqué.** Vous pouvez perdre tout votre capital.
- **Commencez toujours en paper trading** avant d'engager de l'argent réel.
- Ce bot n'est **pas un conseil financier**.
- Les performances passées ne garantissent pas les performances futures.
- Les meme coins sont extrêmement volatils et souvent des scams.

---

## 📄 Licence

MIT — Libre d'utilisation, modification et distribution.
