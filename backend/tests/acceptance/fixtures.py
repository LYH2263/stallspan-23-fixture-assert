"""验收造数夹具：测试 DB/HTTP 基建 + 声明式场景数据。

三文件分工：本文件只造数（场景"是什么"），invariants.py 只做几何/口径
断言（"必须满足什么"），test_scenarios.py 只做场景编排（"喂给谁、跑哪些
断言"）。任何场景都不在此判定摊主"能不能放下"——期望落位一律以数据
声明，编排层不出现落位 if 分支。

同一份夹具数据既可落库后喂现网入口 POST /api/allocate/run，也可经
dict_snapshot 按 backend/app/api/allocate.py:15-18 同字段导出直喂引擎
函数 allocate_first_fit，供 invariants.assert_http_engine_agree 对账。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models.models import MarketDay, Pillar, Segment, Vendor
from app.services.seed import seed_if_empty  # noqa: F401  （绿仓由生产 seed 起算）

# 必须逐字等于现网拒因 first_fit_engine.py:73；线上与本常量分叉即验收失败。
REJECT_REASON = "无连续空档可放下且不跨越挡柱"


# --------------------------------------------------------------------------- #
# 测试基建：sqlite 内存库 + 覆盖 get_db 的 TestClient（不进 lifespan，不碰
# postgres，也不触发 app.main 的自动 seed；绿仓数据由场景显式播种）。
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 单连接常驻，内存库在 session 间共享
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    testing_session = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)
    session = testing_session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def client(db_engine):
    from fastapi.testclient import TestClient

    from app.main import app

    testing_session = sessionmaker(bind=db_engine, autocommit=False, autoflush=False)

    def _get_test_db():
        session = testing_session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_test_db
    # 不使用 with：避免触发 lifespan（那会对真实 postgres 建表/seed）。
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 场景声明：期望全部是数据，不是落位判断。
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    name: str
    loader: Callable[[Session], None]
    segment_id: int = 1
    # 仅需要逐坐标锁定的场景给值：[(vendor_id, start_m, end_m), ...]，顺序为放置顺序。
    expected_placements: Optional[list[tuple[int, float, float]]] = None
    # [(start_m, end_m), ...] 分配后残余空档，顺序不敏感（比对时按集合）。
    expected_free_spans: Optional[list[tuple[float, float]]] = None
    expected_accepted_ids: Optional[list[int]] = None
    expected_rejected_ids: Optional[list[int]] = None
    reject_reason: str = REJECT_REASON
    # 净距条件锁专用：None 表示本场景不涉净距；非 None 时所有断言只能读这个值。
    clearance: Optional[float] = None
    # 净距锁下被"净距语义"拒绝的摊主 id（其拒因必须区别于空档不够）。
    clearance_rejected_ids: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 造数小工具（仅插入，不做任何可行性判断）。
# --------------------------------------------------------------------------- #
def _add_day_segment(session: Session, width_m: float, name: str = "验收街段") -> Segment:
    day = MarketDay(name="验收集日", day=date(2026, 10, 1))
    session.add(day)
    session.flush()
    seg = Segment(market_day_id=day.id, name=name, width_m=width_m)
    session.add(seg)
    session.flush()
    return seg


def _add_vendor(session: Session, day_id: int, vid: int, name: str,
                width_m: float, priority: int = 1) -> None:
    # id 显式给定，保证任何场景下 id 与期望表一致。
    session.add(Vendor(id=vid, market_day_id=day_id, name=name,
                       stall_width_m=width_m, priority=priority))


def _add_pillar(session: Session, seg_id: int, position_m: float,
                thickness_m: float, label: str = "挡柱") -> None:
    session.add(Pillar(segment_id=seg_id, position_m=position_m,
                       thickness_m=thickness_m, label=label))


def dict_snapshot(session: Session, segment_id: int) -> tuple[float, list[dict], list[dict]]:
    """按现网入口 allocate.py:15-18 同一字段口径导出 (width_m, vendors, pillars)。

    同一份夹具快照既能喂 HTTP（经落库），也能直喂引擎函数，杜绝两套口径。
    """
    seg = session.get(Segment, segment_id)
    pillars = [
        {"position_m": p.position_m, "thickness_m": p.thickness_m}
        for p in session.scalars(
            select(Pillar).where(Pillar.segment_id == segment_id)
        ).all()
    ]
    vendors = [
        {"id": v.id, "name": v.name, "stall_width_m": v.stall_width_m, "priority": v.priority}
        for v in session.scalars(
            select(Vendor).where(Vendor.market_day_id == seg.market_day_id)
        ).all()
    ]
    return seg.width_m, vendors, pillars


# --------------------------------------------------------------------------- #
# 场景工厂：夹具名即场景名，失败信息可直接定位。
# --------------------------------------------------------------------------- #
def green_seed() -> Scenario:
    """绿仓：生产 seed_if_empty 的周末夜市/东街段。

    基线（手算，柱子阻塞 [9.75,10.25]、[19.75,20.25]）：
    id1 0-4；id2 4-7；id5 10.25-16.25；id3 20.25-25.25；id4 7-9.5；
    id6 16.25-19.75（摊宽 3.5 恰等于空档 3.5，贴齐柱缘）；id7(12) 拒。
    """
    return Scenario(
        name="green-seed-周末夜市东街段",
        loader=lambda db: seed_if_empty(db),
        expected_placements=[
            (1, 0.0, 4.0),
            (2, 4.0, 7.0),
            (5, 10.25, 16.25),
            (3, 20.25, 25.25),
            (4, 7.0, 9.5),
            (6, 16.25, 19.75),
        ],
        expected_free_spans=[(9.5, 9.75), (25.25, 30.0)],
        expected_accepted_ids=[1, 2, 5, 3, 4, 6],
        expected_rejected_ids=[7],
    )


def exact_flush() -> Scenario:
    """摊宽等于空档长：8m 空街 5+3 恰好铺满，第二条 end 贴齐 8.0。"""
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 8.0, "贴齐空街")
        _add_vendor(db, seg.market_day_id, 1, "五米摊", 5.0, 1)
        _add_vendor(db, seg.market_day_id, 2, "三米摊", 3.0, 1)

    return Scenario(
        name="exact-flush-摊宽等于空档长",
        loader=load,
        expected_placements=[(1, 0.0, 5.0), (2, 5.0, 8.0)],
        expected_free_spans=[],
        expected_accepted_ids=[1, 2],
        expected_rejected_ids=[],
    )


def pillar_edge_flush() -> Scenario:
    """贴柱缘：柱阻塞 [5.0,5.5]，左摊 5.0 贴 5.0、右摊 4.5 起 5.5 贴 10.0。"""
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 10.0, "贴柱街段")
        _add_pillar(db, seg.id, 5.25, 0.5, "中柱")
        _add_vendor(db, seg.market_day_id, 1, "左摊", 5.0, 1)
        _add_vendor(db, seg.market_day_id, 2, "右摊", 4.5, 1)

    return Scenario(
        name="pillar-edge-flush-贴齐柱缘",
        loader=load,
        expected_placements=[(1, 0.0, 5.0), (2, 5.5, 10.0)],
        expected_free_spans=[],
        expected_accepted_ids=[1, 2],
        expected_rejected_ids=[],
    )


def oversized_reject() -> Scenario:
    """两柱各剩 4.75 空档，5.0 摊无处可落，唯一拒因逐字匹配。"""
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 10.0, "拒单街段")
        _add_pillar(db, seg.id, 5.0, 0.5)
        _add_vendor(db, seg.market_day_id, 1, "五米大摊", 5.0, 1)

    return Scenario(
        name="oversized-reject-落不进任何开区间",
        loader=load,
        expected_placements=[],
        expected_free_spans=[(0.0, 4.75), (5.25, 10.0)],
        expected_accepted_ids=[],
        expected_rejected_ids=[1],
    )


def packed_layout() -> Scenario:
    """复杂禁入并集：边界柱、相接合并柱、退化柱。

    W=12；柱：[0,0.5]（贴左边界）、[3.5,4.5] 与 [4.0,5.0]（相接/重叠合并为
    [3.5,5.0]）、[11.5,12]（贴右边界）、center=7 厚度 0（hi==lo 退化丢弃）。
    可用开区间 (0.5,3.5) 长 3、(5.0,11.5) 长 6.5；3 米与 6.5 米摊各自铺满，
    0.6 米摊无残档可落。
    """
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 12.0, "复杂柱群")
        _add_pillar(db, seg.id, 0.0, 1.0, "左边界柱")
        _add_pillar(db, seg.id, 4.0, 1.0, "合并柱A")
        _add_pillar(db, seg.id, 4.5, 1.0, "合并柱B")
        _add_pillar(db, seg.id, 12.0, 1.0, "右边界柱")
        _add_pillar(db, seg.id, 7.0, 0.0, "退化柱")
        _add_vendor(db, seg.market_day_id, 1, "三米铺满", 3.0, 1)
        _add_vendor(db, seg.market_day_id, 2, "六米半铺满", 6.5, 1)
        _add_vendor(db, seg.market_day_id, 3, "零六米落单", 0.6, 2)

    return Scenario(
        name="packed-layout-边界相接退化柱",
        loader=load,
        expected_placements=[(1, 0.5, 3.5), (2, 5.0, 11.5)],
        expected_free_spans=[],
        expected_accepted_ids=[1, 2],
        expected_rejected_ids=[3],
    )


def priority_order() -> Scenario:
    """乱序插入，放置序必须按 (priority asc, id asc)：2→3→1→4。"""
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 10.0, "优先级街段")
        _add_vendor(db, seg.market_day_id, 1, "次优先", 2.0, 2)
        _add_vendor(db, seg.market_day_id, 2, "先到A", 3.0, 1)
        _add_vendor(db, seg.market_day_id, 3, "先到B", 2.0, 1)
        _add_vendor(db, seg.market_day_id, 4, "垫底", 1.0, 3)

    return Scenario(
        name="priority-order-按优先级与id",
        loader=load,
        expected_placements=[(2, 0.0, 3.0), (3, 3.0, 5.0), (1, 5.0, 7.0), (4, 7.0, 8.0)],
        expected_free_spans=[(8.0, 10.0)],
        expected_accepted_ids=[2, 3, 1, 4],
        expected_rejected_ids=[],
    )


def clearance_probe(clearance: float) -> Scenario:
    """净距条件锁专用夹具（现网不支持净距时该锁整体 skip，不会跑到这里）。

    W=10 单空档：
      id81 宽 10.0 —— 摊宽恰好等于空档长（贴齐，残余 0）；任何 c>0 的净距
        语义下都必须拒，且拒因不得写成"空档不够"。
      id82 宽 9.7  —— 残余 0.3；c=0.5 时拒（0.3<0.5），c=0.25 时放
        （0.3>=0.25）。两档同测，断言只读本场景 clearance 值，杜绝吃旧缓存。
    被拒摊主互不挤占剩余（拒绝不消耗空档），故两个判定彼此独立。
    """
    def load(db: Session) -> None:
        seg = _add_day_segment(db, 10.0, f"净距探针-{clearance}")
        _add_vendor(db, seg.market_day_id, 81, "贴齐摊", 10.0, 1)
        _add_vendor(db, seg.market_day_id, 82, "残余零点三", 9.7, 2)

    band_accepted = 0.3 >= clearance
    return Scenario(
        name=f"clearance-probe-净距{clearance}",
        loader=load,
        clearance=clearance,
        expected_accepted_ids=[82] if band_accepted else [],
        expected_rejected_ids=[81, 82] if not band_accepted else [81],
        clearance_rejected_ids=[81, 82] if not band_accepted else [81],
        expected_free_spans=None,
    )
