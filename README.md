# QCM Studio (POC)

Créer des QCM imprimables dans le navigateur et les corriger en photographiant les copies.

- **Backend** : FastAPI, sans état, sans base de données — rendu PDF (WeasyPrint) + lecture optique (OpenCV).
- **Frontend** : PWA en JS natif (pas de build), installable ; QCM, résultats et clés stockés dans le navigateur.

## Lancer

```bash
uv sync
uv run uvicorn app.main:app --reload --reload-include .env
# http://localhost:8000
```

Tests (génère un PDF, simule des copies photographiées de travers / bruitées, relit) :

```bash
uv run pytest
```


### Génération par IA (facultatif)

```bash
cp .env.example .env   # puis renseigner OPENROUTER_API_KEY (et éventuellement le modèle)
```

Consigne libre + documents joints (PDF, texte, photos de cours). Les PDF texte sont
extraits localement ; les PDF scannés et les photos sont lus par le modèle (vision). Le
modèle répond en JSON structuré ; l'enseignant relit, choisit les questions à garder, puis
tout reste modifiable dans l'éditeur. `AI_ACCESS_CODE` protège vos crédits si le serveur
est exposé. `OPENROUTER_BASE_URL` permet un autre point d'accès compatible OpenAI (ex. Ollama).

> Pendant la génération, le corrigé transite par le serveur et le fournisseur d'IA
> (aucune donnée élève n'est envoyée). Le reste de l'app ne lui envoie jamais le corrigé.

> **HTTPS obligatoire hors localhost** : WebCrypto (chiffrement du QR), le service worker et
> l'installation PWA ne fonctionnent qu'en contexte sécurisé. Derrière Caddy/Traefik, ou en
> test sur le LAN : `tailscale serve`, `cloudflared tunnel`, ou un certificat `mkcert`.

## Déploiement (Docker + Cloudflare Tunnel)

L'image est construite et publiée par GitHub Actions sur `ghcr.io/ralphi2811/proto-qcm`
(`.github/workflows/ci.yml`) : tests → construction → test de fumée du conteneur →
publication (`latest` pour `main`, `sha-xxxxxxx`, et `1.2.0` / `1.2` pour un tag `v1.2.0`).
Les pull requests sont testées et construites, sans publication.

Sur le serveur (Docker + Compose), seuls `docker-compose.yml` et `.env` sont nécessaires :

```bash
cp .env.example .env    # OPENROUTER_API_KEY, AI_ACCESS_CODE, CLOUDFLARE_TUNNEL_TOKEN…
docker compose pull && docker compose up -d
```

1. **Tunnel** : Cloudflare Zero Trust → Networks → Tunnels → *Create a tunnel*
   (Cloudflared) → copier le jeton dans `CLOUDFLARE_TUNNEL_TOKEN`. Onglet *Public
   Hostname* : `qcm.mondomaine.fr` → service `HTTP` `app:8000`. Aucun port à ouvrir ; le
   HTTPS fourni par Cloudflare suffit pour la caméra, le chiffrement et l'installation PWA.
2. **Image** : le paquet GHCR est privé par défaut. Le rendre public (GitHub → Packages →
   proto-qcm → *Package settings* → *Change visibility*) ou faire `docker login ghcr.io`
   sur le serveur avec un jeton `read:packages`.
3. **Sécurité** : l'app n'a pas de comptes. Définir `AI_ACCESS_CODE` (sinon n'importe qui
   peut consommer les crédits OpenRouter) ; pour restreindre tout l'accès, ajouter une
   application Cloudflare Access (e-mail à usage unique) devant le nom d'hôte.
   Le conteneur tourne sans root, en lecture seule. Cloudflare limite les envois à 100 Mo
   (PDF de scanner : 80 Mo max côté app).

**Mise à jour automatique**, au choix :

- *Par la CI (SSH)* : variable de dépôt `DEPLOY_ENABLED=true`, variable `DEPLOY_PATH`
  (dossier du compose sur le serveur, `DEPLOY_PORT` si ≠ 22), secrets `DEPLOY_HOST`,
  `DEPLOY_USER`, `DEPLOY_SSH_KEY` (clé privée dédiée) et `DEPLOY_KNOWN_HOSTS`
  (`ssh-keyscan -p 22 hote`). Chaque push sur `main` fait `docker compose pull && up -d`.
  Nécessite un SSH joignable depuis GitHub.
- *Par le serveur (sans SSH entrant, adapté au tunnel)* : une tâche cron
  `*/10 * * * * cd /srv/qcm && docker compose pull -q app && docker compose up -d`.

Construction locale : `docker build -t ghcr.io/ralphi2811/proto-qcm:latest .` puis
`docker compose up -d --pull never`.

## Fonctionnement

```
Navigateur (enseignant)                       Serveur (sans état)
───────────────────────                       ───────────────────
éditeur ──► chiffre le corrigé (AES-GCM) ──►  /api/pdf  : HTML → PDF (sujet + grilles + QR)
photo   ──────────────────────────────────►  /api/scan : repères → homographie → cases
        ◄── texte brut du QR + taux de noircissement ──┘
déchiffre le QR avec sa clé, note, enregistre localement
```

- **Le serveur ne voit jamais le corrigé** : il reçoit les intitulés sans les bonnes réponses,
  et un QR déjà chiffré. Il peut donc être mutualisé.
- **Deux modes de réponse** (réglage par QCM) :
  - **sur le sujet** (par défaut) : l'élève coche sous chaque question. Chaque page porte
    4 repères, un QR propre (n° d'exemplaire, page, position exacte de chaque case relevée
    au rendu, corrigé chiffré) et le cartouche Nom/Prénom/Classe en page 1. Propositions
    courtes côte à côte, longues en colonne. Exemplaires **numérotés** (pages regroupées
    automatiquement, dans n'importe quel ordre) ou **non numérotés** pour photocopier
    (pages regroupées dans l'ordre des photos) ;
  - **grille séparée** : le sujet + une grille de réponses (utile pour les longs QCM).
- **Taille du texte** (Grande / Moyenne / Petite) indépendante de la taille des cases ;
  l'éditeur affiche le nombre de pages du sujet (calculé sur la vraie mise en page) et
  propose la taille qui fait gagner une page.
- 4 repères carrés aux coins + le QR en haut à gauche, qui donne l'orientation (photo à
  l'envers ou pivotée de 90° OK).
- **QR code** : correction d'erreur niveau H (~30 % de la surface récupérable), données en
  Base45 (mode alphanumérique, le plus dense), ~50 à 140 caractères selon le QCM.
  Contenu : en-tête clair (géométrie de la grille + id) puis corrigé, barème et mode de
  notation chiffrés en AES-128-GCM avec la clé de l'enseignant.
- **Identité** : cartouche Nom / Prénom / Classe à cadres fixes. L'OMR renvoie le recadrage
  de chaque cadre ; l'IA le lit et le rapproche de la liste de classe (Réglages), les
  champs restent modifiables.
- **Deux tailles de cases** : grandes (6 mm, pour le primaire, ~60 questions) ou standard
  (4 mm, ~130 questions). La taille est inscrite dans l'en-tête du QR.
- **Gestes acceptés** : case cochée ✓, croix ou noircie → lue ; case **entourée** → signalée
  « douteuse » (non comptée, l'enseignant valide d'un toucher). Exemples « coche / n'entoure
  pas » imprimés sur la grille. Seuils en surface d'encre (mm²), indépendants de la taille.
- **Cases douteuses** signalées en jaune ; un toucher sur une case inverse la lecture.

## Arborescence

```
app/
  sheet.py      géométrie de la grille et de l'en-tête de page (rendu ET OMR)
  subject.py    mode réponses sur le sujet (rendu 2 passes, relevé des cases)
  pageqr.py     partie « page » du QR (exemplaire, page, positions des cases)
  payload.py    format du QR (Base45, en-tête)
  render.py     HTML → PDF (WeasyPrint, réseau désactivé)
  omr.py        lecture optique
  ai.py         génération de QCM via OpenRouter (documents, prompt, validation)
  templates/exam.html
static/         PWA (index.html, js/, css/, sw.js, manifest)
tests/          tests bout en bout avec photos simulées
```

## Limites connues / pistes

- OMR côté serveur uniquement (portage possible en OpenCV.js / WASM pour un mode hors-ligne).
- Mode grille : une seule page de grille (jusqu'à ~130 questions à 4 choix).
- localStorage (~5 Mo) : passer à IndexedDB si beaucoup d'images.
- À venir : sujets mélangés A/B (le QR par copie le permet déjà), formules (KaTeX),
  mode scan en rafale (enchaîner les copies sans fermer la caméra).
