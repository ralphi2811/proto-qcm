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
    top_left: tuple[float, float] | None = None  # coin du motif de recherche haut-gauche du symbole


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
            return QrHit(res.text, (cx, cy), (p.top_left.x, p.top_left.y))
    return None


def read_qr_text(data: bytes) -> str | None:
    """Texte du premier QR code d'une photo (transfert de clé vers un téléphone sans BarcodeDetector)."""
    hit = _read_qr(_load_gray(data))
    return hit.text if hit else None


# ----------------------------------------------------------------------- repères


def _square_blobs(th: np.ndarray, min_area: float, max_area: float) -> list[tuple[float, float, float]]:
    """[(cx, cy, aire)] des taches carrées pleines d'une image binaire (encre = 255)."""
    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
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


def _marker_candidates(gray: np.ndarray) -> list[tuple[float, float, float]]:
    """Renvoie [(cx, cy, aire)] des taches carrées pleines.

    Deux passes : ouverture fine (petits repères, photo prise de loin), puis ouverture
    d'environ 1,5 mm à l'échelle d'une page plein cadre, qui détache d'un repère un trait
    fin qui le touche (poussière du scanner, trait de stylo) et l'empêchait d'être carré."""
    h, w = gray.shape
    block = int(max(h, w) / 8) | 1
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, block, 12)
    min_area = (max(h, w) / 300) ** 2
    max_area = (max(h, w) / 12) ** 2
    k = max(5, int(max(h, w) * 1.5 / sheet.PAGE_H) | 1)
    out: list[tuple[float, float, float]] = []
    for size in (3, k):
        opened = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))
        for cx, cy, area in _square_blobs(opened, min_area, max_area):
            r = np.sqrt(area) / 2
            if not any(np.hypot(cx - x, cy - y) < r for x, y, _ in out):
                out.append((cx, cy, area))
    return out


def _quad_area(pts: np.ndarray) -> float:
    return float(cv2.contourArea(cv2.convexHull(pts.astype(np.float32))))


def _marker_quads(gray: np.ndarray, keep: int = 25) -> list[np.ndarray]:
    """Quadrilatères plausibles de 4 repères (sens horaire), du plus grand au plus petit."""
    cands = sorted(_marker_candidates(gray), key=lambda t: -t[2])[:16]
    if len(cands) < 4:
        raise ScanError(f"Repères introuvables ({len(cands)}/4) — cadrez toute la feuille, bien éclairée")
    expected_ratio = (sheet.MARKERS[1][0] - sheet.MARKERS[0][0]) / (sheet.MARKERS[3][1] - sheet.MARKERS[0][1])
    found = []
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
        found.append((a, ordered))
    if not found:
        raise ScanError("Impossible d'identifier les 4 repères de la feuille")
    found.sort(key=lambda t: -t[0])
    return [q for _, q in found[:keep]]


def _homography(corners: np.ndarray) -> np.ndarray:
    """Image -> page en mm, d'après les 4 repères orientés (TL, TR, BR, BL)."""
    return cv2.getPerspectiveTransform(corners.astype(np.float32), np.array(sheet.MARKERS, np.float32))


QR_CENTER_MM = (sheet.QR_BOX[0] + sheet.QR_BOX[2] / 2, sheet.QR_BOX[1] + sheet.QR_BOX[2] / 2)
QR_TOL_MM = 5.0


def _qr_fits(corners: np.ndarray, qr: QrHit) -> bool:
    """Le QR, ramené sur la page par ces repères, tombe-t-il à sa place et dans le bon sens ?
    Écarte un quadrilatère formé avec une case noircie ou un motif du QR au lieu d'un repère."""
    H = _homography(corners)
    pts = [qr.center] + ([qr.top_left] if qr.top_left else [])
    mm = cv2.perspectiveTransform(np.array([pts], np.float32), H)[0]
    cx, cy = mm[0]
    if np.hypot(cx - QR_CENTER_MM[0], cy - QR_CENTER_MM[1]) > QR_TOL_MM:
        return False
    if qr.top_left:  # le coin haut-gauche du symbole doit être en haut à gauche de son centre
        tx, ty = mm[1]
        if not (tx < cx - 8 and ty < cy - 8):
            return False
    return True


CORNER_NAMES = ("haut gauche", "haut droit", "bas droit", "bas gauche")


