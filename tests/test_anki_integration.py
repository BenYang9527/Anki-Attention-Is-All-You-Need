"""Run with Anki's bundled Python packages for an isolated collection smoke test."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
import sys
import unittest


try:
    from anki.collection import Collection
except ImportError:  # normal developer Python does not include Anki
    Collection = None

sys.path.insert(0, str(Path(__file__).parents[1] / "addon"))
import attention_dashboard as dashboard
import dashboard_actions
from data import AnkiData


@unittest.skipUnless(Collection, "requires Anki's Python packages")
class IsolatedAnkiCollectionTest(unittest.TestCase):
    def test_introduced_search_counts_first_answer_not_imported_cards(self):
        with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                model = col.models.by_name("Basic")
                deck_id = col.decks.id_for_name("Default")
                card_ids = []
                for index in range(100):
                    note = col.new_note(model)
                    note["Front"] = f"Q{index}"
                    note["Back"] = f"A{index}"
                    col.add_note(note, deck_id)
                    card_ids.append(int(note.card_ids()[0]))
                now = int(time.time() * 1000)
                rows = []
                for index, card_id in enumerate(card_ids[:5]):
                    rows.append((now + index, card_id, -1, 3, 1, 0, 0, 1000, 0))
                rows.append((now + 100, card_ids[0], -1, 3, 1, 0, 0, 1000, 1))
                rows.append((now - 10 * 86400 * 1000, card_ids[5], -1, 3, 1, 0, 0, 1000, 0))
                col.db.executemany(
                    "insert into revlog (id,cid,usn,ease,ivl,lastIvl,factor,time,type) values (?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                self.assertEqual(len(col.find_cards("introduced:1")), 5)
                self.assertEqual(len(col.find_cards("introduced:7")), 5)
            finally:
                col.close()

    def test_rich_note_detail_and_native_writes(self):
        with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                model = col.models.by_name("Basic")
                self.assertIsNotNone(model)
                note = col.new_note(model)
                note["Front"] = '<b>Question</b><a href="obsidian://open?vault=Test">source</a>'
                note["Back"] = '<img src="missing-for-smoke.png">Answer'
                col.add_note(note, col.decks.id_for_name("Default"))
                card_id = int(note.card_ids()[0])

                data = AnkiData(col, {})
                detail = data.card_detail(card_id)
                self.assertIn("<b>Question</b>", detail["questionHtml"])
                self.assertEqual(detail["fields"][1]["value"], '<img src="missing-for-smoke.png">Answer')

                saved = dashboard_actions.save_note(col, note.id, {"Front": "<i>Changed</i>"}, ["keep"])
                self.assertEqual(saved["cardIds"], [card_id])
                self.assertEqual(col.get_note(note.id)["Back"], '<img src="missing-for-smoke.png">Answer')

                optimized = dashboard_actions.set_optimization(col, card_id, True)
                self.assertTrue(optimized["suspended"])
                self.assertIn("需优化卡片", col.get_note(note.id).tags)
                dashboard_actions.set_optimization(col, card_id, False)
                self.assertTrue(data.card_detail(card_id)["suspended"])

                dashboard_actions.set_hidden(col, card_id, True)
                self.assertIn("看板忽略", col.get_note(note.id).tags)
                prepared = dashboard_actions.prepare_delete(col, card_id)
                dashboard_actions.delete_note(col, note.id, prepared["cardIds"])
                self.assertEqual(col.card_ids_of_note(note.id), [])
            finally:
                col.close()

    def test_sibling_scope_native_undo_and_cross_deck_delete(self):
        with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name("Basic (and reversed card)"))
                note['Front'], note['Back'] = 'Q', 'A'
                col.add_note(note, 1)
                cards = sorted(note.card_ids())
                col.set_deck([cards[1]], col.decks.id('Other'))
                detail = AnkiData(col, {}).card_detail(cards[0])
                self.assertEqual(detail['noteCardIds'], cards)
                result = dashboard_actions.set_optimization(col, cards[0], True)
                self.assertTrue(result.changes.note)
                self.assertTrue(result.changes.card)
                self.assertEqual(col.get_card(cards[0]).queue, -1)
                self.assertNotEqual(col.get_card(cards[1]).queue, -1)
                col.undo()
                self.assertNotIn('需优化卡片', col.get_note(note.id).tags)
                self.assertTrue(all(col.get_card(cid).queue != -1 for cid in cards))
                dashboard_actions.set_hidden(col, cards[0], True)
                self.assertEqual(sorted(col.find_cards('tag:看板忽略')), cards)
                dashboard_actions.set_hidden(col, cards[0], False)
                self.assertFalse(col.find_cards('tag:看板忽略'))
                with self.assertRaisesRegex(ValueError, '影响范围已变化'):
                    dashboard_actions.delete_note(col, note.id, [cards[0]])
                self.assertEqual(sorted(col.card_ids_of_note(note.id)), cards)
                dashboard_actions.delete_note(col, note.id, cards)
                self.assertEqual(col.card_ids_of_note(note.id), [])
            finally:
                col.close()

    def test_cloze_original_fields_survive_explicit_save(self):
        with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                note = col.new_note(col.models.by_name('Cloze'))
                note['Text'] = '<b>{{c1::answer}}</b><img src="picture.png">'
                extra = '<a href="marginnote4app://note/QA">source</a>'
                note['Back Extra'] = extra
                col.add_note(note, 1)
                dashboard_actions.save_note(col, note.id, {'Text': note['Text'] + ' edited'}, ['keep'])
                stored = col.get_note(note.id)
                self.assertIn('{{c1::answer}}', stored['Text'])
                self.assertEqual(stored['Back Extra'], extra)
            finally:
                col.close()

    def test_subdeck_preset_groups_simulate_separately(self):
        # P1 回归（真实集合）：子牌组换过预设后，选父牌组时必须按预设
        # 分组模拟——config_dict_for_deck_id 按牌组返回各自预设，父牌组
        # 的搜索虽然覆盖子树卡片，参数却不能整棵子树共用。
        with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
            col = Collection(str(Path(folder) / "collection.anki2"))
            try:
                model = col.models.by_name("Basic")
                parent_id = col.decks.id("学习")
                child_id = col.decks.id("学习::英语")
                for deck_id, front in ((parent_id, "p"), (child_id, "c")):
                    note = col.new_note(model)
                    note["Front"] = front
                    note["Back"] = "a"
                    col.add_note(note, deck_id)

                child_preset = col.decks.add_config_returning_id("子预设")
                conf = col.decks.get_config(child_preset)
                conf["desiredRetention"] = 0.95
                conf["easyDaysPercentages"] = [1, 1, 1, 1, 1, 0, 0]
                col.decks.update_config(conf)
                deck = col.decks.get(child_id)
                deck["conf"] = child_preset
                col.decks.save(deck)

                data = AnkiData(col, {})
                groups = data.simulation_groups("学习", [])
                self.assertEqual(
                    [group["decks"] for group in groups], [["学习"], ["学习::英语"]]
                )
                parent, child = groups
                self.assertEqual(parent["config"]["id"], col.decks.config_dict_for_deck_id(parent_id)["id"])
                self.assertEqual(child["config"]["id"], child_preset)
                self.assertEqual(child["config"]["desiredRetention"], 0.95)
                self.assertEqual(child["easyDays"], [1, 1, 1, 1, 1, 0, 0])
                # 组内搜索互不重叠、合起来覆盖子树：父组 1 张、子组 1 张。
                import fsrs_adapter
                for card_id, deck_of_card in col.db.all("select id, did from cards"):
                    if deck_of_card in (parent_id, child_id):
                        col.db.execute(
                            "update cards set queue=2, type=2, ivl=10 where id=?",
                            card_id,
                        )
                for group, expected in ((parent, 1), (child, 1)):
                    search = fsrs_adapter.group_search(group["decks"])
                    self.assertEqual(len(col.find_cards(search)), expected)
                self.assertEqual(len(col.find_cards('deck:"学习"')), 2)  # 子树合计
                # 两个组都能跑真实 FSRS 模拟。
                for group in groups:
                    result = data.simulate_group(group, additional_new=0, days=30, new_limit=0)
                    self.assertEqual(len(result["dailyTimeCost"]), 30)
            finally:
                col.close()

    def test_new_space_invariant_to_group_order(self):
        # 复审 P2 回归（真实集合复现）：一个预设复习上限 0（适配器
        # new_cards_ignore_review_limit=False，实测模拟实际引入 0 张新
        # 卡）、另一个 9999。展示数量曾取第一组模拟的引入量——组按名称
        # 排序，牌组改名交换组序就会改变“新增空间”。现在数量与成本
        # 同源：都按“全部新增集中在最贵预设组”的分配策略。
        def scenario(slow_name, normal_name):
            with tempfile.TemporaryDirectory(prefix="attention-anki-") as folder:
                col = Collection(str(Path(folder) / "collection.anki2"))
                try:
                    model = col.models.by_name("Basic")
                    for name, per_day in ((slow_name, 0), (normal_name, 9999)):
                        deck_id = col.decks.id(name)
                        preset = col.decks.add_config_returning_id(f"预设-{name}")
                        conf = col.decks.get_config(preset)
                        conf.setdefault("rev", {})["perDay"] = per_day
                        col.decks.update_config(conf)
                        deck = col.decks.get(deck_id)
                        deck["conf"] = preset
                        col.decks.save(deck)
                        note = col.new_note(model)
                        note["Front"], note["Back"] = "q", "a"
                        col.add_note(note, deck_id)
                    for card_id, did in col.db.all("select id, did from cards"):
                        col.db.execute(
                            "update cards set queue=2, type=2, ivl=10 where id=?",
                            card_id,
                        )
                    data = AnkiData(col, {"managedDecks": [slow_name, normal_name]})
                    groups = data.simulation_groups("", [slow_name, normal_name])
                    baseline = [
                        data.simulate_group(group, additional_new=0, days=90, new_limit=0)
                        for group in groups
                    ]
                    plan = dashboard.new_card_capacity(groups, baseline, 90, 300, data)
                    slow = next(
                        group for group in groups
                        if group["config"].get("rev", {}).get("perDay") == 0
                    )
                    slow_introduced = sum(
                        data.simulate_group(slow, additional_new=90, days=90, new_limit=1)
                        ["dailyNewCount"]
                    )
                    return {"plan": plan, "slowIntroduced": slow_introduced}
                finally:
                    col.close()

        first = scenario("甲", "乙")
        second = scenario("乙", "甲")  # 交换哪个预设复习上限为 0 → 组序颠倒
        # 前提与复审一致：复习上限 0 的组，模拟实际引入 0 张新卡。
        self.assertEqual(first["slowIntroduced"], 0)
        self.assertEqual(second["slowIntroduced"], 0)
        for field in ("state", "dailyPace", "weeklyNew", "worstWindowDailyMinutes"):
            self.assertEqual(first["plan"][field], second["plan"][field], field)


if __name__ == "__main__":
    unittest.main()
