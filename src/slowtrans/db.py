"""SQLite 状态库：任务、分段、译文缓存、术语表、事件流。

三条设计约束：
1. 译文只存在库里，成品 epub 最后才组装 —— 任何时刻崩溃都不会留下半个坏文件。
2. 每批提交一次事务 —— kill -9 之后最多丢一批，重跑不会重复付费。
3. 键是 (job_id, chapter, block, seg) 而不是全局序号 —— 章节增删不会错位。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  src_path TEXT NOT NULL,
  out_path TEXT,
  out_path_mono TEXT,
  lang_from TEXT NOT NULL,
  lang_to TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'created',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  total_segments INTEGER NOT NULL DEFAULT 0,
  done_segments INTEGER NOT NULL DEFAULT 0,
  failed_segments INTEGER NOT NULL DEFAULT 0,
  review_segments INTEGER NOT NULL DEFAULT 0,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  error TEXT
);

CREATE TABLE IF NOT EXISTS chapters(
  job_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  href TEXT NOT NULL,
  title TEXT,
  seg_total INTEGER NOT NULL DEFAULT 0,
  seg_done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(job_id, idx)
);

CREATE TABLE IF NOT EXISTS segments(
  job_id TEXT NOT NULL,
  chapter_idx INTEGER NOT NULL,
  block_idx INTEGER NOT NULL,
  seg_idx INTEGER NOT NULL,
  src_hash TEXT NOT NULL,
  src_text TEXT NOT NULL,
  ph_count INTEGER NOT NULL DEFAULT 0,
  dst_text TEXT,
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  model TEXT,
  updated_at TEXT,
  PRIMARY KEY(job_id, chapter_idx, block_idx, seg_idx)
);
CREATE INDEX IF NOT EXISTS idx_seg_state ON segments(job_id, state);
CREATE INDEX IF NOT EXISTS idx_seg_hash  ON segments(src_hash);

CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY,
  dst_text TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glossary(
  job_id TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'term',
  locked INTEGER NOT NULL DEFAULT 1,
  note TEXT,
  hits INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(job_id, src)
);

CREATE TABLE IF NOT EXISTS style_cards(
  job_id TEXT PRIMARY KEY,
  body TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  type TEXT NOT NULL,
  ts TEXT NOT NULL,
  data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_text(s: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", s).split())


def text_hash(s: str) -> str:
    return hashlib.sha256(norm_text(s).encode("utf-8")).hexdigest()


_DIGEST_CACHE: dict[tuple[str, int, float], str] = {}


def _file_digest(path: Path) -> str:
    """内容哈希（按 mtime/size 缓存）。

    早期用文件大小当标识：同名同大小换一本书会沿用旧进度，
    症状是"新书一打开就显示已完成"。哈希一遍几十 MB 的书只要零点几秒。
    """
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime)
    hit = _DIGEST_CACHE.get(key)
    if hit:
        return hit
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _DIGEST_CACHE[key] = digest
    return digest


def job_id_for(src_path: str | Path, lang_from: str, lang_to: str, provider: str, model: str) -> str:
    """同一本书 + 同一引擎 => 同一个 job_id，天然支持断点续跑。"""
    st = Path(src_path).resolve()
    payload = f"{st}|{_file_digest(st)}|{lang_from}|{lang_to}|{provider}|{model}"
    return "j_" + hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class Segment:
    chapter_idx: int
    block_idx: int
    seg_idx: int
    src_text: str
    ph_count: int
    dst_text: str | None = None
    state: str = "pending"
    attempts: int = 0


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 的同步依赖在线程池里建连接，
        # 路由函数在事件循环线程里用它。每个请求一个独立连接，不会并发共用。
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, check_same_thread=False, timeout=30
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _migrate(self) -> None:
        """老库平滑升级：SQLite 没有 IF NOT EXISTS 加列，只能先查。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for col, decl in (
            ("out_path_mono", "TEXT"),
            ("budget_cny", "REAL NOT NULL DEFAULT 0"),
            ("bilingual", "INTEGER NOT NULL DEFAULT 1"),
            ("locked_by", "TEXT"),
            ("locked_at", "TEXT"),
            ("total_blocks", "INTEGER NOT NULL DEFAULT 0"),
            ("partial_blocks", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")

    # ---------- job ----------
    def ensure_job(self, jid: str, **kw) -> None:
        ts = now()
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs(id,src_path,out_path,lang_from,lang_to,provider,model,"
            "state,created_at,updated_at,budget_cny,bilingual) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, kw["src_path"], kw.get("out_path"), kw["lang_from"], kw["lang_to"],
             kw["provider"], kw["model"], "created", ts, ts,
             float(kw.get("budget_cny") or 0), int(bool(kw.get("bilingual", True)))),
        )

    # 允许的状态迁移。故意宽松：只用来发现"绕过 queued 直接重跑"这类异常，
    # 不阻断流程——把用户卡在某个状态比状态机不严谨更糟。
    _ALLOWED = {
        "created": {"queued", "segmenting", "failed", "cancelled"},
        "queued": {"segmenting", "analyzing", "done", "failed", "cancelled", "paused"},
        "segmenting": {"analyzing", "translating", "failed", "cancelled", "paused"},
        "analyzing": {"translating", "failed", "cancelled", "paused", "awaiting_review"},
        "awaiting_review": {"translating", "cancelled", "paused"},
        "translating": {"done", "failed", "cancelled", "cancelling", "paused", "verifying"},
        "verifying": {"done", "failed", "paused"},
        "paused": {"queued", "translating", "segmenting", "failed", "cancelled", "done"},
        "done": {"queued", "done"},
        "failed": {"queued", "done", "cancelled"},
        "cancelling": {"cancelled", "done", "failed"},
        "cancelled": {"queued", "cancelled"},
    }

    def set_state(self, jid: str, state: str, error: str | None = None) -> None:
        row = self.conn.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()
        if row and row["state"] != state:
            allowed = self._ALLOWED.get(row["state"])
            if allowed is not None and state not in allowed:
                import sys

                sys.stderr.write(
                    f"[slowtrans] 非预期状态迁移 {row['state']} -> {state} (job {jid})\n"
                )
        self.conn.execute(
            "UPDATE jobs SET state=?, error=?, updated_at=? WHERE id=?", (state, error, now(), jid)
        )
        self.event(jid, f"job.{state}", {})

    def job(self, jid: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if row is None:
            raise KeyError(jid)
        return row

    def refresh_stats(self, jid: str) -> None:
        r = self.conn.execute(
            "SELECT COUNT(*) total,"
            " SUM(state='done') done,"
            " SUM(state='failed') failed,"
            " SUM(state='review') review"
            " FROM segments WHERE job_id=?",
            (jid,),
        ).fetchone()
        self.conn.execute(
            "UPDATE jobs SET total_segments=?, done_segments=?, failed_segments=?,"
            " review_segments=?, updated_at=? WHERE id=?",
            (r["total"] or 0, r["done"] or 0, r["failed"] or 0, r["review"] or 0, now(), jid),
        )

    # ---------- chapters / segments ----------
    def add_chapter(self, jid: str, idx: int, href: str, title: str, total: int) -> None:
        self.conn.execute(
            "INSERT INTO chapters(job_id,idx,href,title,seg_total,seg_done) VALUES(?,?,?,?,?,0)"
            " ON CONFLICT(job_id,idx) DO UPDATE SET href=excluded.href,"
            " title=excluded.title, seg_total=excluded.seg_total",
            (jid, idx, href, title, total),
        )

    def chapter_has_segments(self, jid: str, idx: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM segments WHERE job_id=? AND chapter_idx=? LIMIT 1", (jid, idx)
        ).fetchone()
        return row is not None

    def insert_segments(self, jid: str, segs: Iterable[Segment]) -> tuple[int, int]:
        """返回 (新增, 因分段规则变化而重置)。

        分段规则改进后，老任务的 src_text 会变。主键 INSERT OR IGNORE 不会更新它，
        结果就是"标题永远漏译"。这里显式比对模板，变了就重置成 pending 重译。
        """
        added = changed = 0
        self.conn.execute("BEGIN")
        for s in segs:
            row = self.conn.execute(
                "SELECT src_text FROM segments WHERE job_id=? AND chapter_idx=?"
                " AND block_idx=? AND seg_idx=?",
                (jid, s.chapter_idx, s.block_idx, s.seg_idx),
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO segments(job_id,chapter_idx,block_idx,seg_idx,"
                    "src_hash,src_text,ph_count,state) VALUES(?,?,?,?,?,?,?,'pending')",
                    (jid, s.chapter_idx, s.block_idx, s.seg_idx, text_hash(s.src_text),
                     s.src_text, s.ph_count),
                )
                added += 1
            elif row["src_text"] != s.src_text:
                self.conn.execute(
                    "UPDATE segments SET src_text=?, src_hash=?, ph_count=?, state='pending',"
                    " dst_text=NULL, error=NULL, attempts=0 WHERE job_id=? AND chapter_idx=?"
                    " AND block_idx=? AND seg_idx=?",
                    (s.src_text, text_hash(s.src_text), s.ph_count, jid, s.chapter_idx,
                     s.block_idx, s.seg_idx),
                )
                changed += 1
        self.conn.execute("COMMIT")
        return added, changed

    def pending_segments(self, jid: str, chapter_idx: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM segments WHERE job_id=? AND chapter_idx=? AND state IN ('pending','failed')"
            " ORDER BY block_idx, seg_idx",
            (jid, chapter_idx),
        ).fetchall()

    def all_segments(self, jid: str, state: str | None = None) -> list[sqlite3.Row]:
        if state:
            return self.conn.execute(
                "SELECT * FROM segments WHERE job_id=? AND state=? ORDER BY chapter_idx,block_idx,seg_idx",
                (jid, state),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM segments WHERE job_id=? ORDER BY chapter_idx,block_idx,seg_idx", (jid,)
        ).fetchall()

    def mark_done(self, jid: str, key: tuple[int, int, int], dst: str, model: str) -> None:
        self.conn.execute(
            "UPDATE segments SET dst_text=?,state='done',model=?,error=NULL,updated_at=?"
            " WHERE job_id=? AND chapter_idx=? AND block_idx=? AND seg_idx=?",
            (dst, model, now(), jid, *key),
        )

    def mark_failed(self, jid: str, key: tuple[int, int, int], err: str, attempts: int) -> None:
        self.conn.execute(
            "UPDATE segments SET state='failed',attempts=?,error=?,updated_at=?"
            " WHERE job_id=? AND chapter_idx=? AND block_idx=? AND seg_idx=?",
            (attempts, err[:500], now(), jid, *key),
        )

    def mark_review(self, jid: str, key: tuple[int, int, int], dst: str, note: str) -> None:
        self.conn.execute(
            "UPDATE segments SET dst_text=?,state='review',error=?,updated_at=?"
            " WHERE job_id=? AND chapter_idx=? AND block_idx=? AND seg_idx=?",
            (dst, note[:500], now(), jid, *key),
        )

    def touch_chapter(self, jid: str, idx: int) -> None:
        self.conn.execute(
            "UPDATE chapters SET seg_done=(SELECT COUNT(*) FROM segments"
            " WHERE job_id=? AND chapter_idx=? AND state IN ('done','review'))"
            " WHERE job_id=? AND idx=?",
            (jid, idx, jid, idx),
        )

    # ---------- 任务锁 ----------
    def try_lock(self, jid: str, owner: str, stale_seconds: int = 900) -> bool:
        """CAS 抢任务锁。

        CLI 和服务可能同时指向同一个 job_id：两边都从 pending 取段、都调 API。
        写库幂等不会丢数据，但会重复花钱。锁是唯一的防线。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        cur = self.conn.execute(
            "UPDATE jobs SET locked_by=?, locked_at=? WHERE id=?"
            " AND (locked_by IS NULL OR locked_by='' OR locked_at < ?)",
            (owner, now(), jid, cutoff),
        )
        return cur.rowcount == 1

    def touch_lock(self, jid: str, owner: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET locked_at=? WHERE id=? AND locked_by=?", (now(), jid, owner)
        )

    def release_lock(self, jid: str, owner: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET locked_by=NULL, locked_at=NULL WHERE id=? AND locked_by=?",
            (jid, owner),
        )

    def job_lock(self, jid: str) -> tuple[str, str]:
        row = self.conn.execute(
            "SELECT locked_by, locked_at FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        return (row["locked_by"] or "", row["locked_at"] or "") if row else ("", "")

    # ---------- 查询视图（供 HTTP 层用，避免 API 里写裸 SQL） ----------
    def list_jobs(self, state: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        if state:
            return self.conn.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def job_chapters(self, jid: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT idx,href,title,seg_total,seg_done FROM chapters WHERE job_id=? ORDER BY idx",
            (jid,),
        ).fetchall()

    def job_review(self, jid: str, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT chapter_idx,block_idx,seg_idx,src_text,error FROM segments"
            " WHERE job_id=? AND state='review' LIMIT ?",
            (jid, limit),
        ).fetchall()

    def job_log(self, jid: str, limit: int = 200) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT id,type,ts,data FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (jid, limit),
        ).fetchall()
        return rows[::-1]

    def jobs_by_src(self) -> dict[str, sqlite3.Row]:
        return {
            str(Path(r["src_path"]).resolve()): r
            for r in self.conn.execute("SELECT * FROM jobs")
        }

    def mark_queued(self, jid: str, state: str = "queued") -> None:
        self.conn.execute(
            "UPDATE jobs SET state=?, updated_at=? WHERE id=?", (state, now(), jid)
        )

    def chapter_done_blocks(self, jid: str, chapter_idx: int) -> tuple[dict[int, int], dict[int, list]]:
        """返回 (每块期望段数, 每块已完成译文)。只查一章，组装时不再全量加载。"""
        expect: dict[int, int] = {}
        done: dict[int, list] = {}
        for r in self.conn.execute(
            "SELECT block_idx, seg_idx, state, dst_text FROM segments"
            " WHERE job_id=? AND chapter_idx=? ORDER BY block_idx, seg_idx",
            (jid, chapter_idx),
        ):
            b = r["block_idx"]
            expect[b] = expect.get(b, 0) + 1
            if r["state"] == "done" and r["dst_text"]:
                done.setdefault(b, []).append((r["seg_idx"], r["dst_text"]))
        return expect, done

    def chapter_idx_by_href(self, jid: str) -> dict[str, int]:
        return {
            r["href"]: r["idx"]
            for r in self.conn.execute(
                "SELECT idx, href FROM chapters WHERE job_id=?", (jid,)
            )
        }

    # ---------- hooks ----------
    def add_hook(self, url: str, events: list[str], secret: str = "") -> int:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS hooks(id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " url TEXT NOT NULL, events TEXT, secret TEXT, active INTEGER DEFAULT 1)"
        )
        cur = self.conn.execute(
            "INSERT INTO hooks(url,events,secret,active) VALUES(?,?,?,1)",
            (url, ",".join(events), secret),
        )
        return int(cur.lastrowid)

    def active_hooks(self) -> list[sqlite3.Row]:
        try:
            return self.conn.execute(
                "SELECT id,url,events,secret FROM hooks WHERE active=1"
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    # ---------- 清理 ----------
    def prune(self, events_days: int = 7, cache_days: int = 30) -> tuple[int, int]:
        """长期运行必须有 TTL，否则 events/cache 会无声地吃掉磁盘。"""
        e_cut = (datetime.now(timezone.utc) - timedelta(days=events_days)).isoformat()
        c_cut = (datetime.now(timezone.utc) - timedelta(days=cache_days)).isoformat()
        e = self.conn.execute("DELETE FROM events WHERE ts < ?", (e_cut,)).rowcount
        c = self.conn.execute("DELETE FROM cache WHERE created_at < ?", (c_cut,)).rowcount
        return e, c

    # ---------- cache ----------
    def cache_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT dst_text FROM cache WHERE key=?", (key,)).fetchone()
        return row["dst_text"] if row else None

    def cache_put(self, key: str, dst: str, model: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO cache(key,dst_text,model,created_at) VALUES(?,?,?,?)",
            (key, dst, model, now()),
        )

    # ---------- glossary ----------
    def glossary(self, jid: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM glossary WHERE job_id=? ORDER BY hits DESC, src", (jid,)
        ).fetchall()

    def glossary_version(self, jid: str) -> int:
        rows = self.conn.execute(
            "SELECT src,dst FROM glossary WHERE job_id=? ORDER BY src", (jid,)
        ).fetchall()
        payload = "\n".join(f"{r['src']}={r['dst']}" for r in rows)
        return int(hashlib.sha256(payload.encode()).hexdigest()[:8], 16)

    def put_glossary(self, jid: str, src: str, dst: str, kind: str = "term", locked: bool = True) -> None:
        self.conn.execute(
            "INSERT INTO glossary(job_id,src,dst,kind,locked) VALUES(?,?,?,?,?)"
            " ON CONFLICT(job_id,src) DO UPDATE SET dst=excluded.dst, locked=excluded.locked",
            (jid, src, dst, kind, int(locked)),
        )

    def bump_glossary_hits(self, jid: str, hits: dict[str, int]) -> None:
        if not hits:
            return
        self.conn.execute("BEGIN")
        for src, n in hits.items():
            self.conn.execute(
                "UPDATE glossary SET hits=hits+? WHERE job_id=? AND src=?", (n, jid, src)
            )
        self.conn.execute("COMMIT")

    # ---------- style ----------
    def style_card(self, jid: str) -> tuple[str, int]:
        row = self.conn.execute(
            "SELECT body,version FROM style_cards WHERE job_id=?", (jid,)
        ).fetchone()
        return (row["body"], row["version"]) if row else ("", 0)

    def put_style_card(self, jid: str, body: str) -> int:
        old = self.conn.execute(
            "SELECT version FROM style_cards WHERE job_id=?", (jid,)
        ).fetchone()
        ver = (old["version"] + 1) if old else 1
        self.conn.execute(
            "INSERT OR REPLACE INTO style_cards(job_id,body,version,created_at) VALUES(?,?,?,?)",
            (jid, body, ver, now()),
        )
        return ver

    # ---------- events ----------
    def event(self, jid: str, type_: str, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO events(job_id,type,ts,data) VALUES(?,?,?,?)",
            (jid, type_, now(), json.dumps(data, ensure_ascii=False)),
        )

    def events_since(self, jid: str | None, last_id: int, limit: int = 200) -> list[sqlite3.Row]:
        if jid:
            return self.conn.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (jid, last_id, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (last_id, limit)
        ).fetchall()

    def set_build_stats(self, jid: str, total_blocks: int, partial_blocks: int) -> None:
        """记录这次组装有多少块因为没译完而被跳过。"""
        self.conn.execute(
            "UPDATE jobs SET total_blocks=?, partial_blocks=? WHERE id=?",
            (total_blocks, partial_blocks, jid),
        )

    def set_mono_path(self, jid: str, path: str) -> None:
        self.conn.execute("UPDATE jobs SET out_path_mono=? WHERE id=?", (path, jid))

    def artifact_path(self, jid: str, variant: str = "bilingual") -> Path | None:
        row = self.conn.execute(
            "SELECT out_path, out_path_mono FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        if not row:
            return None
        p = row["out_path_mono"] if variant == "mono" else row["out_path"]
        return Path(p) if p else None
