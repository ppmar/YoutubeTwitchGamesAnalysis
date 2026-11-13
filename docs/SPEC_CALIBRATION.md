# SPEC — Calibration du score d'attention

Spec à destination de Claude Code, à déposer dans `docs/SPEC_CALIBRATION.md`. Rédigée le 13 novembre 2026 à partir du code du commit `f71c7e3` de `ppmar/YoutubeTwitchGamesAnalysis`.

Elle complète `docs/SPEC.md` sans la remplacer. Les règles de sa section 0 s'appliquent ici à l'identique : aucune attribution à Claude dans les commits, repo public donc aucun secret ni donnée client, aucun appel réel à l'API dans les tests, aucun déploiement de ta part, arrêt pour validation à la fin de chaque jalon.

## 1. Pourquoi cette spec

Le score d'attention répartit les vues cumulées d'une vidéo dans le temps. Dans son état actuel, il a quatre faiblesses qui rendent la date des pics peu fiable.

1. **`r = 1` avance les pics.** Les premiers spectateurs commentent plus que les suivants. Répartir les vues au prorata des commentaires attribue donc trop de poids au début de vie de la vidéo.
2. **La calibration prévue ne peut pas corriger ce biais.** Toutes les vidéos collectées ont plus de 18 mois (la fenêtre s'arrête début 2025). Les instantanés hebdomadaires ne mesureront `r(âge)` que pour des âges élevés, alors que le biais se joue dans les premières semaines.
3. **La résolution mensuelle écrase la dynamique.** L'essentiel des commentaires arrive dans les premiers jours ; en mois, tout cela tient dans un seul point.
4. **Le noyau de la méthode B est fragile.** C'est une moyenne de courbes mensuelles sans correction de censure, estimée sur très peu de vidéos, avec un repli géométrique arbitraire (`0.5^i`). Or il porte la majorité de la masse (`share_method_a` entre 2 % et 33 % sur le pilote).

## 2. Objectifs et limites

À livrer :

- une **cohorte de calibration** : des vidéos récentes suivies dès leur publication ;
- le passage du calcul en **résolution journalière** ;
- un **noyau de décroissance** `k(τ)` estimé proprement, avec correction de censure et comparaison de modèles ;
- un **modèle du taux de commentaire** `ρ(âge)`, d'où `r(âge) = 1 / ρ(âge)` ;
- un **rapport de validation** contre des vues réellement observées, avec le signe du biais sur la date des pics.

Hors périmètre : modèles de type Hawkes, effets propres à chaque vidéo, visualisations nouvelles dans l'application web, toute autre source de données.

Garder en tête : la validation portera sur les 8 premières semaines de vie d'une vidéo. Au-delà, la correction est une extrapolation, et le rapport doit le dire.

## 3. Jalon C0 — Trois défauts à corriger d'abord

Constatés à la lecture du code. Pour chacun : écrire d'abord un test qui échoue, puis corriger.

**3.1 Les indicateurs de commentaires se perdent au rafraîchissement.** `comments_truncated` et `comments_disabled` sont des colonnes de `video_snapshots`, et `comments._mark` ne marque que l'instantané le plus récent. Or `videos.build_snapshot` crée chaque nouvel instantané avec les valeurs par défaut (`False`), et `attention._video_rows` lit le plus récent. Après un `refresh-stats`, une vidéo tronquée redevient donc éligible à la méthode A avec un historique amputé de son début.
Correction : nouvelle table `video_comment_state` (clé `video_id` ; colonnes `disabled`, `truncated`, `complete`, `n_stored`, `oldest_comment_at`, `newest_comment_at`, `last_fetched_at`). `attention` et `comments.eligible_videos` lisent cette table. Migration qui reprend les indicateurs existants (vrai si un instantané quelconque de la vidéo le porte), puis suppression des deux colonnes.

**3.2 Le rafraîchissement hebdomadaire ne se déclenche pas pendant la collecte.** `daily.needs_refresh` compare la date du jour au `max(collected_at)` de toute la table. Tant que la recherche découvre de nouvelles vidéos, un instantané est créé chaque jour, ce maximum est toujours récent, et le rafraîchissement n'a jamais lieu.
Correction : raisonner par vidéo. Rafraîchir celles dont le dernier instantané a plus de `refresh.after_days` jours (défaut 7), en s'appuyant sur `videos.pending_video_ids(stale_before=…)` qui existe déjà.

**3.3 `n_comments_dated` est identique pour tous les mois d'un jeu.** `attention.build` y écrit le total des commentaires du jeu. La spec d'origine demande le nombre de commentaires datés **du mois**.
Correction : compter par mois du commentaire.

## 4. Jalon C1 — Cohorte de calibration

### Principe

Chaque jour, repérer des vidéos publiées dans les dernières 48 heures sur quelques jeux encore actifs, puis les suivre par instantanés rapprochés pendant 8 semaines. On obtient pour chaque vidéo une vraie courbe de vues depuis sa naissance.

### Configuration : `config/cohorts/calibration.yml`

```yaml
cohort: calibration
study: battle_royale
games: [fortnite, apex-legends, call-of-duty-warzone, pubg, fall-guys]   # game_id à vérifier dans battle_royale.yml
enrollment_days: 14          # durée de la phase de recrutement
videos_per_game_per_day: 10
discovery:
  window_hours: 48
  order: viewCount           # voir « biais de sélection »
tracking_days: 56
snapshot_schedule:           # âge en jours -> intervalle en jours
  - {until_age: 14, every: 1}
  - {until_age: 28, every: 2}
  - {until_age: 56, every: 7}
budget:
  search_calls_per_day: 6
  units_per_day: 500
```

### Recrutement (`ytc cohort enroll`)

- Un appel `search.list` par jeu et par jour : `publishedAfter = maintenant − window_hours`, `type=video`, `videoCategoryId=20`, `order` selon la configuration, `maxResults=50`.
- Passer les résultats par `videos.list`, ne garder que ceux avec `title_match = true`, puis les `videos_per_game_per_day` premiers qui ne sont pas déjà dans la cohorte.
- Enregistrer dans `cohort_videos` : `cohort`, `video_id`, `game_id`, `enrolled_at`, `age_hours_at_enrollment`, `discovery_order`, `discovery_rank`, `tracking_until`, `status` (`tracking`, `done`, `lost`).
- Le premier instantané est pris au recrutement.
- Le recrutement s'arrête après `enrollment_days` jours comptés depuis le premier recrutement réussi. Volume attendu : environ 700 vidéos.

**Biais de sélection, à documenter.** `order=viewCount` recrute des vidéos déjà en train de marcher : c'est assumé, car le score d'attention est pondéré par les vues et ce sont ces vidéos qui portent la masse. Prévoir `order: date` comme variante de configuration pour mesurer la sensibilité, sans la lancer par défaut.

### Suivi (`ytc cohort track`)

- À chaque exécution, prendre un instantané (`videos.list`, lots de 50) des vidéos dont l'âge et le dernier instantané le demandent selon `snapshot_schedule`.
- Une vidéo absente de la réponse (supprimée, passée en privé) passe en `lost` après 3 absences consécutives ; ses instantanés sont conservés.
- À `tracking_until`, la vidéo passe en `done` et retombe dans le rafraîchissement hebdomadaire normal.
- Coût : au plus 14 unités par jour.

### Commentaires de la cohorte

Les commentaires étant datés, **une seule collecte complète à la fin du suivi suffit** à reconstituer toute leur chronologie. Pas de collecte quotidienne.

- Collecte complète à `tracking_until`, plus une collecte intermédiaire à 14 jours d'âge pour permettre un premier ajustement.
- Ajouter à `stages/comments.py` un mode **incrémental** : `order=time` renvoie les plus récents d'abord, on s'arrête à la première page qui ne contient que des `comment_id_hash` déjà connus. Il sert à la seconde collecte de la cohorte et à la mise à jour des vidéos anciennes (section 7).

### Intégration à `ytc daily`

La cohorte passe **avant** la recherche principale : un jour de recrutement manqué ne se rattrape pas, alors que la recherche historique peut attendre.

1. `cohort enroll`, dans la limite de `budget.search_calls_per_day` ;
2. `cohort track` ;
3. recherche principale avec les appels restants ;
4. suite inchangée, la collecte de commentaires de la cohorte étant prioritaire sur celle de l'étude, dans la limite de `budget.units_per_day`.

### Isolation

Les vidéos de la cohorte sont liées dans `game_videos` avec `source = 'cohort'`. Elles sont **exclues** de `yt_game_month` et du score d'attention de l'étude : elles servent à estimer des paramètres, pas à mesurer l'attention de 2026.

## 5. Jalon C2 — Résolution journalière

- L'âge d'un commentaire et d'un instantané se compte en **jours** depuis `published_at` (UTC), plus en mois.
- Table `video_comment_days` (`video_id`, `day`, `n_comments`), reconstruite depuis `comments` par `build-tables`. Elle remplace le parcours complet de `comments` fait par `attention._comment_counts`.
- La répartition des vues se calcule au jour, puis s'agrège au mois calendaire pour `yt_game_month`.
- Grille d'âges pour toutes les estimations, en jours, bornes à gauche incluses : `0, 1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90, 120, 180, 270, 365, 545, 730, 1095, 1460, 2190, 2920, 3650`. Elle est définie une seule fois, dans `ytcollector/calibration/grid.py`.
- Invariant conservé et testé : pour chaque vidéo, la somme de l'attention sur tous les jours vaut `view_count`.

## 6. Jalon C3 — Noyau de décroissance `k(τ)`

`k(τ)` est la densité de l'âge auquel arrive un commentaire ; `K(τ)` sa fonction de répartition. Nouveau module `ytcollector/calibration/kernel.py`.

### Données

Vidéos avec historique complet (`video_comment_state.complete`, non tronquées) et au moins 30 commentaires, cohorte comprise. Pour chaque vidéo : le nombre de commentaires par case de la grille, et `T_v`, son âge à la date de collecte des commentaires.

### Censure

Une vidéo de 3 mois n'a pas encore reçu ses commentaires tardifs. Sans correction, la queue de `k` est sous-estimée. Les deux estimateurs ci-dessous la corrigent.

### Estimateur empirique (référence)

Par récurrence arrière sur la grille. Pour chaque case `j`, parmi les seules vidéos observées jusqu'au bout de la case (`T_v ≥ borne droite`) :

`g_j = moyenne sur v de [ commentaires de v dans la case j / commentaires de v jusqu'à la fin de la case j ]`

puis `K(fin de case j−1) = K(fin de case j) × (1 − g_j)`, en partant de `K = 1` à la dernière case qui compte au moins `kernel.min_videos_per_bin` vidéos (défaut 10). Chaque vidéo pèse autant, quelle que soit sa taille.

### Modèles paramétriques

Ajustés par maximum de vraisemblance sur les comptes par case, conditionnellement à `T_v` : la probabilité qu'un commentaire de `v` tombe dans la case `[a, b)` est `(K(b) − K(a)) / K(T_v)`. Chaque vidéo a un poids total de 1 (un commentaire pèse `1 / C_v`).

| Modèle | `K(τ)` | Paramètres |
|---|---|---|
| Exponentiel | `1 − exp(−τ/τ0)` | `τ0` |
| Loi de puissance décalée (Lomax) | `1 − (1 + τ/c)^(−θ)` | `c`, `θ` |
| Weibull | `1 − exp(−(τ/λ)^β)` | `λ`, `β` |
| Lomax + fond uniforme | `π × Lomax(τ) + (1 − π) × τ/H` | `c`, `θ`, `π` (`H` = horizon fixe, 3650 jours) |

Optimisation avec `scipy.optimize`, paramètres en échelle logarithmique, plusieurs points de départ.

### Sélection

- Validation croisée à 5 plis **par vidéo** (jamais par commentaire) : log-vraisemblance moyenne hors échantillon.
- AIC en complément.
- Le modèle retenu est le meilleur en validation croisée. À moins d'un écart-type d'écart, prendre le plus simple.
- Si aucun modèle paramétrique ne bat l'estimateur empirique sur les cases où celui-ci est défini, garder l'empirique et le prolonger par la queue du meilleur modèle.

### Strates

Deux au départ : Shorts et vidéos longues (`is_short_proxy`). Une strate de moins de `kernel.min_videos_per_stratum` vidéos (défaut 30) utilise l'ajustement global. La stratification actuelle (`video_type` × court × tranche de vues) est abandonnée : elle produit des strates quasi vides.

### Utilisation

`spread_by_kernel` répartit les vues selon le `K` retenu, borné à l'âge de la vidéo à la collecte. `average_kernel`, `default_kernel` et `KERNEL_HORIZON_MONTHS` disparaissent. S'il n'existe encore aucun ajustement, le repli est le Lomax avec des paramètres fixés en configuration, signalé par `kernel_source = 'default'`.

## 7. Jalon C4 — Taux de commentaire `ρ(âge)`

`ρ(âge)` est le nombre de commentaires par vue à un âge donné ; `r(âge) = 1 / ρ(âge)` est le poids appliqué aux commentaires dans la méthode A. Seule sa **forme** compte : à l'intérieur d'une vidéo, tout facteur constant s'annule. Nouveau module `ytcollector/calibration/rate.py`.

### Données : une ligne par intervalle entre deux instantanés consécutifs d'une vidéo

- `ΔV` : variation de `view_count` ;
- `ΔC` : **nombre de commentaires de premier niveau datés dans l'intervalle**, lu dans `comments`. C'est la grandeur que la méthode A pondère. Pour les vidéos anciennes, lancer d'abord la collecte incrémentale afin que `comments` couvre l'intervalle ;
- `ΔC_stat` : variation de `comment_count`, conservée pour un contrôle de robustesse. Elle inclut les réponses, donc elle n'est pas l'estimateur principal ;
- âge au milieu de l'intervalle, en jours ; durée de l'intervalle ; strate ; origine (`cohort` ou `study`).

Règles : **garder les intervalles avec `ΔC = 0`** (le code actuel les écarte, ce qui fausse le taux aux âges élevés) ; écarter ceux avec `ΔV ≤ 0`, et compter combien sont écartés.

### Modèle

Régression de Poisson, lien logarithmique, `log(ΔV)` en offset :

`log E[ΔC] = log(ΔV) + α + f(log(1 + âge)) + effet de strate`

- `f` linéaire par morceaux en `log(1 + âge)`, nœuds à 1, 7, 30, 180 et 730 jours. Un nœud sans au moins 50 intervalles de chaque côté est supprimé.
- Erreurs standard robustes groupées par vidéo.
- Si la dispersion de Pearson dépasse 2, passer en binomiale négative.
- Bibliothèque : `statsmodels`.

### Utilisation et garde-fous

- `w_v(jour) = c_v(jour) × r(âge du jour)`.
- En dehors de la plage d'âges observée, `r` est maintenu constant à sa valeur de bord. La plage est enregistrée (`age_support_min`, `age_support_max`).
- Moins de `rate.min_intervals` intervalles (défaut 200) : pas d'ajustement, `r = 1`, et la sortie le dit.
- `calibrate_rate` et `snapshot_deltas` (médiane de ratios par mois d'âge) sont remplacés.

## 8. Jalon C5 — Validation

La cohorte fournit la vérité terrain : pour chaque vidéo, la part de ses vues atteinte à chaque instantané. Module `ytcollector/calibration/validation.py`, commande `ytc validate-attention`.

**Protocole.** Mettre de côté 20 % des vidéos de la cohorte, tirées par vidéo avec une graine fixe. Ajuster noyau et taux sur le reste. Pour chaque vidéo mise de côté, reconstruire la courbe cumulée des vues à partir de ses seules vues finales et de ses commentaires, selon trois variantes : méthode A avec `r = 1`, méthode A calibrée, méthode B.

**Mesures, par variante et par strate :**

- écart absolu moyen entre courbe cumulée prédite et observée, aux dates d'instantané ;
- **erreur signée sur `t50`**, le jour où la moitié des vues est atteinte (prédit − observé, en jours). Une valeur négative signifie que la méthode avance l'attention. C'est la mesure qui compte pour les questions d'avance et de retard ;
- même erreur sur `t80`.

**Sensibilité au niveau des jeux.** Recalculer `yt_game_month` sous chaque variante et produire, par jeu, le mois de pic selon chacune et l'écart en mois entre la plus précoce et la plus tardive. Un pic qui bouge selon la variante n'est pas un résultat.

**Sortie.** Un enregistrement dans `calibration_reports` (JSON : mesures, effectifs, plages d'âges couvertes, identifiants des ajustements) et un résumé lisible sur stdout. Le rapport rappelle toujours que la validation ne couvre que les `tracking_days` premiers jours.

## 9. Tables et colonnes

Nouvelles tables, avec migrations Alembic :

| Table | Clé | Contenu |
|---|---|---|
| `video_comment_state` | `video_id` | État de la collecte des commentaires (3.1) |
| `cohort_videos` | `cohort, video_id` | Recrutement et suivi |
| `video_comment_days` | `video_id, day` | Commentaires par jour |
| `kernel_fits` | `id` | Strate, modèle, paramètres (JSON), log-vraisemblance, AIC, score de validation croisée, effectifs, `selected`, `fitted_at` |
| `rate_fits` | `id` | Famille, coefficients (JSON), nœuds, dispersion, plage d'âges, effectifs, `fitted_at` |
| `calibration_reports` | `id` | Rapport de validation (JSON) |

`yt_game_month` : conserver `attention_raw` et `attention_index` tels quels (version `r = 1`, noyau courant) et **ajouter** `attention_raw_cal`, `attention_index_cal`, `kernel_fit_id`, `rate_fit_id`, `peak_month_spread` (écart en mois entre variantes, rempli par la validation). Les deux versions doivent rester comparables côte à côte.

## 10. Commandes

```
ytc cohort enroll     [--cohort calibration] [--dry-run]
ytc cohort track      [--cohort calibration] [--dry-run]
ytc cohort status     [--cohort calibration]     # recrutées, suivies, perdues, instantanés par vidéo
ytc comments          … [--incremental]
ytc fit-kernel        [--study …]                # ajuste, compare, enregistre, affiche le tableau des modèles
ytc fit-rate          [--study …]
ytc validate-attention [--holdout 0.2] [--seed 0]
ytc build-attention   --calibrated               # utilise les derniers ajustements enregistrés
```

`ytc daily` enchaîne recrutement et suivi comme décrit en section 4, puis relance `fit-kernel` et `fit-rate` une fois par semaine si de nouvelles données existent.

Dépendances à ajouter : `numpy`, `scipy`, `statsmodels`.

## 11. Critères d'acceptation

Tout est testé sur données synthétiques ou réponses mockées.

- **C0.** Un `refresh-stats` ne rend plus éligible à la méthode A une vidéo tronquée. Le rafraîchissement hebdomadaire se déclenche pour une vidéo ancienne même si une autre a été instantanée la veille. `n_comments_dated` diffère d'un mois à l'autre.
- **C1.** Le recrutement ne dépasse jamais son budget d'appels, ne recrute pas deux fois la même vidéo, et s'arrête après `enrollment_days`. Le suivi respecte le calendrier par âge (test avec horloge figée). Une vidéo disparue passe en `lost` à la troisième absence. Les vidéos de cohorte n'apparaissent pas dans `yt_game_month`. La collecte incrémentale s'arrête à la première page entièrement connue.
- **C2.** Somme de l'attention journalière égale à `view_count` pour chaque vidéo. L'agrégat mensuel d'une vidéo dont tous les commentaires tombent le même jour est concentré sur le mois de ce jour.
- **C3.** Sur des commentaires simulés selon un Lomax connu, avec des vidéos d'âges variés : `θ` et `c` retrouvés à 15 % près ; l'estimateur empirique sans correction de censure sous-estime la queue et l'estimateur corrigé non ; la validation croisée choisit le Lomax contre l'exponentiel ; les plis ne séparent jamais les commentaires d'une même vidéo.
- **C4.** Sur des intervalles simulés avec une pente connue : pente retrouvée à 15 % près, y compris avec 40 % d'intervalles à `ΔC = 0` ; une surdispersion simulée déclenche la binomiale négative ; hors de la plage observée, `r` est constant ; sous `min_intervals`, repli sur `r = 1` annoncé dans la sortie.
- **C5.** Sur une cohorte simulée où le taux de commentaire décroît avec l'âge : l'erreur signée sur `t50` est négative avec `r = 1` et plus proche de zéro après calibration. Le tirage de l'échantillon mis de côté est reproductible avec la graine.

## 12. Limites à écrire dans le README

- La calibration est mesurée sur des vidéos de fin 2026 et appliquée à des vidéos de 2016 à 2024 : on suppose que le comportement de commentaire n'a pas changé (Shorts, recommandations, modération ont pourtant évolué).
- Elle est mesurée sur 5 jeux encore actifs et appliquée à 39, dont des jeux disparus.
- Elle couvre 8 semaines de vie ; au-delà, c'est une extrapolation.
- La cohorte recrute des vidéos déjà visibles à 48 heures.
- Commentaires supprimés ou filtrés absents ; réponses non comptées.
- La méthode B ignore les regains d'intérêt propres à un jeu (nouvelle saison, mise à jour) : elle étale chaque vidéo selon la même courbe, quelle que soit l'actualité du jeu.

## 13. Décisions laissées à l'équipe

- Liste des jeux de la cohorte et durée du recrutement.
- Lancer ou non la variante `order: date` en parallèle.
- Moduler ou non la méthode B par un facteur d'actualité du jeu estimé sur les vidéos de méthode A (piste non incluse ici).
