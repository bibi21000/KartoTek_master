# Kartotek Master – Flask léger + SQLite (sans ORM)

Application Flask minimaliste qui :

1. Lit une liste de serveurs distants dans `conf/servers.json`.
2. Interroge périodiquement (durée configurable) `GET /api/v1/dbid` sur
   chacun d'eux.
3. Si le `dbid` renvoyé diffère de celui connu en base, télécharge
   l'intégralité des points GPS via `GET /api/v1/gps?start=&offset=`
   (pagination) et les enregistre en SQLite.
4. Si un serveur disparaît de `conf/servers.json`, supprime automatiquement
   toutes ses données (points GPS + état) de la base au cycle suivant.
5. Affiche une carte OpenStreetMap (Leaflet) dynamique, centrée sur la
   position de l'utilisateur, avec recherche de lieu et affichage des
   points GPS enregistrés — y compris quand il y en a beaucoup — colorés
   par serveur d'origine, avec une liste (scrollable) des serveurs
   affichés à l'écran.

## Démarrage

```bash
make install   # crée le venv (python3 -m venv venv) et installe l'appli
make run       # lance le serveur de développement Flask
make gunicorn  # lance l'appli en production via gunicorn
```

Voir `make help` pour la liste complète des commandes (test, lint, format, clean...).

L'application écoute par défaut sur `http://0.0.0.0:5000` (voir `conf/config.conf`).

## Structure du projet

```
kartotek-master/
├── pyproject.toml          # packaging + dépendances (flask, requests, extras prod/dev)
├── Makefile                # install, run, gunicorn, test, lint, format, clean
├── gunicorn.conf.py        # config gunicorn, lue depuis conf/config.conf
├── scripts/run_gunicorn.sh # script de lancement prod
├── conf/
│   ├── config.conf         # tous les paramètres (configparser)
│   └── servers.json        # liste des serveurs distants à interroger
├── data/                   # base SQLite (créée au premier lancement)
├── logs/                   # logs applicatifs + gunicorn
└── src/kartotek_master/
    ├── __init__.py
    ├── app.py       (factory Flask + entrée console `kartotek-master`)
    ├── wsgi.py      (entrée gunicorn : kartotek_master.wsgi:app)
    ├── db.py        (accès SQLite brut)
    ├── poller.py    (synchronisation périodique en arrière-plan)
    ├── colors.py    (couleur déterministe par serveur)
    ├── templates/index.html
    └── static/
```

## Fichiers de configuration

| Fichier | Rôle |
|---|---|
| `conf/config.conf` | Tous les paramètres (intervalle de synchro, ports, timeouts, section `[gunicorn]`…), lu via `configparser`. |
| `conf/servers.json` | Liste des serveurs distants à interroger, sous la forme `[{"name": "...", "url": "..."}, ...]`. |

Ces deux fichiers, ainsi que `data/` et `logs/`, sont cherchés à la racine
du projet (le répertoire courant au lancement). Le chemin de base peut
être surchargé avec la variable d'environnement `KARTOTEK_MASTER_HOME`, et
le chemin exact du fichier de config avec `KARTOTEK_MASTER_CONF` (utilisée
à la fois par l'appli Flask et par `gunicorn.conf.py`).

## Choix techniques et pourquoi ils sont « robustes » et économes

- **Pas de SQLAlchemy** : accès direct via `sqlite3` (stdlib), une connexion
  par thread, mode `WAL` pour permettre des lectures pendant l'écriture du
  poller, insertions en une seule transaction (`executemany`).
- **Poller isolé et résilient** : chaque serveur est traité indépendamment ;
  une erreur réseau sur l'un n'affecte pas les autres ni les cycles
  suivants. Les requêtes HTTP sortantes ont un timeout et un mécanisme de
  retry configurables.
- **Nettoyage automatique** : à chaque cycle, le poller compare la liste
  de `conf/servers.json` à celle connue en base ; tout serveur disparu voit
  ses points GPS et sa ligne d'état supprimés. Si le fichier est
  introuvable ou mal formé, aucune suppression n'a lieu (on ne purge que
  sur une lecture réussie, même si la liste résultante est vide).
