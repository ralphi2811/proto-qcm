"""Lecture optique (OMR) d'une photo de grille de réponses.

Pipeline :
  1. détection des 4 repères carrés pleins (seuillage adaptatif + contours) ;
  2. orientation : le repère le plus proche du QR code est le coin haut-gauche ;
  3. redressement par homographie vers la géométrie de `sheet.py` ;
  4. recalage fin de chaque case (recherche locale du cadre) puis taux de noircissement.

Le serveur ne déchiffre rien : il renvoie le texte brut du QR et les taux de remplissage,
la notation est faite dans le navigateur de l'enseignant.
"""

from __future__ import annotations

import base64
import itertools
from dataclasses import dataclass

import cv2
import numpy as np
import zxingcpp

from . import sheet, subject
from .payload import PayloadError, parse_qr

PX_PER_MM = 8.0
MAX_SIDE = 2600  # px, au-delà on réduit la photo (gain de temps, aucune perte utile)

# Seuils en surface d'encre (mm²) dans l'intérieur de la case (cadre exclu) : indépendants
# de la taille des cases. Repères : coche fine au stylo bille ≈ 2-3 mm², case noircie ≫ 5 mm².
MARK_MM2 = 1.7
UNSURE_MM2 = 0.7
# Case entourée : un même trait manuscrit passe à l'extérieur d'au moins HALO_SIDES côtés
HALO_SIDES = 3


class ScanError(Exception):
    pass


@dataclass
class QrHit:
    text: str
    center: tuple[float, float]


# --------------------------------------------------------------------------- utils


def _load_gray(data: bytes | np.ndarray) -> np.ndarray:
    if isinstance(data, np.ndarray):
        img = data
    else:
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)  # applique l'orientation EXIF
    if img is None:
        raise ScanError("Image illisible")
    h, w = img.shape
    s = MAX_SIDE / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def _read_qr(gray: np.ndarray) -> QrHit | None:
    for img in (gray, cv2.GaussianBlur(gray, (3, 3), 0)):
        for res in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode):
            if not res.text:
                continue
            p = res.position
            pts = [p.top_left, p.top_right, p.bottom_right, p.bottom_left]
            cx = sum(q.x for q in pts) / 4
            cy = sum(q.y for q in pts) / 4
            return QrHit(res.text, (cx, cy))
    return None


def read_qr_text(data: bytes) -> str | None:
    """Texte du premier QR code d'une photo (transfert de clé vers un téléphone sans BarcodeDetector)."""
    hit = _read_qr(_load_gray(data))
    return hit.text if hit else None


# ----------------------------------------------------------------------- repères


def _marker_candidates(gray: np.ndarray) -> list[tuple[float, float, float]]:
    """Renvoie [(cx, cy, aire)] des taches carrées pleines."""
    h, w = gray.shape
    block = int(max(h, w) / 8) | 1
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 12)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    min_area = (max(h, w) / 300) ** 2
    max_area = (max(h, w) / 12) ** 2
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if not min_area < area < max_area:
            continue
        (cx, cy), (rw, rh), _ = cv2.minAreaRect(c)
        if min(rw, rh) == 0 or max(rw, rh) / min(rw, rh) > 1.6:
            continue
        if area / (rw * rh) < 0.85:  # un cercle donne ~0.785
            continue
        hull = cv2.convexHull(c)
        if area / max(cv2.contourArea(hull), 1) < 0.92:
            continue
        approx = cv2.approxPolyDP(c, 0.06 * cv2.arcLength(c, True), True)
        if len(approx) != 4:
            continue
        # plein (et pas un cadre / motif de recherche du QR)
        x, y, bw, bh = cv2.boundingRect(c)
        mask = np.zeros((bh, bw), np.uint8)
        cv2.drawContours(mask, [c - [x, y]], -1, 255, -1)
        roi = th[y : y + bh, x : x + bw]
        fill = cv2.countNonZero(cv2.bitwise_and(roi, mask)) / max(cv2.countNonZero(mask), 1)
        if fill < 0.9:
            continue
        out.append((cx, cy, area))
    return out


def _quad_area(pts: np.ndarray) -> float:
    return float(cv2.contourArea(cv2.convexHull(pts.astype(np.float32))))


