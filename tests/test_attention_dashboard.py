from __future__ import annotations

import importlib.util
import time
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[1] / "addon" / "attention_dashboard.py"
SPEC = importlib.util.spec_from_file_location("attention_dashboard", MODULE_PATH)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)


DAY_MS = 24 * 3600 * 1000


def make_summary(card_id: int = 1, avg_ms: int = 10_000) -> dashboard.CardReview:
    return dashboard.CardReview(
        card_id=card_id,
        total_ms_90=600_000,
        total_ms_30=200_000,
        reviews_90=10,
        slow_count=1,
        again_count=0,
        avg_ms=avg_ms,
        last_times=[30, 25],
    )


def make_info(
    card_id: int = 1,
    front: str = "题干",
    back: str = "答案",
    *,
    image: bool = False,
    tags: list[str] | None = None,
    deck: str = "A",
    note_type: str = "Basic",
) -> dict:
    back_value = f'<img src="x.png">{back}' if image else back
    return {
        "cardId": card_id,
        "fields": {
            "Front": {"value": front, "order": 0},
            "Back": {"value": back_value, "order": 1},
        },
        "deckName": deck,
        "noteType": note_type,
        "tags": list(tags or []),
    }


def make_due_reviewed_cards(fake, due_ids, seconds_by_id, fast_seconds=10):
    """把一组卡片布置为“到期、未暂停、有复习历史”，平均耗时由秒数决定。"""
    now = int(time.time() * 1000)
    for cid in due_ids:
        seconds = seconds_by_id.get(int(cid), fast_seconds)
        fake.cards[int(cid)] = make_info(int(cid), front=f"到期卡 {cid}", back="答")
        fake.reviews[int(cid)] = [
            {"id": now - index * 1000, "ease": 3, "time": seconds * 1000}
            for index in range(3)
        ]
    fake.queries['is:due -is:suspended -tag:看板忽略 (deck:"A")'] = list(due_ids)
    fake.queries['is:due -is:suspended (deck:"A")'] = list(due_ids)


class FakeData:
    """固定场景数据源：牌组 A；卡 1 是注意力黑洞，卡 2 题干过长，FSRS 基线超预算。

    实现 AttentionData 协议，供 build_dashboard 注入；与真实 AnkiData 同构。"""

    def __init__(self) -> None:
        now = int(time.time() * 1000)
        self.simulate_calls: list[dict] = []
        self.queries_seen: list[str] = []
        self._easy_days: dict[str, list[float]] = {}
        self._deck_names = ["A"]
        # 显式预设组（不同预设的父子牌组场景）；None 时用默认单组。
        self._groups: list[dict] | None = None
        # 按组配置成本：键是组的牌组元组，值是 (new_limit, days) -> 标量或逐日列表。
        self._group_costs = {}
        self.cards = {
            1: make_info(1, front="题干一", back="答案一", tags=["tagA"]),
            2: make_info(2, front="题" * 200, back="答"),
        }
        self.reviews = {
            1: [
                {"id": now, "ease": 3, "time": 60_000},
                {"id": now - 40 * DAY_MS, "ease": 1, "time": 30_000},
                {"id": now - 40 * DAY_MS - 1_000, "ease": 3, "time": 31_000},
            ],
            2: [{"id": now, "ease": 3, "time": 4_000}],
        }
        self.queries = {
            '-is:suspended (deck:"A")': [1, 2],
            '-is:suspended -tag:看板忽略 -tag:需优化卡片 (deck:"A")': [1, 2],
            'is:due -is:suspended (deck:"A")': list(range(10)),
            'is:due -is:suspended -tag:看板忽略 (deck:"A")': [1, 2],
            'added:1 -is:suspended (deck:"A")': [101, 102],
            'added:7 -is:suspended (deck:"A")': [101, 102, 103, 104],
            'added:7 (deck:"A")': [101, 102, 103, 104, 105, 106],
            'is:new added:7 (deck:"A")': [103, 104],
            'introduced:1 (deck:"A")': [101, 102],
            'introduced:7 (deck:"A")': [101, 102, 103, 104],
            'is:suspended (deck:"A")': [999],
            'tag:需优化卡片 (deck:"A")': [],
            'is:due -is:new -is:suspended (deck:"A")': list(range(10)),
            'is:review prop:due=1 -is:suspended (deck:"A")': list(range(5)),
        }
        self._config = {
            "managedDecks": ["A"],
            "simulationDays": 90,
            "dailyBudgetMinutes": 30,
        }

    def deck_names(self) -> list:
        return list(self._deck_names)

    def find_cards(self, query: str) -> list:
        self.queries_seen.append(query)
        return list(self.queries.get(query, []))

    def card_infos(self, card_ids) -> dict:
        return {
            int(cid): self.cards[int(cid)]
            for cid in card_ids
            if int(cid) in self.cards
        }

    def reviews_of_cards(self, card_ids) -> dict:
        return {int(cid): self.reviews.get(int(cid), []) for cid in card_ids}

    def simulation_groups(self, deck, managed_decks) -> list:
        if self._groups is not None:
            return self._groups
        # 默认场景：范围内全部牌组共享一个预设；有效复习日取范围内第一
        # 个设置了休息日的牌组日程（与旧“代表日程”行为一致的简化）。
        if deck:
            scope = [deck] + [n for n in self._deck_names if n.startswith(deck + "::")]
        elif managed_decks:
            scope = []
            for root in managed_decks:
                if root not in self._deck_names:
                    continue
                scope.append(root)
                scope.extend(n for n in self._deck_names if n.startswith(root + "::"))
            scope = list(dict.fromkeys(scope))
        else:
            scope = list(self._deck_names)
        schedule = [1.0] * 7
        for name in scope:
            pct = self._easy_days.get(name)
            if pct and any(p < 1 for p in pct):
                schedule = list(pct)
                break
        return [{"decks": sorted(scope), "config": {}, "easyDays": schedule}]

    def simulate_group(self, group, additional_new, days, new_limit) -> dict:
        self.simulate_calls.append(
            {
                "decks": list(group["decks"]),
                "additionalNew": additional_new,
                "days": days,
                "newLimit": new_limit,
            }
        )
        maker = self._group_costs.get(tuple(group["decks"]))
        if maker is None:
            cost: list[float] = [100_000.0] * days
        else:
            value = maker(new_limit, days)
            cost = (
                [float(value)] * days
                if isinstance(value, (int, float))
                else [float(item) for item in value]
            )
        return {
            "dailyReviewCount": [5] * days,
            "dailyNewCount": [int(new_limit)] * days,
            "dailyTimeCost": cost,
        }

    def config(self) -> dict:
        return dict(self._config)


