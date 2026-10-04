# Reprise de session — QCM Studio (état au 2026-10-04)

## État
POC fonctionnel de bout en bout, **rien n'est commité** (repo sans aucun commit).
12 tests passent (`uv run pytest`). Parcours testé dans le navigateur : éditeur → PDF → photo
simulée → OMR → note → résultats.

## Relancer
```bash
uv sync
uv run uvicorn app.main:app --reload --reload-include .env   # http://localhost:8000 (PC seul)
uv run uvicorn app.main:app --host 0.0.0.0      # accessible depuis le LAN
```
Smartphone : **HTTPS obligatoire** (WebCrypto, service worker, installation PWA) →
`tailscale serve 8000` ou `cloudflared tunnel --url http://localhost:8000`.
Puis Réglages → « Transférer vers un smartphone » pour copier la clé de correction.

## Choix d'architecture (validés en session)
- Serveur sans état et sans BDD ; tout est stocké dans le navigateur (localStorage).
- Corrigé chiffré **dans le navigateur** (AES-128-GCM, clé de l'enseignant) puis mis dans le QR
  en Base45, avec correction d'erreur H. Le serveur ne reçoit jamais les bonnes réponses.
- En-tête du QR en clair (8 octets : version, nb questions, nb options max, octet réservé = 0,
  id d'examen) → le serveur en déduit la géométrie de la grille.
- Grille de réponses **séparée** du sujet (sujet multipage). 4 repères aux coins ;
  le QR en haut à gauche donne l'orientation.
- **Pas de n° élève** (retiré le 2026-10-04, décision utilisateur) : identification par
  Nom / Prénom / Classe manuscrits. Cadres à coordonnées fixes (`sheet.NAME_FIELDS`) ;
  `/api/scan` renvoie `name_fields` (recadrage JPEG de chaque cadre), affiché à la correction
  au-dessus de champs à saisir → **brancher l'OCR ici** pour pré-remplir.
- Statiques servis avec `Cache-Control: no-cache` + service worker « réseau d'abord » :
  une mise à jour est visible au rechargement suivant.
- OMR côté serveur (OpenCV) : repères → homographie → recalage local de chaque case →
  surface d'encre à l'intérieur (mm², indépendant de la taille de case) : cochée ≥ 1,7 mm²,
  douteuse ≥ 0,7 mm² (`app/omr.py`).
- **Public CM2** (2026-10-04) : les enfants cochent ou entourent. Coches/croix fines lues ;
  case **entourée** détectée (cadres effacés sauf coins, traits manuscrits isolés en
  composantes connexes, un trait = une case entourée sur ≥ 3 côtés) → « douteuse », non
  comptée, l'enseignant valide d'un toucher. Stress test : 97/97 cercles détectés,
  ~0,3 fausse alerte par feuille dense.
- Taille des cases par QCM (`exam.boxSize` : `large` 6 mm par défaut / `standard` 4 mm),
  inscrite dans l'octet 3 de l'en-tête du QR (`sheet.STYLES`). Bandeau d'exemples
  « ✓ coche / ◯ n'entoure pas » imprimé entre le cartouche et la grille.
- Notation faite dans le navigateur (`static/js/grading.js`) : « tout ou rien » ou
  « partielle » (bonnes − mauvaises), note ramenée sur N.

- **Génération IA** (2026-10-04) : `app/ai.py` + `static/js/ai.js`. OpenRouter, config dans
  `.env` (voir `.env.example` : `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` = défaut
  `anthropic/claude-sonnet-5.5`, option éco `openai/gpt-6-luna`, `AI_ACCESS_CODE`,
  `OPENROUTER_BASE_URL`). Pas d'OCR séparé : PDF texte extraits localement (pypdfium2),
  PDF scannés / photos envoyés en images au modèle multimodal. Sortie JSON structurée
  (repli : JSON extrait du texte), validée/normalisée côté serveur, relue par l'enseignant
  (cases à cocher par question) avant ajout. Testé avec un faux serveur OpenRouter,
  **pas encore avec la vraie API** (pas de clé dans la session).

- **Réponses sur le sujet** (2026-10-04, mode par défaut, `exam.answerMode` = `subject` /
  `grid`) : `app/subject.py` rend le sujet en 2 passes WeasyPrint (passe 1 : relevé de la
  position exacte de chaque `.cb` via l'arbre de mise en page ; passe 2 : fond SVG par page
  via `@page :nth(k)` avec repères + QR + cartouche, sans changer la mise en page, contrôlé).
  QR de page = partie page (`app/pageqr.py` : exemplaire, page, positions des cases au mm)
  + blob chiffré du navigateur (inchangé). Correction : `scanner.js` assemble les pages en
  copies (numérotées : par n° ; non numérotées : dans l'ordre des photos, une page 1 ouvre
  une copie). `/api/scan` renvoie désormais `cells` (liste plate q/o) + `page`.
- Import PDF (scanner / imprimante multifonction) : `POST /api/scan-pdf` rend chaque page à
  200 dpi (pypdfium2) et renvoie un flux NDJSON (une ligne par page : `scan` ou `error`) ;
  le navigateur traite chaque page comme une photo (même regroupement en copies).
- Lecture des noms : `POST /api/ai/read-names` (code d'accès IA) envoie les 3 recadrages du
  cartouche + la liste de classe au modèle OpenRouter, qui renvoie le texte lu, `roster_index`
  et une confiance. Listes de classe dans `qcm.rosters` (Réglages, incluses dans la sauvegarde).
  À la correction : champs pré-remplis (jamais par-dessus une saisie manuelle), autocomplétion
  sur la liste, alerte doublon (même élève déjà corrigé pour ce QCM ou dans une copie ouverte).
  3 lectures simultanées au maximum (PDF de toute une classe).
- Photocopies : un original numéroté puis photocopié donne le même n° sur toutes les copies.
  À la correction, une page déjà présente dans la copie de ce n° ouvre une nouvelle copie
  (regroupement dans l'ordre) avec une alerte, au lieu de remplacer la page. Alertes d'ordre
  suspect : page > 1 sans page 1, page rattachée à une copie antérieure, page 1 arrivée avant
  la fin de la copie précédente. Fenêtre PDF : 1 exemplaire = non numéroté (case désactivée).
- Transfert de clé : Réglages → « Scanner la clé d'un autre appareil » (caméra en direct via
  BarcodeDetector sur Chrome Android, sinon photo décodée par `POST /api/qr/decode`).
- Mode scan (`camera.js` + `markers.js`) : caméra en direct, détection des 4 repères sur le
  téléphone (seuillage adaptatif + composantes carrées + quadrilatère A4 aux angles ~droits),
  guidage (cadrage, distance, lumière, reflet, flou), déclenchement auto après 0,7 s stable,
  lampe si dispo, photo pleine résolution via ImageCapture (sinon image vidéo). Rafale : à faire.
- Déploiement : `Dockerfile` multi-étapes (uv, non-root, healthcheck, fontconfig requis pour
  `fc-match`), `docker-compose.yml` (app en lecture seule + cloudflared, jeton
  `CLOUDFLARE_TUNNEL_TOKEN`, hôte public → http://app:8000), CI `.github/workflows/ci.yml`
  (pytest → build → fumée → GHCR ; déploiement SSH optionnel via `vars.DEPLOY_ENABLED`).
- Mobile : pas de backdrop-filter sur `.topbar` (il retenait la barre d'onglets fixe en haut).

- **Densité** (2026-10-04) : `exam.density` = `large` / `normal` / `compact`
  (`subject.DENSITIES` : police + espacements), envoyée au rendu, n'affecte pas le QR.
  `POST /api/layout` renvoie le nb de pages pour les 3 tailles → compteur de pages et
  suggestion dans la barre de l'éditeur. 10 questions CM2 typiques = 1 page en Petite
  (même avec grandes cases), 2 pages en Moyenne / Grande. Mode grille : non concerné.

- **En-tête aligné sur les repères** (2026-10-04) : `sheet.INNER_TOP` = 8,5 mm (haut des
  repères), `INNER_BOTTOM` = 280 mm, pied de page entre les repères du bas (`FOOTER_Y`).
  +13,5 mm utiles par page en mode sujet. Les grilles séparées imprimées avant ce
  changement ne sont plus lisibles (géométrie calculée, pas stockée dans le QR).

- **Colonnes des propositions** (2026-10-04) : largeur réelle du texte mesurée avec la police
  du PDF (Pillow + fontconfig, `subject.option_columns`) → 4, 3, 2 ou 1 colonnes selon ce
  qui tient, en tenant compte de la taille du texte. Sous-titre placé sur la ligne des
  exemples. QCM histoire CM2 de 10 questions : 2 pages → 1 page (89 %) en Petite.

## Fichiers clés
- `app/sheet.py` : géométrie de la grille, utilisée par le PDF **et** par l'OMR (à modifier ici uniquement)
- `app/payload.py` / `static/js/crypto.js` : format du QR (les deux doivent rester synchronisés)
- `app/omr.py` : lecture optique ; `app/render.py` + `app/templates/exam.html` : PDF
- `static/js/` : editor, scanner, results, settings, store, ui ; `static/sw.js` (penser à
  incrémenter `CACHE` quand les fichiers statiques changent)
- `tests/test_pipeline.py` : génère une copie, simule une photo dégradée, la relit

## À faire / pistes
1. **Tester avec de vraies impressions et de vraies copies d'enfants** (priorité) : coches au
   crayon / stylo bleu clair, cercles, ratures ; ajuster `MARK_MM2` / `UNSURE_MM2` si besoin.
   Décider de la consigne pour se corriger (actuellement : correcteur ; ratures non gérées).
2. Premier commit.
3. Tester la génération IA avec une vraie clé OpenRouter (qualité pour du CM2, PDF scanné,
   photo de cahier) et ajuster le prompt système (`ai.SYSTEM`) si besoin.
4. Sujets A/B mélangés (un QR par copie, déjà compatible).
5. OCR des noms sur `name_fields` (manuscrit en capitales : TrOCR / PaddleOCR côté serveur,
   ou API) + rapprochement flou avec une liste de classe importée (CSV).
6. Formules mathématiques (KaTeX).
7. Passer de localStorage à IndexedDB si beaucoup d'images (quota ~5 Mo).
8. OMR hors-ligne dans la PWA (OpenCV.js / WASM).
9. Grille sur plusieurs pages au-delà d'environ 130 questions à 4 choix.
10. Mode sujet : détecter aussi le texte d'une proposition entouré (pas seulement la case).

## Points d'attention
- Le corrigé est figé dans le QR à l'impression : si on change les réponses, il faut réimprimer.
- Perte de la clé = QR des grilles déjà imprimées illisibles (il reste la correction
  depuis le QCM local). Exporter une sauvegarde depuis Réglages.
- WeasyPrint 70 : utiliser `URLFetcher(allowed_protocols={"data"})`
  (`default_url_fetcher` n'existe plus).
