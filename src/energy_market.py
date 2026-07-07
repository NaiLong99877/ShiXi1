"""
电能量市场收益计算
基于《山东电力市场规则（试行）（2026年修订版）》
- 第14.5节: 发电机收入计算
- 第14.7节: 偏差考核
"""
from typing import List, Tuple
from models import (
    StorageParams, DayAheadData, RealTimeData,
    SettlementParams, IntervalResult
)


def calc_energy_revenue_per_interval(
    storage: StorageParams,
    da_interval,   # MarketInterval
    rt_interval,   # RealTimeInterval
) -> float:
    """
    电能量收益 (单个15分钟时段, 单位: 元)
    含能量套利 + AGC电量结算

    AGC电量: 正=放电(卖电), 负=充电(买电), 按实时LMP结算
    """
    hours = 0.25
    lmp = rt_interval.rt_lmp_yuan_per_mwh

    # 总计放电 = 套利放电 + AGC放电
    total_discharge = rt_interval.actual_discharge_mw
    # 总计充电 = 套利充电 + AGC充电
    total_charge = rt_interval.actual_charge_mw

    energy_revenue = (total_discharge - total_charge) * hours * lmp

    return round(energy_revenue, 2)


def calc_deviation_penalty_per_interval(
    da_interval,   # MarketInterval
    rt_interval,   # RealTimeInterval
    settlement: SettlementParams,
) -> float:
    """
    偏差考核扣款 (PDF 第14.7节)

    当实际充放电量与日前中标量偏差超过 tolerance 时,
    对超出部分按 penalty_rate 处罚

    以LMP为基准计算罚金
    """
    lmp = rt_interval.rt_lmp_yuan_per_mwh
    hours = 0.25
    tolerance = settlement.deviation_tolerance
    penalty_rate = settlement.deviation_penalty_rate

    # 充电偏差
    da_charge = da_interval.win_charge_mw
    rt_charge = rt_interval.actual_charge_mw
    charge_dev = abs(rt_charge - da_charge)
    charge_tolerated = da_charge * tolerance
    charge_excess = max(0, charge_dev - charge_tolerated)

    # 放电偏差
    da_discharge = da_interval.win_discharge_mw
    rt_discharge = rt_interval.actual_discharge_mw
    discharge_dev = abs(rt_discharge - da_discharge)
    discharge_tolerated = da_discharge * tolerance
    discharge_excess = max(0, discharge_dev - discharge_tolerated)

    # 偏差电量按 LMP × penalty_rate 处罚
    penalty_mwh = (charge_excess + discharge_excess) * hours
    penalty = penalty_mwh * lmp * penalty_rate

    return round(penalty, 2)


def calc_transmission_fee(
    energy_abs_mwh: float,
    settlement: SettlementParams,
) -> float:
    """
    输配电费 (按充放电电量绝对值计算)
    """
    return round(energy_abs_mwh * settlement.transmission_fee_rate, 2)


def calc_gov_fund_fee(
    energy_abs_mwh: float,
    settlement: SettlementParams,
) -> float:
    """
    政府基金及附加
    """
    return round(energy_abs_mwh * settlement.gov_fund_rate, 2)


def calc_battery_degradation(
    charge_mwh: float,
    discharge_mwh: float,
    settlement: SettlementParams,
) -> float:
    """
    电池衰减成本

    按充放电吞吐量计算，每次充放电都会造成电池寿命衰减。
    典型取值: 50~150 元/MWh (0.05~0.15 元/kWh)
    """
    throughput = charge_mwh + discharge_mwh
    return round(throughput * settlement.battery_degradation_per_mwh, 2)


def calc_self_consumption_cost(
    charge_mwh: float,
    lmp: float,
    settlement: SettlementParams,
) -> float:
    """
    辅助用电成本

    储能系统自身消耗电力（空调、PCS、BMS等），
    按充电量的一定比例估算，以LMP计价。
    典型取值: 2%~5% 的充电量
    """
    self_use_mwh = charge_mwh * settlement.self_consumption_rate
    return round(self_use_mwh * lmp, 2)


