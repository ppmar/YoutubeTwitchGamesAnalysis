# SPEC — Genres par NMF sur la matrice jeu × tags

Spec à destination de Claude Code, à déposer dans `docs/SPEC_GENRES.md`. Rédigée le 22 novembre 2026 à partir du commit `6fec822` de `ppmar/YoutubeTwitchGamesAnalysis`.

Elle complète `docs/SPEC.md` sans la remplacer. Les règles de sa section 0 s'appliquent ici à l'identique : aucune attribution à Claude dans les commits, repo public donc aucun secret ni donnée client, aucun appel réel à une API dans les tests, aucun déploiement de ta part, arrêt pour validation à la fin de chaque jalon.

**Cette spec manipule des données client sous accord de confidentialité. Le jalon G0 passe avant tout le reste.**

## 1. Pourquoi cette spec

L'étude battle royale repose aujourd'hui sur une liste de 39 jeux choisie à la main. Pour mesurer l'offre et la demande par genre, il faut une définition du genre qui vienne des données : un genre est une combinaison pondérée de tags Steam, et un jeu peut appartenir à plusieurs genres.

Méthode : matrice jeu × tags pondérée en TF-IDF, factorisée par NMF, `V ≈ W × H` :

- `H` (genres × tags) décrit chaque genre par un poids sur chaque tag ;
- `W` (jeux × genres), normalisée par ligne, donne la part de chaque genre dans chaque jeu.

`W` sert ensuite de clé de passage : toute mesure par jeu (sorties, heures Twitch, attention YouTube, ventes) se convertit en mesure par genre en la multipliant par ces parts.

## 2. Entrées : `data/client/`

Deux fichiers fournis par le client, **jamais versionnés, jamais copiés dans l'image Docker, jamais lus par `ytc daily`**. Les traitements de cette spec tournent en local uniquement.

| Fichier | Contenu | Remarques |
|---|---|---|
| `steam_tags.csv` | `nb, tag, appid` : une ligne par couple (jeu, tag) ; `nb` = nombre de votes des joueurs pour ce tag sur ce jeu | ≈ 3,45 M lignes, 206 428 jeux, 477 tags distincts, ≈ 16,7 tags par jeu. Espaces parasites autour de `tag` : faire un `strip`. |
| `steam_reference.csv` | une ligne par jeu : `appid`, `name`, `releaseDate`, `genreNames`, `tagNames`, `copiesSold`, `revenue`, `reviewCount`, `wishlists`, `followerCount`, `ccuMaxAll`, … | 208 016 jeux. Types mixtes sur une colonne : lire avec `low_memory=False`. Les valeurs de ventes, wishlists, etc. sont des instantanés, pas des séries. |

Ces chiffres viennent du notebook d'exploration de l'équipe. Les recalculer au chargement et les journaliser ; un écart important doit faire arrêter le travail et poser la question.

Le chemin du dossier se lit dans `config/genres.yml` (`client_dir`, défaut `data/client`), jamais en dur.

## 3. Jalon G0 — Protection des données client

À faire **avant toute autre modification**, dans un commit à part.

1. Ajouter au `.gitignore`, dans le bloc « Data exports » :
   ```
   # Client data under NDA — never commit, never push
   data/client/
   ```
2. Ajouter `data/client/` au `.dockerignore`.
3. Vérifier qu'aucune donnée client n'est déjà suivie ou dans l'historique :
   - `git ls-files data/client` doit être vide ;
   - `git log --all --oneline -- data/client` doit être vide.

   Si l'une des deux commandes renvoie quelque chose : **ne rien réécrire toi-même** (pas de `git filter-repo`, pas de `push --force`). Arrête-toi et préviens l'utilisateur, le repo étant public.
4. Test `tests/test_client_data_guard.py` :
   - `git check-ignore -q data/client/steam_tags.csv` réussit ;
   - `git check-ignore -q data/client/sous/dossier/x.parquet` réussit ;
   - `data/client` figure dans `.dockerignore`.
   Le test est sauté proprement si le répertoire n'est pas un dépôt git.
5. Mettre à jour le `CLAUDE.md` local (non versionné) : une ligne qui rappelle que `data/client/` est sous accord de confidentialité, et que tout fichier dérivé de ces données va dans `data/processed/genres/`, déjà ignoré.

**Sorties dérivées.** Les matrices, `W`, `H` et les tables par jeu sont dérivées des données client : elles restent dans `data/processed/genres/` (ignoré par le `.gitignore` actuel). Rien de ce qui en sort n'est versionné, y compris les figures. L'équipe décidera après validation par PulluP de ce qui peut être publié.

Critères : les trois vérifications du point 3 sont vides, le test passe, `git status` après copie de fichiers dans `data/client/` ne montre rien.

## 4. Jalon G1 — Matrice jeu × tags

Nouveau sous-paquet `ytcollector/genres/` : `load.py`, `matrix.py`, `nmf.py`, `select.py`, `report.py`, `supply.py`.

