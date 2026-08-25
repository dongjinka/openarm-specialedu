"""시나리오 로더. 시나리오는 코드에서 분리되어야 한다 (§4.6).

목록이 바뀌어도 재학습은 물론 코드 변경도 없어야 한다 — Orchestrator 의
체크리스트만 교체된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ObjectSpec:
    key: str
    ko: str
    image: str = ""
    utterances: dict[str, str] = field(default_factory=dict)
    #: 태블릿(openarm-special-web) 쪽 항목 id. 없으면 key 를 그대로 쓴다.
    #: Python 쪽 이름(tree/flower/whale)은 VLM 프롬프트와 eval/labels_45.csv 에
    #: 묶여 있으므로 바꾸지 않고, 경계에서만 매핑한다.
    tablet_id: str = ""


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    friend_name: str
    checklist: tuple[str, ...]
    objects: dict[str, ObjectSpec]
    distractors: tuple[str, ...] = ()
    prompt_hierarchy: tuple[str, ...] = ("verbal", "hint", "model")
    max_retries_per_item: int = 3

    #: True 면 호명한 항목이 아닌 체크리스트 물건도 오답 처리한다.
    #: 기본 False — §5 의 `should_pack` 의미(체크리스트에 있고 아직 안 담김)를 따른다.
    #: 어느 쪽이든 로그의 `on_target` 으로 연구자가 사후 분해할 수 있다.
    strict_order: bool = False


    #: 60epi 평균 46.3초 × 1.5. **데이터셋 시간 기준의 임시 하한**이다 —
    #: 실행(벽시계) 시간은 달성 루프 레이트에 따라 최대 2배까지 길 수 있으므로
    #: 드라이런 1회로 재측정해 그 값 × 1.3 으로 다시 잡는다.
    robot_deadline_ms: int = 69_500
    #: 판정 지연 실측 중앙 3.6초 · 최대 14.1초 (gemini-3.7-flash, 320px).
    judge_timeout_ms: int = 15_000
    verify_timeout_ms: int = 10_000
    #: WAIT_CHILD 에서 이만큼 아무 변화가 없으면 촉진 발화를 다시 낸다 (§C-3).
    stall_timeout_ms: int = 20_000
    #: 로봇 턴의 하위 단계. (진행비율 상한, 단계 id) 오름차순.
    #:
    #: 60에피소드의 그리퍼 채널에서 뽑았다 — 왼쪽은 가방을, 오른쪽은 물건을 잡는다.
    #: 경계는 매우 일관적이다(표준편차 0.019~0.029):
    #:   가방 열림 0.266 · 물체 집기 0.517 · 닫기 시작 0.917
    #:
    #: 이걸 두는 이유는 로봇 턴이 46초(실행은 더 길 수 있음)로 무발화이기 때문이다.
    #: `progress_tick` 은 스톱워치일 뿐이라 아동에게 "얼마나 남았나"를 못 알려준다.
    #: **주의: 이건 시간 기준 근사다.** 정책이 멈추거나 느려지면 실제 동작과 어긋난다 —
    #: 표시용이지 성공 판정용이 아니다 (성공은 ROBOT_VERIFY 의 VLM 이 정한다).
    turn_phases: tuple[tuple[float, str], ...] = ()

    def turn_phase(self, ratio: float) -> str | None:
        """진행 비율 → 단계 id. 설정이 없으면 None (표시하지 않는다)."""
        for until, name in self.turn_phases:
            if ratio < until:
                return name
        return self.turn_phases[-1][1] if self.turn_phases else None

    def utterance(self, obj: str, kind: str) -> str:
        spec = self.objects.get(obj)
        if spec and kind in spec.utterances:
            return spec.utterances[kind]
        return f"{kind}_{obj}"

    def label(self, obj: str) -> str:
        spec = self.objects.get(obj)
        return spec.ko if spec else obj

    def tablet_id(self, obj: str) -> str:
        """태블릿 쪽 항목 id. 이 매핑이 두 코드베이스의 유일한 이름 경계다."""
        spec = self.objects.get(obj)
        return spec.tablet_id if spec and spec.tablet_id else obj


def _object_specs(raw: dict[str, Any]) -> dict[str, ObjectSpec]:
    out: dict[str, ObjectSpec] = {}
    for key, body in (raw or {}).items():
        out[key] = ObjectSpec(
            key=key,
            ko=body.get("ko", key),
            image=body.get("image", ""),
            utterances=dict(body.get("utterances", {})),
            tablet_id=body.get("tablet_id", "") or key,
        )
    return out


def load_scenario(path: str | Path) -> Scenario:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Scenario(
        scenario_id=data["scenario_id"],
        friend_name=data.get("friend_name", ""),
        checklist=tuple(data["checklist"]),
        objects=_object_specs(data.get("objects", {})),
        distractors=tuple(data.get("distractors", ())),
        prompt_hierarchy=tuple(data.get("prompt_hierarchy", ("verbal", "hint", "model"))),
        max_retries_per_item=int(data.get("max_retries_per_item", 3)),
        strict_order=bool(data.get("strict_order", False)),
        robot_deadline_ms=int(data.get("robot_deadline_ms", 69_500)),
        judge_timeout_ms=int(data.get("judge_timeout_ms", 15_000)),
        verify_timeout_ms=int(data.get("verify_timeout_ms", 10_000)),
        stall_timeout_ms=int(data.get("stall_timeout_ms", 20_000)),
        turn_phases=tuple(
            (float(x["until"]), str(x["id"])) for x in data.get("turn_phases", ())
        ),
    )


def find_scenario(scenario_id: str, root: str | Path = "scenarios") -> Scenario:
    path = Path(root) / f"{scenario_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"시나리오를 찾을 수 없다: {path}")
    return load_scenario(path)
