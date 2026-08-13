"""채점 — 증거 요약을 Ground Truth 에 대조한다.

── 원칙 ───────────────────────────────────────────────────────────
① **정답 하나를 강요하지 않는다.** 시설 장애 대응에 유일해는 없다. 채점은
   "무엇을 했는가"보다 "근거를 남겼는가"를 본다. 그래서 check 는 대부분
   evidence 의 존재와 내용으로 판정한다.
② **부분 점수를 준다.** 5단계 중 3단계를 맞게 짚었으면 3단계만큼 받는다.
   전부 아니면 0 인 채점은 학생이 어디서 갈렸는지 알려 주지 못한다.
③ **금지 행위는 감점이 아니라 표시다.** 점수를 깎는 대신 forbidden_hit 로
   남긴다. 강사가 그 항목만 따로 볼 수 있어야 지도가 된다.

── check 유형 ─────────────────────────────────────────────────────
  evidence_kind   해당 종류의 증거가 있는가 (예: 조치를 하긴 했는가)
  keyword         증거 본문에 이 표현이 있는가 (근거를 댔는가)
  check_id        에이전트가 그 check 를 직접 통과로 보고했는가
  alarm_seen      해당 경보를 인지했다고 보고했는가
  order           두 행위의 선후가 맞는가 (예: 통보가 조치보다 먼저)
  no_evidence     이런 증거가 **없어야** 통과 (금지 행위 확인)
"""
from __future__ import annotations

import re

import db


# ── 기계가 본 것과 학생이 쓴 것을 섞지 않는다 ──────────────────────
# 에이전트는 경보를 자동 수집한다. 그 텍스트에는 scope=2F/aisle-A 같은 문자열이
# 들어 있어서, "아일" 을 찾는 keyword 체크가 **학생이 아무것도 안 해도** 통과해
# 버린다(실제로 첫 검증에서 50/100 이 공짜로 나갔다).
# 그래서 채점의 기본 모집단은 **학생이 쓴 증거**뿐이다. 기계 관측은 맥락일 뿐
# 답안이 아니다. 기계 관측까지 보려면 체크가 include_machine 을 명시해야 한다.
MACHINE_SOURCES = {"envsim", "injector", "agent", "siem"}


def _pool(evs: list[dict], ck: dict) -> list[dict]:
    if ck.get("include_machine"):
        return evs
    return [e for e in evs if e["source"] not in MACHINE_SOURCES]


def _match(text: str, pat: str) -> bool:
    """대소문자·공백을 너그럽게 본다. 학생이 쓴 한국어 표현이 정확히 같을 리 없다."""
    try:
        return re.search(pat, text, re.I | re.S) is not None
    except re.error:
        return pat.lower() in text.lower()


def _eval_check(ck: dict, all_evs: list[dict]) -> tuple[bool, str]:
    t = ck["type"]
    evs = _pool(all_evs, ck)
    blob = lambda e: f"{e['summary']}\n{e['snippet']}"      # noqa: E731

    if t == "evidence_kind":
        hit = [e for e in evs if e["kind"] == ck.get("kind")]
        return bool(hit), (f"{ck.get('kind')} 증거 {len(hit)}건" if hit
                           else "해당 종류의 학생 증거 없음")

    if t == "keyword":
        pats = ck.get("any") or [ck.get("pattern", "")]
        for e in evs:
            for p in pats:
                if p and _match(blob(e), p):
                    return True, f"'{p}' — {e['summary'][:60]}"
        if not evs:
            return False, "학생이 제출한 증거가 없다"
        return False, f"근거 표현 미발견 ({', '.join(p for p in pats if p)[:80]})"

    if t == "check_id":
        hit = [e for e in evs if e["check_id"] == ck["id"] and e["passed"] == 1]
        return bool(hit), ("통과 보고 있음" if hit else "통과 보고 없음")

    if t == "alarm_seen":
        # 경보가 **떴는가**가 아니라 학생이 그것을 **인지해 보고했는가**를 본다.
        # 전자는 시뮬레이터가 하는 일이지 학생의 성취가 아니다.
        want = ck.get("alarm", "")
        for e in evs:
            if _match(blob(e), want):
                return True, f"학생이 인지 보고 — {want}"
        fired = any(e["kind"] == "alarm" and _match(blob(e), want) for e in all_evs)
        return False, (f"경보({want})는 발생했으나 학생 인지 보고가 없다" if fired
                       else f"경보 인지 보고 없음 — {want}")

    if t == "order":
        first, second = ck.get("first", ""), ck.get("second", "")
        tf = next((e["ts"] for e in evs if _match(blob(e), first)), None)
        ts = next((e["ts"] for e in evs if _match(blob(e), second)), None)
        if tf is None or ts is None:
            return False, "선후를 판정할 증거가 부족하다"
        return tf <= ts, (f"{first} → {second} 순서 준수" if tf <= ts
                          else f"순서 역전 — {second} 가 {first} 보다 앞섰다")

    if t == "no_evidence":
        pats = ck.get("any") or [ck.get("pattern", "")]
        for e in evs:
            for p in pats:
                if p and _match(blob(e), p):
                    return False, f"금지 행위 발견 — '{p}'"
        return True, "금지 행위 없음"

    return False, f"알 수 없는 check 유형: {t}"


