#!/usr/bin/env python3
"""Attention dashboard business logic.

Pure: no ``aqt``/``anki`` imports, no HTTP, no global state. All collection
access goes through the :class:`~data.AttentionData` interface injected into
:func:`build_dashboard`, so the entire dashboard can be built and tested
against a fake data source. The add-on's webview layer (see
:mod:`attention_dashboard_window`) calls ``build_dashboard`` from a
``QueryOp`` and hands the resulting dict to JavaScript for rendering.
"""

from __future__ import annotations

import html
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # avoid a runtime import cycle / anki dependency
    from .data import AttentionData


SLOW_MS = 40_000
CONCENTRATION_SLOW_MS = 30_000
OBVIOUS_SLOW_MS = 60_000
TARGET_DAILY_MINUTES = 30
WEEK_DAYS = 7
# 新增空间没有固定配额：按每日节奏搜索预算内的可持续新增量。搜索只设
# 计算保护上限（每天 100 张）；触及上限时 capped=True，展示必须说明
# “实际余量可能更高”，不能把上限冒充精确容量。
NEW_PACE_SEARCH_MAX = 100
# 健康分级固定按 FSRS 未来 90 天模拟判断；simulationDays 配置只能加长
# 观察期（更保守），不能把它缩短到 90 天以下，也不允许大到撑爆 protobuf。
HEALTH_SIMULATION_DAYS = 90
SIMULATION_DAYS_MAX = 730


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    """安全读取整数配置：坏值回退默认值，而不是让看板崩溃。

    OverflowError 一并捕获：JSON 里的 1e309 会解析成 inf，
    ``int(inf)`` 抛的是 OverflowError 而不是 ValueError。与
    ``data.AnkiData.config`` 里的同名逻辑成对存在——数据层保证
    ``config()`` 本身不抛异常，业务层保证任意注入源（含 FakeData）都安全。
    """
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default
# 长尾“突出”的判定：少数卡片（<50%）吃掉至少一半的预计复习时间，且样本足够。
LONG_TAIL_MIN_SAMPLES = 5
LONG_TAIL_TIME_SHARE = 0.5


def strip_html(value: str) -> str:
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(div|p|li|tr|h\d)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    return value.strip()


def has_image(fields: dict[str, Any]) -> bool:
    return any("<img" in (field.get("value") or "").lower() for field in fields.values())


class _FirstImageSrcParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.src = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.src or tag.lower() != "img":
            return
        for name, value in attrs:
            if name.lower() == "src" and value:
                self.src = value.strip()
                return


def first_image_src(fields: dict[str, Any]) -> str:
    html_value = "\n".join(str(field.get("value") or "") for field in fields.values())
    parser = _FirstImageSrcParser()
    try:
        parser.feed(html_value)
    except Exception:
        parser.src = ""
    if parser.src:
        return parser.src
    image_match = re.search(
        r"<img\b[^>]*\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))",
        html_value,
        flags=re.I,
    )
    if not image_match:
        return ""
    return next((group for group in image_match.groups() if group), "")


def first_readable_field(fields: dict[str, Any], names: list[str]) -> tuple[str, str]:
    for name in names:
        field = fields.get(name)
        if not field:
            continue
        text = strip_html(field.get("value") or "")
        if text:
            return name, text
    return "", ""


def field_text(fields: dict[str, Any], names: list[str]) -> str:
    values = []
    for name in names:
        field = fields.get(name)
        if field:
            values.append(strip_html(field.get("value") or ""))
    return "\n".join(v for v in values if v).strip()


def detect_quality(front: str, back: str, image_card: bool) -> list[str]:
    problems: list[str] = []
    combined = f"{front}\n{back}"
    if len(front) > 180:
        problems.append("题干过长")
    if len(back) > 320:
        problems.append("答案过长")
    if len(re.findall(r"\n", back)) >= 5:
        problems.append("答案多行")
    if re.search(r"(哪些|哪几个|几点|步骤|流程|优缺点|列举|分别|如何排查|原因.*有|包括)", combined):
        problems.append("疑似列表卡")
    if len(re.findall(r"[?？]", front)) >= 2:
        problems.append("多问合一")
    if re.search(r"(如下图|本题|第\s*\d+\s*行|上面|下面)", combined):
        problems.append("上下文依赖")
    return problems[:4]


@dataclass
class CardReview:
    card_id: int
    total_ms_90: int
    total_ms_30: int
    reviews_90: int
    slow_count: int
    again_count: int
    avg_ms: int
    last_times: list[int]
    recent_reviews: list[dict[str, Any]] = field(default_factory=list)
    again_day_count: int = 0


def summarize_reviews(card_id: int, reviews: list[dict[str, Any]], now_ms: int) -> CardReview | None:
    start_90 = now_ms - 90 * 24 * 3600 * 1000
    start_30 = now_ms - 30 * 24 * 3600 * 1000
    valid = [
        r
        for r in reviews
        if r.get("ease", 0) > 0 and r.get("time", 0) > 0 and r.get("id", 0) > 0
    ]
    if not valid:
        return None
    in_90 = [r for r in valid if int(r["id"]) >= start_90]
    recent = sorted(valid, key=lambda r: int(r["id"]), reverse=True)[:5]
    total_90 = sum(int(r["time"]) for r in in_90)
    total_30 = sum(int(r["time"]) for r in in_90 if int(r["id"]) >= start_30)
    slow = sum(1 for r in recent if int(r["time"]) >= SLOW_MS)
    again_reviews = [r for r in recent if int(r["ease"]) == 1]
    again = len(again_reviews)
    again_days = {datetime.fromtimestamp(int(r["id"]) / 1000).date() for r in again_reviews}
    last = [round(int(r["time"]) / 1000) for r in recent]
    events = [
        {
            "reviewId": int(r["id"]),
            "reviewedAt": int(r["id"]) / 1000,
            "durationMs": int(r["time"]),
            "rating": int(r["ease"]),
            "reviewType": int(r.get("type", 0)),
        }
        for r in recent
    ]
    return CardReview(
        card_id=card_id,
        total_ms_90=total_90,
        total_ms_30=total_30,
        reviews_90=len(in_90),
        slow_count=slow,
        again_count=again,
        again_day_count=len(again_days),
        avg_ms=round(sum(int(r["time"]) for r in recent) / len(recent)),
        last_times=last,
        recent_reviews=events,
    )


