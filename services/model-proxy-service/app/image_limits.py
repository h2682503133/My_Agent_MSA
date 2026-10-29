"""生图的两道闸：尺寸上限 + 调用频率（都在 model-proxy 侧强制，工具层只透传错误）。

配置写在 `model_list.json` 的 image 条目里（**不影响 chat 条目**）：

```json
"image-gen": {
  "kind": "image",
  "api_url": "...",
  "model": "...",
  "api_key": "...",
  "model_params": { "response_format": "url" },

  "default_size": "1024x1024",   // 调用方没指定时用这个（可省略 → 用 max_size）
  "max_size": "1440x1440",       // 单边/长宽上限：超出直接报错，不静默缩
  "max_pixels": 2073600,         // 可选：像素总数上限（1024*1024*2 之类）
  "rate_limit": { "per_minute": 5, "per_hour": 60, "per_day": 200 }
  // 允许小数：per_minute: 0.5 = 每 2 分钟 1 次（按最小间隔执行）
}
```

三种上限可只配一种；`max_pixels` 与 `max_size` 同时配时按更严格的那个判。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any

_SIZE_RE = re.compile(r"^\s*(\d{1,5})\s*[xX*×]\s*(\d{1,5})\s*$")


def parse_size(text: str) -> tuple[int, int] | None:
    """`1024x1024` / `1024*1024` / `1024×1024` → (1024, 1024)；解析不出返回 None。"""
    match = _SIZE_RE.match(str(text or ""))
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    return width, height


@dataclass
class SizeDecision:
    ok: bool
    size: str = ""        # 最终要发给接口的尺寸（可能来自 default_size）
    error: str = ""


def decide_size(profile: dict[str, Any], requested: str) -> SizeDecision:
    """按 profile 的 default_size / max_size / max_pixels 判定本次尺寸。

    - 调用方给了 size → 必须能解析，且不得超出上限（超了显式报错，不静默缩）
    - 调用方没给 → 用 default_size；没配 default_size 就用 max_size；都没配 → ""（交给接口默认）
    """
    max_size_text = str(profile.get("max_size") or "").strip()
    default_text = str(profile.get("default_size") or "").strip() or max_size_text
    max_pixels_raw = profile.get("max_pixels")

    requested = str(requested or "").strip()
    if requested:
        parsed = parse_size(requested)
        if parsed is None:
            return SizeDecision(ok=False, error=f"尺寸写法不对：{requested!r}（应形如 1024x1024）")
        width, height = parsed
        size_text = f"{width}x{height}"
    elif default_text:
        parsed = parse_size(default_text)
        if parsed is None:
            return SizeDecision(ok=False, error=f"配置里的 default_size 写错了：{default_text!r}")
        width, height = parsed
        size_text = f"{width}x{height}"
    else:
        return SizeDecision(ok=True, size="")

    # 上限校验：先单边，再像素
    if max_size_text:
        cap = parse_size(max_size_text)
        if cap is None:
            return SizeDecision(ok=False, error=f"配置里的 max_size 写错了：{max_size_text!r}")
        cap_w, cap_h = cap
        if width > cap_w or height > cap_h:
            return SizeDecision(
                ok=False,
                error=(
                    f"尺寸 {size_text} 超过上限 {cap_w}x{cap_h}"
                    f"（宽 {width}/{cap_w}，高 {height}/{cap_h}）"
                ),
            )

    if max_pixels_raw not in (None, "", 0, "0"):
        try:
            max_pixels = int(max_pixels_raw)
        except (TypeError, ValueError):
            return SizeDecision(ok=False, error=f"配置里的 max_pixels 不是整数：{max_pixels_raw!r}")
        if max_pixels > 0 and width * height > max_pixels:
            return SizeDecision(
                ok=False,
                error=f"尺寸 {size_text} 共 {width * height} 像素，超过上限 {max_pixels}",
            )

    return SizeDecision(ok=True, size=size_text)


class RateLimiter:
    """按 profile 的滑动窗口限流（内存态，进程级）。

    只约束生图这种"贵"的调用；配置在 model_list.json 的 rate_limit 里：
      {"per_minute": 5, "per_hour": 60, "per_day": 200}
    没配 = 不限。被限时返回明确的剩余等待秒数，方便调用方原样回给模型/用户。
    """

    WINDOWS = (("per_minute", 60), ("per_hour", 3600), ("per_day", 86400))

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def check(self, profile_name: str, profile: dict[str, Any]) -> tuple[bool, float, str]:
        """返回 (ok, retry_after_seconds, error)。"""
        limits = profile.get("rate_limit") or {}
        if not isinstance(limits, dict):
            return True, 0.0, ""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(profile_name, [])
            # 只保留最长窗口内的记录
            longest = max(seconds for _key, seconds in self.WINDOWS)
            hits[:] = [t for t in hits if now - t < longest]
            for key, seconds in self.WINDOWS:
                raw = limits.get(key)
                if raw in (None, "", 0, "0"):
                    continue
                try:
                    limit = float(raw)
                except (TypeError, ValueError):
                    continue
                if limit <= 0:
                    continue
                unit = {"per_minute": "每分钟", "per_hour": "每小时", "per_day": "每天"}[key]

                # 小数 = 速率（如 0.5 表示每 2 分钟 1 次）→ 按最小间隔判定
                if limit < 1 or not float(limit).is_integer():
                    gap = seconds / limit
                    if hits:
                        wait = max(gap - (now - hits[-1]), 0.0)
                        if wait > 0:
                            return (
                                False,
                                wait,
                                f"生图频率限制：{unit}最多 {limit:g} 次（约每 {int(gap + 0.999)} 秒一次），"
                                f"请约 {int(wait + 0.999)} 秒后再试（模型 {profile_name}）",
                            )
                    continue

                limit_count = int(limit)
                recent = [t for t in hits if now - t < seconds]
                if len(recent) >= limit_count:
                    oldest = min(recent)
                    wait = max(seconds - (now - oldest), 1.0)
                    return (
                        False,
                        wait,
                        f"生图频率限制：{unit}最多 {limit_count} 次，"
                        f"请约 {int(wait + 0.999)} 秒后再试（模型 {profile_name}）",
                    )
        return True, 0.0, ""

    def record(self, profile_name: str) -> None:
        with self._lock:
            self._hits.setdefault(profile_name, []).append(self._clock())

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {name: len(hits) for name, hits in self._hits.items()}


rate_limiter = RateLimiter()
