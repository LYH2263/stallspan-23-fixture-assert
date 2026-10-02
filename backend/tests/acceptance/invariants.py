"""验收不变量断言：只校验几何与口径，不产出自己的分配方案。

所有判定都与现网同口径：
- 可用开区间直接复用生产函数 free_spans_from_pillars
  （backend/app/services/first_fit_engine.py:26），不另写空档引擎；
- 可放入判定的容差必须等于生产容差 first_fit_engine.py:65；
- 残段过滤阈值必须等于生产阈值 first_fit_engine.py:50/:74。
线上代码与这里的常量/结论一旦分叉，HTTP↔引擎对账与铺平断言立即变红。

每个断言首参均为夹具名，失败信息统一为：
    夹具[<name>] <what> 期望=<...> 实际=<...>
"""
from __future__ import annotations

from typing import Iterable

# 与生产代码同口径（first_fit_engine.py:65 / :50,:74）。若生产改了这里没跟，
# 几何断言与双向对账会兜爆，而不是悄悄放宽。
FIT_TOL = 1e-9   # avail + FIT_TOL >= need 才算放得下
GAP_EPS = 1e-6   # 残段长度 <= GAP_EPS 被生产过滤
COORD_EPS = 1e-6  # 贴齐/坐标比对容差（输出已 round 3 位）


class AcceptanceError(AssertionError):
    """带夹具名/期望/实际的验收失败。"""


def _fail(fixture_name: str, what: str, expected, actual) -> None:
    raise AcceptanceError(f"夹具[{fixture_name}] {what} 期望={expected!r} 实际={actual!r}")


def _approx(a: float, b: float, eps: float = COORD_EPS) -> bool:
    return abs(a - b) <= eps


# --------------------------------------------------------------------------- #
# 归一化：HTTP 响应（去 id/segment/pillars 包装）与 result_to_dict 同形。
# --------------------------------------------------------------------------- #
def _payload(result: dict) -> dict:
    return {
        "placements": [
            (p["vendor_id"], float(p["start_m"]), float(p["end_m"]))
            for p in result["placements"]
        ],
        "rejected": [
            (r["vendor_id"], r["reason"]) for r in result["rejected"]
        ],
        "free_spans": [
            (round(float(s["start_m"]), 3), round(float(s["end_m"]), 3))
            for s in result["free_spans"]
        ],
    }


def _blocked_union(width_m: float, pillars: list[dict]) -> list[tuple[float, float]]:
    """禁入并集：独立按 first_fit_engine.py:29-41 同规则展开合并挡柱。

    这是断言侧的独立参照（用于侵入检查），不是放置引擎：不决定摊主落位。
    """
    blocked = []
    for p in pillars:
        half = p.get("thickness_m", 0.4) / 2.0
        lo = max(0.0, p["position_m"] - half)
        hi = min(width_m, p["position_m"] + half)
        if hi > lo:
            blocked.append((lo, hi))
    blocked.sort()
    merged: list[list[float]] = []
    for lo, hi in blocked:
        if not merged or lo > merged[-1][1]:
            merged.append([lo, hi])
        else:
            merged[-1][1] = max(merged[-1][1], hi)
    return [(a, b) for a, b in merged]


# --------------------------------------------------------------------------- #
# 不变量 1：放置两两不相交（端点贴齐允许）。
# --------------------------------------------------------------------------- #
def assert_pairwise_disjoint(fixture_name: str, result: dict) -> None:
    placements = sorted(_payload(result)["placements"], key=lambda t: (t[1], t[2]))
    for vid, start, end in placements:
        if end <= start:
            _fail(fixture_name, f"放置(vendor {vid})非正长度区间", f"end>start", (start, end))
    for (id_a, sa, ea), (id_b, sb, eb) in zip(placements, placements[1:]):
        if ea > sb + COORD_EPS:
            _fail(fixture_name,
                  f"放置 vendor {id_a} 与 vendor {id_b} 相交",
                  f"{id_a}.end <= {id_b}.start（可贴齐）",
                  (f"{id_a}:[{sa},{ea}]", f"{id_b}:[{sb},{eb}]"))


