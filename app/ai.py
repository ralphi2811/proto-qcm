"""Génération de QCM par IA via OpenRouter (API compatible OpenAI).

Documents joints :
  - texte / Markdown : envoyés tels quels ;
  - PDF : texte extrait localement (pypdfium2) ; si le PDF est scanné (presque pas de
    texte), ses pages sont envoyées en images au modèle, qui fait office d'OCR ;
  - images : réduites puis envoyées au modèle (vision).

Le modèle répond en JSON structuré, validé et normalisé ici avant d'être renvoyé au
navigateur, où l'enseignant relit et choisit les questions à garder.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

import cv2
import httpx
import numpy as np
import pypdfium2 as pdfium
from pydantic import BaseModel, Field, ValidationError

from .sheet import MAX_OPTIONS

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"

MAX_FILES = 6
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 120_000  # au total, tous documents confondus
MAX_IMAGES = 12  # images envoyées au modèle (photos + pages de PDF scannés)
IMAGE_MAX_SIDE = 1600
SCANNED_PDF_CHARS_PER_PAGE = 80  # en dessous : on considère la page comme scannée


_TRANSPORT: httpx.AsyncBaseTransport | None = None  # injectable pour les tests


class AiError(Exception):
    pass


@dataclass
class Config:
    api_key: str
    model: str
    access_code: str
    base_url: str = DEFAULT_BASE_URL

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def config() -> Config:
    return Config(
        api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        model=os.getenv("OPENROUTER_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
        access_code=os.getenv("AI_ACCESS_CODE", "").strip(),
        base_url=(os.getenv("OPENROUTER_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/"),
    )


# ------------------------------------------------------------------- documents


@dataclass
class Prepared:
    texts: list[tuple[str, str]]  # (nom, contenu)
    images: list[tuple[str, str]]  # (nom, data URL jpeg)
    notes: list[str]  # informations pour l'utilisateur (troncatures…)


def _jpeg_data_url(img: np.ndarray) -> str:
    h, w = img.shape[:2]
    s = min(1.0, IMAGE_MAX_SIDE / max(h, w))
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise AiError("Encodage d'image impossible")
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def prepare_files(files: list[tuple[str, bytes]]) -> Prepared:
    if len(files) > MAX_FILES:
        raise AiError(f"{MAX_FILES} fichiers maximum")
    out = Prepared([], [], [])
    for name, data in files:
        if len(data) > MAX_FILE_BYTES:
            raise AiError(f"« {name} » dépasse {MAX_FILE_BYTES // 1024 // 1024} Mo")
        low = name.lower()
        if low.endswith(".pdf") or data[:5] == b"%PDF-":
            _prepare_pdf(name, data, out)
        elif low.endswith((".txt", ".md", ".markdown", ".csv")):
            out.texts.append((name, data.decode("utf-8", errors="replace")))
        else:
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                raise AiError(f"Format non pris en charge : « {name} » (PDF, texte ou image)")
            _add_image(out, name, img)

    total = sum(len(t) for _, t in out.texts)
    if total > MAX_TEXT_CHARS:
        ratio = MAX_TEXT_CHARS / total
        out.texts = [(n, t[: int(len(t) * ratio)]) for n, t in out.texts]
        out.notes.append(f"Documents longs : seuls ~{int(ratio * 100)} % du texte ont été utilisés")
    return out


def _add_image(out: Prepared, name: str, img: np.ndarray) -> None:
    if len(out.images) >= MAX_IMAGES:
        if not any("images" in n for n in out.notes):
            out.notes.append(f"Seules les {MAX_IMAGES} premières images / pages scannées ont été lues")
        return
    out.images.append((name, _jpeg_data_url(img)))


def _prepare_pdf(name: str, data: bytes, out: Prepared) -> None:
    try:
        doc = pdfium.PdfDocument(data)
    except Exception as e:  # noqa: BLE001 - pdfium lève des types variés
        raise AiError(f"PDF illisible : « {name} »") from e
    pages_text = []
    scanned = []
    for i in range(len(doc)):
        page = doc[i]
        txt = page.get_textpage().get_text_range().strip()
        if len(txt) >= SCANNED_PDF_CHARS_PER_PAGE:
            pages_text.append(f"[page {i + 1}]\n{txt}")
        else:
            scanned.append(i)
    if pages_text:
        out.texts.append((name, "\n\n".join(pages_text)))
    for i in scanned:  # pages scannées -> images pour le modèle (OCR)
        bmp = doc[i].render(scale=150 / 72).to_numpy()
        img = cv2.cvtColor(bmp, cv2.COLOR_RGB2BGR if bmp.shape[2] == 3 else cv2.COLOR_RGBA2BGR)
        _add_image(out, f"{name} p.{i + 1}", img)


# ---------------------------------------------------------------------- prompt


class GenOptions(BaseModel):
    prompt: str = Field(default="", max_length=4000)
    n_questions: int = Field(default=10, ge=1, le=60)
    n_options: int = Field(default=4, ge=2, le=MAX_OPTIONS)
    multiple: bool = False  # plusieurs bonnes réponses possibles
    level: str = Field(default="", max_length=100)


SYSTEM = """Tu es un enseignant expérimenté qui rédige des QCM en français.
Règles :
- Respecte exactement le nombre de questions et de propositions demandé.
- Une question = une seule notion, formulée clairement, adaptée au niveau indiqué.
- Les mauvaises propositions (distracteurs) sont plausibles : erreurs typiques d'élèves,
  confusions fréquentes. Jamais de proposition absurde ou humoristique.
