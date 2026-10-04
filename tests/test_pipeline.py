"""Test bout en bout : PDF -> image -> marques simulées -> dégradations -> OMR."""

from __future__ import annotations

import base64
import os
import random

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytest

from app import omr, sheet
from app.payload import b45encode, parse_header
from app.render import ExamIn, PdfRequest, render_pdf

DPI = 150
N_Q, N_OPT = 30, 4
PEN = 0.35  # épaisseur d'un trait de stylo bille, mm


def fake_qr(n_q=N_Q, n_opt=N_OPT, style=sheet.STANDARD) -> str:
    header = bytes([1, n_q, n_opt, style]) + os.urandom(4)
    return b45encode(header + os.urandom(12) + os.urandom(n_q // 2 + 10))


def make_exam(n_q=N_Q, n_opt=N_OPT) -> ExamIn:
    qs = []
    for i in range(n_q):
        k = n_opt if i % 3 else 3  # options variables
        qs.append({"html": f"Question <b>{i + 1}</b> ?", "options": [{"html": f"Réponse {j}"} for j in range(k)]})
    return ExamIn(title="Test OMR", questions=qs)


def render_sheet(style=sheet.STANDARD, n_q=N_Q):
    qr = fake_qr(n_q=n_q, style=style)
    pdf = render_pdf(PdfRequest(exam=make_exam(n_q), qr=qr, mode="grid", subject=False, sheets=1))
    img = pdfium.PdfDocument(pdf)[0].render(scale=DPI / 72).to_numpy()
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY), qr


@pytest.fixture(scope="module")
def sheet_image():
    return render_sheet()


def mm2px(v):
    return int(round(v * DPI / 25.4))


def pts(*xy):
    return np.array([(mm2px(x), mm2px(y)) for x, y in xy], np.int32)


# ----------------------------------------------------------------- gestes simulés


def fill(img, b):
    m = b.w * 0.12
    cv2.rectangle(img, (mm2px(b.x + m), mm2px(b.y + m)), (mm2px(b.x + b.w - m), mm2px(b.y + b.h - m)), 40, -1)


def cross(img, b, thick=0.45):
    t = max(1, mm2px(thick))
    m = b.w * 0.08
    cv2.line(img, *pts((b.x + m, b.y + m), (b.x + b.w - m, b.y + b.h - m)), 30, t, cv2.LINE_AA)
    cv2.line(img, *pts((b.x + b.w - m, b.y + m), (b.x + m, b.y + b.h - m)), 30, t, cv2.LINE_AA)


def check(img, b, size=1.0, dx=0.0, dy=0.0):
    """Coche ✓ dessinée dans une case de 4 mm, mise à l'échelle de la case."""
    k = b.w / 4 * size
    p = [(b.x + dx + x * k, b.y + dy + y * k) for x, y in [(0.6, 2.2), (1.6, 3.4), (3.6, 0.5)]]
    cv2.polylines(img, [pts(*p)], False, 20, max(1, mm2px(PEN)), cv2.LINE_AA)


def circle(img, b, margin=1.0):
    """Case entourée (ellipse un peu penchée, pas tout à fait fermée, comme à la main)."""
    r = b.w / 2 + margin
    cv2.ellipse(img, (mm2px(b.cx), mm2px(b.cy)), (mm2px(r + 0.4), mm2px(r)), 10, 0, 340, 20, max(1, mm2px(PEN)), cv2.LINE_AA)


def draw_marks(gray, answers, name="DUPONT", style=sheet.STANDARD, n_q=N_Q):
    img = gray.copy()
    lay = sheet.compute_layout(n_q, N_OPT, style)
    rnd = random.Random(1)
    for q, opts in answers.items():
        for o in opts:
            (fill if rnd.random() < 0.5 else cross)(img, lay.question_boxes[q][o])
    _, x, y, w, h = sheet.NAME_FIELDS["nom"]
    cv2.putText(img, name, (mm2px(x + 2), mm2px(y + h - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 20, 2)
    return img


def photograph(img, angle=0.0, persp=0.04, noise=8, rot90=0, seed=0):
    """Simule une photo : fond sombre, perspective, rotation, flou, bruit, éclairage."""
    rng = np.random.default_rng(seed)
    h, w = img.shape
    canvas = np.full((int(h * 1.4), int(w * 1.4)), 70, np.uint8)
    oy, ox = (canvas.shape[0] - h) // 2, (canvas.shape[1] - w) // 2
    canvas[oy : oy + h, ox : ox + w] = img
    H, W = canvas.shape
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    j = persp * W
    dst = src + rng.uniform(-j, j, src.shape).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    out = cv2.warpPerspective(canvas, M, (W, H), borderValue=70)
    R = cv2.getRotationMatrix2D((W / 2, H / 2), angle, 1.0)
    out = cv2.warpAffine(out, R, (W, H), borderValue=70)
    grad = np.linspace(0.75, 1.05, W)[None, :]  # éclairage inégal
    out = np.clip(out * grad + rng.normal(0, noise, out.shape), 0, 255).astype(np.uint8)
    out = cv2.GaussianBlur(out, (3, 3), 0)
    out = np.rot90(out, rot90).copy()
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes()


def states(res):
    return {(c["q"], c["o"]): c for c in res["cells"]}


# ----------------------------------------------------------------------- tests

ANSWERS = {0: [0], 1: [1, 3], 4: [2], 7: [0], 13: [3], 20: [1], 29: [0, 1, 2]}


def check_answers(res, answers=ANSWERS):
    assert res["header"] is not None
    marked: dict[int, list[int]] = {}
    for (q, o), c in sorted(states(res).items()):
        if c["state"] == "marked":
            marked.setdefault(q, []).append(o)
    assert marked == answers
    assert not [k for k, c in states(res).items() if c["state"] == "unsure"]


@pytest.mark.parametrize(
    "kw",
    [
        dict(),
        dict(angle=8, seed=1),
        dict(angle=-12, persp=0.06, seed=2),
        dict(rot90=1, seed=3),
        dict(rot90=2, angle=5, seed=4),
        dict(noise=18, seed=5),
    ],
)
def test_scan_degraded(sheet_image, kw):
    gray, qr = sheet_image
    res = omr.scan(photograph(draw_marks(gray, ANSWERS), **kw), preview=False)
    assert res["qr"] == qr
    check_answers(res)
    # le nom écrit à la main doit se retrouver dans le recadrage
    crop = cv2.imdecode(np.frombuffer(base64.b64decode(res["name_fields"]["nom"]["jpeg"]), np.uint8), 0)
    assert (crop < 100).mean() > 0.01
    empty = cv2.imdecode(np.frombuffer(base64.b64decode(res["name_fields"]["classe"]["jpeg"]), np.uint8), 0)
    assert (empty < 100).mean() < 0.002


@pytest.mark.parametrize("style", [sheet.STANDARD, sheet.LARGE])
@pytest.mark.parametrize("seed", [1, 2])
def test_child_gestures(style, seed):
    """Gestes d'enfant : coches de toutes tailles = cochée ; case entourée = douteuse
    (signalée, à confirmer) ; jamais de fausse détection sur les cases voisines."""
    n_q = 20
    gray, _ = render_sheet(style, n_q)
    lay = sheet.compute_layout(n_q, N_OPT, style)
    B = lambda q, o: lay.question_boxes[q][o]  # noqa: E731
    img = gray.copy()
    expect_marked = []
    for i, (size, dx, dy) in enumerate([(1.0, 0, 0), (0.8, 0.3, 0.3), (0.6, 0.6, 0.6), (1.3, -0.3, -0.6), (0.5, 1.2, 1.0)]):
        check(img, B(i, 1), size, dx, dy)
        expect_marked.append((i, 1))
    cross(img, B(5, 2), thick=PEN)
    expect_marked.append((5, 2))
    fill(img, B(6, 0))
    expect_marked.append((6, 0))
    expect_circled = []
    for i, margin in enumerate([0.6, 1.0, 1.4]):
        circle(img, B(9 + i, 1 + i % 2), margin)
        expect_circled.append((9 + i, 1 + i % 2))

    res = omr.scan(photograph(img, angle=5 * seed, seed=seed), preview=False)
    st = states(res)
    for k in expect_marked:
        assert st[k]["state"] == "marked", (k, st[k])
    for k in expect_circled:
        assert st[k]["state"] == "unsure" and st[k]["circled"], (k, st[k])
    others = {k: c["state"] for k, c in st.items() if k not in expect_marked + expect_circled and c["state"] != "empty"}
    assert others == {}


def test_qr_damaged_still_reads(sheet_image):
    gray, qr = sheet_image
    img = draw_marks(gray, ANSWERS)
    # tache d'encre sur ~15 % du QR
    x, y, sz = (mm2px(v) for v in sheet.QR_BOX)
    cv2.circle(img, (x + sz // 2, y + sz // 2 + sz // 6), sz // 7, 0, -1)
    res = omr.scan(photograph(img, angle=4, seed=7), preview=False)
    assert res["qr"] == qr


def test_header_roundtrip():
    h = parse_header(fake_qr(style=sheet.LARGE))
    assert (h.n_questions, h.n_options, h.style) == (N_Q, N_OPT, sheet.LARGE)
