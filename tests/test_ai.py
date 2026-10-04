"""Génération IA : préparation des documents, normalisation, appel OpenRouter simulé."""

from __future__ import annotations

import json

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import ai
from app.main import app
from app.render import HTML

CFG = ai.Config(api_key="sk-test", model="test/model", access_code="")


def text_pdf() -> bytes:
    body = "<p>" + "La photosynthèse transforme la lumière en énergie chimique. " * 10 + "</p>"
    return HTML(string=body).write_pdf()


def scanned_pdf() -> bytes:
    img = np.full((400, 300, 3), 255, np.uint8)
    cv2.putText(img, "SCAN", (40, 200), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
    ok, png = cv2.imencode(".png", img)
    import base64

    uri = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode()
    return HTML(string=f'<img src="{uri}" style="width:100%">').write_pdf()


def test_prepare_files_kinds():
    ok, jpg = cv2.imencode(".jpg", np.full((3000, 2000, 3), 200, np.uint8))
    prep = ai.prepare_files([
        ("cours.pdf", text_pdf()),
        ("scan.pdf", scanned_pdf()),
        ("notes.txt", "Les fractions".encode()),
        ("photo.jpg", jpg.tobytes()),
    ])
    names = [n for n, _ in prep.texts]
    assert names == ["cours.pdf", "notes.txt"]
    assert "photosynthèse" in prep.texts[0][1]
    assert [n for n, _ in prep.images] == ["scan.pdf p.1", "photo.jpg"]
    assert all(u.startswith("data:image/jpeg;base64,") for _, u in prep.images)


def test_prepare_rejects_unknown():
    with pytest.raises(ai.AiError):
        ai.prepare_files([("x.bin", b"\x00\x01garbage")])


def test_normalize_filters_and_warns():
    raw = {"title": "T", "questions": [
        {"question": "Q1", "options": [{"text": "a", "correct": True}, {"text": "b", "correct": False}], "explanation": ""},
        {"question": "Q2", "options": [{"text": "a", "correct": False}, {"text": "b", "correct": False}], "explanation": ""},
        {"question": "Q3", "options": [{"text": "a", "correct": True}, {"text": "b", "correct": True}], "explanation": ""},
    ]}
    out, warnings = ai.normalize(raw, ai.GenOptions(n_questions=3, multiple=False))
    assert [q.question for q in out.questions] == ["Q1", "Q3"]
    assert any("Question 2 écartée" in w for w in warnings)
    assert any("Question 3" in w and "vérifier" in w for w in warnings)


def _fake_openrouter(answer: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "test/model",
            "choices": [{"message": {"content": "```json\n" + json.dumps(answer) + "\n```"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })
    return httpx.MockTransport(handler)


ANSWER = {"title": "Fractions", "questions": [
    {"question": "1/2 + 1/4 = ?", "options": [{"text": "3/4", "correct": True}, {"text": "2/6", "correct": False}], "explanation": "4/4…"},
]}


async def test_generate_with_mock(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "_TRANSPORT", _fake_openrouter(ANSWER, seen))
    res = await ai.generate(ai.GenOptions(prompt="Fractions", n_questions=1, n_options=2, level="CM2"),
                            [("notes.txt", b"Les fractions")], CFG)
    assert res["questions"][0]["options"][0]["correct"] is True
    body = seen[0]
    assert body["model"] == "test/model" and body["response_format"]["type"] == "json_schema"
    user = body["messages"][1]["content"]
    assert "CM2" in user[0]["text"] and "Les fractions" in user[1]["text"]


def test_endpoint_access_code(monkeypatch):
    seen = []
    monkeypatch.setattr(ai, "_TRANSPORT", _fake_openrouter(ANSWER, seen))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("AI_ACCESS_CODE", "secret")
    client = TestClient(app)
    assert client.get("/api/ai/status").json()["needs_code"] is True
    opts = {"options": json.dumps({"prompt": "Fractions", "n_questions": 1, "n_options": 2})}
    assert client.post("/api/ai/generate", data=opts).status_code == 403
    r = client.post("/api/ai/generate", data=opts, headers={"X-AI-Code": "secret"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Fractions"


def test_endpoint_disabled(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("AI_ACCESS_CODE", "")
    client = TestClient(app)
    assert client.get("/api/ai/status").json()["enabled"] is False
    r = client.post("/api/ai/generate", data={"options": json.dumps({"prompt": "x"})})
    assert r.status_code == 422
