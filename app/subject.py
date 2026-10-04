"""Mode « réponses sur le sujet » : l'élève coche directement sous chaque question.

Rendu en deux passes :
  1. mise en page du sujet (WeasyPrint) et relevé de la position exacte de chaque case ;
  2. même document, avec pour chaque page un fond (repères, QR propre à la page,
     cartouche) : le fond ne modifie pas la mise en page, les positions restent exactes.

Le QR de chaque page contient les positions de ses cases (pageqr) et le corrigé chiffré
par le navigateur : chaque page se lit seule, sans base de données.
"""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from html import escape, unescape

import segno
from PIL import ImageFont
from weasyprint import HTML

from . import pageqr, sheet
from .payload import b45decode, b45encode, parse_blob

PX_MM = 25.4 / 96  # px CSS -> mm

# Marges de la zone de contenu : le haut de chaque page est réservé au QR et au cartouche
MARGIN_TOP = sheet.NAME_BOX[1] + sheet.NAME_BOX[3] + 4.0
MARGIN_SIDE = sheet.INNER_LEFT
MARGIN_BOTTOM = sheet.PAGE_H - sheet.INNER_BOTTOM

BOX_MM = {sheet.STANDARD: 4.5, sheet.LARGE: 6.0}
OPT_GAP = 3.0  # espace minimal entre deux cases (lecture des cases entourées)

# Densité du texte : taille de police et espacements (la taille des cases est un réglage à part)
DENSITIES = {
    "large": {"font_pt": 13.0, "q_gap": 6.5, "opts_top": 2.5, "row_gap": 4.0, "col_gap": 5.0, "help_pt": 11.0},
    "normal": {"font_pt": 11.5, "q_gap": 5.0, "opts_top": 2.0, "row_gap": 3.5, "col_gap": 5.0, "help_pt": 10.0},
    "compact": {"font_pt": 10.0, "q_gap": 3.2, "opts_top": 1.3, "row_gap": OPT_GAP, "col_gap": 4.0, "help_pt": 9.0, "line_height": 1.22},
}
LINE_HEIGHT = 1.3
PT_MM = 25.4 / 72
OPTS_INDENT = 10.5  # retrait des propositions sous l'énoncé
OPTS_WIDTH = sheet.PAGE_W - 2 * sheet.INNER_LEFT - OPTS_INDENT
OPT_TEXT_GAP = 2.5  # espace case -> texte


def subject_style(box: float) -> sheet.Style:
    """Style équivalent pour la détection des cases entourées."""
    return sheet.Style(box=box, opt_pitch=box + OPT_GAP, row_pitch=box + OPT_GAP, qnum_w=0, font_pt=0)


