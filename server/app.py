"""kt66_scenario — 강사용 중앙 시나리오 서버.

    강사 콘솔 ──▶ 발사 ──▶ [큐] ◀── 폴링 ── 학생 에이전트 ──▶ 학생 랩(envsim/injector)
                                    │
                                    └── 증거 요약 ──▶ 채점

중앙은 **학생 랩에 직접 접속하지 않는다.** 큐에 넣고 기다린다. 학생 서버가 NAT
뒤에 있어도, 강의실 망이 바뀌어도 동작한다. 대가는 지연(폴링 주기)뿐인데
실습 시나리오에서 몇 초의 지연은 문제가 되지 않는다.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db
import grading
import scenarios as scen

HERE = Path(__file__).parent
INSTRUCTOR_KEY = os.environ.get("INSTRUCTOR_KEY", "kt66s-instructor-2026")
POLL_HINT_SEC = int(os.environ.get("POLL_HINT_SEC", "5"))
OFFLINE_AFTER = 30.0          # 이 시간 넘게 안 오면 끊긴 것으로 본다

app = FastAPI(title="kt66_scenario", docs_url="/api/docs")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=str(HERE / "templates"))

CONN = db.connect()
db.init(CONN)
CATALOG: dict[str, dict] = {}
CAT_ERRORS: list[str] = []


def reload_catalog() -> None:
    global CATALOG, CAT_ERRORS
    CATALOG, CAT_ERRORS = scen.load_all()


reload_catalog()


# ── 인증 ────────────────────────────────────────────────────────────
# 강사 키와 학생 토큰을 분리한다. 학생 토큰으로 발사가 되면 실습이 성립하지 않는다.
def instructor(x_api_key: str = Header(default="")) -> bool:
    if not secrets.compare_digest(x_api_key, INSTRUCTOR_KEY):
        raise HTTPException(401, "강사 키가 필요하다")
    return True


def student_of(token: str) -> dict:
    s = db.one(CONN, "SELECT * FROM student WHERE token=?", token)
    if not s:
        raise HTTPException(401, "등록되지 않은 토큰이다")
    return s


# ── 모델 ────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    name: str
    cohort: str = ""
    lab_url: str = ""
    host: str = ""
    token: str = ""          # 재등록 시 기존 토큰을 들고 온다


class LaunchIn(BaseModel):
    scenario_id: str
    audience: str = "all"    # all | cohort:<name> | students:<id,id,...>
    mode: str = "manual"


class EvidenceIn(BaseModel):
    launch_id: int
    kind: str = "observation"
    check_id: str = ""
    passed: bool | None = None
    summary: str
    snippet: str = ""
    source: str = "agent"


class StepResult(BaseModel):
    step_id: int
    ok: bool
    result: str = ""


class ScheduleIn(BaseModel):
    name: str
    every_sec: int = Field(ge=30)
    scenarios: list[str]
    audience: str = "all"
    pick: str = "sequence"
    enabled: bool = True


# ── 학생 등록 ───────────────────────────────────────────────────────
@app.post("/api/register")
def register(r: RegisterIn):
    """학생 랩이 자기를 등록한다. 강사 키가 필요 없다 — 수업 시작이 매끄러워야 한다.

    대신 토큰은 서버가 발급한다. 학생이 남의 토큰을 추측해서 남의 발사를
    가로채는 것은 막아야 하므로 충분히 긴 난수를 쓴다.
    """
    if r.token:
        cur = db.one(CONN, "SELECT * FROM student WHERE token=?", r.token)
        if cur:
            CONN.execute(
                "UPDATE student SET name=?, cohort=?, lab_url=?, host=?, last_seen=? WHERE id=?",
                (r.name, r.cohort, r.lab_url, r.host, db.now(), cur["id"]))
            CONN.commit()
            return {"student_id": cur["id"], "token": r.token, "rejoined": True,
                    "poll_sec": POLL_HINT_SEC}
    token = secrets.token_urlsafe(24)
    cur = CONN.execute(
        "INSERT INTO student(token,name,cohort,lab_url,host,registered,last_seen) "
        "VALUES(?,?,?,?,?,?,?)",
        (token, r.name, r.cohort, r.lab_url, r.host, db.now(), db.now()))
    CONN.commit()
    return {"student_id": cur.lastrowid, "token": token, "rejoined": False,
            "poll_sec": POLL_HINT_SEC}


# ── 에이전트 폴링 ───────────────────────────────────────────────────
@app.get("/api/poll")
def poll(token: str):
    """학생 에이전트가 5초마다 물어본다. "지금 실행할 것이 있는가?"

    타임라인 시나리오의 단계는 **시각이 되어야** 내려간다. 미리 다 주면
    에이전트가 시계를 관리해야 하고, 학생이 에이전트를 멈추면 시나리오가
    통째로 사라진다. 시계는 중앙이 쥔다.
    """
    s = student_of(token)
    CONN.execute("UPDATE student SET last_seen=? WHERE id=?", (db.now(), s["id"]))
    CONN.commit()

    out = []
    launches = db.rows(
        CONN, "SELECT * FROM launch WHERE student_id=? AND state IN "
        "('queued','delivered','running') ORDER BY created", s["id"])
    for L in launches:
        if L["state"] == "queued":
            CONN.execute("UPDATE launch SET state='running', delivered=?, started=? WHERE id=?",
                         (db.now(), db.now(), L["id"]))
        base = L["started"] or L["created"]
        due = db.rows(
            CONN, "SELECT * FROM step WHERE launch_id=? AND state='pending' ORDER BY seq", L["id"])
        for st in due:
            if db.now() - base < st["at_sec"]:
                continue                      # 아직 시각이 안 됐다
            CONN.execute("UPDATE step SET state='delivered' WHERE id=?", (st["id"],))
            out.append({"step_id": st["id"], "launch_id": L["id"],
                        "scenario_id": L["scenario_id"], "spec": db.unjs(st["spec"])})
    CONN.commit()
    return {"tasks": out, "poll_sec": POLL_HINT_SEC, "server_time": db.now()}


@app.post("/api/step_result")
def step_result(r: StepResult, token: str):
    """에이전트가 주입 결과를 돌려준다. 실패도 반드시 올라와야 한다 —
    조용히 실패한 주입은 "학생이 이미 고쳤다"와 구분되지 않는다."""
    s = student_of(token)
    st = db.one(CONN, "SELECT s.*, l.student_id FROM step s JOIN launch l ON l.id=s.launch_id "
                      "WHERE s.id=?", r.step_id)
    if not st or st["student_id"] != s["id"]:
        raise HTTPException(404, "해당 단계가 없다")
    CONN.execute("UPDATE step SET state=?, applied=?, result=? WHERE id=?",
                 ("applied" if r.ok else "failed", db.now(), r.result[:2000], r.step_id))
    CONN.execute(
        "INSERT INTO evidence(launch_id,student_id,ts,kind,summary,snippet,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (st["launch_id"], s["id"], db.now(), "note",
         f"주입 {'성공' if r.ok else '실패'} — step {r.step_id}", r.result[:1000], "agent"))
    CONN.commit()
    return {"ok": True}


@app.post("/api/evidence")
def add_evidence(e: EvidenceIn, token: str):
    """증거 요약 수집. 전량이 아니라 발췌다(snippet 은 서버에서 잘라 저장한다)."""
    s = student_of(token)
    L = db.one(CONN, "SELECT * FROM launch WHERE id=? AND student_id=?", e.launch_id, s["id"])
    if not L:
        raise HTTPException(404, "해당 발사가 없다")
    CONN.execute(
        "INSERT INTO evidence(launch_id,student_id,ts,kind,check_id,passed,summary,snippet,source)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (e.launch_id, s["id"], db.now(), e.kind, e.check_id,
         None if e.passed is None else int(e.passed),
         e.summary[:500], e.snippet[:4000], e.source))
    CONN.commit()
    return {"ok": True}


@app.post("/api/finish")
def finish(launch_id: int, token: str):
    """학생이 대응을 마쳤다고 선언한다. 여기서 채점이 돈다."""
    s = student_of(token)
    L = db.one(CONN, "SELECT * FROM launch WHERE id=? AND student_id=?", launch_id, s["id"])
    if not L:
        raise HTTPException(404, "해당 발사가 없다")
    sc = CATALOG.get(L["scenario_id"])
    if not sc:
        raise HTTPException(400, f"카탈로그에 없는 시나리오다: {L['scenario_id']}")
    CONN.execute("UPDATE launch SET state='done', finished=? WHERE id=?", (db.now(), launch_id))
    CONN.commit()
    return grading.grade(CONN, L, sc)


# ── 강사 API ────────────────────────────────────────────────────────
def _audience(aud: str) -> list[dict]:
    if aud == "all":
        return db.rows(CONN, "SELECT * FROM student ORDER BY id")
    if aud.startswith("cohort:"):
        return db.rows(CONN, "SELECT * FROM student WHERE cohort=? ORDER BY id", aud[7:])
    if aud.startswith("students:"):
        ids = [i for i in aud[9:].split(",") if i.strip().isdigit()]
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        return db.rows(CONN, f"SELECT * FROM student WHERE id IN ({q}) ORDER BY id", *ids)
    return []


def _fire(scenario_id: str, audience: str, mode: str) -> dict:
    sc = CATALOG.get(scenario_id)
    if not sc:
        raise HTTPException(404, f"없는 시나리오다: {scenario_id}")
    targets = _audience(audience)
    if not targets:
        raise HTTPException(400, f"대상 학생이 없다: {audience}")
    batch = uuid.uuid4().hex[:12]
    made = []
    for stu in targets:
        cur = CONN.execute(
            "INSERT INTO launch(scenario_id,student_id,mode,state,created,deadline,batch,detail)"
            " VALUES(?,?,?,'queued',?,?,?,?)",
            (scenario_id, stu["id"], mode, db.now(),
             db.now() + sc["duration_sec"], batch, db.js({"title": sc["title"]})))
        lid = cur.lastrowid
        for i, st in enumerate(sc["steps"]):
            CONN.execute("INSERT INTO step(launch_id,seq,at_sec,spec) VALUES(?,?,?,?)",
                         (lid, i, st["at_sec"], db.js(st)))
        made.append({"launch_id": lid, "student_id": stu["id"], "name": stu["name"]})
    CONN.commit()
    return {"batch": batch, "scenario_id": scenario_id, "count": len(made), "launches": made}


@app.post("/api/launch", dependencies=[Depends(instructor)])
def launch(r: LaunchIn):
    """강사가 직접 쏜다. audience 로 전체·분반·개인을 가른다."""
    return _fire(r.scenario_id, r.audience, r.mode)


@app.post("/api/cancel", dependencies=[Depends(instructor)])
def cancel(batch: str = "", launch_id: int = 0):
    if launch_id:
        CONN.execute("UPDATE launch SET state='cancelled' WHERE id=? AND state NOT IN"
                     " ('done','cancelled')", (launch_id,))
    elif batch:
        CONN.execute("UPDATE launch SET state='cancelled' WHERE batch=? AND state NOT IN"
                     " ('done','cancelled')", (batch,))
    else:
        raise HTTPException(400, "batch 또는 launch_id 가 필요하다")
    CONN.commit()
    return {"ok": True}


@app.get("/api/catalog")
def catalog():
    return {"scenarios": [scen.summary(d) for d in CATALOG.values()],
            "errors": CAT_ERRORS, "count": len(CATALOG)}


@app.get("/api/scenario/{sid}")
def scenario_detail(sid: str):
    d = CATALOG.get(sid)
    if not d:
        raise HTTPException(404, "없는 시나리오다")
    return d


@app.post("/api/reload", dependencies=[Depends(instructor)])
def reload_():
    reload_catalog()
    return {"count": len(CATALOG), "errors": CAT_ERRORS}


@app.get("/api/students")
def students():
    out = db.rows(CONN, "SELECT * FROM student ORDER BY cohort, name")
    t = db.now()
    for s in out:
        s.pop("token", None)                    # 토큰은 목록에 싣지 않는다
        s["online"] = bool(s["last_seen"] and t - s["last_seen"] < OFFLINE_AFTER)
        s["active"] = db.one(
            CONN, "SELECT COUNT(*) n FROM launch WHERE student_id=? AND state IN"
            " ('queued','delivered','running')", s["id"])["n"]
    return {"students": out}


@app.get("/api/launches")
def launches(limit: int = 100, student_id: int = 0):
    q = ("SELECT l.*, s.name student_name FROM launch l JOIN student s ON s.id=l.student_id ")
    a: list = []
    if student_id:
        q += "WHERE l.student_id=? "
        a.append(student_id)
    q += "ORDER BY l.id DESC LIMIT ?"
    a.append(limit)
    out = db.rows(CONN, q, *a)
    for L in out:
        L["detail"] = db.unjs(L["detail"])
        L["steps"] = db.rows(CONN, "SELECT seq,at_sec,state,applied FROM step WHERE launch_id=?"
                                   " ORDER BY seq", L["id"])
        L["score"] = db.one(CONN, "SELECT * FROM score WHERE launch_id=?", L["id"])
    return {"launches": out}


@app.get("/api/evidence/{launch_id}")
def evidence(launch_id: int):
    return {"evidence": db.rows(
        CONN, "SELECT * FROM evidence WHERE launch_id=? ORDER BY ts", launch_id)}


@app.post("/api/grade/{launch_id}", dependencies=[Depends(instructor)])
def grade_now(launch_id: int):
    """강사가 강제로 채점한다. 학생이 finish 를 안 눌러도 수업은 넘어가야 한다."""
    L = db.one(CONN, "SELECT * FROM launch WHERE id=?", launch_id)
    if not L:
        raise HTTPException(404, "없는 발사다")
    sc = CATALOG.get(L["scenario_id"])
    if not sc:
        raise HTTPException(400, "카탈로그에 없는 시나리오다")
    return grading.grade(CONN, L, sc)


@app.get("/api/scoreboard")
def scoreboard():
    return {"rows": db.rows(CONN, """
        SELECT s.id, s.name, s.cohort,
               COUNT(sc.launch_id) attempts,
               ROUND(COALESCE(SUM(sc.points),0),1) points,
               ROUND(COALESCE(SUM(sc.max_points),0),1) max_points,
               SUM(sc.sla_detect_ok) detect_ok,
               SUM(sc.sla_mitigate_ok) mitigate_ok,
               SUM(sc.forbidden_hit) forbidden
        FROM student s LEFT JOIN score sc ON sc.student_id=s.id
        GROUP BY s.id ORDER BY points DESC, s.name""")}


# ── 자동 발사 ───────────────────────────────────────────────────────
@app.post("/api/schedule", dependencies=[Depends(instructor)])
def make_schedule(r: ScheduleIn):
    unknown = [s for s in r.scenarios if s not in CATALOG]
    if unknown:
        raise HTTPException(400, f"카탈로그에 없는 시나리오: {', '.join(unknown)}")
    cur = CONN.execute(
        "INSERT INTO schedule(name,enabled,every_sec,pick,scenarios,audience,next_fire,created)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (r.name, int(r.enabled), r.every_sec, r.pick, db.js(r.scenarios), r.audience,
         db.now() + r.every_sec, db.now()))
    CONN.commit()
    return {"schedule_id": cur.lastrowid}


@app.get("/api/schedules")
def list_schedules():
    out = db.rows(CONN, "SELECT * FROM schedule ORDER BY id DESC")
    for s in out:
        s["scenarios"] = db.unjs(s["scenarios"], [])
    return {"schedules": out}


@app.post("/api/schedule/{sid}/toggle", dependencies=[Depends(instructor)])
def toggle_schedule(sid: int, enabled: bool):
    CONN.execute("UPDATE schedule SET enabled=?, next_fire=? WHERE id=?",
                 (int(enabled), db.now() + 5, sid))
    CONN.commit()
    return {"ok": True}


@app.delete("/api/schedule/{sid}", dependencies=[Depends(instructor)])
def del_schedule(sid: int):
    CONN.execute("DELETE FROM schedule WHERE id=?", (sid,))
    CONN.commit()
    return {"ok": True}


async def scheduler_loop():
    """자동 발사. 5초마다 깨어 만기된 일정을 쏜다.

    실패해도 루프가 죽으면 안 된다 — 수업 중에 조용히 멈추는 것이 최악이다.
    """
    import random
    while True:
        try:
            for s in db.rows(CONN, "SELECT * FROM schedule WHERE enabled=1"):
                if (s["next_fire"] or 0) > db.now():
                    continue
                lst = db.unjs(s["scenarios"], [])
                lst = [x for x in lst if x in CATALOG]
                if lst:
                    if s["pick"] == "random":
                        sid = random.choice(lst)
                    else:
                        sid = lst[s["cursor"] % len(lst)]
                    with contextlib.suppress(HTTPException):
                        _fire(sid, s["audience"], "auto")
                CONN.execute("UPDATE schedule SET cursor=cursor+1, next_fire=? WHERE id=?",
                             (db.now() + s["every_sec"], s["id"]))
                CONN.commit()
        except Exception as e:                          # noqa: BLE001
            print(f"[scheduler] {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(5)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(scheduler_loop())


# ── 화면 ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def console(request: Request):
    return tpl.TemplateResponse("console.html", {"request": request})


@app.get("/health")
def health():
    return {"ok": True, "scenarios": len(CATALOG), "errors": len(CAT_ERRORS),
            "students": db.one(CONN, "SELECT COUNT(*) n FROM student")["n"],
            "time": time.time()}