def _find_markers(gray: np.ndarray) -> np.ndarray:
    cands = sorted(_marker_candidates(gray), key=lambda t: -t[2])[:16]
    if len(cands) < 4:
        raise ScanError(f"Repères introuvables ({len(cands)}/4) — cadrez toute la feuille, bien éclairée")
    expected_ratio = (sheet.MARKERS[1][0] - sheet.MARKERS[0][0]) / (sheet.MARKERS[3][1] - sheet.MARKERS[0][1])
    best, best_score = None, 0.0
    for combo in itertools.combinations(cands, 4):
        areas = [c[2] for c in combo]
        if max(areas) / min(areas) > 3:
            continue
        pts = np.array([(c[0], c[1]) for c in combo], np.float32)
        hull = cv2.convexHull(pts, returnPoints=False)
        if len(hull) != 4:
            continue
        ordered = _cyclic(pts)
        sides = [np.linalg.norm(ordered[i] - ordered[(i + 1) % 4]) for i in range(4)]
        r = (sides[0] + sides[2]) / (sides[1] + sides[3])
        r = min(r, 1 / r)
        if abs(r - expected_ratio) > 0.25:
            continue
        # les repères doivent être petits devant la feuille
        a = _quad_area(pts)
        if np.mean(areas) > a / 150:
            continue
        if a > best_score:
            best, best_score = ordered, a
    if best is None:
        raise ScanError("Impossible d'identifier les 4 repères de la feuille")
    return best


def _cyclic(pts: np.ndarray) -> np.ndarray:
    """Trie 4 points dans le sens horaire (repère image, y vers le bas)."""
    c = pts.mean(axis=0)
    ang = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    return pts[np.argsort(ang)]


def _orient(cyc: np.ndarray, qr: QrHit | None) -> np.ndarray:
    if qr is not None:
        d = np.linalg.norm(cyc - np.array(qr.center, np.float32), axis=1)
        start = int(np.argmin(d))
    else:
        start = int(np.argmin(cyc.sum(axis=1)))  # on suppose la photo à peu près droite
    return np.roll(cyc, -start, axis=0)


# --------------------------------------------------------------------- lecture


def _warp(gray: np.ndarray, src: np.ndarray) -> np.ndarray:
    dst = np.array(sheet.MARKERS, np.float32) * PX_PER_MM
    H = cv2.getPerspectiveTransform(src.astype(np.float32), dst)
    size = (int(sheet.PAGE_W * PX_PER_MM), int(sheet.PAGE_H * PX_PER_MM))
    return cv2.warpPerspective(gray, H, size, flags=cv2.INTER_LINEAR, borderValue=255)


def _ink_mask(warped: np.ndarray) -> np.ndarray:
    block = int(12 * PX_PER_MM) | 1
    th = cv2.adaptiveThreshold(warped, 1, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 18)
    return th


class _BoxReader:
    def __init__(self, ink: np.ndarray):
        self.ink = ink
        self.ii = cv2.integral(ink, sdepth=cv2.CV_32S)
        self.h, self.w = ink.shape

    def _sum(self, x0, y0, x1, y1):
        ii = self.ii
        x0 = np.clip(x0, 0, self.w)
        x1 = np.clip(x1, 0, self.w)
        y0 = np.clip(y0, 0, self.h)
        y1 = np.clip(y1, 0, self.h)
        return ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]

    def read(self, box: sheet.Box, search_mm: float = 1.5) -> dict:
        s = PX_PER_MM
        bx, by, bw, bh = (int(round(v * s)) for v in (box.x, box.y, box.w, box.h))
        r = int(search_mm * s)
        dx, dy = np.meshgrid(np.arange(-r, r + 1), np.arange(-r, r + 1))
        dx, dy = dx.ravel(), dy.ravel()
        bt = max(2, int(0.45 * s))  # épaisseur de bande pour le cadre
        x0, y0 = bx + dx, by + dy
        outer = self._sum(x0 - 1, y0 - 1, x0 + bw + 1, y0 + bh + 1)
        inner = self._sum(x0 + bt, y0 + bt, x0 + bw - bt, y0 + bh - bt)
        border = outer - inner
        # pénalise légèrement les grands décalages à score égal
        score = border - 0.02 * (dx**2 + dy**2)
        k = int(np.argmax(score))
        bx, by = bx + int(dx[k]), by + int(dy[k])

        m = max(3, int(0.8 * s))  # intérieur : on exclut le cadre imprimé
        ix0, iy0, ix1, iy1 = bx + m, by + m, bx + bw - m, by + bh - m
        area = max((ix1 - ix0) * (iy1 - iy0), 1)
        ink_px = float(self._sum(ix0, iy0, ix1, iy1))

        return {
            "ratio": ink_px / area,
            "ink_mm2": ink_px / (s * s),
            "rect": [bx / s, by / s, box.w, box.h],
        }


