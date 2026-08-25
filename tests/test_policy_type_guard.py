"""정책 타입 사전 검사 (§A Day-0 블로커).

체크포인트 `leapshared/nuedive_test_60epi_new_20260824_182026_GR00T17` 의 config.json 은
`type: groot_frozen_bf16` 인데, 공개 `openarmsciedu-vla` 포크는 `"groot"` 하나만 등록한다.
게다가 config 에는 포크의 `GrootConfig` 에 **없는** 필드가 셋 있다
(`frozen_params_dtype` · `use_peft` · `pretrained_revision`) — 학습에 쓴 설정 클래스가
포크에 없는 변형이라는 뜻이다.

미리 잡지 않으면 draccus 가 알아보기 힘든 KeyError 를 낸다.
"""

from __future__ import annotations

import json

import pytest

from robot_bridge import groot_runner as gr

#: 실제 체크포인트에서 확인한 값. 포크에 없는 필드 셋이 함께 들어 있다.
REAL_CONFIG = {
    "type": "groot_frozen_bf16",
    "chunk_size": 16,
    "n_action_steps": 16,
    "frozen_params_dtype": "bfloat16",
    "use_peft": False,
    "pretrained_revision": None,
    "base_model_path": "nvidia/GR00T-N1.7-3B",
}


def _checkpoint(tmp_path, config: dict):
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return str(tmp_path)


def test_reads_the_declared_type_from_a_local_checkpoint(tmp_path):
    assert gr._declared_policy_type(_checkpoint(tmp_path, REAL_CONFIG)) == "groot_frozen_bf16"


def test_missing_checkpoint_yields_none(tmp_path):
    assert gr._declared_policy_type(str(tmp_path / "nope")) is None


def test_unregistered_type_fails_with_an_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "_known_policy_types", lambda: {"groot", "act", "pi0"})
    with pytest.raises(RuntimeError) as excinfo:
        gr.check_policy_type(_checkpoint(tmp_path, REAL_CONFIG))
    message = str(excinfo.value)
    assert "groot_frozen_bf16" in message
    # 무엇이 등록돼 있는지, 왜 안 되는지, 어떻게 해야 하는지가 다 들어 있어야 한다.
    assert "groot" in message and "frozen_params_dtype" in message
    # 어디서 등록되는지를 알려줘야 한다 — 이게 없으면 원인을 찾는 데 반나절이 든다.
    assert "openarm_sciedu" in message and "register_third_party_plugins" in message


def test_registered_type_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(gr, "_known_policy_types", lambda: {"groot_frozen_bf16"})
    gr.check_policy_type(_checkpoint(tmp_path, REAL_CONFIG))     # 예외 없음


def test_no_registry_means_no_check(tmp_path, monkeypatch):
    """lerobot 이 없는 환경(개발 머신)에서는 검사를 건너뛴다 — 막지 않는다."""
    monkeypatch.setattr(gr, "_known_policy_types", set)
    gr.check_policy_type(_checkpoint(tmp_path, REAL_CONFIG))


def test_envelope_matches_the_checkpoint_stats():
    """봉투가 체크포인트에 구워진 정규화 통계와 같은지.

    `policy_preprocessor_step_2_*.safetensors` 의 `observation.state.min/max` 와
    `preflight.STATE_ENVELOPE` 를 대조해 소수점까지 일치함을 확인했다(Δ=0).
    `processor_groot.py:_min_max_norm` 이 q01/q99 가 아니라 **min/max** 를 쓰고,
    `clip_outliers` 는 정규화값을 [-1,1] 로 clamp 한다 — 원값을 [min,max] 로 자르는 것과 같다.
    여기서는 그 결론이 뒤집히지 않았는지(차원·부호) 만 지킨다.
    """
    from robot_bridge.preflight import STATE_ENVELOPE

    assert len(STATE_ENVELOPE) == 16
    assert dict(zip([n for n, _, _ in STATE_ENVELOPE], range(16)))["right_gripper.pos"] == 15
    _, lo, hi = STATE_ENVELOPE[15]
    # 오른쪽 그리퍼는 최댓값이 음수다. 이 부호가 뒤집히면 홈 포즈 검사가 무의미해진다.
    assert hi < 0, "right_gripper 최댓값이 음수가 아니다 — 봉투가 옛 120epi 판으로 되돌아갔다"
    assert lo < hi


#: 60에피소드 프레임 0(= 턴이 시작되는 시점)의 관절 상태 중앙값.
TURN_START = [16.34, -10.61, 8.49, 87.48, 6.63, -15.14, -9.38, -60.95,
              -28.47, 8.43, -9.84, 88.94, -6.91, 15.73, 12.86, -67.72]


def test_turn_start_pose_passes_preflight():
    from robot_bridge.preflight import check_state

    assert check_state(TURN_START).ok, "학습 데이터의 턴 시작 자세가 봉투를 통과하지 못한다"


def test_small_gripper_drift_does_not_block_the_turn():
    """턴 시작 시 그리퍼는 봉투 최솟값에 거의 붙어 있다 (right_gripper 여유 0.16°).

    실기 캘리브레이션이 조금만 달라도 그 아래로 내려간다. 0.2° 때문에 턴 전체를
    막는 것은, 전처리기가 그 0.2° 를 클리핑하도록 두는 것보다 나쁘다.
    """
    from robot_bridge.preflight import check_state

    drifted = list(TURN_START)
    drifted[7] -= 1.5           # left_gripper
    drifted[15] -= 1.0          # right_gripper
    assert not check_state(drifted, margin=0.0).ok, "여유 0 이면 막혀야 한다 (전제 확인)"
    assert check_state(drifted).ok, "기본 여유가 사소한 드리프트를 막고 있다"


def test_a_wrong_home_pose_is_still_blocked():
    """여유가 진짜 문제까지 가려서는 안 된다. 홈 포즈 오류는 수십 도씩 벗어난다."""
    from robot_bridge.preflight import check_state

    result = check_state([0.0] * 16)
    assert not result.ok
    assert "right_gripper.pos" in result.detail
