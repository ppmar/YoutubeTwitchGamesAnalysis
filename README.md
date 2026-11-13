# YoutubeTwitchGamesAnalysis

Collecteur YouTube pour l'étude Procom (IMT Atlantique) sur l'émergence et la durée des
tendances dans le jeu vidéo. L'outil collecte les vidéos YouTube associées aux jeux d'une
étude, leurs statistiques, les chaînes qui les publient et l'horodatage de leurs
commentaires, puis calcule un **score d'attention mensuel par jeu** joignable au dataset
Twitch sur `(game_id, year, month)`.

Première étude : le genre **battle royale** (39 titres du dataset Twitch).

Spécification complète : [`docs/SPEC.md`](docs/SPEC.md).

## Installation

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # puis renseigner YOUTUBE_API_KEY
pytest
```

## Utilisation

```sh
ytc --help
```

Le détail des commandes est en section 9 de la spec. La collecte quotidienne est assurée
par `ytc daily`, lancée par un service cron Railway (voir `docs/RAILWAY.md`).

## Contraintes de quota

`search.list` est limité à **100 appels par jour**, compteur séparé du budget de 10 000
unités des autres endpoints. C'est le goulot d'étranglement : la collecte complète de
l'étude battle royale demande une douzaine de jours. Aucun test n'appelle l'API réelle.

## Données

`data/external/` contient les deux CSV Twitch publics (fournis, non modifiés). Aucune
donnée collectée, aucun export et aucune donnée client ne sont versionnés : **le dépôt est
public**.

## Biais connus

Section 12 de la spec. Les principaux :

- le score d'attention est une estimation reposant sur l'hypothèse commentaires ↔ vues ;
- échantillon limité aux 50 vidéos les plus vues par jeu et par mois ;
- biais de survie (vidéos et chaînes supprimées ou privées) ;
- côté Twitch, censure par le seuil du top 200 et catégories mixtes (Warzone, Fortnite) ;
- le genre est dominé par quatre titres (91 % des heures regardées).