class AttentionDashboardTest(unittest.TestCase):
    """纯函数行为特征测试。"""

    def test_detect_quality_flags_structural_risks(self) -> None:
        problems = dashboard.detect_quality(
            "请分别回答：为什么？如何处理？",
            "1. 第一项\n2. 第二项\n3. 第三项\n4. 第四项\n5. 第五项\n6. 第六项",
            False,
        )

        self.assertIn("疑似列表卡", problems)
        self.assertIn("多问合一", problems)
        self.assertIn("答案多行", problems)

    def test_standard_cloze_and_custom_fields_remain_identifiable(self):
        for fields in ({'Text': {'value': '长题面' * 80}, 'Back Extra': {'value': '补充'}},
                       {'自定义问题': {'value': '长题面' * 80, 'order': 0}, '解释': {'value': '补充', 'order': 1}}):
            row = dashboard.make_card_row(dashboard.empty_review(1), {'fields': fields})
            self.assertTrue(row['summary'].startswith('长题面'))
            self.assertIn('题干过长', row['problems'])

    def test_daily_budget_checks_every_rolling_week(self) -> None:
        self.assertTrue(dashboard.within_daily_budget([20 * 60] * 14, 30 * 60))
        self.assertFalse(dashboard.within_daily_budget([31 * 60] * 14, 30 * 60))

    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(dashboard.percentile([], 0.5), 0.0)
        self.assertEqual(dashboard.percentile([7], 0.25), 7.0)
        self.assertEqual(dashboard.percentile([0, 10], 0.5), 5.0)
        self.assertEqual(dashboard.percentile([1, 2, 3, 4], 0.25), 1.75)

    def test_rolling_windows_only_use_full_weeks(self) -> None:
        # 末日尖峰不能凭不足 7 天的尾巴单独判超预算。
        costs = [0.0] * 89 + [1801.0]
        self.assertTrue(dashboard.within_daily_budget(costs, 1800))
        self.assertAlmostEqual(
            dashboard.worst_window_average(costs), 1801.0 / 7, places=3
        )
        # 完整窗口超预算仍然被拦下。
        self.assertFalse(dashboard.within_daily_budget([1801.0] * 7, 1800))
        # 不足 7 天的样本退回整段单窗口，不会恒真。
        self.assertFalse(dashboard.within_daily_budget([1801.0] * 3, 1800))
        self.assertTrue(dashboard.within_daily_budget([10.0] * 3, 1800))
        # 每一天都被完整窗口覆盖：中段尖峰按所在完整周计算。
        # 12600/7 = 1800 恰好贴线不算超；12601/7 > 1800 才算超。
        boundary = [0.0] * 40 + [12_600.0] + [0.0] * 49
        self.assertTrue(dashboard.within_daily_budget(boundary, 1800))
        middle = [0.0] * 40 + [12_601.0] + [0.0] * 49
        self.assertFalse(dashboard.within_daily_budget(middle, 1800))

    def test_summarize_reviews_drops_invalid_and_old_entries(self) -> None:
        now = int(time.time() * 1000)
        reviews = [
            {"id": now, "ease": 0, "time": 5_000},
            {"id": now, "ease": 3, "time": 0},
            {"id": now - 91 * DAY_MS, "ease": 3, "time": 9_999_999},
            {"id": now - 10 * DAY_MS, "ease": 3, "time": 70_000},
            {"id": now - 40 * DAY_MS, "ease": 1, "time": 20_000},
        ]
        summary = dashboard.summarize_reviews(7, reviews, now)
        assert summary is not None
        self.assertEqual(summary.reviews_90, 2)
        self.assertEqual(summary.total_ms_90, 90_000)
        self.assertEqual(summary.total_ms_30, 70_000)
        self.assertEqual(summary.slow_count, 2)
        self.assertEqual(summary.again_count, 1)
        self.assertEqual(summary.avg_ms, 3_363_333)
        self.assertEqual(summary.last_times, [70, 20, 10000])

    def test_recent_five_crosses_ninety_day_boundary(self) -> None:
        now = int(time.time() * 1000)
        reviews = [
            {"id": now - days * DAY_MS, "ease": 1 if days in (100, 120) else 3, "time": 65_000}
            for days in (5, 40, 80, 100, 120, 150)
        ]
        summary = dashboard.summarize_reviews(7, reviews, now)
        assert summary is not None
        self.assertEqual(summary.reviews_90, 3)
        self.assertEqual(summary.total_ms_30, 65_000)
        self.assertEqual(summary.slow_count, 5)
        self.assertEqual(summary.again_count, 2)
        self.assertEqual(len(summary.recent_reviews), 5)

    def test_build_dashboard_uses_candidate_and_introduced_queries(self) -> None:
        fake = FakeData()
        fake.cards[3] = make_info(3, front="无历史但题干" * 40)
        fake.cards[4] = make_info(4, front="两次四十秒")
        now = int(time.time() * 1000)
        fake.reviews[4] = [
            {"id": now, "ease": 3, "time": 40_000},
            {"id": now - DAY_MS, "ease": 3, "time": 41_000},
        ]
        fake.queries['-is:suspended -tag:看板忽略 -tag:需优化卡片 (deck:"A")'] = [1, 2, 3, 4]
        result = dashboard.build_dashboard(data=fake)
        review_ids = [row["card_id"] for row in result["review_queue"]]
        self.assertIn(4, review_ids)
        self.assertNotIn(3, review_ids)
        self.assertEqual(result["balance"]["new_today"], 2)
        self.assertEqual(result["balance"]["new_7"], 4)
        self.assertIn('introduced:1 (deck:"A")', fake.queries_seen)
        self.assertNotIn('added:1 -is:suspended (deck:"A")', fake.queries_seen)

    def test_summarize_reviews_returns_none_without_valid_entries(self) -> None:
        now = int(time.time() * 1000)
        self.assertIsNone(
            dashboard.summarize_reviews(7, [{"id": now, "ease": 0, "time": 5_000}], now)
        )

    def test_make_card_row_recommends_action_by_rules(self) -> None:
        stage = dashboard.make_card_row(make_summary(), make_info(front="面试要点"))
        self.assertEqual(stage["action"], "阶段卡：考虑暂停并打标签")

        bad = dashboard.make_card_row(make_summary(), make_info(front="题" * 200))
        self.assertEqual(bad["action"], "疑似卡差：考虑拆小")
        self.assertEqual(bad["problems"], ["题干过长"])

        hard = dashboard.make_card_row(make_summary(avg_ms=70_000), make_info())
        self.assertEqual(hard["action"], "有价值但难：先观察")

        normal = dashboard.make_card_row(make_summary(), make_info())
        self.assertEqual(normal["action"], "审计")

    def test_make_card_row_marks_image_cards_without_text(self) -> None:
        row = dashboard.make_card_row(make_summary(), make_info(front="", image=True))
        self.assertEqual(row["summary"], "Basic · A · Front / Back · 卡片 1")
        self.assertEqual(row["front"], "")
        self.assertEqual(row["previewKind"], "image")
        self.assertEqual(row["thumbnail"], "x.png")
        self.assertNotIn("图片卡：不可按空题干判断", row["problems"])
        self.assertIn("图片无法分析", row["limitations"])

    def test_make_card_row_reads_common_image_src_forms(self) -> None:
        for html_value in ('<img src=x.png>', '<img src = "x.png">', "<img alt='x' src='x.png'>"):
            with self.subTest(html=html_value):
                row = dashboard.make_card_row(
                    make_summary(),
                    {
                        "cardId": 1,
                        "fields": {
                            "Front": {"value": "", "order": 0},
                            "Back": {"value": html_value, "order": 1},
                        },
                        "deckName": "A",
                        "noteType": "Basic",
                        "tags": [],
                    },
                )
                self.assertEqual(row["previewKind"], "image")
                self.assertEqual(row["thumbnail"], "x.png")

    def test_readable_text_without_image_uses_text_preview(self) -> None:
        row = dashboard.make_card_row(make_summary(), make_info(front="一段可读题面", image=False))
        self.assertEqual(row["summary"], "一段可读题面")
        self.assertEqual(row["previewKind"], "text")
        self.assertEqual(row["previewText"], "一段可读题面")
        self.assertEqual(row["thumbnail"], "")

    def test_empty_standard_front_falls_back_to_first_readable_field(self) -> None:
        row = dashboard.make_card_row(
            make_summary(),
            {
                "cardId": 1,
                "fields": {
                    "Front": {"value": "", "order": 0},
                    "Prompt": {"value": "真实可读题面", "order": 1},
                    "Back": {"value": "答案", "order": 2},
                },
                "deckName": "A",
                "noteType": "Basic",
                "tags": [],
            },
        )
        self.assertEqual(row["summary"], "真实可读题面")
        self.assertEqual(row["front"], "真实可读题面")
        self.assertEqual(row["previewKind"], "text")
        self.assertEqual(row["previewText"], "真实可读题面")
        self.assertEqual(row["titleFallback"], "Basic · A · Front / Prompt / Back · 卡片 1")

    def test_unreadable_card_uses_metadata_title_without_quality_reason(self) -> None:
        row = dashboard.make_card_row(
            dashboard.empty_review(9),
            make_info(card_id=9, front="", back="", deck="Kafka", note_type="Cloze"),
        )
        self.assertEqual(row["summary"], "Cloze · Kafka · Front / Back · 卡片 9")
        self.assertEqual(row["front"], "")
        self.assertEqual(row["previewKind"], "fallback")
        self.assertEqual(row["previewText"], "")
        self.assertEqual(row["reasons"], [])
        self.assertEqual(row["problems"], [])

    def test_recent_30_minutes_does_not_create_candidate_reason(self) -> None:
        review = dashboard.CardReview(1, 300_000, 300_000, 5, 1, 0, 35_000, [35, 35])
        row = dashboard.make_card_row(review, make_info())
        self.assertEqual(row["recent30Minutes"], 5.0)
        self.assertEqual(row["reasons"], [])

    def test_structural_problem_needs_performance_evidence_to_be_candidate(self) -> None:
        no_evidence = dashboard.make_card_row(dashboard.empty_review(1), make_info(front="题" * 200))
        self.assertEqual(no_evidence["problems"], ["题干过长"])
        self.assertEqual(no_evidence["reasons"], [])

        one_obvious_slow = dashboard.CardReview(1, 65_000, 65_000, 1, 1, 0, 65_000, [65], [
            {"durationMs": 65_000, "rating": 3, "reviewedAt": 1}
        ])
        with_evidence = dashboard.make_card_row(one_obvious_slow, make_info(front="题" * 200))
        self.assertIn("结构疑点", with_evidence["reasons"])

    def test_again_candidate_requires_two_days_or_three_recent_again(self) -> None:
        now = int(time.time() * 1000)
        two_same_day = dashboard.summarize_reviews(1, [
            {"id": now, "ease": 1, "time": 10_000},
            {"id": now - 1_000, "ease": 1, "time": 10_000},
        ], now)
        assert two_same_day is not None
        self.assertEqual(dashboard.make_card_row(two_same_day, make_info())["reasons"], [])

        two_days = dashboard.summarize_reviews(1, [
            {"id": now, "ease": 1, "time": 10_000},
            {"id": now - DAY_MS, "ease": 1, "time": 10_000},
        ], now)
        assert two_days is not None
        self.assertIn("跨 2 个复习日", dashboard.make_card_row(two_days, make_info())["reasons"][0])

        three_same_day = dashboard.summarize_reviews(1, [
            {"id": now, "ease": 1, "time": 10_000},
            {"id": now - 1_000, "ease": 1, "time": 10_000},
            {"id": now - 2_000, "ease": 1, "time": 10_000},
        ], now)
        assert three_same_day is not None
        self.assertIn("3 次 Again", dashboard.make_card_row(three_same_day, make_info())["reasons"][0])

    def test_image_only_card_without_review_evidence_is_not_a_candidate(self) -> None:
        row = dashboard.make_card_row(
            dashboard.empty_review(9),
            make_info(card_id=9, front="", back="", image=True),
        )
        self.assertEqual(row["reasons"], [])
        self.assertEqual(row["problems"], [])
        self.assertEqual(row["limitations"], ["图片无法分析"])

    def test_make_card_row_handles_missing_card_info(self) -> None:
        row = dashboard.make_card_row(make_summary(), None)
        self.assertEqual(row["front"], "")
        self.assertEqual(row["deck"], "")

    def test_html_cover_contract_removes_old_unavailable_preview(self) -> None:
        html = (Path(__file__).parents[1] / "addon" / "attention_dashboard.html").read_text()
        self.assertIn("function coverArt", html)
        self.assertIn("fallback-cover", html)
        self.assertIn("text-preview", html)
        self.assertIn("function detailFallbackTitle", html)
        self.assertIn("detail-fallback-title", html)
        self.assertNotIn("tile-symbol", html)
        self.assertNotIn("预览不可用", html)

    def test_query_builders_scope_and_escape_deck_names(self) -> None:
        self.assertEqual(dashboard.scoped_query("is:due", "A"), 'is:due deck:"A"')
        self.assertEqual(
            dashboard.scoped_query("is:due", 'My "Deck"'),
            'is:due deck:"My \\"Deck\\""',
        )
        self.assertEqual(
            dashboard.managed_query("is:due", ["A", "B"]),
            'is:due (deck:"A" or deck:"B")',
        )
        self.assertEqual(dashboard.managed_query("is:due", []), "is:due")
        self.assertEqual(dashboard.view_query("is:due", "A", ["B"]), 'is:due deck:"A"')
        self.assertEqual(dashboard.view_query("is:due", "", ["B"]), 'is:due (deck:"B")')
        self.assertEqual(dashboard.view_query("is:due", "", []), "is:due")

    def test_combined_simulation_merges_decks_and_truncates_days(self) -> None:
        simulations = [
            {
                "dailyReviewCount": [3, 3],
                "dailyNewCount": [1, 1],
                "dailyTimeCost": [10.0, 20.0],
            },
            {
                "dailyReviewCount": [2],
                "dailyNewCount": [0],
                "dailyTimeCost": [5.0],
            },
        ]

        counts, costs = dashboard.combined_simulation(simulations, 2)

        self.assertEqual(counts, [6, 4])
        self.assertEqual(costs, [15.0, 20.0])


