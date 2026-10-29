"""生图闸门自检：尺寸上限 + 频率限流（不联网、不依赖 requests）。

覆盖：
 1. decide_size：没配上限 / max_size 单边 / max_pixels / 两种都配取更严 / 写法非法
 2. 默认尺寸：default_size 优先，其次 max_size，都没配则交给接口默认
 3. RateLimiter：per_minute / per_hour / per_day 触发与等待秒数；未配置=不限；
    成功记录才计数（失败不记，见 provider_client）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.image_limits import RateLimiter, decide_size, parse_size  # noqa: E402

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


def main():
    print("尺寸解析")
    check("1024x1024", parse_size("1024x1024") == (1024, 1024))
    check("1024*768", parse_size("1024*768") == (1024, 768))
    check("全角 ×", parse_size("512×512") == (512, 512))
    check("非法写法", parse_size("大图") is None)
    check("0 边", parse_size("0x100") is None)

    print("decide_size：没配上限")
    decision = decide_size({}, "2048x2048")
    check("放行", decision.ok is True and decision.size == "2048x2048", str(decision))
    decision = decide_size({}, "")
    check("不给尺寸也不给默认 → 空串（交给接口）", decision.ok and decision.size == "", str(decision))

    print("decide_size：max_size 单边上限")
    profile = {"max_size": "1024x1024"}
    check("合规尺寸放行", decide_size(profile, "1024x1024").ok is True)
    over = decide_size(profile, "1025x1024")
    check("超宽被拒", over.ok is False and "上限" in over.error, over.error)
    tall = decide_size(profile, "800x1440")
    check("超高被拒", tall.ok is False, tall.error)
    check("错误里带具体数值", "1025" in over.error and "1024" in over.error, over.error)

    print("decide_size：max_pixels")
    profile_px = {"max_pixels": 1024 * 1024}
    check("刚好等于上限放行", decide_size(profile_px, "1024x1024").ok is True)
    px_over = decide_size(profile_px, "1024x1025")
    check("超像素被拒", px_over.ok is False and "像素" in px_over.error, px_over.error)

    print("decide_size：两个上限都配（取更严）")
    both = {"max_size": "2048x2048", "max_pixels": 1024 * 1024}
    check("长宽合规但像素超 → 拒", decide_size(both, "1500x1500").ok is False)

    print("decide_size：默认尺寸")
    check("default_size 优先", decide_size({"default_size": "512x512", "max_size": "1024x1024"}, "").size == "512x512")
    check("无 default 时用 max_size", decide_size({"max_size": "1024x1024"}, "").size == "1024x1024")
    check("调用方指定覆盖默认", decide_size({"default_size": "512x512"}, "768x768").size == "768x768")

    print("decide_size：非法输入显式报错")
    bad = decide_size({"max_size": "1024x1024"}, "巨幅")
    check("尺寸写法错 → 明确报错", bad.ok is False and "写法不对" in bad.error, bad.error)
    bad_cfg = decide_size({"max_size": "big"}, "512x512")
    check("配置写错 → 明确报错", bad_cfg.ok is False and "max_size" in bad_cfg.error, bad_cfg.error)

    print("RateLimiter：每分钟 2 次")
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    profile = {"rate_limit": {"per_minute": 2}}
    check("第 1 次放行", limiter.check("image-gen", profile)[0] is True)
    limiter.record("image-gen")
    now[0] += 1
    check("第 2 次放行", limiter.check("image-gen", profile)[0] is True)
    limiter.record("image-gen")
    ok, wait, error = limiter.check("image-gen", profile)
    check("第 3 次被限", ok is False, str(ok))
    check("等待时间约 59 秒", 58 <= wait <= 60, str(wait))
    check("错误里说明频率与等待", "每分钟" in error and "秒后再试" in error, error)
    now[0] += 60
    check("窗口过去后恢复", limiter.check("image-gen", profile)[0] is True)

    print("RateLimiter：不同 profile 独立计数")
    limiter2 = RateLimiter(clock=lambda: 100.0)
    prof = {"rate_limit": {"per_minute": 1}}
    limiter2.record("a")
    check("a 被限", limiter2.check("a", prof)[0] is False)
    check("b 不受影响", limiter2.check("b", prof)[0] is True)

    print("RateLimiter：未配置 = 不限")
    limiter3 = RateLimiter(clock=lambda: 0.0)
    for _ in range(50):
        limiter3.record("x")
    check("无 rate_limit 不拦", limiter3.check("x", {})[0] is True)

    print("RateLimiter：per_day")
    limiter4 = RateLimiter(clock=lambda: 0.0)
    prof_day = {"rate_limit": {"per_day": 1}}
    limiter4.record("d")
    ok, wait, error = limiter4.check("d", prof_day)
    check("当天第 2 次被限", ok is False and "每天" in error, error)
    check("等待接近一天", wait > 86000, str(wait))


    print("RateLimiter：小数速率（0.5/分钟 = 每 2 分钟一次）")
    now2 = [0.0]
    limiter5 = RateLimiter(clock=lambda: now2[0])
    prof_fraction = {"rate_limit": {"per_minute": 0.5}}
    check("第 1 次放行", limiter5.check("img", prof_fraction)[0] is True)
    limiter5.record("img")
    ok, wait, error = limiter5.check("img", prof_fraction)
    check("紧接着被限", ok is False, str(ok))
    check("等待约 120 秒", 119 <= wait <= 121, str(wait))
    check("文案带'每 X 秒一次'", "每 120 秒一次" in error, error)
    now2[0] += 100
    check("100 秒后仍被限", limiter5.check("img", prof_fraction)[0] is False)
    now2[0] += 25
    check("满 2 分钟后放行", limiter5.check("img", prof_fraction)[0] is True)

    print("RateLimiter：2.5/分钟（非整数）也按间隔")
    now3 = [0.0]
    limiter6 = RateLimiter(clock=lambda: now3[0])
    prof_25 = {"rate_limit": {"per_minute": 2.5}}
    limiter6.record("x")
    ok, wait, _err = limiter6.check("x", prof_25)
    check("间隔 24 秒", ok is False and 23 <= wait <= 25, str(wait))
    now3[0] += 24
    check("够间隔就放行", limiter6.check("x", prof_25)[0] is True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
