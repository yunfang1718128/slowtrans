"""HTTP 桥接层：AstrBot 插件和本地面板都只依赖这一套接口。

边界很硬：
- 只监听 127.0.0.1（nginx / SSH 隧道 / Tailscale 是外面的事，本项目不管）
- 鉴权 Bearer token，token 只从环境变量来
- 服务端是唯一真相：客户端不持有状态，重连只需 Last-Event-ID
- 转换接口先返回 501，把"还没做"说清楚，而不是假装能用
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import load_config, parse_engine_spec
from ..db import DB
from ..engines.openai_compat import OpenAICompatEngine
from ..pipeline import Pipeline, RunOptions

TERMINAL = {"done", "failed", "cancelled"}


class JobCreate(BaseModel):
    type: str = Field(default="translate", description="translate | convert")
    src_path: str
    out_path: str | None = None
    lang_from: str = "en"
    lang_to: str = "zh-CN"
    engine: str | None = None
    batch_size: int = 4
    bilingual: bool = True
    analyze: bool = True
    budget_cny: float = 0.0


class GlossaryPut(BaseModel):
    src: str
    dst: str
    locked: bool = True
    replan: bool = False


class HookCreate(BaseModel):
    url: str
    events: list[str] = ["job.completed", "job.failed", "job.needs_review"]
    secret: str = ""


@dataclass
class Runtime:
    config_path: str | None
    data_dir: Path
    queue: asyncio.Queue
    stop_flags: dict[str, threading.Event]
    worker: asyncio.Task | None = None


def create_app(config_path: str | None = None, data_dir: str = "data") -> FastAPI:
    app = FastAPI(title="slowtrans", version=__version__)
    rt = Runtime(config_path, Path(data_dir), asyncio.Queue(), {})
    app.state.rt = rt
    token = os.environ.get("SLOWTRANS_TOKEN", "")

    def db() -> DB:
        return DB(rt.data_dir / "slowtrans.db")

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not token:
            return
        if authorization != f"Bearer {token}":
            raise HTTPException(401, "bad token")

    @app.on_event("startup")
    async def _startup() -> None:
        rt.worker = asyncio.create_task(_worker(app))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if rt.worker:
            rt.worker.cancel()

    # ---------- 基础 ----------
    @app.get("/", response_class=HTMLResponse)
    async def panel() -> str:
        p = Path(__file__).with_name("panel.html")
        return p.read_text(encoding="utf-8") if p.exists() else "<h1>panel.html missing</h1>"

    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        d = db()
        rows = d.conn.execute(
            "SELECT state, COUNT(*) n FROM jobs GROUP BY state"
        ).fetchall()
        return {
            "ok": True,
            "version": __version__,
            "jobs": {r["state"]: r["n"] for r in rows},
            "queue": rt.queue.qsize(),
        }

    @app.get("/api/v1/version")
    async def version() -> dict[str, str]:
        return {"name": "slowtrans", "version": __version__, "api": "v1"}

    # ---------- 任务 ----------
    @app.post("/api/v1/jobs", dependencies=[Depends(auth)])
    async def create_job(body: JobCreate) -> JSONResponse:
        if body.type == "convert":
            return JSONResponse(
                status_code=501,
                content={
                    "error": "not_implemented",
                    "feature": "convert",
                    "message": "格式转换尚未实现，当前版本只支持 epub -> epub 翻译",
                    "planned": ["md", "txt", "html", "docx", "pdf(仅文本)"],
                },
            )
        if body.type != "translate":
            raise HTTPException(400, f"未知任务类型：{body.type}")
        src = Path(body.src_path).expanduser()
        if not src.exists():
            raise HTTPException(400, f"源文件不存在：{src}")
        cfg = load_config(rt.config_path)
        provider, model = parse_engine_spec(
            body.engine or f"{cfg.engine_cfg['provider']}:{cfg.engine_cfg['model']}"
        )
        out = Path(body.out_path) if body.out_path else src.with_name(src.stem + f".{body.lang_to}.epub")
        d = db()
        from ..db import job_id_for

        jid = job_id_for(src, body.lang_from, body.lang_to, provider, model)
        d.ensure_job(
            jid, src_path=str(src), out_path=str(out), lang_from=body.lang_from,
            lang_to=body.lang_to, provider=provider, model=model,
        )
        d.conn.execute(
            "UPDATE jobs SET state='queued', updated_at=datetime('now') WHERE id=?", (jid,)
        )
        d.event(jid, "job.queued", {"type": "translate"})
        await rt.queue.put(jid)
        return JSONResponse(status_code=202, content={"job_id": jid, "state": "queued"})

    @app.get("/api/v1/jobs", dependencies=[Depends(auth)])
    async def list_jobs(state: str | None = None, limit: int = 50) -> list[dict]:
        d = db()
        if state:
            rows = d.conn.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        else:
            rows = d.conn.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_job_dict(r) for r in rows]

    @app.get("/api/v1/jobs/{jid}", dependencies=[Depends(auth)])
    async def get_job(jid: str) -> dict:
        d = db()
        try:
            row = d.job(jid)
        except KeyError:
            raise HTTPException(404, "job not found")
        data = _job_dict(row)
        data["chapters"] = [
            dict(r) for r in d.conn.execute(
                "SELECT idx,href,title,seg_total,seg_done FROM chapters WHERE job_id=? ORDER BY idx",
                (jid,),
            )
        ]
        data["review"] = [
            dict(r) for r in d.conn.execute(
                "SELECT chapter_idx,block_idx,seg_idx,src_text,error FROM segments"
                " WHERE job_id=? AND state='review' LIMIT 50",
                (jid,),
            )
        ]
        return data

    @app.post("/api/v1/jobs/{jid}/pause", dependencies=[Depends(auth)])
    async def pause(jid: str) -> dict:
        d = db()
        d.job(jid)
        flag = rt.stop_flags.get(jid) or threading.Event()
        flag.set()
        rt.stop_flags[jid] = flag
        d.set_state(jid, "paused")
        return {"job_id": jid, "state": "paused", "note": "当前批次结束后停"}

    @app.post("/api/v1/jobs/{jid}/resume", dependencies=[Depends(auth)])
    async def resume(jid: str) -> dict:
        d = db()
        row = d.job(jid)
        flag = rt.stop_flags.get(jid)
        if flag:
            flag.clear()
        d.set_state(jid, "queued")
        await rt.queue.put(jid)
        return {"job_id": jid, "state": "queued", "model": row["model"]}

    @app.post("/api/v1/jobs/{jid}/cancel", dependencies=[Depends(auth)])
    async def cancel(jid: str) -> dict:
        d = db()
        d.job(jid)
        flag = rt.stop_flags.get(jid) or threading.Event()
        flag.set()
        rt.stop_flags[jid] = flag
        d.set_state(jid, "cancelled")
        return {"job_id": jid, "state": "cancelled"}

    # ---------- 术语表 ----------
    @app.get("/api/v1/jobs/{jid}/glossary", dependencies=[Depends(auth)])
    async def get_glossary(jid: str) -> dict:
        d = db()
        d.job(jid)
        return {
            "version": d.glossary_version(jid),
            "terms": [dict(r) for r in d.glossary(jid)],
        }

    @app.put("/api/v1/jobs/{jid}/glossary", dependencies=[Depends(auth)])
    async def put_glossary(jid: str, body: GlossaryPut) -> dict:
        d = db()
        d.job(jid)
        d.put_glossary(jid, body.src, body.dst, locked=body.locked)
        ver = d.glossary_version(jid)
        d.event(jid, "glossary.updated", {"src": body.src, "dst": body.dst, "version": ver})
        return {
            "job_id": jid,
            "version": ver,
            "note": "术语版本已变；下次 resume 时只会重译命中旧术语的段落"
            if body.replan else "已保存",
        }

    # ---------- 事件 ----------
    @app.get("/api/v1/jobs/{jid}/events")
    async def events(jid: str, request: Request, last_id: int = 0):
        return await _sse(db, request, jid, last_id)

    @app.get("/api/v1/events")
    async def all_events(request: Request, last_id: int = 0):
        return await _sse(db, request, None, last_id)

    # ---------- 产物 ----------
    @app.get("/api/v1/artifacts/{jid}", dependencies=[Depends(auth)])
    async def artifact(jid: str):
        d = db()
        row = d.job(jid)
        p = Path(row["out_path"]) if row["out_path"] else None
        if not p or not p.exists():
            raise HTTPException(404, "成品还没生成")
        return FileResponse(p, filename=p.name, media_type="application/epub+zip")

    # ---------- hooks / 能力 ----------
    @app.post("/api/v1/hooks", dependencies=[Depends(auth)])
    async def add_hook(body: HookCreate) -> dict:
        d = db()
        d.conn.execute(
            "CREATE TABLE IF NOT EXISTS hooks(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " url TEXT NOT NULL, events TEXT, secret TEXT, active INTEGER DEFAULT 1)"
        )
        cur = d.conn.execute(
            "INSERT INTO hooks(url,events,secret,active) VALUES(?,?,?,1)",
            (body.url, ",".join(body.events), body.secret),
        )
        return {"hook_id": cur.lastrowid}

    @app.get("/api/v1/capabilities")
    async def capabilities() -> dict:
        return {
            "translate": {"epub": True, "bilingual": True, "resume": True},
            "convert": {"implemented": False, "status": "not_implemented"},
            "engines": ["openai_compat", "mock"],
            "events": "sse",
        }

    return app


def _job_dict(r) -> dict:
    return {
        "job_id": r["id"],
        "state": r["state"],
        "src_path": r["src_path"],
        "out_path": r["out_path"],
        "lang_from": r["lang_from"],
        "lang_to": r["lang_to"],
        "engine": f"{r['provider']}:{r['model']}",
        "segments": {
            "total": r["total_segments"],
            "done": r["done_segments"],
            "review": r["review_segments"],
            "failed": r["failed_segments"],
        },
        "tokens": {"in": r["tokens_in"], "out": r["tokens_out"]},
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "error": r["error"],
    }


async def _sse(db_factory, request: Request, jid: str | None, last_id: int):
    async def gen():
        cursor = last_id
        idle = 0.0
        while True:
            if await request.is_disconnected():
                break
            d = db_factory()
            rows = d.events_since(jid, cursor, limit=200)
            if rows:
                for r in rows:
                    cursor = r["id"]
                    payload = {
                        "schema_version": 1,
                        "seq": r["id"],
                        "job_id": r["job_id"],
                        "event": r["type"],
                        "ts": r["ts"],
                        "data": json.loads(r["data"] or "{}"),
                    }
                    yield f"id: {r['id']}\nevent: {r['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                idle = 0.0
            else:
                idle += 0.5
                if idle >= 15:
                    yield ": keepalive\n\n"
                    idle = 0.0
            await asyncio.sleep(0.5)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _worker(app: FastAPI) -> None:
    rt: Runtime = app.state.rt
    while True:
        jid = await rt.queue.get()
        try:
            await asyncio.to_thread(_run_job, rt, jid)
        except Exception as exc:  # 一个任务炸了不能带走整个服务
            d = DB(rt.data_dir / "slowtrans.db")
            d.set_state(jid, "failed", str(exc)[:500])
        finally:
            rt.queue.task_done()


def _run_job(rt: Runtime, jid: str) -> None:
    cfg = load_config(rt.config_path)
    d = DB(rt.data_dir / "slowtrans.db")
    row = d.job(jid)
    if row["state"] in TERMINAL:
        return
    provider, model = row["provider"], row["model"]
    p = cfg.provider(provider)
    engine = OpenAICompatEngine(
        provider=provider,
        model=model,
        api_key=p.key(),
        base_url=p.base_url,
        batch_size=int(cfg.engine_cfg.get("batch_size", 4)),
        timeout_s=int(cfg.engine_cfg.get("request_timeout_s", 90)),
        max_output_tokens=int(cfg.engine_cfg.get("max_output_tokens", 3072)),
        temperature=float(cfg.engine_cfg.get("temperature", 0.3)),
    )
    stop = rt.stop_flags.setdefault(jid, threading.Event())
    opts = RunOptions(
        src=Path(row["src_path"]),
        out=Path(row["out_path"]),
        lang_from=row["lang_from"],
        lang_to=row["lang_to"],
        bilingual=bool(cfg.epub_cfg.get("bilingual", True)),
        max_segment_chars=int(cfg.epub_cfg.get("max_segment_chars", 1200)),
        batch_size=int(cfg.engine_cfg.get("batch_size", 4)),
    )
    pipe = Pipeline(opts, d, engine, provider, model, should_stop=stop.is_set)
    pipe.run()