# --------------------------------------------------------------------------- #
# 不变量 2：放置 + 残余空档无缝无重叠铺满每条可用开区间。
# --------------------------------------------------------------------------- #
def assert_tiles_open_spans(fixture_name: str, result: dict,
                            width_m: float, pillars: list[dict]) -> None:
    from app.services.first_fit_engine import free_spans_from_pillars

    payload = _payload(result)
    placements = payload["placements"]
    actual_tails = set(payload["free_spans"])
    expected_tails: set[tuple[float, float]] = set()

    for a, b in free_spans_from_pillars(width_m, pillars):
        inside = sorted(
            (p for p in placements if p[1] >= a - COORD_EPS and p[2] <= b + COORD_EPS),
            key=lambda t: t[1],
        )
        cursor = a
        for vid, start, end in inside:
            if not _approx(start, cursor):
                _fail(fixture_name,
                      f"空档({a},{b})内 vendor {vid} 起点未无缝衔接（存在缝/重叠）",
                      cursor, start)
            cursor = end
        remain_len = b - cursor
        if remain_len > GAP_EPS:
            expected_tails.add((round(cursor, 3), round(b, 3)))
        # 残档 <= GAP_EPS 时生产会过滤（first_fit_engine.py:74），不得出现在结果里。

    if actual_tails != expected_tails:
        _fail(fixture_name, "放置与残余铺满可用开区间后的残段集合",
              sorted(expected_tails), sorted(actual_tails))


# --------------------------------------------------------------------------- #
# 不变量 3：放置不侵入禁入并集，且不越街段 [0, width]。
# --------------------------------------------------------------------------- #
def assert_no_forbidden_intrusion(fixture_name: str, result: dict,
                                  width_m: float, pillars: list[dict]) -> None:
    blocked = _blocked_union(width_m, pillars)
    for vid, start, end in _payload(result)["placements"]:
        if start < -COORD_EPS or end > width_m + COORD_EPS:
            _fail(fixture_name, f"放置 vendor {vid} 越出街段边界",
                  f"[0,{width_m}]", (start, end))
        for lo, hi in blocked:
            intrudes = not (end <= lo + COORD_EPS or start >= hi - COORD_EPS)
            if intrudes:
                _fail(fixture_name,
                      f"放置 vendor {vid} 侵入禁入区间[{lo},{hi}]",
                      f"end<={lo} 或 start>={hi}（可贴齐）", (start, end))


# --------------------------------------------------------------------------- #
# 不变量 4：拒因 ⇔ 未能落入任何可用开区间；成功的不得出现在拒单。
# 复用生产空档函数与生产容差做成员判定（不放宽、不另造方案）。
# --------------------------------------------------------------------------- #
def assert_rejection_semantics(fixture_name: str, result: dict,
                               width_m: float, vendors: list[dict],
                               pillars: list[dict], reason_text: str) -> None:
    from app.services.first_fit_engine import free_spans_from_pillars

    remain = [[a, b] for a, b in free_spans_from_pillars(width_m, pillars)]
    expected_placed: dict[int, tuple[float, float]] = {}
    expected_rejected: dict[int, str] = {}

    for v in sorted(vendors, key=lambda x: (x.get("priority", 1), x["id"])):
        need = float(v["stall_width_m"])
        home = None
        for span in remain:  # 纯成员判定：该时刻是否存在满足生产容差的空档
            if (span[1] - span[0]) + FIT_TOL >= need:
                home = span
                break
        if home is None:
            expected_rejected[v["id"]] = reason_text
        else:
            start, end = home[0], home[0] + need
            expected_placed[v["id"]] = (round(start, 3), round(end, 3))
            home[0] = end

    payload = _payload(result)
    actual_placed = {vid: (s, e) for vid, s, e in payload["placements"]}
    actual_rejected = dict(payload["rejected"])

    if set(actual_placed) != set(expected_placed):
        _fail(fixture_name, "成功/拒绝的摊主集合（拒因须与落不进可用开区间一致）",
              {"placed": sorted(expected_placed), "rejected": sorted(expected_rejected)},
              {"placed": sorted(actual_placed), "rejected": sorted(actual_rejected)})
    for vid, (s, e) in expected_placed.items():
        as_, ae_ = actual_placed[vid]
        if not (_approx(as_, s) and _approx(ae_, e)):
            _fail(fixture_name, f"vendor {vid} 落位", (s, e), (as_, ae_))
    for vid, reason in expected_rejected.items():
        if actual_rejected.get(vid) != reason:
            _fail(fixture_name, f"vendor {vid} 拒因逐字匹配",
                  reason, actual_rejected.get(vid))


