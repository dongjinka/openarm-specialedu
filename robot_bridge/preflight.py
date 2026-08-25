"""호출 직전 관절 상태 검사.

정책 전처리기가 `normalize_min_max: true` + `clip_outliers: true` 로 동작한다.
실측 관절값이 학습 min/max 밖이면 **조용히 클리핑되어 정책이 잘못된 state 를 본다** —
에러도 없이 엉뚱하게 움직인다. 그래서 추론 전에 봉투를 확인하고, 벗어나면
추론하지 않고 `robot_error` 를 낸다.

봉투 값은 데이터셋 `meta/stats.json` 의 `observation.state` min/max 를 그대로 옮긴 것이다
(단위: 도). 데이터셋이 바뀌면 이 표도 다시 생성해야 한다.

  repo: leapshared/nuedive_test_60epi_new_20260824_182026
        (60 에피소드 / 83,351 프레임 / 30fps. 이전 120epi 판에서 **전면 교체**됐다 —
         태스크 문자열의 관사와 관절 봉투 16개가 모두 바뀌었다.)
"""

from __future__ import annotations

from dataclasses import dataclass

#: 학습에 쓰인 유일한 태스크 문자열. **바이트 단위로 동일하게** 보내야 한다.
#: 언어 인코더가 동결(`tune_llm: false`)돼 있고 태스크가 1개뿐이라, 다른 문자열은
#: 조향은 못 하면서 프로젝터에 분포 밖 노이즈만 넣는다.
#: 60epi 판에서 관사가 `a` → `the` 로 바뀌었다. 눈에 안 띄지만 바이트가 다르면
#: 조향은 못 하면서 프로젝터에 분포 밖 노이즈만 들어간다.
GROOT_TASK = "Open the backpack. Put things in the backpack. Close the backpack."

#: (관절명, min, max) — observation.state 16차원의 순서 그대로.
#:
#: 두 관절은 단순한 수치 갱신이 아니라 신호다:
#:   right_gripper : [-57.2, +1.3] → [-67.9, **-17.3**] — 최댓값이 음수다. 새 데이터에는
#:                   그리퍼가 0 근처로 가는 구간이 없다. 홈 포즈 실측이 -17.3 보다 크면
#:                   preflight 가 매 턴 추론을 막는다 → 봉투가 아니라 홈 포즈를 맞춰야 한다.
#:   right_joint_7 : [-30.6, +16.0] → [-6.6, +48.2] — 범위가 통째로 이동했다.
#:                   마운트나 자세가 바뀌었다는 뜻이다.
STATE_ENVELOPE: tuple[tuple[str, float, float], ...] = (
    ("left_joint_1.pos", -28.599803924560547, 60.51093673706055),
    ("left_joint_2.pos", -41.62654113769531, 8.27285099029541),
    ("left_joint_3.pos", -20.075597763061523, 43.593666076660156),
    ("left_joint_4.pos", 35.96559143066406, 110.78189086914062),
    ("left_joint_5.pos", -53.866424560546875, 46.67549133300781),
    ("left_joint_6.pos", -39.59384536743164, 21.408870697021484),
    ("left_joint_7.pos", -29.015087127685547, 32.79633712768555),
    ("left_gripper.pos", -61.97534942626953, 5.147309303283691),
    ("right_joint_1.pos", -68.79471588134766, 40.14026641845703),
    ("right_joint_2.pos", -8.294708251953125, 23.660137176513672),
    ("right_joint_3.pos", -67.68000793457031, 42.78495788574219),
    ("right_joint_4.pos", 38.91627883911133, 134.3655242919922),
    ("right_joint_5.pos", -24.00984764099121, 27.725526809692383),
    ("right_joint_6.pos", -30.89478302001953, 42.47896194458008),
    ("right_joint_7.pos", -6.611723899841309, 48.20547866821289),
    ("right_gripper.pos", -67.96414947509766, -17.256053924560547),
)

STATE_DIM = len(STATE_ENVELOPE)

#: 봉투를 이만큼(도) 넓혀서 검사한다.
#:
#: 0 으로 두면 안 되는 이유가 데이터에 있다. 턴이 시작되는 시점(에피소드 프레임 0)의
#: 그리퍼는 **봉투 최솟값에 거의 붙어 있다**:
#:     right_gripper  시작값 중앙 -67.72  ·  봉투 최솟값 -67.88  → 여유 0.16°
#:     left_gripper   시작값 중앙 -60.95  ·  봉투 최솟값 -61.93  → 여유 0.98°
#: 실기 캘리브레이션이 조금만 달라도 이 아래로 내려간다. 그때 0.2° 때문에 턴 전체를
#: 막는 것은, 전처리기가 그 0.2° 를 클리핑하도록 두는 것보다 나쁘다 — 클리핑 오차는
#: 무시할 만하고, 막히면 시연이 멈춘다.
#:
#: 반대로 홈 포즈가 통째로 틀린 경우는 수십 도씩 벗어나므로 이 여유로는 안 가려진다.
DEFAULT_MARGIN_DEG = 2.0


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    violations: tuple[tuple[str, float, float, float], ...] = ()   # (관절, 값, min, max)

    @property
    def detail(self) -> str:
        if self.ok:
            return ""
        return "; ".join(
            f"{n}={v:.3f} 범위[{lo:.3f}, {hi:.3f}] 밖"
            for n, v, lo, hi in self.violations
        )


def check_state(state: list[float] | tuple[float, ...], *,
                margin: float = DEFAULT_MARGIN_DEG) -> PreflightResult:
    """16차원 관절 상태가 학습 봉투 안인지 본다.

    `margin` 은 봉투를 그만큼 넓힌다(도). 기본값의 근거는 위 DEFAULT_MARGIN_DEG 주석에 있다.
    0 을 주면 학습 범위 그대로 — 데이터 분석용이지 실기 게이트용이 아니다.
    """
    if len(state) != STATE_DIM:
        return PreflightResult(False, (("__dim__", float(len(state)), STATE_DIM, STATE_DIM),))
    bad = []
    for value, (name, lo, hi) in zip(state, STATE_ENVELOPE, strict=True):
        if not (lo - margin) <= value <= (hi + margin):
            bad.append((name, float(value), lo, hi))
    return PreflightResult(not bad, tuple(bad))


def envelope_midpoint() -> list[float]:
    """봉투 중앙값. `--sim` 이 그럴듯한 관절 상태를 흉내낼 때 쓴다."""
    return [(lo + hi) / 2 for _, lo, hi in STATE_ENVELOPE]