def empty_review(card_id: int) -> CardReview:
    return CardReview(card_id, 0, 0, 0, 0, 0, 0, [], [])


def has_single_obvious_time(review: CardReview) -> bool:
    return any(event.get("durationMs", 0) >= OBVIOUS_SLOW_MS for event in review.recent_reviews)


def has_again_signal(review: CardReview) -> bool:
    return review.again_count >= 3 or (review.again_count >= 2 and review.again_day_count >= 2)


def candidate_reasons(review: CardReview, problems: list[str]) -> list[str]:
    reasons: list[str] = []
    recent_count = len(review.recent_reviews)
    if review.slow_count >= 2:
        reasons.append(f"最近 {recent_count} 次中 {review.slow_count} 次≥40秒")
    if has_again_signal(review):
        if review.again_count >= 3:
            reasons.append(f"最近 {recent_count} 次中 {review.again_count} 次 Again")
        else:
            reasons.append(
                f"最近 {recent_count} 次中 {review.again_count} 次 Again，跨 {review.again_day_count} 个复习日"
            )
    if problems and (review.slow_count >= 2 or has_again_signal(review) or has_single_obvious_time(review)):
        reasons.append("结构疑点")
    return reasons


def make_card_row(review: CardReview, info: dict[str, Any] | None) -> dict[str, Any]:
    fields = (info or {}).get("fields") or {}
    front_names = ["Remarks", "TextFront", "ClozeFront", "Front", "Text", "Question"]
    back_names = ["TextBack", "ClozeBack", "Back", "Back Extra", "Answer"]
    ordered = sorted(fields, key=lambda name: fields[name].get("order", 0))
    ordered_names = [name for name in ordered]
    front_source, front = first_readable_field(fields, front_names)
    if not front:
        front_source, front = first_readable_field(
            fields, [name for name in ordered_names if name not in back_names]
        )
    back = field_text(fields, back_names)
    if not back:
        back = field_text(fields, [name for name in ordered_names if name != front_source])
    image_card = has_image(fields)
    deck = (info or {}).get("deckName", "")
    note_type = (info or {}).get("noteType", "")
    field_names = ordered_names
    tags = (info or {}).get("tags", [])
    problems = detect_quality(front, back, image_card)
    limitations = ["图片无法分析"] if image_card and not front else []
    thumbnail = first_image_src(fields)
    title_parts = [part for part in [note_type, deck, " / ".join(field_names[:3])] if part]
    title_fallback = " · ".join(title_parts + [f"卡片 {review.card_id}"])
    preview_kind = "image" if thumbnail else ("text" if front else "fallback")
    summary = front[:180] or title_fallback
    reasons = candidate_reasons(review, problems)
    action = "审计"
    if "面试" in front + back or "考试" in front + back:
        action = "阶段卡：考虑暂停并打标签"
    elif any(p in problems for p in ["题干过长", "答案过长", "疑似列表卡", "多问合一"]):
        action = "疑似卡差：考虑拆小"
    elif review.avg_ms >= SLOW_MS and review.again_count == 0:
        action = "有价值但难：先观察"
    return {
        "card_id": review.card_id,
        "cardId": review.card_id,
        "noteId": int((info or {}).get("noteId", 0)),
        "deckId": int((info or {}).get("deckId", 0)),
        "deck": deck,
        "summary": summary,
        "thumbnail": thumbnail,
        "previewKind": preview_kind,
        "previewText": front[:180],
        "titleFallback": title_fallback,
        "noteType": note_type,
        "fieldNames": field_names,
        "suspended": bool((info or {}).get("suspended", False)),
        "front": front[:180],
        "total_min_90": round(review.total_ms_90 / 60000, 1),
        "total_min_30": round(review.total_ms_30 / 60000, 1),
        "recent30Minutes": round(review.total_ms_30 / 60000, 1),
        "reviews_90": review.reviews_90,
        "avg_s": round(review.avg_ms / 1000),
        "avgMs": review.avg_ms,
        "slow_count": review.slow_count,
        "again_count": review.again_count,
        "again_day_count": review.again_day_count,
        "last_times": review.last_times,
        "recent_reviews": review.recent_reviews,
        "recentReviews": review.recent_reviews,
        "reasons": reasons,
        "problems": problems,
        "limitations": limitations,
        "tags": tags,
        "action": action,
    }


def query_count(query: str, data: "AttentionData") -> int:
    return len(data.find_cards(query))


