"""usage 归一自检：DeepSeek / OpenAI / Anthropic / Ollama 的缓存字段都要能被识别。

背景：DeepSeek 把 prompt_tokens 拆成 prompt_cache_hit_tokens / prompt_cache_miss_tokens；
OpenAI 放在 prompt_tokens_details.cached_tokens；Anthropic 用 cache_read_input_tokens。
命中缓存计费低一个量级，所以日志必须能看出来。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.provider_client import parse_usage, usage_log_fields  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name} {extra if not cond else ''}")
    if not cond:
        FAILS.append(name)


print("parse_usage：四种 provider 形状")
ds = parse_usage({"prompt_tokens": 12000, "completion_tokens": 340,
                  "prompt_cache_hit_tokens": 10000, "prompt_cache_miss_tokens": 2000})
check("DeepSeek 直给 hit/miss", ds["cached_tokens"] == 10000 and ds["cache_miss_tokens"] == 2000, ds)

oa = parse_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                  "prompt_tokens_details": {"cached_tokens": 768}})
check("OpenAI details.cached_tokens", oa["cached_tokens"] == 768, oa)
check("miss 由 prompt-cached 推出", oa["cache_miss_tokens"] == 232, oa)

an = parse_usage({"prompt_tokens": 1000, "completion_tokens": 50, "cache_read_input_tokens": 900})
check("Anthropic cache_read_input_tokens", an["cached_tokens"] == 900 and an["cache_miss_tokens"] == 100, an)

plain = parse_usage({"prompt_tokens": 1000, "completion_tokens": 50})
check("没有缓存字段 → cached=0/miss=prompt", plain["cached_tokens"] == 0 and plain["cache_miss_tokens"] == 1000, plain)

ol = parse_usage({}, prompt_fallback=800, completion_fallback=20)
check("Ollama 用 fallback 计数", ol["prompt_tokens"] == 800 and ol["completion_tokens"] == 20, ol)
check("Ollama 无缓存概念", ol["cached_tokens"] == 0 and ol["cache_miss_tokens"] == 800, ol)

print("边界")
check("空/None 不炸", parse_usage(None) == {"prompt_tokens": 0, "completion_tokens": 0,
                                            "cached_tokens": 0, "cache_miss_tokens": 0})
check("cached 不超过 prompt", parse_usage({"prompt_tokens": 10, "prompt_cache_hit_tokens": 99})["cached_tokens"] == 10)
check("负数归零", parse_usage({"prompt_tokens": 0, "prompt_cache_hit_tokens": -5})["cached_tokens"] == 0)
check("字符串数字也能解析", parse_usage({"prompt_tokens": "100", "prompt_cache_hit_tokens": "40"})["cached_tokens"] == 40)

print("usage_log_fields")
log_line = usage_log_fields(ds)
check("日志含 cached/miss/hit%", "cached=10000" in log_line and "miss=2000" in log_line and "83.3%" in log_line, log_line)
check("prompt=0 不除零（退化时代码不触达除法）", usage_log_fields({"prompt_tokens": 0}) == "prompt=0 completion=0")

print("退化：没有缓存字段/命中为 0 时日志必须与改造前完全一致")
check("无缓存字段 → 老格式", usage_log_fields(plain) == "prompt=1000 completion=50", usage_log_fields(plain))
check("Ollama → 老格式", usage_log_fields(ol) == "prompt=800 completion=20", usage_log_fields(ol))
check("显式 cached=0 → 老格式",
      usage_log_fields({"prompt_tokens": 100, "completion_tokens": 5, "cached_tokens": 0}) == "prompt=100 completion=5")
check("全空 → 老格式", usage_log_fields({}) == "prompt=0 completion=0")
check("有命中才加缓存段", "(cached=" in usage_log_fields(ds))
check("cache_miss_tokens 缺省不影响老格式", usage_log_fields({"prompt_tokens": 10, "completion_tokens": 2}) == "prompt=10 completion=2")

print()
if FAILS:
    print("FAILED:", FAILS)
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks")
