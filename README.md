# Unsharp Candles Bot — XTB / xStation5

Bot de trading automatique implémentant la méthode **Unsharp Candles**
(Lead Candle → Confirmation Candle → Execution Candle sur niveau clé),
connecté au broker **XTB** via l'API **xStation5 (xAPI)** en WebSocket,
avec un **money management progressif** basé sur une fraction du capital
disponible.

> ⚠️ **Logiciel expérimental et éducatif. Ce n'est pas un conseil financier.**
> Le trading à effet de levier comporte un risque de perte totale du capital.
> Lisez la section [Avertissements](#10-avertissements) avant toute utilisation.

---

## Sommaire

1. [Architecture](#1-architecture)
2. [Structure des fichiers](#2-structure-des-fichiers)
3. [La méthode Unsharp, telle qu'implémentée](#3-la-méthode-unsharp-telle-quimplémentée)
4. [Money management progressif](#4-money-management-progressif)
5. [Installation](#5-installation)
6. [Configuration](#6-configuration)
7. [Lancement du bot](#7-lancement-du-bot)
8. [Logs et fichiers de signaux](#8-logs-et-fichiers-de-signaux)
9. [Adapter à XTB (démo → réel)](#9-adapter-à-xtb-démo--réel)
10. [Avertissements](#10-avertissements)

---

## 1. Architecture

Le projet suit une **architecture hexagonale** (ports & adaptateurs). Le principe :
la logique de trading ne sait pas qu'elle parle à XTB.

```
                    ┌─────────────────────────────────────────┐
                    │            engine/bot.py                │
                    │   boucle principale / orchestration      │
                    └───┬──────────┬──────────┬──────────┬─────┘
                        │          │          │          │
          ┌─────────────▼──┐  ┌────▼─────┐ ┌──▼───────┐ ┌▼──────────┐
          │   strategy/    │  │  risk/   │ │ engine/  │ │ journal.py│
          │  (cœur pur)    │  │          │ │ trader   │ │           │
          │ • indicators   │  │ • sizing │ │ • ordres │ │ • JSON    │
          │ • levels       │  │ • guards │ │ • stops  │ │ • CSV     │
          │ • unsharp      │  │          │ │ • recon. │ │ • audit   │
          │ • planner      │  │          │ │          │ │           │
          └────────────────┘  └──────────┘ └────┬─────┘ └───────────┘
                                                │
                                     ┌──────────▼──────────┐
                                     │  broker/base.py     │  ← le PORT
                                     │  (interface Broker) │
                                     └──────────┬──────────┘
                                     ┌──────────┴──────────┐
                              ┌──────▼──────┐       ┌──────▼──────┐
                              │ XtbBroker   │       │ PaperBroker │
                              │ (xAPI WS)   │       │ (simulation)│
                              └─────────────┘       └─────────────┘
```

### Pourquoi ces choix

| Décision | Raison |
|---|---|
| **Port `Broker` abstrait** | Changer de broker ou de bibliothèque = écrire un adaptateur, sans toucher à la stratégie. La consigne « rester facilement adaptable » est structurelle, pas déclarative. |
| **Cœur stratégie *pur*** (candles → signal, aucune I/O) | Testable sans réseau ni compte. Les 114 tests tournent en moins de deux secondes. |
| **Client xAPI écrit à la main** | xAPI est un protocole JSON requête/réponse d'environ 200 lignes. L'écrire nous donne le contrôle des deux choses qui comptent en production : le **rate limit** (XTB coupe au-delà d'environ 1 requête / 200 ms) et la **reconnexion + re-login**. Les wrappers PyPI masquent le `streamSessionId`, nécessaire pour le socket de streaming. |
| **Décision sur bougie *clôturée* uniquement** | Pas de repaint : un signal émis à la bougie *t* n'est jamais révisé. C'est ce qui rend le backtest comparable au live. |
| **Le backtester pilote les mêmes classes** | Détecteur, planner, sizer et broker papier sont partagés. Ce que vous mesurez en backtest est ce que le bot ferait. |
| **Aucune dépendance lourde** | `websocket-client` et `PyYAML`. Pas de pandas : les séries sont de simples listes de `Candle`, ce qui suffit largement à ce volume et simplifie l'installation. |
| **Deux sockets XTB** | Le socket principal sert les bougies et les ordres (synchrone). Le socket de streaming sert les prix temps réel et les mises à jour de position (asynchrone, thread dédié). Le streaming est un accélérateur, jamais une dépendance : s'il tombe, le bot continue. |

---

## 2. Structure des fichiers

```
Unsharp/
├── README.md                       ce fichier
├── install.py                      installeur en une commande (sans dépendance)
├── Makefile                        raccourcis : setup, init, check, test…
├── Dockerfile / docker-compose.yml environnement identique partout
├── pyproject.toml                  packaging + entrée CLI "unsharp-bot"
├── requirements.txt                dépendances runtime
├── requirements-dev.txt            + pytest
├── .env.example                    identifiants XTB (à copier en .env)
│
├── config/
│   └── config.example.yaml         toute la configuration, commentée
│
├── src/unsharp_bot/
│   ├── cli.py                      point d'entrée : init / run / check / scan / backtest / symbols
│   ├── config.py                   configuration typée (YAML + .env + surcharges CLI)
│   ├── models.py                   modèles du domaine (Candle, Level, Setup, TradePlan…)
│   ├── journal.py                  journal des signaux (JSON / CSV / audit JSONL)
│   ├── logging_setup.py            logs texte rotatifs
│   │
│   ├── strategy/                   ← cœur pur, sans I/O
│   │   ├── indicators.py           ATR, corps moyen, fractales
│   │   ├── levels.py               détection et clustering des niveaux clés
│   │   ├── unsharp.py              détecteur Lead / Confirmation / Execution
│   │   └── planner.py              entrée / stop / target / ratio R-R
│   │
│   ├── risk/
│   │   ├── sizing.py               money management progressif
│   │   └── guards.py               garde-fous (perte journalière, cooldowns…)
│   │
│   ├── broker/
│   │   ├── base.py                 interface Broker (le port)
│   │   ├── xtb_client.py           transport WebSocket xAPI brut
│   │   ├── xtb_broker.py           adaptateur XTB → modèles du domaine
│   │   └── paper.py                broker simulé (offline / backtest)
│   │
│   ├── engine/
│   │   ├── clock.py                sessions, fuseaux, filtre de timing
│   │   ├── trader.py               envoi d'ordres, stops, réconciliation
│   │   └── bot.py                  boucle principale
│   │
│   └── backtest/
│       └── runner.py               rejeu historique + statistiques
│
├── examples/
│   ├── signals-2026-09-12.json     exemple réel produit par le bot
│   ├── signals-2026-09-12.csv      le même en CSV
│   └── audit-2026-09-12.jsonl      trace d'audit
│
├── tests/                          114 tests (pytest)
└── data/
    ├── logs/                       logs texte (créé au lancement)
    └── signals/                    journaux quotidiens (créé au lancement)
```

---

## 3. La méthode Unsharp, telle qu'implémentée

Le détecteur évalue toujours la **dernière bougie clôturée** comme Execution
Candle candidate, puis remonte la séquence. Il teste les longs et les shorts.

### 3.1 Lead Candle — `strategy/unsharp.py::_check_lead`

Grosse bougie directionnelle qui pousse dans un niveau, balaie les stops et fait
peur au retail. Trois conditions :

| Critère | Paramètre | Défaut |
|---|---|---|
| Corps ≥ N × ATR | `lead_min_body_atr` | 0.8 |
| Corps ≥ N × corps moyen récent | `lead_min_body_ratio` | 1.5 |
| Corps / amplitude (bougie « propre ») | `lead_min_body_to_range` | 0.5 |

Pour un setup **LONG**, la Lead est **baissière** (vente panique dans un support).
Pour un **SHORT**, elle est haussière.

### 3.2 Confirmation Candles — `_check_confirmation`

Le prix n'arrive plus à aller plus loin. La zone contient de 1 à 5 bougies
(paramétrable) et doit vérifier :

| Critère | Paramètre | Défaut |
|---|---|---|
| La zone ne prolonge pas la Lead (le balayage reste contenu) | `confirmation_max_overshoot_atr` | 0.35 |
| Petits corps vs la Lead (« mâchouillage ») | `confirmation_max_body_ratio` | 0.55 |
| Zone compacte vs l'amplitude de la Lead | `confirmation_max_zone_to_lead_range` | 0.85 |
| **Mèches de rejet** du côté opposé | `confirmation_min_wick_ratio` | 0.30 |
| Part des bougies portant cette mèche | `confirmation_min_wick_share` | 0.34 |

Les mèches du côté opposé sont le signal cœur de la méthode : quelqu'un absorbe
le flux et défend le niveau.

### 3.3 Execution Candle — `_check_execution`

Bougie qui confirme le pivot et repart clairement dans le sens opposé à la Lead.

| Critère | Paramètre | Défaut |
|---|---|---|
| Corps ≥ N × ATR | `execution_min_body_atr` | 0.35 |
| Corps / amplitude | `execution_min_body_to_range` | 0.45 |
| Clôture dans le haut (long) / bas (short) de sa propre amplitude | `execution_min_close_position` | 0.60 |
| Clôture au-delà de la zone de confirmation | `execution_must_break_zone` | true |

### 3.4 Niveaux clés — `strategy/levels.py`

**Si la Lead arrive « dans le vide », le setup est ignoré** (`require_level: true`).
La carte des niveaux combine :

- les **swings** fractals (structure),
- les **extrêmes récents** et **extrêmes de session** (poches de liquidité),
- les **plus haut / plus bas / clôture de la veille** (bougies journalières),
- les **zones de consolidation** (prix testés au moins 3 fois),
- optionnellement les **nombres ronds** psychologiques.

Les niveaux distants de moins de `cluster_atr × ATR` sont fusionnés en une zone ;
le nombre de fusions devient le compteur de `touches`, qui pilote le classement.

La zone de confirmation doit se trouver à moins de `level_tolerance_atr × ATR`
(0.60 par défaut) d'un niveau.

### 3.5 Timing — `engine/clock.py`

La méthode privilégie les moments actifs. Le `TimingFilter` autorise les entrées :

- pendant les `opening_minutes` (60 par défaut) qui suivent l'ouverture de session,
- puis autour de chaque pivot horaire (`pivot_period_minutes`, 30 par défaut),
  avec une tolérance de `pivot_window_minutes` (10 par défaut).

Les `blackout_windows` permettent d'exclure des créneaux (le creux de midi, par
exemple). Le filtre se désactive entièrement avec `timing.enabled: false`.

### 3.6 Gestion du trade — `strategy/planner.py`

- **Entrée** : clôture de l'Execution Candle (`entry_mode: execution_close`) ou
  prix d'ouverture de la bougie suivante (`next_open`).
- **Stop** : juste sous (achat) / au-dessus (vente) des mèches de la zone de
  confirmation, plus un tampon de `stop_buffer_atr × ATR`.
  `stop_reference: lead_and_zone` élargit le stop jusqu'au-delà de la mèche de la
  Lead — plus conservateur, à privilégier si vous vous faites sortir par des
  re-balayages.
- **Target** : le **prochain niveau clé** dans le sens du trade qui offre au moins
  `min_risk_reward` (2.0 par défaut). Le bot vise le **bord proche** de la zone,
  pas son centre. Si aucun niveau ne convient et que
  `allow_synthetic_target: true`, la cible est posée exactement au R-R minimum.

---

## 4. Money management progressif

Implémenté dans `risk/sizing.py`. Deux règles s'enchaînent : la première fixe
**l'ambition**, la seconde fixe **la loi**.

### Étape 1 — le capital disponible

```
capital_disponible = equity − marge_utilisée − (buffer_fraction × equity)
```

Le `buffer_fraction` (10 % par défaut) est une réserve qu'on ne touche jamais,
pour ne pas se retrouver collé à l'appel de marge.

### Étape 2 — la fraction engagée (l'ambition)

```
notionnel = capital_disponible × risk_fraction_per_trade      (0.5 = 50 %)
```

Le paramètre `notional_basis` précise ce que « engager » veut dire :

| Valeur | Signification | Volume obtenu |
|---|---|---|
| `margin` *(défaut)* | le notionnel est la **marge déposée** | `notionnel / marge_par_lot` |
| `exposure` | le notionnel est la **valeur du contrat** | `notionnel / (prix × contract_size)` |

`margin` est le mode conservateur : vous n'immobilisez jamais plus que la
fraction configurée de votre cash. Quand `use_broker_margin_check: true`, la
marge exacte est demandée à XTB (`getMarginTrade`) plutôt qu'estimée localement.

### Étape 3 — le plafond de perte (la loi)

```
perte_max        = max_loss_fraction_of_equity × equity        (0.02 = 2 %)
risque_par_lot   = |entrée − stop| × valeur_du_point_par_lot
volume_max_perte = perte_max / risque_par_lot
```

### Étape 4 — arbitrage et contraintes broker

```
volume = min(volume_fraction_capital, volume_max_perte)
volume = arrondi_inférieur(volume, lot_step)  puis borné à [lot_min, lot_max]
```

Puis, dans l'ordre :

- si `volume < lot_min` : on vérifie si le lot minimum respecterait quand même la
  perte maximale. Si oui, on prend `lot_min`. **Sinon le trade est refusé**
  (`min_lot_exceeds_max_loss`).
- si la marge requise dépasse le capital disponible, le volume est réduit ;
  s'il tombe sous `lot_min`, le trade est refusé.

La note `capped_by_max_loss_constraint` ou `capped_by_capital_fraction` dans le
journal indique laquelle des deux contraintes a mordu.

### Progressivité

L'equity est relue **avant chaque trade**. Le capital monte, la mise monte ;
il descend, la mise descend, proportionnellement.
**Aucune martingale** : une perte ne fait jamais grossir la mise suivante — au
contraire, elle la réduit mécaniquement puisque l'equity a baissé.

Exemple avec les réglages par défaut (US500, stop à 14.5 points) :

| Equity | Capital dispo. | Volume | Risque au stop |
|---:|---:|---:|---:|
| 5 000 | 4 500 | 6.89 | 99.91 (2.00 %) |
| 10 000 | 9 000 | 13.79 | 199.95 (2.00 %) |
| 20 000 | 18 000 | 27.58 | 399.91 (2.00 %) |

### Garde-fous portefeuille — `risk/guards.py`

`max_open_positions`, `max_positions_per_symbol`, `max_trades_per_day`,
`daily_loss_limit_fraction` (arrêt de la journée à −6 %),
`daily_profit_target_fraction`, `cooldown_minutes_after_loss` (30 min),
`min_equity_fraction`.

---

## 5. Installation

**Prérequis** : Python 3.10 ou plus, et un compte **démo** XTB. Rien d'autre.

### En une commande

```bash
git clone https://github.com/Gastouille/Unsharp.git Unsharp
cd Unsharp
python3 install.py
```

C'est tout. Le script est écrit en Python pur, sans aucune dépendance, donc il
tourne sur **Linux, macOS et Windows** avec un Python nu. Il :

1. vérifie votre version de Python,
2. crée l'environnement virtuel `.venv`,
3. installe les dépendances — il utilise [`uv`](https://github.com/astral-sh/uv)
   s'il est présent sur votre machine, car c'est plus rapide, et retombe sur
   `pip` sinon,
4. crée `config/config.yaml` et `.env` à partir des modèles,
5. affiche les commandes suivantes adaptées à votre shell.

Le relancer est sans danger : rien de ce qui existe déjà n'est écrasé.

Puis :

```bash
source .venv/bin/activate     # Windows : .venv\Scripts\Activate.ps1
unsharp-bot init              # saisie guidée de vos identifiants XTB
unsharp-bot check             # vérifie la configuration et la connexion
unsharp-bot run --dry-run     # tourne sans envoyer le moindre ordre
```

### Options de l'installeur

```bash
python3 install.py --dev          # installe aussi pytest
python3 install.py --no-venv      # installe dans le Python courant
python3 install.py --no-editable  # copie figée plutôt qu'installation éditable
```

### Avec `make` (Linux, macOS)

```bash
make setup        # équivaut à python3 install.py
make init         # saisie des identifiants
make check        # vérification
make dry-run      # boucle sans ordre
make test         # suite de tests
make help         # toutes les cibles
```

### Avec Docker

Pour un environnement identique partout, sans toucher au Python de votre machine :

```bash
cp .env.example .env          # puis renseignez vos identifiants
docker compose build
docker compose run --rm bot check
docker compose up             # lance le bot en mode --dry-run par défaut
```

La configuration et les journaux restent sur votre machine grâce aux volumes.
Ajustez `TZ` dans `docker-compose.yml` : les sessions dépendent du fuseau.

### Installation manuelle

Si vous préférez tout contrôler :

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .              # ou: pip install -e ".[dev]" pour les tests
```

Sans installation, le bot se lance aussi depuis la racine du projet :

```bash
PYTHONPATH=src python3 -m unsharp_bot check
```

### Le fichier de configuration est optionnel

`config/config.yaml` n'est **pas obligatoire**. S'il est absent, le bot utilise
ses valeurs par défaut, qui sont celles du fichier d'exemple. Un dépôt fraîchement
cloné n'a donc besoin que de vos identifiants pour démarrer. Créez le fichier
quand vous voulez régler quelque chose, avec `unsharp-bot init` ou en copiant
`config/config.example.yaml`.

### Tests

```bash
python3 install.py --dev
.venv/bin/pytest              # 114 tests, moins de deux secondes
```

---

## 6. Configuration

Deux fichiers, avec une règle stricte : **aucun secret dans le YAML**.

### 6.1 `.env` — identifiants (jamais commité)

Le plus simple est l'assistant, qui ne fait jamais apparaître votre mot de passe
à l'écran ni dans l'historique du shell :

```bash
unsharp-bot init
```

Il écrit `.env` en lecture pour vous seul (permissions `600`) et crée
`config/config.yaml` s'il manque. Pour scripter la chose :

```bash
unsharp-bot init --non-interactive --user-id 12345678 --mode demo
```

Le contenu résultant, que vous pouvez aussi écrire à la main :

```bash
XTB_USER_ID=12345678              # numéro de compte xStation
XTB_PASSWORD=votre_mot_de_passe   # mot de passe du compte
XTB_MODE=demo                     # demo | real
```

Il n'y a **pas de clé API séparée** chez XTB : on utilise le numéro de compte et
le mot de passe. Créez d'abord un compte de démonstration.

Si vous laissez les valeurs d'exemple en place, le bot vous le dit clairement au
démarrage plutôt que de vous laisser face à un refus de connexion XTB obscur.

Surcharges optionnelles, pratiques pour un test rapide sans éditer le YAML :

```bash
UNSHARP_DRY_RUN=true              # détecte et journalise, n'envoie aucun ordre
UNSHARP_RISK_FRACTION=0.3         # fraction du capital par trade
UNSHARP_MAX_LOSS_FRACTION=0.02    # perte max par trade
UNSHARP_SYMBOLS=EURUSD,US500      # liste d'actifs
UNSHARP_BROKER=paper              # broker simulé, 100 % hors ligne
```

### 6.2 `config/config.yaml` — la stratégie

```bash
cp config/config.example.yaml config/config.yaml
```

Le fichier d'exemple est entièrement commenté. Les sections clés :

```yaml
market:
  symbols: [EURUSD, US500, GER40]   # vérifiez les noms avec : unsharp-bot symbols
  timeframe_minutes: 5              # 1, 5, 15, 30, 60, 240, 1440

sessions:                           # le bot dort en dehors de ces fenêtres
  - name: europe
    start: "09:00"
    end: "17:30"
    timezone: Europe/Paris
    days: [mon, tue, wed, thu, fri]
    flatten_before_close_minutes: 5

risk:
  risk_fraction_per_trade: 0.5      # 50 % du capital disponible
  max_loss_fraction_of_equity: 0.02 # plafond dur : 2 % de l'equity
  buffer_fraction: 0.10             # réserve anti-appel de marge

unsharp:
  min_risk_reward: 2.0              # ratio R-R minimum
  lead_min_body_atr: 0.8
  min_confirmation_candles: 1
  max_confirmation_candles: 5

execution:
  dry_run: false                    # true = aucun ordre envoyé
```

Toute option peut être surchargée en ligne de commande :

```bash
unsharp-bot --set risk.risk_fraction_per_trade=0.25 \
            --set unsharp.min_risk_reward=3.0 run
```

---

## 7. Lancement du bot

### Saisir ses identifiants

```bash
unsharp-bot init
```

Assistant guidé : numéro de compte, mot de passe (masqué), type de compte,
actifs à scanner. Écrit `.env` et `config/config.yaml`.

### Vérifier la configuration et la connexion

```bash
unsharp-bot check
```

Affiche les paramètres chargés, se connecte à XTB, montre le solde et la fiche
technique de chaque instrument (lot minimum, pas de lot, valeur du tick, levier).
**À faire en premier, toujours.**

### Trouver les noms de symboles XTB

```bash
unsharp-bot symbols --filter US
unsharp-bot symbols --filter EUR --limit 20
```

### Scanner une fois, sans trader

```bash
unsharp-bot scan
```

Un passage de détection sur les bougies actuelles. Affiche les niveaux trouvés,
les setups détectés et leur géométrie. Aucun ordre, jamais.

### Backtester

```bash
# Données récupérées depuis XTB : la fiche instrument est lue automatiquement
unsharp-bot backtest --symbol US500 --days 60 --spread 0.5 --json trades.json
```

Depuis un fichier CSV, il n'y a **pas de broker à interroger** : vous devez
décrire l'instrument vous-même, sinon le dimensionnement est faux.

```bash
unsharp-bot backtest --symbol US500 --csv mes_donnees.csv --equity 10000 \
  --contract-size 1 --tick-size 0.1 --tick-value 0.1 \
  --lot-min 0.01 --lot-step 0.01 --lot-max 100 \
  --precision 1 --leverage 5 --spread 0.4
```

Récupérez ces valeurs avec `unsharp-bot check`. Sans ces options, des valeurs par
défaut de type forex sont utilisées et le bot vous avertit : sur un indice, tous
les signaux finiront rejetés en `min_lot_exceeds_max_loss`, ce qui est le
garde-fou qui fait son travail.

Le CSV attend les colonnes `timestamp,open,high,low,close[,volume]`
(`timestamp` en ISO 8601 ou en secondes epoch).

### Lancer en réel (ou en démo)

```bash
unsharp-bot run --dry-run     # détecte, dimensionne, journalise — n'envoie rien
unsharp-bot run               # envoie les ordres sur le compte de XTB_MODE
```

### Ce que fait le bot, cycle par cycle

1. Il vérifie s'il est dans une session configurée. Sinon, il dort jusqu'à la
   prochaine ouverture.
2. Il attend la clôture de la prochaine bougie (plus `poll_offset_seconds`).
3. Pour chaque actif : il récupère les bougies fraîches, reconstruit la carte des
   niveaux, lance le détecteur Unsharp.
4. Pour un setup valide : filtre de timing → garde-fous de risque → géométrie
   (stop / target / R-R) → dimensionnement → envoi de l'ordre avec SL et TP
   attachés.
5. Il réconcilie les positions ouvertes, gère les stops (break-even, trailing),
   et journalise tout.
6. `Ctrl-C` arrête proprement : déconnexion, résumé de la journée.

---

## 8. Logs et fichiers de signaux

### Log texte — `data/logs/unsharp-bot.log`

Rotation quotidienne, 30 jours conservés. Contient les connexions, les erreurs
API, les setups détectés avec leurs métriques, les ordres envoyés avec prix,
volume, stop et target, et les résumés de session.

```
2026-09-12 16:45:04 | INFO | unsharp_bot.engine.bot | SETUP LONG US500 @ 2026-09-12T14:45:00+00:00 | level 5220.20000 (recent_low) | lead body 2.51 ATR | zone 2 candle(s)
2026-09-12 16:45:04 | INFO | unsharp_bot.engine.bot | PLAN LONG US500 | entry=5234.50000 stop=5218.70000 target=5266.10000 | vol=14.0600 R/R=2.00 risk=222.15 (EUR) notional=3679.85 capital=10000.00
2026-09-12 16:45:05 | INFO | unsharp_bot.engine.trader | ORDER SENT LONG US500 vol=14.0600 @ 5234.60000 sl=5218.80000 tp=5266.20000 (position=418823941)
```

### Journal des signaux — `data/signals/`

Trois fichiers par jour :

| Fichier | Rôle |
|---|---|
| `signals-AAAA-MM-JJ.json` | **source de vérité**, réécrit atomiquement à chaque changement d'état |
| `signals-AAAA-MM-JJ.csv` | miroir plat pour tableur |
| `audit-AAAA-MM-JJ.jsonl` | trace append-only de chaque transition |

Tous les setups sont enregistrés, **y compris ceux qui n'ont pas été tradés**,
avec la raison du refus. Une journée se relit entièrement : ce qui a été vu, ce
qui a été refusé et pourquoi, ce qui a été envoyé, comment ça s'est terminé.

#### Exemple de signal (extrait réel de `examples/signals-2026-09-12.json`)

```json
{
  "signal_id": "US500-20260912T144500-08b25c",
  "timestamp": "2026-09-12T14:45:00+00:00",
  "symbol": "US500",
  "direction": "LONG",
  "entry_price": 5234.5,
  "stop_price": 5218.7,
  "target_price": 5266.1,
  "volume": 14.06,
  "ratio_rr": 2.0,
  "level_used": {
    "price": 5220.2,
    "type": "recent_low",
    "touches": 1,
    "width": 2.53530501
  },
  "status": "OPEN",
  "pnl": null,
  "risk_fraction_used": 0.5,
  "capital_available_before_trade": 10000.0,
  "notional_engaged": 3679.85,
  "risk_amount": 222.15,
  "margin_required": 3679.85,
  "position_id": 418823941,
  "session": "europe",
  "timeframe_minutes": 5,
  "notes": [
    "synthetic_target_no_level_at_min_rr",
    "capped_by_max_loss_constraint"
  ],
  "setup": {
    "lead":       { "timestamp": "2026-09-12T14:30:00+00:00", "open": 5250.0, "high": 5251.0, "low": 5221.0, "close": 5224.5 },
    "confirmation": [
      { "timestamp": "2026-09-12T14:35:00+00:00", "open": 5224.5, "high": 5228.0, "low": 5220.2, "close": 5226.5 },
      { "timestamp": "2026-09-12T14:40:00+00:00", "open": 5226.5, "high": 5229.5, "low": 5221.5, "close": 5227.5 }
    ],
    "execution":  { "timestamp": "2026-09-12T14:45:00+00:00", "open": 5227.5, "high": 5235.2, "low": 5227.0, "close": 5234.5 },
    "metrics": {
      "lead_body_atr": 2.51449,
      "zone_max_wick_ratio": 0.625,
      "execution_close_position": 0.914634
    }
  }
}
```

#### Champs importants

| Champ | Signification |
|---|---|
| `status` | `DETECTED` → `PENDING` → `OPEN` → `CLOSED`, ou `REJECTED` / `CANCELLED` / `ERROR` |
| `rejection_reason` | pourquoi le setup n'a pas été tradé (`cooldown_after_loss`, `risk_reward_below_minimum`, `min_lot_exceeds_max_loss`…) |
| `notes` | quelle contrainte a limité la taille (`capped_by_max_loss_constraint` ou `capped_by_capital_fraction`) |
| `risk_amount` | la perte en devise si le stop est touché |
| `notional_engaged` | le montant réellement immobilisé |
| `capital_available_before_trade` | l'assiette du calcul de la fraction |
| `setup.metrics` | les mesures du pattern, pour régler les seuils après coup |

Lecture rapide de la journée :

```bash
python -c "import json;d=json.load(open('data/signals/signals-2026-09-12.json'));[print(f\"{s['timestamp']} {s['symbol']:<8}{s['direction']:<6}{s['status']:<10}{s.get('rejection_reason') or ''} pnl={s['pnl']}\") for s in d['signals']]"
```

---

## 9. Adapter à XTB (démo → réel)

### Endpoints

| Compte | WebSocket principal | WebSocket streaming |
|---|---|---|
| Démonstration | `wss://ws.xtb.com/demo` | `wss://ws.xtb.com/demoStream` |
| Réel | `wss://ws.xtb.com/real` | `wss://ws.xtb.com/realStream` |

Le choix est automatique à partir de `XTB_MODE` : vous ne touchez jamais aux URL.

### Passer de la démo au réel

1. Testez **longuement** en démo. Plusieurs semaines, pas quelques heures.
2. Dans `.env`, remplacez `XTB_MODE=demo` par `XTB_MODE=real`, et mettez les
   identifiants du compte réel.
3. **Réduisez drastiquement la taille** avant le premier ordre réel :
   ```yaml
   risk:
     risk_fraction_per_trade: 0.05      # 5 %, pas 50 %
     max_loss_fraction_of_equity: 0.005 # 0.5 % par trade
     max_trades_per_day: 2
   ```
4. Lancez d'abord `unsharp-bot run --dry-run` en réel pendant quelques jours :
   vous voyez les signaux et les tailles calculées sur le vrai compte, sans
   qu'aucun ordre ne parte.
5. Le bot journalise un avertissement explicite au démarrage en mode réel.

### Commandes xAPI utilisées

`login`, `logout`, `ping`, `getAllSymbols`, `getSymbol`, `getChartLastRequest`,
`getChartRangeRequest`, `getTickPrices` (streaming), `getMarginLevel`,
`getMarginTrade`, `getCurrentUserData`, `getProfitCalculation`, `getServerTime`,
`tradeTransaction`, `tradeTransactionStatus`, `getTrades`, `getTradesHistory`,
et les abonnements streaming `getBalance`, `getTrades`, `getKeepAlive`.

### Robustesse

- **Rate limit** : un limiteur sérialise les commandes à une toutes les 250 ms.
- **Reconnexion** : backoff exponentiel (2 s, 4 s, 8 s… plafonné à 60 s) sur le
  socket principal comme sur le streaming, avec re-login automatique. Les
  abonnements sont rejoués après reconnexion.
- **Erreurs de session** (`BE005`, `BE006`, `BE117`) : re-login puis rejeu de la
  commande, une seule fois.
- **Ordres** : jusqu'à 4 tentatives avec backoff exponentiel. Un refus *métier*
  (volume invalide, marché fermé) arrête immédiatement les tentatives — inutile
  d'insister.
- **SL et TP attachés à l'ordre** : si le bot crashe, la position reste protégée
  côté broker.
- **Décodage des bougies** : XTB renvoie les prix en entiers mis à l'échelle
  `10^digits`, et `high`/`low`/`close` sont des **deltas** par rapport à `open`.
  C'est la source de bug numéro un avec cette API ; elle est traitée dans
  `parse_rate_info` et couverte par les tests.

### Changer de broker ou de bibliothèque

Implémentez `broker/base.py::Broker` (12 méthodes) dans un nouveau fichier, puis
ajoutez-le à `cli.py::build_broker`. Aucune ligne de stratégie ne bouge. Pour
utiliser le paquet PyPI `XTBApi` à la place du client maison, seul
`xtb_broker.py` est à réécrire.

---

## 10. Avertissements

**Ce projet est un outil d'expérimentation et d'éducation. Ce n'est pas un
conseil financier, ni une recommandation d'investissement.**

- Le trading de CFD à effet de levier comporte un **risque de perte totale ou
  partielle du capital**. Une majorité de comptes particuliers perd de l'argent
  sur ces produits.
- **Aucune performance passée ne garantit une performance future.** Les chiffres
  d'un backtest sont une approximation : le spread, le slippage, les swaps, les
  gaps d'ouverture et les exécutions partielles y sont simplifiés. Quand une
  bougie touche à la fois le stop et la cible, le simulateur suppose le stop —
  c'est prudent, ce n'est pas la réalité.
- **La fraction par défaut de 50 % du capital disponible est agressive.** Elle
  correspond à la consigne de conception, pas à une recommandation. C'est le
  plafond `max_loss_fraction_of_equity` (2 %) qui borne réellement le risque par
  trade. Si vous augmentez ce plafond, vous levez le seul vrai garde-fou.
- **Backtestez** la stratégie sur vos propres instruments et vos propres périodes
  avant toute chose. Les seuils par défaut du détecteur sont un point de départ
  raisonnable, pas un réglage optimisé.
- **Testez longuement en démo XTB.** Le mode démo utilise exactement le même code
  et le même chemin d'exécution : seules les URL changent.
- **Commencez avec des tailles très faibles en réel**, et augmentez seulement
  après plusieurs semaines de comportement conforme à vos attentes.
- Surveillez le bot. Une déconnexion prolongée, un instrument suspendu ou un gap
  de week-end peuvent produire des situations que ce code ne couvre pas.
- Vérifiez la fiscalité et la réglementation applicables au trading automatisé
  dans votre pays.

---

## Licence

MIT. Fourni « en l'état », sans aucune garantie.
