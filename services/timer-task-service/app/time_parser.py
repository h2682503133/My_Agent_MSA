"""
灵活时间解析器：开始时间 + 间隔重复（多单位 + 区间随机时长）分开解析。

协议格式：定时任务:任务类别|智能体id|任务内容|开始时间|重复计划(可选,0=不重复)

- parse_time_spec(spec)  -> (next_trigger_ts, schedule_dict | None)
  解析「开始时间」（第 4 参）。interval 语法也会被识别（兼容旧格式
  第 4 参直接写重复计划：此时返回 interval schedule 由调用方处理）。
- parse_repeat_spec(spec) -> dict | None
  仅解析「重复计划」（第 5 参），非间隔语法返回 None。

支持格式（大小写、全半角、多余空格均兼容）：

  1. 开始时间
     - 立即：现在 / 立即 / 马上（或第 4 参留空）
     - 绝对时间：2026-01-31 10:00:00 / 2026-01-31 10:00 / 2026-01-31
       / 2026/01/31 10:00 / 2026-1-31 0:00
     - 当天时间：10:00 / 10:00:30 / 10点 / 10点30分 / 10点半
       / 早上7点 / 上午9点 / 中午12点 / 下午3点 / 晚上8点
       （今天已过自动顺延到明天）
     - 相对时间：5分钟后 / 30秒后 / 2小时后 / 3天后 / 明天10:00 / 后天9点 / 今晚8点
     - 区间随机：10:00-11:00（当天随机时刻）/ 5-10分钟后（随机延迟）
  2. 重复计划（间隔重复，统一建模 interval_range = [lo, hi]，不随机时 lo == hi）
     - 多单位：每10秒 / 每30分钟 / 每2小时 / 每天(1天) / 每1小时30分钟
     - 区间随机（两端均可混合单位）：每5-10分钟 / 每1-2小时
       / 每5分钟-2小时 / 每1小时30分钟-2小时（每次触发随机取区间内时长）

schedule_dict 结构（存入任务 JSON）：
  once:     None（schedule 字段省略）
  interval: {"type": "interval", "interval_range": [lo_s, hi_s]}
            兼容旧格式 {"type": "interval", "interval_seconds": N}（读取时自动归一）
"""

import random
import re
import time as _time
from datetime import datetime, timedelta

UNIT_SECONDS = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400, "日": 86400}


# ======================
# 基础工具
# ======================
def _normalize(text: str) -> str:
    """全角转半角 + 折叠多余空白 + 统一冒号。"""
    text = (text or "").strip()
    result = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            result.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(ch)
    text = "".join(result)
    text = text.replace("：", ":")
    text = re.sub(r"\s+", " ", text)
    return text.strip().strip("，,。.、 ")


def _fmt_tod(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h:02d}:{m:02d}"