- **Beaucoup de points GPS** : la carte ne charge jamais toute la table.
  L'API `/api/points` filtre par bounding box (zone visible) et, tant que
  le zoom est faible, renvoie des **clusters agrégés côté SQL** (`GROUP BY`
  sur des coordonnées arrondies et le serveur d'origine) plutôt que des
  milliers de points bruts. Au-delà du seuil de zoom configuré
  (`cluster_zoom_threshold`), les points bruts sont renvoyés, mais toujours
  plafonnés par `max_points_returned`.
- **Couleur par serveur** : chaque point/cluster est coloré selon une
  couleur dérivée d'un hash de l'URL du serveur (stable, cohérente entre
  la carte et la légende).
- **Rendu carte en Canvas** (`preferCanvas: true`) plutôt qu'en SVG :
  bien plus léger avec de nombreux marqueurs.
- **Recherche** : proxifiée côté serveur vers Nominatim (OpenStreetMap)
  pour respecter sa politique d'usage (en-tête `User-Agent`) et éviter les
  soucis CORS côté navigateur.
- **Démarrage/arrêt propres** : le poller tourne dans un thread daemon,
  s'arrête proprement sur `SIGTERM`/`SIGINT`, et ne démarre qu'une seule
  fois même avec le reloader Flask en mode debug.

## Format de `conf/servers.json`

```json
[
  { "name": "serveur1", "url": "http://serveur1.example.com" },
  { "name": "serveur2", "url": "http://serveur2.example.com" }
]
```

- `name` est le libellé affiché dans la liste des serveurs sur la carte.
- `url` est l'URL de base utilisée pour interroger `/api/v1/dbid` et
  `/api/v1/gps`. Dans la liste des serveurs affichés, `name` est un lien
  cliquable vers `url/map/`.
- Rétrocompatibilité : une simple liste de chaînes (`["http://...", ...]`)
  reste acceptée ; le nom affiché est alors l'URL elle-même.
- **Si un serveur est retiré de ce fichier**, ses données (points GPS et
  état) sont supprimées de la base au prochain cycle de synchronisation.

## Format attendu des API distantes

- `GET /api/v1/dbid` → `{"dbid": "..."}` (ou une valeur brute JSON/texte).
- `GET /api/v1/gps?start=0&offset=100` →
  ```json
  {
    "count": 42,
    "total": 150,
    "cards": [
      { "id": "1", "lat": 46.749, "lon": 5.620 },
      { "id": "2", "lat": null, "lon": null }
    ]
  }
  ```
  - `count` : nombre de résultats dans cette page.
  - `total` : nombre total de cartes connues côté serveur, **avec ou sans**
    coordonnées — sert à savoir quand arrêter la pagination (le poller
    s'arrête quand la somme des `count` reçus atteint `total`, ou à défaut
    dès qu'une page renvoie moins de résultats que `offset`).
  - `cards` : les cartes de la page. Seules celles avec `lat`/`lon` non
    nuls sont géolocalisées sur la carte ; les autres sont ignorées côté
    GPS (elles comptent dans `total` mais pas dans les points affichés).
  - Rétrocompatibilité : l'ancien format `[[lat, lon], ...]` (simple liste
    de paires) est toujours accepté.
- **Resynchronisation** : à chaque changement de `dbid`, les anciens points
  du serveur sont purgés avant réinsertion des nouveaux — évite toute
  accumulation de doublons au fil des cycles.

## Internationalisation

L'appli détecte automatiquement la langue du navigateur (`Accept-Language`)
parmi celles supportées (`fr`, `en` — français par défaut). Un petit
sélecteur **FR / EN** dans le bandeau du logo permet de forcer la langue
(mémorisée en session via `?lang=fr` ou `?lang=en`).

Pour ajouter une langue ou mettre à jour les traductions après avoir
modifié des textes :

```bash
# 1. Extraire les chaînes traduisibles (Python + templates)
pybabel extract -F babel.cfg -o src/kartotek_master/translations/messages.pot .

# 2a. Nouvelle langue (exemple : espagnol)
pybabel init -i src/kartotek_master/translations/messages.pot -d src/kartotek_master/translations -l es

# 2b. Ou mettre à jour une langue existante après extraction
pybabel update -i src/kartotek_master/translations/messages.pot -d src/kartotek_master/translations

# 3. Traduire les msgstr vides dans src/kartotek_master/translations/<langue>/LC_MESSAGES/messages.po

# 4. Compiler (obligatoire avant de lancer l'appli)
pybabel compile -d src/kartotek_master/translations
```

N'oubliez pas d'ajouter le code de la nouvelle langue à `SUPPORTED_LOCALES`
dans `app.py`, et un lien dans `templates/partials/header.html`.

## Formulaire de contact

En plus de la section `[contact]` (voir plus haut), le formulaire utilise
les sessions Flask (`flash()`) et nécessite donc une `secret_key` dans la
section `[flask]` de `conf/config.conf`. Si elle est vide, une clé
aléatoire est générée au démarrage (l'appli fonctionne, mais les sessions
ne survivent pas à un redémarrage) — définissez une valeur fixe pour un
déploiement durable.

## Indexation / SEO

- **`/sitemap.xml`** : généré dynamiquement — les pages du site (`/`, `/info/`,
  `/contact/` si activé) plus une entrée `<url>/map/` pour chaque serveur
  connu de `conf/servers.json` (le site fait office d'annuaire de
  collections). `<lastmod>` reprend la date de dernière synchro du serveur
  quand elle est connue.
- **`/robots.txt`** : autorise l'exploration générale, référence le
  sitemap, et exclut `/status/`.
- **Vérification de propriété de site** (Google Search Console, Bing,
  Yandex...) : déposez le fichier fourni par l'outil dans
  `data/verification/` (extension `.html` ou `.txt` uniquement), il devient
  accessible à `https://votre-domaine/<nom-du-fichier>` — ces outils
  exigent un accès à la racine du domaine. Aucun autre fichier de ce
  répertoire, ni d'ailleurs sur le serveur, n'est exposé par cette route.

## Page d'état des synchronisations

**`/status/`** liste, pour chaque serveur, la date de dernière vérification
et de dernière synchro réussie, le `dbid` connu, le nombre de points, et la
dernière erreur le cas échéant. Elle n'est **volontairement liée nulle
part** dans le site (pas dans le header, pas dans le sitemap) — accès
uniquement en connaissant l'URL directement. En renfort, la réponse porte
un en-tête `X-Robots-Tag: noindex, nofollow` et `/status/` est exclue via
`robots.txt` : ça n'empêche pas d'y accéder, mais évite qu'elle finisse
indexée ou explorée par des robots respectueux de ces règles.

## Gunicorn / production

`gunicorn.conf.py` lit ses paramètres (bind, workers, threads, timeout,
logs…) directement dans la section `[gunicorn]` de `conf/config.conf`.
Chaque valeur reste surchargeable individuellement par une variable
d'environnement `KARTOTEK_MASTER_<NOM>` (ex : `KARTOTEK_MASTER_WORKERS=2`),
sans avoir à modifier le fichier de config.

```bash
make gunicorn
# ou directement :
./scripts/run_gunicorn.sh
KARTOTEK_MASTER_CONF=/etc/kartotek-master/config.conf ./scripts/run_gunicorn.sh
```

Par défaut, un seul worker (`gthread`, plusieurs threads) : le thread de
synchronisation démarre par process, donc plusieurs workers dupliqueraient
les cycles de polling. N'augmentez `workers` que si la logique du poller a
été adaptée (verrou externe, process dédié, etc.).

## Aller plus loin en production

- Ajouter un reverse proxy (nginx) devant, notamment pour le cache des
  tuiles OSM si le volume d'usage est important.
- Si le volume de points devient très important (plusieurs millions), une
  table virtuelle `R*Tree` de SQLite peut remplacer le filtre
  `WHERE lat BETWEEN.. AND lon BETWEEN..` pour des requêtes bbox encore
  plus rapides.
