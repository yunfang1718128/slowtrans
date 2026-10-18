"""Webhook 派发。

存在的意义：AstrBot 插件、本地面板、CI 之外的系统都想知道"这本书译完了"。
之前只提供了 POST /hooks 注册接口却从不发送——虚假接口比没有更糟。

设计约束：
- 必须不阻塞翻译：后台线程发，失败只吞掉。
- 必须能验证：带 HMAC-SHA256 签名（如果注册时给了 secret）。
- 事件名与 SSE 里的一致，客户端同一套判断逻辑。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 5.0


def dispatch(db, job_id: str, event: str, data: dict) -> None:
    hooks = db.active_hooks()
    if not hooks:
        return
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "event": event,
        "data": data,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for hook in hooks:
        allowed = [x.strip() for x in (hook["events"] or "").split(",") if x.strip()]
        if allowed and event not in allowed:
            continue
        threading.Thread(
            target=_post,
            args=(hook["url"], hook["secret"] or "", body),
            daemon=True,
            name=f"hook-{hook['id']}",
        ).start()


def _post(url: str, secret: str, body: bytes) -> None:
    headers = {"Content-Type": "application/json", "User-Agent": "slowtrans-hook/0.1"}
    if secret:
        sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Slowtrans-Signature"] = f"sha256={sig}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            resp.read(256)
    except (urllib.error.URLError, OSError, ValueError):
        # webhook 挂掉是接收方的事，绝不能影响翻译进度
        pass