def _classify(ink_mm2: float, circled: bool) -> str:
    if ink_mm2 >= MARK_MM2:
        return "marked"
    if circled or ink_mm2 >= UNSURE_MM2:
        return "unsure"  # douteuse (ou entourée) : à confirmer par l'enseignant
    return "empty"


def _circled_boxes(ink: np.ndarray, rects: dict, st: sheet.Style) -> set:
    """Repère les cases entourées.

    On efface les cadres imprimés, puis chaque trait manuscrit (composante connexe) est
    attribué à la seule case dont il fait le tour : il doit passer à l'extérieur d'au moins
    HALO_SIDES côtés de cette case. Les morceaux de cercles voisins ne se cumulent donc pas.
    """
    s = PX_PER_MM
    work = ink.copy()
    e_out, e_in, corner = int(0.3 * s), int(0.5 * s), int(1.0 * s)
    look = max(2, int(0.25 * s))  # profondeur examinée de part et d'autre d'une bande
    for x, y, w, h in rects.values():
        x0, y0, x1, y1 = (int(round(v * s)) for v in (x, y, x + w, y + h))
        # On efface les 4 côtés du cadre imprimé (pas les coins : un cercle serré qui coupe
        # un coin reste d'un seul tenant). Une colonne/ligne de la bande est restaurée si un
        # trait la traverse (encre des deux côtés) : on recolle un cercle qui chevauche le
        # cadre sans jamais souder deux traits distincts.
        for horiz, a0, a1, b0, b1 in (
            (True, max(0, y0 - e_out), y0 + e_in, x0 + corner, x1 - corner),
            (True, y1 - e_in, y1 + e_out, x0 + corner, x1 - corner),
            (False, max(0, x0 - e_out), x0 + e_in, y0 + corner, y1 - corner),
            (False, x1 - e_in, x1 + e_out, y0 + corner, y1 - corner),
        ):
            if horiz:  # bande horizontale : lignes a0..a1, colonnes b0..b1
                before = ink[max(0, a0 - look) : a0, b0:b1].any(axis=0)
                after = ink[a1 : a1 + look, b0:b1].any(axis=0)
                work[a0:a1, b0:b1] = (before & after)[None, :]
            else:  # bande verticale : colonnes a0..a1, lignes b0..b1
                before = ink[b0:b1, max(0, a0 - look) : a0].any(axis=1)
                after = ink[b0:b1, a1 : a1 + look].any(axis=1)
                work[b0:b1, a0:a1] = (before & after)[:, None]
    n, labels, stats, _ = cv2.connectedComponentsWithStats(work, connectivity=8)

    g = int(0.45 * s)
    dh = int(min(st.h_gap - 0.75, 1.8) * s)
    dv = int(min(st.v_gap - 0.75, 1.8) * s)
    min_side = st.box * s
    circled = set()
    for key, (x, y, w, h) in rects.items():
        bx, by, bw, bh = (int(round(v * s)) for v in (x, y, w, h))
        cx, cy = bx + bw // 2, by + bh // 2
        bands = [
            (bx, by - g - dv, bx + bw, by - g),  # haut
            (bx, by + bh + g, bx + bw, by + bh + g + dv),  # bas
            (bx - g - dh, by, bx - g, by + bh),  # gauche
            (bx + bw + g, by, bx + bw + g + dh, by + bh),  # droite
        ]
        seen: dict[int, int] = {}
        for a, b, c, d in bands:
            a, b = max(a, 0), max(b, 0)
            ids = np.unique(labels[b:d, a:c])
            for i in ids[ids > 0]:
                seen[i] = seen.get(i, 0) + 1
        for i, sides in seen.items():
            if sides < HALO_SIDES:
                continue
            l, t, ww, hh, _ = stats[i]
            # le trait doit entourer cette case (et pas seulement la longer)
            if ww >= min_side and hh >= min_side and l <= cx <= l + ww and t <= cy <= t + hh:
                circled.add(key)
                break
    return circled


def _name_crops(warped: np.ndarray) -> dict:
    """Recadre les zones manuscrites (Nom, Prénom, Classe) : affichage à la correction,
    et point d'entrée d'un futur OCR."""
    s = PX_PER_MM
    out = {}
    for key, (label, x, y, w, h) in sheet.NAME_FIELDS.items():
        # intérieur du cadre imprimé, pour ne pas gêner un OCR
        x0, y0, x1, y1 = (int(round(v * s)) for v in (x + 0.6, y + 0.6, x + w - 0.6, y + h - 0.6))
        crop = warped[y0:y1, x0:x1]
        ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
        out[key] = {"label": label, "jpeg": base64.b64encode(jpg.tobytes()).decode()}
    return out