def percentile(values: list[float], fraction: float) -> float:
    """线性插值分位数（与 numpy 默认 ``linear`` 方法一致）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = fraction * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def concentration_stats(avg_ms_values: list[int]) -> dict[str, Any]:
    """注意力集中度：对范围内到期卡片的平均耗时做长尾统计。

    输入是每张卡“最近最多 5 次有效复习”的平均耗时。慢卡按 ≥30 秒判定；
    个人异常线 = Q3 + 1.5 × IQR，随个人数据浮动，不写死阈值。

    ``filters`` 给出每个筛选项自己的数量与占比：结论句、比例条与卡片
    列表必须对同一组卡片成立，占比分母固定为全部已分析卡片（到期、
    未暂停、未被看板忽略、有复习历史）。``scopeNote`` / ``statNotes``
    是展示层预计算的口径说明与“统计说明”弹层内容。
    """
    analyzed = len(avg_ms_values)
    slow_values = [value for value in avg_ms_values if value >= CONCENTRATION_SLOW_MS]
    very_slow_values = [value for value in avg_ms_values if value >= OBVIOUS_SLOW_MS]
    total_ms = sum(avg_ms_values)
    time_share = sum(slow_values) / total_ms if total_ms else 0.0
    card_share = len(slow_values) / analyzed if analyzed else 0.0
    q1 = percentile(avg_ms_values, 0.25)
    q3 = percentile(avg_ms_values, 0.75)
    outlier_line = q3 + 1.5 * (q3 - q1)
    # 个人异常数在 ≥30 秒组内统计，保证筛选数字与列表一致。
    outlier_values = [value for value in slow_values if value >= outlier_line]

    def filter_stats(values: list[int]) -> dict[str, Any]:
        return {
            "count": len(values),
            "cardSharePct": round(len(values) / analyzed * 100, 1) if analyzed else 0.0,
            "timeSharePct": round(sum(values) / total_ms * 100, 1) if total_ms else 0.0,
        }

    c30 = filter_stats(slow_values)
    prominent = (
        analyzed >= LONG_TAIL_MIN_SAMPLES
        and bool(slow_values)
        and time_share >= LONG_TAIL_TIME_SHARE
        and card_share < LONG_TAIL_TIME_SHARE
    )
    p50_s = round(percentile(avg_ms_values, 0.5) / 1000, 1)
    p90_s = round(percentile(avg_ms_values, 0.9) / 1000, 1)
    outlier_s = round(outlier_line / 1000, 1)
    if analyzed:
        scope_note = (
            f"{analyzed} 张到期且有复习历史的卡片 · 按最近 ≤5 次复习的平均耗时估算"
        )
        stat_notes = [
            f"统计范围：当前范围内已到期、未暂停、未被看板忽略、且有复习历史的 {analyzed} 张卡片。",
            f"时间口径：每张卡按最近最多 5 次有效复习的平均耗时估算一次复习的成本；"
            f"“估算复习耗时”是这 {analyzed} 张卡的估算合计，不是全历史累计。",
            f"P50 = {p50_s} 秒：一半卡片的平均耗时不超过 {p50_s} 秒。",
            f"P90 = {p90_s} 秒：九成卡片的平均耗时不超过 {p90_s} 秒。",
            f"个人异常线 = {outlier_s} 秒：由你的分布推算（Q3 + 1.5 × IQR），不是固定阈值；"
            "平均耗时超过它的卡片显著偏慢。",
            "慢卡判定：平均耗时 ≥30 秒计入慢卡，≥60 秒视为明显偏慢。",
        ]
    else:
        scope_note = "当前范围内没有可统计的到期卡片"
        stat_notes = ["当前范围内没有到期且有复习历史的卡片，暂无可统计的分布。"]
    return {
        "cardsAnalyzed": analyzed,
        "slowCount": c30["count"],
        "verySlowCount": len(very_slow_values),
        "outlierCount": len(outlier_values),
        "cardSharePct": c30["cardSharePct"],
        "timeSharePct": c30["timeSharePct"],
        "filters": {
            "c30": c30,
            "c60": filter_stats(very_slow_values),
            "coutlier": filter_stats(outlier_values),
        },
        "p50S": p50_s,
        "p90S": p90_s,
        "q1S": round(q1 / 1000, 1),
        "q3S": round(q3 / 1000, 1),
        "outlierS": outlier_s,
        "outlierMs": outlier_line,
        "prominent": prominent,
        "scopeNote": scope_note,
        "statNotes": stat_notes,
    }


_BS = chr(92)
_Q = chr(34)


def _escape_deck(deck: str) -> str:
    """Escape backslashes and double quotes for an Anki search deck filter."""
    return deck.replace(_BS, _BS + _BS).replace(_Q, _BS + _Q)


def scoped_query(query: str, deck: str) -> str:
    if not deck:
        return query
    return f'{query} deck:"{_escape_deck(deck)}"'


def managed_query(query: str, decks: list[str]) -> str:
    if not decks:
        return query
    clauses = [f'deck:"{_escape_deck(deck)}"' for deck in decks]
    return f'{query} ({" or ".join(clauses)})'


def view_query(query: str, deck: str, managed_decks: list[str]) -> str:
    return scoped_query(query, deck) if deck else managed_query(query, managed_decks)


def union_easy_days(groups: list[dict[str, Any]]) -> list[float]:
    """预算侧的有效复习日：某天任一预设未休息，用户当天仍会复习。

    单一预设时与该预设的日程一致；预设混合时按天取最大百分比——
    父牌组全天复习、子牌组每周休两天时，用户仍每天复习父牌组的卡，
    预算按 7 个复习日计，而子牌组的休息日只折减它自己的模拟成本。
    """
    schedules = [
        [float(p) for p in (group.get("easyDays") or [1.0] * WEEK_DAYS)]
        for group in groups
    ]
    if not schedules:
        return [1.0] * WEEK_DAYS
    return [max(day) for day in zip(*schedules)]


def attention_config(data: "AttentionData") -> dict[str, Any]:
    return data.config() or {}


def native_simulation(
    groups: list[dict[str, Any]],
    days: int,
    deck_size: int,
    new_limit: int,
    data: "AttentionData",
) -> list[dict[str, Any]]:
    """逐预设组调用 FSRS 模拟。

    ``groups`` 来自 :meth:`data.AttentionData.simulation_groups`：每个组
    一个预设、一次模拟，组间搜索互不重叠。``deck_size`` 是模拟接口的
    **新增总量池**（整个预测期共引入多少张，已在 Anki 26.08.1 上实测
    核对），``new_limit`` 是**每日引入上限**。参数在这里显式命名，
    防止调用方把每周数量当成每天数量传进来。
    """
    if not groups:
        raise RuntimeError("尚未设置管理范围。")
    return [
        data.simulate_group(group, deck_size, days, new_limit)
        for group in groups
    ]


def combined_simulation(
    simulations: list[dict[str, Any]],
    days: int,
) -> tuple[list[int], list[float]]:
    counts = [0] * days
    costs = [0.0] * days
    for result in simulations:
        reviews = result.get("dailyReviewCount") or []
        new_cards = result.get("dailyNewCount") or []
        time_cost = result.get("dailyTimeCost") or []
        for index in range(min(days, len(time_cost))):
            counts[index] += int(reviews[index]) + int(new_cards[index])
            costs[index] += float(time_cost[index])
    return counts, costs


def rolling_windows(costs: list[float]) -> list[list[float]]:
    """完整的滚动 7 天窗口。

    只取长度恰为 7 天的窗口：末日尖峰不能凭一段不足 7 天的尾巴单独判超
    预算。当样本不足 7 天时退回整段作为唯一窗口，避免出现“零窗口恒真”。
    每一天都至少被一个完整窗口覆盖（第 i 天落在起点 min(i, len-7) 的窗口）。
    """
    if len(costs) < WEEK_DAYS:
        return [list(costs)]
    return [
        costs[start : start + WEEK_DAYS]
        for start in range(len(costs) - WEEK_DAYS + 1)
    ]


def within_daily_budget(costs: list[float], daily_budget_seconds: int) -> bool:
    if not costs:
        return False
    return all(
        sum(window) / len(window) <= daily_budget_seconds
        for window in rolling_windows(costs)
    )


def merged_worst_costs(
    baseline: list[dict[str, Any]],
    baseline_costs: list[float],
    days: int,
    simulations: list[dict[str, Any]],
) -> list[float]:
    """把各预设组的模拟结果按“逐日最差增量”合并到基线成本（秒）。

    每个预设组按完整新增节奏单独模拟，再取相对基线的增量、逐日取
    最差合并：等价于把全部新增集中在最贵的预设组（新增分配策略），
    避免不同组的峰谷互相抵消，也不会按组数重复累计（不求和）。
    """
    baseline_by_deck = [combined_simulation([result], days)[1] for result in baseline]
    worst_costs = baseline_costs.copy()
    for index, result in enumerate(simulations):
        _, candidate_costs = combined_simulation([result], days)
        for day in range(days):
            delta = max(0.0, candidate_costs[day] - baseline_by_deck[index][day])
            worst_costs[day] = max(worst_costs[day], baseline_costs[day] + delta)
    return worst_costs


def worst_window_average(costs: list[float]) -> float:
    """最重的完整滚动 7 天窗口的日均成本（秒）。"""
    if not costs:
        return 0.0
    return max(sum(window) / len(window) for window in rolling_windows(costs))


def sustained_weekly_new(
    baseline: list[dict[str, Any]],
    simulations: list[dict[str, Any]],
    days: int,
) -> int:
    """由模拟输出折算每周新增量（与成本合并同一新增分配策略）。

    成本合并（:func:`merged_worst_costs`）等价于“全部新增集中在最贵的
    预设组”，展示数量必须同源：取**最贵组**（相对基线的总增量最大）的
    实际引入量——不取第一组（组按名称排序，牌组改名不能改变结果），
    也不按组求和（那会超出成本口径覆盖的量）。并列时取引入量更大的
    一组，保证结果与组顺序无关。``deck_size`` 只是总量池；模拟实际
    引入多少（``dailyNewCount``）才是真实节奏，按 ``总引入量 × 7 ÷
    预测天数`` 折算成每周量。
    """
    if not simulations:
        return 0
    ranked: list[tuple[float, int]] = []
    for index, result in enumerate(simulations):
        base_costs = combined_simulation([baseline[index]], days)[1]
        costs = combined_simulation([result], days)[1]
        delta = sum(
            max(0.0, costs[day] - base_costs[day]) for day in range(days)
        )
        introduced = sum(int(count) for count in (result.get("dailyNewCount") or []))
        ranked.append((delta, introduced))
    _, introduced = max(ranked)
    return round(introduced * WEEK_DAYS / days)


def new_card_capacity(
    groups: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    days: int,
    daily_budget_seconds: float,
    data: "AttentionData",
) -> dict[str, Any]:
    """预算内可持续的新增空间（每天引入、折算每周展示）。

    口径：候选节奏 ``pace`` 按 ``deck_size = pace × days``、``new_limit =
    pace`` 模拟“每天引入 pace 张、持续整个预测期”；90 天观察期与完整
    滚动 7 天预算检查沿用健康分级口径。搜索用指数探界 + 二分，且设计算
    保护上限：触及上限时 ``capped=True``，展示不得把上限冒充精确容量。

    每日节奏是整数，表达不了“每周不足 7 张”；此时用 pace=1 的边际
    成本线性估算周余量（``estimated=True``），不把“每天一张放不下”
    说成“没有新增余量”。预算是“预测期内的估算”，不保证永久可持续
    （展示层负责说明）。
    """
    _, baseline_costs = combined_simulation(baseline, days)
    if not baseline_costs:
        raise RuntimeError("FSRS 模拟没有返回逐日成本。")

    if not within_daily_budget(baseline_costs, daily_budget_seconds):
        worst = worst_window_average(baseline_costs)
        return {
            "state": "overloaded",
            "dailyPace": None,
            "weeklyNew": None,
            "capped": False,
            "estimated": False,
            "excessPerDayMinutes": round(
                max(0.0, worst - daily_budget_seconds) / 60, 1
            ),
            "worstWindowDailyMinutes": round(worst / 60, 1),
        }

    simulation_cache: dict[int, list[dict[str, Any]]] = {}
    cost_cache: dict[int, list[float]] = {}

    def costs_for(pace: int) -> list[float]:
        if pace not in simulation_cache:
            simulation_cache[pace] = native_simulation(
                groups, days, pace * days, pace, data
            )
        if pace not in cost_cache:
            cost_cache[pace] = merged_worst_costs(
                baseline, baseline_costs, days, simulation_cache[pace]
            )
        return cost_cache[pace]

    def fits(pace: int) -> bool:
        return within_daily_budget(costs_for(pace), daily_budget_seconds)

    # 指数探界：典型答案不大，先试小节奏可以避免一上来就模拟巨大的牌池。
    low, fail, step = 0, None, 1
    while step <= NEW_PACE_SEARCH_MAX and fail is None:
        if fits(step):
            low = step
            step *= 2
        else:
            fail = step
    capped = False
    if fail is None:
        # 2 的幂一路通过，只有保护上限本身还没验证过。
        if fits(NEW_PACE_SEARCH_MAX):
            pace, capped = NEW_PACE_SEARCH_MAX, True
        else:
            fail = NEW_PACE_SEARCH_MAX
    if fail is not None:
        high = fail
        while low + 1 < high:
            middle = (low + high) // 2
            if fits(middle):
                low = middle
            else:
                high = middle
        pace = low
    if pace:
        return {
            "state": "healthy",
            "dailyPace": pace,
            "weeklyNew": sustained_weekly_new(baseline, simulation_cache[pace], days),
            "capped": capped,
            "estimated": False,
            "excessPerDayMinutes": 0,
            "worstWindowDailyMinutes": round(
                worst_window_average(costs_for(pace)) / 60, 1
            ),
        }
    # pace=0：每天 1 张放不下。模拟接口表达不了更细的周节奏（new_limit
    # 是每日整数），按“每张日节奏卡的边际窗口成本”线性折算周余量：
    # 每周 w 张 ≫ 每天引入密度 w/7，稳态窗口均值近似线性。
    baseline_worst = worst_window_average(baseline_costs)
    headroom = daily_budget_seconds - baseline_worst
    marginal = worst_window_average(costs_for(1)) - baseline_worst
    if marginal > 0 and headroom > 0:
        weekly = min(WEEK_DAYS - 1, int(WEEK_DAYS * headroom // marginal))
    else:
        weekly = 0
    return {
        "state": "healthy",
        "dailyPace": 0,
        "weeklyNew": weekly,
        "capped": False,
        "estimated": weekly > 0,
        "excessPerDayMinutes": 0,
        "worstWindowDailyMinutes": round(
            (baseline_worst + weekly / WEEK_DAYS * max(0.0, marginal)) / 60, 1
        ),
    }


def new_space_rows(health: dict[str, Any], short_reason: str) -> dict[str, str]:
    """右栏“新增空间”一行的展示值与辅助文案（展示层预计算）。

    超预算或没有余量都显示“暂无预算余量”——这只是预算判断，不是禁止
    学习新卡；有余量按模拟折算的每周量展示；模拟失败给出简短原因。
    """
    if health["state"] == "unknown":
        return {
            "newSpaceValue": "暂无法估算",
            "newSpaceNote": short_reason or "FSRS 模拟不可用",
        }
    if not health.get("weeklyNew"):
        return {
            "newSpaceValue": "暂无预算余量",
            "newSpaceNote": "仍可学习新卡，但会增加待复习负担。",
        }
    note = "按每周持续新增估算。"
    if health.get("capped"):
        note = "已达估算搜索上限，实际余量可能更高。"
    return {
        "newSpaceValue": f"约 {health['weeklyNew']} 张／周",
        "newSpaceNote": note,
    }


def short_simulation_reason(error: str) -> str:
    """模拟失败时的简短原因（右栏一行放不下完整错误）。"""
    if not error:
        return ""
    if "no cards to simulate" in error:
        return "范围内没有可模拟的卡片"
    if "尚未设置管理范围" in error:
        return "尚未设置管理范围"
    return "FSRS 模拟不可用"


def hero_forecast_note(forecast: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    """首页“预测说明”弹层：90 天模拟、最重的 7 天、日均超出等细节。

    主区域只保留 7 天口径的结论与数字；长周期的依据全部收进这里，
    避免正文里混用观察期。新增空间的估算期限与保护上限也在这里说明。
    """
    lines: list[str] = []
    daily = forecast["daily_budget_minutes"]
    budget_note = f"每天 {daily} 分钟" + (
        f" × {forecast['active_days']} 个复习日"
        if forecast["active_days"] < WEEK_DAYS
        else ""
    )
    weekly = forecast["weekly_budget_minutes"]
    if forecast["source"] == "fsrs_simulator":
        days = forecast["simulation_days"]
        worst = forecast.get("worst_window_daily_minutes")
        average_budget = forecast["daily_budget_average_minutes"]
        lines.append(f"来源：Anki FSRS 未来 {days} 天模拟。")
        lines.append(
            f"未来 7 天按现有卡片预计复习 {forecast['forecast_minutes']} 分钟（未计新增），"
            f"可用预算 {weekly} 分钟（{budget_note}）。"
        )
        if health["state"] == "overloaded":
            line = f"即使不新增卡片，未来 {days} 天的模拟负担也超出预算，系统处于恢复中。"
            if worst is not None:
                excess = health.get("excessPerDayMinutes")
                line += f"最重的连续 7 天平均每天 {worst} 分钟"
                if excess:
                    line += f"（日均预算 {average_budget} 分钟，超 {excess} 分钟）。"
                else:
                    line += f"，仍在日均预算 {average_budget} 分钟内。"
                line += "治理高耗时卡片可以缩短恢复时间。"
            lines.append(line)
        elif worst is not None:
            weekly_new = health.get("weeklyNew")
            if weekly_new:
                line = (
                    f"按约 {weekly_new} 张／周持续新增，最重的连续 7 天平均每天 {worst} 分钟，"
                    f"仍在日均预算 {average_budget} 分钟内。"
                )
                line += f"这是未来 {days} 天预测期内的估算，不保证永久可持续。"
                if health.get("estimated"):
                    line += "每周不足 7 张时，余量按边际成本线性估算。"
                if health.get("capped"):
                    line += "估算已达搜索上限，实际余量可能更高。"
            else:
                line = "现有复习虽在预算内，但加入每天 1 张的新卡即会超出，预算内没有新增余量。"
            lines.append(line)
    else:
        lines.append("来源：FSRS 模拟不可用，已按排程估算（未来 7 天到期数 × 近 30 天平均耗时）。")
        if forecast.get("error"):
            lines.append(f"降级原因：{forecast['error']}")
        lines.append(
            f"未来 7 天预计复习 {forecast['forecast_minutes']} 分钟，"
            f"可用预算 {weekly} 分钟（{budget_note}）。"
        )
    return {"lines": lines}


def hero_payload(
    concentration: dict[str, Any],
    health: dict[str, Any],
    forecast: dict[str, Any],
    due_count: int,
    today_reviews: int,
    today_total_ms: int,
) -> dict[str, Any]:
    """首页 Hero 左侧：统一以“未来 7 天预测 vs 预算”开场。

    主区域只有三行：结论、预测数字、一句行动建议；健康/长尾状态收进
    标题旁的小标签，90 天模拟等细节进“预测说明”。长尾突出优先于超预
    算表达；“今日完成”仍按当天口径（原始毫秒比较，展示才舍入）。
    不声称替用户安排新卡或休息日——新增节奏由 Anki 原生排程决定。
    """
    headroom = forecast["headroom_minutes"]
    if headroom < 0:
        headline = f"未来 7 天，预计超出预算 {abs(headroom)} 分钟"
    elif headroom == 0:
        headline = "未来 7 天，预计刚好用满预算"
    else:
        headline = f"未来 7 天，预计余 {headroom} 分钟"
    numbers_line = (
        f"预计复习 {forecast['forecast_minutes']} 分钟 · "
        f"可用预算 {forecast['weekly_budget_minutes']} 分钟"
    )
    daily = forecast["daily_budget_minutes"]
    has_slow_cards = concentration["slowCount"] > 0
    base = {
        "stateTag": health["statusLine"],
        "headline": headline,
        "numbersLine": numbers_line,
        "ctaLabel": "查看高耗时卡片" if has_slow_cards else "检查候选",
        "ctaTarget": "concentration" if has_slow_cards else "cards",
        "bars": None,
        "forecastNote": hero_forecast_note(forecast, health),
    }

    if concentration["prominent"]:
        detail = (
            f"{concentration['slowCount']} 张卡占到期卡片的 {concentration['cardSharePct']}%，"
            f"却占估算复习耗时的 {concentration['timeSharePct']}%。先治理最贵的卡。"
        )
        return {
            **base,
            "mode": "longtail",
            "detail": detail,
            "bars": {
                "cardSharePct": concentration["cardSharePct"],
                "timeSharePct": concentration["timeSharePct"],
            },
        }

    if health["state"] == "overloaded":
        return {
            **base,
            "mode": "overloaded",
            "detail": f"每天仍按你的 {daily} 分钟复习。先检查高耗时卡片，寻找减负空间。",
        }

    if due_count == 0 or today_total_ms >= daily * 60_000:
        today_minutes = round(today_total_ms / 60000, 1)
        if due_count == 0 and not today_reviews:
            body = {
                "headline": "今天没有到期卡片",
                "numbersLine": None,
                "detail": "当前范围内没有需要复习的卡片。",
            }
        else:
            body = {
                "headline": "今天的复习预算已经完成",
                "numbersLine": f"已复习 {today_minutes} 分钟 · 预算 {daily} 分钟",
                "detail": (
                    f"剩余 {due_count} 张到期卡片可以留到明天。"
                    if due_count
                    else "当前没有到期卡片。"
                ),
            }
        return {
            **base,
            "mode": "todaydone",
            "stateTag": "今日已完成",
            "headline": body["headline"],
            "numbersLine": body["numbersLine"],
            "detail": body["detail"],
            "ctaLabel": "检查候选",
            "ctaTarget": "cards",
        }

    if health["state"] == "healthy":
        weekly_new = health.get("weeklyNew")
        if weekly_new:
            detail = f"现有复习在预算内，预计还可新增约 {weekly_new} 张／周。"
        else:
            detail = "现有复习在预算内，但暂无新增余量。"
    else:
        detail = "FSRS 模拟暂不可用，以上按排程估算。"
    return {**base, "mode": health["state"], "detail": detail}


def build_dashboard(
    deck: str = "",
    requested_daily_budget_minutes: int | None = None,
    data: "AttentionData | None" = None,
) -> dict[str, Any]:
    if data is None:
        raise RuntimeError(
            "数据通道未初始化：请从 Anki 工具菜单或工具栏启动 Attention Is All You Need。"
        )
    started = time.time()
    now_ms = int(time.time() * 1000)
    config = attention_config(data)
    managed_decks = [str(name) for name in config.get("managedDecks") or []]
    # 健康分级与新增节奏固定按 ≥90 天的 FSRS 模拟判断；观察期夹在
    # [90, 730]：配置坏了回退默认，过大的值收敛到 730 天，避免 protobuf
    # 溢出导致整块看板不可用。
    simulation_days = min(
        max(
            _config_int(config, "simulationDays", HEALTH_SIMULATION_DAYS),
            HEALTH_SIMULATION_DAYS,
        ),
        SIMULATION_DAYS_MAX,
    )
    configured_budget_minutes = _config_int(
        config, "dailyBudgetMinutes", TARGET_DAILY_MINUTES
    )
    daily_budget_minutes = (
        requested_daily_budget_minutes
        if requested_daily_budget_minutes is not None
        else configured_budget_minutes
    )
    if not 1 <= daily_budget_minutes <= 1440:
        raise RuntimeError("每日预算须为 1-1440 分钟。")
    deck_names = sorted(data.deck_names(), key=str.casefold)
    if deck and deck not in deck_names:
        raise RuntimeError(f"牌组不存在：{deck}")

    # A selected deck defines the entire dashboard scope, including capacity.
    if deck:
        managed_decks = [deck]
    # 模拟按预设分组：父牌组的搜索覆盖子树卡片，但每张卡的复习参数
    # 取决于它所属牌组的预设；子牌组换过预设时必须分别参与模拟。
    simulation_groups = data.simulation_groups(deck, managed_decks)
    easy_days = union_easy_days(simulation_groups)
    active_days = sum(1 for p in easy_days if p > 0)

    burden_ids = data.find_cards(view_query("-is:suspended", deck, managed_decks))
    candidate_source_ids = data.find_cards(
        view_query(
            "-is:suspended -tag:看板忽略 -tag:需优化卡片",
            deck,
            managed_decks,
        )
    )
    optimization_ids = data.find_cards(
        view_query("tag:需优化卡片", deck, managed_decks)
    )
    # 注意力集中度：范围服从当前选中牌组；只统计到期、未暂停、未被隐藏且
    # 有复习历史的卡片，因此暂停/隐藏/删除后刷新即退出该集合。
    concentration_ids = data.find_cards(
        view_query("is:due -is:suspended -tag:看板忽略", deck, managed_decks)
    )
    all_needed_ids = sorted(
        set(burden_ids)
        | set(candidate_source_ids)
        | set(optimization_ids)
        | set(concentration_ids)
    )
    infos = data.card_infos(all_needed_ids)
    reviews = data.reviews_of_cards(all_needed_ids)
    burden_reviews = {cid: reviews.get(cid, []) for cid in burden_ids}
    capacity_reviews = burden_reviews
    if deck or managed_decks:
        capacity_ids = data.find_cards(managed_query("-is:suspended", managed_decks))
        capacity_reviews = data.reviews_of_cards(capacity_ids)
    summaries_by_id = {
        int(cid): summarize_reviews(int(cid), reviews.get(int(cid), []), now_ms)
        or empty_review(int(cid))
        for cid in all_needed_ids
    }
    concentration_summaries = [
        summaries_by_id[int(cid)]
        for cid in concentration_ids
        if summaries_by_id[int(cid)].recent_reviews
    ]
    concentration = concentration_stats(
        [summary.avg_ms for summary in concentration_summaries]
    )
    concentration_queue = sorted(
        (
            make_card_row(summary, infos.get(summary.card_id))
            for summary in concentration_summaries
            if summary.avg_ms >= CONCENTRATION_SLOW_MS
        ),
        key=lambda row: (-row["avgMs"], row["cardId"]),
    )
    concentration_payload = dict(concentration)
    concentration_payload["queue"] = concentration_queue
    candidate_rows = [
        make_card_row(summaries_by_id[int(cid)], infos.get(int(cid)))
        for cid in candidate_source_ids
    ]
    review_queue = sorted(
        [row for row in candidate_rows if row["reasons"]],
        key=lambda row: (
            row["total_min_30"],
            row["slow_count"],
            row["again_count"],
            row["again_day_count"],
            len(row["problems"]),
        ),
        reverse=True,
    )
    optimization_queue = [
        make_card_row(summaries_by_id[int(cid)], infos.get(int(cid)))
        for cid in optimization_ids
    ]
    summaries = [summaries_by_id[int(cid)] for cid in burden_ids]

    today_reviews = [
        r
        for revs in burden_reviews.values()
        for r in revs
        if r.get("ease", 0) > 0
        and r.get("time", 0) > 0
        and datetime.fromtimestamp(int(r["id"]) / 1000).date() == datetime.now().date()
    ]
    today_total_ms = sum(int(r["time"]) for r in today_reviews)
    today_avg_ms = round(today_total_ms / len(today_reviews)) if today_reviews else 0
    today_slow = sum(1 for r in today_reviews if int(r["time"]) >= SLOW_MS)
    today_again = sum(1 for r in today_reviews if int(r["ease"]) == 1)
    over_budget = max(0, round(today_total_ms / 60000 - daily_budget_minutes, 1))

    start_30 = now_ms - 30 * 24 * 3600 * 1000
    recent_30_reviews = [
        review
        for card_reviews in reviews.values()
        for review in card_reviews
        if review.get("ease", 0) > 0
        and review.get("time", 0) > 0
        and int(review.get("id", 0)) >= start_30
    ]
    scope_recent_avg_ms = (
        round(sum(int(r["time"]) for r in recent_30_reviews) / len(recent_30_reviews))
        if recent_30_reviews
        else max(today_avg_ms, 45_000)
    )
    capacity_recent_30 = [
        review
        for card_reviews in capacity_reviews.values()
        for review in card_reviews
        if review.get("ease", 0) > 0
        and review.get("time", 0) > 0
        and int(review.get("id", 0)) >= start_30
    ]
    capacity_avg_ms = (
        round(sum(int(r["time"]) for r in capacity_recent_30) / len(capacity_recent_30))
        if capacity_recent_30
        else 45_000
    )

    top_attention = sorted(summaries, key=lambda s: s.total_ms_90, reverse=True)[:20]
    top_slow = sorted(summaries, key=lambda s: (s.slow_count, s.total_ms_90), reverse=True)[:20]
    top_again_slow = sorted(summaries, key=lambda s: (s.again_count, s.slow_count, s.total_ms_90), reverse=True)[:20]
    needed_ids = sorted({s.card_id for s in top_attention + top_slow + top_again_slow})
    infos.update(data.card_infos([cid for cid in needed_ids if cid not in infos]))

    rows_attention = [make_card_row(s, infos.get(s.card_id)) for s in top_attention]
    rows_slow = [make_card_row(s, infos.get(s.card_id)) for s in top_slow if s.slow_count > 0]
    rows_again = [make_card_row(s, infos.get(s.card_id)) for s in top_again_slow if s.again_count > 0]
    quality_by_id = {
        row["card_id"]: row
        for row in rows_attention + rows_slow + rows_again
        if row["problems"]
    }
    quality_cards = sorted(
        quality_by_id.values(),
        key=lambda row: (len(row["problems"]), row["total_min_90"]),
        reverse=True,
    )

    due_count = query_count(view_query("is:due -is:suspended", deck, managed_decks), data)
    new_today = query_count(view_query("introduced:1", deck, managed_decks), data)
    new_7 = query_count(view_query("introduced:7", deck, managed_decks), data)
    suspended = query_count(view_query("is:suspended", deck, managed_decks), data)
    needs_optimization = query_count(view_query("tag:需优化卡片", deck, managed_decks), data)
    scope_forecast_counts = [
        query_count(view_query("is:due -is:new -is:suspended", deck, managed_decks), data),
        *[
            query_count(
                view_query(
                    f"is:review prop:due={day} -is:suspended",
                    deck,
                    managed_decks,
                ),
                data,
            )
            for day in range(1, WEEK_DAYS)
        ],
    ]
    forecast_counts = [
        query_count(managed_query("is:due -is:new -is:suspended", managed_decks), data),
        *[
            query_count(
                managed_query(
                    f"is:review prop:due={day} -is:suspended",
                    managed_decks,
                ),
                data,
            )
            for day in range(1, WEEK_DAYS)
        ],
    ]
    forecast_reviews = sum(forecast_counts)
    forecast_ms = forecast_reviews * capacity_avg_ms
    scope_forecast_reviews = sum(scope_forecast_counts)
    scope_forecast_ms = scope_forecast_reviews * scope_recent_avg_ms
    weekly_budget_ms = active_days * daily_budget_minutes * 60_000
    headroom_ms = weekly_budget_ms - forecast_ms
    # 新卡漏斗：生成与激活分离。added:7 是本周生成/导入，introduced:7 是已
    # 开始学习；仍在等待的卡片保持 is:new——不暂停、不打标签、不写额外数据。
    generated_7 = query_count(view_query("added:7", deck, managed_decks), data)
    waiting_7 = query_count(view_query("is:new added:7", deck, managed_decks), data)
    capacity_source = "schedule_estimate"
    simulation_error = ""
    health = {
        "state": "unknown",
        "dailyPace": None,
        "weeklyNew": None,
        "capped": False,
        "estimated": False,
        "excessPerDayMinutes": None,
        "worstWindowDailyMinutes": None,
    }
    try:
        baseline_simulations = native_simulation(
            simulation_groups,
            simulation_days,
            0,
            0,
            data,
        )
        native_counts, native_costs = combined_simulation(
            baseline_simulations,
            simulation_days,
        )
        health = new_card_capacity(
            simulation_groups,
            baseline_simulations,
            simulation_days,
            daily_budget_minutes * 60 * active_days / WEEK_DAYS,
            data,
        )
        forecast_counts = native_counts[:WEEK_DAYS]
        forecast_reviews = sum(forecast_counts)
        forecast_seconds = sum(native_costs[:WEEK_DAYS])
        forecast_ms = round(forecast_seconds * 1000)
        headroom_ms = weekly_budget_ms - forecast_ms
        capacity_source = "fsrs_simulator"
    except RuntimeError as exc:
        simulation_error = str(exc)

    # Pre-computed, display-ready values so JS only renders, never re-derives
    # business rules (tone thresholds, the hero judgment, the weekly plan).
    today_avg_s = round(today_avg_ms / 1000) if today_avg_ms else 0
    today_again_rate = round(today_again / max(len(today_reviews), 1) * 100, 1)
    signals = [
        {
            "label": "单卡均时",
            "value": today_avg_s,
            "unit": "秒",
            "tone": (
                "tone-red" if today_avg_s >= 60
                else "tone-amber" if today_avg_s >= 40
                else "tone-green"
            ),
            "status": "检索偏重" if today_avg_s >= 50 else "可控",
        },
        {
            "label": "40 秒以上",
            "value": today_slow,
            "unit": "次",
            "tone": "tone-amber" if today_slow else "tone-green",
            "status": "需要审计" if today_slow else "无异常",
        },
        {
            "label": "Again 率",
            "value": today_again_rate,
            "unit": "%",
            "tone": (
                "tone-red" if today_again_rate >= 25
                else "tone-amber" if today_again_rate >= 15
                else "tone-green"
            ),
            "status": "提取不稳" if today_again_rate >= 15 else "稳定",
        },
        {
            "label": "当前到期",
            "value": due_count,
            "unit": "张",
            "tone": "tone-red" if due_count > 100 else "tone-green",
            "status": "队列积压" if due_count > 100 else "可控",
        },
    ]

    if not today_reviews:
        for signal in signals[:3]:
            signal.update(value="—", tone="tone-muted", status="暂无复习数据")

    headroom_minutes = round(headroom_ms / 60000)
    simulator_ready = capacity_source == "fsrs_simulator"
    state_labels = {"healthy": "健康", "overloaded": "恢复中"}
    state_label = state_labels.get(health["state"], "待评估")
    if concentration["prominent"]:
        tail_label = "长尾偏重"
    elif concentration["slowCount"]:
        tail_label = "有优化空间"
    else:
        tail_label = "长尾平稳"
    health_payload = {
        "state": health["state"],
        "stateLabel": state_label,
        "tailLabel": tail_label,
        "statusLine": f"{state_label} · {tail_label}",
        "weeklyNew": health["weeklyNew"],
        "dailyPace": health["dailyPace"],
        "capped": health["capped"],
        "estimated": health["estimated"],
        "excessPerDayMinutes": health["excessPerDayMinutes"],
        "worstWindowDailyMinutes": health["worstWindowDailyMinutes"],
        "simulatorReady": simulator_ready,
        **new_space_rows(health, short_simulation_reason(simulation_error)),
    }
    budget_note = f"每天 {daily_budget_minutes} 分钟" + (
        f" · {active_days} 个复习日" if active_days < WEEK_DAYS else ""
    )
    daily_budget_average_minutes = round(
        daily_budget_minutes * active_days / WEEK_DAYS, 1
    )
    forecast_context = {
        "forecast_minutes": round(forecast_ms / 60000),
        "weekly_budget_minutes": round(weekly_budget_ms / 60000),
        "headroom_minutes": headroom_minutes,
        "daily_budget_minutes": daily_budget_minutes,
        "active_days": active_days,
        "simulation_days": simulation_days,
        "daily_budget_average_minutes": daily_budget_average_minutes,
        "worst_window_daily_minutes": health.get("worstWindowDailyMinutes"),
        "source": capacity_source,
        "error": simulation_error,
    }
    hero = hero_payload(
        concentration,
        health_payload,
        forecast_context,
        due_count,
        len(today_reviews),
        today_total_ms,
    )

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "runtime_s": round(time.time() - started, 1),
        "selected_deck": deck,
        "decks": deck_names,
        "title": "Attention Is All You Need",
        "subtitle": "人的注意力有限，能制造的 Anki 卡片近乎无限；两者的矛盾，就是 Anki 系统的核心风险。",
        "signals": signals,
        "today": {
            "reviews": len(today_reviews),
            "minutes": round(today_total_ms / 60000, 1),
            "avg_s": today_avg_s,
            "slow_count": today_slow,
            "again_rate": today_again_rate,
            "over_budget_min": over_budget,
        },
        "balance": {
            "due": due_count,
            "new_today": new_today,
            "new_7": new_7,
            "generated_7": generated_7,
            "waiting_7": waiting_7,
            "suspended": suspended,
            "needs_optimization": needs_optimization,
            "budget_minutes": daily_budget_minutes,
        },
        "capacity": {
            "forecast_counts": forecast_counts,
            "forecast_reviews": forecast_reviews,
            "forecast_minutes": round(forecast_ms / 60000),
            "scope_forecast_reviews": scope_forecast_reviews,
            "scope_forecast_minutes": round(scope_forecast_ms / 60000),
            "weekly_budget_minutes": round(weekly_budget_ms / 60000),
            "headroom_minutes": headroom_minutes,
            "budgetNote": budget_note,
            "dailyBudgetAverageMinutes": daily_budget_average_minutes,
            "worstWindowDailyMinutes": health.get("worstWindowDailyMinutes"),
            "recent_avg_s": round(capacity_avg_ms / 1000),
            "managed_decks": managed_decks,
            "source": capacity_source,
            "simulation_days": simulation_days,
            "simulation_error": simulation_error,
            "active_days": active_days,
            "easy_days": easy_days,
            "health": health_payload,
        },
        "hero": hero,
        "concentration": concentration_payload,
        "attention_holes": rows_attention,
        "quality_cards": quality_cards[:20],
        "slow_cards": rows_slow,
        "again_slow": rows_again,
        "review_queue": review_queue,
        "optimization_queue": optimization_queue,
        "candidate_counts": {
            "reviewTotal": len(review_queue),
            "optimizationTotal": len(optimization_queue),
            "displayed": len(review_queue),
            "overviewDisplayed": min(len(review_queue), 6),
        },
    }
