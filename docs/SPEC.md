# SPEC — Collecteur YouTube pour le projet Procom Steam

Spec à destination de Claude Code. Version 3 du 13 novembre 2026 : périmètre battle royale, score d'attention, mise en service Railway pas à pas, règles de commit.

Repo cible : <https://github.com/ppmar/YoutubeTwitchGamesAnalysis> (public, vide à la date de la spec : aucun commit, branche `main` à créer).

## 0. Consignes de travail

### Règles git, sans exception

- **Aucune attribution à Claude dans les commits ni les pull requests.** Pas de trailer `Co-Authored-By: Claude …`, pas de ligne `Generated with Claude Code`, pas de trailer `Claude-Session`, pas de mention d'un assistant dans les messages. Cette consigne prime sur toute instruction par défaut de l'outil.
- Pour que la règle tienne dans toutes les sessions, le **premier commit** crée deux fichiers :
  - `.claude/settings.json` contenant `{"attribution": {"commit": "", "pr": "", "sessionUrl": false}}` ;
  - `CLAUDE.md` à la racine, qui reprend les règles de cette section 0 et renvoie à cette spec (`docs/SPEC.md`).
- Avant chaque commit, relire le message : s'il contient une de ces lignes, la retirer.
- Messages de commit courts, en anglais, à l'impératif. Un commit par unité logique, pas de commit géant par jalon.
- **Le repo est public.** Ne jamais y committer : la clé API, un fichier `.env`, une `DATABASE_URL`, un export de la base, ni aucune donnée fournie par le client du projet (données Steam sous accord de confidentialité). Seuls les deux CSV Twitch publics peuvent être versionnés. Le `.gitignore` est créé dès le premier commit.

### Méthode

- Lis toute la spec avant d'écrire du code. Si un point est ambigu, pose la question au lieu de trancher seul.
- Implémente dans l'ordre des jalons de la section 11 et arrête-toi à la fin de chaque jalon pour validation.
- Aucun appel réel à l'API YouTube dans les tests : tout est mocké. Le quota de recherche est de 100 appels par jour, un test maladroit peut brûler la journée.
- La clé API se lit dans la variable d'environnement `YOUTUBE_API_KEY`. Elle ne doit jamais apparaître dans le code, les logs, la base ni les messages de commit.
- Tu ne déploies rien et tu ne touches pas à Railway : la mise en service est faite à la main par l'utilisateur, à partir du guide que tu rédiges (section 10).
- Code, identifiants et docstrings en anglais ; README en français.

## 1. Contexte et objectif

Projet étudiant (IMT Atlantique) sur l'émergence et la durée des tendances dans le jeu vidéo. On dispose d'un dataset Twitch mensuel et on veut le croiser avec YouTube comme second signal de demande, à côté de Google Trends.

**Périmètre de la première étude : le genre battle royale**, soit 39 titres présents dans le dataset Twitch (section 4). L'outil doit rester générique : une étude est un fichier de configuration, et on doit pouvoir en ajouter une autre (souls-like, friendslop) sans toucher au code.

Questions de recherche que les données doivent permettre de traiter :

1. Une tendance apparaît-elle d'abord sur YouTube ou sur Twitch (avance / retard) ?
2. Les gros créateurs sont-ils à l'origine d'une tendance ou l'amplifient-ils une fois lancée ?
3. Combien de temps l'intérêt dure-t-il après le pic, et que deviennent les jeux arrivés tard ?

Livrable : un outil en ligne de commande, exécuté chaque jour par un cron Railway, qui collecte les vidéos YouTube associées aux jeux de l'étude, leurs statistiques, les chaînes qui les publient et l'horodatage de leurs commentaires, puis calcule un **score d'attention mensuel par jeu** joignable au dataset Twitch sur `(game_id, year, month)`.

## 2. Contraintes de l'API (vérifiées le 13/11/2026)

Sources : <https://developers.google.com/youtube/v3/getting-started> et <https://developers.google.com/youtube/v3/docs/search/list>. Ces valeurs par défaut peuvent changer : ce sont des paramètres de configuration, pas des constantes en dur.

