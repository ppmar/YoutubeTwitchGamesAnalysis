# YoutubeTwitchGamesAnalysis

Collecteur YouTube pour l'étude Procom (IMT Atlantique) sur l'émergence et la durée des
tendances dans le jeu vidéo. L'outil collecte les vidéos YouTube associées aux jeux d'une
étude, leurs statistiques, les chaînes qui les publient et l'horodatage de leurs
commentaires, puis calcule un **score d'attention mensuel par jeu** joignable au dataset
Twitch sur `(game_id, year, month)`.

Première étude : le genre **battle royale**, 39 jeux actifs du dataset Twitch.

Spécification complète : [`docs/SPEC.md`](docs/SPEC.md).
Correctifs après revue : [`docs/SPEC_FIXES.md`](docs/SPEC_FIXES.md) — non implémentés.
Calibration du score d'attention : [`docs/SPEC_CALIBRATION.md`](docs/SPEC_CALIBRATION.md) — jalon C0 fait, le reste non.
Genres par NMF sur la matrice jeu × tags : [`docs/SPEC_GENRES.md`](docs/SPEC_GENRES.md) — non implémentés.
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

`ytc --help` liste les commandes. Le détail est en section 9 de la spec.

La collecte tourne **automatiquement** chaque jour à 08:30 UTC, par un service cron
Railway (`30 8 * * *`). Pour l'arrêter, vider le champ *Cron Schedule* du service :
la base reste intacte et la collecte reprend où elle s'était arrêtée.

## Genres par NMF (local uniquement)

`docs/SPEC_GENRES.md`. Les entrées sont les deux exports Steam du client, dans
`data/client/`, **sous accord de confidentialité** : rien de tout cela n'est
versionné ni déployé, et `ytc daily` n'y touche jamais.

```sh
pip install -e ".[dev,genres]"     # scikit-learn et scipy, hors image Railway
ytc genres init-tag-families       # brouillon de config/tag_families.yml
ytc genres build-matrix            # matrice jeu x tags en TF-IDF
ytc genres fit --k 10:60:5 --seeds 5   # NMF sur la grille de k
ytc genres report --k 25               # genres, appartenances, rapport Markdown
ytc genres select                      # tables finales, une fois k_final choisi
ytc genres suggest-appids --study battle_royale
ytc genres check-study --study battle_royale --k 25
ytc genres supply --k 25               # sorties par genre, par mois et par an
```

`init-tag-families` écrit les 477 tags en `other`, du plus porté au moins porté :
le classement en familles est une décision de l'équipe, pas de l'outil.
`build-matrix` échoue tant qu'un tag n'y figure pas, et liste ceux qui manquent.
Il échoue aussi si les comptes relevés dans l'export s'écartent de ceux du
notebook d'exploration (`expected:` dans `config/genres.yml`) : c'est voulu,
`--ignore-expected` ne sert qu'une fois l'écart compris.

Réglages dans `config/genres.yml` : `min_total_votes` (défaut 100) et
`min_tag_share` (défaut 0,02). Les deux changent l'IDF, donc les genres.
Sorties dans `data/processed/genres/`, lisibles dans l'onglet *Matrice jeu × tags*
de la page de suivi.

`fit` ajuste chaque k avec plusieurs graines et plusieurs sous-échantillons à 80 %,
écrit un `fits/k{k}_s{graine}.npz` par ajustement et récapitule dans
`k_selection.csv` : erreur relative (moyenne et écart-type), stabilité (ARI moyen
entre partitions dominantes), corrélation cophénétique du consensus et cohérence
NPMI des 10 premiers tags. Aucun de ces quatre chiffres ne choisit `k_final` :
c'est une lecture à faire avec PulluP.