def _qr_error(corners: np.ndarray, qr: QrHit) -> float:
    """Écart (mm) entre le centre du QR ramené sur la page et sa position théorique."""
    mm = cv2.perspectiveTransform(np.array([[qr.center]], np.float32), _homography(corners))[0, 0]
    return float(np.hypot(mm[0] - QR_CENTER_MM[0], mm[1] - QR_CENTER_MM[1]))


def _rebuild_corner(gray: np.ndarray, qr: QrHit) -> list[tuple[np.ndarray, int]]:
    """Repère manquant (masqué, taché, coupé, non reconnu) : avec 3 repères et les proportions
    de la page, on calcule où est le 4e ; le QR valide la reconstitution.

    Pour chaque triplet de candidats et chaque attribution aux coins, une transformation affine
    (une page à plat se déforme peu : échelle, rotation, léger cisaillement) donne la position
    du coin manquant. On ne garde que les transformations proches d'une similitude, sans
    retournement, et cohérentes avec le QR ; s'il y a un carré sombre à l'endroit prévu, sa
    position exacte remplace l'estimation."""
    cands = sorted(_marker_candidates(gray), key=lambda t: -t[2])[:12]
    if len(cands) < 3:
        return []
    dst_all = np.array(sheet.MARKERS, np.float32)
    found: list[tuple[float, np.ndarray, int]] = []
    for trio in itertools.combinations(cands, 3):
        areas = [c[2] for c in trio]
        if max(areas) / min(areas) > 3:
            continue
        pts = np.array([(c[0], c[1]) for c in trio], np.float32)
        for missing in range(4):
            idx = [i for i in range(4) if i != missing]
            for perm in itertools.permutations(range(3)):
                src = pts[list(perm)]
                A = cv2.getAffineTransform(dst_all[idx], src)  # page (mm) -> image
                M = A[:, :2]
                if np.linalg.det(M) <= 0:  # retournement
                    continue
                sx, sy = np.linalg.norm(M[:, 0]), np.linalg.norm(M[:, 1])
                if not 0.8 < sx / sy < 1.25:
                    continue
                cos = abs(float(M[:, 0] @ M[:, 1])) / (sx * sy)
                if cos > 0.15:  # cisaillement
                    continue
                # taille des repères cohérente avec l'échelle
                side = np.sqrt(np.mean(areas))
                if not 0.5 < side / (sheet.MARKER_SIZE * (sx + sy) / 2) < 1.6:
                    continue
                guess = A @ np.array([*dst_all[missing], 1.0], np.float32)
                near = [c for c in cands if np.hypot(c[0] - guess[0], c[1] - guess[1]) < sheet.MARKER_SIZE * sx / 2]
                if near:
                    guess = np.array(near[0][:2], np.float32)
                corners = np.zeros((4, 2), np.float32)
                corners[idx] = src
                corners[missing] = guess
                if _qr_fits(corners, qr):
                    found.append((_qr_error(corners, qr), corners, missing))
    found.sort(key=lambda t: t[0])
    return [(c, m) for _, c, m in found[:8]]


BOX_FIT_MM = 1.0  # décalage médian toléré des cadres de cases après redressement


def _box_fit(gray: np.ndarray, corners: np.ndarray, boxes: dict, strict: bool = False):
    """Cherche le cadre de chaque case autour de sa place prévue sur la page redressée.
    Renvoie (décalage médian en mm — 9e décile si `strict` —, part des cadres trouvés,
    points trouvés, points prévus)."""
    s = PX_PER_MM
    reader = _BoxReader(_ink_mask(_warp(gray, corners)))
    src, dst, offs = [], [], []
    for b in boxes.values():
        r = reader.read(b, search_mm=3.5)
        if r["frame"] < 0.3:
            continue
        x, y, w, h = r["rect"]
        src.append(((x + w / 2) * s, (y + h / 2) * s))
        dst.append(((b.x + b.w / 2) * s, (b.y + b.h / 2) * s))
        offs.append(np.hypot(x - b.x, y - b.y))
    med = float(np.percentile(offs, 90 if strict else 50)) if offs else float("inf")
    return med, len(offs) / max(len(boxes), 1), src, dst


FIXED_TOL_MM = 1.5  # déplacement maximal d'un repère mesuré lors du recalage


