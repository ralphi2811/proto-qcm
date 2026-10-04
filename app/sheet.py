"""Géométrie de la grille de réponses (source de vérité unique).

Toutes les coordonnées sont en millimètres, origine en haut à gauche d'une page A4
portrait. Le même calcul sert au rendu PDF et à la lecture OMR : la grille ne dépend
que de (nb questions, nb options max, taille des cases), paramètres stockés en clair
dans l'en-tête du QR code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

PAGE_W, PAGE_H = 210.0, 297.0

MARKER_SIZE = 7.0
MARKER_OFFSET = 12.0  # centre des repères depuis les bords
MARKERS = [  # ordre horaire : TL, TR, BR, BL
    (MARKER_OFFSET, MARKER_OFFSET),
    (PAGE_W - MARKER_OFFSET, MARKER_OFFSET),
    (PAGE_W - MARKER_OFFSET, PAGE_H - MARKER_OFFSET),
    (MARKER_OFFSET, PAGE_H - MARKER_OFFSET),
]

# Zone utile. En haut, le QR et le cartouche s'alignent sur le haut des repères (entre
# les deux repères du haut) ; en bas, le contenu s'arrête juste au-dessus des repères et
# le pied de page passe entre eux.
INNER_LEFT, INNER_RIGHT = 18.0, 192.0
INNER_TOP = MARKER_OFFSET - MARKER_SIZE / 2  # 8,5 mm
INNER_BOTTOM = PAGE_H - MARKER_OFFSET - MARKER_SIZE / 2 - 1.5  # 280 mm
FOOTER_Y = PAGE_H - MARKER_OFFSET + 1.0  # ligne de base du pied de page, entre les repères du bas

QR_BOX = (INNER_LEFT, INNER_TOP, 36.0)  # x, y, taille (zone de silence de 2 modules incluse)

COL_GAP = 6.0


@dataclass(frozen=True)
class Style:
    """Dimensions de la grille. L'identifiant est stocké dans l'en-tête du QR."""

    box: float  # côté d'une case à cocher
    opt_pitch: float  # pas horizontal entre cases
    row_pitch: float  # pas vertical entre questions
    qnum_w: float  # largeur du numéro de question
    font_pt: float  # taille des numéros / lettres

    @property
    def h_gap(self) -> float:  # espace libre entre deux cases d'une même ligne
        return self.opt_pitch - self.box

    @property
    def v_gap(self) -> float:  # espace libre entre deux lignes
        return self.row_pitch - self.box


STANDARD, LARGE = 0, 1
STYLES = {
    STANDARD: Style(box=4.0, opt_pitch=6.0, row_pitch=6.0, qnum_w=9.0, font_pt=9),
    LARGE: Style(box=6.0, opt_pitch=9.5, row_pitch=9.5, qnum_w=11.0, font_pt=12),  # primaire
}

# Cartouche d'identification, à droite du QR. Les zones d'écriture ont des coordonnées
# fixes : l'OMR en renvoie un recadrage (affiché à la correction, futur OCR).
NAME_BOX = (QR_BOX[0] + QR_BOX[2] + 4.0, INNER_TOP, INNER_RIGHT - (QR_BOX[0] + QR_BOX[2] + 4.0), QR_BOX[2])
NAME_LABEL_W = 19.0
NAME_FIELD_H = 8.5
NAME_FIELDS = {  # clé -> (libellé, x, y, l, h) de la zone d'écriture
    key: (
        label,
        NAME_BOX[0] + NAME_LABEL_W,
        NAME_BOX[1] + 7.0 + i * (NAME_FIELD_H + 1.5),
        NAME_BOX[2] - NAME_LABEL_W - 2.5,
        NAME_FIELD_H,
    )
    for i, (key, label) in enumerate([("nom", "Nom"), ("prenom", "Prénom"), ("classe", "Classe")])
}

# Bandeau d'aide (exemples « coche / n'entoure pas ») entre le cartouche et la grille
HELP_TOP = NAME_BOX[1] + NAME_BOX[3] + 3.0
HELP_H = 10.0

MAX_OPTIONS = 8


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class Layout:
    n_questions: int
    n_options: int
    style: Style
    rows: int
    cols: int
    grid_top: float
    question_boxes: list[list[Box]] = field(default_factory=list)  # [q][opt]
    question_labels: list[tuple[float, float]] = field(default_factory=list)
    option_headers: list[tuple[float, float, str]] = field(default_factory=list)


class LayoutError(ValueError):
    pass


def option_letter(i: int) -> str:
    return chr(ord("A") + i)


def capacity(n_options: int, style: int = STANDARD) -> int:
    rows, cols, _ = _grid_dims(n_options, STYLES[style])
    return rows * cols


def _grid_dims(n_options: int, st: Style) -> tuple[int, int, float]:
    col_w = st.qnum_w + n_options * st.opt_pitch
    cols = int((INNER_RIGHT - INNER_LEFT + COL_GAP) // (col_w + COL_GAP))
    grid_top = HELP_TOP + HELP_H + 2.0
    # une ligne d'en-tête (lettres A B C ...) puis les lignes de questions
    rows = int((INNER_BOTTOM - (grid_top + st.row_pitch)) // st.row_pitch)
    return rows, cols, grid_top


def compute_layout(n_questions: int, n_options: int, style: int = STANDARD) -> Layout:
    if not 1 <= n_questions <= 255:
        raise LayoutError("Le nombre de questions doit être entre 1 et 255")
    if not 2 <= n_options <= MAX_OPTIONS:
        raise LayoutError(f"Le nombre d'options doit être entre 2 et {MAX_OPTIONS}")
    if style not in STYLES:
        raise LayoutError(f"Taille de cases inconnue : {style}")
    st = STYLES[style]

    max_rows, cols, grid_top = _grid_dims(n_options, st)
    if n_questions > max_rows * cols:
        raise LayoutError(
            f"Trop de questions pour une grille : {n_questions} > {max_rows * cols} "
            f"(avec {n_options} options et cette taille de cases)"
        )
    used_cols = math.ceil(n_questions / max_rows)
    rows = math.ceil(n_questions / used_cols)  # équilibre les colonnes

    lay = Layout(n_questions, n_options, st, rows, used_cols, grid_top)
    col_w = st.qnum_w + n_options * st.opt_pitch
    pad = (st.opt_pitch - st.box) / 2

    for c in range(used_cols):
        x0 = INNER_LEFT + c * (col_w + COL_GAP)
        for o in range(n_options):
            lay.option_headers.append((x0 + st.qnum_w + o * st.opt_pitch + st.opt_pitch / 2, grid_top, option_letter(o)))

    for q in range(n_questions):
        c, r = divmod(q, rows)
        x0 = INNER_LEFT + c * (col_w + COL_GAP)
        y0 = grid_top + st.row_pitch + r * st.row_pitch
        lay.question_labels.append((x0, y0 + st.row_pitch / 2))
        lay.question_boxes.append(
            [
                Box(x0 + st.qnum_w + o * st.opt_pitch + pad, y0 + (st.row_pitch - st.box) / 2, st.box, st.box)
                for o in range(n_options)
            ]
        )
    return lay
