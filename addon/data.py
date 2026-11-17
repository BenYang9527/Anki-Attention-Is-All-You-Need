"""Anki data module.

A *deep* module: a narrow ``AttentionData`` interface that hides all
collection access (deck lookup, card search, revlog reads, FSRS simulation)
and its batching. Business logic talks only to this interface, so it can be
tested with a trivial fake and never imports ``aqt``/``anki``.

The FSRS private backend is delegated to :mod:`fsrs_adapter`, the single
place that touches Anki's protobuf. ``AnkiData`` itself stays importable
without Anki installed.
"""

from __future__ import annotations

from typing import Any, Protocol

try:  # real addon package
    from . import fsrs_adapter as _fsrs
except ImportError:  # tests load this module standalone
    import fsrs_adapter as _fsrs


_CARD_BATCH = 100
_REVIEW_BATCH = 80


def chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _config_int(config: dict[str, Any], key: str, default: int) -> int:
    """安全读取整数配置：坏值回退默认值，保证 ``config()`` 不抛异常。

    OverflowError 一并捕获：JSON 里的 1e309 会解析成 inf，
    ``int(inf)`` 抛的是 OverflowError 而不是 ValueError。与
    ``attention_dashboard._config_int`` 成对存在——业务层对注入源
    做同样的防御，两层职责见各自注释。
    """
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError, OverflowError):
        return default


class AttentionData(Protocol):
    def deck_refs(self) -> list[dict[str, Any]]: ...
    def current_deck_id(self) -> int | None: ...
    def resolve_deck(self, deck_id: int) -> str | None: ...
    def deck_names(self) -> list[str]: ...
    def find_cards(self, query: str) -> list[int]: ...
    def card_infos(self, card_ids: list[int]) -> dict[int, dict[str, Any]]: ...
    def reviews_of_cards(self, card_ids: list[int]) -> dict[int, list[dict[str, Any]]]: ...
    def simulation_groups(self, deck: str, managed_decks: list[str]) -> list[dict[str, Any]]: ...
    def simulate_group(
        self, group: dict[str, Any], additional_new: int, days: int, new_limit: int
    ) -> dict[str, Any]: ...
    def config(self) -> dict[str, Any]: ...


