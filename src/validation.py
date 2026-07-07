"""
硬约束校验 — 检查实时调度输出是否严格服从指令
"""
from typing import List, Dict, Any, Optional


def validate_dispatch_constraints(
    storage_params: Dict[str, Any],
    day_ahead_intervals: List[Dict[str, Any]],
    real_time_intervals: List[Dict[str, Any]],
    initial_soc: Optional[float] = None,
) -> List[str]:
    """
    校验实时调度是否满足硬约束。

    核心原则:
      - AGC != 0 时, 实际功率必须与AGC指令方向一致、幅值一致, 且无套利叠加。
      - AGC  = 0 时, 实际功率不得超过日前中标计划
        (可在 [0, 日前中标] 范围内优化, 允许因 SOC 截断导致的少充/少放)。
      - 不得出现 "多放电" 或 "多充电" (实际功率超过指令)。
      - 因 SOC 硬边界导致的 "少放/少充" 属于物理安全截断, 允许但会在日志中提示,
        不记为硬约束违规。
      - 不得同时充电和放电; SOC 不得突破硬边界。

    Args:
        storage_params: 储能参数 (来自 storage_params.json 的字典)
        day_ahead_intervals: 日前市场中标时段列表
        real_time_intervals: 实时运行时段列表
        initial_soc: 当日初始SOC; 若为None则使用 storage_params["initial_soc"]

    Returns:
        violations: 硬约束违规描述列表, 空列表表示全部通过
    """
    issues = []
    soc_min = storage_params["soc_min"]
    soc_max = storage_params["soc_max"]
    cap = storage_params["rated_capacity_mwh"]
    eta_c = storage_params["charging_efficiency"]
    eta_d = storage_params["discharging_efficiency"]
    self_dis = storage_params.get("self_discharge_rate", 0.0) * 0.25 * cap
    hours = 0.25

    soc = storage_params["initial_soc"] if initial_soc is None else initial_soc

    for da, rt in zip(day_ahead_intervals, real_time_intervals):
        time = rt.get("time", "--:--")
        agc = rt.get("actual_agc_mw", 0.0)
        act_c = rt.get("actual_charge_mw", 0.0)
        act_d = rt.get("actual_discharge_mw", 0.0)
        arb_c = rt.get("arbitrage_charge_mw", 0.0)
        arb_d = rt.get("arbitrage_discharge_mw", 0.0)
        agc_c = rt.get("agc_charge_mw", 0.0)
        agc_d = rt.get("agc_discharge_mw", 0.0)
        da_c = da.get("win_charge_mw", 0.0)
        da_d = da.get("win_discharge_mw", 0.0)

        # 4. 起始SOC 不突破硬边界 (前置检查)
        if soc < soc_min - 1e-9 or soc > soc_max + 1e-9:
            issues.append(
                f"{time}: 起始SOC={soc:.4%} 突破硬边界 [{soc_min:.0%}, {soc_max:.0%}]"
            )

        # 计算当前SOC下的物理可执行量
        max_c_dc = max(0.0, (soc_max - soc) * cap)
        max_d_dc = max(0.0, (soc - soc_min) * cap)
        max_c_ac = max_c_dc / (hours * eta_c) if eta_c > 0 else 0.0
        max_d_ac = max_d_dc * eta_d / hours if eta_d > 0 else 0.0

        # 5. 实际功率不得超过物理可执行量 (防止越限, 允许 0.01MW 以内的浮点/舍入误差)
        if act_c > max_c_ac + 0.01:
            issues.append(
                f"{time}: 实际充电{act_c:.2f}MW 超出 SOC 允许最大值 {max_c_ac:.2f}MW "
                f"(SOC={soc:.2%})"
            )
        if act_d > max_d_ac + 0.01:
            issues.append(
                f"{time}: 实际放电{act_d:.2f}MW 超出 SOC 允许最大值 {max_d_ac:.2f}MW "
                f"(SOC={soc:.2%})"
            )

        # 1. AGC 指令必须完全响应, 且不与套利叠加
        if agc > 1e-9:
            # 多放电 = 实际放电 > AGC 指令
            if act_d > agc + 1e-6:
                issues.append(
                    f"{time}: AGC放电指令{agc:.2f}MW, 实际放电{act_d:.2f}MW, 多放电"
                )
            # 方向错误或多充电
            if act_c > 1e-6 or arb_c > 1e-6 or arb_d > 1e-6:
                issues.append(
                    f"{time}: AGC放电指令期间存在充电/套利 "
                    f"(actual_c={act_c:.2f}, arb_d={arb_d:.2f}, arb_c={arb_c:.2f})"
                )
        elif agc < -1e-9:
            # 多充电 = 实际充电 > |AGC| 指令
            if act_c > abs(agc) + 1e-6:
                issues.append(
                    f"{time}: AGC充电指令{agc:.2f}MW, 实际充电{act_c:.2f}MW, 多充电"
                )
            # 方向错误或多放电
            if act_d > 1e-6 or arb_d > 1e-6 or arb_c > 1e-6:
                issues.append(
                    f"{time}: AGC充电指令期间存在放电/套利 "
                    f"(actual_d={act_d:.2f}, arb_c={arb_c:.2f}, arb_d={arb_d:.2f})"
                )
        else:
            # 2. AGC=0 时可在日前中标计划内优化; 仅允许因 SOC 截断导致的少充/少放
            if act_c > da_c + 1e-6:
                issues.append(
                    f"{time}: AGC=0时实际充电{act_c:.2f}MW 多于日前中标{da_c:.2f}MW, 多充电"
                )
            if act_d > da_d + 1e-6:
                issues.append(
                    f"{time}: AGC=0时实际放电{act_d:.2f}MW 多于日前中标{da_d:.2f}MW, 多放电"
                )

        # 3. 无同时充放电
        if act_c > 1e-6 and act_d > 1e-6:
            issues.append(f"{time}: 同时存在充电{act_c:.2f}MW和放电{act_d:.2f}MW")

        # 更新 SOC 用于下一时段校验
        plan_c_dc = act_c * hours * eta_c
        plan_d_dc = act_d * hours / eta_d
        actual_c_dc = min(plan_c_dc, max_c_dc)
        actual_d_dc = min(plan_d_dc, max_d_dc)
        soc += (actual_c_dc - actual_d_dc - self_dis) / cap
        soc = max(soc_min, min(soc_max, soc))

    return issues
