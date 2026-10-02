"""验收场景编排：声明式场景表 + 逐场景跑同一组不变量。

编排层不写任何"该摊主能否落下"的 if 判断——成败名单与坐标都在
fixtures.Scenario 里声明；这里只负责把同一份夹具喂给现网入口
POST /api/allocate/run、把快照直喂引擎函数，并依次调用 invariants。
"""
from __future__ import annotations

import inspect
import os

# 必须在 import 任何 app.* 之前：让生产 settings 指向 sqlite，验收绝不连 postgres。
# 真正使用的是 fixtures.db_engine 的内存库；此处仅为让 app.database 模块级
# create_engine（database.py:6）不抓取 psycopg2。
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SEED_ON_EMPTY", "false")

import pytest
from sqlalchemy import func, select

from app.database import Base
from app.models.models import AllocationRun, Pillar, Vendor
from app.services.first_fit_engine import allocate_first_fit

# 同目录三文件以顶层模块互相导入（目录无 __init__.py，pytest 将其置于 sys.path 前部）。
from fixtures import (  # noqa: E402
    REJECT_REASON,
    clearance_probe,
    client,
    db_engine,
    db_session,
    dict_snapshot,
    exact_flush,
    green_seed,
    oversized_reject,
    packed_layout,
    pillar_edge_flush,
    priority_order,
)
import invariants as inv  # noqa: E402

API_RUN = "/api/allocate/run?segment_id={seg}"
API_LATEST = "/api/allocate/latest?segment_id={seg}"

# 普通场景表：每个场景都过全量不变量 + HTTP↔引擎同口径硬闸门。
SCENARIOS = [
    green_seed(),
    exact_flush(),
    pillar_edge_flush(),
    oversized_reject(),
    packed_layout(),
    priority_order(),
]


def _run_online(scenario, db_session, client) -> dict:
    """同一份夹具：落库（生产 ORM）→ 打现网入口。返回 JSON 结果。"""
    scenario.loader(db_session)
    db_session.commit()
    resp = client.post(API_RUN.format(seg=scenario.segment_id))
    assert resp.status_code == 200, (
        f"夹具[{scenario.name}] 现网入口非 200：{resp.status_code} {resp.text!r}"
    )
    return resp.json()


def _assert_all_invariants(scenario, result, width_m, vendors, pillars) -> None:
    name = scenario.name
    # 1/2/3：几何三大不变量。
    inv.assert_pairwise_disjoint(name, result)
    inv.assert_tiles_open_spans(name, result, width_m, pillars)
    inv.assert_no_forbidden_intrusion(name, result, width_m, pillars)
    # 4：拒因 ⇔ 未能落入可用开区间（含理由逐字匹配）。
    inv.assert_rejection_semantics(name, result, width_m, vendors, pillars,
                                   scenario.reject_reason)
    # 5：成功放置不得带拒因。
    inv.assert_accepted_has_no_reason(name, result)
    # 7：同一夹具，线上入口与引擎直调两边结论必须一致；分叉即废。
    inv.assert_http_engine_agree(name, result, width_m, vendors, pillars)
    # 6：场景声明了精确基线时逐坐标锁定。
    inv.assert_exact_baseline(name, result, scenario)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenario_invariants(scenario, db_session, client):
    result = _run_online(scenario, db_session, client)
    width_m, vendors, pillars = dict_snapshot(db_session, scenario.segment_id)
    _assert_all_invariants(scenario, result, width_m, vendors, pillars)


# --------------------------------------------------------------------------- #
# 绿仓专项：生产 seed 起算，放置须与绿仓基线一致；测例不得改写种子成功行数。
# --------------------------------------------------------------------------- #
def test_green_seed_baseline_and_run_rows(db_session, client):
    scenario = green_seed()
    name = scenario.name

    result = _run_online(scenario, db_session, client)
    width_m, vendors, pillars = dict_snapshot(db_session, scenario.segment_id)

    # 几何/口径全套 + 6 放置 1 拒因的精确坐标基线。
    _assert_all_invariants(scenario, result, width_m, vendors, pillars)

    # 种子库实体行数与"成功运行行数"在测例前后不变。
    n_vendors = db_session.scalar(select(func.count()).select_from(Vendor))
    n_pillars = db_session.scalar(select(func.count()).select_from(Pillar))
    assert (n_vendors, n_pillars) == (7, 2), (
        f"夹具[{name}] 绿仓种子实体行数被改写 期望=(7,2) 实际={(n_vendors, n_pillars)}"
    )
    n_runs = db_session.scalar(
        select(func.count()).select_from(AllocationRun)
        .where(AllocationRun.segment_id == scenario.segment_id)
    )
    assert n_runs == 1, f"夹具[{name}] 首次分配落库行数 期望=1 实际={n_runs}"

    # 再跑一次：多一行 run，但成功放置仍是绿仓基线的 6 行（不许被测例改掉）。
    second = client.post(API_RUN.format(seg=scenario.segment_id))
    assert second.status_code == 200
    second_json = second.json()
    inv.assert_exact_baseline(name, second_json, scenario)
    n_runs = db_session.scalar(
        select(func.count()).select_from(AllocationRun)
        .where(AllocationRun.segment_id == scenario.segment_id)
    )
    assert n_runs == 2, f"夹具[{name}] 再次分配落库行数 期望=2 实际={n_runs}"

    # latest 取到的就是最新一次落库结果。
    latest = client.get(API_LATEST.format(seg=scenario.segment_id))
    assert latest.status_code == 200
    latest_json = latest.json()
    assert latest_json["id"] == second_json["id"], (
        f"夹具[{name}] latest run id 期望={second_json['id']} 实际={latest_json['id']}"
    )
    inv.assert_exact_baseline(name, latest_json, scenario)


