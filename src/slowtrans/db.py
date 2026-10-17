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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  src_path TEXT NOT NULL,
  out_path TEXT,
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


def job_id_for(src_path: str | Path, lang_from: str, lang_to: str, provider: str, model: str) -> str:
    """同一本书 + 同一引擎 => 同一个 job_id，天然支持断点续跑。"""
    st = Path(src_path).resolve()
    payload = f"{st}|{st.stat().st_size}|{lang_from}|{lang_to}|{provider}|{model}"
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
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---------- job ----------
    def ensure_job(self, jid: str, **kw) -> None:
        ts = now()
        self.conn.execute(
            "INSERT OR IGNORE INTO jobs(id,src_path,out_path,lang_from,lang_to,provider,model,"
            "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (jid, kw["src_path"], kw.get("out_path"), kw["lang_from"], kw["lang_to"],
             kw["provider"], kw["model"], "created", ts, ts),
        )

    def set_state(self, jid: str, state: str, error: str | None = None) -> None:
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

    def insert_segments(self, jid: str, segs: Iterable[Segment]) -> int:
        n = 0
        self.conn.execute("BEGIN")
        for s in segs:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO segments(job_id,chapter_idx,block_idx,seg_idx,"
                "src_hash,src_text,ph_count,state) VALUES(?,?,?,?,?,?,?,'pending')",
                (jid, s.chapter_idx, s.block_idx, s.seg_idx, text_hash(s.src_text),
                 s.src_text, s.ph_count),
            )
            n += cur.rowcount
        self.conn.execute("COMMIT")
        return n

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

    def artifact_path(self, jid: str) -> Path | None:
        row = self.conn.execute("SELECT out_path FROM jobs WHERE id=?", (jid,)).fetchone()
        return Path(row["out_path"]) if row and row["out_path"] else None