# --------------------------------------------------------------------------- #
# 不变量 5：成功放置不得带拒因；placed/rejected 不相交。
# --------------------------------------------------------------------------- #
def assert_accepted_has_no_reason(fixture_name: str, result: dict) -> None:
    placed_ids = {p["vendor_id"] for p in result["placements"]}
    rejected_ids = {r["vendor_id"] for r in result["rejected"]}
    both = placed_ids & rejected_ids
    if both:
        _fail(fixture_name, "同一摊主不得既放置又带拒因", set(), sorted(both))
    for p in result["placements"]:
        if "reason" in p and p["reason"]:
            _fail(fixture_name, f"成功放置 vendor {p['vendor_id']} 不得携带拒因",
                  None, p.get("reason"))
    for r in result["rejected"]:
        if not r.get("reason"):
            _fail(fixture_name, f"被拒 vendor {r['vendor_id']} 必须给出拒因",
                  "非空拒因", r.get("reason"))


# --------------------------------------------------------------------------- #
# 不变量 6：场景声明的精确基线（坐标/顺序/残段/成败名单）。
# --------------------------------------------------------------------------- #
def assert_exact_baseline(fixture_name: str, result: dict, scenario) -> None:
    payload = _payload(result)
    if scenario.expected_placements is not None:
        expected = [(vid, float(s), float(e)) for vid, s, e in scenario.expected_placements]
        if payload["placements"] != expected:
            _fail(fixture_name, "放置清单(vendor,start,end)及顺序",
                  expected, payload["placements"])
        if len(result["placements"]) != len(expected):
            _fail(fixture_name, "成功运行行数（种子库基线不得被改写）",
                  len(expected), len(result["placements"]))
    if scenario.expected_free_spans is not None:
        expected_spans = {(float(a), float(b)) for a, b in scenario.expected_free_spans}
        actual_spans = set(payload["free_spans"])
        if actual_spans != expected_spans:
            _fail(fixture_name, "残余空档集合",
                  sorted(expected_spans), sorted(actual_spans))
    if scenario.expected_accepted_ids is not None:
        actual_ids = [vid for vid, _, _ in payload["placements"]]
        if actual_ids != list(scenario.expected_accepted_ids):
            _fail(fixture_name, "成功名单及放置顺序",
                  list(scenario.expected_accepted_ids), actual_ids)
    if scenario.expected_rejected_ids is not None:
        actual_ids = [vid for vid, _ in payload["rejected"]]
        if actual_ids != list(scenario.expected_rejected_ids):
            _fail(fixture_name, "拒单名单及顺序",
                  list(scenario.expected_rejected_ids), actual_ids)
    # 锁拒因行数：成功名单与拒单名单互不重叠且覆盖全部摊主。
    if (scenario.expected_accepted_ids is not None
            and scenario.expected_rejected_ids is not None):
        total = len(scenario.expected_accepted_ids) + len(scenario.expected_rejected_ids)
        if len(payload["placements"]) + len(payload["rejected"]) != total:
            _fail(fixture_name, "放置+拒单总数守恒",
                  total, len(payload["placements"]) + len(payload["rejected"]))


# --------------------------------------------------------------------------- #
# 硬闸门 7：同一夹具，HTTP 现网入口与引擎函数直调两边结论必须一致。
# 线上与断言分叉即废——本断言不过，整场验收失败。
# --------------------------------------------------------------------------- #
def assert_http_engine_agree(fixture_name: str, http_result: dict,
                             width_m: float, vendors: list[dict],
                             pillars: list[dict]) -> None:
    from app.services.first_fit_engine import allocate_first_fit, result_to_dict

    engine_result = result_to_dict(allocate_first_fit(width_m, vendors, pillars))
    http_only = _payload(http_result)
    engine_only = _payload(engine_result)
    for key in ("placements", "rejected", "free_spans"):
        if http_only[key] != engine_only[key]:
            _fail(fixture_name,
                  f"线上入口与引擎直调在 {key} 上分叉（同夹具两边结论必须一致）",
                  engine_only[key], http_only[key])