def simulate_soc(
    storage: StorageParams,
    rt_intervals: list,
    initial_soc: float = None,
) -> Tuple[List[float], List[float], List[float]]:
    """
    模拟SOC变化轨迹 (含容量约束)

    SOC约束: 充电时不超过soc_max, 放电时不低于soc_min。
    超出的充/放电量被丢弃(不计入实际能量流)。

    Returns:
        soc_list: 每个时段末的SOC
        eff_charge_mwh_list: 实际充入电池的MWh (已扣除被截断部分)
        eff_discharge_mwh_list: 实际从电池放出的MWh (已扣除被截断部分)
    """
    if initial_soc is None:
        initial_soc = storage.initial_soc

    capacity_mwh = storage.rated_capacity_mwh
    soc = initial_soc
    soc_list = []
    eff_charge_list = []
    eff_discharge_list = []

    for rt in rt_intervals:
        hours = 0.25

        # 总实际充放电 (已由套利 + AGC 合成)
        total_charge = rt.actual_charge_mw
        total_discharge = rt.actual_discharge_mw

        # 计划充放电量 (MWh, 直流侧)
        plan_charge_dc = total_charge * hours * storage.charging_efficiency
        plan_discharge_dc = total_discharge * hours / storage.discharging_efficiency

        # 自放电
        self_discharge = storage.self_discharge_rate * hours * capacity_mwh

        # 充电约束: 不能超过soc_max
        max_charge_dc = (storage.soc_max - soc) * capacity_mwh
        actual_charge_dc = min(plan_charge_dc, max(max_charge_dc, 0))

        # 放电约束: 不能低于soc_min
        max_discharge_dc = (soc - storage.soc_min) * capacity_mwh
        actual_discharge_dc = min(plan_discharge_dc, max(max_discharge_dc, 0))

        # 实际SOC变化
        delta_soc = (actual_charge_dc - actual_discharge_dc - self_discharge) / capacity_mwh
        soc = soc + delta_soc
        soc = max(storage.soc_min, min(storage.soc_max, soc))

        soc_list.append(round(soc, 4))
        # 折算回交流侧MWh (用于结算)
        eff_charge_list.append(round(actual_charge_dc / storage.charging_efficiency, 4))
        eff_discharge_list.append(round(actual_discharge_dc * storage.discharging_efficiency, 4))

    return soc_list, eff_charge_list, eff_discharge_list


def calc_energy_market(
    storage: StorageParams,
    da_data: DayAheadData,
    rt_data: RealTimeData,
    settlement: SettlementParams,
    initial_soc: float = None,
) -> Tuple[List[IntervalResult], List[float]]:
    """
    电能量市场完整计算

    Args:
        initial_soc: 初始SOC, 若为None则使用storage.initial_soc
                    跨日计算时传入前一天的日末SOC

    返回:
    - interval_results: 每个时段的结算结果
    - soc_list: SOC变化轨迹
    """
    n = len(da_data.intervals)
    soc_list, eff_charge_list, eff_discharge_list = simulate_soc(
        storage, rt_data.intervals, initial_soc
    )

    results = []
    for i in range(n):
        da = da_data.intervals[i]
        rt = rt_data.intervals[i]
        soc = soc_list[i]
        hours = 0.25

        # 使用受SOC约束后的有效电量 (而非计划值)
        eff_charge_mwh = eff_charge_list[i]    # 实际充入的MWh
        eff_discharge_mwh = eff_discharge_list[i]  # 实际放出的MWh
        energy_abs_mwh = eff_charge_mwh + eff_discharge_mwh

        # 电能量套利 = 放电收入 - 充电成本 (按有效电量)
        lmp = rt.rt_lmp_yuan_per_mwh
        discharge_revenue = eff_discharge_mwh * lmp
        charge_cost = eff_charge_mwh * lmp
        energy_rev = round(discharge_revenue - charge_cost, 2)

        # 偏差考核 (按计划值 vs 实际值)
        dev_penalty = calc_deviation_penalty_per_interval(da, rt, settlement)

        # 输配电费
        trans_fee = calc_transmission_fee(energy_abs_mwh, settlement)

        # 政府基金
        gov_fee = calc_gov_fund_fee(energy_abs_mwh, settlement)

        # 电池衰减成本 (按有效吞吐量)
        batt_deg = calc_battery_degradation(eff_charge_mwh, eff_discharge_mwh, settlement)

        # 辅助用电成本 (按有效充电量)
        self_cons = calc_self_consumption_cost(eff_charge_mwh, lmp, settlement)

        # 净功率 (放电为正, 充电为负, 按有效值)
        net_power = (eff_discharge_mwh - eff_charge_mwh) / hours

        results.append(IntervalResult(
            time=da.time,
            energy_revenue=energy_rev,
            deviation_penalty=dev_penalty,
            transmission_fee=trans_fee,
            gov_fund_fee=gov_fee,
            battery_degradation_cost=batt_deg,
            self_consumption_cost=self_cons,
            soc=soc,
            net_power_mw=round(net_power, 2),
            lmp=lmp,
        ))

    return results, soc_list
