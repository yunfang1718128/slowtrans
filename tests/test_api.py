from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from slowtrans.api.app import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SLOWTRANS_TOKEN", raising=False)
    app = create_app(data_dir=str(tmp_path))
    with TestClient(app) as c:
        yield c


def test_health_and_capabilities(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    caps = client.get("/api/v1/capabilities").json()
    assert caps["translate"]["mono"] is True
    assert caps["convert"]["implemented"] is False


def test_convert_returns_501(client):
    r = client.post("/api/v1/jobs", json={"type": "convert", "src_path": "x.md"})
    assert r.status_code == 501
    assert r.json()["error"] == "not_implemented"


def test_create_job_validates_path(client):
    r = client.post("/api/v1/jobs", json={"type": "translate", "rel": "../../etc/passwd"})
    assert r.status_code == 400


def test_catalog_lists_free_models(client):
    data = client.get("/api/v1/catalog").json()
    sf = {m["id"]: m for m in data["providers"]["siliconflow"]}
    assert sf["Qwen/Qwen2.5-7B-Instruct"]["free"] is True
    assert sf["Qwen/Qwen2.5-7B-Instruct"]["default"] is True
    assert any(p["name"] == "ollama" for p in data["presets"])


def test_settings_roundtrip_masks_key(client, tmp_path: Path):
    r = client.put(
        "/api/v1/settings",
        json={"providers": {"siliconflow": {"api_key": "TEST-KEY-0123456789"}}},
    )
    assert r.status_code == 200
    hint = r.json()["providers"]["siliconflow"]["hint"]
    assert "0123456789" not in hint
    # 空值不能清掉已有 key
    client.put("/api/v1/settings", json={"providers": {"siliconflow": {"api_key": ""}}})
    again = client.get("/api/v1/settings").json()
    assert again["providers"]["siliconflow"]["configured"] is True


def test_library_empty_dir(client):
    data = client.get("/api/v1/library").json()
    assert data["books"] == []
    assert "root" in data


def test_artifacts_404_for_unknown_job(client):
    assert client.get("/api/v1/artifacts/j_nope").status_code == 404
    assert client.get("/api/v1/jobs/j_nope").status_code == 404


def test_auth_required_when_token_set(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SLOWTRANS_TOKEN", "secret-token")
    app = create_app(data_dir=str(tmp_path))
    with TestClient(app) as c:
        assert c.get("/api/v1/jobs").status_code == 401
        ok = c.get("/api/v1/jobs", headers={"Authorization": "Bearer secret-token"})
        assert ok.status_code == 200
        # health/catalog 不需要 token，方便探活
        assert c.get("/api/v1/health").status_code == 200


def test_delete_job_removes_everything(client, tmp_path):
    """终止 = 删干净：连记录带成品文件都不该留。"""
    from slowtrans.db import DB

    db = DB(tmp_path / "slowtrans.db")
    out = tmp_path / "outputs" / "x.zh.epub"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake epub")
    db.ensure_job(
        "j_del", src_path=str(tmp_path / "a.epub"), out_path=str(out),
        lang_from="en", lang_to="zh-CN", provider="siliconflow", model="m",
    )
    db.put_glossary("j_del", "foo", "福", locked=True)
    db.event("j_del", "job.queued", {})
    db.close()

    r = client.delete("/api/v1/jobs/j_del")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert not out.exists()

    db = DB(tmp_path / "slowtrans.db")
    for table in ("jobs", "segments", "chapters", "events", "glossary", "style_cards"):
        assert db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    db.close()


def test_delete_missing_job_404(client):
    assert client.delete("/api/v1/jobs/j_nope").status_code == 404
