"""
数据模型定义 — 储能电站日收益计算
基于《山东电力市场规则（试行）（2026年修订版）》
"""
from dataclasses import dataclass, field
from typing import List, Optional
import json


# ============================================================
# 输入数据模型
# ============================================================

@dataclass
class StorageParams:
    """储能聚合模型参数"""
    name: str = "示例储能电站"
    rated_power_mw: float = 100.0           # 额定功率 (MW)
    rated_capacity_mwh: float = 200.0        # 额定容量 (MWh)
    charging_efficiency: float = 0.92        # 充电效率
    discharging_efficiency: float = 0.92     # 放电效率
    soc_min: float = 0.10                   # SOC硬下限
    soc_max: float = 0.90                   # SOC硬上限
    agc_soc_reserve: float = 0.10           # AGC能量裕量比例(上下各预留)
    soc_soft_min: float = 0.20              # 套利放电软下限
    soc_soft_max: float = 0.80              # 套利充电软上限
    ramp_rate_mw_per_min: float = 10.0      # 爬坡速率 (MW/min)
    self_discharge_rate: float = 0.001      # 自放电率 (每小时)
    initial_soc: float = 0.50               # 初始SOC
    cycles_per_day: int = 2                 # 每日充放电循环次数
    agc_reserve_mw: float = 1.0             # AGC功率裕量 (MW)
    agc_max_capacity_mw: float = 10.0       # AGC中标容量上限 (MW)

    @classmethod
    def from_json(cls, path: str) -> "StorageParams":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class MarketInterval:
    """单个15分钟时段的市场数据"""
    time: str = "00:00"                     # 时段标签 HH:MM
    lmp_yuan_per_mwh: float = 300.0         # 日前出清LMP (元/MWh)
    win_charge_mw: float = 0.0              # 中标充电功率 (MW, 正=充电)
    win_discharge_mw: float = 0.0           # 中标放电功率 (MW, 正=放电)
    win_agc_capacity_mw: float = 0.0        # 中标AGC调频容量 (MW)
    agc_capacity_price: float = 12.0        # AGC容量报价 (元/MW)
    soc: float = 0.5                        # 该时段末SOC(日前计划轨迹)


@dataclass
class DayAheadData:
    """日前市场中标数据（96点）"""
    date: str = "2026-06-30"
    node_name: str = "节点A"
    intervals: List[MarketInterval] = field(default_factory=list)
    agc_mileage_price: float = 10.0         # AGC里程价格 (元/MW)

    @classmethod
    def from_json(cls, path: str) -> "DayAheadData":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        intervals = [MarketInterval(**it) for it in d["intervals"]]
        return cls(
            date=d["date"],
            node_name=d.get("node_name", "节点A"),
            intervals=intervals,
            agc_mileage_price=d.get("agc_mileage_price", 10.0),
        )


@dataclass
class RealTimeInterval:
    """单个15分钟时段的实时运行数据"""
    time: str = "00:00"
    rt_lmp_yuan_per_mwh: float = 300.0      # 实时LMP (元/MWh)
    # 总实际功率 (套利 + AGC)
    actual_charge_mw: float = 0.0           # 实际总充电功率 (MW, 正=充电)
    actual_discharge_mw: float = 0.0        # 实际总放电功率 (MW, 正=放电)
    # 套利部分 (仅来自日前中标计划)
    arbitrage_charge_mw: float = 0.0        # 套利充电功率 (MW)
    arbitrage_discharge_mw: float = 0.0     # 套利放电功率 (MW)
    # AGC部分 (来自AGC指令)
    agc_charge_mw: float = 0.0              # AGC充电功率 (MW, 仅当 actual_agc_mw < 0 时非零)
    agc_discharge_mw: float = 0.0           # AGC放电功率 (MW, 仅当 actual_agc_mw > 0 时非零)
    actual_agc_mw: float = 0.0              # 实际AGC调频出力 (MW, 正=放电方向, 负=充电方向)
    agc_mileage_mw: float = 0.0             # 实际调频里程 (MW)
    soc: float = 0.5                        # 该时段末SOC(实时运行轨迹)

    def __post_init__(self):
        """保证总功率 = 套利 + AGC"""
        # 如果新增字段未提供, 从旧字段反推
        if self.agc_charge_mw == 0.0 and self.agc_discharge_mw == 0.0 and self.actual_agc_mw != 0.0:
            if self.actual_agc_mw > 0:
                self.agc_discharge_mw = self.actual_agc_mw
            else:
                self.agc_charge_mw = abs(self.actual_agc_mw)
        if self.arbitrage_charge_mw == 0.0 and self.arbitrage_discharge_mw == 0.0:
            self.arbitrage_charge_mw = max(0.0, self.actual_charge_mw - self.agc_charge_mw)
            self.arbitrage_discharge_mw = max(0.0, self.actual_discharge_mw - self.agc_discharge_mw)
        # 兜底: 如果没有总功率, 由套利+AGC合成
        if self.actual_charge_mw == 0.0 and self.actual_discharge_mw == 0.0:
            self.actual_charge_mw = self.arbitrage_charge_mw + self.agc_charge_mw
            self.actual_discharge_mw = self.arbitrage_discharge_mw + self.agc_discharge_mw


