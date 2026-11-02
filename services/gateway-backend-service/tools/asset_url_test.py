"""图片地址改写自检（离线，纯函数）。"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from asset_urls import rewrite_asset_urls, rewrite_images  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    print("单条文本里的绝对图片地址")
    cases = [
        ("看图 http://image-assets-service/assets/cat.png 好可爱",
         "看图 /api/assets/cat.png 好可爱"),
        ("![x](http://image-assets-service:80/assets/a%20b.webp)",
         "![x](/api/assets/a%20b.webp)"),
        ("https://cdn.example.com/assets/1.png", "/api/assets/1.png"),
        ("http://127.0.0.1:5102/assets/x.jpg", "/api/assets/x.jpg"),
        ("没有图片的文本", "没有图片的文本"),
        ("", ""),
    ]
    for src, want in cases:
        got = rewrite_asset_urls(src)
        check(f"{src[:38]!r} → 相对路径", got == want, f"got={got!r}")

    print("图片列表")
    imgs = ["http://image-assets-service/assets/a.png", "/api/assets/b.png",
            "https://x/assets/c.png"]
    check("逐条改写",
          rewrite_images(imgs) == ["/api/assets/a.png", "/api/assets/b.png", "/api/assets/c.png"],
          str(rewrite_images(imgs)))
    check("幂等（不会变成 /api/api/assets）",
          rewrite_asset_urls(rewrite_asset_urls("http://h/assets/a.png")) == "/api/assets/a.png")
    check("空列表安全", rewrite_images(None) == [])
    check("非字符串元素原样保留", rewrite_images([None]) == [None])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