**Chargement** (`load.py`). Lire les deux CSV, nettoyer les tags (`strip`), écarter les lignes sans `appid` ou sans `tag`, agréger les doublons (jeu, tag) en sommant `nb`. Joindre `name` et `releaseDate` depuis `steam_reference.csv`.

**Filtre des jeux.** On ne garde que les jeux dont le total des votes de tags est au moins `min_total_votes` (configurable, défaut 100). Journaliser le nombre de jeux retenus.

**Familles de tags : `config/tag_families.yml`.** Les noms de tags Steam sont publics, ce fichier est versionné. Format :

```yaml
families: [gameplay, players, visuals, themes, business, other]
used_for_factorization: [gameplay, players]
tags:
  Battle Royale: gameplay
  Online Co-Op: players
  Pixel Graphics: visuals
  Atmospheric: themes
  Free to Play: business
  # ... les 477 tags
```

Générer une première version avec `ytc genres init-tag-families` : tous les tags en `other`, triés par nombre de jeux. **Ne pas proposer de classement toi-même** : c'est une décision de l'équipe (section 11). Un tag absent du fichier fait échouer `build-matrix` avec la liste des tags manquants.

**Poids** (`matrix.py`).
- TF : part des votes du tag dans le jeu, `nb / Σ nb du jeu`, calculée **sur tous les tags du jeu** avant le filtre par famille.
- Mise à zéro des parts inférieures à `min_tag_share` (défaut 0,02).
- On ne garde que les colonnes des familles listées dans `used_for_factorization`.
- IDF lissé, calculé sur les jeux retenus : `idf = ln((1 + N) / (1 + df)) + 1`.
- Normalisation L2 de chaque ligne. Les lignes devenues vides sont écartées et comptées.
- Stockage en `scipy.sparse` CSR : `data/processed/genres/matrix.npz`, plus `rows.parquet` (`appid`, `name`, `release_date`, `total_votes`) et `cols.parquet` (`tag`, `family`, `df`, `idf`).

Critères : sur une matrice synthétique, les valeurs TF-IDF correspondent à un calcul fait à la main ; chaque ligne est de norme 1 ; aucun appid en double.

## 5. Jalon G2 — NMF sur une grille de k

`sklearn.decomposition.NMF`, avec `init="nndsvda"`, `max_iter` configurable (défaut 500), `beta_loss` et pénalité L1 sur `W` (`alpha_W`, `l1_ratio`) lus dans `config/genres.yml`. Valeurs par défaut : `beta_loss: frobenius`, `alpha_W: 0`. L'équipe comparera ensuite avec `kullback-leibler` et une pénalité L1.

Pour chaque k de la grille (défaut `10:60:5`) et chaque graine (défaut 5 graines) :

- ajuster sur toutes les lignes, sauver `W` et `H` dans `data/processed/genres/fits/k{k}_s{seed}.npz` ;
- ajuster aussi sur `n_bootstrap` sous-échantillons de 80 % des jeux (défaut 5), pour la stabilité.

Indicateurs par k, dans `data/processed/genres/k_selection.csv` :

- `rel_error` : `‖V − WH‖_F / ‖V‖_F`, moyenne et écart-type sur les graines ;
- `stability` : accord entre partitions. Chaque jeu est rangé dans son genre dominant, puis on calcule l'ARI moyen entre toutes les paires d'ajustements (graines et sous-échantillons, sur les jeux communs) ;
- `cophenetic` : corrélation cophénétique de la matrice de consensus (part des ajustements où deux jeux ont le même genre dominant), calculée sur un échantillon de 5 000 jeux tiré avec une graine fixe pour tenir en mémoire ;
- `coherence` : NPMI moyen des 10 premiers tags de chaque genre, calculé sur la présence des tags dans les jeux.

Critères : sur des données synthétiques à structure par blocs connue (par exemple 4 blocs de tags), la grille retrouve k = 4 comme meilleure stabilité ; deux exécutions avec les mêmes graines donnent des résultats identiques.

## 6. Jalon G3 — Genres, appartenance, hiérarchie

`config/genres.yml` porte `k_final: null` tant que l'équipe n'a pas choisi. `ytc genres report --k K` fonctionne pour n'importe quel k de la grille. `select` refuse de produire les tables finales tant que `k_final` est nul.

Pour un k donné, en retenant la graine qui a la plus faible erreur :

- **Normalisation** : chaque ligne de `H` est mise à l'échelle pour que son maximum vaille 1, et `W` est compensée.
- **Appartenance** : parts `share = W_ij / Σ_j W_ij`. Un jeu sans poids est écarté et compté.
- **Nom provisoire** de chaque genre : ses 3 premiers tags, joints par ` + `.
- **Tables** :
  - `genre_definition.parquet` : `genre_id`, `label`, `top_tags` (10 tags et leurs poids), `top_games` (10 appid et noms ayant le plus fort poids), `n_dominant` ;
  - `game_genre.parquet` : `appid`, `genre_id`, `share`, une ligne par couple dont la part dépasse 0,01.
