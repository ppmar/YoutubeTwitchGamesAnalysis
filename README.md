# YoutubeTwitchGamesAnalysis

Collecteur YouTube pour l'étude Procom (IMT Atlantique) sur l'émergence et la durée des
tendances dans le jeu vidéo. L'outil collecte les vidéos YouTube associées aux jeux d'une
étude, leurs statistiques, les chaînes qui les publient et l'horodatage de leurs
commentaires, puis calcule un **score d'attention mensuel par jeu** joignable au dataset
Twitch sur `(game_id, year, month)`.

Première étude : le genre **battle royale**, 39 jeux actifs du dataset Twitch.

Spécification complète : [`docs/SPEC.md`](docs/SPEC.md).
Calibration du score d'attention : [`docs/SPEC_CALIBRATION.md`](docs/SPEC_CALIBRATION.md) — non implémentée.
Déploiement : [`docs/RAILWAY.md`](docs/RAILWAY.md).

## Installation

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env     # puis renseigner YOUTUBE_API_KEY et DATABASE_URL
alembic upgrade head
```

## Tests

```sh
pytest
```

Aucun test n'appelle l'API YouTube : tout est mocké. Les tests qui portent sur du
comportement propre à PostgreSQL (verrou d'exécution, exécutions concurrentes) sont
**sautés** tant que `TEST_DATABASE_URL` n'est pas renseignée — voir
[`docs/RAILWAY.md`](docs/RAILWAY.md) §10.

## Utilisation

```sh
ytc prepare-games                 # étape 0, aucun appel API
ytc plan                          # tranches, coût, nombre de jours
ytc search --dry-run              # ce qui serait collecté, sans rien émettre
ytc daily                         # ce que lance le cron Railway
ytc status                        # quota restant, avancement par étape
ytc export --format parquet
```

`ytc --help` liste les 13 commandes. Le détail est en section 9 de la spec.

## Budget de quota

`search.list` est limité à **100 appels par jour**, compteur séparé du budget de 10 000
unités des autres endpoints. C'est le goulot d'étranglement de tout le projet.

Pour l'étude battle royale, `ytc plan` annonce **1 600 tranches, soit 17 jours** à
95 appels/jour (100 moins 5 % de marge).

> La spec §5 estimait 1 200 à 1 400 appels. L'écart vient d'un choix délibéré : la fenêtre
> de collecte est **continue** du premier au dernier mois d'un jeu, alors que l'estimation
> ne comptait que les mois réellement présents dans le top 200 Twitch (728 couples
> jeu-mois contre 898 mois continus). Les mois manquants ne sont pas des mois sans
> audience, ce sont des mois sous le 200ᵉ rang : les ignorer reviendrait à ne jamais
> observer le déclin d'un jeu, qui est précisément la question 3.

## Score d'attention

L'API ne donne **aucun historique de vues** : `viewCount` est un cumul à la date de
collecte. Le seul signal nativement daté est l'horodatage des commentaires.

Le score répartit donc les vues cumulées de chaque vidéo dans le temps selon la
distribution de ses commentaires (méthode A), ou selon la courbe de décroissance moyenne
de sa strate quand les commentaires sont inexploitables (méthode B). Le résultat est un
**indice relatif**, jamais un nombre de vues : il s'appelle `attention` partout.

`attention_index` est ramené sur 0–100 par jeu, comme Google Trends, pour que les deux
soient comparables. `share_method_a` indique la part du mois issue de la méthode A : un
mois où elle est faible est moins fiable.

## Biais à connaître

Tous ces biais sont structurels, pas des défauts d'implémentation.

**Score d'attention**

- Il repose sur l'hypothèse commentaires ↔ vues. Le taux de commentaire **baisse en
  général avec l'âge** d'une vidéo (les abonnés commentent tôt), ce qui avance
  artificiellement l'attention tant que la calibration n'est pas faite.
- La calibration (`ytc build-attention --calibrated`) demande au moins 4 instantanés
  hebdomadaires. Avant ça, `r = 1`.
- Les commentaires supprimés et le spam ne sont pas distinguables.
- Les Shorts gonflent les vues sans engagement comparable.
- Une vidéo dont l'historique de commentaires est tronqué bascule en méthode B.

**Échantillon**

- Limité aux 50 vidéos les plus vues par jeu et par mois.
- Biais de survie : vidéos et chaînes supprimées ou passées en privé sont invisibles.
- Le classement est décidé par la recherche YouTube **actuelle** : la collecte n'est pas
  reproductible à l'identique.
- Le filtre catégorie Gaming exclut les vidéos mal catégorisées.

**Requêtes par nom**

- Faux positifs sur les noms génériques (`SOS`, `Scavengers`).
- Faux négatifs sur les titres qui ne citent pas le jeu, ou le citent dans une autre
  langue — notamment Free Fire et PUBG Mobile, dont une grande part de l'audience est
  hispanophone, lusophone ou hindiphone.
- `title_match` mesure ce taux. Sous 70 %, resserrer `yt_query` dans l'étude.

**Côté Twitch**

- **Censure par le seuil** : un jeu absent d'un mois n'a pas une audience nulle, il est
  sous le 200ᵉ rang. Ces mois ne sont jamais remplis par 0.
- La série commence en janvier 2016.
- Catégories mixtes : `Fortnite` couvre tous les modes, `Call of Duty: Warzone` démarre en
  août 2019 alors que le jeu sort en mars 2020 — d'où le champ `valid_from`, qui écarte
  ces mois (62 mois bruts → 55 mois retenus).
- Un jeu peut tenir **deux catégories le même mois** (Fortnite et Fortnite Zero Build) :
  une jointure ligne à ligne duplique ces mois. Utiliser `tables.twitch_game_month()`,
  qui replie correctement.
- Renommages : `PLAYERUNKNOWN'S BATTLEGROUNDS` devient `PUBG: BATTLEGROUNDS` en mai 2021.
- 24 doublons de casse et deux formes d'accents cassés dans l'export, réparés à la
  lecture.

**Le genre**

Quatre titres (Fortnite, Warzone, Apex Legends, PUBG) pèsent **91 % des heures regardées**
du genre. Toute moyenne de genre reflète surtout ces quatre-là.

## Vie privée et conformité

Des commentaires, seuls sont conservés : `video_id`, la date de publication, le nombre de
likes et de réponses. **Ni auteur, ni identifiant d'auteur, ni texte.** L'identifiant du
commentaire est haché de façon non réversible, uniquement pour dédoublonner.

Une seule clé API, un seul projet Google Cloud : multiplier les projets pour contourner le
quota viole les
[Developer Policies](https://developers.google.com/youtube/terms/developer-policies).

## Données

`data/external/` contient les deux CSV Twitch publics, fournis et non modifiés. Aucune
donnée collectée, aucun export et aucune donnée client ne sont versionnés : **le dépôt est
public**.

## Reste à faire par l'équipe

- Renseigner les 43 `release_date` dans `config/studies/battle_royale.yml`.
- Valider la liste battle royale avec le tag Steam et avec PulluP.
- Trancher sur les 4 jeux dont le battle royale n'est qu'un mode (`role: mode`,
  désactivés par défaut).
- Revoir les `role` (`precursor` / `leader` / `follower`), qui sont une première passe.
- Décider de la durée de conservation des données au regard des règles de l'API.
