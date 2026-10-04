from __future__ import annotations

import json
from pathlib import Path

import hmac

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, omr, render, sheet
from .payload import PayloadError

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

STATIC = Path(__file__).resolve().parent.parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024
MAX_PDF_UPLOAD = 80 * 1024 * 1024  # PDF d'une classe entière passée au scanner
MAX_PDF_PAGES = 200

app = FastAPI(title="Proto QCM", version="0.1.0")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/capacity")
def capacity(n_options: int = 4, style: int = 0):
    return {"max_questions": sheet.capacity(n_options, style)}


@app.post("/api/pdf")
async def make_pdf(req: render.PdfRequest):
    try:
        pdf = await run_in_threadpool(render.render_pdf, req)
    except (sheet.LayoutError, PayloadError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="qcm.pdf"'})


@app.post("/api/layout")
async def layout(req: render.LayoutRequest):
    """Nombre de pages du sujet selon la taille du texte (aperçu dans l'éditeur)."""
    try:
        return {"pages": await run_in_threadpool(render.count_pages, req)}
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/scan")
async def scan(image: UploadFile = File(...), params: str | None = Form(None)):
    data = await image.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image trop lourde")
    fallback = json.loads(params) if params else None
    try:
        return await run_in_threadpool(omr.scan, data, fallback)
    except (omr.ScanError, sheet.LayoutError) as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/qr/decode")
async def qr_decode(image: UploadFile = File(...)):
    data = await image.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image trop lourde")
    try:
        text = await run_in_threadpool(omr.read_qr_text, data)
    except omr.ScanError as e:
        raise HTTPException(422, str(e)) from e
    if not text:
        raise HTTPException(422, "Aucun QR code trouvé sur la photo")
    return {"text": text}


@app.post("/api/scan-pdf")
async def scan_pdf(file: UploadFile = File(...), params: str | None = Form(None)):
    """PDF issu d'un scanner / d'une imprimante : chaque page est analysée comme une photo.
    Réponse en flux NDJSON, une ligne par page, pour afficher la progression."""
    data = await file.read(MAX_PDF_UPLOAD + 1)
    if len(data) > MAX_PDF_UPLOAD:
        raise HTTPException(413, "PDF trop lourd")
    fallback = json.loads(params) if params else None
    try:
        n = await run_in_threadpool(omr.pdf_page_count, data)
    except omr.ScanError as e:
        raise HTTPException(422, str(e)) from e
    if n > MAX_PDF_PAGES:
        raise HTTPException(413, f"PDF trop long ({n} pages, maximum {MAX_PDF_PAGES})")

    async def pages():
        for i in range(n):
            line = {"page": i + 1, "n_pages": n}
            try:
                line["scan"] = await run_in_threadpool(omr.scan_pdf_page, data, i, fallback)
            except (omr.ScanError, sheet.LayoutError) as e:
                line["error"] = str(e)
            yield json.dumps(line) + "\n"

    return StreamingResponse(pages(), media_type="application/x-ndjson")


@app.get("/api/ai/status")
def ai_status():
    cfg = ai.config()
    return {"enabled": cfg.enabled, "model": cfg.model if cfg.enabled else None, "needs_code": bool(cfg.access_code)}


def _ai_config(code: str | None) -> ai.Config:
    cfg = ai.config()
    # code d'accès facultatif : protège les crédits OpenRouter si le serveur est exposé
    if cfg.access_code and not hmac.compare_digest((code or "").encode(), cfg.access_code.encode()):
        raise HTTPException(403, "Code d'accès IA incorrect")
    return cfg


@app.post("/api/ai/generate")
async def ai_generate(
    options: str = Form(...),
    files: list[UploadFile] = File(default=[]),
    x_ai_code: str | None = Header(default=None),
):
    cfg = _ai_config(x_ai_code)
    try:
        opts = ai.GenOptions.model_validate_json(options)
    except ValueError as e:
        raise HTTPException(422, "Paramètres invalides") from e
    if len(files) > ai.MAX_FILES:
        raise HTTPException(422, f"{ai.MAX_FILES} fichiers maximum")
    loaded = []
    for f in files:
        data = await f.read(ai.MAX_FILE_BYTES + 1)
        loaded.append((f.filename or "document", data))
    try:
        return await ai.generate(opts, loaded, cfg)
    except ai.AiError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # réseau, timeout…
        raise HTTPException(502, f"Échec de l'appel à l'IA : {e.__class__.__name__}") from e


@app.post("/api/ai/read-names")
async def ai_read_names(req: ai.NamesIn, x_ai_code: str | None = Header(default=None)):
    """Lecture du cartouche manuscrit (Nom, Prénom, Classe), rapprochée de la liste de classe."""
    cfg = _ai_config(x_ai_code)
    if any(len(v) > 400_000 for v in req.fields.values()):
        raise HTTPException(413, "Recadrage trop lourd")
    try:
        return await ai.read_names(req, cfg)
    except ai.AiError as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Échec de l'appel à l'IA : {e.__class__.__name__}") from e


class QrIn(BaseModel):
    text: str = Field(max_length=1000)


@app.post("/api/qr.svg")
def qr_svg(body: QrIn):
    return Response(render.qr_svg(body.text), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
def service_worker():
    # servi à la racine pour que sa portée couvre toute l'app
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


class RevalidatedStatic(StaticFiles):
    """Fichiers statiques revalidés à chaque requête (304 via ETag) : une mise à jour
    de l'app est visible immédiatement, sans cache navigateur périmé."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", RevalidatedStatic(directory=STATIC, html=True), name="static")
