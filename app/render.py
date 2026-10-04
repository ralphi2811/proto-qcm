"""Génération HTML -> PDF (WeasyPrint) du sujet et de la grille de réponses."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Literal

import segno
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from weasyprint import HTML, URLFetcher

from . import sheet, subject
from .payload import parse_header
from .sanitize import clean_html

TEMPLATES = Path(__file__).parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
env.filters["clean"] = clean_html
env.globals["letter"] = sheet.option_letter


class OptionIn(BaseModel):
    html: str = ""


class QuestionIn(BaseModel):
    html: str = ""
    options: list[OptionIn] = Field(min_length=2, max_length=sheet.MAX_OPTIONS)
    points: float = 1


class ExamIn(BaseModel):
    title: str = "QCM"
    subtitle: str = ""
    instructions: str = ""
    questions: list[QuestionIn] = Field(min_length=1, max_length=255)


class PdfRequest(BaseModel):
    exam: ExamIn
    qr: str  # corrigé chiffré par le navigateur (Base45)
    mode: Literal["subject", "grid"] = "subject"
    # mode « grid » : sujet + grilles de réponses séparées
    subject: bool = True
    sheets: int = Field(default=1, ge=0, le=200)
    # mode « subject » : réponses cochées sur le sujet
    copies: int = Field(default=1, ge=1, le=200)
    density: Literal["compact", "normal", "large"] = "normal"  # taille du texte
    numbered: bool = False  # exemplaires numérotés (regroupement automatique des pages)


# Pas d'accès réseau/fichiers pendant le rendu (évite SSRF / lecture de fichiers locaux) :
# seules les ressources data: (images collées dans l'éditeur, QR) sont autorisées.
_FETCHER = URLFetcher(allowed_protocols={"data"}, fail_on_errors=False)


def _svg_uri(body: str, w: float, h: float) -> str:
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">{body}</svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# Exemples imprimés sur la grille (case de 10 unités, trait de stylo simulé)
_BOX_SVG = '<rect x="3" y="3" width="10" height="10" rx="1" fill="#fff" stroke="#222" stroke-width="0.75"/>'
_EXAMPLE_CHECK = _svg_uri(
    _BOX_SVG + '<path d="M5 8.2 L7.4 11 L12.6 4.6" fill="none" stroke="#000" stroke-width="1.3" '
    'stroke-linecap="round" stroke-linejoin="round"/>',
    16, 16,
)
_EXAMPLE_CIRCLE = _svg_uri(
    _BOX_SVG + '<ellipse cx="8" cy="8" rx="7.3" ry="6.6" fill="none" stroke="#000" stroke-width="0.9"/>'
    '<path d="M22 4.5 L29 11.5 M29 4.5 L22 11.5" stroke="#000" stroke-width="1.6" stroke-linecap="round"/>',
    31, 16,
)


def qr_data_uri(text: str) -> str:
    qr = segno.make(text, error="h", micro=False, boost_error=False)
    return qr.svg_data_uri(border=2, dark="#000", omitsize=True)


def render_html(req: PdfRequest) -> str:
    exam = req.exam
    n_opt = max(len(q.options) for q in exam.questions)
    header = parse_header(req.qr)
    if (header.n_questions, header.n_options) != (len(exam.questions), n_opt):
        raise ValueError("L'en-tête du QR ne correspond pas au QCM")
    style = header.style  # la taille des cases fait foi depuis le QR (lu tel quel à la correction)
    layout = sheet.compute_layout(len(exam.questions), n_opt, style)
    tpl = env.get_template("exam.html")
    return tpl.render(
        exam=exam,
        req=req,
        layout=layout,
        S=sheet,
        n_options=[len(q.options) for q in exam.questions],
        qr_uri=qr_data_uri(req.qr),
        exam_id=header.exam_id,
        large=style == sheet.LARGE,
        check_svg=_EXAMPLE_CHECK,
        circle_svg=_EXAMPLE_CIRCLE,
    )


class LayoutRequest(BaseModel):
    exam: ExamIn
    box_size: Literal["standard", "large"] = "large"


def count_pages(req: LayoutRequest) -> dict:
    """Pages d'un exemplaire pour chaque taille de texte (mode sujet)."""
    style = sheet.LARGE if req.box_size == "large" else sheet.STANDARD
    examples = {"check_svg": _EXAMPLE_CHECK, "circle_svg": _EXAMPLE_CIRCLE}
    return {d: subject.count_pages(req.exam, style, d, env, examples, _FETCHER) for d in subject.DENSITIES}


def render_pdf(req: PdfRequest) -> bytes:
    if req.mode == "subject":
        examples = {"check_svg": _EXAMPLE_CHECK, "circle_svg": _EXAMPLE_CIRCLE}
        return subject.render_subject_pdf(req, env, examples, _FETCHER)
    html = render_html(req)
    return HTML(string=html, url_fetcher=_FETCHER).write_pdf()


def qr_svg(text: str) -> str:
    qr = segno.make(text, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=6, border=2)
    return buf.getvalue().decode()