def _refine(corners: np.ndarray, src: list, dst: list, fixed: list[int]) -> np.ndarray | None:
    """Homographie de correction (RANSAC) ajustée sur les cadres de cases retrouvés.

    Les repères réellement mesurés (`fixed`) servent d'ancres : sans eux, la grille régulière
    des cases laisse place à une solution décalée d'une ligne entière qui « colle » aussi bien."""
    if len(src) < 8:
        return None
    s = PX_PER_MM
    anchors = [tuple(np.array(sheet.MARKERS[i], np.float32) * s) for i in fixed]
    src_all = np.float32(src + anchors * 10)
    dst_all = np.float32(dst + anchors * 10)
    Hc, inliers = cv2.findHomography(src_all, dst_all, cv2.RANSAC, 0.6 * s)
    if Hc is None or inliers[: len(src)].sum() < max(8, 0.6 * len(src)):
        return None
    marks = np.array([sheet.MARKERS], np.float32) * s
    moved = cv2.perspectiveTransform(marks, Hc)[0] - marks[0]  # déplacement de chaque repère
    if any(np.hypot(*moved[i]) > FIXED_TOL_MM * s for i in fixed):
        return None
    H0 = cv2.getPerspectiveTransform(corners.astype(np.float32), marks[0])
    return cv2.perspectiveTransform(marks, np.linalg.inv(Hc @ H0))[0]


REBUILT_FIT_MM = 0.8  # repère reconstitué : 90 % des cadres à moins de 0,8 mm


def _validate(gray: np.ndarray, corners: np.ndarray, boxes: dict | None, fixed: list[int]) -> np.ndarray | None:
    """Les cases tombent-elles à leur place ? Sinon, recalage sur leurs cadres (2 passes).

    Repère reconstitué (3 repères fixes) : recalage systématique et critère strict, une
    erreur locale ne devant pas se cacher derrière une médiane correcte."""
    if not boxes:
        return corners
    rebuilt = len(fixed) < 4
    med, frac, src, dst = _box_fit(gray, corners, boxes, strict=rebuilt)
    if not rebuilt and med <= BOX_FIT_MM and frac >= 0.5:
        return corners
    for _ in range(2):
        refined = _refine(corners, src, dst, fixed)
        if refined is None:
            return None
        corners = refined
        med, frac, src, dst = _box_fit(gray, corners, boxes, strict=rebuilt)
        if rebuilt and med <= REBUILT_FIT_MM and frac >= 0.8:
            return corners
        if not rebuilt and med <= BOX_FIT_MM / 2 and frac >= 0.5:
            return corners
    return None


def _frame_points(boxes: dict, per_edge: int = 4) -> np.ndarray:
    """Points (mm, homogènes) répartis sur les côtés des cadres de cases."""
    t = (np.arange(per_edge) + 0.5) / per_edge
    pts = []
    for b in boxes.values():
        for u in t:
            pts += [(b.x + u * b.w, b.y), (b.x + u * b.w, b.y + b.h), (b.x, b.y + u * b.h), (b.x + b.w, b.y + u * b.h)]
    p = np.array(pts, np.float64)
    return np.hstack([p, np.ones((len(p), 1))])


def _search_missing(gray: np.ndarray, corners: np.ndarray, missing: int, boxes: dict) -> np.ndarray:
    """Position du repère manquant qui aligne le mieux tous les cadres de cases sur l'encre.

    Les 3 repères mesurés étant fixes, seule la position du 4e reste inconnue (2 degrés de
    liberté) : on l'explore autour de l'estimation, du grossier au fin. Toute la grille doit
    coïncider à la fois, ce qui lève l'ambiguïté d'un décalage d'une ligne de cases."""
    ink = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 1, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, int(max(gray.shape) / 25) | 1, 10)
    ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))
    h, w = ink.shape
    pts = _frame_points(boxes)
    dst = np.array(sheet.MARKERS, np.float32)
    px_mm = np.linalg.norm(corners[1] - corners[0]) / (dst[1][0] - dst[0][0])  # échelle approchée

    def score(c):
        H = cv2.getPerspectiveTransform(dst, c.astype(np.float32))
        q = pts @ H.T
        x = np.round(q[:, 0] / q[:, 2]).astype(int)
        y = np.round(q[:, 1] / q[:, 2]).astype(int)
        ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
        return ink[y[ok], x[ok]].sum() / len(pts)

    best = corners.astype(np.float32).copy()
    for radius, step in ((12.0, 1.0), (1.5, 0.25)):
        center = best[missing].copy()
        top = (-1.0, center)
        for dy in np.arange(-radius, radius + 1e-6, step):
            for dx in np.arange(-radius, radius + 1e-6, step):
                c = best.copy()
                c[missing] = center + np.array([dx, dy], np.float32) * px_mm
                sc = score(c)
                if sc > top[0]:
                    top = (sc, c[missing].copy())
        best[missing] = top[1]
    return best


