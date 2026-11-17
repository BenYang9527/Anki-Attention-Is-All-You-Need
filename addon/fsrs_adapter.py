"""FSRS review simulator adapter.

This is the ONLY place that touches Anki's private protobuf backend
(``collection._backend.simulate_fsrs_review`` and the generated
``SimulateFsrsReviewRequest``). Anki's own docs warn that the backend /
protobuf shape can change between versions without notice, so everything
behind this file is treated as a compatibility surface: a version gate and an
explicit degradation path keep callers out of trouble when the private API
moves.
"""

from __future__ import annotations

from typing import Any


_BS = chr(92)  # backslash
_Q = chr(34)   # double quote


def _escape_deck(deck: str) -> str:
    """Escape backslashes and double quotes for an Anki search deck filter.

    Mirrors ``attention_dashboard._escape_deck`` so the FSRS ``search`` field
    uses the same escaping as the regular query builders.
    """
    return deck.replace(_BS, _BS + _BS).replace(_Q, _BS + _Q)


def fsrs_params(config: dict[str, Any]) -> list[float]:
    """Return saved weights, or [] to let Anki use its FSRS defaults.

    FSRS accepts empty parameters for presets that have not been optimized;
    keep defaults in the backend so they match the installed Anki version.
    """
    params = (
        config.get("fsrsParams6")
        or config.get("fsrsParams5")
        or config.get("fsrsWeights")
        or []
    )
    return list(params)


def group_search(decks: list[str]) -> str:
    """预设组的模拟搜索：每个牌组只匹配自己的卡片，组间互不重叠。

    ``deck:"X"`` 会连子牌组一起匹配；两个预设组的搜索若都用子树形式，
    父组的搜索会把子组的卡片再算一遍。这里对组内每个牌组使用精确形式
    ``deck:"X" -deck:"X::*"``（已实测核对），保证按预设分组的模拟互不
    重复、合起来覆盖范围内全部卡片。
    """
    terms = [
        f'(deck:"{_escape_deck(deck)}" -deck:"{_escape_deck(deck)}::*")'
        for deck in decks
    ]
    return f'({" or ".join(terms)}) -is:suspended -is:new'


def build_request_params(
    config: dict[str, Any],
    decks: list[str],
    additional_new: int,
    days: int,
    new_limit: int,
) -> dict[str, Any]:
    """Build the kwargs for ``SimulateFsrsReviewRequest`` from a deck config.

    Pure: no Anki imports, no collection access. This isolates the part of the
    FSRS request that is easy to get wrong (defaults, clamping, the optional
    suspend-after-lapse knob) so it can be tested without a live collection.
    ``decks`` 是同一预设组内的牌组列表，共用这份配置。
    """
    params = {
        "params": fsrs_params(config),
        "desired_retention": float(config.get("desiredRetention", 0.9)),
        "deck_size": max(0, int(additional_new)),
        "days_to_simulate": max(1, int(days)),
        "new_limit": max(0, int(new_limit)),
        "review_limit": int(config.get("rev", {}).get("perDay", 9999)),
        "max_interval": int(config.get("rev", {}).get("maxIvl", 36500)),
        "search": group_search(decks),
        "new_cards_ignore_review_limit": False,
        "easy_days_percentages": config.get("easyDaysPercentages") or [1.0] * 7,
        "review_order": int(config.get("reviewOrder", 0)),
        "historical_retention": float(config.get("sm2Retention", 0.9)),
        "learning_step_count": len(config.get("new", {}).get("delays") or []),
        "relearning_step_count": len(config.get("lapse", {}).get("delays") or []),
    }
    if int(config.get("lapse", {}).get("leechAction", 1)) == 0:
        params["suspend_after_lapse_count"] = int(
            config.get("lapse", {}).get("leechFails", 8)
        )
    return params


def _new_request(params: dict[str, Any]):
    # Lazy import: keeps this module importable without Anki installed, and
    # localizes the generated-protobuf dependency to this one call site.
    from anki.scheduler_pb2 import SimulateFsrsReviewRequest

    suspend = params.pop("suspend_after_lapse_count", None)
    request = SimulateFsrsReviewRequest(**params)
    if suspend is not None:
        request.suspend_after_lapse_count = suspend
    return request


def simulate(
    collection,
    config: dict[str, Any],
    decks: list[str],
    additional_new: int,
    days: int,
    new_limit: int,
) -> dict[str, Any]:
    """Run the FSRS simulation for one preset group.

    Degrades to a ``RuntimeError`` (caught by the dashboard, which then falls
    back to the schedule-estimate path) whenever the installed Anki version no
    longer exposes the private backend / protobuf this adapter targets —
    whether the method is missing, the generated module moved, or a field was
    renamed. Callers never see a raw ``ImportError``/``AttributeError``.
    """
    backend = getattr(collection, "_backend", None)
    runner = getattr(backend, "simulate_fsrs_review", None)
    if runner is None:
        raise RuntimeError("当前 Anki 版本未暴露 FSRS 模拟接口，已降级为排程估算。")
    label = "、".join(decks[:3]) + ("…" if len(decks) > 3 else "")
    try:
        request = _new_request(
            build_request_params(config, decks, additional_new, days, new_limit)
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        # ValueError covers protobuf range errors (e.g. absurd simulationDays),
        # so even a bad config degrades instead of crashing the dashboard.
        raise RuntimeError(
            f"FSRS 模拟接口不可用，已降级为排程估算：{exc}"
        ) from exc
    # Anki backend errors are not necessarily RuntimeError subclasses. Convert
    # them at this boundary so one deck cannot abort the entire dashboard.
    try:
        result = runner(request)
    except Exception as exc:
        if str(exc).strip() == "no cards to simulate" and additional_new <= 0:
            horizon = max(1, int(days))
            return {
                "dailyReviewCount": [0] * horizon,
                "dailyNewCount": [0] * horizon,
                "dailyTimeCost": [0.0] * horizon,
            }
        raise RuntimeError(
            f"牌组 {label} 的 FSRS 模拟失败，已降级为排程估算：{exc}"
        ) from exc
    return {
        "dailyReviewCount": list(result.daily_review_count),
        "dailyNewCount": list(result.daily_new_count),
        "dailyTimeCost": list(result.daily_time_cost),
    }