def _plain(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", html or "")).strip()


@lru_cache(maxsize=4)
def _font(bold: bool):
    """Police réellement utilisée dans le PDF, pour mesurer la largeur des propositions."""
    name = "DejaVu Sans:bold" if bold else "DejaVu Sans"
    try:
        path = subprocess.run(["fc-match", "-f", "%{file}", name], capture_output=True, text=True, timeout=5).stdout
        return ImageFont.truetype(path, 100)  # 100 px = 100 pt (rapport conservé)
    except Exception:  # noqa: BLE001 - pas de fontconfig : estimation
        return None


def text_width_mm(html: str, font_pt: float) -> float:
    text = _plain(html)
    font = _font("<b>" in html or "<strong>" in html)
    if font is None:
        return len(text) * 0.6 * font_pt * PT_MM
    return font.getlength(text) / 100 * font_pt * PT_MM


def option_columns(options, font_pt: float = 11.5, col_gap: float = 5.0, box: float = 6.0) -> int:
    """Nombre de colonnes pour les propositions : le moins de lignes possible, à condition
    que chaque proposition tienne sur une ligne dans sa colonne (largeur mesurée)."""
    n = len(options)
    if n <= 1:
        return 1
    if any("<img" in (o.html or "") for o in options):
        return 2
    longest = max(text_width_mm(o.html, font_pt) for o in options) + 0.5  # au pire : retour à la ligne
    best = 1
    for k in range(min(n, 4), 1, -1):
        col = (OPTS_WIDTH - (k - 1) * col_gap) / k
        if longest <= col - box - OPT_TEXT_GAP:
            rows = -(-n // k)
            if rows < -(-n // best):
                best = k
    # à nombre de lignes égal, on garde le moins de colonnes (plus lisible)
    rows = -(-n // best)
    return min(k for k in range(1, best + 1) if -(-n // k) == rows)


# --------------------------------------------------------------------- passe 1


@dataclass
class Pagination:
    pages: list[list[pageqr.PageBox]]  # cases par page (pour UN exemplaire)


def _walk(box):
    yield box
    for child in getattr(box, "children", None) or []:
        yield from _walk(child)


def extract_boxes(doc) -> list[list[tuple[int, int, int, float, float, float]]]:
    """[(exemplaire, q, o, cx, cy, côté)] par page, en mm."""
    pages = []
    for page in doc.pages:
        seen, found = set(), []
        for b in _walk(page._page_box):
            el = getattr(b, "element", None)
            if el is None or el.get("data-q") is None or not hasattr(b, "border_box_x"):
                continue
            key = (int(el.get("data-c")), int(el.get("data-q")), int(el.get("data-o")))
            if key in seen or b.border_width() <= 0:
                continue
            seen.add(key)
            w = b.border_width() * PX_MM
            found.append((*key, (b.border_box_x() + b.border_width() / 2) * PX_MM,
                          (b.border_box_y() + b.border_height() / 2) * PX_MM, w))
        pages.append(found)
    return pages


# ------------------------------------------------------------------- fonds SVG


def _qr_svg(text: str) -> str:
    qr = segno.make(text, error="h", micro=False, boost_error=False)
    if qr.version > 14:  # très gros QR : on privilégie des modules plus grands
        qr = segno.make(text, error="q", micro=False, boost_error=False)
    x, y, size = sheet.QR_BOX
    inner = qr.svg_inline(border=2, omitsize=True, dark="#000")
    return inner.replace("<svg ", f'<svg x="{x}" y="{y}" width="{size}" height="{size}" ', 1)


def _text(x, y, s, size, weight="normal", anchor="start", fill="#222"):
    return (f'<text x="{x:.2f}" y="{y:.2f}" font-family="DejaVu Sans, sans-serif" font-size="{size}" '
            f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">{escape(s)}</text>')


def page_background(qr_text: str, title: str, page: int, n_pages: int, copy: int, exam_id: str) -> str:
    S = sheet
    parts = []
    for mx, my in S.MARKERS:
        h = S.MARKER_SIZE / 2
        parts.append(f'<rect x="{mx - h}" y="{my - h}" width="{S.MARKER_SIZE}" height="{S.MARKER_SIZE}" fill="#000"/>')
    parts.append(_qr_svg(qr_text))

    nx, ny, nw, nh = S.NAME_BOX
    parts.append(f'<rect x="{nx}" y="{ny}" width="{nw}" height="{nh}" rx="1.5" fill="none" stroke="#222" stroke-width="0.4"/>')
    short_title = title if len(title) <= 48 else title[:46] + "…"
    parts.append(_text(nx + 3, ny + 5.2, short_title, 3.7, "bold", fill="#111"))
    pages_txt = f"page {page} / {n_pages}" + (f" · n° {copy}" if copy else "")
    if page == 1:
        parts.append(_text(nx + nw - 3, ny + 5.0, "écrire en MAJUSCULES dans les cadres", 2.5, anchor="end", fill="#555"))
        for key, (label, fx, fy, fw, fh) in S.NAME_FIELDS.items():
            parts.append(_text(nx + 3, fy + fh / 2 + 1.2, label, 3.2))
            parts.append(f'<rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="0.8" fill="none" stroke="#999" stroke-width="0.25"/>')
    else:
        label, fx, fy, fw, fh = S.NAME_FIELDS["nom"]
        parts.append(_text(nx + 3, fy + fh / 2 + 1.2, label, 3.2))
        parts.append(f'<rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="0.8" fill="none" stroke="#999" stroke-width="0.25"/>')
        parts.append(_text(nx + nw / 2, ny + nh - 7, pages_txt.capitalize(), 5, "bold", anchor="middle", fill="#333"))
    parts.append(_text(S.INNER_LEFT, S.FOOTER_Y,
                       f"{title} · {exam_id} · {pages_txt} · ne pas plier, ne pas écrire près des carrés noirs",
                       2.3, fill="#777"))
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">{"".join(parts)}</svg>'
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


# ---------------------------------------------------------------------- rendu


def _ctx(exam, style: int, density: str, examples: dict) -> tuple[dict, float]:
    box = BOX_MM.get(style, BOX_MM[sheet.STANDARD])
    d = DENSITIES.get(density, DENSITIES["normal"])
    lh = d.get("line_height", LINE_HEIGHT)
    line_mm = d["font_pt"] * 25.4 / 72 * lh
    ctx = dict(
        exam=exam, large=style == sheet.LARGE, box=box, D=d,
        line_height=lh,
        text_pad=max(0.0, (box - line_mm) / 2),  # centre la 1re ligne de texte sur la case
        box_pad=max(0.0, (line_mm - box) / 2),
        cols=[option_columns(q.options, d["font_pt"], d["col_gap"], box) for q in exam.questions],
        M={"top": MARGIN_TOP, "side": MARGIN_SIDE, "bottom": MARGIN_BOTTOM},
        indent=OPTS_INDENT, text_gap=OPT_TEXT_GAP,
        **examples,
    )
    return ctx, box


def _context(req, env, examples: dict):
    exam = req.exam
    blob = b45decode(req.qr.strip())
    header = parse_blob(blob)
    n_opt = max(len(q.options) for q in exam.questions)
    if (header.n_questions, header.n_options) != (len(exam.questions), n_opt):
        raise ValueError("L'en-tête du QR ne correspond pas au QCM")
    ctx, box = _ctx(exam, header.style, req.density, examples)
    return env.get_template("subject.html"), ctx, blob, header, box


def count_pages(exam, style: int, density: str, env, examples: dict, fetcher=None) -> int:
    """Nombre de pages d'un exemplaire (aperçu dans l'éditeur, sans QR)."""
    ctx, _ = _ctx(exam, style, density, examples)
    doc = HTML(string=env.get_template("subject.html").render(copies=1, backgrounds=[], **ctx), url_fetcher=fetcher).render()
    return len(doc.pages)


def layout_pages(req, env, examples: dict, fetcher=None) -> list[list[tuple]]:
    """Passe 1 : un exemplaire, sans fond -> [(c, q, o, cx, cy, côté)] par page."""
    tpl, ctx, *_ = _context(req, env, examples)
    doc = HTML(string=tpl.render(copies=1, backgrounds=[], **ctx), url_fetcher=fetcher).render()
    per_page = extract_boxes(doc)
    found = {(q, o) for page in per_page for (_, q, o, *_r) in page}
    expected = {(qi, o) for qi, q in enumerate(req.exam.questions) for o in range(len(q.options))}
    if found != expected:
        raise ValueError("Mise en page : certaines cases n'ont pas pu être localisées")
    if len(per_page) > 255:
        raise ValueError("Sujet trop long")
    return per_page


def render_subject_pdf(req, env, examples: dict, fetcher=None) -> bytes:
    """req : render.PdfRequest (mode 'subject')."""
    exam = req.exam
    tpl, ctx, blob, header, box = _context(req, env, examples)
    per_page = layout_pages(req, env, examples, fetcher)
    n_pages = len(per_page)

    copies = max(1, req.copies)
    backgrounds = []
    for c in range(copies):
        copy_no = c + 1 if req.numbered else 0
        for p, boxes in enumerate(per_page):
            info = pageqr.PageInfo(
                copy=copy_no, page=p + 1, n_pages=n_pages, box=box,
                boxes=[pageqr.PageBox(q, o, cx, cy) for (_, q, o, cx, cy, _w) in boxes],
            )
            qr_text = b45encode(pageqr.encode(info) + blob)
            backgrounds.append(page_background(qr_text, exam.title, p + 1, n_pages, copy_no, header.exam_id))

    # Passe 2 : tous les exemplaires, avec leur fond
    doc2 = HTML(string=tpl.render(copies=copies, backgrounds=backgrounds, **ctx), url_fetcher=fetcher).render()
    if len(doc2.pages) != copies * n_pages:
        raise ValueError("Mise en page instable entre les deux passes")
    # contrôle : positions identiques à la passe 1 pour chaque exemplaire
    for i, page in enumerate(extract_boxes(doc2)):
        ref = per_page[i % n_pages]
        if [(q, o, round(x, 1), round(y, 1)) for (_, q, o, x, y, _w) in page] != [
            (q, o, round(x, 1), round(y, 1)) for (_, q, o, x, y, _w) in ref
        ]:
            raise ValueError("Mise en page instable entre les exemplaires")
    return doc2.write_pdf()