class ConcentrationStatsTest(unittest.TestCase):
    """注意力集中度：各筛选集合的占比、P50/P90/IQR 与个人异常线。"""

    def test_thirty_second_threshold_is_inclusive_and_sixty_is_preserved(self) -> None:
        stats = dashboard.concentration_stats([29_999, 30_000, 39_999, 40_000, 59_999, 60_000])
        self.assertEqual(stats["filters"]["c30"]["count"], 5)
        self.assertEqual(stats["filters"]["c60"]["count"], 1)
        self.assertEqual(stats["slowCount"], 5)
        self.assertIn("平均耗时 ≥30 秒", " ".join(stats["statNotes"]))

    def test_personal_outliers_remain_within_thirty_second_group(self) -> None:
        stats = dashboard.concentration_stats([1_000] * 4 + [31_000])
        self.assertLess(stats["outlierMs"], 30_000)
        self.assertEqual(stats["filters"]["coutlier"]["count"], 1)

    def test_concentration_threshold_does_not_change_review_candidate_threshold(self) -> None:
        reviews = [{"id": 10_000, "ease": 3, "time": 30_000},
                   {"id": 11_000, "ease": 3, "time": 39_999}]
        summary = dashboard.summarize_reviews(1, reviews, 12_000)
        self.assertEqual(summary.slow_count, 0)
        self.assertEqual(dashboard.candidate_reasons(summary, []), [])

    def test_slow_group_counts_and_shares(self) -> None:
        # 10 张到期卡：8 张 10 秒、2 张 100 秒。
        stats = dashboard.concentration_stats([10_000] * 8 + [100_000] * 2)
        self.assertEqual(stats["cardsAnalyzed"], 10)
        self.assertEqual(stats["slowCount"], 2)
        self.assertEqual(stats["cardSharePct"], 20.0)
        self.assertEqual(stats["timeSharePct"], 71.4)  # 200 / 280 秒
        self.assertEqual(stats["verySlowCount"], 2)

    def test_filters_carry_their_own_counts_and_shares(self) -> None:
        # 每个筛选项的结论句/比例条数字必须对同一组卡片成立：
        # 分母固定为全部已分析卡片，分子是该筛选自己的集合。
        avgs = [10_000] * 7 + [45_000] * 2 + [100_000]
        stats = dashboard.concentration_stats(avgs)
        filters = stats["filters"]
        self.assertEqual(set(filters), {"c30", "c60", "coutlier"})
        # ≥30 秒：3 张（45k×2 + 100k），190 / 260 秒。
        self.assertEqual(filters["c30"]["count"], 3)
        self.assertEqual(filters["c30"]["cardSharePct"], 30.0)
        self.assertEqual(filters["c30"]["timeSharePct"], 73.1)
        # ≥60 秒：1 张。
        self.assertEqual(filters["c60"]["count"], 1)
        self.assertEqual(filters["c60"]["cardSharePct"], 10.0)
        self.assertEqual(filters["c60"]["timeSharePct"], 38.5)
        # 与扁平字段同源：右栏与首页长尾条引用的 30 秒组口径一致。
        self.assertEqual(filters["c30"]["count"], stats["slowCount"])
        self.assertEqual(filters["c30"]["cardSharePct"], stats["cardSharePct"])
        self.assertEqual(filters["c30"]["timeSharePct"], stats["timeSharePct"])

    def test_filter_counts_match_list_predicates(self) -> None:
        # 筛选数字与卡片列表的判定式逐一对齐（含异常线低于 30 秒的边界）。
        avgs = [5_000, 8_000, 41_000, 44_000, 90_000]
        stats = dashboard.concentration_stats(avgs)
        outlier_ms = stats["outlierMs"]
        self.assertEqual(
            stats["filters"]["c30"]["count"],
            sum(1 for v in avgs if v >= 30_000),
        )
        self.assertEqual(
            stats["filters"]["c60"]["count"],
            sum(1 for v in avgs if v >= 60_000),
        )
        # 个人异常集合在 ≥30 秒组内统计。
        self.assertEqual(
            stats["filters"]["coutlier"]["count"],
            sum(1 for v in avgs if v >= 30_000 and v >= outlier_ms),
        )

    def test_scope_and_stat_notes_are_dynamic(self) -> None:
        avgs = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 60_000, 100_000]
        stats = dashboard.concentration_stats(avgs)
        self.assertIn("8 张", stats["scopeNote"])
        notes = " ".join(stats["statNotes"])
        self.assertIn("P50 = 22.5 秒", notes)
        self.assertIn("P90 = 72.0 秒", notes)
        self.assertIn("个人异常线 = 73.1 秒", notes)
        self.assertIn("8 张卡片", notes)

    def test_empty_input_is_all_zero(self) -> None:
        stats = dashboard.concentration_stats([])
        self.assertEqual(stats["cardsAnalyzed"], 0)
        self.assertEqual(stats["slowCount"], 0)
        self.assertEqual(stats["cardSharePct"], 0.0)
        self.assertEqual(stats["timeSharePct"], 0.0)
        self.assertFalse(stats["prominent"])
        for key in ("c30", "c60", "coutlier"):
            self.assertEqual(
                stats["filters"][key],
                {"count": 0, "cardSharePct": 0.0, "timeSharePct": 0.0},
            )
        self.assertIn("没有可统计的到期卡片", stats["scopeNote"])

    def test_percentiles_and_personal_outlier_line(self) -> None:
        avgs = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 60_000, 100_000]
        stats = dashboard.concentration_stats(avgs)
        self.assertEqual(stats["q1S"], 13.8)
        self.assertEqual(stats["p50S"], 22.5)
        self.assertEqual(stats["q3S"], 37.5)
        self.assertEqual(stats["p90S"], 72.0)
        # 个人异常线 = Q3 + 1.5 × IQR = 37.5 + 1.5 × 23.75 = 73.125 秒。
        self.assertEqual(stats["outlierS"], 73.1)
        self.assertEqual(stats["outlierCount"], 1)

    def test_outlier_line_follows_personal_distribution(self) -> None:
        narrow = dashboard.concentration_stats([40_000] * 5 + [41_000] * 5)
        wide = dashboard.concentration_stats([5_000] * 5 + [41_000] * 5)
        self.assertEqual(narrow["outlierS"], 42.5)
        self.assertEqual(wide["outlierS"], 95.0)
        self.assertNotEqual(narrow["outlierS"], wide["outlierS"])
        self.assertNotEqual(narrow["outlierS"], 66.0)

    def test_prominence_needs_minority_time_heavy_sample(self) -> None:
        self.assertTrue(
            dashboard.concentration_stats([10_000] * 8 + [100_000] * 2)["prominent"]
        )
        # 多数卡片都慢不是“长尾”。
        self.assertFalse(
            dashboard.concentration_stats([10_000] * 2 + [100_000] * 8)["prominent"]
        )
        # 时间占比不足一半不算突出。
        self.assertFalse(
            dashboard.concentration_stats([10_000] * 19 + [100_000])["prominent"]
        )
        # 样本太少不做判断。
        self.assertFalse(
            dashboard.concentration_stats([10_000] + [100_000])["prominent"]
        )

    def test_empty_input_is_all_zero(self) -> None:
        stats = dashboard.concentration_stats([])
        self.assertEqual(stats["cardsAnalyzed"], 0)
        self.assertEqual(stats["slowCount"], 0)
        self.assertEqual(stats["cardSharePct"], 0.0)
        self.assertEqual(stats["timeSharePct"], 0.0)
        self.assertFalse(stats["prominent"])


