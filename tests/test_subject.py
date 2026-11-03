"""Mode réponses sur le sujet : pagination, QR par page, lecture des cases."""

from __future__ import annotations

import os

import cv2
import pypdfium2 as pdfium
import pytest

from app import omr, pageqr, sheet, subject
from app.payload import b45encode, parse_qr
from app.render import _EXAMPLE_CHECK, _EXAMPLE_CIRCLE, _FETCHER, ExamIn, PdfRequest, env, render_pdf
from tests.test_pipeline import DPI, check, circle, cross, fill, mm2px, photograph, states

EX = {"check_svg": _EXAMPLE_CHECK, "circle_svg": _EXAMPLE_CIRCLE}
N_Q = 12


def make_exam() -> ExamIn:
    long = "Une proposition volontairement longue pour passer en colonne, numéro {}"
    qs = []
    for i in range(N_Q):
        if i % 3 == 0:
            opts = [{"html": f"{j}/4"} for j in range(4)]  # courtes : en ligne
        elif i % 3 == 1:
            opts = [{"html": f"Réponse moyenne {j}"} for j in range(4)]  # 2 colonnes
        else:
            opts = [{"html": long.format(j)} for j in range(3)]  # une colonne
        qs.append({"html": f"Question <b>{i + 1}</b> : énoncé de la question ?", "options": opts})
    return ExamIn(title="Sujet test", questions=qs)


def fake_blob(style=sheet.LARGE) -> bytes:
    return bytes([1, N_Q, 4, style]) + os.urandom(4) + os.urandom(12) + os.urandom(24)


@pytest.fixture(scope="module", params=[sheet.LARGE, sheet.STANDARD], ids=["grandes", "standard"])
def rendered(request):
    blob = fake_blob(request.param)
    req = PdfRequest(exam=make_exam(), qr=b45encode(blob), mode="subject", copies=2, numbered=True)
    per_page = subject.layout_pages(req, env, EX, _FETCHER)
    pdf = render_pdf(req)
    doc = pdfium.PdfDocument(pdf)
    pages = [cv2.cvtColor(doc[i].render(scale=DPI / 72).to_numpy(), cv2.COLOR_RGB2GRAY) for i in range(len(doc))]
    return req, per_page, pages, blob


def test_pagination_and_copies(rendered):
    req, per_page, pages, _ = rendered
    assert len(per_page) >= 2  # le sujet tient sur plusieurs pages
    assert len(pages) == 2 * len(per_page)


def _box(cx, cy, w):
    return sheet.Box(cx - w / 2, cy - w / 2, w, w)


def test_scan_subject_pages(rendered):
    req, per_page, pages, blob = rendered
    n_pages = len(per_page)
    copy = 2  # deuxième exemplaire
    for p in range(n_pages):
        img = pages[(copy - 1) * n_pages + p].copy()
        expect_marked, expect_circled = set(), set()
        for k, (_, q, o, cx, cy, w) in enumerate(per_page[p]):
            b = _box(cx, cy, w)
            if o == (q % 3) and k % 2 == 0:
                [check, cross, fill][q % 3](img, b)
                expect_marked.add((q, o))
            elif o == 2 and q % 4 == 1:
                circle(img, b, 1.0)
                expect_circled.add((q, o))
        res = omr.scan(photograph(img, angle=(-1) ** p * 6, seed=p), preview=False)
        assert res["page"] == {"copy": copy, "page": p + 1, "n_pages": n_pages}
        assert res["header"]["exam_id"] == blob[4:8].hex()
        st = states(res)
        assert set(st) == {(q, o) for (_, q, o, *_r) in per_page[p]}
        assert {k for k, c in st.items() if c["state"] == "marked"} == expect_marked
        assert {k for k, c in st.items() if c["circled"]} == expect_circled
        assert {k for k, c in st.items() if c["state"] == "unsure"} == expect_circled
        assert bool(res["name_fields"]) == (p == 0)  # cartouche lu sur la page 1 seulement


def test_page_qr_contents(rendered):
    req, per_page, pages, blob = rendered
    import zxingcpp

    text = zxingcpp.read_barcodes(pages[0])[0].text
    header, info = parse_qr(text)
    assert (info.copy, info.page) == (1, 1)
    got = sorted((b.q, b.o) for b in info.boxes)
    assert got == sorted((q, o) for (_, q, o, *_r) in per_page[0])
    for b, (_, q, o, cx, cy, _w) in zip(sorted(info.boxes, key=lambda b: (b.q, b.o)), sorted(per_page[0], key=lambda t: (t[1], t[2]))):
        assert abs(b.cx - cx) <= 0.51 and abs(b.cy - cy) <= 0.51


