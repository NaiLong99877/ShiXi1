"""
调频市场收益计算 (AGC辅助服务)
基于《山东电力市场规则（试行）（2026年修订版）》
- 第5章: AGC性能考核 (K1, K2, K3, Kp)
- 第11.2节: 调频辅助服务市场
- 第14.8节: AGC费用结算
"""
from typing import List
from models import (
    DayAheadData, RealTimeData, AgcPerformance,
    IntervalResult
)


def calc_kp(perf: AgcPerformance) -> float:
    """
    综合性能指标 Kp

    Kp = w1×(K1/K1_base) + w2×(K2/K2_base) + w3×(K3/K3_base)

    PDF 第348页: 权重 = 0.35, 0.40, 0.25
    """
    k1_norm = perf.k1_speed / perf.k1_base
    k2_norm = perf.k2_precision / perf.k2_base
    k3_norm = perf.k3_response / perf.k3_base

    kp = (perf.weight_k1 * k1_norm +
          perf.weight_k2 * k2_norm +
          perf.weight_k3 * k3_norm)

    return round(kp, 4)


def calc_agc_capacity_revenue_per_interval(
    da_interval,    # MarketInterval
    rt_interval,    # RealTimeInterval
    kp: float,
    kp_avg: float,
) -> float:
    """
    调频容量收益 (单个15分钟时段, 单位: 元)

    容量收益 = 日前中标容量(MW) × 容量价格(元/MW) × (Kp/Kp_avg) × 0.25h

    注意: 容量收益按日前中标容量结算, 与实际AGC出力无关。
    Kp高于平均值 → 收益加成; Kp低于平均值 → 收益打折
    """
    hours = 0.25
    # 按日前中标容量结算, 不再使用 actual_agc_mw
    agc_mw = da_interval.win_agc_capacity_mw
    price = da_interval.agc_capacity_price

    # 性能调节系数
    perf_factor = kp / kp_avg if kp_avg > 0 else 1.0

    revenue = agc_mw * price * perf_factor * hours

    return round(revenue, 2)


def calc_agc_mileage_revenue_per_interval(
    rt_interval,    # RealTimeInterval
    mileage_price: float,  # 元/MW
) -> float:
    """
    调频里程收益 (单个15分钟时段, 单位: 元)

    里程收益 = 实际调频里程(MW) × 里程价格(元/MW)

    里程越大, 说明实际调频工作量越大, 收益越高
    """
    mileage = rt_interval.agc_mileage_mw
    revenue = mileage * mileage_price

    return round(revenue, 2)


def calc_ancillary_fee_per_interval(
    da_interval,
    rt_interval,
    agc_capacity_rev: float,
    agc_mileage_rev: float,
    ancillary_cost_rate: float,
) -> float:
    """
    辅助服务费用分摊 (单个时段)

    发电企业需要分摊部分辅助服务成本
    按调频收益的一定比例计算
    """
    total_agc = agc_capacity_rev + agc_mileage_rev
    fee = total_agc * ancillary_cost_rate
    return round(fee, 2)


def calc_frequency_market(
    da_data: DayAheadData,
    rt_data: RealTimeData,
    agc_perf: AgcPerformance,
    ancillary_cost_rate: float,
    interval_results: List[IntervalResult],
) -> List[IntervalResult]:
    """
    调频市场完整计算

    为每个时段填充AGC相关字段
    """
    kp = calc_kp(agc_perf)
    kp_avg = agc_perf.kp_avg
    mileage_price = da_data.agc_mileage_price

    for i, result in enumerate(interval_results):
        da = da_data.intervals[i]
        rt = rt_data.intervals[i]

        # 容量收益
        cap_rev = calc_agc_capacity_revenue_per_interval(
            da, rt, kp, kp_avg
        )

        # 里程收益
        mil_rev = calc_agc_mileage_revenue_per_interval(
            rt, mileage_price
        )

        # 辅助服务分摊
        ancillary_fee = calc_ancillary_fee_per_interval(
            da, rt, cap_rev, mil_rev, ancillary_cost_rate
        )

        # 更新结果
        result.agc_capacity_revenue = cap_rev
        result.agc_mileage_revenue = mil_rev
        result.ancillary_fee = ancillary_fee

    return interval_results


def get_kp_detail(perf: AgcPerformance) -> dict:
    """获取Kp计算明细, 供前端展示"""
    kp = calc_kp(perf)
    return {
        "kp": kp,
        "k1_contribution": round(perf.weight_k1 * perf.k1_speed / perf.k1_base, 4),
        "k2_contribution": round(perf.weight_k2 * perf.k2_precision / perf.k2_base, 4),
        "k3_contribution": round(perf.weight_k3 * perf.k3_response / perf.k3_base, 4),
        "kp_avg": perf.kp_avg,
        "perf_factor": round(kp / perf.kp_avg, 4) if perf.kp_avg > 0 else 1.0,
    }
