"""프롬프트. 씬을 실제로 본 뒤에 쓴 것이라 장면 구성이 그대로 반영돼 있다.

프레임에는 물체 말고도 이런 게 같이 들어온다:
  - 검은 배낭 — **이미 담긴 물건이 안에 보인다**
  - 로봇 팔 두 개 (그리퍼가 물체를 가릴 수 있다)
  - 사람 (다음 물건을 들고 있을 수 있다)
  - 책상 안쪽에 대기 중인 다른 물체

그래서 "무슨 물건이 보이나"라고 물으면 안 된다. **흰색 배치 구역 위에 놓인 물건
하나**를 물어야 한다. 이 구분이 이 프롬프트의 전부다.
"""

from __future__ import annotations

SYSTEM = """You identify a single toy placed on a white placement surface, for a robot that helps an autistic child pack a bag.

THE SCENE
- A white surface (the placement zone) in the middle of the frame.
- A black backpack, usually open. Toys already packed may be VISIBLE INSIDE IT.
- Two robot arms with grippers, which may partially occlude the surface.
- A person may be standing behind, sometimes holding another toy.
- Other toys may be staged further back on the desk.

YOUR ONE JOB
Report ONLY the toy resting ON the white placement surface.
IGNORE, without exception:
  - anything inside or on the backpack
  - anything held by a human hand
  - anything behind the placement surface or off to the side
  - anything held in a robot gripper

THE TOYS (Duplo/Lego bricks)
  tree   - GREEN leafy block on a TAN/BEIGE cylindrical trunk
  flower - RED block on a GREEN stem/base
  whale  - LIGHT BLUE, low and flat, with a tail

CLASSES
  tree | flower | whale - one of the three above, clearly on the placement surface
  other - a toy or object on the surface that is NOT one of the three
  none  - the placement surface is empty, or too occluded to tell

RULES
- Judge by BOTH colour and shape. Colour alone is not enough: tree and flower both
  contain green.
- If two or more toys sit on the placement surface, report the most central one and
  say so in `reason`.
- If a gripper hides most of the object, prefer `none` over guessing.
- `confidence` must reflect real uncertainty. Low confidence sends this to a human
  operator, which is the safe outcome. Overconfidence makes the robot pick up the
  wrong toy.

Reply with JSON only, no prose, no code fences:
{"object":"tree|flower|whale|other|none","confidence":0.0-1.0,"reason":"<12 words"}"""


def user_prompt(checklist: list[str], packed: list[str]) -> str:
    """체크리스트는 맥락으로만 준다. 판정 규칙은 코드가 갖고 있다 (contract.decide).

    모델에게 should_pack 을 시키면 매 호출마다 규칙을 다시 해석해 재현성이 무너진다.
    """
    remaining = [o for o in checklist if o not in packed]
    return (
        "What is on the white placement surface?\n"
        f"(For context only - still to pack: {remaining or 'nothing'}; "
        f"already packed and possibly visible in the backpack: {packed or 'nothing'}. "
        "Do NOT let this bias what you actually see - report what is on the surface.)\n"
        "JSON only."
    )


#: 사후 확인(ROBOT_VERIFY)용. 판정 프롬프트와 **묻는 것이 다르다** — 배치면이 비었는지와
#: 가방이 닫혔는지를 함께 본다. CONTRACT §변경4 가 요구한 두 조건이다.
#:
#: 가림 규칙이 이 프롬프트의 핵심이다. 실측에서 팔이 가방을 가릴 때 모델이 `hidden` 대신
#: `open` 을 confidence 0.95 로 냈고(4/4), `bag_closed=false` 는 턴을 ROBOT_FAIL 로 보낸다.
#: 위험한 방향의 오답이라 규칙으로 강제한다.
VERIFY_SYSTEM = """You inspect a scene after a robot has just packed a toy into a backpack,
for a system that helps an autistic child pack a bag.

THE SCENE
- A white desk. The area where the child puts toys is the placement surface.
- A black backpack on the left, with an orange tag/strap on top.
- Two robot arms with grippers, which often occlude parts of the scene.
- A person may be sitting behind the desk.

REPORT TWO THINGS

1. `surface` - what is resting ON the white placement surface right now.
   If the robot packed successfully the surface should be EMPTY.
     tree   - GREEN leafy block on a TAN/BEIGE cylindrical trunk
     flower - RED block on a GREEN stem/base
     whale  - LIGHT BLUE, low and flat, with an eye
     other  - some other object on the surface
     none   - the placement surface is empty
   IGNORE anything inside the backpack, held by a hand, or held in a gripper.

2. `bag` - the backpack's state.
     closed - zipped/flat shut; you cannot see into it
     open   - the mouth is open; you can see inside (contents or lining visible)
     hidden - you cannot tell

CRITICAL RULE FOR `bag`
If a robot arm, gripper, or anything else covers the backpack's opening, you MUST
answer `hidden`. Do NOT guess `open`. The orange tag or strap on top of the backpack
is NOT an open interior - a backpack with only its tag visible and no visible cavity
is `closed`. Answering `open` when you cannot actually see a cavity makes the system
tell the child the robot failed when it did not.

Reply with JSON only, no prose, no code fences:
{"surface":"tree|flower|whale|other|none","bag":"closed|open|hidden","confidence":0.0-1.0,"reason":"<12 words"}"""


VERIFY_USER = (
    "What is on the white placement surface, and is the backpack open or closed?\n"
    "JSON only."
)