def test_unnumbered_copies():
    req = PdfRequest(exam=make_exam(), qr=b45encode(fake_blob()), mode="subject", copies=1, numbered=False)
    doc = pdfium.PdfDocument(render_pdf(req))
    img = cv2.cvtColor(doc[0].render(scale=DPI / 72).to_numpy(), cv2.COLOR_RGB2GRAY)
    res = omr.scan(photograph(img, seed=9), preview=False)
    assert res["page"]["copy"] == 0


def test_option_columns():
    O = lambda *t: [type("O", (), {"html": x}) for x in t]  # noqa: E731
    assert subject.option_columns(O("1", "2", "3", "4")) == 4
    assert subject.option_columns(O("Réponse moyenne", "b")) == 2
    assert subject.option_columns(O("x" * 50, "b")) == 1
    assert subject.option_columns(O('<img src="data:x">', "b")) == 2


def test_dense_twenty_questions_one_page():
    """« Très petite » : 20 questions courtes sur une page, réponses alignées en colonnes,
    et la page se lit toujours."""
    qs = []
    for i in range(20):
        if i % 4 == 0:
            q, opts = f"{i + 1}/4 + 1/4 = ?", [f"{j}/4" for j in range(4)]  # tout sur une ligne
        elif i == 1:  # trop longues pour 4 colonnes : 2 colonnes, sur les mêmes bords
            q, opts = "Quelle fraction représente la moitié d'un gâteau ?", ["Un demi du gâteau", "Un quart", "Trois quarts", "Un tiers"]
        else:
            q, opts = "Parmi ces fractions, laquelle est égale à la fraction trois quarts ?", ["6/8", "3/5", "4/3", "9/16"]
        qs.append({"html": q, "options": [{"html": o} for o in opts]})
    exam = ExamIn(title="Vingt questions", questions=qs)
    ctx, box = subject._ctx(exam, sheet.STANDARD, "dense", EX)
    assert set(ctx["cols"]) == {2, 4}  # 4 colonnes, ou 2 (alignées sur les mêmes bords)
    assert not ctx["show_pts"]
    # 42 o chiffrés : corrigé + modèle B (lettre, groupe, ordre mélangé) inscrits dans le QR
    blob = bytes([1, 20, 4, sheet.STANDARD]) + os.urandom(4) + os.urandom(12) + os.urandom(42)
    req = PdfRequest(exam=exam, qr=b45encode(blob), mode="subject", density="dense")
    per_page = subject.layout_pages(req, env, EX, _FETCHER)
    assert len(per_page) == 1
    assert len({round(cx, 1) for (*_r, cx, _cy, _w) in per_page[0]}) == 4  # cases alignées sur 4 colonnes
    doc = pdfium.PdfDocument(render_pdf(req))
    img = cv2.cvtColor(doc[0].render(scale=DPI / 72).to_numpy(), cv2.COLOR_RGB2GRAY)
    expect_marked, expect_circled = set(), set()
    for _, q, o, cx, cy, w in per_page[0]:
        if o == q % 4:
            [check, cross, fill][q % 3](img, _box(cx, cy, w))
            expect_marked.add((q, o))
        elif o == (q + 2) % 4 and q % 5 == 3:
            circle(img, _box(cx, cy, w), 1.0)
            expect_circled.add((q, o))
    res = omr.scan(photograph(img, angle=4, seed=3), preview=False)
    st = states(res)
    assert len(st) == 80
    assert {k for k, c in st.items() if c["state"] == "marked"} == expect_marked
    assert {k for k, c in st.items() if c["circled"]} == expect_circled


def test_long_title_stays_clear_of_hint_and_markers():
    """Titre long (IA, « Sujet B — ») : réduit puis tronqué, sans chevaucher la consigne du
    cartouche ni atteindre le repère noir du bas."""
    nx, _ny, nw, _nh = sheet.NAME_BOX
    hint_left = nx + nw - 3 - subject.text_mm(subject.NAME_HINT, subject.HINT_MM)
    marker_left = sheet.MARKERS[2][0] - sheet.MARKER_SIZE / 2
    for title in ["Les fractions", "Sujet B — Évaluation de mathématiques : les fractions et les nombres décimaux (CM2)"]:
        text, size = subject.header_title(title)
        assert subject.TITLE_MIN_MM <= size <= subject.TITLE_MM
        assert nx + 3 + subject.text_mm(text, size, bold=True) < hint_left
        assert (text == title) == (len(title) < 40)
        alone, _ = subject.header_title(title, with_hint=False)  # pages suivantes : toute la largeur
        assert len(alone) >= len(text)
        foot = subject.footer_text(title, "abcd1234 · page 1 / 3 · n° 12 · ne pas plier, ne pas écrire près des carrés noirs")
        assert sheet.INNER_LEFT + subject.text_mm(foot, subject.FOOT_MM) < marker_left
        assert foot.endswith("carrés noirs")
