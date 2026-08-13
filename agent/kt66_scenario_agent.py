#!/usr/bin/env python3
"""kt66_scenario 학생 에이전트 — 학생 랩 1대에 1개.

    중앙서버 ◀── 폴링 ── 이 에이전트 ──▶ 로컬 envsim(8010) · injector(8030)
                └── 증거 요약 ──┘

중앙이 학생 랩에 접속하지 않으므로, 학생 서버가 NAT 뒤에 있어도 방화벽을 열
필요가 없다. 나가는 HTTP 하나만 되면 된다.

── 왜 증거를 에이전트가 올리는가 ───────────────────────────────────
학생이 조치하면 그 결과는 **학생 랩의 상태**로 나타난다(경보 해제, 고장 복구,
컨테이너 재기동). 그것을 볼 수 있는 것은 랩 안에 있는 이 에이전트뿐이다.
중앙은 판정만 하고 관측은 현장이 한다.

사용:
    python3 kt66_scenario_agent.py --server http://강사서버:8040 --name "홍길동" --cohort A
    (최초 실행 시 토큰을 받아 ~/.kt66s_agent.json 에 저장한다. 이후 인자 없이 실행 가능)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("KT66S_STATE", Path.home() / ".kt66s_agent.json"))
DEFAULT_ENVSIM = os.environ.get("KT66S_ENVSIM", "http://127.0.0.1:8010")
DEFAULT_INJECTOR = os.environ.get("KT66S_INJECTOR", "http://127.0.0.1:8030")
LAB_KEY = os.environ.get("KT66S_LAB_KEY", "ccc-api-key-2026")


def http(method: str, url: str, body=None, timeout=15) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return 0, str(e)


class Agent:
    def __init__(self, server: str, envsim: str, injector: str):
        self.server = server.rstrip("/")
        self.envsim = envsim.rstrip("/")
        self.injector = injector.rstrip("/")
        self.token = ""
        self.student_id = 0
        self.poll_sec = 5
        # 이미 보고한 경보는 다시 올리지 않는다. 5초마다 같은 경보를 올리면
        # 증거가 수천 건이 되고 채점 화면이 쓸모없어진다.
        self.seen_alarms: set[str] = set()
        self.active: dict[int, str] = {}      # launch_id -> scenario_id

    # ── 상태 ────────────────────────────────────────────────────────
    def load(self) -> bool:
        if STATE.exists():
            d = json.loads(STATE.read_text())
            self.token, self.student_id = d.get("token", ""), d.get("student_id", 0)
        return bool(self.token)

    def save(self):
        STATE.write_text(json.dumps(
            {"token": self.token, "student_id": self.student_id, "server": self.server}, indent=2))
        STATE.chmod(0o600)         # 토큰이다. 남이 읽으면 남의 발사를 가로챈다

    def register(self, name: str, cohort: str):
        st, r = http("POST", f"{self.server}/api/register", {
            "name": name, "cohort": cohort, "token": self.token,
            "lab_url": self.envsim, "host": socket.gethostname()})
        if st != 200 or not isinstance(r, dict):
            sys.exit(f"등록 실패 ({st}): {r}")
        self.token, self.student_id = r["token"], r["student_id"]
        self.poll_sec = r.get("poll_sec", 5)
        self.save()
        print(f"[등록] {name} (id={self.student_id}) "
              f"{'재연결' if r.get('rejoined') else '신규'}", flush=True)

    # ── 주입 실행 ───────────────────────────────────────────────────
    def apply(self, spec: dict) -> tuple[bool, str]:
        via = spec.get("via")
        if via == "manual":
            return True, "주입 없음 — 상황 제시형"
        if via == "envsim":
            q = urllib.parse.urlencode({
                "fault": spec["fault"], "target": spec.get("target", "*"),
                "key": LAB_KEY})
            st, r = http("POST", f"{self.envsim}/inject?{q}")
            return (200 <= st < 300), f"envsim {st}: {json.dumps(r, ensure_ascii=False)[:300]}"
        if via in ("injector", "attack"):
            # 공격도 injector 의 화이트리스트를 통해서만 돈다. 임의 명령 실행은 열지 않는다.
            #
            # injector /inject 는 **전부 쿼리 파라미터**로 받는다(FastAPI 의 스칼라 인자).
            # params 는 dict 가 아니라 **JSON 문자열**이다. 예전엔 여기서 본문(JSON body)에
            # id/target 을 실어 보냈는데, 그러면 injector 가 필수 쿼리 인자 누락으로 422 를
            # 낸다 — 즉 **injector 기반 시나리오는 한 번도 동작한 적이 없었다.**
            # 지금까지의 시나리오가 전부 via: envsim 이라 드러나지 않았을 뿐이다.
            q = {"id": spec.get("fault") or spec.get("action"),
                 "target": spec.get("target", ""), "key": LAB_KEY}
            if spec.get("ttl") is not None:
                q["ttl"] = spec["ttl"]
            if spec.get("params"):
                q["params"] = json.dumps(spec["params"], ensure_ascii=False)
            st, r = http("POST", f"{self.injector}/inject?{urllib.parse.urlencode(q)}")
            # 409 = 같은 state 주입이 이미 걸려 있다. 실패가 아니다 — 이 단계가 원하는
            # 랩 상태는 **이미 성립해 있다.** 앞 시나리오와 겹치는 경우가 대부분이라
            # 실패로 처리하면 시나리오가 반쪽만 나간 것처럼 보인다. 겹쳤다는 사실은
            # 남겨서 강사가 알 수 있게 한다.
            if st == 409:
                return True, f"{via} 409: 이미 같은 상태다(앞 시나리오와 겹침) — 그대로 진행"
            return (200 <= st < 300), f"{via} {st}: {json.dumps(r, ensure_ascii=False)[:300]}"
        return False, f"알 수 없는 via: {via}"

    # ── 관측 ────────────────────────────────────────────────────────
    def observe(self):
        """랩 상태를 읽어 새 경보만 증거로 올린다."""
        if not self.active:
            return
        st, r = http("GET", f"{self.envsim}/state", timeout=8)
        if st != 200 or not isinstance(r, dict):
            return
        for a in r.get("alarms", []):
            key = f"{a.get('id')}@{a.get('scope')}"
            if key in self.seen_alarms:
                continue
            self.seen_alarms.add(key)
            snippet = (f"level={a.get('level')} metric={a.get('metric')} "
                       f"value={a.get('value')} scope={a.get('scope')}")
            for lid in self.active:
                self.send_evidence(lid, "alarm", f"{a.get('id')} — {a.get('msg')}",
                                   snippet, "envsim")

    def send_evidence(self, launch_id: int, kind: str, summary: str,
                      snippet: str = "", source: str = "agent",
                      check_id: str = "", passed=None):
        http("POST", f"{self.server}/api/evidence?token={urllib.parse.quote(self.token)}",
             {"launch_id": launch_id, "kind": kind, "summary": summary,
              "snippet": snippet, "source": source, "check_id": check_id,
              "passed": passed})

    # ── 루프 ────────────────────────────────────────────────────────
    def run(self):
        print(f"[시작] server={self.server} envsim={self.envsim} injector={self.injector}",
              flush=True)
        fails = 0
        while True:
            st, r = http("GET",
                         f"{self.server}/api/poll?token={urllib.parse.quote(self.token)}")
            if st == 401:
                sys.exit("토큰이 거부됐다. --name 을 주고 다시 등록하라")
            if st != 200 or not isinstance(r, dict):
                fails += 1
                if fails % 12 == 1:
                    print(f"[경고] 중앙서버 응답 없음 ({st}) — 재시도 중", flush=True)
                time.sleep(min(30, self.poll_sec * (1 + fails // 6)))    # 점진 후퇴
                continue
            fails = 0
            self.poll_sec = r.get("poll_sec", self.poll_sec)

            for task in r.get("tasks", []):
                lid, spec = task["launch_id"], task["spec"]
                self.active[lid] = task["scenario_id"]
                ok, msg = self.apply(spec)
                note = spec.get("note", "")
                print(f"[주입] launch={lid} {spec.get('via')}/{spec.get('fault') or spec.get('action')}"
                      f" -> {'OK' if ok else 'FAIL'} {note}", flush=True)
                http("POST",
                     f"{self.server}/api/step_result?token={urllib.parse.quote(self.token)}",
                     {"step_id": task["step_id"], "ok": ok, "result": msg})

            self.observe()
            time.sleep(self.poll_sec)


    # ── 학생 보고 ───────────────────────────────────────────────────
    def my_launches(self) -> list[dict]:
        st, r = http("GET", f"{self.server}/api/launches?student_id={self.student_id}&limit=20")
        if st != 200 or not isinstance(r, dict):
            return []
        return [L for L in r.get("launches", []) if L["state"] in ("running", "delivered", "queued")]

    def report(self, kind: str, text: str, snippet: str, launch_id: int) -> None:
        """학생이 자기 판단·조치를 기록한다.

        채점의 모집단은 **이것**이다. 에이전트가 자동 수집한 경보는 맥락일 뿐
        답안이 아니다 — 경보가 떴다는 사실은 학생의 성취가 아니기 때문이다.
        """
        if not launch_id:
            act = self.my_launches()
            if not act:
                sys.exit("진행 중인 시나리오가 없다. --launch 로 지정하거나 발사를 기다려라")
            if len(act) > 1:
                print("진행 중인 시나리오가 여럿이다. --launch 로 지정하라:", flush=True)
                for L in act:
                    print(f"  #{L['id']}  {L['scenario_id']}  {L['detail'].get('title','')}")
                sys.exit(1)
            launch_id = act[0]["id"]
        st, r = http("POST",
                     f"{self.server}/api/evidence?token={urllib.parse.quote(self.token)}",
                     {"launch_id": launch_id, "kind": kind, "summary": text,
                      "snippet": snippet, "source": "student"})
        if st == 200:
            print(f"[보고] #{launch_id} [{kind}] {text}", flush=True)
        else:
            sys.exit(f"보고 실패 ({st}): {r}")

    def finish(self, launch_id: int) -> None:
        if not launch_id:
            act = self.my_launches()
            if len(act) != 1:
                sys.exit("--launch 로 대상을 지정하라")
            launch_id = act[0]["id"]
        st, r = http("POST",
                     f"{self.server}/api/finish?launch_id={launch_id}"
                     f"&token={urllib.parse.quote(self.token)}")
        if st != 200 or not isinstance(r, dict):
            sys.exit(f"제출 실패 ({st}): {r}")
        print(f"제출 완료 — {r['points']}/{r['max_points']}점", flush=True)
        for c in r["checks"]:
            print(f"  {'✔' if c['passed'] else '✘'} {c['id']:16} "
                  f"{c['points']:5.1f}/{c['of']:<5.1f} {c['why']}")


def main():
    p = argparse.ArgumentParser(
        description="kt66_scenario 학생 에이전트",
        epilog="예) 상주 실행: --name 홍길동 --server http://강사:8040\n"
               "    조사 보고: --report '냉동기 정상, A아일 단독' --kind observation\n"
               "    조치 기록: --report '학습 job 차단' --kind action\n"
               "    제출:      --submit",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default="", help="중앙 시나리오 서버 URL")
    p.add_argument("--name", default="", help="학생 표시 이름 (최초 등록 시 필요)")
    p.add_argument("--cohort", default="", help="분반")
    p.add_argument("--envsim", default=DEFAULT_ENVSIM)
    p.add_argument("--injector", default=DEFAULT_INJECTOR)
    p.add_argument("--report", default="", metavar="TEXT",
                   help="판단·조치를 한 줄로 보고한다 (채점 대상)")
    p.add_argument("--kind", default="observation",
                   choices=["observation", "action", "alarm", "note"],
                   help="보고 종류. observation=조사/판단, action=조치")
    p.add_argument("--snippet", default="", help="근거 발췌 (명령 출력 등)")
    p.add_argument("--launch", type=int, default=0, help="대상 발사 번호")
    p.add_argument("--submit", action="store_true", help="대응 완료 제출 후 채점 결과 확인")
    a = p.parse_args()

    server = a.server
    if not server and STATE.exists():
        server = json.loads(STATE.read_text()).get("server", "")
    if not server:
        sys.exit("--server 가 필요하다 (예: --server http://192.168.1.10:8040)")

    ag = Agent(server, a.envsim, a.injector)
    had = ag.load()
    if a.report or a.submit:
        if not had:
            sys.exit("등록되지 않았다. 먼저 --name 으로 에이전트를 실행하라")
        if a.report:
            ag.report(a.kind, a.report, a.snippet, a.launch)
        if a.submit:
            ag.finish(a.launch)
        return
    if a.name or not had:
        if not a.name:
            sys.exit("최초 실행이다. --name 으로 표시 이름을 지정하라")
        ag.register(a.name, a.cohort)
    ag.run()


if __name__ == "__main__":
    main()
