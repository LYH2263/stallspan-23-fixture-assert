"""场景编排：同一夹具 → 现网入口 + 引擎直喂，两边结论必须一致，再逐条过不变量。

编排只做四件事：造数入库、喂现网入口、喂引擎、对账 + 不变量。
这里不写任何 if 判断可否落——每个夹具的期望结论都在 fixtures.py 里
以字面量锁死（绿仓口径），编排不对放置结果做任何分支。
"""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.models import AllocationRun, MarketDay, Pillar, Segment, Vendor
from app.services.first_fit_engine import allocate_first_fit
from app.services.seed import seed_if_empty

from acceptance import fixtures as fx_mod
from acceptance.fixtures import Fixture
from acceptance.invariants import (
    AllocView,
    assert_all_invariants,
    assert_views_same_conclusion,
    view_from_api,
    view_from_engine,
)


def _persist(db, fx: Fixture) -> int:
    """把夹具写进隔离库，返回街段 id——与现网入口读取的表结构完全一致。"""
    day = MarketDay(name=f"验收-{fx.name}", day=date(2026, 10, 1))
    db.add(day)
    db.flush()
    seg = Segment(market_day_id=day.id, name=fx.name, width_m=fx.width_m)
    db.add(seg)
    db.flush()
    for p in fx.pillars:
        db.add(Pillar(segment_id=seg.id, position_m=p.position_m, thickness_m=p.thickness_m))
    for v in fx.vendors:
        db.add(Vendor(id=v.id, market_day_id=day.id, name=v.name,
                      stall_width_m=v.stall_width_m, priority=v.priority))
    db.commit()
    return seg.id


def _engine_view(fx: Fixture, extra_kwargs: dict | None = None) -> AllocView:
    """引擎直喂：现网入口内部调用的同一个 allocate_first_fit。"""
    result = allocate_first_fit(fx.width_m, fx.vendor_dicts(), fx.pillar_dicts(),
                                **(extra_kwargs or {}))
    return view_from_engine(result)


def _api_view(client, segment_id: int) -> AllocView:
    """现网入口：POST /api/allocate/run。"""
    resp = client.post("/api/allocate/run", params={"segment_id": segment_id})
    assert resp.status_code == 200, f"现网入口拒绝夹具: {resp.status_code} {resp.text}"
    return view_from_api(resp.json())


def _run_both_sides(fx: Fixture, db, client) -> None:
    segment_id = _persist(db, fx)
    engine_view = _engine_view(fx)
    api_view = _api_view(client, segment_id)
    assert_views_same_conclusion(fx.name, engine_view, api_view)
    assert_all_invariants(fx, engine_view)
    assert_all_invariants(fx, api_view)


# ---------------------------------------------------------------- 合成夹具

SYNTHETIC = [
    fx_mod.fx_exact_fit_abut,        # 另锁①：摊宽 == 空档长可贴齐
    fx_mod.fx_full_then_reject,      # 贴齐铺满后拒
    fx_mod.fx_pillar_split_reject,   # 挡柱切段拒大摊
    fx_mod.fx_priority_then_id_order,  # 处理序：priority 升序、同级按 id
]


@pytest.mark.parametrize("build", SYNTHETIC, ids=lambda b: b().name)
def test_live_entry_and_engine_same_fixture_same_conclusion(build, db, client):
    _run_both_sides(build(), db, client)


# ---------------------------------------------------------------- 绿仓种子

def test_green_seed_placements_match_golden(db, client):
    """夹具用绿仓种子起算：放置须与绿仓一致；运行行数只在隔离库 +1。"""
    seed_if_empty(db)  # 生产种子函数，夹具输入即种子落库的行
    seg = db.scalars(select(Segment)).one()
    vendors = db.scalars(select(Vendor).where(Vendor.market_day_id == seg.market_day_id)).all()
    pillars = db.scalars(select(Pillar).where(Pillar.segment_id == seg.id)).all()
    fx = fx_mod.fx_green_seed(
        width_m=seg.width_m,
        vendors=[{"id": v.id, "name": v.name, "stall_width_m": v.stall_width_m,
                  "priority": v.priority} for v in vendors],
        pillars=[{"position_m": p.position_m, "thickness_m": p.thickness_m} for p in pillars],
    )
    runs_before = db.scalar(select(func.count()).select_from(AllocationRun))
    engine_view = _engine_view(fx)
    api_view = _api_view(client, seg.id)
    assert_views_same_conclusion(fx.name, engine_view, api_view)
    assert_all_invariants(fx, engine_view)
    assert_all_invariants(fx, api_view)
    runs_after = db.scalar(select(func.count()).select_from(AllocationRun))
    assert runs_after == runs_before + 1, (
        f"[夹具={fx.name}] 现网入口的运行记录必须落在隔离库\n"
        f"  期望: {runs_before} + 1\n  实际: {runs_after}"
    )


# ---------------------------------------------------------------- 另锁②：净距（能力门控）

def test_clearance_lock_follows_fixture_value(db):
    """净距 0.5 贴齐拒；改净距为 0 后必须按新值重算（禁止吃旧净距缓存）。

    现网引擎当前不支持净距 → 整锁休眠（skip）；一旦 allocate_first_fit
    接受净距参数，本锁自动生效，无需改测例。
    """
    param = fx_mod.engine_clearance_param()
    if param is None:  # 能力探测分支，与"可否落"无关
        pytest.skip("现网引擎未支持净距：另锁②休眠，支持后自动生效")
    for fx in (fx_mod.fx_clearance_half_abut(), fx_mod.fx_clearance_zero_same_stall()):
        view = _engine_view(fx, {param: fx.clearance_m})
        assert_all_invariants(fx, view)