class NewCardCapacityTest(unittest.TestCase):
    """动态新增空间：每日节奏搜索、总量池口径、保护上限与多牌组合并。"""

    DAYS = 14

    def baseline_for(self, baseline_cost, deck_count=1):
        days = self.DAYS
        return [{
            "dailyReviewCount": [1] * days,
            "dailyNewCount": [0] * days,
            "dailyTimeCost": [float(baseline_cost)] * days,
        } for _ in range(deck_count)]

    def capacity_with(self, baseline_cost, cost_per_pace, budget_seconds, decks=None):
        decks = decks or ["Deck"]
        baseline = self.baseline_for(baseline_cost, deck_count=len(decks))
        days = self.DAYS

        def simulation(deck_names, simulation_days, deck_size, new_limit, _data):
            # 模拟接口实测语义（Anki 26.08.1）：deck_size 是总量池，
            # new_limit 是每日引入；池给足整个预测期 → 每天 new_limit 张，
            # 成本随每日节奏线性增长。
            return [{
                "dailyReviewCount": [1] * simulation_days,
                "dailyNewCount": [int(new_limit)] * simulation_days,
                "dailyTimeCost": [float(baseline_cost + cost_per_pace * new_limit)] * simulation_days,
            } for _ in deck_names]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            return dashboard.new_card_capacity(decks, baseline, days, budget_seconds, None)

    def test_over_budget_has_no_new_space(self) -> None:
        plan = self.capacity_with(3000, 100, 2000)  # 基线即超预算
        self.assertEqual(plan["state"], "overloaded")
        self.assertIsNone(plan["dailyPace"])
        self.assertIsNone(plan["weeklyNew"])
        self.assertFalse(plan["capped"])
        self.assertEqual(plan["excessPerDayMinutes"], 16.7)  # (3000-2000)/60
        self.assertEqual(plan["worstWindowDailyMinutes"], 50.0)

    def test_search_finds_max_sustainable_daily_pace(self) -> None:
        plan = self.capacity_with(1000, 100, 2000)  # fits(p) ⇔ p ≤ 10
        self.assertEqual(plan["state"], "healthy")
        self.assertEqual(plan["dailyPace"], 10)
        self.assertEqual(plan["weeklyNew"], 70)  # 每天 10 张折算每周
        self.assertFalse(plan["capped"])
        self.assertEqual(plan["worstWindowDailyMinutes"], 33.3)  # 2000 秒/天

    def test_sub_daily_weekly_space_is_estimated(self) -> None:
        # 每天 1 张放不下（1950+100 > 2000），但每周 3 张可以：接口表达
        # 不了更细的周节奏，按 pace=1 的边际成本线性估算，不能说成
        # “没有新增余量”。
        plan = self.capacity_with(1950, 100, 2000)
        self.assertEqual(plan["state"], "healthy")
        self.assertEqual(plan["dailyPace"], 0)
        self.assertEqual(plan["weeklyNew"], 3)  # floor(7 × 50 / 100)
        self.assertTrue(plan["estimated"])
        # 最重 7 天日均也按线性折算：1950 + 3/7 × 100 ≈ 1993 秒/天。
        self.assertEqual(plan["worstWindowDailyMinutes"], 33.2)

    def test_no_headroom_when_even_one_per_week_exceeds(self) -> None:
        # 余量 5 秒折到每周仍不足 1 张：这时“暂无预算余量”才是诚实的。
        plan = self.capacity_with(1995, 100, 2000)
        self.assertEqual(plan["dailyPace"], 0)
        self.assertEqual(plan["weeklyNew"], 0)
        self.assertFalse(plan["estimated"])

    def test_search_guard_cap_is_not_exact_capacity(self) -> None:
        plan = self.capacity_with(0, 0, 999_999)  # 预算极大 → 触及保护上限
        self.assertEqual(plan["dailyPace"], dashboard.NEW_PACE_SEARCH_MAX)
        self.assertEqual(plan["weeklyNew"], dashboard.NEW_PACE_SEARCH_MAX * 7)
        self.assertTrue(plan["capped"])  # 展示必须说明“实际余量可能更高”

    def test_simulation_receives_total_pool_and_daily_limit(self) -> None:
        # 单位回归：deck_size 必须是总量池（pace × days），new_limit 是每日
        # 节奏。把每周量当每天量、或当成一次性脉冲，都会严重失真。
        days = self.DAYS
        calls: list[dict] = []

        def simulation(_decks, simulation_days, deck_size, new_limit, _data):
            calls.append({"deckSize": deck_size, "newLimit": new_limit,
                          "days": simulation_days})
            return [{
                "dailyReviewCount": [1] * simulation_days,
                "dailyNewCount": [int(new_limit)] * simulation_days,
                "dailyTimeCost": [float(60 * new_limit)] * simulation_days,
            }]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            dashboard.new_card_capacity(
                ["Deck"], self.baseline_for(0), days, 10_000, None
            )
        self.assertIn({"deckSize": days, "newLimit": 1, "days": days}, calls)
        self.assertTrue(all(call["deckSize"] == call["newLimit"] * call["days"] for call in calls))
        self.assertTrue(all(call["newLimit"] <= dashboard.NEW_PACE_SEARCH_MAX for call in calls))

    def test_weekly_rate_trusts_simulation_output(self) -> None:
        # 模拟实际引入少于每日上限时，按输出折算，不按上限宣称。
        days = self.DAYS

        def simulation(_decks, simulation_days, _deck_size, _new_limit, _data):
            return [{
                "dailyReviewCount": [1] * simulation_days,
                "dailyNewCount": [3] * simulation_days,
                "dailyTimeCost": [600.0] * simulation_days,
            }]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            plan = dashboard.new_card_capacity(
                ["Deck"], self.baseline_for(600), days, 999_999, None
            )
        self.assertEqual(plan["weeklyNew"], round(3 * days * 7 / days))

    def test_multi_deck_full_pace_does_not_double_count(self) -> None:
        # 两个牌组各按完整节奏模拟、逐日取最差增量合并（不求和）：
        # 基线合并 2000 秒/天，fits(p) ⇔ 2000 + 100p ≤ 3000 → p ≤ 10。
        # 若按牌组求和会得到 p ≤ 5，把容量凭空砍半。
        plan = self.capacity_with(1000, 100, 3000, decks=["A", "B"])
        self.assertEqual(plan["dailyPace"], 10)
        self.assertEqual(plan["weeklyNew"], 70)

    def heterogeneous_capacity(self, group_names):
        # 复审 P2 场景：一个预设组复习上限为 0（实测模拟实际引入 0 张
        # 新卡，成本无增量），另一个正常按每日节奏引入。合并基线
        # 2000 秒/天，增量取最贵组 → fits(p) ⇔ 2000 + 100p ≤ 3000。
        days = self.DAYS
        baseline = self.baseline_for(1000, deck_count=len(group_names))

        def simulation(names, simulation_days, _deck_size, new_limit, _data):
            results = []
            for name in names:
                introduced = 0 if name == "停滞组" else int(new_limit)
                results.append({
                    "dailyReviewCount": [1] * simulation_days,
                    "dailyNewCount": [introduced] * simulation_days,
                    "dailyTimeCost": [float(1000 + 100 * introduced)] * simulation_days,
                })
            return results

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            return dashboard.new_card_capacity(
                group_names, baseline, days, 3000, None
            )

    def test_weekly_space_follows_costliest_group_not_first(self) -> None:
        # 展示数量必须与成本口径同源：合并成本来自最贵组（正常组）的
        # 增量，数量也取它的实际引入量。取第一组会把“新增空间”错报
        # 成 0——复习上限 0 只是那个预设自己引入不了新卡。
        plan = self.heterogeneous_capacity(["停滞组", "正常组"])
        self.assertEqual(plan["state"], "healthy")
        self.assertEqual(plan["dailyPace"], 10)
        self.assertEqual(plan["weeklyNew"], 70)

    def test_weekly_space_invariant_to_group_order(self) -> None:
        # 组按名称排序：牌组改名会交换组顺序，容量结果不能随之变化。
        first = self.heterogeneous_capacity(["停滞组", "正常组"])
        second = self.heterogeneous_capacity(["正常组", "停滞组"])
        self.assertEqual(first["weeklyNew"], second["weeklyNew"])
        self.assertEqual(first["dailyPace"], second["dailyPace"])
        self.assertEqual(
            first["worstWindowDailyMinutes"], second["worstWindowDailyMinutes"]
        )
        self.assertEqual(second["weeklyNew"], 70)

    def test_short_simulation_reason(self) -> None:
        self.assertEqual(
            dashboard.short_simulation_reason("牌组 X 的 FSRS 模拟失败，已降级为排程估算：no cards to simulate"),
            "范围内没有可模拟的卡片",
        )
        self.assertEqual(dashboard.short_simulation_reason("offline"), "FSRS 模拟不可用")
        self.assertEqual(dashboard.short_simulation_reason(""), "")

    def test_budget_change_rescales_capacity(self) -> None:
        fake = FakeData()

        def simulation(_decks, days, _deck_size, new_limit, _data):
            return [{
                "dailyReviewCount": [1] * days,
                "dailyNewCount": [int(new_limit)] * days,
                "dailyTimeCost": [float(300 + 60 * new_limit)] * days,
            }]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            small = dashboard.build_dashboard(requested_daily_budget_minutes=10, data=fake)["capacity"]["health"]
            large = dashboard.build_dashboard(requested_daily_budget_minutes=30, data=fake)["capacity"]["health"]
        self.assertEqual(small["weeklyNew"], 35)   # 600 秒预算：300+60p ≤ 600 → p ≤ 5
        self.assertEqual(large["weeklyNew"], 175)  # 1800 秒预算：p ≤ 25
        self.assertEqual(small["newSpaceValue"], "约 35 张／周")
        self.assertEqual(large["newSpaceValue"], "约 175 张／周")

    def test_config_garbage_falls_back_to_defaults(self) -> None:
        # 坏类型的配置回退默认值，而不是让整块看板崩溃。
        fake = FakeData()
        fake._config["simulationDays"] = "bogus"
        fake._config["dailyBudgetMinutes"] = "bogus"
        result = dashboard.build_dashboard(data=fake)
        self.assertEqual(result["capacity"]["simulation_days"], 90)
        self.assertEqual(result["balance"]["budget_minutes"], 30)

    def test_simulation_days_clamped_to_sane_range(self) -> None:
        # 过大的观察期收敛到 730 天：不因 protobuf 溢出而失去 FSRS 模拟。
        fake = FakeData()
        fake._config["simulationDays"] = 10 ** 12
        result = dashboard.build_dashboard(data=fake)
        self.assertEqual(result["capacity"]["simulation_days"], 730)
        self.assertEqual(result["capacity"]["source"], "fsrs_simulator")

    def test_health_judgment_never_shortens_below_ninety_days(self) -> None:
        # simulationDays=30 时，第 41 天起才超预算的场景仍必须按 90 天判超载。
        fake = FakeData()
        fake._config["simulationDays"] = 30

        def simulate(group, additional_new, days, new_limit):
            cost = [0.0] * days
            for day in range(40, days):
                cost[day] = 5000.0
            return {
                "dailyReviewCount": [0] * days,
                "dailyNewCount": [0] * days,
                "dailyTimeCost": cost,
            }

        fake.simulate_group = simulate
        result = dashboard.build_dashboard(requested_daily_budget_minutes=30, data=fake)
        self.assertEqual(result["capacity"]["simulation_days"], 90)
        self.assertEqual(result["capacity"]["health"]["state"], "overloaded")
        self.assertEqual(result["hero"]["mode"], "overloaded")