Ordres de grandeur mesurés sur la matrice complète (194 111 jeux × 477 tags, un
cœur) : 17 s pour un ajustement à k = 10, 1 min 40 à k = 30, 5 min à k = 60, plus
~25 s d'indicateurs par k. La grille par défaut (11 valeurs de k, 5 graines et
5 sous-échantillons) demande donc **plusieurs heures** et de l'ordre du gigaoctet
dans `fits/`. Réduire la grille d'abord (`--k 10:30:10 --seeds 2`) est raisonnable.

Avec `init: nndsvda`, la graine ne fait presque rien bouger : la stabilité
mesurée vient surtout des sous-échantillons. `init: random` dans
`config/genres.yml` donne une vraie variation d'une graine à l'autre.

`report --k K` marche pour n'importe quel k de la grille et écrit
`report_k{K}.md` : les genres, leurs 10 tags et 10 jeux les plus lourds, les
effectifs par seuil d'appartenance. `select` écrit les tables sur lesquelles
l'étude se joint (`genre_definition.parquet`, `game_genre.parquet`,
`genre_tree.csv`) et **refuse de tourner tant que `k_final` est `null`** : le
choix de k est une décision, pas un défaut.

`suggest-appids` rapproche les noms des jeux de l'étude de ceux de Steam et
écrit `appid_suggestions.csv` — **sans toucher au YAML ni à la base**. Sur la
liste battle royale : 42 jeux sur 43 reçoivent au moins un candidat, 28 avec un
score de 1,0. Les jeux absents de Steam (Fortnite, PUBG Mobile, Garena Free
Fire…) n'ont que des coïncidences de mots : la commande les signale comme
douteux, et ils restent à `null`, ce qui est le résultat attendu.

`supply --k K` compte les sorties par genre de deux façons : `n_releases_weighted`
répartit chaque jeu au prorata de ses parts, `n_releases_dominant` le compte en
entier dans son genre principal. Attention : l'export écrit **9999** quand il n'a
pas de date — 46 892 jeux, soit 22 % du catalogue. Ces jeux sont comptés à part
et jamais imputés (149 078 datés contre 45 033 sans date exploitable).

## La page de suivi

Une vue web en lecture seule, qui n'écrit rien et n'appelle jamais l'API :

```sh
ytc serve        # http://127.0.0.1:8000
```

Elle est découpée en onglets — Collecte, Twitch, YouTube, Franchises, Les jeux,
Matrice jeu × tags, Données. Sans JavaScript la page reste une seule page qui se
déroule, comme avant. Les graphiques réagissent : la légende du signal Twitch
masque une courbe et recalcule l'échelle, la chronologie se trie et se filtre,
les grilles de cartes et le tableau des jeux ont un filtre et des en-têtes
triables.

L'onglet **Matrice jeu × tags** lit la matrice de `docs/SPEC_GENRES.md`
(`data/processed/genres/`), dérivée des données client sous accord de
confidentialité. Le serveur ne la sert **que** si `YTC_GENRES_LOCAL=1` est posé
dans son environnement : à ne jamais poser sur un service déployé. Sans elle,
l'onglet explique ce qui manque et comment le construire.

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

`data/client/` contient les exports Steam du client, **sous accord de confidentialité** :
ignorés par git et par Docker, jamais lus par `ytc daily`. Tout ce qui en dérive (matrices,
`W`, `H`, figures) reste dans `data/processed/genres/`, lui aussi ignoré.

## Reste à faire par l'équipe

- Renseigner les 43 `release_date` dans `config/studies/battle_royale.yml`.
- Valider la liste battle royale avec le tag Steam et avec PulluP.
- Trancher sur les 4 jeux dont le battle royale n'est qu'un mode (`role: mode`,
  désactivés par défaut).
- Revoir les `role` (`precursor` / `leader` / `follower`), qui sont une première passe.
- Décider de la durée de conservation des données au regard des règles de l'API.
- Classer les 477 tags Steam en familles dans `config/tag_families.yml`, puis choisir
  `used_for_factorization` (défaut proposé par la spec : `gameplay` et `players`).
- Trancher `min_total_votes` et `min_tag_share`, puis la grille de k et `k_final`.