| Contrainte | Valeur | Conséquence |
|---|---|---|
| `search.list` | 100 appels / jour, compteur séparé (chaque page compte pour un appel) | C'est le goulot d'étranglement. |
| Tous les autres endpoints | 10 000 unités / jour au total | Suffisant, mais les commentaires peuvent le saturer. |
| `videos.list`, `channels.list`, `playlistItems.list` | 1 unité pour 50 éléments | Quasi gratuit. |
| `commentThreads.list` | 1 unité pour 100 fils | Environ 1 M de commentaires / jour au maximum. |
| `pageInfo.totalResults` | Approximation, plafonnée à 1 000 000 | À stocker comme indicateur grossier uniquement. |
| `order` autre que `relevance` | Peut renvoyer un ensemble de résultats plus petit ou incomplet | À documenter comme biais. |
| Remise à zéro des quotas | Minuit, heure du Pacifique | Le suivi de quota se cale sur `America/Los_Angeles`. |

Trois limites de fond :

- **Pas d'historique des vues.** `viewCount`, `likeCount`, `commentCount` sont des cumuls à la date de collecte. La seule date native d'une vidéo est `publishedAt`.
- **Pas de compteur exact de vidéos par jeu.**
- **Seul signal réellement daté : les commentaires** (`publishedAt` de chaque commentaire). C'est la base du score d'attention (section 6).

