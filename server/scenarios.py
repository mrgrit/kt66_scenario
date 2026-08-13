"""시나리오 카탈로그 — 로드·검증.

── 세 종류를 한 스키마로 ──────────────────────────────────────────
  single    한 번 주입하고 끝. 입문용.
  composite 여러 계통을 동시에 건드린다. 원인이 하나로 안 보인다.
  timeline  시간이 지나며 관련 문제가 **늘어난다**. 초기 판단이 틀리면 나중에 갚는다.

셋의 차이는 `steps` 의 개수와 `at` 값뿐이다. 형식을 나누지 않은 이유는
발사기·에이전트·채점기가 전부 같은 코드를 타게 하기 위해서다. 형식이 갈리면
"timeline 만 채점이 안 된다" 같은 버그가 반드시 생긴다.

── Ground Truth 는 선택이 아니다 ──────────────────────────────────
`ground_truth` 가 없는 시나리오는 로드를 거부한다. 정답을 모르는 문제를 쏘면
학생은 시간을 버리고 강사는 채점을 못 한다. 검증되지 않은 것을 카탈로그에
넣지 않기 위한 최소한의 방벽이다.
"""
from __future__ import annotations

from pathlib import Path

import yaml

SCEN_DIR = Path(__file__).parent.parent / "scenarios"

KINDS = {"single", "composite", "timeline"}
# 주입 경로. 학생 랩의 어느 서비스에 명령을 보낼 것인가.
VIAS = {
    "envsim",    # 시설·환경 (전력·냉각·소방·물리보안) — kt66 envsim /inject
    "injector",  # IT 계통 (시스템·스토리지·네트워크·보안·부하) — kt66 injector
    "attack",    # 공격 시나리오 — attacker 컨테이너에서 실행
    "manual",    # 주입 없음. 상황만 제시하고 학생이 조사한다
}


class ScenarioError(ValueError):
    pass


def _dur(v) -> int:
    """'5m' · '90s' · '1h' · 300 → 초."""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower()
    if s.endswith("ms"):
        return max(0, int(float(s[:-2]) / 1000))
    mult = {"s": 1, "m": 60, "h": 3600}.get(s[-1:])
    if mult:
        return int(float(s[:-1]) * mult)
    return int(float(s))


def _validate(d: dict, path: Path) -> dict:
    def bad(msg):
        raise ScenarioError(f"{path.name}: {msg}")

    for k in ("id", "title", "kind", "steps", "ground_truth"):
        if k not in d:
            bad(f"필수 항목 누락 — {k}")
    if d["kind"] not in KINDS:
        bad(f"kind 는 {'|'.join(sorted(KINDS))} 중 하나여야 한다 (받은 값: {d['kind']})")

    steps = d["steps"]
    if not isinstance(steps, list) or not steps:
        bad("steps 는 1개 이상의 배열이어야 한다")
    for i, s in enumerate(steps):
        if "via" not in s:
            bad(f"steps[{i}] — via 누락")
        if s["via"] not in VIAS:
            bad(f"steps[{i}] — 알 수 없는 via: {s['via']} (가능: {', '.join(sorted(VIAS))})")
        s["at_sec"] = _dur(s.get("at", 0))
        if s["via"] != "manual" and not s.get("fault") and not s.get("action"):
            bad(f"steps[{i}] — fault 또는 action 이 필요하다")
    steps.sort(key=lambda s: s["at_sec"])

    # kind 는 **단계 수가 아니라 성격**으로 가른다. 냉각탑 2대 동시 정지는 주입이
    # 2건이지만 사건은 하나다 — 그것을 composite 로 부르면 학생에게 거짓말이 된다.
    #   single    한 계통에서 벌어진 한 사건 (t+0 에 모여 있다)
    #   composite 서로 다른 계통이 동시에 (원인이 하나로 안 보인다)
    #   timeline  시간을 두고 벌어진다
    faults = {s.get("fault") or s.get("action") for s in steps}
    spans = steps[-1]["at_sec"] > 0
    if d["kind"] == "single":
        if spans:
            bad("kind=single 인데 단계가 시간에 걸쳐 있다 — timeline 이 맞다")
        if len(faults) > 1:
            bad(f"kind=single 인데 서로 다른 고장이 {len(faults)}종이다 — composite 가 맞다")
    if d["kind"] == "composite":
        if spans:
            bad("kind=composite 인데 단계가 시간에 걸쳐 있다 — timeline 이 맞다")
        if len(faults) < 2:
            bad("kind=composite 인데 고장이 한 종류다 — single 이 맞다")
    if d["kind"] == "timeline":
        if len(steps) < 2:
            bad("kind=timeline 은 단계가 2개 이상이어야 한다")
        if not spans:
            bad("kind=timeline 인데 모든 단계가 t+0 이다 — single/composite 가 맞다")

    gt = d["ground_truth"]
    if not gt.get("root_cause"):
        bad("ground_truth.root_cause 가 비어 있다 — 정답 없는 문제는 싣지 않는다")
    checks = gt.get("checks") or []
    if not checks:
        bad("ground_truth.checks 가 비어 있다 — 채점할 수 없는 문제는 싣지 않는다")
    for i, ck in enumerate(checks):
        for k in ("id", "type", "points"):
            if k not in ck:
                bad(f"ground_truth.checks[{i}] — {k} 누락")

    sla = d.get("sla") or {}
    d["sla_detect_sec"] = _dur(sla["detect"]) if sla.get("detect") else None
    d["sla_mitigate_sec"] = _dur(sla["mitigate"]) if sla.get("mitigate") else None
    d["duration_sec"] = _dur(d.get("duration", steps[-1]["at_sec"] + 900))
    d["max_points"] = sum(float(c["points"]) for c in checks)
    d.setdefault("category", "misc")
    d.setdefault("difficulty", 2)
    d.setdefault("tags", [])
    d.setdefault("verified", False)
    return d


def load_all() -> tuple[dict[str, dict], list[str]]:
    """카탈로그 전체를 읽는다. 깨진 파일은 실어 두지 않고 오류 목록으로 돌려준다.

    한 파일이 깨졌다고 전체가 안 뜨면 수업 중에 손을 못 쓴다.
    """
    cat: dict[str, dict] = {}
    errs: list[str] = []
    for p in sorted(SCEN_DIR.glob("*.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ScenarioError(f"{p.name}: 최상위가 매핑이 아니다")
            d = _validate(d, p)
            if d["id"] in cat:
                raise ScenarioError(f"{p.name}: id 중복 — {d['id']}")
            d["_file"] = p.name
            cat[d["id"]] = d
        except (ScenarioError, yaml.YAMLError) as e:
            errs.append(str(e))
    return cat, errs


def summary(d: dict) -> dict:
    """목록 화면용 요약. 본문 전체를 목록에 실으면 화면이 느려진다."""
    return {
        "id": d["id"], "title": d["title"], "kind": d["kind"],
        "category": d.get("category"), "difficulty": d.get("difficulty"),
        "steps": len(d["steps"]),
        "span_sec": d["steps"][-1]["at_sec"],
        "duration_sec": d["duration_sec"],
        "max_points": d["max_points"],
        "verified": bool(d.get("verified")),
        "tags": d.get("tags", []),
        "vias": sorted({s["via"] for s in d["steps"]}),
    }
