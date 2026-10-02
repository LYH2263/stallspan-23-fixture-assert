"""不变量断言：只吃现网入口的真实输出，自身不实现任何放置逻辑。

可用开区间由现网引擎的 free_spans_from_pillars 现算（同一口径），
断言只做几何与对账校验。所有失败信息都带：夹具名、期望、实际。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.first_fit_engine import free_spans_from_pillars

from acceptance.fixtures import REASON_NO_FIT, Fixture

# 现网输出统一 round(..., 3)，比较 slack 与之同量级。
SLACK = 1e-3


@dataclass
class AllocView:
    """引擎对象与 API JSON 的统一投影——两种来源、同一形状，断言只写一遍。"""
    placements: list[dict]      # vendor_id, vendor_name, start_m, end_m, width_m
    rejected: list[dict]        # vendor_id, vendor_name, width_m, reason
    free_spans: list[tuple[float, float]]


def view_from_engine(result) -> AllocView:
    return AllocView(
        placements=[
            {"vendor_id": p.vendor_id, "vendor_name": p.vendor_name,
             "start_m": p.start_m, "end_m": p.end_m, "width_m": p.width_m}
            for p in result.placements
        ],
        rejected=[
            {"vendor_id": r.vendor_id, "vendor_name": r.vendor_name,
             "width_m": r.width_m, "reason": r.reason}
            for r in result.rejected
        ],
        free_spans=[(a, b) for a, b in result.free_spans],
    )


def view_from_api(payload: dict) -> AllocView:
    return AllocView(
        placements=[dict(p) for p in payload["placements"]],
        rejected=[dict(r) for r in payload["rejected"]],
        free_spans=[(s["start_m"], s["end_m"]) for s in payload["free_spans"]],
    )


def _fail(fx_name: str, what: str, expected, actual) -> None:
    raise AssertionError(
        f"[夹具={fx_name}] {what}\n  期望: {expected}\n  实际: {actual}"
    )


# ---------------------------------------------------------------- 两边一致

def assert_views_same_conclusion(fx_name: str, engine_view: AllocView, api_view: AllocView) -> None:
    """同一夹具：引擎直喂与现网入口 /api/allocate/run 的结论必须逐字段一致。"""
    if engine_view.placements != api_view.placements:
        _fail(fx_name, "线上与断言分叉（placements）", engine_view.placements, api_view.placements)
    if engine_view.rejected != api_view.rejected:
        _fail(fx_name, "线上与断言分叉（rejected）", engine_view.rejected, api_view.rejected)
    if engine_view.free_spans != api_view.free_spans:
        _fail(fx_name, "线上与断言分叉（free_spans）", engine_view.free_spans, api_view.free_spans)


# ---------------------------------------------------------------- 不变量

def assert_pairwise_disjoint(fx_name: str, view: AllocView) -> None:
    """不变量①：放置两两不相交（贴齐相邻允许）。"""
    spans = sorted((p["start_m"], p["end_m"], p["vendor_name"]) for p in view.placements)
    for (a0, a1, na), (b0, b1, nb) in zip(spans, spans[1:]):
        if b0 < a1 - SLACK:
            _fail(fx_name, "放置两两不相交",
                  f"{na}[{a0},{a1}] 与 {nb}[{b0},{b1}] 不得重叠",
                  f"重叠 {a1 - b0:.6f}m")


def assert_tiling_available(fx_name: str, view: AllocView,
                            available: list[tuple[float, float]]) -> None:
    """不变量②：放置 ∪ 剩余空档 恰好铺满可用开区间。

    三条子句合取即"铺满"：每片都落在某条可用开区间内、片与片互不重叠、
    片总长 == 可用开区间总长。
    """
    pieces = ([(p["start_m"], p["end_m"], f"放置:{p['vendor_name']}") for p in view.placements]
              + [(a, b, f"剩余空档:{a}~{b}") for a, b in view.free_spans])
    for a, b, label in pieces:
        if not any(a >= s - SLACK and b <= t + SLACK for s, t in available):
            _fail(fx_name, "放置与剩余铺满可用开区间（片须落在可用开区间内）",
                  f"每片 ⊆ 可用开区间 {available}", f"{label} = ({a}, {b}) 越界")
    ordered = sorted(pieces)
    for (a0, a1, la), (b0, b1, lb) in zip(ordered, ordered[1:]):
        if b0 < a1 - SLACK:
            _fail(fx_name, "放置与剩余铺满可用开区间（片间不得重叠）",
                  f"{la} 与 {lb} 不得重叠", f"重叠 {a1 - b0:.6f}m")
    piece_total = sum(b - a for a, b, _ in pieces)
    avail_total = sum(t - s for s, t in available)
    if abs(piece_total - avail_total) > SLACK * max(1, len(pieces)):
        _fail(fx_name, "放置与剩余铺满可用开区间（总长须相等）",
              f"片总长 == 可用开区间总长 {avail_total}",
              f"片总长 {piece_total}，差 {piece_total - avail_total:+.6f}m")


def assert_no_forbidden_intrusion(fx_name: str, view: AllocView,
                                  available: list[tuple[float, float]], width_m: float) -> None:
    """不变量③：放置不侵入禁入并集（[0, 街宽] 减去可用开区间）。"""
    forbidden: list[tuple[float, float]] = []
    cursor = 0.0
    for s, t in available:
        if s > cursor:
            forbidden.append((cursor, s))
        cursor = t
    if cursor < width_m:
        forbidden.append((cursor, width_m))
    for p in view.placements:
        for f0, f1 in forbidden:
            overlap = min(p["end_m"], f1) - max(p["start_m"], f0)
            if overlap > SLACK:
                _fail(fx_name, "放置不侵入禁入并集",
                      f"{p['vendor_name']}[{p['start_m']},{p['end_m']}] 与禁入区 ({f0},{f1}) 不得相交",
                      f"侵入 {overlap:.6f}m")


def assert_rejection_consistency(fx_name: str, view: AllocView, fx: Fixture,
                                 available: list[tuple[float, float]]) -> None:
    """不变量④：拒因与"未能落入可用开区间"一致。

    - 每条拒单必须带非空拒因；
    - 被拒后空档只缩不增，故被拒摊的"摊宽(+净距)"必须放不进结果里
      每一条剩余空档（容差 SLACK，与输出舍入同量级）；
    - 反向：连初始可用开区间都放不下的摊，必须出现在拒单里——
      无净距时摊宽 == 空档长可贴齐（另锁①，严格超过才算放不下）；
      有净距时摊宽+净距 == 空档长贴齐即拒（另锁②，贴齐就算放不下）；
    - 放置与拒单不得交集，且并集 == 夹具全部摊贩（结论完整）。
    """
    clearance = fx.clearance_m if fx.clearance_m and fx.clearance_m > 0 else None
    for r in view.rejected:
        if not (isinstance(r.get("reason"), str) and r["reason"].strip()):
            _fail(fx_name, "拒因与未能落入可用开区间一致（拒单须带非空拒因）",
                  "非空拒因", f"{r['vendor_name']} 的拒因 = {r.get('reason')!r}")
        limit = r["width_m"] + (clearance or 0.0)
        for a, b in view.free_spans:
            if b - a > limit + SLACK:
                _fail(fx_name, "拒因与未能落入可用开区间一致（被拒者须放不进所有剩余空档）",
                      f"{r['vendor_name']} 需 {limit}m，放不进所有剩余空档",
                      f"剩余空档 ({a},{b}) 长 {b - a:.3f}m 可容")
    max_avail = max((t - s for s, t in available), default=0.0)
    rejected_ids = {r["vendor_id"] for r in view.rejected}
    placed_ids = {p["vendor_id"] for p in view.placements}
    for v in fx.vendors:
        need = v.stall_width_m + (clearance or 0.0)
        hopeless = max_avail - need <= SLACK if clearance else need > max_avail + SLACK
        if hopeless and v.id not in rejected_ids:
            _fail(fx_name, "拒因与未能落入可用开区间一致（放不进可用开区间者必须被拒）",
                  f"{v.name} 需 {need}m（净距 {clearance or 0}）出现在拒单",
                  f"拒单 = {[r['vendor_name'] for r in view.rejected]}")
    if placed_ids & rejected_ids:
        _fail(fx_name, "拒因与未能落入可用开区间一致（放置与拒单不得交集）",
              "放置 ∩ 拒单 == ∅", f"交集 vendor_id = {sorted(placed_ids & rejected_ids)}")
    all_ids = {v.id for v in fx.vendors}
    if placed_ids | rejected_ids != all_ids:
        _fail(fx_name, "拒因与未能落入可用开区间一致（每个摊贩恰有一条结论）",
              f"放置 ∪ 拒单 == {sorted(all_ids)}",
              f"放置 {sorted(placed_ids)} ∪ 拒单 {sorted(rejected_ids)}")


def assert_placements_carry_no_reason(fx_name: str, view: AllocView) -> None:
    """不变量⑤：成功放置不得带拒因。"""
    for p in view.placements:
        if p.get("reason"):
            _fail(fx_name, "成功放置不得带拒因",
                  f"{p['vendor_name']} 无拒因", f"reason = {p['reason']!r}")


# ---------------------------------------------------------------- 对账

def assert_matches_fixture(fx: Fixture, view: AllocView) -> None:
    """对账：现网结论必须与夹具锁死的期望逐字一致。"""
    actual_names = [p["vendor_name"] for p in view.placements]
    expect_names = [e.name for e in fx.expect_placements]
    if actual_names != expect_names:
        _fail(fx.name, "对账：落位名单与顺序", expect_names, actual_names)
    for expect, actual in zip(fx.expect_placements, view.placements):
        if abs(actual["start_m"] - expect.start_m) > SLACK or abs(actual["end_m"] - expect.end_m) > SLACK:
            _fail(fx.name, f"对账：{expect.name} 落位区间",
                  f"[{expect.start_m}, {expect.end_m}]",
                  f"[{actual['start_m']}, {actual['end_m']}]")
    actual_rejected = {r["vendor_name"]: r["reason"] for r in view.rejected}
    if set(actual_rejected) != set(fx.expect_rejected):
        _fail(fx.name, "对账：拒单名单", sorted(fx.expect_rejected), sorted(actual_rejected))
    for name, expect_reason in fx.expect_rejected.items():
        actual_reason = actual_rejected.get(name)
        if expect_reason is not None and actual_reason != expect_reason:
            _fail(fx.name, f"对账：{name} 拒因", expect_reason, actual_reason)
        if expect_reason is None and not (isinstance(actual_reason, str) and actual_reason.strip()):
            _fail(fx.name, f"对账：{name} 拒因", "非空拒因", actual_reason)
    if fx.forbid_gap_reason:
        for name, reason in actual_rejected.items():
            if reason == REASON_NO_FIT:
                _fail(fx.name, f"对账：{name} 拒因不得写成空档不够",
                      f"非 {REASON_NO_FIT!r} 的拒因", reason)


# ---------------------------------------------------------------- 总装

def assert_all_invariants(fx: Fixture, view: AllocView) -> None:
    """对一份现网输出跑全部不变量 + 对账。可用开区间用现网引擎现算（同一口径）。"""
    available = free_spans_from_pillars(fx.width_m, fx.pillar_dicts())
    assert_pairwise_disjoint(fx.name, view)
    assert_tiling_available(fx.name, view, available)
    assert_no_forbidden_intrusion(fx.name, view, available, fx.width_m)
    assert_rejection_consistency(fx.name, view, fx, available)
    assert_placements_carry_no_reason(fx.name, view)
    assert_matches_fixture(fx, view)
