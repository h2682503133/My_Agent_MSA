"""llbot 掉线退订逻辑自检（不依赖 satori / gRPC：把 main 里的状态机抽出来跑）。

覆盖：
 1. 掉线边沿 → online_event 清空（事件流应 break）
 2. 上线边沿 → online_event 置位 + flush 缓冲
 3. 连续 N 次发送失败 → 判定掉线
 4. 成功一次即重置计数
"""

import asyncio
import sys

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


class FakeLoop:
    """最小复刻 main.py 里的 online_event + fail_streak 逻辑。"""

    def __init__(self, limit=3):
        self.online = asyncio.Event()
        self.online.set()
        self.flushed = 0
        self.offline_calls = 0
        self.limit = limit
        self.streak = 0

    async def on_online(self):
        self.online.set()
        self.flushed += 1

    async def on_offline(self):
        self.offline_calls += 1
        self.online.clear()

    async def deliver(self, ok: bool):
        """返回 True 表示"应 break 出事件流"。"""
        if ok:
            self.streak = 0
            return not self.online.is_set()
        self.streak += 1
        if self.streak >= self.limit:
            self.streak = 0
            await self.on_offline()
            return True
        return not self.online.is_set()


async def main():
    print("掉线边沿")
    loop = FakeLoop()
    check("初始视为在线", loop.online.is_set() is True)
    await loop.on_offline()
    check("掉线后置空", loop.online.is_set() is False)

    print("上线边沿")
    await loop.on_online()
    check("上线后置位", loop.online.is_set() is True)
    check("上线会 flush 缓冲", loop.flushed == 1)

    print("连续失败判定")
    loop = FakeLoop(limit=3)
    should_break = [await loop.deliver(False) for _ in range(2)]
    check("失败 2 次还不退订", should_break == [False, False], str(should_break))
    check("此时仍在线", loop.online.is_set() is True)
    third = await loop.deliver(False)
    check("第 3 次失败 → 退订", third is True and loop.online.is_set() is False)

    print("成功即重置")
    loop = FakeLoop(limit=3)
    await loop.deliver(False)
    await loop.deliver(False)
    await loop.deliver(True)   # 重置
    check("成功后计数归零", loop.streak == 0, str(loop.streak))
    check("成功后退订状态仍在线", loop.online.is_set() is True)
    two_more = [await loop.deliver(False) for _ in range(2)]
    check("再连续 2 次失败仍不退订（计数从头算）", two_more == [False, False], str(two_more))
    check("离线调用次数为 0", loop.offline_calls == 0, str(loop.offline_calls))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
