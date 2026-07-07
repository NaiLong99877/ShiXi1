"""
结算总成
汇总电能量市场收益和调频市场收益, 计算最终净利润
"""
from typing import List, Tuple
from models import (
    StorageParams, DayAheadData, RealTimeData,
    AgcPerformance, SettlementParams,
    IntervalResult, DailyResult
)
from energy_market import calc_energy_market
from frequency_market import calc_frequency_market, get_kp_detail


def calc_daily_settlement(
    storage: StorageParams,
    da_data: DayAheadData,
    rt_data: RealTimeData,
    agc_perf: AgcPerformance,
    settlement: SettlementParams,
    initial_soc: float = None,
) -> DailyResult:
    """
    日度完整结算

    Args:
        initial_soc: 当日初始SOC, None则使用storage.initial_soc (跨日结转用)

    执行顺序:
    1. 电能量市场计算 (含SOC模拟)
    2. 调频市场计算 (填充AGC字段)
    3. 净收益汇总

    净收益 = 电能量收益 + 调频容量 + 调频里程
           - 偏差考核 - 输配电费 - 政府基金 - 辅助分摊
    """
    # Step 1: 电能量市场
    results, soc_list = calc_energy_market(
        storage, da_data, rt_data, settlement, initial_soc
    )

    # Step 2: 调频市场
    results = calc_frequency_market(
        da_data, rt_data, agc_perf,
        settlement.ancillary_cost_rate,
        results,
    )

    # Step 3: 计算每个时段的净收益
    kp = get_kp_detail(agc_perf)["kp"]

    for result in results:
        # 该时段总收入
        gross = (result.energy_revenue +
                 result.agc_capacity_revenue +
                 result.agc_mileage_revenue)

        # 该时段总费用 (市场结算 + 运行成本)
        cost = (result.deviation_penalty +
                result.transmission_fee +
                result.gov_fund_fee +
                result.ancillary_fee +
                result.battery_degradation_cost +
                result.self_consumption_cost)

        result.net_revenue = round(gross - cost, 2)

    # Step 4: 汇总
    total_energy = sum(r.energy_revenue for r in results)
    total_cap = sum(r.agc_capacity_revenue for r in results)
    total_mil = sum(r.agc_mileage_revenue for r in results)
    total_gross = total_energy + total_cap + total_mil

    total_dev = sum(r.deviation_penalty for r in results)
    total_trans = sum(r.transmission_fee for r in results)
    total_gov = sum(r.gov_fund_fee for r in results)
    total_anc = sum(r.ancillary_fee for r in results)

    # AGC不合格扣款 (简化: K3<0.9 算不合格)
    agc_unqualified = 0
    if agc_perf.k3_response / agc_perf.k3_base < 0.9:
        agc_unqualified = 1
    agc_unqualified_penalty = agc_unqualified * settlement.agc_unqualified_penalty

    # 市场结算总费用 (交给电网/交易中心的钱)
    total_market_cost = (total_dev + total_trans + total_gov +
                         total_anc + agc_unqualified_penalty)

    # 运行成本 (电站自身物理成本)
    total_batt_deg = sum(r.battery_degradation_cost for r in results)
    total_self_cons = sum(r.self_consumption_cost for r in results)
    total_om = settlement.daily_om_cost_yuan
    total_operating = total_batt_deg + total_self_cons + total_om

    # 总费用 = 市场结算 + 运行成本
    total_cost = total_market_cost + total_operating

    net_profit = total_gross - total_cost

    # 充放电汇总
    hours = 0.25
    total_charge = sum(r.net_power_mw * hours for r in results if r.net_power_mw < 0)
    total_discharge = sum(r.net_power_mw * hours for r in results if r.net_power_mw > 0)
    final_soc = results[-1].soc if results else 0

    # 构建结果
    daily = DailyResult(
        date=da_data.date,
        node_name=da_data.node_name,
        storage_name=storage.name,
        total_energy_revenue=round(total_energy, 2),
        total_agc_capacity_revenue=round(total_cap, 2),
        total_agc_mileage_revenue=round(total_mil, 2),
        total_gross_revenue=round(total_gross, 2),
        total_deviation_penalty=round(total_dev, 2),
        total_transmission_fee=round(total_trans, 2),
        total_gov_fund_fee=round(total_gov, 2),
        total_ancillary_fee=round(total_anc, 2),
        agc_unqualified_count=agc_unqualified,
        agc_unqualified_penalty=round(agc_unqualified_penalty, 2),
        total_market_cost=round(total_market_cost, 2),
        total_battery_degradation=round(total_batt_deg, 2),
        total_self_consumption=round(total_self_cons, 2),
        total_om_cost=round(total_om, 2),
        total_operating_cost=round(total_operating, 2),
        total_cost=round(total_cost, 2),
        net_profit=round(net_profit, 2),
        kp=kp,
        total_charge_mwh=round(abs(total_charge), 2),
        total_discharge_mwh=round(total_discharge, 2),
        final_soc=round(final_soc, 4),
        interval_results=results,
    )

    return daily


def print_summary(daily: DailyResult):
    """打印收益摘要到控制台"""
    print("=" * 60)
    print(f"  储能电站日收益结算报告")
    print(f"  日期: {daily.date}  |  节点: {daily.node_name}")
    print(f"  电站: {daily.storage_name}")
    print("=" * 60)
    print(f"\n  [收入]")
    print(f"    电能量套利收益:       {daily.total_energy_revenue:>12,.2f} 元")
    print(f"    调频容量收益:         {daily.total_agc_capacity_revenue:>12,.2f} 元")
    print(f"    调频里程收益:         {daily.total_agc_mileage_revenue:>12,.2f} 元")
    print(f"    总收入:               {daily.total_gross_revenue:>12,.2f} 元")
    print(f"\n  [市场结算费用]")
    print(f"    偏差考核扣款:         {daily.total_deviation_penalty:>12,.2f} 元")
    print(f"    输配电费:             {daily.total_transmission_fee:>12,.2f} 元")
    print(f"    政府基金及附加:       {daily.total_gov_fund_fee:>12,.2f} 元")
    print(f"    辅助服务分摊:         {daily.total_ancillary_fee:>12,.2f} 元")
    print(f"    AGC不合格扣款:        {daily.agc_unqualified_penalty:>12,.2f} 元")
    print(f"    市场费用小计:         {daily.total_market_cost:>12,.2f} 元")
    print(f"\n  [运行成本]")
    print(f"    电池衰减成本:         {daily.total_battery_degradation:>12,.2f} 元")
    print(f"    辅助用电成本:         {daily.total_self_consumption:>12,.2f} 元")
    print(f"    日运维固定费用:       {daily.total_om_cost:>12,.2f} 元")
    print(f"    运行成本小计:         {daily.total_operating_cost:>12,.2f} 元")
    print(f"\n  [总费用]                {daily.total_cost:>12,.2f} 元")
    print(f"\n  [净收益]                {daily.net_profit:>12,.2f} 元")
    print(f"\n  [其他]")
    print(f"    Kp性能指标:           {daily.kp:>12.4f}")
    print(f"    总充电量:             {daily.total_charge_mwh:>12.2f} MWh")
    print(f"    总放电量:             {daily.total_discharge_mwh:>12.2f} MWh")
    print(f"    日末SOC:              {daily.final_soc:>12.2%}")
    print("=" * 60)
