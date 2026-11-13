# Mise en service sur Railway

Guide de déploiement du collecteur. **Ces étapes se font à la main dans le tableau de bord
Railway** : rien ici n'est exécuté automatiquement.

Documentation de référence consultée le 13/11/2026 :
[cron jobs](https://docs.railway.com/reference/cron-jobs),
[restart policy](https://docs.railway.com/deployments/restart-policy),
[config as code](https://docs.railway.com/config-as-code).

---

## 0. Pourquoi un service cron et pas un service permanent

La collecte utile dure quelques minutes par jour : le quota `search.list` (100 appels)
s'épuise vite, le reste du temps il n'y a rien à faire. Le conteneur lance `ytc daily`,
ferme ses connexions et **se termine**. C'est une exigence de Railway, pas une préférence :
un service cron doit « execute a task, and exit as soon as that task is finished, not
leaving any resources open ». Un processus qui ne rend pas la main bloque les exécutions
suivantes.

Tout l'état vit dans le PostgreSQL du projet. Le disque du conteneur est éphémère : aucun
volume, pas de SQLite hors tests.

## 1. Prérequis

- Le dépôt contient le `Dockerfile` sur `main` (fait).
- Une clé **YouTube Data API v3** créée dans un projet Google Cloud. Une seule clé, un seul
  projet : multiplier les projets pour contourner le quota viole les
  [Developer Policies](https://developers.google.com/youtube/terms/developer-policies).

## 2. Créer le projet

1. Railway → **New Project** → **Deploy from GitHub repo**.
2. Choisir `ppmar/YoutubeTwitchGamesAnalysis`, branche `main`.
3. Lors de l'autorisation de l'application GitHub de Railway, n'accorder l'accès **qu'à ce
   dépôt** (*Only select repositories*).

Railway déclenche un premier build tout seul. Il échouera ou tournera dans le vide tant que
les variables de l'étape 4 ne sont pas posées — c'est normal, on le laisse faire.

## 3. Ajouter la base

Dans le même projet : **New** → **Database** → **Add PostgreSQL**.

Garder la base dans le même projet que le collecteur, sinon la référence de variable de
l'étape suivante ne sera pas proposée.

## 4. Variables du service de collecte

Service du collecteur → onglet **Variables** :

| Variable | Valeur |
|---|---|
| `YOUTUBE_API_KEY` | la clé, saisie **uniquement ici** — jamais dans le dépôt, jamais en clair ailleurs |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `YTC_STUDY` | `battle_royale` |

`DATABASE_URL` doit être une **référence** à la variable du service Postgres, pas une copie
de la valeur : si Railway fait tourner les identifiants, la référence suit, une copie non.
Saisir littéralement `${{Postgres.DATABASE_URL}}` (remplacer `Postgres` par le nom réel du
service de base si tu l'as renommé).

## 5. Réglages du service

Service du collecteur → onglet **Settings** :

- **Build** : builder `Dockerfile`, chemin `Dockerfile` à la racine. Railway le détecte
  normalement tout seul.
- **Cron Schedule** : `30 8 * * *`.
  Les crons Railway sont en **UTC**. Le quota YouTube repart à minuit heure du Pacifique,
  soit 07:00 UTC (heure d'été) ou 08:00 UTC (heure d'hiver) : 08:30 UTC tombe après la
  remise à zéro toute l'année.
- **Restart Policy** : **Never**.
  Le défaut est *On Failure* avec jusqu'à 10 redémarrages — à proscrire ici : une relance
  automatique après échec rejouerait la collecte et brûlerait du quota pour rien.
- **Pas de domaine public**, **pas de healthcheck** : ce n'est pas un service web, il
  n'écoute sur aucun port.

## 6. Première exécution

La déclencher à la main (**Deploy** / redéploiement du service), puis lire les logs.

Le résumé de `ytc daily` doit afficher : migrations appliquées, jeux chargés, appels
consommés par compteur, tâches restantes, nombre de jours estimés. Le déploiement doit
passer à l'état terminé — s'il reste « actif », le processus ne rend pas la main et il faut
corriger avant de laisser le cron tourner.

Vérifier aussi qu'**aucune URL complète de requête n'apparaît dans les logs** : elle
contiendrait la clé API.

## 7. Suivi quotidien

- Logs du service dans le tableau de bord.
- Ou en local, avec l'URL publique de la base :
  ```sh
  DATABASE_URL="<url publique Postgres>" ytc status
  ```

## 8. Récupérer les données

En local, toujours avec l'URL publique de la base :

```sh
DATABASE_URL="<url publique Postgres>" ytc export --format parquet --out exports/
```

`exports/` est ignoré par git : **les exports ne retournent jamais dans le dépôt**, qui est
public.

## 9. Arrêter la collecte

Vider le champ **Cron Schedule** suffit. La base reste intacte et la collecte peut
reprendre là où elle s'était arrêtée.


## 10. Une seconde base pour les tests

Certains tests portent sur du comportement propre à PostgreSQL — le verrou d'exécution
(advisory lock) et deux exécutions concurrentes — que SQLite ne sait pas reproduire. Ils
ont besoin d'un vrai serveur.

**Ne jamais les pointer sur la base de collecte** : ils créent et détruisent des objets.

1. Dans le même projet Railway : **New** → **Database** → **Add PostgreSQL**. Le renommer
   `Postgres-test` pour ne pas le confondre avec l'autre.
2. Service `Postgres-test` → onglet **Variables** → copier `DATABASE_PUBLIC_URL`.
   C'est l'URL du proxy TCP (`…proxy.rlwy.net:PORT`), joignable depuis ta machine.
   `DATABASE_URL` pointe sur `…railway.internal`, qui ne répond qu'à l'intérieur de
   Railway : elle ne marchera pas en local.
3. La poser en local dans `.env` :
   ```
   TEST_DATABASE_URL=postgresql+psycopg://postgres:…@…proxy.rlwy.net:12345/railway
   ```
   Remplacer le préfixe `postgresql://` donné par Railway par `postgresql+psycopg://`.
4. Lancer la suite :
   ```sh
   pytest
   ```

Sans `TEST_DATABASE_URL`, les tests PostgreSQL sont **sautés** et le reste passe quand
même. La suite refuse de démarrer si `TEST_DATABASE_URL` est égale à `DATABASE_URL`.

Garde-fou supplémentaire : ces tests créent leurs tables dans un schéma jetable
`test_<aléa>`, détruit à la fin. Même pointés par erreur sur la mauvaise base, ils ne
touchent pas aux tables de collecte, qui vivent dans `public`.

Ce service ne sert qu'aux tests : pas de cron, pas de variables à y ajouter.

---

## Comportements Railway à connaître

- **L'heure d'exécution dérive** de quelques minutes. Sans importance ici : la marge entre
  la remise à zéro du quota et 08:30 UTC est d'au moins trente minutes.
- **Chevauchement** : si l'exécution précédente tourne encore à l'heure prévue, Railway
  saute la suivante et ne tue rien. Une collecte bloquée fait donc perdre des jours en
  silence — surveiller les logs.
- **Fréquence minimale** : deux exécutions doivent être espacées d'au moins 5 minutes.
- Le saut d'exécution de Railway ne protège que le cron. Le **verrou d'exécution**
  (advisory lock PostgreSQL, spec §8) reste nécessaire pour les lancements manuels
  concurrents.

## Ce qu'on ne fait pas

**Ne pas créer de `railway.json` ni de `railway.toml`.** Ce mécanisme (« Config as Code »)
est déprécié, fermé aux nouveaux services, et cesse de fonctionner le **1ᵉʳ décembre
2026**. Les réglages du service se font dans le tableau de bord, comme décrit ci-dessus.

Le remplaçant (« Infrastructure as Code », fichier `.railway/railway.ts`) est optionnel :
ne l'ajouter que sur demande, après lecture de
<https://docs.railway.com/infrastructure-as-code>.

## En local, sans Railway

```sh
cp .env.example .env            # renseigner YOUTUBE_API_KEY
docker compose up -d db
docker compose run --rm collector ytc daily --dry-run
```

`--dry-run` applique les migrations et n'émet aucune requête réseau.
