from fastapi.testclient import TestClient

from app.main import app
from tests.test_pipeline import fake_qr, make_exam

client = TestClient(app)


def test_pdf_and_scan_endpoints():
    exam = make_exam()
    r = client.post("/api/pdf", json={"exam": exam.model_dump(), "qr": fake_qr(), "mode": "grid", "sheets": 2})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_pdf_rejects_mismatched_header():
    r = client.post("/api/pdf", json={"exam": make_exam().model_dump(), "qr": fake_qr(n_q=5), "mode": "grid"})
    assert r.status_code == 422


def test_scan_rejects_garbage():
    r = client.post("/api/scan", files={"image": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 422


def test_static_and_sw():
    assert client.get("/").status_code == 200
    assert client.get("/sw.js").status_code == 200


def test_layout_counts_pages():
    exam = make_exam(n_q=12)
    r = client.post("/api/layout", json={"exam": exam.model_dump(), "box_size": "large"})
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert set(pages) == {"large", "normal", "compact"}
    assert pages["large"] >= pages["normal"] >= pages["compact"] >= 1


def test_scan_pdf_streams_each_page():
    """PDF sorti d'un scanner : toutes les pages sont lues, une ligne NDJSON par page."""
    import json

    qr = fake_qr()
    pdf = client.post("/api/pdf", json={"exam": make_exam().model_dump(), "qr": qr, "mode": "grid", "subject": False, "sheets": 2}).content
    r = client.post("/api/scan-pdf", files={"file": ("scan.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    lines = [json.loads(x) for x in r.text.splitlines() if x]
    assert [(x["page"], x["n_pages"]) for x in lines] == [(1, 2), (2, 2)]
    assert all(x["scan"]["qr"] == qr for x in lines)
    assert not any(c["state"] == "marked" for c in lines[0]["scan"]["cells"])


def test_scan_pdf_rejects_garbage():
    r = client.post("/api/scan-pdf", files={"file": ("x.pdf", b"%PDF-nope", "application/pdf")})
    assert r.status_code == 422
