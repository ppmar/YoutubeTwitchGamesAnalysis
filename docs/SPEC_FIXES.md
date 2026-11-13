# SPEC — Correctifs après revue du tableau de bord

Spec à destination de Claude Code, à déposer dans `docs/SPEC_FIXES.md`. Rédigée le 13 novembre 2026 à partir du commit `abd5179` et de l'état de la page <https://ytc-web-production.up.railway.app/> le même jour.

Les règles de la section 0 de `docs/SPEC.md` s'appliquent à l'identique : aucune attribution à Claude dans les commits, repo public, aucun appel réel à l'API dans les tests, aucun déploiement de ta part, arrêt pour validation après chaque jalon.

Ordre conseillé : cette spec d'abord, puis `docs/SPEC_CALIBRATION.md`. Son jalon C0 peut être traité en même temps que F1.

## Ce que la revue a montré

1. La carte PUBG annonce « Twitch en avance de 33 mois » alors que la requête YouTube mélange PUBG et PUBG Mobile, que 1,5 % des vidéos éligibles ont leurs commentaires, et que les deux courbes ne couvrent pas la même période.
2. La courbe « Reste du genre » additionne les jeux désactivés, dont Minecraft (3,16 milliards d'heures).
3. Le nuage « Monter vite, tomber vite ? » mesure la présence au top 200, pas le déclin, et ignore la censure aux deux bouts de la série.
4. L'API a déclaré le quota d'unités épuisé à 6 655 unités comptées sur 9 500, et la page n'affiche que le compteur de recherche.
5. Au rythme observé (3 773 vidéos pour 92 tranches), l'étude produira environ 65 000 vidéos. Une journée d'unités a couvert les commentaires de 51 vidéos. Collecter les commentaires de toutes les vidéos éligibles est hors de portée.

## Jalon F1 — Un jeu, une correspondance

### 1.1 `title_match` par couple (jeu, vidéo)

Aujourd'hui `title_match` est une colonne de `video_snapshots`, calculée avec la réunion des termes de **tous** les jeux liés à la vidéo (`videos.match_terms_by_video`). Une vidéo trouvée par deux jeux est donc « correspondante » pour les deux dès qu'elle cite l'un des deux.

- Ajouter `title_match` et `match_reason` (terme ayant déclenché, ou terme d'exclusion ayant bloqué) à `game_videos`.
- Tous les consommateurs lisent cette colonne : `attention`, `tables`, `comments.eligible_videos`, l'application web.
- Supprimer `title_match` de `video_snapshots` une fois la migration faite.

### 1.2 Termes d'exclusion

Nouveau champ `exclude_terms` par jeu dans le fichier d'étude et la table `games`. Une vidéo ne correspond à un jeu que si un `match_terms` est présent **et** qu'aucun `exclude_terms` n'apparaît dans le titre ou les tags (pas la description, trop bavarde).

Valeurs initiales à appliquer dans `battle_royale.yml` **et** en base, puisque `prepare-games` n'écrase pas les champs réglables. Prévoir pour cela `ytc prepare-games --force-fields yt_query,match_terms,exclude_terms --games …`.

| Jeu | `yt_query` | `match_terms` | `exclude_terms` |
|---|---|---|---|
| `pubg` | `"PUBG" -mobile` | `PUBG`, `PLAYERUNKNOWN` | `mobile`, `BGMI`, `PUBG Lite`, `New State` |
| `apex-legends` | `"Apex Legends" -mobile` | inchangé | `mobile` |
| `call-of-duty-warzone` | `"Warzone" -mobile` | inchangé | `mobile` |

Retirer `BATTLEGROUNDS` des termes de PUBG : il attrape *Totally Accurate Battlegrounds* et *Battlegrounds Mobile India*. L'opérateur `-` est accepté dans le paramètre `q` de `search.list` ; le vérifier dans la documentation avant de l'utiliser.

### 1.3 Recalcul sans appel réseau

Commande `ytc rematch [--games …]` : recalcule `game_videos.title_match` pour toutes les vidéos déjà collectées, à partir de `raw_responses` (qui contient les descriptions complètes), puis relance `build-tables` et `build-attention`. Aucune requête à l'API.

Les tranches PUBG déjà collectées avec l'ancienne requête sont **conservées** : le recalcul écarte les vidéos mobiles. Ajouter `search_slices.query_version` pour distinguer les tranches selon la requête utilisée, et afficher dans `ytc plan` combien relèvent de l'ancienne. Les relancer est une décision de l'équipe (coût : jusqu'à 115 appels).

### 1.4 Audit des requêtes

Commande `ytc audit-queries` : pour chaque jeu, nombre de vidéos trouvées, taux de correspondance, part écartée par exclusion, et matrice de recouvrement (part des vidéos d'un jeu qui correspondent aussi aux termes d'un autre). Signaler en rouge tout jeu sous 70 % de correspondance ou au-dessus de 10 % de recouvrement. Noms à surveiller : `SOS`, `Scavengers`, `The Culling`, `Darwin Project`, `TETRIS 99`.

**Acceptation.** Une vidéo « PUBG Mobile gameplay » ne correspond plus à `pubg` mais correspond à `pubg-mobile`. Une vidéo liée à deux jeux porte deux valeurs de correspondance indépendantes. `rematch` n'émet aucune requête. L'audit signale un recouvrement simulé.

## Jalon F2 — Graphique Twitch

- « Reste du genre » n'agrège que les jeux `enabled`. Les quatre jeux désactivés sortent de l'agrégat.
- Renommer la série « Reste du genre (35 jeux) », le nombre étant calculé.
- La chronologie et le nuage de points gardent les jeux désactivés en gris, avec une légende qui le dit.

**Acceptation.** Test sur `twitchSeries` ou son équivalent serveur : la somme du reste du genre n'inclut pas Minecraft.

## Jalon F3 — Carte « YouTube contre Twitch »

### Fenêtre commune

Les pics se comparent sur l'intersection des deux séries : du premier mois Twitch valide du jeu (`valid_from` appliqué) au dernier mois du dataset Twitch. La courbe YouTube au-delà reste tracée, estompée, sans participer au calcul du pic.

### Statut de fiabilité, calculé côté serveur

Pour chaque jeu, dans `collect_state` :

- `share_a_at_peak` : part de méthode A au mois du pic YouTube ;
- `view_coverage` : part des vues des vidéos correspondantes du jeu portée par des vidéos à historique de commentaires complet ;
- `slices_done_ratio` : tranches collectées sur tranches prévues pour ce jeu ;
- `status` : `fiable` si `share_a_at_peak ≥ 0,5`, `view_coverage ≥ 0,5` et `slices_done_ratio ≥ 0,9` ; sinon `provisoire`. Seuils dans `settings.yml`.

### Affichage

- Statut `provisoire` : l'étiquette d'écart devient neutre et grisée, « provisoire », suivie de la raison la plus limitante (« 19 % de méthode A au pic », « 12 % des vues couvertes », « collecte à 40 % »). L'écart en mois reste visible dans le sous-titre, pas dans l'étiquette.
- Statut `fiable` : étiquette actuelle.
- Sous la courbe, une bande fine donnant `share_method_a` mois par mois.
- Reformuler le paragraphe d'introduction : l'écart entre pics est une observation, pas la preuve d'un sens de causalité.

**Acceptation.** Avec les données du 13 novembre (PUBG, 19 % de méthode A au pic), la carte s'affiche en `provisoire`. Un pic YouTube situé après le dernier mois Twitch n'est pas retenu.

## Jalon F4 — Forme des cycles

Remplacer la mesure du nuage de points. Calcul côté serveur, dans `tables.py`, exposé dans une table `twitch_game_shape` et un export CSV, pour que l'analyse et la page partagent les mêmes définitions.

Par jeu, sur ses mois valides :

- `peak_month`, `peak_hours` ;
- `rise_months` : du premier mois au top 200 au mois de pic ; `rise_censored = true` si le premier mois est le premier mois du dataset ;
- `half_life_months` : du pic au premier mois à partir duquel le jeu reste **durablement** sous 50 % de son pic, c'est-à-dire sur tous les mois suivants du dataset ;
- un mois absent du top 200 compte comme « sous 50 % » si les heures du 200ᵉ jeu ce mois-là sont inférieures à la moitié du pic ; sinon il est indéterminé et ne déclenche pas la demi-vie ;
- `half_life_censored = true` si le jeu n'est jamais passé durablement sous 50 % avant la fin du dataset ; la valeur stockée est alors une borne inférieure ;
- `presence_after_peak_months` : l'ancienne mesure, conservée pour comparaison.

Affichage : `rise_months` en abscisse, `half_life_months` en ordonnée, point creux quand l'une des deux valeurs est censurée, infobulle disant « au moins N mois ». Graduations rondes. Titre et texte d'introduction réécrits en conséquence.

**Acceptation.** Sur séries synthétiques : un jeu qui tombe à 40 % au mois 5 puis remonte à 60 % au mois 8 n'a pas une demi-vie de 5 ; un jeu encore à 70 % de son pic au dernier mois est censuré ; un jeu présent dès le premier mois a `rise_censored`.

## Jalon F5 — Quota

- Afficher dans l'en-tête et le bloc de synthèse les **deux** compteurs : appels de recherche et unités, utilisés sur limite.
- Quand l'API signale l'épuisement avant le compteur local, enregistrer `units_at_exhaustion` (ou `search_at_exhaustion`) dans `quota_ledger` et afficher un avertissement : « quota épuisé par l'API à 6 655 unités comptées sur 9 500 : cette clé sert probablement ailleurs ».
- `ytc status` affiche le même avertissement.
- README : toute exécution locale avec la même clé consomme le même quota sans apparaître dans le registre de Railway.
- Vérifier au passage, dans `client.py`, que chaque appel `commentThreads.list` est bien compté pour 1 unité, y compris les tentatives en échec.

**Acceptation.** Un 403 `quotaExceeded` reçu alors que le compteur local est sous la limite renseigne `units_at_exhaustion` et déclenche l'avertissement.

## Jalon F6 — Échantillonnage des commentaires

### Règle

Les commentaires ne sont collectés que pour les `comments.top_n_per_slice` vidéos les plus vues (défaut 10) de chaque couple (jeu, tranche), parmi celles qui correspondent au jeu et ont au moins `min_comments` commentaires.

Ordre de traitement : tour de rôle entre les jeux, puis, pour chaque jeu, du mois de pic vers l'extérieur, puis par vues décroissantes dans la tranche.

Les tâches `comments` déjà en file pour des vidéos hors échantillon passent à un nouveau statut `skipped`. Les commentaires déjà collectés sont conservés.

### Coût prévisible

`comment_count` est connu dès l'instantané : le coût se calcule avant de collecter. `ytc plan --comments` affiche, par jeu et au total : vidéos dans l'échantillon, unités estimées (`comment_count / 100`, arrondi au-dessus), nombre de jours au budget d'unités disponible.

### Couverture

Deux mesures, par jeu et au global, exposées dans `collect_state` et affichées dans « Ce qui est en base » :

- en nombre : vidéos à historique complet sur vidéos de l'échantillon ;
- **en vues** : part des vues des vidéos correspondantes portée par des vidéos à historique complet. C'est celle-ci qui renseigne sur la fiabilité du score.

**Acceptation.** Avec 50 vidéos dans une tranche et `top_n_per_slice = 10`, 10 tâches sont créées. Une tâche hors échantillon déjà en file passe en `skipped`. `plan --comments` n'émet aucune requête. La couverture en vues vaut 1 quand la seule vidéo à historique complet porte toutes les vues.

## Jalon F7 — Axes du temps

Dans les graphiques par jeu (`renderCompare`, `renderWho`), la position horizontale d'un point est aujourd'hui son rang parmi les mois qui ont une donnée. Un mois manquant resserre l'axe au lieu de laisser un trou.

Construire un axe mensuel continu du premier au dernier mois affiché, et interrompre le tracé quand un mois manque (plusieurs segments), comme l'exige la règle « un mois absent n'est pas un zéro ».

**Acceptation.** Fonction pure de mise à l'échelle testée : deux points séparés par un trou de 6 mois sont espacés de 6 pas, et le tracé produit deux segments.

## Jalon F8 — Finitions de l'application web

- **Libellé d'état.** Remplacer « collecte en cours » (affiché dès qu'une tranche existe) par un état réel : `en cours` si une tâche est `running`, `en attente du quota` si des tâches restent et qu'un compteur est épuisé, `terminée` s'il ne reste rien, `à l'arrêt` si la dernière exécution date de plus de 36 heures. Stocker l'heure de fin de la dernière exécution de `ytc daily`.
- **Compte à rebours.** « Remise à zéro dans 16 h 50 », sans la mention du fuseau, qui passe dans une infobulle.
- **Cache.** `collect_state` et les exports recalculent tout à chaque requête, dont des comptages distincts sur `comments`, table appelée à contenir des millions de lignes. Mettre en cache le résultat en mémoire pendant `web.cache_seconds` (défaut 300) et remplacer les comptages sur `comments` par une lecture de `video_comment_state` ou `video_comment_days`.
- **Accès.** Authentification HTTP Basic optionnelle : active si `YTC_WEB_USER` et `YTC_WEB_PASSWORD` sont définies, absente sinon. `/healthz` reste ouvert. L'équipe décide de l'activer ou non.

**Acceptation.** Deux requêtes successives dans la fenêtre de cache ne déclenchent qu'un calcul. Avec les deux variables définies, `/` et `/export/*` renvoient 401 sans identifiants.

## Décisions laissées à l'équipe

- Relancer ou non les tranches PUBG avec la nouvelle requête.
- Valeur de `top_n_per_slice` : 10 par défaut, à ajuster après lecture de `ytc plan --comments`.
- Fenêtre des petits jeux : les 26 jeux présents moins d'un an sur Twitch consomment 665 tranches sur 1 600 (42 %). Réduire leur fenêtre (par exemple 6 mois avant, 3 après) libérerait environ une semaine de quota.
- Protection par mot de passe de la page.
