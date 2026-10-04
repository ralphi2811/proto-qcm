"""Partie « page » du QR code, pour le mode réponses sur le sujet.

Chaque page du sujet porte son propre QR :

    partie page (claire) | blob examen (en-tête 8 o + IV + corrigé chiffré, cf. payload.py)

Partie page :
    [0] 2 (marqueur de format)   [1] longueur de la partie page (octets)
    [2:4] n° d'exemplaire (0 = non numéroté)   [4] page (1..)   [5] nb de pages
    [6] côté des cases (dixièmes de mm)   [7] nb de groupes
    puis, bit à bit, un groupe par question présente sur la page :
        q (8 bits) · 1re option (3) · nb de cases - 1 (3) · disposition (2)
        disposition 0 : x (8) y (8) par case        (positions libres)
        disposition 1 : y (8) commun, x (8) par case (propositions en ligne)
        disposition 2 : x (8) commun, y (8) par case (propositions en colonne)
    x : centre de la case en mm depuis le bord gauche ; y : en mm depuis CONTENT_TOP.
"""

from __future__ import annotations

from dataclasses import dataclass

from .payload import PayloadError

PAGE_FORMAT = 2
FIXED_LEN = 8
CONTENT_TOP = 40.0  # origine des y (aucune case au-dessus) -> y tient sur 8 bits (40..295 mm)


@dataclass
class PageBox:
    q: int
    o: int
    cx: float  # mm
    cy: float  # mm


@dataclass
class PageInfo:
    copy: int
    page: int
    n_pages: int
    box: float  # mm
    boxes: list[PageBox]

    def to_json(self) -> dict:
        return {"copy": self.copy, "page": self.page, "n_pages": self.n_pages}


class _W:
    def __init__(self):
        self.bits: list[int] = []

    def put(self, v: int, n: int):
        if not 0 <= v < (1 << n):
            raise PayloadError(f"Valeur hors limites pour le QR : {v} sur {n} bits")
        self.bits += [(v >> i) & 1 for i in range(n - 1, -1, -1)]

    def bytes(self) -> bytes:
        b = self.bits + [0] * (-len(self.bits) % 8)
        return bytes(int("".join(map(str, b[i : i + 8])), 2) for i in range(0, len(b), 8))


class _R:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def get(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3] if (self.pos >> 3) < len(self.data) else None
            if byte is None:
                raise PayloadError("QR de page tronqué")
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v


def _mm(v: float) -> int:
    return int(round(v))


def encode(info: PageInfo) -> bytes:
    groups: list[list[PageBox]] = []
    for b in sorted(info.boxes, key=lambda b: (b.q, b.o)):
        if groups and groups[-1][-1].q == b.q and groups[-1][-1].o == b.o - 1 and len(groups[-1]) < 8:
            groups[-1].append(b)
        else:
            groups.append([b])
    if len(groups) > 255:
        raise PayloadError("Trop de questions sur une page")
    w = _W()
    for g in groups:
        xs = [_mm(b.cx) for b in g]
        ys = [_mm(b.cy - CONTENT_TOP) for b in g]
        if len(set(ys)) == 1:
            mode = 1
        elif len(set(xs)) == 1:
            mode = 2
        else:
            mode = 0
        w.put(g[0].q, 8)
        w.put(g[0].o, 3)
        w.put(len(g) - 1, 3)
        w.put(mode, 2)
        if mode == 1:
            w.put(ys[0], 8)
            for x in xs:
                w.put(x, 8)
        elif mode == 2:
            w.put(xs[0], 8)
            for y in ys:
                w.put(y, 8)
        else:
            for x, y in zip(xs, ys):
                w.put(x, 8)
                w.put(y, 8)
    body = w.bytes()
    total = FIXED_LEN + len(body)
    if total > 255:
        raise PayloadError("Trop de cases sur une page pour le QR")
    head = bytes([PAGE_FORMAT, total]) + info.copy.to_bytes(2, "big") + bytes(
        [info.page, info.n_pages, int(round(info.box * 10)), len(groups)]
    )
    return head + body


def decode(raw: bytes) -> tuple[PageInfo, bytes]:
    """Renvoie (infos de page, blob examen)."""
    if len(raw) < FIXED_LEN or raw[0] != PAGE_FORMAT:
        raise PayloadError("Pas un QR de page")
    total = raw[1]
    copy = int.from_bytes(raw[2:4], "big")
    page, n_pages, box10, n_groups = raw[4], raw[5], raw[6], raw[7]
    r = _R(raw[FIXED_LEN:total])
    boxes = []
    for _ in range(n_groups):
        q, o0, n, mode = r.get(8), r.get(3), r.get(3) + 1, r.get(2)
        if mode == 1:
            y = r.get(8)
            pts = [(r.get(8), y) for _ in range(n)]
        elif mode == 2:
            x = r.get(8)
            pts = [(x, r.get(8)) for _ in range(n)]
        else:
            pts = [(r.get(8), r.get(8)) for _ in range(n)]
        boxes += [PageBox(q, o0 + i, x, y + CONTENT_TOP) for i, (x, y) in enumerate(pts)]
    return PageInfo(copy, page, n_pages, box10 / 10, boxes), raw[total:]