- Interdits : « toutes les réponses », « aucune des réponses », doubles négations,
  questions pièges sur un détail de formulation.
- Les propositions sont courtes, homogènes (même forme, longueur proche) et leur ordre
  ne trahit pas la bonne réponse (varie la position de la bonne réponse).
- Si des documents sont fournis, les questions portent sur leur contenu et uniquement
  dessus ; n'invente pas d'informations absentes des documents.
- Texte brut uniquement (pas de Markdown ni de HTML). Les formules simples s'écrivent
  en ligne (ex. 3/4, x², 2 × 5).
- Pour chaque question, une explication courte (1 phrase) justifie la bonne réponse :
  elle sert à l'enseignant pour vérifier.
Réponds uniquement avec l'objet JSON demandé."""


def _schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "questions"],
        "properties": {
            "title": {"type": "string", "description": "Titre court du QCM"},
            "questions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "options", "explanation"],
                    "properties": {
                        "question": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["text", "correct"],
                                "properties": {"text": {"type": "string"}, "correct": {"type": "boolean"}},
                            },
                        },
                        "explanation": {"type": "string"},
                    },
                },
            },
        },
    }


def build_messages(opts: GenOptions, prep: Prepared) -> list[dict]:
    consigne = [
        f"Rédige un QCM de {opts.n_questions} questions, avec exactement {opts.n_options} propositions par question.",
        (
            "Chaque question a au moins une bonne réponse, et peut en avoir plusieurs."
            if opts.multiple
            else "Chaque question a exactement UNE bonne réponse."
        ),
    ]
    if opts.level:
        consigne.append(f"Niveau des élèves : {opts.level}.")
    if opts.prompt.strip():
        consigne.append(f"Demande de l'enseignant :\n{opts.prompt.strip()}")
    if prep.texts or prep.images:
        consigne.append("Appuie-toi sur les documents fournis ci-dessous.")

    content: list[dict] = [{"type": "text", "text": "\n".join(consigne)}]
    for name, txt in prep.texts:
        content.append({"type": "text", "text": f"--- Document « {name} » ---\n{txt}\n--- Fin du document ---"})
    for name, url in prep.images:
        content.append({"type": "text", "text": f"Image : « {name} »"})
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]


# ------------------------------------------------------------------ résultat


class OptOut(BaseModel):
    text: str
    correct: bool = False


class QuestionOut(BaseModel):
    question: str
    options: list[OptOut]
    explanation: str = ""


class GenOut(BaseModel):
    title: str = ""
    questions: list[QuestionOut]


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise AiError("Réponse de l'IA illisible (pas de JSON)")
    return json.loads(text[start : end + 1])


def normalize(raw: dict, opts: GenOptions) -> tuple[GenOut, list[str]]:
    try:
        out = GenOut.model_validate(raw)
    except ValidationError as e:
        raise AiError("Réponse de l'IA au mauvais format") from e
    warnings: list[str] = []
    kept = []
    for i, q in enumerate(out.questions, 1):
        q.question = q.question.strip()
        q.options = [o for o in q.options if o.text.strip()][:MAX_OPTIONS]
        n_ok = sum(o.correct for o in q.options)
        if not q.question or len(q.options) < 2 or n_ok == 0:
            warnings.append(f"Question {i} écartée (incomplète ou sans bonne réponse)")
            continue
        if not opts.multiple and n_ok > 1:
            warnings.append(f"Question {i} : plusieurs bonnes réponses alors qu'une seule était demandée — à vérifier")
        kept.append(q)
    if not kept:
        raise AiError("L'IA n'a produit aucune question exploitable")
    out.questions = kept
    if len(kept) != opts.n_questions:
        warnings.append(f"{len(kept)} questions obtenues pour {opts.n_questions} demandées")
    return out, warnings


# --------------------------------------------------------------------- appel


async def generate(opts: GenOptions, files: list[tuple[str, bytes]], cfg: Config | None = None) -> dict:
    cfg = cfg or config()
    if not cfg.enabled:
        raise AiError("Génération IA non configurée (OPENROUTER_API_KEY absente du .env)")
    if not opts.prompt.strip() and not files:
        raise AiError("Donnez une consigne ou au moins un document")

    prep = prepare_files(files)
    body = {
        "model": cfg.model,
        "messages": build_messages(opts, prep),
        "temperature": 0.4,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "qcm", "strict": True, "schema": _schema()},
        },
    }
    res = await _chat(cfg, body, timeout=240)
    raw, data = res["raw"], res["data"]
    out, warnings = normalize(raw, opts)
    usage = data.get("usage") or {}
    return {
        "title": out.title,
        "questions": [q.model_dump() for q in out.questions],
        "warnings": prep.notes + warnings,
        "model": data.get("model", cfg.model),
        "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cost") if k in usage},
    }