# --------------------------------------------------------------------------- #
# 无历史 run 时 GET /latest 内联现算并落 1 行（allocate.py:31-32）。
# --------------------------------------------------------------------------- #
def test_latest_triggers_run_when_no_history(db_session, client):
    scenario = green_seed()
    scenario.loader(db_session)
    db_session.commit()

    before = db_session.scalar(select(func.count()).select_from(AllocationRun))
    assert before == 0, f"夹具[{scenario.name}] 前置 run 行数 期望=0 实际={before}"

    resp = client.get(API_LATEST.format(seg=scenario.segment_id))
    assert resp.status_code == 200, (
        f"夹具[{scenario.name}] latest 非 200：{resp.status_code} {resp.text!r}"
    )
    after = db_session.scalar(select(func.count()).select_from(AllocationRun))
    assert after == 1, f"夹具[{scenario.name}] latest 内联落库行数 期望=1 实际={after}"

    result = resp.json()
    width_m, vendors, pillars = dict_snapshot(db_session, scenario.segment_id)
    # 现算结果同样满足全部几何不变量且与引擎直调一致。
    inv.assert_pairwise_disjoint(scenario.name, result)
    inv.assert_tiles_open_spans(scenario.name, result, width_m, pillars)
    inv.assert_no_forbidden_intrusion(scenario.name, result, width_m, pillars)
    inv.assert_accepted_has_no_reason(scenario.name, result)
    inv.assert_http_engine_agree(scenario.name, result, width_m, vendors, pillars)
    inv.assert_exact_baseline(scenario.name, result, scenario)


# --------------------------------------------------------------------------- #
# 条件锁：净距。先探测现网引擎是否具备净距形参；不具备则显式 skip 并上报，
# 绝不伪造一个只服务测例的"放宽/加距引擎"。一旦未来引擎支持净距，下面的
# 强制断言自动生效。
# --------------------------------------------------------------------------- #
_CLEARANCE_PARAM_NAMES = {
    "clearance", "clearance_m", "gap", "gap_m", "spacing", "spacing_m",
    "margin", "margin_m", "净距",
}


def _clearance_kwarg():
    params = inspect.signature(allocate_first_fit).parameters
    for cand in _CLEARANCE_PARAM_NAMES:
        if cand in params:
            return cand
    return None


def test_clearance_lock_if_supported(db_session):
    param = _clearance_kwarg()
    if param is None:
        pytest.skip(
            "现网不支持净距：allocate_first_fit 无 clearance 形参、入口无净距口径；"
            "条件锁（0.5 贴齐拒 / 净距拒因不得写成空档不够 / 改值跟新值）跳过并上报"
        )

    # 支持时：两档净距用两个独立夹具，断言只读各自 scenario.clearance，
    # 不持有任何跨场景的净距缓存。
    half = clearance_probe(0.5)
    quarter = clearance_probe(0.25)

    outcomes = {}
    for scenario in (half, quarter):
        scenario.loader(db_session)
        db_session.commit()
        width_m, vendors, pillars = dict_snapshot(db_session, scenario.segment_id)
        result = allocate_first_fit(width_m, vendors, pillars,
                                   **{param: scenario.clearance})
        placed = {p.vendor_id for p in result.placements}
        rejected = {x.vendor_id: x.reason for x in result.rejected}
        outcomes[scenario.clearance] = (placed, rejected)

        # 与夹具声明的成败名单一致（期望值按本场景新净距现算）。
        assert placed == set(scenario.expected_accepted_ids), (
            f"夹具[{scenario.name}] 成功名单 期望={sorted(scenario.expected_accepted_ids)} "
            f"实际={sorted(placed)}（净距须取本场景值 {scenario.clearance}）"
        )
        assert set(rejected) == set(scenario.expected_rejected_ids), (
            f"夹具[{scenario.name}] 拒单名单 期望={sorted(scenario.expected_rejected_ids)} "
            f"实际={sorted(rejected)}"
        )
        # 净距语义的拒因不得写成"空档不够"那一条。
        for vid in scenario.clearance_rejected_ids:
            reason = rejected.get(vid)
            assert reason and reason != REJECT_REASON, (
                f"夹具[{scenario.name}] vendor {vid} 净距贴齐拒因 "
                f"期望=区别于空档不够的净距语义拒因 实际={reason!r}"
            )
        # 每档夹具之间整库重建，两档互不串味，也不残留任何旧净距状态。
        db_session.rollback()
        Base.metadata.drop_all(bind=db_session.bind)
        Base.metadata.create_all(bind=db_session.bind)

    # 改净距夹具后结论必须跟新值：同一 9.7m 摊，0.5 档拒、0.25 档放。
    placed_half, rejected_half = outcomes[0.5]
    placed_quarter, rejected_quarter = outcomes[0.25]
    assert 82 in rejected_half and 82 in placed_quarter, (
        "夹具[clearance-probe] 改净距后未跟新值（疑似吃旧净距缓存）："
        f"0.5 档 vendor82 期望=拒 实际={'拒' if 82 in rejected_half else '放'}；"
        f"0.25 档 vendor82 期望=放 实际={'放' if 82 in placed_quarter else '拒'}"
    )
    # 摊宽恰等于空档长（贴齐）在任何正净距下都必拒。
    assert 81 in rejected_half and 81 in rejected_quarter, (
        "夹具[clearance-probe] 贴齐摊 vendor81 期望=两档均拒 "
        f"实际=0.5档{'拒' if 81 in rejected_half else '放'},"
        f"0.25档{'拒' if 81 in rejected_quarter else '放'}"
    )
