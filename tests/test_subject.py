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