def _api_error(res: httpx.Response) -> str:
    try:
        err = res.json().get("error", {})
        msg = err.get("message") or res.text[:300]
    except ValueError:
        msg = res.text[:300]
    hints = {
        401: "clé OpenRouter invalide",
        402: "crédits OpenRouter insuffisants",
        404: "modèle introuvable (vérifiez OPENROUTER_MODEL)",
        429: "trop de requêtes, réessayez dans un instant",
    }
    hint = hints.get(res.status_code)
    return f"OpenRouter {res.status_code}" + (f" ({hint})" if hint else "") + f" : {msg}"


# ------------------------------------------------------------ lecture des noms

MAX_ROSTER = 80
NAME_FIELDS = ("nom", "prenom", "classe")

NAMES_SYSTEM = """Tu lis l'écriture manuscrite d'élèves (souvent des enfants) dans le cartouche d'une copie.
On te donne une image par champ : Nom, Prénom, Classe. Recopie ce qui est écrit, sans inventer.
Champ vide ou illisible : chaîne vide.
Si une liste de classe est fournie, indique dans roster_index le numéro (à partir de 0) de l'élève
qui correspond à ce qui est écrit, en tolérant les fautes d'orthographe, les lettres mal formées,
l'inversion nom/prénom ou un prénom seul s'il est unique dans la liste ; -1 si aucun ne correspond
de façon convaincante. Ne choisis jamais un élève au hasard.
confidence : "high" si la lecture (ou la correspondance) ne fait aucun doute, "medium" si probable,
"low" sinon. Réponds uniquement en JSON."""


class NamesIn(BaseModel):
    fields: dict[str, str]  # champ -> JPEG en base64 (recadrage renvoyé par /api/scan)
    roster: list[str] = Field(default_factory=list, max_length=MAX_ROSTER)


def _names_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["nom", "prenom", "classe", "roster_index", "confidence"],
        "properties": {
            "nom": {"type": "string"},
            "prenom": {"type": "string"},
            "classe": {"type": "string"},
            "roster_index": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
    }


def build_names_messages(req: NamesIn) -> list[dict]:
    content: list[dict] = []
    labels = {"nom": "Nom", "prenom": "Prénom", "classe": "Classe"}
    for k in NAME_FIELDS:
        jpeg = req.fields.get(k)
        if not jpeg:
            continue
        content.append({"type": "text", "text": f"Champ « {labels[k]} » :"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg}"}})
    if req.roster:
        lst = "\n".join(f"{i}. {name.strip()}" for i, name in enumerate(req.roster))
        content.append({"type": "text", "text": f"Liste de classe :\n{lst}"})
    else:
        content.append({"type": "text", "text": "Pas de liste de classe : roster_index = -1."})
    return [{"role": "system", "content": NAMES_SYSTEM}, {"role": "user", "content": content}]


async def _chat(cfg: Config, body: dict, timeout: float) -> dict:
    """Appel OpenRouter ; renvoie {"raw": contenu JSON décodé, "data": réponse complète}."""
    headers = {"Authorization": f"Bearer {cfg.api_key}", "X-Title": "QCM Studio"}
    url = f"{cfg.base_url}/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15), transport=_TRANSPORT) as client:
        res = await client.post(url, json=body, headers=headers)
        if res.status_code == 400 and "response_format" in res.text:
            body = {k: v for k, v in body.items() if k != "response_format"}
            res = await client.post(url, json=body, headers=headers)
    if res.status_code != 200:
        raise AiError(_api_error(res))
    data = res.json()
    try:
        msg = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AiError(_api_error(res)) from None
    if isinstance(msg, list):
        msg = "".join(part.get("text", "") for part in msg if isinstance(part, dict))
    try:
        raw = json.loads(msg)
    except (json.JSONDecodeError, TypeError):
        raw = _extract_json(msg or "")
    return {"raw": raw, "data": data}


async def read_names(req: NamesIn, cfg: Config | None = None) -> dict:
    cfg = cfg or config()
    if not cfg.enabled:
        raise AiError("IA non configurée (OPENROUTER_API_KEY absente du .env)")
    if not any(req.fields.get(k) for k in NAME_FIELDS):
        raise AiError("Aucun cartouche à lire")
    body = {
        "model": cfg.model,
        "messages": build_names_messages(req),
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "noms", "strict": True, "schema": _names_schema()},
        },
    }
    out = (await _chat(cfg, body, timeout=60))["raw"]
    if not isinstance(out, dict):
        raise AiError("Réponse de l'IA illisible")
    idx = out.get("roster_index")
    idx = idx if isinstance(idx, int) and 0 <= idx < len(req.roster) else -1
    conf = out.get("confidence") if out.get("confidence") in ("high", "medium", "low") else "low"
    return {
        **{k: str(out.get(k) or "").strip() for k in NAME_FIELDS},
        "roster_index": idx,
        "confidence": conf,
    }
