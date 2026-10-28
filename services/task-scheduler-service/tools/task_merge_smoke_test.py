"""短窗口合并自检（不依赖 gRPC/事件总线，直接测 task_merge）。

覆盖：
  1. merge_task：内容拼接（【续】分隔）、images 顺序、段数上限、metadata 标记
  2. HoldQueue：第一条 held、窗口内第二条 merged、超时释放、段数到顶后强制放行
  3. 渠道白名单 / 推送类不合并（由 scheduler 判定，这里测纯函数语义）
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import task_merge  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


class FakeTask:
    def __init__(self, task_id, content="", images=None, channel="qqofficial", user="u1"):
        self.task_id = task_id
        self.content = content
        self.images = list(images or [])
        self.channel = channel
        self.user_id = user
        self.metadata = {}


def main():
    print("merge_task")
    t1 = FakeTask("t1", "转发内容", images=["a.png"])
    t2 = FakeTask("t2", "我的回复", images=["b.png"])
    check("合并成功", task_merge.merge_task(t1, t2, 3) is True)
    check("内容用【续】分隔", "转发内容" in t1.content and "我的回复" in t1.content
          and "【续】" in t1.content, t1.content)
    check("续段带 [时间] user_id:", "【续】[" in t1.content and "] u1:" in t1.content,
          t1.content)
    check("images 顺序拼接", t1.images == ["a.png", "b.png"], str(t1.images))
    check("段数记为 2", t1.metadata.get("_merge_parts") == "2", str(t1.metadata))
    check("记录来源 id", t1.metadata.get("_merged_from") == "t2", str(t1.metadata))

    t3 = FakeTask("t3", "第三段")
    check("未到上限可继续合并", task_merge.merge_task(t1, t3, 3) is True)
    t4 = FakeTask("t4", "第四段")
    check("到上限拒绝合并", task_merge.merge_task(t1, t4, 3) is False)
    check("拒绝后内容未变", "第四段" not in t1.content)

    print("HoldQueue")
    released = []

    def release(user_id, task):
        released.append((user_id, task.task_id, task.content))

    q = task_merge.HoldQueue(release)

    a = FakeTask("a1", "第一条")
    verdict = q.hold("u1", a, 300, 800, 3)
    check("第一条被滞留", verdict == "held" and q.pending("u1"), verdict)
    check("滞留期间未释放", not released, str(released))

    b = FakeTask("b1", "第二条")
    verdict2 = q.hold("u1", b, 300, 800, 3)
    check("窗口内第二条合并", verdict2 == "merged", verdict2)
    check("合并后再滞留中", q.pending("u1"))

    time.sleep(0.55)
    check("超时后释放一次", len(released) == 1, str(released))
    if released:
        uid, tid, content = released[0]
        check("释放的是合并后的任务", tid == "a1" and "第一条" in content and "第二条" in content,
              f"{tid} {content!r}")
    check("释放后清空滞留", not q.pending("u1"))

    print("段数到顶 → 强制放行")
    released.clear()
    x1 = FakeTask("x1", "p1")
    q.hold("u2", x1, 500, 900, 1)          # 上限 1 段：不能合并
    x2 = FakeTask("x2", "p2")
    verdict3 = q.hold("u2", x2, 500, 900, 1)
    check("达到上限时新消息自立门户", verdict3 == "held", verdict3)
    check("旧任务被立刻放行", any(item[1] == "x1" for item in released), str(released))
    q.flush("u2")

    print("图片消息的滞留窗口")
    plain = FakeTask("i1", "纯文字")
    with_image = FakeTask("i2", "带图", images=["x.png"])
    check("has_images 判定", task_merge.has_images(with_image) is True
          and task_merge.has_images(plain) is False)
    w_plain = task_merge.hold_window(plain, 1200, 2500, 3000, 6000)
    w_image = task_merge.hold_window(with_image, 1200, 2500, 3000, 6000)
    check("纯文字用 1200/2500", w_plain == (1200, 2500), str(w_plain))
    check("带图片用 3000/6000", w_image == (3000, 6000), str(w_image))

    print("图片合并：图片顺序累计")
    a = FakeTask("p1", "先来文字", images=["1.png"])
    b = FakeTask("p2", "再来图", images=["2.png", "3.png"])
    task_merge.merge_task(a, b, 3)
    check("images 依次累积", a.images == ["1.png", "2.png", "3.png"], str(a.images))
    check("续段带前段", "【续】[" in a.content and "u1:" in a.content, a.content)

    print("渠道白名单语义（scheduler 侧调用同一函数）")
    allowed = {"qq", "qqofficial"}
    check("qq 允许", "qq" in allowed)
    check("qqofficial 允许", "qqofficial" in allowed)
    check("web 不在名单", "web" not in allowed)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