def grade(conn, launch: dict, sc: dict) -> dict:
    """launch 1건을 채점하고 score 에 기록한다. 여러 번 불러도 결과가 같다(멱등)."""
    lid = launch["id"]
    evs = db.rows(conn, "SELECT * FROM evidence WHERE launch_id=? ORDER BY ts", lid)
    gt = sc["ground_truth"]
    base = launch["started"] or launch["created"]

    results, points = [], 0.0
    for ck in gt.get("checks", []):
        ok, why = _eval_check(ck, evs)
        pts = float(ck["points"]) if ok else 0.0
        points += pts
        results.append({"id": ck["id"], "type": ck["type"], "passed": ok,
                        "points": pts, "of": float(ck["points"]),
                        "why": why, "note": ck.get("note", "")})

    # 금지 행위 — 점수를 깎지 않고 표시만 한다. 학생이 쓴 것만 본다.
    student_evs = [e for e in evs if e["source"] not in MACHINE_SOURCES]
    forbidden = []
    for f in gt.get("forbidden", []):
        pat = f if isinstance(f, str) else f.get("pattern", "")
        for e in student_evs:
            if pat and _match(f"{e['summary']}\n{e['snippet']}", pat):
                forbidden.append({"pattern": pat, "at": e["ts"], "evidence": e["summary"][:80]})
                break

    # SLA — 학생의 최초 보고와 마지막 조치까지. 에이전트가 경보를 자동 수집한
    # 시각을 쓰면 모든 학생의 탐지 시간이 0 이 되어 SLA 가 무의미해진다.
    detect = next((e["ts"] - base for e in student_evs
                   if e["kind"] in ("alarm", "observation")), None)
    mitigate = next((e["ts"] - base for e in reversed(student_evs)
                     if e["kind"] == "action"), None)
    d_ok = bool(sc["sla_detect_sec"] and detect is not None and detect <= sc["sla_detect_sec"])
    m_ok = bool(sc["sla_mitigate_sec"] and mitigate is not None
                and mitigate <= sc["sla_mitigate_sec"])

    detail = {"checks": results, "forbidden": forbidden,
              "root_cause": gt.get("root_cause", ""),
              "chain": gt.get("chain", []),
              "evidence_count": len(evs)}
    conn.execute("""INSERT INTO score(launch_id,student_id,scenario_id,points,max_points,
                    detect_sec,mitigate_sec,sla_detect_ok,sla_mitigate_ok,forbidden_hit,
                    graded,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(launch_id) DO UPDATE SET
                    points=excluded.points, max_points=excluded.max_points,
                    detect_sec=excluded.detect_sec, mitigate_sec=excluded.mitigate_sec,
                    sla_detect_ok=excluded.sla_detect_ok,
                    sla_mitigate_ok=excluded.sla_mitigate_ok,
                    forbidden_hit=excluded.forbidden_hit, graded=excluded.graded,
                    detail=excluded.detail""",
                 (lid, launch["student_id"], launch["scenario_id"], points, sc["max_points"],
                  detect, mitigate, int(d_ok), int(m_ok), len(forbidden),
                  db.now(), db.js(detail)))
    conn.commit()
    return {"launch_id": lid, "points": points, "max_points": sc["max_points"],
            "detect_sec": detect, "mitigate_sec": mitigate,
            "sla_detect_ok": d_ok, "sla_mitigate_ok": m_ok,
            "forbidden": forbidden, "checks": results}
