"""AstrBot 桥接插件：提交任务、查进度、完成后主动推送。

设计约束（和服务器现状强相关）：
- AstrBot 常驻就占 ~690 MB，这台机器一共 1.7 GB。
  所以插件只做 HTTP 客户端，绝不能把翻译逻辑塞进 Bot 进程。
- 只用标准库 urllib，避免和 AstrBot 的 httpx 版本打架。
- 主动推送用的是 AstrBot 4.x 的 context.send_message；如果版本不兼容，
  会退化成“只能手动查进度”，不会把插件搞崩。
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

TERMINAL = {"done", "failed", "cancelled"}


@register("slowtrans", "slowtrans", "慢速 EPUB 翻译桥接（提交/监控/推送）", "0.1.0")
class SlowtransPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.base = str(cfg.get("api_base", "http://127.0.0.1:8790")).rstrip("/")
        self.token = str(cfg.get("token", ""))
        self.poll = int(cfg.get("poll_seconds", 30))
        self.watchers: dict[str, asyncio.Task] = {}

    # ---------- HTTP ----------
    def _req(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"连不上 slowtrans（{self.base}）：{exc}") from exc

    # ---------- 命令 ----------
    @filter.command("翻译")
    async def cmd_translate(self, event: AstrMessageEvent, path: str = "", lang: str = "zh-CN"):
        """翻译一本服务器上的 epub：/翻译 /srv/webdav/data/books/x.epub [zh-CN]"""
        if not path:
            yield event.plain_result(
                "用法：/翻译 <服务器上的 epub 路径> [目标语言]\n"
                "例：/翻译 /srv/webdav/data/book.epub zh-CN\n"
                "注意：路径是**服务器**上的，不是本机的。"
            )
            return
        try:
            res = self._req("/api/v1/jobs", "POST", {
                "type": "translate", "src_path": path, "lang_to": lang,
            })
        except RuntimeError as exc:
            yield event.plain_result(f"提交失败：{exc}")
            return
        jid = res.get("job_id", "?")
        yield event.plain_result(
            f"已排队：{jid}\n"
            f"引擎会在后台按顺序跑，用 /译进度 {jid} 查看。\n"
            f"（免费 API 较慢，一本 200 页的书通常 20-60 分钟）"
        )
        umo = getattr(event, "unified_msg_origin", None)
        if umo and jid not in self.watchers:
            self.watchers[jid] = asyncio.create_task(self._watch(umo, jid))

    @filter.command("译进度")
    async def cmd_progress(self, event: AstrMessageEvent, job_id: str = ""):
        """查进度：/译进度 [job_id]"""
        try:
            if job_id:
                j = self._req(f"/api/v1/jobs/{job_id}")
                yield event.plain_result(self._fmt(j))
            else:
                jobs = self._req("/api/v1/jobs?limit=5")
                if not jobs:
                    yield event.plain_result("还没有任务。")
                    return
                yield event.plain_result("\n".join(self._fmt(j, short=True) for j in jobs))
        except RuntimeError as exc:
            yield event.plain_result(f"查询失败：{exc}")

    @filter.command("译暂停")
    async def cmd_pause(self, event: AstrMessageEvent, job_id: str = ""):
        """暂停：/译暂停 <job_id>"""
        if not job_id:
            yield event.plain_result("用法：/译暂停 <job_id>")
            return
        try:
            self._req(f"/api/v1/jobs/{job_id}/pause", "POST")
            yield event.plain_result(f"{job_id} 已标记暂停，当前批次结束后生效。")
        except RuntimeError as exc:
            yield event.plain_result(f"操作失败：{exc}")

    @filter.command("译继续")
    async def cmd_resume(self, event: AstrMessageEvent, job_id: str = ""):
        """继续：/译继续 <job_id>"""
        if not job_id:
            yield event.plain_result("用法：/译继续 <job_id>")
            return
        try:
            self._req(f"/api/v1/jobs/{job_id}/resume", "POST")
            yield event.plain_result(f"{job_id} 已重新排队。")
        except RuntimeError as exc:
            yield event.plain_result(f"操作失败：{exc}")

    # ---------- 主动推送 ----------
    async def _watch(self, umo: str, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.poll)
            try:
                j = self._req(f"/api/v1/jobs/{job_id}")
            except RuntimeError:
                continue
            if j.get("state") in TERMINAL:
                text = self._fmt(j)
                if j.get("state") == "done":
                    text = "✅ 翻译完成\n" + text + f"\n下载：/api/v1/artifacts/{job_id}（走 SSH 隧道）"
                try:
                    from astrbot.core.message.message_event_result import MessageChain

                    await self.context.send_message(umo, MessageChain().message(text))
                except Exception as exc:  # 版本不兼容时静默降级
                    logger.warning(f"slowtrans: 主动推送失败（不影响任务）：{exc}")
                self.watchers.pop(job_id, None)
                return

    @staticmethod
    def _fmt(j: dict, short: bool = False) -> str:
        s = j.get("segments", {})
        total = s.get("total") or 1
        pct = round(100 * (s.get("done") or 0) / total)
        line = (
            f"{j.get('job_id')} [{j.get('state')}] {pct}% "
            f"({s.get('done')}/{s.get('total')})"
            f"{' 待复核 ' + str(s.get('review')) if s.get('review') else ''}"
        )
        if short:
            return line
        src = str(j.get("src_path", "")).split("/")[-1]
        return (
            f"{line}\n书名：{src}\n引擎：{j.get('engine')}\n"
            f"tokens：{j.get('tokens', {}).get('in')}/{j.get('tokens', {}).get('out')}"
            + (f"\n错误：{j.get('error')}" if j.get("error") else "")
        )

    async def terminate(self):
        for t in self.watchers.values():
            t.cancel()
        self.watchers.clear()