def _parse_time_of_day(text: str) -> int | None:
    """解析一天内的时刻，返回当日秒数。支持 10:00 / 10点30分 / 10点半 / 下午3点。"""
    if not text:
        return None

    m = re.match(r"(早上|上午|中午|下午|傍晚|晚上)?\s*(\d{1,2})点半", text)
    if m:
        hour = int(m.group(2))
        if not 0 <= hour <= 23:
            return None
        if m.group(1) in ("中午", "下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        return hour * 3600 + 1800

    m = re.match(r"(早上|上午|中午|下午|傍晚|晚上)?\s*(\d{1,2})点(?:(\d{1,2})分)?", text)
    if m:
        hour = int(m.group(2))
        minute = int(m.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if m.group(1) in ("中午", "下午", "傍晚", "晚上") and hour < 12:
            hour += 12
        return hour * 3600 + minute * 60

    m = re.match(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        second = int(m.group(3) or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return hour * 3600 + minute * 60 + second

    return None


def _parse_time_window(text: str) -> tuple[int, int] | None:
    """解析一天内的时间区间，返回 (起始秒, 结束秒)。支持 10:00-11:00 / 下午3点-5点。"""
    parts = re.split(r"\s*[-至到~]\s*", text)
    if len(parts) != 2:
        return None
    t1 = _parse_time_of_day(parts[0].strip())
    t2 = _parse_time_of_day(parts[1].strip())
    if t1 is not None and t2 is not None and t1 < t2:
        return (t1, t2)
    # 下午3点-5点：后半段省略前缀时继承前半段的 12 小时偏移
    if (
        t1 is not None
        and t2 is not None
        and t1 >= t2
        and re.match(r"^(?:早上|上午|中午|下午|傍晚|晚上)", parts[0].strip())
        and not re.match(r"^(?:早上|上午|中午|下午|傍晚|晚上)", parts[1].strip())
    ):
        t2 += 12 * 3600
        if t1 < t2 and t2 < 24 * 3600:
            return (t1, t2)
    return None


# ======================
# 重复计划解析（间隔重复：多单位 + 区间随机时长，区间可混合单位）
# ======================
def _duration_seconds(part: str) -> int | None:
    """解析固定时长（无区间），支持多段混合单位：1小时30分钟 / 5分钟 / 30秒。"""
    part = part.strip()
    if not part:
        return None
    seg_re = re.compile(r"(\d+)?\s*(秒|分钟|小时|天|日)")
    segs = seg_re.findall(part)
    if not segs:
        return None
    matched = "".join((num_part or "") + unit for num_part, unit in segs)
    if matched != part.replace(" ", ""):
        return None
    total = 0
    for num_part, unit in segs:
        mult = UNIT_SECONDS[unit]
        if not num_part:
            total += mult
            continue
        n = int(num_part)
        if n <= 0:
            return None
        total += n * mult
    return total


def _first_unit(part: str) -> str | None:
    m = re.search(r"(秒|分钟|小时|天|日)", part)
    return m.group(1) if m else None


def _parse_interval(text: str) -> dict | None:
    """每10秒 / 每30分钟 / 每2小时 / 每天 / 每1小时30分钟 / 每5分钟30秒
    / 每5-10分钟 / 每1-2小时 / 每5分钟-2小时 / 每1小时30分钟-2小时。

    统一返回 {"type": "interval", "interval_range": [lo, hi]}；
    不随机（单一时长）时 lo == hi。区间两端均可为多段混合单位。
    """
    m = re.match(r"每(?:隔)?(.+)", text)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None

    # 1) 区间：低界-高界（可混合单位）：每5分钟-2小时 / 每1小时30分钟-2小时 / 每5-10分钟
    range_parts = re.split(r"\s*[-至到~]\s*", body)
    if len(range_parts) == 2:
        lo_part = range_parts[0].strip()
        hi_part = range_parts[1].strip()
        lo_sec = _duration_seconds(lo_part)
        hi_sec = _duration_seconds(hi_part)
        # 单侧纯数字时继承对侧单位：每5-10分钟 / 每5分钟-10
        if lo_sec is None and lo_part.isdigit():
            hi_unit = _first_unit(hi_part)
            if hi_unit is not None:
                lo_sec = int(lo_part) * UNIT_SECONDS[hi_unit]
        if hi_sec is None and hi_part.isdigit():
            lo_unit = _first_unit(lo_part)
            if lo_unit is not None:
                hi_sec = int(hi_part) * UNIT_SECONDS[lo_unit]
        if lo_sec is not None and hi_sec is not None and 0 < lo_sec <= hi_sec:
            return {"type": "interval", "interval_range": [lo_sec, hi_sec]}

    # 2) 固定时长（可多段混合单位）：每1小时30分钟 / 每5分钟30秒 / 每小时
    seg_re = re.compile(r"(\d+(?:-\d+)?)?\s*(秒|分钟|小时|天|日)")
    segs = seg_re.findall(body)
    if not segs:
        return None
    # 校验无残留文本：防止「每天10:00」「每月1日10:00」被当作每1天
    matched = "".join((num_part or "") + unit for num_part, unit in segs)
    if matched != body.replace(" ", ""):
        return None
    lo_total = 0
    hi_total = 0
    for num_part, unit in segs:
        mult = UNIT_SECONDS[unit]
        if not num_part:
            lo_total += mult
            hi_total += mult
            continue
        if "-" in num_part:
            a, b = (int(x) for x in num_part.split("-", 1))
            if a <= 0 or b < a:
                return None
            lo_total += a * mult
            hi_total += b * mult
        else:
            n = int(num_part)
            if n <= 0:
                return None
            lo_total += n * mult
            hi_total += n * mult
    if lo_total <= 0 or hi_total < lo_total:
        return None
    return {"type": "interval", "interval_range": [lo_total, hi_total]}


# ======================
# 开始时间解析（一次性）
# ======================
def _parse_relative(text: str) -> float | None:
    """5分钟后 / 30秒后 / 2小时后 / 3天后 / 5-10分钟后 / 明天10:00 / 今晚8点。"""
    m = re.match(r"(\d+(?:-\d+)?)\s*(秒|分钟|小时|天|日)\s*(?:后|之后|以后)", text)
    if m:
        num_part, unit = m.group(1), m.group(2)
        now = _time.time()
        if "-" in num_part:
            a, b = (int(x) for x in num_part.split("-", 1))
            if a <= 0 or b < a:
                return None
            return now + random.randint(a * UNIT_SECONDS[unit], b * UNIT_SECONDS[unit])
        num = int(num_part)
        if num <= 0:
            return None
        return now + num * UNIT_SECONDS[unit]

    m = re.match(r"(明天|后天)\s*(.*)", text)
    if m:
        day_offset = 1 if m.group(1) == "明天" else 2
        rest = m.group(2).strip()
        tod = _parse_time_of_day(rest) if rest else 0
        if rest and tod is None:
            return None
        target = datetime.now() + timedelta(days=day_offset)
        return datetime(target.year, target.month, target.day).timestamp() + tod

    m = re.match(r"今晚\s*(.*)", text)
    if m:
        rest = m.group(1).strip()
        tod = _parse_time_of_day(rest)
        if tod is None:
            return None
        now = datetime.now()
        cand = datetime(now.year, now.month, now.day).timestamp() + tod
        if cand <= _time.time():
            cand += 86400
        return cand

    return None


def _parse_absolute(text: str) -> float | None:
    """2026-01-31 10:00:00 / 2026/01/31 10:00 / 2026-1-31。"""
    m = re.match(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
        r"(?:\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        text,
    )
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    if not (
        1 <= month <= 12
        and 1 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 59
    ):
        return None
    try:
        return datetime(year, month, day, hour, minute, second).timestamp()
    except ValueError:
        return None


# ======================
# 主入口
# ======================
# 立即执行：开始时间为空时的默认语义（也可显式写这些词）
IMMEDIATE_WORDS = {"现在", "立即", "马上", "立刻", "now", "immediately", "0分钟后"}


def parse_repeat_spec(spec: str) -> dict | None:
    """仅解析重复计划（间隔重复），成功返回 interval schedule，否则 None。

    用于第五参「重复计划」：每30分钟 / 每5-10分钟 / 每1小时30分钟。
    非间隔语法（如 10:00、5分钟后）返回 None。
    """
    if not spec:
        return None
    return _parse_interval(_normalize(spec))


def parse_time_spec(spec: str) -> tuple[float, dict | None]:
    """
    解析时间描述，返回 (首次触发时间戳, 重复计划或 None)。

    解析失败抛出 ValueError（带中文原因），由调用方转为用户可见错误。
    """
    text = _normalize(spec)
    if not text:
        raise ValueError("任务时间不能为空")

    # 0. 立即执行
    if text in IMMEDIATE_WORDS:
        return _time.time(), None

    # 1. 重复计划（每N单位 / 每N-M单位，可多段混合单位）
    schedule = _parse_interval(text)
    if schedule:
        nxt = next_trigger(schedule, _time.time())
        if nxt is None:
            raise ValueError(f"无法计算重复计划的首次触发时间：{spec}")
        return nxt, schedule

    # 2. 相对时间
    rel = _parse_relative(text)
    if rel:
        return rel, None

    # 3. 绝对时间
    abs_ts = _parse_absolute(text)
    if abs_ts:
        return abs_ts, None

    # 4. 一次性区间随机：10:00-11:00（今天窗口，已过则明天）
    win = _parse_time_window(text)
    if win:
        lo, hi = win
        now = datetime.now()
        base = datetime(now.year, now.month, now.day).timestamp()
        if base + hi <= _time.time():
            base += 86400
        return base + random.randint(lo, hi), None

    # 5. 当天时间（今天已过则明天）
    tod = _parse_time_of_day(text)
    if tod is not None:
        now = datetime.now()
        base = datetime(now.year, now.month, now.day).timestamp()
        cand = base + tod
        if cand <= _time.time():
            cand += 86400
        return cand, None

    raise ValueError(f"无法识别的任务时间：{spec}")


# ======================
# 重复任务：计算下一次触发
# ======================
def _interval_range(schedule: dict) -> tuple[int, int]:
    """归一化间隔：优先 interval_range，兼容旧 interval_seconds。"""
    rng = schedule.get("interval_range")
    if rng and len(rng) >= 2:
        lo, hi = int(rng[0]), int(rng[1])
        if hi < lo:
            hi = lo
        return (lo, hi)
    secs = int(schedule.get("interval_seconds", 0) or 0)
    return (secs, secs)


def next_trigger(schedule: dict, from_ts: float) -> float | None:
    """根据重复计划计算 from_ts 之后的下一次触发时间戳。"""
    if not schedule or schedule.get("type") != "interval":
        return None
    lo, hi = _interval_range(schedule)
    if lo <= 0:
        return None
    if lo == hi:
        return from_ts + lo
    return from_ts + random.randint(lo, hi)


# ======================
# 展示
# ======================
def _fmt_interval(seconds: int) -> str:
    """把秒数转成人类可读多单位：1天2小时 / 5分钟30秒 / 10秒。"""
    parts = []
    for unit, mult in (("天", 86400), ("小时", 3600), ("分钟", 60), ("秒", 1)):
        if seconds >= mult:
            n = seconds // mult
            seconds %= mult
            parts.append(f"{n}{unit}")
    return "".join(parts) or "0秒"


def _fmt_interval_range(lo: int, hi: int) -> str:
    s_lo = _fmt_interval(lo)
    s_hi = _fmt_interval(hi)
    # 同单位区间简写：5分钟-10分钟 → 5-10分钟；1小时-2小时 → 1-2小时
    m_hi = re.match(r"^(\d+)(秒|分钟|小时|天)$", s_hi)
    if m_hi:
        m_lo = re.match(r"^(\d+)(秒|分钟|小时|天)$", s_lo)
        if m_lo and m_lo.group(2) == m_hi.group(2):
            return f"{m_lo.group(1)}-{m_hi.group(1)}{m_hi.group(2)}"
    return f"{s_lo}-{s_hi}"


def schedule_to_str(schedule: dict) -> str:
    """把重复计划转成人类可读描述，如「每30分钟」「每5-10分钟随机间隔」。"""
    if not schedule or schedule.get("type") != "interval":
        return ""
    lo, hi = _interval_range(schedule)
    if lo <= 0:
        return ""
    if lo == hi:
        return f"每{_fmt_interval(lo)}"
    return f"每{_fmt_interval_range(lo, hi)}随机间隔"
