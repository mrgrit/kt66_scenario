"""kt66_scenario — 저장소.

SQLite 하나다. 강사 1명 · 학생 수십 명 규모에서 Postgres 를 세울 이유가 없고,
파일 하나라 백업이 `cp` 한 번이라는 것이 교육 운영에서 실제로 중요하다.

── 왜 폴링인가 ────────────────────────────────────────────────────
학생 서버는 각자의 망에 있고 NAT 뒤인 경우가 흔하다. 중앙에서 밀어 넣으려면
inbound 를 열어야 하는데, 그건 실습 환경마다 방화벽을 손대야 한다는 뜻이다.
그래서 **학생 에이전트가 중앙으로 물어보러 온다**. 중앙은 큐에 넣기만 한다.
kt66 이 GPU 존에 WireGuard 를 고른 것과 같은 이유다 — NAT 를 이기려 하지 않는다.

── 무엇을 저장하는가 ──────────────────────────────────────────────
증거 '요약'이다. 판정 결과 + 근거 스니펫 + 타임라인.
셸 히스토리 전량을 빨아들이지 않는다. 이유는 두 가지다:
  ① 사후 리뷰와 이의제기에 필요한 것은 "무엇을 근거로 그렇게 판정했는가"이지
     학생이 친 모든 명령이 아니다.
  ② 전량 수집은 학생 서버에 계측을 깔아야 하고, 그 계측 자체가 실습 환경을 바꾼다.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "kt66s.db"

SCHEMA = """
-- 학생 = 랩 1대. hostname 이 아니라 발급 토큰이 신원이다(재설치해도 이어진다).
CREATE TABLE IF NOT EXISTS student (
  id          INTEGER PRIMARY KEY,
  token       TEXT UNIQUE NOT NULL,      -- 에이전트가 들고 오는 신원
  name        TEXT NOT NULL,             -- 표시 이름 (학번·이름)
  cohort      TEXT DEFAULT '',           -- 분반. 반별 발사에 쓴다
  lab_url     TEXT DEFAULT '',           -- 학생 랩의 envsim/injector 진입점 (에이전트가 보고)
  host        TEXT DEFAULT '',
  registered  REAL NOT NULL,
  last_seen   REAL,                      -- 폴링 시각. 연결 상태 판정의 근거
  meta        TEXT DEFAULT '{}'
);

-- 발사 1건. 강사가 "쏜다"고 할 때 생기는 단위.
CREATE TABLE IF NOT EXISTS launch (
  id          INTEGER PRIMARY KEY,
  scenario_id TEXT NOT NULL,
  student_id  INTEGER NOT NULL REFERENCES student(id),
  mode        TEXT NOT NULL,             -- manual | scheduled | auto
  state       TEXT NOT NULL,             -- queued | delivered | running | done | failed | cancelled
  created     REAL NOT NULL,
  delivered   REAL,
  started     REAL,
  finished    REAL,
  deadline    REAL,                      -- SLA 판정 기준 시각
  batch       TEXT DEFAULT '',           -- 같은 발사 묶음(전체 발사 시 동일 값)
  detail      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_launch_student ON launch(student_id, state);
CREATE INDEX IF NOT EXISTS idx_launch_batch   ON launch(batch);

-- 타임라인형 시나리오의 각 단계. 시간이 지나며 문제가 늘어나는 것이 여기서 나온다.
CREATE TABLE IF NOT EXISTS step (
  id          INTEGER PRIMARY KEY,
  launch_id   INTEGER NOT NULL REFERENCES launch(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  at_sec      INTEGER NOT NULL,          -- 발사 후 몇 초에 터지는가
  spec        TEXT NOT NULL,             -- 주입 명세 JSON
  state       TEXT NOT NULL DEFAULT 'pending',   -- pending | delivered | applied | failed
  applied     REAL,
  result      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_step_launch ON step(launch_id, state);

-- 증거 요약. 학생 에이전트가 올려 보내는 것.
CREATE TABLE IF NOT EXISTS evidence (
  id          INTEGER PRIMARY KEY,
  launch_id   INTEGER NOT NULL REFERENCES launch(id) ON DELETE CASCADE,
  student_id  INTEGER NOT NULL REFERENCES student(id),
  ts          REAL NOT NULL,
  kind        TEXT NOT NULL,             -- action | observation | alarm | check | note
  check_id    TEXT DEFAULT '',           -- ground_truth 의 check 와 대응
  passed      INTEGER,                   -- 1 통과 · 0 실패 · NULL 판정 아님
  summary     TEXT NOT NULL,             -- 한 줄 요약
  snippet     TEXT DEFAULT '',           -- 근거 몇 줄. 전량이 아니라 발췌다
  source      TEXT DEFAULT ''            -- envsim | injector | shell | siem | agent
);
CREATE INDEX IF NOT EXISTS idx_ev_launch ON evidence(launch_id);

-- 채점 결과. launch 당 1건.
CREATE TABLE IF NOT EXISTS score (
  launch_id   INTEGER PRIMARY KEY REFERENCES launch(id) ON DELETE CASCADE,
  student_id  INTEGER NOT NULL,
  scenario_id TEXT NOT NULL,
  points      REAL NOT NULL DEFAULT 0,
  max_points  REAL NOT NULL DEFAULT 0,
  detect_sec  REAL,                      -- 최초 인지까지
  mitigate_sec REAL,                     -- 조치 완료까지
  sla_detect_ok   INTEGER DEFAULT 0,
  sla_mitigate_ok INTEGER DEFAULT 0,
  forbidden_hit   INTEGER DEFAULT 0,     -- 금지 행위를 했는가
  graded      REAL NOT NULL,
  detail      TEXT DEFAULT '{}'
);

-- 자동 발사 일정. 강사가 걸어 두고 수업을 진행한다.
CREATE TABLE IF NOT EXISTS schedule (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  every_sec   INTEGER NOT NULL,          -- 발사 간격
  pick        TEXT NOT NULL DEFAULT 'sequence',  -- sequence | random
  scenarios   TEXT NOT NULL,             -- JSON 배열. 돌려 가며 쏜다
  audience    TEXT NOT NULL DEFAULT 'all',       -- all | cohort:<name> | students:<id,id>
  cursor      INTEGER NOT NULL DEFAULT 0,
  next_fire   REAL,
  created     REAL NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15.0, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")     # 폴링이 잦다. 읽기가 쓰기를 막으면 안 된다
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def rows(c, sql, *a) -> list[dict]:
    return [dict(r) for r in c.execute(sql, a).fetchall()]


def one(c, sql, *a) -> dict | None:
    r = c.execute(sql, a).fetchone()
    return dict(r) if r else None


def js(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def unjs(s: str, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def now() -> float:
    return time.time()