@dataclass
class RealTimeData:
    """实时市场数据（96点）"""
    date: str = "2026-06-30"
    intervals: List[RealTimeInterval] = field(default_factory=list)

    @classmethod
    def from_json(cls, path: str) -> "RealTimeData":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        intervals = [RealTimeInterval(**it) for it in d["intervals"]]
        return cls(date=d["date"], intervals=intervals)


@dataclass
class AgcPerformance:
    """AGC调频性能指标"""
    date: str = "2026-06-30"
    k1_speed: float = 1.0                   # 调节速率指标
    k1_base: float = 1.0                    # 基准K1
    k2_precision: float = 1.0               # 调节精度指标
    k2_base: float = 1.0                    # 基准K2
    k3_response: float = 1.0                # 响应时间指标
    k3_base: float = 1.0                    # 基准K3
    weight_k1: float = 0.35                 # K1权重
    weight_k2: float = 0.40                 # K2权重
    weight_k3: float = 0.25                 # K3权重
    kp_avg: float = 1.0                     # 市场平均Kp

    @classmethod
    def from_json(cls, path: str) -> "AgcPerformance":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SettlementParams:
    """结算参数（分摊费率、考核标准、运行成本）"""
    # 市场结算参数
    deviation_tolerance: float = 0.03        # 偏差免考核范围 (3%)
    deviation_penalty_rate: float = 0.10     # 偏差考核惩罚系数
    transmission_fee_rate: float = 0.03      # 输配电价比例
    gov_fund_rate: float = 0.019             # 政府基金及附加
    ancillary_cost_rate: float = 0.005       # 辅助服务分摊比例
    agc_unqualified_penalty: float = 500.0   # AGC不合格一次罚款 (元)

    # 运行成本参数
    battery_degradation_per_mwh: float = 100.0   # 电池衰减成本 (元/MWh, 按充放电吞吐量)
    self_consumption_rate: float = 0.03          # 辅助用电比例 (占充电量的比例)
    daily_om_cost_yuan: float = 500.0            # 日运维固定费用 (元/天, 含人工、巡检等)

    @classmethod
    def from_json(cls, path: str) -> "SettlementParams":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ============================================================
# 输出数据模型
# ============================================================

@dataclass
class IntervalResult:
    """单个15分钟时段的结算结果"""
    time: str = "00:00"
    energy_revenue: float = 0.0              # 电能量套利 (元)
    agc_capacity_revenue: float = 0.0        # 调频容量收益 (元)
    agc_mileage_revenue: float = 0.0         # 调频里程收益 (元)
    deviation_penalty: float = 0.0           # 偏差考核扣款 (元)
    transmission_fee: float = 0.0            # 输配电费 (元)
    gov_fund_fee: float = 0.0                # 政府基金 (元)
    ancillary_fee: float = 0.0               # 辅助服务分摊 (元)
    battery_degradation_cost: float = 0.0     # 电池衰减成本 (元)
    self_consumption_cost: float = 0.0        # 辅助用电成本 (元)
    soc: float = 0.5                         # 该时段末SOC
    net_power_mw: float = 0.0                # 净功率 (放电为正, 充电为负)
    lmp: float = 0.0                         # 使用的LMP
    net_revenue: float = 0.0                 # 该时段净收益 (元)


