"""로거 — §7 평가 근거 데이터의 원본이라 봉투가 훼손되면 안 된다."""

from __future__ import annotations

from orchestrator.logger import EpisodeLogger


def test_seq_is_monotonic(tmp_path):
    log = EpisodeLogger("s1", tmp_path)
    for _ in range(5):
        log.write("tick")
    log.close()
    assert [r["seq"] for r in log.read_all()] == [1, 2, 3, 4, 5]


def test_caller_fields_cannot_shadow_the_envelope(tmp_path):
    """`event` 라는 필드명을 넘겨도 레코드의 event 종류가 바뀌면 안 된다.

    조용히 덮이면 로그가 그럴듯한 모습으로 오염되어 나중에 알아채기 어렵다.
    """
    log = EpisodeLogger("s2", tmp_path)
    log.write("forbidden_event", {"role": "vlm", "event": "robot_done", "seq": 999})
    log.close()

    (rec,) = log.read_all()
    assert rec["event"] == "forbidden_event"
    assert rec["seq"] == 1
    assert rec["_shadowed"] == ["event", "seq"]
    assert rec["role"] == "vlm"


def test_ordinary_fields_pass_through(tmp_path):
    log = EpisodeLogger("s3", tmp_path)
    log.write("child_response", {"object": "flower", "correct": True, "latency_ms": 4200})
    log.close()
    (rec,) = log.read_all()
    assert rec["object"] == "flower" and rec["latency_ms"] == 4200
    assert "_shadowed" not in rec


def test_korean_is_not_escaped(tmp_path):
    log = EpisodeLogger("s4", tmp_path)
    log.write("state_change", {"label": "꽃 장난감"})
    log.close()
    assert "꽃 장난감" in log.path.read_text(encoding="utf-8")
