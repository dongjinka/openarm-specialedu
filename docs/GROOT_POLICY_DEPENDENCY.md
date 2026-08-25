# `groot_frozen_bf16` — 이 정책이 어디서 오는가

시연 정책 [`leapshared/nuedive_test_60epi_new_20260824_182026_GR00T17`](https://huggingface.co/leapshared/nuedive_test_60epi_new_20260824_182026_GR00T17)
의 `config.json` 은 `type: groot_frozen_bf16` 이다. 이 이름은 **lerobot 이 등록하지 않는다.**

## 등록 경로 (leap 머신에서 확인)

```
openarm_sciedu/policies/groot_frozen_bf16/configuration_groot_frozen_bf16.py:58
    @PreTrainedConfig.register_subclass("groot_frozen_bf16")

lerobot_robot_openarm_sciedu/__init__.py:66
    from openarm_sciedu.policies.groot_frozen_bf16 import GrootFrozenBf16Config
```

`lerobot.utils.import_utils.register_third_party_plugins()` 가 `lerobot_robot_*` 이름의
배포판을 찾아 import 하는 것이 **등록 메커니즘의 전부**다. import 되는 순간 데코레이터가
돌고 그때부터 `--policy.type=groot_frozen_bf16` 이 해석된다.

→ `robot_bridge/groot_runner.py:_load_blocking()` 이 `PreTrainedConfig.from_pretrained()`
**전에** `register_third_party_plugins()` 를 부른다. 배선은 이미 맞다.

## 왜 우회가 안 되는가

`config.json` 의 `type` 만 `"groot"` 로 바꿔도 로드되지 않는다. `GrootFrozenBf16Config` 는
필드를 자체 정의하며, 그중 셋은 공개 `openarmsciedu-vla` 포크의 `GrootConfig` 에 **없다**:

| 필드 | 값 |
|---|---|
| `frozen_params_dtype` | `bfloat16` |
| `use_peft` | `False` |
| `pretrained_revision` | `None` |

## ⚠️ 이 코드는 원격에 없다

GitHub `LEAP-LAB-KUS/openarmsciedu` main 을 확인한 결과:

| | 원격 main | leap 머신 |
|---|---|---|
| `src/openarm_sciedu/policies/groot_frozen_bf16/` | **없음** | 있음 |
| `src/openarm_sciedu/policies/wall_x_subtask/` | 있음 | 있음 |
| `lerobot_robot_openarm_sciedu/__init__.py` 의 정책 import | `wall_x_subtask` 하나 (65번) | + `groot_frozen_bf16` (66번) |

**시연을 좌우하는 코드가 머신 한 대에, 추적되지 않은 채로 있다.** venv 를 리포에서 다시
만들면 정책이 로드되지 않는다.

## 우리 방침

**`openarmsciedu` 와 leap 머신의 파일을 직접 수정하지 않는다.** 커밋·푸시도 하지 않는다.
필요하면 **사본을 우리 쪽으로 가져와** 관리한다.

백업 사본을 뜨는 절차 (leap 접근 권한이 있는 쪽에서):

```bash
rsync -av leap:/home/leap/Documents/openarmsciedu/src/openarm_sciedu/policies/groot_frozen_bf16/ \
      ./vendor/groot_frozen_bf16/
```

가져온 사본은 **참고·백업용**이다. 실행 시점의 의존성은 leap 머신에 설치된 플러그인이
그대로 충족한다 — 우리가 그 파일을 바꾸지 않기 때문에 사본과 실물이 갈라질 일이 없다.

사본을 떠 두는 이유는 하나다: 그 머신이 사라지면 **정책을 아무도 로드할 수 없기 때문**이다.