PDF_DPI = 200  # assez pour les cases et le QR, sans dépasser MAX_SIDE (A4 ≈ 2340 px)


def _open_pdf(data: bytes):
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ScanError("PDF illisible") from e


def pdf_page_count(data: bytes) -> int:
    doc = _open_pdf(data)
    try:
        return len(doc)
    finally:
        doc.close()


def scan_pdf_page(data: bytes, index: int, fallback_params: dict | None = None) -> dict:
    """Analyse la page `index` d'un PDF (scanner, imprimante multifonction)."""
    doc = _open_pdf(data)
    try:
        img = doc[index].render(scale=PDF_DPI / 72, grayscale=True).to_pil().convert("L")
    finally:
        doc.close()
    return scan(np.asarray(img), fallback_params)


def scan(data: bytes | np.ndarray, fallback_params: dict | None = None, preview: bool = True) -> dict:
    gray = _load_gray(data)
    qr = _read_qr(gray)
    corners = _orient(_find_markers(gray), qr)
    warped = _warp(gray, corners)

    if qr is None:  # seconde chance sur l'image redressée (zone connue)
        x, y, sz = (int(v * PX_PER_MM) for v in sheet.QR_BOX)
        pad = int(4 * PX_PER_MM)
        crop = warped[max(0, y - pad) : y + sz + pad, max(0, x - pad) : x + sz + pad]
        hit = _read_qr(cv2.resize(crop, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC))
        if hit:
            qr = QrHit(hit.text, (0, 0))

    header = page = None
    warnings = []
    if qr is not None:
        try:
            header, page = parse_qr(qr.text)
        except PayloadError as e:
            warnings.append(f"QR invalide : {e}")

    if page is not None:  # page de sujet : positions des cases lues dans le QR
        st = subject.subject_style(page.box)
        h = page.box / 2
        boxes = {(b.q, b.o): sheet.Box(b.cx - h, b.cy - h, page.box, page.box) for b in page.boxes}
        params = None
    else:  # grille séparée : géométrie calculée
        if header is not None:
            params = (header.n_questions, header.n_options, header.style)
        elif fallback_params:
            params = (
                int(fallback_params["n_questions"]),
                int(fallback_params["n_options"]),
                int(fallback_params.get("style", sheet.STANDARD)),
            )
            warnings.append("QR non lu : géométrie fournie manuellement")
        else:
            raise ScanError(
                "QR code illisible : rephotographiez la page (ou, pour une grille séparée, "
                "choisissez le QCM manuellement)"
            )
        layout = sheet.compute_layout(*params)
        st = layout.style
        boxes = {(q, o): b for q, row in enumerate(layout.question_boxes) for o, b in enumerate(row)}

    ink = _ink_mask(warped)
    reader = _BoxReader(ink)
    read = {k: reader.read(b) for k, b in boxes.items()}
    circled = _circled_boxes(ink, {k: c["rect"] for k, c in read.items()}, st)
    cells = [
        {
            "q": q,
            "o": o,
            "ratio": round(c["ratio"], 3),
            "ink_mm2": round(c["ink_mm2"], 2),
            "state": _classify(c["ink_mm2"], (q, o) in circled),
            "circled": (q, o) in circled and c["ink_mm2"] < MARK_MM2,
            "rect": [round(v, 2) for v in c["rect"]],
        }
        for (q, o), c in sorted(read.items())
    ]

    result = {
        "qr": qr.text if qr else None,
        "header": header.to_json() if header else None,
        "page": page.to_json() if page else None,
        "params": {"n_questions": params[0], "n_options": params[1], "style": params[2]} if params else None,
        "thresholds": {"marked_mm2": MARK_MM2, "unsure_mm2": UNSURE_MM2},
        "cells": cells,
        # cartouche Nom / Prénom / Classe : grille, ou page 1 du sujet
        "name_fields": _name_crops(warped) if page is None or page.page == 1 else {},
        "warnings": warnings,
    }
    if preview:
        small = cv2.resize(warped, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        result["preview"] = {
            "jpeg": base64.b64encode(jpg.tobytes()).decode(),
            "px_per_mm": PX_PER_MM / 2,
        }
    return result
