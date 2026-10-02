"""造数夹具：只造数、只声明期望，不含任何"可否落"的判断。

每个 Fixture 携带：
  - 输入（街段宽、挡柱、摊贩、净距）——同一夹具会被同时喂给现网入口
    （POST /api/allocate/run）和引擎直喂（allocate_first_fit）；
  - 期望结论（精确落位、拒单名单与拒因）——以字面量锁死的绿仓口径，
    现网行为一旦漂移，对账立即失败，这正是验收的目的。
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field

# 现网引擎的拒因原文（app/services/first_fit_engine.py 中 allocate_first_fit
# 对放不下且不跨挡柱的摊贩给出的拒因）。此处逐字引用作为金样：
# 引擎改文案 = 口径变更，对账必须跟着失败，不允许静默分叉。
REASON_NO_FIT = "无连续空档可放下且不跨越挡柱"


@dataclass
class VendorSpec:
    id: int
    name: str
    stall_width_m: float
    priority: int = 1


@dataclass
class PillarSpec:
    position_m: float
    thickness_m: float = 0.4


@dataclass
class ExpectedPlacement:
    name: str
    start_m: float
    end_m: float


@dataclass
class Fixture:
    name: str                                   # 夹具名，失败信息必带
    width_m: float
    vendors: list[VendorSpec]
    pillars: list[PillarSpec] = field(default_factory=list)
    clearance_m: float | None = None            # 净距；None = 本夹具不涉及净距
    expect_placements: list[ExpectedPlacement] = field(default_factory=list)
    # 期望拒单：摊贩名 -> 拒因；值为 None 表示"拒因非空即可"（用于净距另锁，
    # 那时拒因的具体措辞由未来的现网实现决定，但不得写成空档不够）。
    expect_rejected: dict[str, str | None] = field(default_factory=dict)
    forbid_gap_reason: bool = False             # True = 拒因不得是 REASON_NO_FIT（空档不够口径）

    def vendor_dicts(self) -> list[dict]:
        """与现网入口喂给引擎的摊贩字典同构（见 app/api/allocate.py）。"""
        return [
            {"id": v.id, "name": v.name, "stall_width_m": v.stall_width_m, "priority": v.priority}
            for v in self.vendors
        ]

    def pillar_dicts(self) -> list[dict]:
        """与现网入口喂给引擎的挡柱字典同构（见 app/api/allocate.py）。"""
        return [{"position_m": p.position_m, "thickness_m": p.thickness_m} for p in self.pillars]


# ---------------------------------------------------------------- 另锁①

def fx_exact_fit_abut() -> Fixture:
    """摊宽 == 空档长：必须贴齐落，不得拒。

    街宽 10，挡柱 @5.0 厚 0.4 → 空档 (0, 4.8) 与 (5.2, 10)。
    摊宽 4.8 恰等于左空档长，贴齐落在 [0, 4.8]。
    """
    return Fixture(
        name="摊宽等于空档长可贴齐",
        width_m=10.0,
        pillars=[PillarSpec(5.0, 0.4)],
        vendors=[VendorSpec(1, "贴齐摊", 4.8, 1)],
        expect_placements=[ExpectedPlacement("贴齐摊", 0.0, 4.8)],
    )


def fx_full_then_reject() -> Fixture:
    """首摊贴齐铺满整段，后续摊贩无档可落必须拒。"""
    return Fixture(
        name="贴齐铺满后拒",
        width_m=8.0,
        vendors=[VendorSpec(1, "满档摊", 8.0, 1), VendorSpec(2, "续摊", 1.0, 1)],
        expect_placements=[ExpectedPlacement("满档摊", 0.0, 8.0)],
        expect_rejected={"续摊": REASON_NO_FIT},
    )


# ---------------------------------------------------------------- 常规场景

def fx_pillar_split_reject() -> Fixture:
    """挡柱把街段切成三段，12m 大摊放不进任何一段必须拒。"""
    return Fixture(
        name="挡柱切段拒大摊",
        width_m=30.0,
        pillars=[PillarSpec(10.0, 0.5), PillarSpec(20.0, 0.5)],
        vendors=[VendorSpec(1, "甲摊", 4.0, 1), VendorSpec(2, "大摊", 12.0, 1)],
        expect_placements=[ExpectedPlacement("甲摊", 0.0, 4.0)],
        expect_rejected={"大摊": REASON_NO_FIT},
    )


def fx_priority_then_id_order() -> Fixture:
    """处理序锁死：priority 升序、同级按 id 升序；后来的丙挤掉先登记的甲。"""
    return Fixture(
        name="优先级同级的处理序",
        width_m=12.0,
        vendors=[
            VendorSpec(1, "甲", 6.0, 2),   # 先登记但优先级低，排最后
            VendorSpec(2, "乙", 6.0, 1),
            VendorSpec(3, "丙", 6.0, 1),
        ],
        expect_placements=[
            ExpectedPlacement("乙", 0.0, 6.0),
            ExpectedPlacement("丙", 6.0, 12.0),
        ],
        expect_rejected={"甲": REASON_NO_FIT},
    )


# ---------------------------------------------------------------- 绿仓种子

def fx_green_seed(width_m: float, vendors: list[dict], pillars: list[dict]) -> Fixture:
    """夹具从绿仓种子起算：输入就是 app.services.seed 落库的行（由编排读回传入），
    期望放置与绿仓现网结论逐字一致（金样，非手推——由现网引擎实算锁定）。
    """
    return Fixture(
        name="绿仓种子起算",
        width_m=width_m,
        vendors=[VendorSpec(v["id"], v["name"], v["stall_width_m"], v["priority"]) for v in vendors],
        pillars=[PillarSpec(p["position_m"], p["thickness_m"]) for p in pillars],
        expect_placements=[
            ExpectedPlacement("阿强烧烤", 0.0, 4.0),
            ExpectedPlacement("林记糖水", 4.0, 7.0),
            ExpectedPlacement("大碗面", 10.25, 16.25),
            ExpectedPlacement("老周水果", 20.25, 25.25),
            ExpectedPlacement("小美饰品", 7.0, 9.5),
            ExpectedPlacement("手作皮具", 16.25, 19.75),
        ],
        expect_rejected={"巨型舞台车": REASON_NO_FIT},
    )


# ---------------------------------------------------------------- 另锁②（净距，能力门控）

def engine_clearance_param() -> str | None:
    """现网引擎若支持净距，返回其参数名；否则 None。只探测签名，不实现任何逻辑。"""
    from app.services.first_fit_engine import allocate_first_fit

    for name in ("clearance_m", "clearance", "net_clearance_m"):
        if name in inspect.signature(allocate_first_fit).parameters:
            return name
    return None


def fx_clearance_half_abut() -> Fixture:
    """净距 0.5 贴齐拒：摊宽 9.5 + 净距 0.5 == 街宽 10，贴齐不算能落，必须拒；
    且拒因不得写成空档不够（forbid_gap_reason）。"""
    return Fixture(
        name="净距0.5贴齐拒",
        width_m=10.0,
        vendors=[VendorSpec(1, "净距摊", 9.5, 1)],
        clearance_m=0.5,
        expect_rejected={"净距摊": None},
        forbid_gap_reason=True,
    )


def fx_clearance_zero_same_stall() -> Fixture:
    """改净距夹具：同一摊同一条街，净距改为 0 → 必须按新值重新得出结论（落 [0, 9.5]），
    不得吃上一档净距的旧缓存。"""
    return Fixture(
        name="净距改0重算可落",
        width_m=10.0,
        vendors=[VendorSpec(1, "净距摊", 9.5, 1)],
        clearance_m=0.0,
        expect_placements=[ExpectedPlacement("净距摊", 0.0, 9.5)],
    )