def _locate(gray: np.ndarray, qr: QrHit | None, boxes: dict | None) -> tuple[np.ndarray, str | None]:
    """Coins orientés de la feuille (TL, TR, BR, BL) et, le cas échéant, une remarque.

    Le plus grand quadrilatère de repères qui place le QR à sa place et les cases à la leur ;
    à défaut, un repère manquant est reconstitué à partir des trois autres puis recalé sur
    les cadres des cases."""
    try:
        quads = _marker_quads(gray)
    except ScanError:
        quads = []
    if qr is None:
        if not quads:
            raise ScanError("Repères introuvables — cadrez toute la feuille, bien éclairée")
        return _orient(quads[0], None), None
    for q in quads:
        corners = _orient(q, qr)
        if _qr_fits(corners, qr) and (ok := _validate(gray, corners, boxes, [0, 1, 2, 3])) is not None:
            return ok, None
    for corners, missing in _rebuild_corner(gray, qr):
        fixed = [i for i in range(4) if i != missing]
        if boxes:
            corners = _search_missing(gray, corners, missing, boxes)
        if (ok := _validate(gray, corners, boxes, fixed)) is not None:
            return ok, f"Repère {CORNER_NAMES[missing]} non reconnu : position déduite des trois autres"
    raise ScanError("Repères de la feuille introuvables ou incohérents avec le QR code — un carré noir "
                    "est masqué, coupé ou confondu avec une case noircie ; reprenez la photo ou le scan")


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
        band = max((bw + 2) * (bh + 2) - (bw - 2 * bt) * (bh - 2 * bt), 1)
        frame = float(border[k]) / band  # part d'encre dans la bande du cadre (cadre net ≈ 0,4+)
        bx, by = bx + int(dx[k]), by + int(dy[k])

        m = max(3, int(0.8 * s))  # intérieur : on exclut le cadre imprimé
        ix0, iy0, ix1, iy1 = bx + m, by + m, bx + bw - m, by + bh - m
        area = max((ix1 - ix0) * (iy1 - iy0), 1)
        ink_px = float(self._sum(ix0, iy0, ix1, iy1))

        return {
            "ratio": ink_px / area,
            "ink_mm2": ink_px / (s * s),
            "rect": [bx / s, by / s, box.w, box.h],
            "frame": frame,
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


def _geometry(qr_text: str | None, fallback_params: dict | None, warnings: list):
    """(header, page, params, style, cases) d'après le QR, ou la géométrie fournie."""
    header = page = None
    if qr_text is not None:
        try:
            header, page = parse_qr(qr_text)
        except PayloadError as e:
            warnings.append(f"QR invalide : {e}")

    if page is not None:  # page de sujet : positions des cases lues dans le QR
        st = subject.subject_style(page.box)
        h = page.box / 2
        boxes = {(b.q, b.o): sheet.Box(b.cx - h, b.cy - h, page.box, page.box) for b in page.boxes}
        return header, page, None, st, boxes
    # grille séparée : géométrie calculée
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
        return header, page, None, None, None
    layout = sheet.compute_layout(*params)
    boxes = {(q, o): b for q, row in enumerate(layout.question_boxes) for o, b in enumerate(row)}
    return header, page, params, layout.style, boxes


def scan(data: bytes | np.ndarray, fallback_params: dict | None = None, preview: bool = True) -> dict:
    gray = _load_gray(data)
    qr = _read_qr(gray)
    warnings: list[str] = []
    header, page, params, st, boxes = _geometry(qr.text if qr else None, fallback_params, warnings)
    corners, locate_note = _locate(gray, qr, boxes if qr else None)
    if locate_note:
        warnings.insert(0, locate_note)
    warped = _warp(gray, corners)

    if qr is None:  # seconde chance sur l'image redressée (zone connue)
        x, y, sz = (int(v * PX_PER_MM) for v in sheet.QR_BOX)
        pad = int(4 * PX_PER_MM)
        crop = warped[max(0, y - pad) : y + sz + pad, max(0, x - pad) : x + sz + pad]
        hit = _read_qr(cv2.resize(crop, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC))
        if hit:
            qr = QrHit(hit.text, (0, 0))
            warnings.clear()
            header, page, params, st, boxes = _geometry(qr.text, fallback_params, warnings)
    if boxes is None:
        raise ScanError(
            "QR code illisible : rephotographiez la page (ou, pour une grille séparée, "
            "choisissez le QCM manuellement)"
        )

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
