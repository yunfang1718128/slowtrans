"""HTTP 桥接层：AstrBot 插件和本地面板都只依赖这一套接口。

边界很硬：
- 只监听 127.0.0.1（nginx / SSH 隧道 / Tailscale 是外面的事，本项目不管）
- 鉴权 Bearer token，token 只从环境变量或同步的 settings 来
- 服务端是唯一真相：客户端不持有状态，重连只需 Last-Event-ID
- 格式转换先返回 501，把"还没做"说清楚，而不是假装能用

HTTP 层不做数据访问：所有 SQL 都走 DB 方法，这里只做协议翻译。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import zipfile as _zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from .. import notify
from ..assembler import assemble as assemble_outputs
from ..config import load_config, parse_engine_spec, validate_config
from ..db import DB, job_id_for
from ..engines.openai_compat import OpenAICompatEngine
from ..library import cover_bytes, resolve_inside, scan_library
from ..models import CATALOG, CUSTOM_PRESETS
from ..pipeline import JobAborted, Pipeline
from ..runtime import engine_from_config, options_from_config
from ..settings import load_settings, save_settings

TERMINAL = {"done", "failed", "cancelled"}
SSE_TICK = 0.5


# ---------------------------------------------------------------- 请求模型
class JobCreate(BaseModel):
    type: str = Field(default="translate", description="translate | convert")
    src_path: str | None = None      # 服务器绝对路径
    rel: str | None = None           # 书库相对路径（优先）
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


class ProbeRequest(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str
    api_key: str | None = None


class SettingsPatch(BaseModel):
    providers: dict[str, dict] = {}
    defaults: dict[str, Any] = {}
    library: dict[str, Any] = {}


class HookCreate(BaseModel):
    url: str
    events: list[str] = ["job.completed", "job.failed", "job.paused"]
    secret: str = ""


@dataclass
class Runtime:
    config_path: str | None
    data_dir: Path
    queue: asyncio.Queue
    stop_flags: dict[str, threading.Event]
    abort_flags: dict[str, threading.Event]
    queued: set[str]
    worker: asyncio.Task | None = None
    token: str = ""


# ---------------------------------------------------------------- 应用装配
def create_app(config_path: str | None = None, data_dir: str = "data") -> FastAPI:
    rt = Runtime(
        config_path=config_path,
        data_dir=Path(data_dir),
        queue=asyncio.Queue(),
        stop_flags={},
        abort_flags={},
        queued=set(),
        token=os.environ.get("SLOWTRANS_TOKEN", ""),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_config(load_config(rt.config_path, rt.data_dir))
        with DB(rt.data_dir / "slowtrans.db") as d:
            d.prune()          # 每次启动清一次过期事件/缓存
            stale = d.clear_stale_locks(socket.gethostname())
            if stale:
                print(f"[slowtrans] 清理了 {stale} 个本机残留任务锁")
        rt.worker = asyncio.create_task(_worker(app))
        try:
            yield
        finally:
            if rt.worker:
                rt.worker.cancel()

    app = FastAPI(title="slowtrans", version=__version__, lifespan=lifespan)
    app.state.rt = rt

    # ------------------------------------------------------------ 依赖
    def get_db():
        d = DB(rt.data_dir / "slowtrans.db")
        try:
            yield d
        finally:
            d.close()

    def auth(authorization: str | None = Header(default=None)) -> None:
        if not rt.token:
            return
        if authorization != f"Bearer {rt.token}":
            raise HTTPException(401, "bad token")

    def require_job(d: DB, jid: str):
        try:
            return d.job(jid)
        except KeyError:
            raise HTTPException(404, "job not found")

    def lib_root() -> Path:
        return load_settings(rt.data_dir).library_root(rt.data_dir / "library").resolve()

    # ------------------------------------------------------------ 基础
    @app.get("/", response_class=HTMLResponse)
    async def panel() -> HTMLResponse:
        p = Path(__file__).with_name("panel.html")
        html = p.read_text(encoding="utf-8") if p.exists() else "<h1>panel.html missing</h1>"
        return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})

    @app.get("/api/v1/health")
    async def health(d: DB = Depends(get_db)) -> dict[str, Any]:
        rows = d.conn.execute("SELECT state, COUNT(*) n FROM jobs GROUP BY state").fetchall()
        return {
            "ok": True,
            "version": __version__,
            "jobs": {r["state"]: r["n"] for r in rows},
            "queue": rt.queue.qsize(),
        }

    @app.get("/api/v1/version")
    async def version() -> dict[str, str]:
        return {"name": "slowtrans", "version": __version__, "api": "v1"}

    # ------------------------------------------------------------ 任务
    @app.post("/api/v1/jobs", dependencies=[Depends(auth)])
    async def create_job(body: JobCreate, d: DB = Depends(get_db)) -> JSONResponse:
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

        if body.rel:
            try:
                src = resolve_inside(lib_root(), body.rel)
            except (FileNotFoundError, PermissionError) as exc:
                raise HTTPException(400, f"书库路径无效：{exc}")
        elif body.src_path:
            src = Path(body.src_path).expanduser()
        else:
            raise HTTPException(400, "必须提供 rel 或 src_path")
        if not src.exists():
            raise HTTPException(400, f"源文件不存在：{src}")

        cfg = load_config(rt.config_path, rt.data_dir)
        provider, model = parse_engine_spec(
            body.engine or f"{cfg.engine_cfg['provider']}:{cfg.engine_cfg['model']}"
        )
        try:
            cfg.provider(provider)
        except SystemExit as exc:
            raise HTTPException(400, str(exc))

        # 默认输出到服务自己的数据目录，不写回书库：
        # systemd 里书库是只读的（ProtectSystem=strict），写那儿必然失败。
        if body.out_path:
            out = Path(body.out_path)
        else:
            settings = load_settings(rt.data_dir)
            out_dir = settings.outputs_dir(rt.data_dir / "outputs")
            out = out_dir / f"{src.stem}.{body.lang_to}.epub"
            out.parent.mkdir(parents=True, exist_ok=True)
        jid = job_id_for(src, body.lang_from, body.lang_to, provider, model)
        d.ensure_job(
            jid,
            src_path=str(src),
            out_path=str(out),
            lang_from=body.lang_from,
            lang_to=body.lang_to,
            provider=provider,
            model=model,
            budget_cny=body.budget_cny,
            bilingual=body.bilingual,
        )
        # 防重复提交：正在跑的任务不能被"开始翻译/继续"打回 queued，
        # 否则状态会变成"排队中"而实际在翻译，还会在队列里排第二份。
        if jid in rt.queued:
            return JSONResponse(status_code=200, content={"job_id": jid, "state": "queued", "note": "已在队列中"})
        held, _ = d.job_lock(jid)
        if held:
            return JSONResponse(
                status_code=409,
                content={"error": "already_running", "job_id": jid,
                         "message": "这个任务正在运行，不用重复提交"},
            )
        d.mark_queued(jid)
        d.event(jid, "job.queued", {"type": "translate"})
        rt.queued.add(jid)
        await rt.queue.put(jid)
        return JSONResponse(status_code=202, content={"job_id": jid, "state": "queued"})

    @app.get("/api/v1/jobs", dependencies=[Depends(auth)])
    async def list_jobs(state: str | None = None, limit: int = 50, d: DB = Depends(get_db)) -> list[dict]:
        return [_job_dict(r) for r in d.list_jobs(state=state, limit=limit)]

    @app.get("/api/v1/jobs/{jid}", dependencies=[Depends(auth)])
    async def get_job(jid: str, d: DB = Depends(get_db)) -> dict:
        try:
            row = d.job(jid)
        except KeyError:
            raise HTTPException(404, "job not found")
        data = _job_dict(row)
        data["chapters"] = [dict(r) for r in d.job_chapters(jid)]
        data["review"] = [dict(r) for r in d.job_review(jid)]
        return data

    @app.post("/api/v1/jobs/{jid}/pause", dependencies=[Depends(auth)])
    async def pause(jid: str, d: DB = Depends(get_db)) -> dict:
        require_job(d, jid)
        flag = rt.stop_flags.get(jid) or threading.Event()
        flag.set()
        rt.stop_flags[jid] = flag
        d.set_state(jid, "paused")
        notify.dispatch(d, jid, "job.paused", {"by": "api"})
        return {"job_id": jid, "state": "paused", "note": "当前批次结束后停"}

    @app.post("/api/v1/jobs/{jid}/resume", dependencies=[Depends(auth)])
    async def resume(jid: str, d: DB = Depends(get_db)) -> dict:
        row = require_job(d, jid)
        flag = rt.stop_flags.get(jid)
        if flag:
            flag.clear()
        abort = rt.abort_flags.get(jid)
        if abort:
            abort.clear()
        if jid in rt.queued:
            return {"job_id": jid, "state": "queued", "model": row["model"], "note": "已在队列中"}
        d.mark_queued(jid)
        rt.queued.add(jid)
        await rt.queue.put(jid)
        return {"job_id": jid, "state": "queued", "model": row["model"]}

    @app.post("/api/v1/jobs/{jid}/cancel", dependencies=[Depends(auth)])
    async def cancel(jid: str, d: DB = Depends(get_db)) -> dict:
        """立即终止：不等当前批次收尾，下一次检查点就抛 JobAborted。

        注意 Python 线程无法被安全强杀：如果此刻正卡在一个 HTTP 请求上，
        最坏要等它超时（默认 60 秒）。所以状态先给 cancelling，worker 退出后才算 cancelled。
        """
        require_job(d, jid)
        # 只有本服务进程的 worker 持有锁时才可能"跑到一半"；
        # 否则（僵尸任务、或 CLI 在另一台/另一个进程里跑）直接置为 cancelled，
        # 免得状态永远停在 cancelling。
        held, _ = d.job_lock(jid)
        owner = f"{socket.gethostname()}:{os.getpid()}"
        if not held.startswith(owner):
            d.set_state(jid, "cancelled", "已终止（当时没有运行中的 worker）")
            d.event(jid, "job.cancelled", {"reason": "not-running"})
            notify.dispatch(d, jid, "job.cancelled", {"reason": "not-running"})
            return {"job_id": jid, "state": "cancelled", "note": "没有运行中的任务，已直接标记为终止"}
        flag = rt.abort_flags.setdefault(jid, threading.Event())
        flag.set()
        d.set_state(jid, "cancelling")
        d.event(jid, "job.cancelling", {})
        notify.dispatch(d, jid, "job.cancelling", {})
        return {
            "job_id": jid,
            "state": "cancelling",
            "note": "已请求终止；最坏等当前 HTTP 请求超时（约 60 秒），进度不会丢",
        }

    # ------------------------------------------------------------ 术语表
    @app.get("/api/v1/jobs/{jid}/glossary", dependencies=[Depends(auth)])
    async def get_glossary(jid: str, d: DB = Depends(get_db)) -> dict:
        require_job(d, jid)
        return {"version": d.glossary_version(jid), "terms": [dict(r) for r in d.glossary(jid)]}

    @app.put("/api/v1/jobs/{jid}/glossary", dependencies=[Depends(auth)])
    async def put_glossary(jid: str, body: GlossaryPut, d: DB = Depends(get_db)) -> dict:
        require_job(d, jid)
        d.put_glossary(jid, body.src, body.dst, locked=body.locked)
        ver = d.glossary_version(jid)
        d.event(jid, "glossary.updated", {"src": body.src, "dst": body.dst, "version": ver})
        return {
            "job_id": jid,
            "version": ver,
            "note": "术语版本已变；下次 resume 时只会重译命中旧术语的段落"
            if body.replan
            else "已保存",
        }

    # ------------------------------------------------------------ 事件
    @app.get("/api/v1/jobs/{jid}/events")
    async def events(jid: str, request: Request, last_id: int = 0):
        return await _sse(rt.data_dir, request, jid, last_id)

    @app.get("/api/v1/events")
    async def all_events(request: Request, last_id: int = 0):
        return await _sse(rt.data_dir, request, None, last_id)

    @app.get("/api/v1/jobs/{jid}/log", dependencies=[Depends(auth)])
    async def job_log(jid: str, limit: int = 200, d: DB = Depends(get_db)) -> dict:
        require_job(d, jid)
        return {
            "events": [
                {
                    "seq": r["id"],
                    "event": r["type"],
                    "ts": r["ts"],
                    "data": json.loads(r["data"] or "{}"),
                }
                for r in d.job_log(jid, limit=limit)
            ]
        }

    # ------------------------------------------------------------ 书库
    @app.get("/api/v1/library", dependencies=[Depends(auth)])
    async def library(limit: int = 300, d: DB = Depends(get_db)) -> dict:
        root = lib_root()
        books = scan_library(root, limit=limit)
        by_path = d.jobs_by_src()
        items = []
        for b in books:
            abs_path = (root / b.rel).resolve()
            items.append(
                {
                    "rel": b.rel,
                    "name": b.name,
                    "title": b.display,
                    "authors": b.authors,
                    "language": b.language,
                    "size": b.size,
                    "mtime": b.mtime,
                    "cover": f"/api/v1/library/cover?rel={quote(b.rel)}" if b.cover_href else "",
                    "error": b.error,
                    "job": _job_dict(by_path[str(abs_path)]) if str(abs_path) in by_path else None,
                }
            )
        return {"root": str(root), "exists": root.exists(), "books": items}

    @app.get("/api/v1/library/cover", dependencies=[Depends(auth)])
    async def library_cover(rel: str):
        try:
            data, ctype = cover_bytes(lib_root(), rel, rt.data_dir / "covers")
        except (FileNotFoundError, PermissionError) as exc:
            raise HTTPException(404 if isinstance(exc, FileNotFoundError) else 403, str(exc))
        except LookupError:
            raise HTTPException(404, "no cover")
        return Response(
            content=data,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # ------------------------------------------------------------ 设置
    @app.get("/api/v1/settings", dependencies=[Depends(auth)])
    async def get_settings() -> dict:
        cfg = load_config(rt.config_path, rt.data_dir)
        s = load_settings(rt.data_dir)
        data = s.masked(list(cfg.providers))
        data["effective"] = {
            "engine": f"{cfg.engine_cfg['provider']}:{cfg.engine_cfg['model']}",
            "batch_size": cfg.engine_cfg.get("batch_size", 4),
            "library_root": str(lib_root()),
            "outputs_dir": str(
                load_settings(rt.data_dir).outputs_dir(rt.data_dir / "outputs").resolve()
            ),
            "data_dir": str(rt.data_dir.resolve()),
        }
        return data

    @app.put("/api/v1/settings", dependencies=[Depends(auth)])
    async def put_settings(body: SettingsPatch) -> dict:
        save_settings(load_settings(rt.data_dir), body.model_dump())
        cfg = load_config(rt.config_path, rt.data_dir)
        data = load_settings(rt.data_dir).masked(list(cfg.providers))
        data["note"] = "已保存；新任务立即生效，进行中的任务下一批生效"
        return data

    # ------------------------------------------------------------ 模型目录 / 探活
    @app.get("/api/v1/catalog")
    async def catalog() -> dict:
        return {
            "providers": {
                name: [
                    {"id": o.id, "note": o.note, "free": o.free, "default": o.default}
                    for o in opts
                ]
                for name, opts in CATALOG.items()
            },
            "presets": CUSTOM_PRESETS,
        }

    @app.post("/api/v1/settings/probe", dependencies=[Depends(auth)])
    async def probe(body: ProbeRequest) -> dict:
        cfg = load_config(rt.config_path, rt.data_dir)
        if body.base_url:
            base, key = body.base_url, (body.api_key or "")
        else:
            try:
                p = cfg.provider(body.provider or "siliconflow")
            except SystemExit as exc:
                raise HTTPException(400, str(exc))
            base, key = p.base_url, (body.api_key or p.key())
        if not key:
            return {"ok": False, "error": "没有可用的 API key"}
        engine = OpenAICompatEngine(
            provider=body.provider or "custom",
            model=body.model,
            api_key=key,
            base_url=base,
            timeout_s=30,
            max_output_tokens=32,
        )
        t0 = asyncio.get_event_loop().time()
        try:
            reply = engine.complete("You are a health checker.", "Reply with exactly: pong", max_tokens=16)
        except Exception as exc:
            elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
            return {"ok": False, "error": str(exc)[:300], "latency_ms": elapsed}
        elapsed = int((asyncio.get_event_loop().time() - t0) * 1000)
        return {
            "ok": True,
            "reply": (reply or "").strip()[:80],
            "latency_ms": elapsed,
            "model": body.model,
            "base_url": base,
        }

    # ------------------------------------------------------------ 预览 / 产物
    @app.delete("/api/v1/jobs/{jid}", dependencies=[Depends(auth)])
    async def delete_job(jid: str, d: DB = Depends(get_db)) -> dict:
        """终止 = 删干净：停掉（如果在跑）→ 删记录 → 删成品。不可恢复。"""
        row = require_job(d, jid)
        owner = f"{socket.gethostname()}:{os.getpid()}"
        held, _ = d.job_lock(jid)
        if held.startswith(owner):
            rt.abort_flags.setdefault(jid, threading.Event()).set()
            for _ in range(60):
                await asyncio.sleep(1)
                if not d.job_lock(jid)[0]:
                    break
            else:
                raise HTTPException(409, "任务仍在收尾（最长约 60 秒），请稍后再试")

        removed: list[str] = []
        for key in ("out_path", "out_path_mono"):
            raw = row[key]
            if not raw:
                continue
            path = Path(raw)
            if path.exists():
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError as exc:
                    raise HTTPException(500, f"删不掉成品文件：{exc}")
        d.delete_job(jid)
        return {"deleted": True, "job_id": jid, "files_removed": removed}

    @app.post("/api/v1/jobs/{jid}/build", dependencies=[Depends(auth)])
    async def build_now(jid: str, d: DB = Depends(get_db)) -> dict:
        """把"截至目前已译完的部分"组装成 epub。

        存在的理由：任务跑到一半想先看看效果时，原来只能等整本跑完。
        生成的文件会在文件名和响应头里标明 partial，不会被误当成完整版。
        """
        row = require_job(d, jid)
        if not row["out_path"]:
            raise HTTPException(400, "这个任务没有输出路径")
        src, out = Path(row["src_path"]), Path(row["out_path"])
        if not src.exists():
            raise HTTPException(400, "源文件不存在")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            res = assemble_outputs(
                d, jid, src, out,
                bilingual=bool(row["bilingual"] if row["bilingual"] is not None else True),
                emit_mono=True,
                allow_partial=True,
                log=lambda *_: None,
            )
        except OSError as exc:
            raise HTTPException(400, f"写不进输出目录：{exc}（检查 out_path 权限）")
        if not res.bilingual_path or res.blocks == 0:
            # 一段都没译完时组装出来的就是原书，别拿这个当"成品"骗人
            for f in (res.bilingual_path, res.mono_path):
                if f:
                    Path(f).unlink(missing_ok=True)
            raise HTTPException(400, "还没有可组装的内容：一段都还没译完")
        if res.mono_path:
            d.set_mono_path(jid, str(res.mono_path))
        info = {
            "job_id": jid,
            "chapters": res.chapters,
            "blocks": res.blocks,
            "partial_blocks": res.skipped_blocks,
            "partial": res.skipped_blocks > 0,
            "out": str(res.bilingual_path),
            "mono": str(res.mono_path) if res.mono_path else None,
        }
        d.event(jid, "job.built", info)
        return info

    @app.get("/api/v1/jobs/{jid}/preview", dependencies=[Depends(auth)])
    async def preview_chapter(jid: str, chapter: int = 0, d: DB = Depends(get_db)):
        row = require_job(d, jid)
        out = Path(row["out_path"]) if row["out_path"] else None
        if not out or not out.exists():
            raise HTTPException(404, "成品还没生成")
        ch = d.conn.execute(
            "SELECT href FROM chapters WHERE job_id=? AND idx=?", (jid, chapter)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "没有这一章")
        with _zipfile.ZipFile(out) as z:
            if ch["href"] not in z.namelist():
                raise HTTPException(404, "成品里没有这一章")
            data = z.read(ch["href"])
        return Response(content=data, media_type="application/xhtml+xml")

    @app.get("/api/v1/artifacts/{jid}", dependencies=[Depends(auth)])
    async def artifact(jid: str, variant: str = "bilingual", d: DB = Depends(get_db)):
        require_job(d, jid)
        p = d.artifact_path(jid, variant)
        if variant == "mono" and (not p or not p.exists()):
            raise HTTPException(
                404, "这个任务还没有纯译文版。双语任务重新组装一次就会同时产出 mono 版。"
            )
        if not p or not p.exists():
            raise HTTPException(404, "成品还没生成")
        info = _completeness(d.job(jid))
        headers = {"X-Slowtrans-Partial": "0"}
        filename = p.name
        if info["partial"]:
            # 文件名直接标注，避免用户以为拿到的是完整版
            filename = f"{p.stem}.partial{p.suffix}"
            headers = {
                "X-Slowtrans-Partial": "1",
                "X-Slowtrans-Pending-Segments": str(info["pending_segments"]),
                "X-Slowtrans-Partial-Blocks": str(info["partial_blocks"]),
            }
        return FileResponse(
            p, filename=filename, media_type="application/epub+zip", headers=headers
        )

    # ------------------------------------------------------------ hooks / 能力
    @app.post("/api/v1/hooks", dependencies=[Depends(auth)])
    async def add_hook(body: HookCreate, d: DB = Depends(get_db)) -> dict:
        hook_id = d.add_hook(body.url, body.events, body.secret)
        return {"hook_id": hook_id, "events": body.events}

    @app.get("/api/v1/hooks", dependencies=[Depends(auth)])
    async def list_hooks(d: DB = Depends(get_db)) -> dict:
        return {"hooks": [dict(r) for r in d.active_hooks()]}

    @app.get("/api/v1/capabilities")
    async def capabilities() -> dict:
        return {
            "translate": {"epub": True, "bilingual": True, "mono": True, "resume": True},
            "convert": {"implemented": False, "status": "not_implemented"},
            "engines": ["openai_compat", "mock"],
            "events": "sse",
            "hooks": True,
        }

    return app


# ---------------------------------------------------------------- 辅助
def _completeness(r) -> dict:
    """这次组装 / 下载到底是不是"完整的"。"""
    total_seg = r["total_segments"] or 0
    done_seg = r["done_segments"] or 0
    pending = max(0, total_seg - done_seg)
    total_blocks = r["total_blocks"] or 0
    partial_blocks = r["partial_blocks"] or 0
    partial = pending > 0 or partial_blocks > 0
    return {
        "partial": bool(partial),
        "percent": round(100 * done_seg / total_seg) if total_seg else 0,
        "pending_segments": pending,
        "total_blocks": total_blocks,
        "partial_blocks": partial_blocks,
    }


def _job_dict(r) -> dict:
    def art(path):
        p = Path(path) if path else None
        exists = bool(p and p.exists())
        return {
            "path": str(p) if p else None,
            "exists": exists,
            "size": p.stat().st_size if exists else 0,
        }

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
        "completeness": _completeness(r),
        "artifacts": {
            "bilingual": art(r["out_path"]),
            "mono": art(r["out_path_mono"]),
        },
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
        "error": r["error"],
    }


async def _sse(data_dir: Path, request: Request, jid: str | None, last_id: int):
    """一个 SSE 连接只开一个 SQLite 连接。

    早期版本在 while 里每 0.5 秒 new 一次连接，靠 GC 回收——
    面板开几个标签页就是每秒十几次连接创建。
    """

    async def gen():
        cursor = last_id
        idle = 0.0
        d = DB(data_dir / "slowtrans.db")
        try:
            while True:
                if await request.is_disconnected():
                    break
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
                        yield (
                            f"id: {r['id']}\nevent: {r['type']}\n"
                            f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        )
                    idle = 0.0
                else:
                    idle += SSE_TICK
                    if idle >= 15:
                        yield ": keepalive\n\n"
                        idle = 0.0
                await asyncio.sleep(SSE_TICK)
        finally:
            d.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _worker(app: FastAPI) -> None:
    rt: Runtime = app.state.rt
    while True:
        jid = await rt.queue.get()
        rt.queued.discard(jid)
        try:
            await asyncio.to_thread(_run_job, rt, jid)
        except JobAborted:
            with DB(rt.data_dir / "slowtrans.db") as d:
                d.set_state(jid, "cancelled", "用户终止")
                d.event(jid, "job.cancelled", {})
                notify.dispatch(d, jid, "job.cancelled", {})
        except Exception as exc:  # 一个任务炸了不能带走整个服务
            with DB(rt.data_dir / "slowtrans.db") as d:
                d.set_state(jid, "failed", str(exc)[:500])
                d.event(jid, "job.failed", {"error": str(exc)[:300]})
                notify.dispatch(d, jid, "job.failed", {"error": str(exc)[:300]})
        finally:
            rt.abort_flags.pop(jid, None)
            rt.stop_flags.pop(jid, None)
            rt.queue.task_done()
            try:
                with DB(rt.data_dir / "slowtrans.db") as d:
                    d.prune()
            except Exception:
                pass


def _run_job(rt: Runtime, jid: str) -> None:
    cfg = load_config(rt.config_path, rt.data_dir)
    validate_config(cfg)
    with DB(rt.data_dir / "slowtrans.db") as d:
        row = d.job(jid)
        if row["state"] in TERMINAL:
            return
        provider, model = row["provider"], row["model"]
        engine = engine_from_config(cfg, provider, model)
        stop = rt.stop_flags.setdefault(jid, threading.Event())
        abort = rt.abort_flags.setdefault(jid, threading.Event())
        opts = options_from_config(
            cfg,
            src=Path(row["src_path"]),
            out=Path(row["out_path"]),
            lang_from=row["lang_from"],
            lang_to=row["lang_to"],
            bilingual=bool(row["bilingual"] if row["bilingual"] is not None else True),
            # 0 表示"任务没设预算"，让它回落到 config
            budget_cny=float(row["budget_cny"] or 0) or None,
            emit_mono=True,
        )
        Pipeline(
            opts, d, engine, provider, model,
            should_stop=stop.is_set,
            should_abort=abort.is_set,
        ).run()