Conformité : une seule clé, un seul projet Google Cloud. Ne pas multiplier les projets pour contourner le quota (YouTube API Services Developer Policies, <https://developers.google.com/youtube/terms/developer-policies>). Ces règles encadrent aussi la durée de conservation des données : la commande `refresh-stats` y répond en partie, et l'équipe doit vérifier ce point avec l'encadrant.

## 3. Entrées

`data/external/Twitch_game_data.csv` et `data/external/Twitch_global_data.csv` (fournis, ne pas modifier, embarqués dans l'image de déploiement).

- `Twitch_game_data.csv` : 21 000 lignes, 105 mois de janvier 2016 à septembre 2024, exactement 200 lignes par mois (top 200 mensuel), 2 359 noms distincts. Colonnes : `Rank, Game, Month, Year, Hours_watched, Hours_streamed, Peak_viewers, Peak_channels, Streamers, Avg_viewers, Avg_channels, Avg_viewer_ratio`. Lire en `latin-1`.
- `Twitch_global_data.csv` : totaux mensuels de la plateforme, utiles pour calculer des parts.

Problèmes de qualité constatés :

- 24 doublons de casse (`NARAKA: BLADEPOINT` / `Naraka: Bladepoint`, `ELDEN RING` / `Elden Ring`…).
- Caractères accentués cassés sous deux formes : séquences littérales `<U+00E9>` (`Pok<U+00E9>mon`) et mojibake (`PokÃ©mon`).
- 1 ligne sans nom de jeu (février 2019, rang 162) : à écarter.
- Catégories qui ne sont pas des jeux (`Just Chatting`, `Music`, `Slots`, `IRL`, `ASMR`…).
- Renommages : un même jeu porte plusieurs noms selon la période (`PLAYERUNKNOWN'S BATTLEGROUNDS` jusqu'en mai 2021, puis `PUBG: BATTLEGROUNDS`).
- **Censure par le seuil** : un jeu absent d'un mois n'a pas une audience nulle, il est sous le 200ᵉ rang. Ne jamais remplir ces mois par 0.

## 4. Étape 0 — Liste des jeux de l'étude (`prepare-games`)

Aucun appel API. Lit `config/studies/<study>.yml` et le CSV Twitch, produit la table `games`.

Traitements communs :

1. Normalisation du nom : remplacer `<U+XXXX>` par le caractère Unicode, réparer le mojibake (`ftfy`), `strip`, NFKC.
2. Clé de regroupement en `casefold`, espaces compressés.
3. Agrégats par jeu depuis Twitch : `hours_watched_total`, `months_in_top200`, `first_month`, `last_month`, `peak_month`, `best_rank`.

Format du fichier d'étude : une entrée par jeu avec `game_id`, `game_name`, `twitch_names` (liste des libellés Twitch à fusionner), `yt_query`, `match_terms`, `release_date` (à renseigner par l'équipe), `role` (`precursor`, `leader`, `follower`, `mode`), `notes`, `enabled`.

### Étude `battle_royale` : contenu initial de `config/studies/battle_royale.yml`

Liste établie par recherche de mots-clés dans le CSV Twitch ; à compléter par l'équipe avec le tag Steam « Battle Royale ».

| Jeu | Libellés Twitch à fusionner | Mois dans le top 200 | Période |
|---|---|---|---|
| Fortnite | `Fortnite`, `Fortnite Zero Build` | 87 | 2017-07 → 2024-09 |
| PUBG | `PLAYERUNKNOWN'S BATTLEGROUNDS`, `PUBG: BATTLEGROUNDS` | 95 | 2016-09 → 2024-09 |
| Apex Legends | `Apex Legends` | 68 | 2019-02 → 2024-09 |
| Call of Duty: Warzone | `Call of Duty: Warzone`, `Call of Duty: Warzone 2.0` | 62 | 2019-08 → 2024-09 |
| PUBG Mobile | `PUBG Mobile` | 73 | 2018-04 → 2024-09 |
| Garena Free Fire | `Garena Free Fire` | 60 | 2019-09 → 2024-08 |
| Fall Guys | `Fall Guys: Ultimate Knockout`, `Fall Guys` | 51 | 2020-07 → 2024-09 |
| H1Z1 / Z1 | `Z1: Battle Royale` | 40 | 2016-01 → 2021-03 |
| Eternal Return | `Eternal Return: Black Survival`, `Eternal Return` | 40 | 2020-10 → 2024-09 |
| Naraka: Bladepoint | `Naraka: Bladepoint`, `NARAKA: BLADEPOINT` | ≤ 22 | 2021-04 → 2024-09 |
| Stumble Guys | `Stumble Guys` | 17 | 2022-05 → 2023-10 |
| Realm Royale | `Realm Royale` | 15 | 2018-06 → 2019-10 |
| Ring of Elysium | `Ring Of Elysium` | 14 | 2018-04 → 2019-08 |

Titres présents moins de 12 mois (les « suiveurs », essentiels pour la question 3) : `The Culling` (11), `Super People` (7), `Darwin Project` (6), `SOS` (6), `Spellbreak` (5), `Farlight 84` (5), `Vampire: The Masquerade - Bloodhunt` (5), `Rumbleverse` (4), `Islands of Nyne: Battle Royale` (4), `Battlerite Royale` (4), `Hyper Scape` (3), `My Hero Ultra Rumble` (3), `TETRIS 99` (3), `Radical Heights` (2), `Totally Accurate Battlegrounds` (2), `Shadow Arena` (2), `Scavengers` (2), `Rules of Survival` (2), `Call of Duty: Warzone Mobile` (1), `Apex Legends Mobile` (1), `CRSED: F.O.A.D.` (1), `Fear the Wolves` (1), `Dying Light: Bad Blood` (1), `ARK: Survival Of The Fittest` (1), `SUPERVIVE` (1), `HeadBangers: Rhythm Royale` (1).

Pièges à consigner dans le champ `notes` et à reprendre dans le README :

- **Warzone** : la catégorie Twitch `Call of Duty: Warzone` commence en août 2019, alors que Warzone sort en mars 2020. Les mois antérieurs correspondent à *Modern Warfare* (2019). Prévoir un champ `valid_from: 2020-03` par jeu, appliqué au moment de la jointure.
- **Fortnite** : la catégorie couvre tous les modes (Save the World, Creative, Zero Build), pas seulement le battle royale.
- **Jeux dont le battle royale n'est qu'un mode** (`role: mode`, `enabled: false` par défaut) : `Call of Duty: Black Ops 4` (Blackout), `Battlefield V` (Firestorm), `Minecraft` (Hunger Games), `Arma 3` (mod de PlayerUnknown). On ne peut pas isoler le mode côté Twitch ; côté YouTube on peut le viser par la requête (`"Blackout" "Black Ops 4"`), ce qui justifie de les garder dans le fichier, désactivés.
- **PUBG** : premières lignes en septembre 2016, avant l'accès anticipé de mars 2017. À vérifier par l'équipe.

Si la table `games` existe déjà, ne jamais écraser `yt_query`, `match_terms`, `enabled` modifiés à la main : fusionner sur `game_id`.

## 5. Pipeline de collecte

### Étape 1 — Découverte des vidéos (`search`)

Unité de travail : une tranche `(game_id, période)`. Un appel `search.list` par tranche et par page.

- Paramètres fixes : `part=snippet`, `type=video`, `videoCategoryId=20` (Gaming), `maxResults=50`, `order=viewCount`, `q=<yt_query>`, `publishedAfter` / `publishedBefore` aux bornes de la tranche (UTC). Pas de `regionCode` ni de `relevanceLanguage` par défaut ; les deux restent configurables.
- Découpage `--slicing {none, year, month}`, **`month` par défaut pour une étude**.
- Fenêtre par jeu : du mois `first_month − 12` au mois `last_month + 6` (bornes configurables, plafonnées à la date du jour). Les 12 mois d'avance servent à tester l'avance / retard ; les 6 mois de queue à observer le déclin.
- `--pages-per-slice` (défaut 1).
- Ordre de traitement : jeux à longue série d'abord (`months_in_top200` décroissant), et pour chaque jeu du mois de pic vers l'extérieur. Une collecte interrompue reste ainsi exploitable.
- Stocker pour chaque tranche : `approx_total_results`, nombre d'éléments renvoyés, présence d'un `nextPageToken`, paramètres exacts, `collected_at`.

Budget indicatif pour `battle_royale` : 728 couples jeu-mois dans Twitch, plus les fenêtres avant et après, soit environ 1 200 à 1 400 appels, donc **12 à 14 jours à 100 appels / jour**. `ytc plan` doit afficher le chiffre exact.

### Étape 2 — Statistiques des vidéos (`videos`)

`videos.list` par lots de 50, `part=snippet,statistics,contentDetails,topicDetails,liveStreamingDetails,status`.

Champs : `video_id`, `channel_id`, `title`, `description` (tronquée à 500 caractères), `tags`, `published_at`, `category_id`, `default_language`, `default_audio_language`, `duration_seconds`, `view_count`, `like_count`, `comment_count`, `topic_categories`, `live_broadcast_content`, `actual_start_time`, `made_for_kids`, `collected_at`.

Champs dérivés :

- `is_short_proxy` : `duration_seconds <= 60`.
- `was_live` : présence de `liveStreamingDetails`.
- `title_match` : un des `match_terms` apparaît dans le titre, les tags ou la description (comparaison sans casse ni accents).
- `video_type` : classification par mots-clés du titre, règles dans `config/video_types.yml` : `trailer`, `gameplay`, `review`, `guide`, `esport`, `other`.

Statistiques masquées : stocker `null`, jamais 0.

### Étape 3 — Chaînes (`channels`)

`channels.list` par lots de 50, `part=snippet,statistics,contentDetails`. Champs : `channel_id`, `title`, `country`, `published_at`, `subscriber_count` (peut être masqué → `null`), `view_count`, `video_count`, `uploads_playlist_id`, `collected_at`.

### Étape 4 — Horodatage des commentaires (`comments`)

Indispensable au score d'attention.

- `commentThreads.list`, `part=snippet`, `maxResults=100`, `order=time`.
- Vidéos concernées : celles avec `title_match = true` et `comment_count >= --min-comments` (défaut 30), traitées par `view_count` décroissant à l'intérieur de chaque jeu, en alternant les jeux pour qu'aucun ne monopolise le quota.
- **L'historique doit être complet** : `order=time` renvoie les plus récents d'abord, donc un plafond ampute le début de vie de la vidéo, qui est justement la partie utile. Plafond `--max-threads-per-video` élevé (défaut 50 000, soit 500 unités). Si le plafond est atteint, marquer `truncated = true` et exclure la vidéo de la méthode par commentaires (elle bascule sur le noyau de décroissance, section 6).
- Reprise en cours de vidéo : mémoriser le `nextPageToken` pour ne pas recommencer une grosse vidéo le lendemain.
- **Minimisation des données** : ne stocker que `video_id`, `comment_published_at`, `like_count`, `reply_count`. Ni auteur, ni identifiant d'auteur, ni texte. Option `--store-text` désactivée par défaut.
- Commentaires désactivés (403 `commentsDisabled`) : marquer la vidéo et continuer.
- Budget : garder `--units-reserve` (défaut 1 000 unités / jour) pour les étapes 2, 3 et `refresh-stats`.

### Étape 5 — Panel de chaînes (`channel-panel`, phase 2)

`playlistItems.list` sur la playlist d'uploads coûte 1 unité pour 50 vidéos, sans toucher au quota de recherche.

- Panel : les chaînes dépassant `--min-subscribers` (défaut 100 000) ou listées dans `config/channel_panel.yml`.
- Récupérer tous leurs uploads, les passer par l'étape 2, puis les rattacher aux jeux par recherche locale des `match_terms` dans le titre.
- Intérêt : comptages mensuels de vidéos par jeu sur un panel fixe, et date de première vidéo de chaque créateur sur chaque jeu (question 2).

### Instantanés (`refresh-stats`)

Relance l'étape 2 sur tous les `video_id` connus et **ajoute** un instantané daté au lieu d'écraser. Exécutée chaque semaine. Double usage : construire une vraie série de vues à partir de maintenant, et calibrer le score d'attention (section 6).

## 6. Score d'attention (`build-attention`)

But : répartir dans le temps les vues cumulées de chaque vidéo, faute d'historique. Le résultat est un **indice relatif**, pas un nombre de vues. Le nommer `attention` partout, jamais `views`.

Notations : vidéo `v`, vues cumulées `V_v`, date de publication `p_v`, nombre de commentaires datés du mois `t` noté `c_v(t)`, total `C_v`.

**Méthode A — par les commentaires** (vidéos avec historique complet et `C_v >= 30`) :

`a_v(t) = V_v × w_v(t) / Σ_t w_v(t)`, avec `w_v(t) = c_v(t) × r(âge)`

où `r(âge)` est le nombre de vues par commentaire en fonction de l'âge de la vidéo. Version 1 : `r = 1` (hypothèse : le taux de commentaire est constant sur la vie de la vidéo).

**Méthode B — noyau de décroissance** (vidéos sans commentaires exploitables) : `a_v(t) = V_v × k(t − p_v)`, où `k` est la courbe moyenne normalisée des vidéos de méthode A, estimée par strate (`video_type` × `is_short_proxy` × tranche de vues).

**Agrégation** : `attention(g, t) = Σ_v a_v(t)` sur les vidéos du jeu `g` avec `title_match = true`. Stocker aussi la part issue de chaque méthode (`share_method_a`) : un mois où elle est faible est moins fiable.

**Calibration (version 2, dès que 4 instantanés hebdomadaires existent)** : les instantanés donnent, pour chaque vidéo, de vraies variations `ΔV` et `Δcommentaires` sur une semaine. On en tire `r(âge)` empirique (médiane de `ΔV / Δcommentaires` par tranche d'âge), qui remplace `r = 1`. Produire aussi un rapport de validation : vues hebdomadaires prédites par la méthode contre vues réellement observées (erreur médiane, corrélation de rang), par strate.

**Sorties complémentaires par `(game_id, year, month)`** : `attention_index` (ramené sur 0–100 par jeu, comme Google Trends, pour la comparaison), `attention_raw`, `n_videos_published`, `n_channels_publishing`, `n_comments_dated`, `share_method_a`, `sample_basis`.

Limites à écrire dans le README : le taux de commentaire baisse en général avec l'âge de la vidéo (les abonnés commentent tôt), ce qui avance artificiellement l'attention tant que la calibration n'est pas faite ; l'échantillon est limité aux 50 vidéos les plus vues par jeu et par mois ; commentaires supprimés et spam ; vues gonflées par les Shorts.

## 7. Sorties

Stockage principal : PostgreSQL. Export Parquet ou CSV à la demande (`ytc export`).

| Table | Clé | Contenu |
|---|---|---|
| `games` | `study, game_id` | Étape 0 |
| `search_slices` | `game_id, period_start, page` | Paramètres, `approx_total_results`, `n_returned`, `collected_at` |
| `game_videos` | `game_id, video_id, period_start` | Lien jeu ↔ vidéo, rang, `source` (`search` ou `panel`) |
| `video_snapshots` | `video_id, collected_at` | Étape 2, un instantané par collecte |
| `channel_snapshots` | `channel_id, collected_at` | Étape 3 |
| `comments` | `video_id, comment_id_hash` | Étape 4 (hash non réversible du `comment_id` pour dédoublonner) |
| `yt_game_month` | `game_id, year, month` | Score d'attention et agrégats, joignable à Twitch |
| `raw_responses` | `request_hash` | Réponse JSON brute compressée, sans la clé API |
| `tasks`, `quota_ledger` | — | État de la collecte |

Agrégats supplémentaires de `yt_game_month` (vidéos `title_match = true`, dernier instantané, par mois de publication) : `views_sum`, `views_median`, `likes_sum`, `comments_sum`, `engagement_rate`, `top_channel_view_share`, `channel_hhi`, `share_shorts`, `share_live`, `share_by_video_type`, `median_subscribers`, `first_big_channel_video_at` (seuil `--big-channel-threshold`, défaut 1 M d'abonnés), `first_video_at`, `trailer_views_max`.

## 8. Quota, cache et reprise

- **Registre de quota** en base, deux compteurs journaliers `search_calls` et `units`, calés sur le fuseau du Pacifique. Limites dans `config/settings.yml` (`search_calls_per_day: 100`, `units_per_day: 10000`), marge de sécurité configurable (défaut 5 %).
- Avant chaque appel : vérifier le budget restant. S'il est épuisé, arrêt propre avec code de sortie 0.
- 403 `quotaExceeded` ou `rateLimitExceeded` : marquer le compteur comme épuisé pour la journée et s'arrêter proprement. Erreurs 5xx et réseau : jusqu'à 5 tentatives avec backoff exponentiel. Toute requête émise compte dans le registre.
- **File de tâches** en base : statut `pending / running / done / failed`, tentatives, dernière erreur. Une tâche restée `running` après un arrêt brutal est remise en `pending` au démarrage suivant.
- **Verrou d'exécution** (advisory lock PostgreSQL) : deux exécutions simultanées ne doivent jamais dépenser le quota en double.
- **Cache brut** : chaque réponse est enregistrée dans `raw_responses`. `build-tables` reconstruit tout sans appel réseau.
- Chaque écriture de lot est transactionnelle.

## 9. Interface en ligne de commande

Point d'entrée `ytc`. Toutes les commandes prennent `--study` (défaut `battle_royale`).

```
ytc prepare-games
ytc plan            [--slicing month] [--games ID,ID] [--limit N]   # tâches, coût par compteur, nombre de jours
ytc search          [--slicing month] [--games ...] [--limit N] [--pages-per-slice 1] [--dry-run]
ytc videos          [--dry-run]
ytc channels        [--dry-run]
ytc comments        [--games ...] [--min-comments 30] [--max-threads-per-video 50000] [--dry-run]
ytc channel-panel   [--min-subscribers 100000] [--dry-run]
ytc refresh-stats
ytc build-tables
ytc build-attention [--calibrated/--no-calibrated]
ytc export          [--format parquet|csv] [--out DIR]
ytc status                                                          # quota restant, avancement par étape
ytc daily                                                           # commande lancée par le cron
```

`ytc daily` enchaîne : `search` (jusqu'à épuisement du compteur de recherche) → `videos` → `channels` → `refresh-stats` si le dernier instantané a plus de 7 jours → `comments` (jusqu'à la réserve d'unités) → `build-tables` → `build-attention`. Elle se termine toujours, journalise un résumé (appels consommés, tâches restantes, jours estimés) et sort en code 0 sauf erreur de configuration ou de base.

## 10. Pile technique, arborescence et déploiement

Python 3.11+, `requests`, `pandas`, `pyarrow`, `typer`, `pyyaml`, `python-dotenv`, `ftfy`, `python-slugify`, `SQLAlchemy` 2 + `psycopg`, migrations `alembic`. Tests : `pytest`, `responses`, SQLite en mémoire ou PostgreSQL de test. Dépendances dans `pyproject.toml`.

```
ytcollector/
  cli.py  config.py  db.py  models.py  games.py  client.py  quota.py  tasks.py
  stages/ search.py videos.py channels.py comments.py panel.py
  tables.py  attention.py  textnorm.py
config/   settings.yml  video_types.yml  channel_panel.yml  studies/battle_royale.yml
data/external/    # CSV Twitch, versionnés
docs/SPEC.md  docs/RAILWAY.md
migrations/  tests/
.claude/settings.json  CLAUDE.md
Dockerfile  docker-compose.yml  README.md  .env.example  .gitignore  .dockerignore
```

### Railway : ce que le code doit garantir

- **Un service de type cron, pas un service permanent** : la collecte utile dure quelques minutes par jour, le quota fait le reste. Le conteneur lance `ytc daily`, ferme ses connexions et **se termine** ; un processus qui ne rend pas la main bloque les exécutions suivantes.
- **État dans le PostgreSQL Railway** du même projet, via `DATABASE_URL`. Ne rien écrire d'important sur le disque du conteneur, qui est éphémère. Pas de SQLite hors des tests unitaires, pas de volume. Les tests du verrou d'exécution tournent sur un vrai PostgreSQL.
- `Dockerfile` à la racine : image `python:3.11-slim`, installation du paquet, copie de `config/` et `data/external/`, utilisateur non root. Commande par défaut : `sh -c "alembic upgrade head && ytc daily"`. `PYTHONUNBUFFERED=1` pour que les logs sortent en direct.
- **Ne pas créer de `railway.json` ni de `railway.toml`.** D'après la documentation Railway consultée le 13/11/2026, ce mécanisme (« Config as Code ») est déprécié, fermé aux nouveaux services, et cesse de fonctionner le 1ᵉʳ décembre 2026. Les réglages du service se font dans le tableau de bord, selon `docs/RAILWAY.md`. Le remplaçant (« Infrastructure as Code », fichier `.railway/railway.ts`) est optionnel : ne l'ajouter que sur demande, après lecture de <https://docs.railway.com/infrastructure-as-code>.
- Logs sur stdout, une ligne par événement, sans jamais inclure l'URL complète des requêtes (elle contient la clé).
- En local : `docker compose up` démarre un Postgres et permet de lancer `ytc` avec les mêmes variables, lues dans `.env`.

### Railway : mise en service (à rédiger dans `docs/RAILWAY.md`)

Ces étapes sont faites à la main par l'utilisateur dans le tableau de bord Railway. Claude Code rédige le guide, il ne les exécute pas. Les libellés d'interface sont à vérifier dans la documentation au moment de la rédaction (<https://docs.railway.com/cron-jobs>).

1. **Prérequis** : le repo contient au moins le `Dockerfile` sur `main` (jalon 6 terminé) ; une clé API YouTube Data v3 a été créée dans un projet Google Cloud.
2. **Créer le projet** : nouveau projet Railway, déploiement depuis le repo GitHub `ppmar/YoutubeTwitchGamesAnalysis`, branche `main`. Autoriser l'application GitHub de Railway sur ce seul repo.
3. **Ajouter la base** : service PostgreSQL dans le même projet.
4. **Variables du service de collecte** :
   - `YOUTUBE_API_KEY` : la clé, saisie dans Railway uniquement ;
   - `DATABASE_URL` : référence à la variable du service Postgres (`${{Postgres.DATABASE_URL}}`), pas une copie de la valeur ;
   - `YTC_STUDY=battle_royale`.
5. **Réglages du service** :
   - build par `Dockerfile` ;
   - **Cron Schedule : `30 8 * * *`**. Les crons Railway sont en UTC. Le quota YouTube repart à minuit, heure du Pacifique, soit 07:00 ou 08:00 UTC selon la saison : 08:30 UTC tombe après la remise à zéro toute l'année ;
   - **politique de redémarrage : Never**. Une relance automatique après échec dépenserait du quota pour rien ;
   - pas de domaine public, pas de healthcheck : ce n'est pas un service web.
6. **Première exécution** : la déclencher à la main, puis lire les logs. Le résumé de `ytc daily` doit afficher les migrations appliquées, les jeux chargés, les appels consommés et le nombre de jours restants.
7. **Suivi quotidien** : logs du service, ou `ytc status` lancé en local avec l'URL publique de la base.
8. **Récupérer les données** : `ytc export --format parquet` en local avec l'URL publique de la base. Les exports restent hors du repo.
9. **Arrêt** : retirer la planification suffit à stopper la collecte ; la base reste intacte.

Comportements Railway à connaître, à rappeler dans le guide : l'heure d'exécution peut dériver de quelques minutes ; si l'exécution précédente tourne encore, la suivante est sautée. Le verrou de la section 8 reste nécessaire pour les lancements manuels.

## 11. Jalons et critères d'acceptation

**Jalon 0 — Initialisation du repo.** Premier commit sur `main` : `.gitignore`, `.dockerignore`, `.claude/settings.json`, `CLAUDE.md`, `docs/SPEC.md` (cette spec), `pyproject.toml`, squelette du paquet, `.env.example`, les deux CSV Twitch dans `data/external/`. Vérification : `git log --format=%B` ne contient aucune ligne `Co-Authored-By`, `Generated with` ni `Claude-Session` ; `git ls-files` ne liste aucun `.env`.

**Jalon 1 — Jeux de l'étude.** `ytc prepare-games` charge `battle_royale.yml` et produit 39 jeux actifs. Tests : les deux libellés PUBG fusionnent en 95 mois ; `NARAKA: BLADEPOINT` et `Naraka: Bladepoint` fusionnent ; `Pok<U+00E9>mon` et `PokÃ©mon` donnent `Pokémon` ; la ligne sans nom est écartée ; `valid_from` de Warzone exclut les mois d'avant mars 2020 à la jointure ; un `yt_query` modifié à la main survit à une seconde exécution.

**Jalon 2 — Base, client, quota, reprise.** Tests mockés : le 101ᵉ appel de recherche de la journée n'est pas émis ; un 403 `quotaExceeded` provoque un arrêt propre ; une relance ne refait pas les tâches terminées ; une tâche `running` orpheline repasse en `pending` ; deux exécutions concurrentes ne dépensent pas deux fois ; la clé API n'apparaît ni dans `raw_responses` ni dans les logs ; le compteur repart à minuit, heure du Pacifique.

**Jalon 3 — Étapes 1 à 3 et tables.** `ytc plan` affiche tranches, coût par compteur et nombre de jours. `--dry-run` n'émet aucune requête. Sur réponses mockées, `build-tables` produit des tables aux clés uniques, et `yt_game_month` se joint au fichier Twitch sans doublon.

**Jalon 4 — Commentaires.** Reprise en cours de vidéo par `nextPageToken` ; plafond atteint → `truncated = true` ; aucun champ auteur ni texte en base ; la réserve d'unités est respectée.

**Jalon 5 — Score d'attention.** Tests sur données synthétiques : pour chaque vidéo, `Σ_t a_v(t) = V_v` ; une vidéo dont tous les commentaires tombent le même mois y concentre toute son attention ; une vidéo tronquée passe en méthode B ; `attention_index` vaut 100 au mois de pic de chaque jeu.

**Jalon 6 — Déploiement.** `Dockerfile`, `docker compose` local fonctionnel, `docs/RAILWAY.md` complet. Tests : l'image se construit ; `docker compose run collector ytc daily --dry-run` applique les migrations, n'émet aucune requête et rend la main avec le code 0 ; le processus se termine même quand le quota est déjà épuisé. Ensuite l'utilisateur suit `docs/RAILWAY.md`, puis lance lui-même le pilote réel : `ytc search --games fortnite,apex-legends --limit 20`, avec contrôle manuel du taux de `title_match` (ajuster `yt_query` sous 70 %).

**Jalon 7 — Panel de chaînes, calibration du score, rapport de validation.**

## 12. Biais à documenter dans le README

- Score d'attention : estimation reposant sur l'hypothèse commentaires ↔ vues (section 6).
- Échantillon limité aux 50 vidéos les plus vues par jeu et par mois.
- Biais de survie : vidéos et chaînes supprimées ou passées en privé.
- Classement décidé par la recherche YouTube actuelle, non reproductible à l'identique.
- Requêtes par nom : faux positifs sur les noms génériques (`SOS`), faux négatifs sur les titres qui ne citent pas le jeu ou le citent dans une autre langue (Free Fire, PUBG Mobile).
- Filtre catégorie Gaming : exclut les vidéos mal catégorisées.
- Côté Twitch : censure par le seuil du top 200, début de série en 2016, catégories mixtes (Warzone, Fortnite), renommages.
- Genre dominé par quatre titres (Fortnite, Warzone, Apex Legends, PUBG : 91 % des heures regardées du genre) : toute moyenne de genre reflète surtout ces quatre-là.

## 13. Hors périmètre

Pas de scraping du site YouTube, pas de téléchargement de vidéos ni de sous-titres, pas d'OAuth, pas de visualisation ni de modélisation au-delà du score d'attention, pas de collecte Google Trends.

## 14. Décisions laissées à l'équipe

- Validation de la liste battle royale avec le tag Steam et avec PulluP ; dates de sortie à renseigner.
- Inclusion ou non des jeux dont le battle royale n'est qu'un mode.
- Stockage ou non du texte des commentaires.
- Durée de conservation des données au regard des règles de l'API.