@dataclass
class DailyResult:
    """每日总结算结果"""
    date: str = "2026-06-30"
    node_name: str = "节点A"
    storage_name: str = "示例储能电站"

    # 收益汇总
    total_energy_revenue: float = 0.0        # 电能量总收益
    total_agc_capacity_revenue: float = 0.0  # 调频容量总收益
    total_agc_mileage_revenue: float = 0.0   # 调频里程总收益
    total_gross_revenue: float = 0.0         # 总收入

    # 费用汇总
    total_deviation_penalty: float = 0.0     # 偏差考核总扣款
    total_transmission_fee: float = 0.0      # 输配电费
    total_gov_fund_fee: float = 0.0          # 政府基金
    total_ancillary_fee: float = 0.0         # 辅助服务分摊
    agc_unqualified_count: int = 0           # AGC不合格次数
    agc_unqualified_penalty: float = 0.0     # AGC不合格扣款
    total_market_cost: float = 0.0           # 市场结算总费用

    # 运行成本
    total_battery_degradation: float = 0.0   # 电池衰减总成本
    total_self_consumption: float = 0.0      # 辅助用电总成本
    total_om_cost: float = 0.0               # 运维固定费用
    total_operating_cost: float = 0.0        # 运行成本合计

    # 净收益
    total_cost: float = 0.0                  # 总费用 (市场+运行)
    net_profit: float = 0.0                  # 净利润

    # AGC性能
    kp: float = 1.0                          # 综合性能指标Kp

    # 充放电汇总
    total_charge_mwh: float = 0.0            # 总充电量
    total_discharge_mwh: float = 0.0         # 总放电量
    final_soc: float = 0.0                   # 日末SOC

    # 96点明细
    interval_results: List[IntervalResult] = field(default_factory=list)

    def to_json(self, path: str) -> None:
        """输出为JSON文件"""
        result = {
            "date": self.date,
            "node_name": self.node_name,
            "storage_name": self.storage_name,
            "summary": {
                "total_energy_revenue": round(self.total_energy_revenue, 2),
                "total_agc_capacity_revenue": round(self.total_agc_capacity_revenue, 2),
                "total_agc_mileage_revenue": round(self.total_agc_mileage_revenue, 2),
                "total_gross_revenue": round(self.total_gross_revenue, 2),
                "total_deviation_penalty": round(self.total_deviation_penalty, 2),
                "total_transmission_fee": round(self.total_transmission_fee, 2),
                "total_gov_fund_fee": round(self.total_gov_fund_fee, 2),
                "total_ancillary_fee": round(self.total_ancillary_fee, 2),
                "agc_unqualified_count": self.agc_unqualified_count,
                "agc_unqualified_penalty": round(self.agc_unqualified_penalty, 2),
                "total_market_cost": round(self.total_market_cost, 2),
                "total_battery_degradation": round(self.total_battery_degradation, 2),
                "total_self_consumption": round(self.total_self_consumption, 2),
                "total_om_cost": round(self.total_om_cost, 2),
                "total_operating_cost": round(self.total_operating_cost, 2),
                "total_cost": round(self.total_cost, 2),
                "net_profit": round(self.net_profit, 2),
                "kp": round(self.kp, 4),
                "total_charge_mwh": round(self.total_charge_mwh, 2),
                "total_discharge_mwh": round(self.total_discharge_mwh, 2),
                "final_soc": round(self.final_soc, 4),
            },
            "intervals": [
                {
                    "time": r.time,
                    "soc": round(r.soc, 4),
                    "net_power_mw": round(r.net_power_mw, 2),
                    "lmp": round(r.lmp, 2),
                    "energy_revenue": round(r.energy_revenue, 2),
                    "agc_capacity_revenue": round(r.agc_capacity_revenue, 2),
                    "agc_mileage_revenue": round(r.agc_mileage_revenue, 2),
                    "deviation_penalty": round(r.deviation_penalty, 2),
                    "transmission_fee": round(r.transmission_fee, 2),
                    "gov_fund_fee": round(r.gov_fund_fee, 2),
                    "ancillary_fee": round(r.ancillary_fee, 2),
                    "battery_degradation_cost": round(r.battery_degradation_cost, 2),
                    "self_consumption_cost": round(r.self_consumption_cost, 2),
                    "net_revenue": round(r.net_revenue, 2),
                }
                for r in self.interval_results
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[OK] 计算结果已保存至: {path}")