class BuildDashboardTest(unittest.TestCase):
    """build_dashboard 端到端 JSON 特征测试。"""

    def setUp(self) -> None:
        self.fake = FakeData()

    def test_backend_failures_preserve_initial_dashboard_and_deck_choices(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        try:
            from test_fsrs_adapter import fsrs
        except ModuleNotFoundError:
            from tests.test_fsrs_adapter import fsrs

        for message in ("no cards to simulate", "invalid parameters"):
            with self.subTest(message=message):
                fake = FakeData()
                fake._deck_names = ["A", "Empty"]
                fake._config["managedDecks"] = []
                # 两个预设组：Empty 组的后端报错，A 组正常返回。
                fake._groups = [
                    {"decks": ["A"], "config": {}, "easyDays": [1.0] * 7},
                    {"decks": ["Empty"], "config": {}, "easyDays": [1.0] * 7},
                ]
                populated_simulation = fake.simulate_group
                collection = SimpleNamespace(_backend=SimpleNamespace(
                    simulate_fsrs_review=Mock(side_effect=Exception(message))
                ))

                def simulate(group, additional_new, days, new_limit):
                    if group["decks"] == ["Empty"]:
                        return fsrs.simulate(
                            collection, {}, group["decks"], additional_new, days, new_limit
                        )
                    return populated_simulation(group, additional_new, days, new_limit)

                fake.simulate_group = simulate
                with patch.object(fsrs, "_new_request", side_effect=lambda params: params):
                    result = dashboard.build_dashboard(data=fake)
                self.assertEqual(result["decks"], ["A", "Empty"])
                self.assertEqual(result["balance"]["budget_minutes"], 30)
                expected = "fsrs_simulator" if message == "no cards to simulate" else "schedule_estimate"
                self.assertEqual(result["capacity"]["source"], expected)
                self.assertNotIn("error", result)

    def test_plan_failure_keeps_schedule_estimate_consistent(self):
        with patch.object(dashboard, "native_simulation", side_effect=RuntimeError("offline")):
            baseline = dashboard.build_dashboard(data=FakeData())["capacity"]
        with patch.object(dashboard, "new_card_capacity", side_effect=RuntimeError("offline")):
            result = dashboard.build_dashboard(data=FakeData())["capacity"]
        self.assertEqual(result, baseline)

    def test_over_budget_reports_no_new_space(self):
        fake = FakeData()
        fake._easy_days = {"A": [1, 1, 1, 1, 1, 0, 0]}
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [1] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [464 * 60 / 7] * days}
        result = dashboard.build_dashboard(requested_daily_budget_minutes=70, data=fake)
        health = result["capacity"]["health"]
        # 超载：不禁止学习；新增空间如实显示没有预算余量，并给出代价。
        self.assertEqual(health["state"], "overloaded")
        self.assertEqual(health["stateLabel"], "恢复中")
        self.assertEqual(health["newSpaceValue"], "暂无预算余量")
        self.assertIn("仍可学习新卡", health["newSpaceNote"])
        self.assertGreater(health["excessPerDayMinutes"], 0)
        self.assertEqual(result["hero"]["mode"], "overloaded")

    def test_new_flow_separates_generation_from_activation(self):
        fake = FakeData()
        result = dashboard.build_dashboard(data=fake)
        balance = result["balance"]
        self.assertEqual(balance["generated_7"], 6)
        self.assertEqual(balance["new_7"], 4)
        self.assertEqual(balance["waiting_7"], 2)
        # 等待中的卡片只是 is:new 计数：读取原生查询，不写卡片状态或标签。
        self.assertIn('is:new added:7 (deck:"A")', fake.queries_seen)
        self.assertIn('added:7 (deck:"A")', fake.queries_seen)

    def test_rest_day_no_longer_claims_scheduling(self):
        # 模拟每天引入新卡、插件也不改 Anki 排程：休息日只影响预算与
        # 模拟成本，不能再声称“今天不安排新增”。
        fake = FakeData()
        fake._easy_days = {"A": [1, 1, 1, 1, 1, 0, 0]}
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [0] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [0.0] * days}
        result = dashboard.build_dashboard(data=fake)
        self.assertEqual(result["capacity"]["active_days"], 5)
        self.assertEqual(result["hero"]["mode"], "healthy")
        self.assertNotIn("休息日", result["hero"]["detail"])
        self.assertNotIn("不安排新增", result["hero"]["detail"])

    def test_deck_filter_scopes_budget_forecast_and_decision(self):
        fake = FakeData()
        fake._deck_names = ["A", "B"]
        fake._easy_days = {"A": [1, 1, 1, 1, 1, 0, 0], "B": [1, 0, 0, 0, 0, 0, 0]}
        selected = dashboard.build_dashboard(deck="B", data=fake)["capacity"]
        self.assertEqual(selected["weekly_budget_minutes"], 30)
        self.assertEqual(selected["managed_decks"], ["B"])
        self.assertEqual(
            {tuple(call["decks"]) for call in fake.simulate_calls}, {("B",)}
        )

    def test_selected_deck_changes_native_forecast_and_decision(self):
        fake = FakeData()
        fake._deck_names = ["A", "B"]
        original = fake.simulate_group
        def simulate(group, additional_new, days, new_limit):
            result = original(group, additional_new, days, new_limit)
            if group["decks"] == ["B"]:
                result["dailyNewCount"] = [int(new_limit)] * days
                result["dailyTimeCost"] = [0.0] * days
            return result
        fake.simulate_group = simulate
        fake.queries['is:due -is:suspended (deck:"B")'] = [200]
        a = dashboard.build_dashboard(deck="A", data=fake)
        b = dashboard.build_dashboard(deck="B", data=fake)
        self.assertGreater(a["capacity"]["forecast_minutes"], b["capacity"]["forecast_minutes"])
        self.assertEqual(a["capacity"]["health"]["state"], "overloaded")
        self.assertEqual(b["capacity"]["health"]["state"], "healthy")
        # B 无负担：搜索一直加到保护上限，如实标注可能更高。
        self.assertTrue(b["capacity"]["health"]["capped"])
        self.assertEqual(b["capacity"]["health"]["weeklyNew"], 700)
        self.assertEqual(b["capacity"]["health"]["newSpaceValue"], "约 700 张／周")
        self.assertNotEqual(a["hero"]["mode"], b["hero"]["mode"])

    def test_selected_deck_scopes_fallback_forecast(self):
        fake = FakeData()
        fake._deck_names = ["A", "B"]
        fake.queries['is:due -is:new -is:suspended (deck:"B")'] = [200]
        with patch.object(dashboard, "native_simulation", side_effect=RuntimeError("offline")):
            cap = dashboard.build_dashboard(deck="B", data=fake)["capacity"]
        self.assertEqual(cap["forecast_reviews"], 1)
        self.assertEqual(cap["managed_decks"], ["B"])
        self.assertEqual(cap["source"], "schedule_estimate")

    def test_selected_full_week_does_not_inherit_other_deck_rest_days(self):
        fake = FakeData()
        fake._deck_names = ["A", "B"]
        fake._easy_days = {"A": [1, 0, 0, 0, 0, 0, 0]}
        cap = dashboard.build_dashboard(deck="B", data=fake)["capacity"]
        self.assertEqual(cap["weekly_budget_minutes"], 210)

    def test_no_reviews_does_not_claim_healthy_retrieval(self):
        fake = FakeData()
        fake.reviews = {}
        result = dashboard.build_dashboard(data=fake)
        for signal in result["signals"][:3]:
            self.assertEqual(signal["value"], "—")
            self.assertEqual(signal["status"], "暂无复习数据")
        self.assertIsInstance(result["signals"][3]["value"], int)

    def test_successful_simulation_has_no_pending_simulation_note(self):
        def simulation(_decks, days, _deck_size, new_limit, _data):
            return [{
                "dailyReviewCount": [0] * days,
                "dailyNewCount": [int(new_limit)] * days,
                "dailyTimeCost": [0.0] * days,
            }]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            result = dashboard.build_dashboard(data=FakeData())
        health = result["capacity"]["health"]
        self.assertEqual(health["state"], "healthy")
        # 零负载在预算内可以一直加到保护上限：如实标注“可能更高”，
        # 不把上限冒充精确容量。
        self.assertTrue(health["capped"])
        self.assertEqual(health["newSpaceValue"], f"约 {health['weeklyNew']} 张／周")
        self.assertIn("搜索上限", health["newSpaceNote"])
        self.assertNotIn("待 FSRS", result["hero"]["detail"])
        self.assertEqual(result["hero"]["mode"], "healthy")
        # 主区域统一 7 天口径；长期依据与估算期限进“预测说明”。
        self.assertEqual(result["hero"]["headline"], "未来 7 天，预计余 210 分钟")
        self.assertIn("预计还可新增约", result["hero"]["detail"])
        notes = " ".join(result["hero"]["forecastNote"]["lines"])
        self.assertIn("FSRS 未来 90 天模拟", notes)
        self.assertIn("不保证永久可持续", notes)
        self.assertIn("搜索上限", notes)

    def test_build_dashboard_returns_expected_json(self) -> None:
        result = dashboard.build_dashboard(data=self.fake)

        self.assertEqual(result["selected_deck"], "")
        self.assertEqual(result["decks"], ["A"])
        self.assertRegex(
            result["generated_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )

        self.assertEqual(
            result["today"],
            {
                "reviews": 2,
                "minutes": 1.1,
                "avg_s": 32,
                "slow_count": 1,
                "again_rate": 0.0,
                "over_budget_min": 0,
            },
        )
        self.assertEqual(
            result["balance"],
            {
                "due": 10,
                "new_today": 2,
                "new_7": 4,
                "generated_7": 6,
                "waiting_7": 2,
                "suspended": 1,
                "needs_optimization": 0,
                "budget_minutes": 30,
            },
        )
        # 卡 1 平均 40.3 秒、卡 2 平均 4 秒：≥30 秒组是 1/2 张、91% 的预计时间。
        self.assertEqual(
            {key: value for key, value in result["concentration"].items() if key != "queue"},
            {
                "cardsAnalyzed": 2,
                "slowCount": 1,
                "verySlowCount": 0,
                "outlierCount": 0,
                "cardSharePct": 50.0,
                "timeSharePct": 91.0,
                "filters": {
                    "c30": {"count": 1, "cardSharePct": 50.0, "timeSharePct": 91.0},
                    "c60": {"count": 0, "cardSharePct": 0.0, "timeSharePct": 0.0},
                    "coutlier": {"count": 0, "cardSharePct": 0.0, "timeSharePct": 0.0},
                },
                "p50S": 22.2,
                "p90S": 36.7,
                "q1S": 13.1,
                "q3S": 31.2,
                "outlierS": 58.5,
                "outlierMs": 58499.5,
                "prominent": False,
                "scopeNote": "2 张到期且有复习历史的卡片 · 按最近 ≤5 次复习的平均耗时估算",
                "statNotes": [
                    "统计范围：当前范围内已到期、未暂停、未被看板忽略、且有复习历史的 2 张卡片。",
                    "时间口径：每张卡按最近最多 5 次有效复习的平均耗时估算一次复习的成本；"
                    "“估算复习耗时”是这 2 张卡的估算合计，不是全历史累计。",
                    "P50 = 22.2 秒：一半卡片的平均耗时不超过 22.2 秒。",
                    "P90 = 36.7 秒：九成卡片的平均耗时不超过 36.7 秒。",
                    "个人异常线 = 58.5 秒：由你的分布推算（Q3 + 1.5 × IQR），不是固定阈值；"
                    "平均耗时超过它的卡片显著偏慢。",
                    "慢卡判定：平均耗时 ≥30 秒计入慢卡，≥60 秒视为明显偏慢。",
                ],
            },
        )
        self.assertEqual(
            [row["cardId"] for row in result["concentration"]["queue"]], [1]
        )
        self.assertEqual(
            result["capacity"],
            {
                "forecast_counts": [5] * 7,
                "forecast_reviews": 35,
                "forecast_minutes": 11667,
                "scope_forecast_reviews": 15,
                "scope_forecast_minutes": 8,
                "weekly_budget_minutes": 210,
                "headroom_minutes": -11457,
                "budgetNote": "每天 30 分钟",
                "dailyBudgetAverageMinutes": 30.0,
                "worstWindowDailyMinutes": 1666.7,
                "recent_avg_s": 32,
                "managed_decks": ["A"],
                "source": "fsrs_simulator",
                "simulation_days": 90,
                "simulation_error": "",
                "active_days": 7,
                "easy_days": [1.0] * 7,
                "health": {
                    "state": "overloaded",
                    "stateLabel": "恢复中",
                    "tailLabel": "有优化空间",
                    "statusLine": "恢复中 · 有优化空间",
                    "weeklyNew": None,
                    "dailyPace": None,
                    "capped": False,
                    "estimated": False,
                    "excessPerDayMinutes": 1636.7,
                    "worstWindowDailyMinutes": 1666.7,
                    "simulatorReady": True,
                    "newSpaceValue": "暂无预算余量",
                    "newSpaceNote": "仍可学习新卡，但会增加待复习负担。",
                },
            },
        )
        self.assertEqual(
            result["hero"],
            {
                "mode": "overloaded",
                "stateTag": "恢复中 · 有优化空间",
                "headline": "未来 7 天，预计超出预算 11457 分钟",
                "numbersLine": "预计复习 11667 分钟 · 可用预算 210 分钟",
                "detail": "每天仍按你的 30 分钟复习。先检查高耗时卡片，寻找减负空间。",
                "ctaLabel": "查看高耗时卡片",
                "ctaTarget": "concentration",
                "bars": None,
                "forecastNote": {
                    "lines": [
                        "来源：Anki FSRS 未来 90 天模拟。",
                        "未来 7 天按现有卡片预计复习 11667 分钟（未计新增），"
                        "可用预算 210 分钟（每天 30 分钟）。",
                        "即使不新增卡片，未来 90 天的模拟负担也超出预算，系统处于恢复中。"
                        "最重的连续 7 天平均每天 1666.7 分钟"
                        "（日均预算 30.0 分钟，超 1636.7 分钟）。治理高耗时卡片可以缩短恢复时间。",
                    ]
                },
            },
        )

        self.assertEqual(len(result["attention_holes"]), 2)
        expected_row = {
                "card_id": 1,
                "deck": "A",
                "front": "题干一",
                "total_min_90": 2.0,
                "total_min_30": 1.0,
                "reviews_90": 3,
                "avg_s": 40,
                "slow_count": 1,
                "again_count": 1,
                "last_times": [60, 30, 31],
                "problems": [],
                "tags": ["tagA"],
                "action": "审计",
            }
        self.assertEqual(
            {key: result["attention_holes"][0][key] for key in expected_row},
            expected_row,
        )
        quality = result["quality_cards"]
        self.assertEqual([row["card_id"] for row in quality], [2])
        self.assertEqual(quality[0]["problems"], ["题干过长"])
        self.assertEqual(quality[0]["action"], "疑似卡差：考虑拆小")
        self.assertEqual(quality[0]["front"], "题" * 180)
        self.assertEqual([row["card_id"] for row in result["slow_cards"]], [1])
        self.assertEqual([row["card_id"] for row in result["again_slow"]], [1])

        # 超载时只做基线一次模拟：没有新增余量可搜索。
        self.assertEqual(
            self.fake.simulate_calls,
            [
                {"decks": ["A"], "additionalNew": 0, "days": 90, "newLimit": 0},
            ],
        )

    def test_optimization_queue_includes_suspended_optimized_cards(self) -> None:
        fake = FakeData()
        fake.cards[5] = make_info(5, front="暂停但仍需优化", tags=["需优化卡片"])
        fake.cards[5]["suspended"] = True
        fake.queries['tag:需优化卡片 (deck:"A")'] = [5]
        result = dashboard.build_dashboard(data=fake)
        self.assertEqual([row["card_id"] for row in result["optimization_queue"]], [5])
        self.assertTrue(result["optimization_queue"][0]["suspended"])

    def test_build_dashboard_uses_requested_budget(self) -> None:
        result = dashboard.build_dashboard(
            requested_daily_budget_minutes=45, data=self.fake
        )
        self.assertEqual(result["balance"]["budget_minutes"], 45)
        self.assertEqual(result["capacity"]["weekly_budget_minutes"], 315)

    def test_build_dashboard_rejects_unknown_deck(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            dashboard.build_dashboard(deck="不存在", data=self.fake)
        self.assertEqual(str(ctx.exception), "牌组不存在：不存在")

    def test_build_dashboard_rejects_out_of_range_budget(self) -> None:
        for budget in (0, 1441):
            with self.assertRaises(RuntimeError) as ctx:
                dashboard.build_dashboard(
                    requested_daily_budget_minutes=budget, data=self.fake
                )
            self.assertEqual(str(ctx.exception), "每日预算须为 1-1440 分钟。")

    def test_build_dashboard_without_data_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            dashboard.build_dashboard()
        self.assertIn("数据通道未初始化", str(ctx.exception))

    def test_budget_discounts_rest_days_via_easy_days(self) -> None:
        # Sat/Sun off (percentages 0) -> 5 active days -> budget 5*30=150,
        # not 7*30=210. This is the core #6 fix: budget side now reads the
        # same easyDaysPercentages the forecast side already uses.
        self.fake._easy_days = {"A": [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0]}
        cap = dashboard.build_dashboard(data=self.fake)["capacity"]
        self.assertEqual(cap["active_days"], 5)
        self.assertEqual(cap["easy_days"], [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0])
        self.assertEqual(cap["weekly_budget_minutes"], 150)
        self.assertEqual(cap["headroom_minutes"], -11517)
        self.assertEqual(cap["budgetNote"], "每天 30 分钟 · 5 个复习日")
        self.assertEqual(cap["dailyBudgetAverageMinutes"], 21.4)

    def test_budget_defaults_to_seven_days_when_no_easy_days(self) -> None:
        cap = dashboard.build_dashboard(data=self.fake)["capacity"]
        self.assertEqual(cap["active_days"], 7)
        self.assertEqual(cap["easy_days"], [1.0] * 7)
        self.assertEqual(cap["weekly_budget_minutes"], 210)

    def test_empty_managed_decks_simulates_top_level_decks(self) -> None:
        # Default "all decks" must NOT raise "尚未设置管理范围"; with the
        # default single preset, every deck joins one simulation unit and each
        # card belongs to exactly one unit (no double counting, full coverage).
        self.fake._deck_names = ["A", "A::B", "C"]
        self.fake._config["managedDecks"] = []
        result = dashboard.build_dashboard(data=self.fake)
        covered = sorted(
            deck
            for call in self.fake.simulate_calls
            if call["additionalNew"] == 0
            for deck in call["decks"]
        )
        self.assertEqual(covered, ["A", "A::B", "C"])
        self.assertEqual(result["capacity"]["managed_decks"], [])

    def test_parent_deck_simulates_each_subdeck_preset(self):
        # P1 回归：父牌组全天复习、子牌组每周休 2 天且用不同预设。选父
        # 牌组时两个预设都要参与模拟（包括新增节奏搜索），预算按并集计
        # 7 个复习日，子牌组的休息日只折减它自己的模拟成本——不再把
        # 整棵子树按父牌组参数算。
        fake = FakeData()
        fake._deck_names = ["A", "A::B"]
        parent = {"decks": ["A"], "config": {"name": "父预设"}, "easyDays": [1.0] * 7}
        child = {"decks": ["A::B"], "config": {"name": "子预设"},
                 "easyDays": [1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0]}
        fake._groups = [parent, child]
        fake._group_costs[("A",)] = lambda new_limit, days: 600.0

        def child_cost(new_limit, days):
            easy = child["easyDays"]
            return [400.0 * easy[index % 7] for index in range(days)]

        fake._group_costs[("A::B",)] = child_cost
        result = dashboard.build_dashboard(deck="A", data=fake)
        calls = fake.simulate_calls
        self.assertTrue(any(call["decks"] == ["A"] for call in calls))
        self.assertTrue(any(call["decks"] == ["A::B"] for call in calls))
        # 子预设组也参与了新增节奏搜索，而不是只有基线。
        self.assertTrue(
            any(call["decks"] == ["A::B"] and call["newLimit"] >= 1 for call in calls)
        )
        # 预算按并集：父牌组每天复习 → 7 个有效复习日。
        self.assertEqual(result["capacity"]["active_days"], 7)
        # 合并预测反映子牌组休息日：工作日 600+400，周末只剩父牌组 600
        # → (5×1000 + 2×600) / 60 = 103 分钟。若按父牌组参数算整棵子树，
        # 会得到 7×1000/60 = 117。
        self.assertEqual(result["capacity"]["forecast_minutes"], 103)

    def test_config_overflow_falls_back_to_defaults(self) -> None:
        # JSON 里的 1e309 解析成 inf：int(inf) 抛 OverflowError，业务层
        # 同样回退默认值而不是崩溃。
        fake = FakeData()
        fake._config["simulationDays"] = float("inf")
        result = dashboard.build_dashboard(data=fake)
        self.assertEqual(result["capacity"]["simulation_days"], 90)

    def test_precomputes_health_and_hero_for_display(self) -> None:
        result = dashboard.build_dashboard(data=self.fake)
        cap = result["capacity"]
        health = cap["health"]
        self.assertTrue(health["simulatorReady"])
        self.assertEqual(health["state"], "overloaded")
        self.assertEqual(health["stateLabel"], "恢复中")
        self.assertEqual(health["tailLabel"], "有优化空间")
        self.assertEqual(health["statusLine"], "恢复中 · 有优化空间")
        self.assertEqual(health["newSpaceValue"], "暂无预算余量")
        self.assertEqual(result["hero"]["ctaTarget"], "concentration")
        self.assertEqual(result["hero"]["stateTag"], "恢复中 · 有优化空间")

    def test_new_space_display_covers_three_states(self):
        # 超预算 → 暂无预算余量；有余量 → 约 X 张／周；模拟失败 → 暂无法估算。
        overloaded = dashboard.build_dashboard(data=FakeData())["capacity"]["health"]
        self.assertEqual(overloaded["newSpaceValue"], "暂无预算余量")
        self.assertIn("仍可学习新卡", overloaded["newSpaceNote"])

        def simulation(_decks, days, _deck_size, new_limit, _data):
            return [{
                "dailyReviewCount": [1] * days,
                "dailyNewCount": [int(new_limit)] * days,
                "dailyTimeCost": [float(600 + 60 * new_limit)] * days,
            }]

        with patch.object(dashboard, "native_simulation", side_effect=simulation):
            healthy = dashboard.build_dashboard(
                requested_daily_budget_minutes=30, data=FakeData()
            )["capacity"]["health"]
        self.assertEqual(healthy["weeklyNew"], 140)  # 1800 秒预算：600+60p ≤ 1800 → p ≤ 20
        self.assertEqual(healthy["newSpaceValue"], "约 140 张／周")
        self.assertEqual(healthy["newSpaceNote"], "按每周持续新增估算。")

        with patch.object(dashboard, "native_simulation", side_effect=RuntimeError("offline")):
            unknown = dashboard.build_dashboard(data=FakeData())["capacity"]["health"]
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(unknown["newSpaceValue"], "暂无法估算")
        self.assertEqual(unknown["newSpaceNote"], "FSRS 模拟不可用")

    def test_concentration_queue_is_exact_thirty_second_set(self):
        # 首页洞察与卡片检查共用同一集合：队列就是 ≥30 秒的精确集合，按平均耗时降序。
        fake = FakeData()
        seconds = {6: 30, 7: 60, 8: 100}  # 其余 7 张都是 10 秒
        make_due_reviewed_cards(fake, list(range(1, 11)), seconds)
        result = dashboard.build_dashboard(data=fake)
        concentration = result["concentration"]
        self.assertEqual(concentration["cardsAnalyzed"], 10)
        self.assertEqual([row["cardId"] for row in concentration["queue"]], [8, 7, 6])
        self.assertEqual(concentration["slowCount"], 3)
        self.assertEqual(concentration["verySlowCount"], 2)
        self.assertEqual(concentration["outlierCount"], 2)
        for row in concentration["queue"]:
            self.assertGreaterEqual(row["avgMs"], 30_000)
            self.assertGreaterEqual(row["avg_s"], 30)

    def test_concentration_follows_selected_deck(self):
        fake = FakeData()
        fake._deck_names = ["A", "B"]
        fake.cards[3] = make_info(3, front="B 牌组慢卡", back="答")
        now = int(time.time() * 1000)
        fake.reviews[3] = [{"id": now, "ease": 3, "time": 90_000}]
        fake.queries['is:due -is:suspended -tag:看板忽略 deck:"B"'] = [3]
        fake.queries['is:due -is:suspended deck:"B"'] = [3]
        fake.queries['is:due -is:suspended -tag:看板忽略 deck:"A"'] = [1, 2]
        original = fake.simulate_group

        def simulate(group, additional_new, days, new_limit):
            sim = original(group, additional_new, days, new_limit)
            if group["decks"] == ["B"]:
                sim["dailyTimeCost"] = [0.0] * days
            return sim

        fake.simulate_group = simulate
        a = dashboard.build_dashboard(deck="A", data=fake)
        b = dashboard.build_dashboard(deck="B", data=fake)
        # 牌组切换后：集中度、健康状态与 Hero 全部随范围变化。
        self.assertEqual(a["concentration"]["cardsAnalyzed"], 2)
        self.assertEqual(a["concentration"]["slowCount"], 1)
        self.assertEqual(b["concentration"]["cardsAnalyzed"], 1)
        self.assertEqual(b["concentration"]["slowCount"], 1)
        self.assertEqual([row["cardId"] for row in b["concentration"]["queue"]], [3])
        self.assertEqual(b["concentration"]["p50S"], 90.0)
        self.assertEqual(b["capacity"]["health"]["state"], "healthy")
        self.assertEqual(a["capacity"]["health"]["state"], "overloaded")

    def test_hero_longtail_beats_overloaded_budget(self):
        # 长尾突出时优先于超预算表达：先治理最昂贵的卡片。
        fake = FakeData()
        seconds = {1: 100, 2: 100}  # 2/10 张吃掉大部分时间
        make_due_reviewed_cards(fake, list(range(1, 11)), seconds)
        hero = dashboard.build_dashboard(data=fake)["hero"]
        self.assertEqual(hero["mode"], "longtail")
        self.assertIn("未来 7 天", hero["headline"])
        self.assertIn("2 张卡占到期卡片的 20.0%", hero["detail"])
        self.assertIn("占估算复习耗时的 71.4%", hero["detail"])
        self.assertIn("先治理最贵的卡", hero["detail"])
        self.assertEqual(hero["ctaLabel"], "查看高耗时卡片")
        self.assertEqual(hero["ctaTarget"], "concentration")
        self.assertEqual(hero["bars"], {"cardSharePct": 20.0, "timeSharePct": 71.4})
        self.assertEqual(hero["stateTag"], "恢复中 · 长尾偏重")
        self.assertTrue(hero["forecastNote"]["lines"])

    def test_hero_today_done_when_nothing_due(self):
        fake = FakeData()
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [0] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [0.0] * days}
        fake.queries['is:due -is:suspended (deck:"A")'] = []
        fake.queries['is:due -is:suspended -tag:看板忽略 (deck:"A")'] = []
        hero = dashboard.build_dashboard(data=fake)["hero"]
        self.assertEqual(hero["mode"], "todaydone")
        self.assertEqual(hero["headline"], "今天的复习预算已经完成")
        self.assertIn("已复习 1.1 分钟", hero["numbersLine"])
        self.assertIn("预算 30 分钟", hero["numbersLine"])
        self.assertEqual(hero["detail"], "当前没有到期卡片。")
        self.assertEqual(hero["stateTag"], "今日已完成")

    def test_hero_today_done_compares_unrounded_time(self):
        # 判定用原始毫秒：59 秒 < 60 秒预算不算完成，61 秒才算；
        # 展示值才四舍五入到 0.1 分钟。
        fake = FakeData()
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [0] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [0.0] * days}
        now = int(time.time() * 1000)
        fake.reviews = {
            1: [{"id": now, "ease": 3, "time": 55_000}],
            2: [{"id": now - 1000, "ease": 3, "time": 4_000}],
        }
        result = dashboard.build_dashboard(requested_daily_budget_minutes=1, data=fake)
        self.assertEqual(result["today"]["minutes"], 1.0)  # 展示仍舍入
        self.assertNotEqual(result["hero"]["mode"], "todaydone")
        fake.reviews[1] = [{"id": now, "ease": 3, "time": 57_000}]
        result = dashboard.build_dashboard(requested_daily_budget_minutes=1, data=fake)
        self.assertEqual(result["hero"]["mode"], "todaydone")
        self.assertIn("预算 1 分钟", result["hero"]["numbersLine"])
        self.assertIn("留到明天", result["hero"]["detail"])

    def test_hero_today_done_when_budget_spent_with_cards_left(self):
        # 预算已经用尽但仍有到期卡：也要明确告诉用户今天已经完成。
        fake = FakeData()
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [0] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [0.0] * days}
        hero = dashboard.build_dashboard(requested_daily_budget_minutes=1, data=fake)["hero"]
        self.assertEqual(hero["mode"], "todaydone")
        self.assertEqual(hero["headline"], "今天的复习预算已经完成")
        self.assertIn("已复习 1.1 分钟", hero["numbersLine"])
        self.assertIn("预算 1 分钟", hero["numbersLine"])
        self.assertIn("留到明天", hero["detail"])

    def test_hero_empty_today_without_reviews(self):
        fake = FakeData()
        fake.simulate_group = lambda group, additional_new, days, new_limit: {
            "dailyReviewCount": [0] * days, "dailyNewCount": [0] * days,
            "dailyTimeCost": [0.0] * days}
        fake.queries['is:due -is:suspended (deck:"A")'] = []
        fake.queries['is:due -is:suspended -tag:看板忽略 (deck:"A")'] = []
        fake.reviews = {}
        hero = dashboard.build_dashboard(data=fake)["hero"]
        self.assertEqual(hero["mode"], "todaydone")
        self.assertEqual(hero["headline"], "今天没有到期卡片")
        self.assertIsNone(hero["numbersLine"])

    def test_hero_degraded_simulator_falls_back_to_estimate(self):
        with patch.object(dashboard, "native_simulation", side_effect=RuntimeError("offline")):
            hero = dashboard.build_dashboard(data=FakeData())["hero"]
        self.assertEqual(hero["mode"], "unknown")
        self.assertIn("未来 7 天，预计余", hero["headline"])
        self.assertIn("排程估算", hero["detail"])
        # 降级原因与口径进“预测说明”，不挤占正文。
        notes = " ".join(hero["forecastNote"]["lines"])
        self.assertIn("降级原因：offline", notes)
        self.assertIn("排程估算", notes)

    def test_hero_html_contract(self):
        html = (Path(__file__).parents[1] / "addon" / "attention_dashboard.html").read_text()
        self.assertIn("data-open-concentration", html)
        self.assertIn("function ratioBars", html)
        self.assertIn("hero-side", html)
        self.assertIn("concentration-summary", html)
        self.assertIn("高耗时集中度", html)
        self.assertIn("统计说明", html)
        self.assertIn("预测说明", html)
        self.assertIn("data-info", html)
        self.assertIn("state-tag", html)
        self.assertIn("卡片占比", html)
        self.assertIn("耗时占比", html)
        self.assertIn(":root.night-mode", html)
        # 右栏是“预算与新增”：复习预算 + 动态新增空间，不再有固定计划。
        self.assertIn("预算与新增", html)
        self.assertIn("新增空间", html)
        self.assertNotIn("本周安排", html)
        self.assertNotIn("计划新增", html)
        # 旧的首页大票面与暂停新增文案不再出现。
        self.assertNotIn("budget-ticket", html)
        self.assertNotIn("big-amount", html)
        self.assertNotIn("decision_basis", html)
        self.assertNotIn("暂停新增", html)
        # 状态移出右栏，右栏只保留预算与新增空间。
        self.assertNotIn("系统状态", html)

    def test_precomputes_today_signals_with_tones(self) -> None:
        signals = dashboard.build_dashboard(data=self.fake)["signals"]
        self.assertEqual(
            [s["label"] for s in signals],
            ["单卡均时", "40 秒以上", "Again 率", "当前到期"],
        )
        avg, slow, again, due = signals
        self.assertEqual(
            avg,
            {"label": "单卡均时", "value": 32, "unit": "秒", "tone": "tone-green", "status": "可控"},
        )
        self.assertEqual(
            slow,
            {"label": "40 秒以上", "value": 1, "unit": "次", "tone": "tone-amber", "status": "需要审计"},
        )
        self.assertEqual(
            again,
            {"label": "Again 率", "value": 0.0, "unit": "%", "tone": "tone-green", "status": "稳定"},
        )
        self.assertEqual(
            due,
            {"label": "当前到期", "value": 10, "unit": "张", "tone": "tone-green", "status": "可控"},
        )


if __name__ == "__main__":
    unittest.main()