- **Seuils** : pour chaque τ de `membership_thresholds` (défaut `[0.2, 0.3, 0.5]`), le nombre de jeux par genre avec `share ≥ τ`.
- **Hiérarchie** : entre deux k consécutifs de la grille, relier chaque genre au genre le plus proche du k précédent (similarité cosinus entre lignes de `H`). Sortie : `genre_tree.csv` (`k`, `genre_id`, `parent_k`, `parent_genre_id`, `similarity`).
- **Rapport** Markdown `data/processed/genres/report_k{K}.md` : tableau des genres, top tags, top jeux, effectifs par seuil.

Critères : les parts de chaque jeu somment à 1 ; sur les données synthétiques, chaque bloc correspond à un genre et la hiérarchie relie correctement deux blocs fusionnés à k plus petit.

## 7. Jalon G4 — Raccord avec l'étude battle royale

1. Ajouter un champ optionnel `steam_appid` aux jeux de `config/studies/battle_royale.yml` et au modèle `games`, avec une migration Alembic. Les appid sont publics, ils peuvent être versionnés.
2. `ytc genres suggest-appids --study battle_royale` : rapprochement approché entre `game_name` (ou `twitch_names`) et `steam_reference.name`. La commande écrit `data/processed/genres/appid_suggestions.csv` (`game_id`, `candidate_appid`, `candidate_name`, `score`) **sans modifier le YAML**. C'est l'équipe qui recopie les appid validés. Les jeux absents de Steam (versions mobiles, Fortnite) restent à `null`, et c'est normal.
3. `ytc genres check-study --study battle_royale --k K` : pour chaque jeu de l'étude qui a un `steam_appid`, ses deux genres principaux et leurs parts ; le genre qui concentre le plus de jeux de l'étude ; comparaison avec le simple critère « porte le tag Battle Royale », en nombre de jeux de l'étude retrouvés et en nombre de jeux du genre hors de la liste.

Critères : `prepare-games` conserve un `steam_appid` saisi à la main, comme pour `yt_query` ; `suggest-appids` ne touche ni au YAML ni à la base.

## 8. Jalon G5 — Offre par genre

`ytc genres supply --k K` :

- `genre_supply_month.parquet` : `genre_id`, `year`, `month`, `n_releases_weighted` (somme des `share` des jeux sortis ce mois-là), `n_releases_dominant` ;
- même table par année ;
- les jeux sans `releaseDate` exploitable sont comptés à part, jamais imputés.

La demande par genre (Twitch, YouTube, ventes) est hors du périmètre de ce jalon. La clé de jointure est prête : `steam_appid` → `game_genre`.

## 9. Commandes

Sous-commande `ytc genres`, sans aucun appel réseau et absente de `ytc daily` :

```sh
ytc genres init-tag-families            # brouillon de config/tag_families.yml
ytc genres build-matrix                 # G1
ytc genres fit --k 10:60:5 --seeds 5    # G2
ytc genres report --k 25                # G3, pour un k de la grille
ytc genres select                       # G3, tables finales pour k_final
ytc genres suggest-appids --study battle_royale   # G4
ytc genres check-study --study battle_royale --k 25
ytc genres supply --k 25                # G5
```

Chaque commande échoue avec un message clair si `data/client/` est absent ou vide, sans trace d'appel ni chemin absolu de la machine.

## 10. Dépendances et tests

- `scikit-learn` et `scipy` dans un extra `genres` du `pyproject.toml` (`pip install -e ".[dev,genres]"`), pour ne pas alourdir l'image Railway. Les imports se font à l'intérieur de `ytcollector/genres/`.
- **Aucun test ne lit `data/client/`.** Tous les tests utilisent des données synthétiques générées dans les fixtures (structure par blocs, votes tirés avec une graine fixe).
- Les tests de la sous-commande sont sautés si `scikit-learn` n'est pas installé.

## 11. Limites à écrire dans le README

- **Les tags sont ceux d'aujourd'hui.** Les votes s'accumulent avec le temps, et certains tags ont été créés puis appliqués après coup à des jeux plus anciens (par exemple Battle Royale). Pour décrire le passé, c'est acceptable. Pour une prédiction « sans information future », c'est une fuite d'information.
- **La fiabilité des tags dépend de la popularité** : un jeu peu joué a peu de votants. Le seuil `min_total_votes` réduit ce biais sans le supprimer.
- **L'IDF dépend des jeux retenus** : changer le filtre change les poids, donc les genres.
- **La NMF n'a pas de solution unique** : d'où la grille de graines et la mesure de stabilité. Les genres d'un k peu stable ne doivent pas être interprétés.
- **Les noms de genres sont provisoires** tant que l'équipe et PulluP ne les ont pas validés.

## 12. Décisions laissées à l'équipe

- Le classement des tags en familles, et les familles utilisées pour la factorisation (défaut proposé : gameplay et mode de jeu).
- `min_total_votes` et `min_tag_share`.
- La grille de k, puis `k_final`, après lecture de `k_selection.csv` et revue avec PulluP.
- La perte (`frobenius` ou `kullback-leibler`) et la pénalité L1 sur `W`.
- Le ou les seuils d'appartenance τ retenus dans l'analyse.
- Ce qui peut être publié dans le rapport, au regard de l'accord de confidentialité, à voir avec l'encadrant.