class AnkiData:
    """``AttentionData`` backed by a live Anki collection.

    ``col`` is duck-typed (the methods/attributes Anki's ``Collection`` exposes),
    and ``addon_config`` is a plain dict snapshot of the add-on's config. Both are
    injected so this class has no hard dependency on ``aqt`` and can be unit-tested
    with stubs.
    """

    def __init__(self, col, addon_config: dict[str, Any]) -> None:
        self._col = col
        self._addon_config = dict(addon_config)

    def deck_names(self) -> list[str]:
        return [item.name for item in self._col.decks.all_names_and_ids()]

    def deck_refs(self) -> list[dict[str, Any]]:
        items = list(self._col.decks.all_names_and_ids())
        ids_by_name = {item.name: int(item.id) for item in items}
        refs = []
        for item in items:
            parent_name = item.name.rpartition("::")[0] or None
            refs.append(
                {
                    "deckId": int(item.id),
                    "name": item.name,
                    "parentId": ids_by_name.get(parent_name) if parent_name else None,
                    "descendantCount": len(self._col.decks.children(int(item.id))),
                }
            )
        return sorted(refs, key=lambda ref: ref["name"].casefold())

    def current_deck_id(self) -> int | None:
        selected = self._col.decks.selected()
        return int(selected) if selected is not None else None

    def resolve_deck(self, deck_id: int) -> str | None:
        return self._col.decks.name_if_exists(int(deck_id)) or None

    def find_cards(self, query: str) -> list[int]:
        return list(self._col.find_cards(query))

    def card_infos(self, card_ids: list[int]) -> dict[int, dict[str, Any]]:
        infos: dict[int, dict[str, Any]] = {}
        for part in chunks(list(card_ids), _CARD_BATCH):
            for card_id in part:
                card = self._col.get_card(int(card_id))
                note = card.note()
                fields = {
                    name: {"value": value, "order": index}
                    for index, (name, value) in enumerate(note.items())
                }
                infos[int(card.id)] = {
                    "cardId": card.id,
                    "noteId": card.nid,
                    "deckId": card.did,
                    "fields": fields,
                    "deckName": self._col.decks.name_if_exists(card.did) or "",
                    "noteType": str(note.note_type().get("name") or ""),
                    "tags": list(note.tags),
                    "suspended": int(card.queue) == -1,
                }
        return infos

    def card_detail(self, card_id: int) -> dict[str, Any]:
        card = self._col.get_card(int(card_id))
        note = card.note()
        card_ids = sorted(int(cid) for cid in note.card_ids())
        return {
            "cardId": int(card.id),
            "noteId": int(card.nid),
            "deckId": int(card.did),
            "deckName": self._col.decks.name_if_exists(card.did) or "",
            "noteType": str(note.note_type().get("name") or ""),
            "fields": [
                {"name": name, "value": value}
                for name, value in note.items()
            ],
            "questionHtml": card.question(),
            "answerHtml": card.answer(),
            "cardCss": str(note.note_type().get("css") or ""),
            "tags": list(note.tags),
            "suspended": int(card.queue) == -1,
            "noteCardCount": len(card_ids),
            "noteCardIds": card_ids,
        }

    def reviews_of_cards(self, card_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        result: dict[int, list[dict[str, Any]]] = {int(cid): [] for cid in card_ids}
        if not card_ids:
            return result
        for part in chunks(list(card_ids), _REVIEW_BATCH):
            placeholders = ",".join("?" for _ in part)
            rows = self._col.db.all(
                f"select id, cid, ease, time, type from revlog where cid in ({placeholders})",
                *part,
            )
            for review_id, card_id, ease, review_time, review_type in rows:
                result[int(card_id)].append(
                    {
                        "id": review_id,
                        "cardId": card_id,
                        "ease": ease,
                        "time": review_time,
                        "type": review_type,
                    }
                )
        return result

    def simulation_groups(self, deck: str, managed_decks: list[str]) -> list[dict[str, Any]]:
        """按牌组预设（deck config）划分的模拟单元。

        父牌组的搜索虽然覆盖子树卡片，但每张卡的复习参数取决于它所属
        牌组的预设（已实测：``config_dict_for_deck_id`` 按牌组返回各自
        预设）。把整棵子树按父牌组参数模拟会失真，因此这里把范围内的
        牌组按预设分组，每组带自己的配置与休息日；组内搜索由
        :func:`fsrs_adapter.group_search` 保证互不重叠、合起来覆盖全部
        卡片。范围：选中牌组的子树 > 管理牌组的子树 > 全部牌组。
        """
        names = [item.name for item in self._col.decks.all_names_and_ids()]
        if deck:
            scope = [deck] + [n for n in names if n.startswith(deck + "::")]
        elif managed_decks:
            scope: list[str] = []
            for root in managed_decks:
                if root not in names:
                    continue
                scope.append(root)
                scope.extend(n for n in names if n.startswith(root + "::"))
            scope = list(dict.fromkeys(scope))
        else:
            scope = list(names)
        groups: dict[Any, dict[str, Any]] = {}
        for name in scope:
            deck_id = self._col.decks.id_for_name(name)
            if deck_id is None:
                continue
            config = self._col.decks.config_dict_for_deck_id(deck_id)
            key = config.get("id", name)
            group = groups.setdefault(
                key,
                {
                    "decks": [],
                    "config": config,
                    "easyDays": list(
                        config.get("easyDaysPercentages") or [1.0] * 7
                    ),
                },
            )
            group["decks"].append(name)
        return sorted(groups.values(), key=lambda group: group["decks"][0])

    def simulate_group(
        self, group: dict[str, Any], additional_new: int, days: int, new_limit: int
    ) -> dict[str, Any]:
        return _fsrs.simulate(
            self._col, group["config"], group["decks"], additional_new, days, new_limit
        )

    def config(self) -> dict[str, Any]:
        available = (
            {item.name for item in self._col.decks.all_names_and_ids()}
            if self._col is not None
            else set()
        )
        configured = self._addon_config.get("managedDecks") or []
        return {
            "managedDecks": [name for name in configured if name in available],
            "simulationDays": _config_int(self._addon_config, "simulationDays", 90),
            "dailyBudgetMinutes": _config_int(self._addon_config, "dailyBudgetMinutes", 30),
        }
