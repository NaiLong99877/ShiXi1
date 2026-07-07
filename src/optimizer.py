"""
储能电站优化调度器 (含AGC)
==========================
基于动态规划(DP)的两阶段优化:

  日前优化: 根据预测LMP最大化套利收益, 永远留1MW AGC裕量
  实时调度: AGC调控命令随机下发(必须响应), AGC=0时在日前中标计划范围内实时优化

实时调度规则:
  - AGC != 0 时, 严格按 AGC 指令执行, 禁止叠加任何套利功率
  - AGC  = 0 时, 在日前中标计划范围内优化套利决策
    (0 <= 套利充电 <= 日前充电, 0 <= 套利放电 <= 日前放电)
  - 物理安全网: 若 AGC 指令突破 SOC 硬边界, 则按硬边界截断并告警

DP状态: SOC离散化为401级 (10.0% ~ 90.0%, 步长0.2%)
"""
import random
from typing import List, Tuple, Callable, Optional
from models import StorageParams, SettlementParams


# ============================================================
# SOC离散化 & DP引擎
# ============================================================

def _build_soc_table(
    storage: StorageParams,
    n_states: int = 401,
) -> Tuple[List[float], int, int]:
    """构建SOC离散化查找表,参数全部来自 storage_params.json"""
    soc_min = storage.soc_min
    soc_max = storage.soc_max
    step = (soc_max - soc_min) / (n_states - 1)
    soc_of = [round(soc_min + s * step, 6) for s in range(n_states)]

    # 单时段最大充/放电导致的状态变化数
    # 按额定功率、额定容量、充放电效率估算,避免硬编码
    hours = 0.25
    max_charge_states = int(
        storage.rated_power_mw * hours * storage.charging_efficiency
        / storage.rated_capacity_mwh / step
    ) + 2
    max_discharge_states = int(
        storage.rated_power_mw * hours / storage.discharging_efficiency
        / storage.rated_capacity_mwh / step
    ) + 2

    return soc_of, max_charge_states, max_discharge_states


def _state_from_soc(soc: float, storage: StorageParams, n_states: int) -> int:
    """将连续SOC映射到最近的离散状态索引"""
    step = (storage.soc_max - storage.soc_min) / (n_states - 1)
    idx = int(round((soc - storage.soc_min) / step))
    return max(0, min(n_states - 1, idx))


def _dp_core(
    n_intervals: int,
    n_states: int,
    soc_of: List[float],
    max_charge_states: int,
    max_discharge_states: int,
    initial_soc: float,
    final_soc_target: Optional[float],
    profit_fn: Callable[[int, float, float], float],
    storage: StorageParams,
    hours: float = 0.25,
    soc_soft_min: Optional[float] = None,
    soc_soft_max: Optional[float] = None,
    max_charge_power: Optional[float] = None,
    max_discharge_power: Optional[float] = None,
    self_discharge_mwh: float = 0.0,
) -> Tuple[List[float], float]:
    """
    DP核心引擎 — 在离散SOC空间上最大化累计利润

    软边界约束:
      - 能量充电不得使SOC超过 soc_soft_max (保留上部裕量给AGC)
      - 能量放电不得使SOC低于 soc_soft_min (保留下部裕量给AGC)
      - 闲置/AGC-only  Transition 不受软边界约束

    功率硬约束:
      - 能量充电功率不得超过 max_charge_power (默认额定功率)
      - 能量放电功率不得超过 max_discharge_power (默认额定功率)

    其他:
      - self_discharge_mwh: 每时段自放电量 (DC 侧 MWh), 默认 0

    Returns:
        powers:     每时段功率 (MW, 正=放电, 负=充电), 长度n_intervals
        final_soc:  优化路径的终态SOC
    """
    T = n_intervals
    N = n_states
    capacity = storage.rated_capacity_mwh
    init_s = _state_from_soc(initial_soc, storage, N)

    if max_charge_power is None:
        max_charge_power = storage.rated_power_mw
    if max_discharge_power is None:
        max_discharge_power = storage.rated_power_mw

    NEG_INF = -1e18
    dp: List[List[Tuple[float, int, float]]] = [
        [(NEG_INF, -1, 0.0) for _ in range(N)] for _ in range(T + 1)
    ]
    dp[0][init_s] = (0.0, -1, 0.0)

    # ---- 前向DP ----
    for t in range(T):
        src = dp[t]
        dst = dp[t + 1]

        for s in range(N):
            cur_profit = src[s][0]
            if cur_profit <= NEG_INF / 2:
                continue

            # ---- 闲置 ----
            idle_profit = profit_fn(t, 0.0, 0.0)
            new_p = cur_profit + idle_profit
            idle_s = _state_from_soc(soc_of[s] - self_discharge_mwh / capacity, storage, N)
            if new_p > dst[idle_s][0]:
                dst[idle_s] = (new_p, s, 0.0)

            # ---- 充电 (SOC上升) ----
            for ds in range(1, max_charge_states + 1):
                s_next = s + ds
                if s_next >= N:
                    break
                # 软边界: 能量充电不得越过 soft_max (考虑自放电后的末态)
                if soc_soft_max is not None and soc_of[s_next] - self_discharge_mwh / capacity > soc_soft_max + 1e-9:
                    break
                delta_soc = soc_of[s_next] - soc_of[s]
                p_charge = delta_soc * capacity / (hours * storage.charging_efficiency)
                if p_charge > max_charge_power:
                    break
                if p_charge < 0.01:
                    continue

                profit = profit_fn(t, p_charge, 0.0)
                new_p = cur_profit + profit
                end_soc = soc_of[s_next] - self_discharge_mwh / capacity
                s_next_eff = _state_from_soc(end_soc, storage, N)
                if new_p > dst[s_next_eff][0]:
                    dst[s_next_eff] = (new_p, s, -round(p_charge, 4))

            # ---- 放电 (SOC下降) ----
            for ds in range(1, max_discharge_states + 1):
                s_next = s - ds
                if s_next < 0:
                    break
                # 软边界: 能量放电不得低于 soft_min (考虑自放电后的末态)
                if soc_soft_min is not None and soc_of[s_next] - self_discharge_mwh / capacity < soc_soft_min - 1e-9:
                    break
                delta_soc = soc_of[s] - soc_of[s_next]
                p_discharge = delta_soc * capacity * storage.discharging_efficiency / hours
                if p_discharge > max_discharge_power:
                    break
                if p_discharge < 0.01:
                    continue

                profit = profit_fn(t, 0.0, p_discharge)
                new_p = cur_profit + profit
                end_soc = soc_of[s_next] - self_discharge_mwh / capacity
                s_next_eff = _state_from_soc(end_soc, storage, N)
                if new_p > dst[s_next_eff][0]:
                    dst[s_next_eff] = (new_p, s, round(p_discharge, 4))

    # ---- 选择最优终态 ----
    if final_soc_target is not None:
        target_s = _state_from_soc(final_soc_target, storage, N)
        window = 5
        candidates = range(max(0, target_s - window),
                          min(N, target_s + window + 1))
    else:
        candidates = range(N)

    best_s = max(candidates, key=lambda s: dp[T][s][0])
    best_profit = dp[T][best_s][0]

    if best_profit <= NEG_INF / 2:
        return [0.0] * T, initial_soc

    # ---- 回溯提取功率路径 ----
    powers = [0.0] * T
    curr_s = best_s
    for t in range(T - 1, -1, -1):
        _, prev_s, power = dp[t + 1][curr_s]
        powers[t] = power
        curr_s = prev_s

    return powers, soc_of[best_s]


# ============================================================
# AGC 调控命令生成
# ============================================================

def generate_agc_commands(
    n_intervals: int = 96,
    max_agc_mw: float = 15.0,
    seed: int = 42,
    sparse: bool = False,
) -> List[float]:
    """
    生成合理的AGC调控命令序列

    AGC特点:
      - 正负交替 (上调/下调频率)
      - 有自相关性 (不会每15分钟剧烈反转)
      - 幅值通常在额定功率的10-15%以内
      - sparse=True时只有约15%时段有命令, 其余为0

    Args:
        n_intervals: 时段数
        max_agc_mw: AGC指令最大幅值 (MW)
        seed: 随机种子
        sparse: 是否稀疏下发

    Returns:
        agc_commands: 每个时段的AGC指令 (MW, 正=放电方向, 负=充电方向)
    """
    rng = random.Random(seed)
    cmds = []
    prev = 0.0

    for i in range(n_intervals):
        if sparse and rng.random() > 0.15:
            cmds.append(0.0)
            prev = 0.0
            continue
        # 均值回归AR: 50%延续 + 对称随机扰动
        ar = 0.5 * prev + rng.uniform(-max_agc_mw * 0.5, max_agc_mw * 0.5)
        # 偶尔大扰动 (电网频率事件, 概率~5%)
        if rng.random() < 0.05:
            ar += rng.uniform(-max_agc_mw * 0.8, max_agc_mw * 0.8)
        cmd = max(-max_agc_mw, min(max_agc_mw, ar))
        cmds.append(round(cmd, 2))
        prev = cmd

    return cmds


# ============================================================
# 日前优化 (含AGC裕量)
# ============================================================

def optimize_day_ahead(
    storage: StorageParams,
    lmp_pred: List[float],
    agc_prices: List[float],
    agc_mileage_price: float,
    initial_soc: float,
    final_soc_target: Optional[float] = None,
    agc_reserve_mw: Optional[float] = None,
    agc_max_capacity_mw: Optional[float] = None,
    n_states: int = 401,
) -> Tuple[List[float], List[float], List[float], float]:
    """
    日前功率优化: 最大化 (能量套利 + AGC容量收益)

    AGC功率裕量与AGC容量上限均从 storage_params.json 读取:
      - agc_reserve_mw: 额定功率中永远预留的AGC功率裕量
      - agc_max_capacity_mw: AGC中标容量上限 (本场景固定10MW)

    AGC容量 = min(额定功率 - 能量功率 - 功率裕量, agc_max_capacity_mw)

    硬约束:
      - 能量充电/放电功率均不得超过 (额定功率 - agc_reserve_mw),
        确保实时运行中永远留有AGC功率裕量。
      - DP 状态转移已计入每时段自放电 (storage.self_discharge_rate),
        规划的 SOC 轨迹与结算端 simulate_soc 保持一致。

    Args:
        storage:       储能参数
        lmp_pred:      日前LMP预测 (96点, 元/MWh)
        agc_prices:    AGC容量报价 (96点, 元/MW)
        agc_mileage_price: AGC里程价格 (元/MW)
        initial_soc:   初始SOC
        final_soc_target: 日末SOC目标
        agc_reserve_mw: AGC功率裕量 (默认取 storage.agc_reserve_mw)
        agc_max_capacity_mw: AGC容量上限 (默认取 storage.agc_max_capacity_mw)

    Returns:
        charge_plan:    能量充电功率 (96点, MW)
        discharge_plan: 能量放电功率 (96点, MW)
        agc_capacity:   AGC容量中标 (96点, MW)
        final_soc:      日末SOC
    """
    if agc_reserve_mw is None:
        agc_reserve_mw = storage.agc_reserve_mw
    if agc_max_capacity_mw is None:
        agc_max_capacity_mw = storage.agc_max_capacity_mw

    soc_of, max_chg, max_dis = _build_soc_table(storage, n_states)

    # 能量可用功率上限 = 额定功率 - AGC功率裕量 (硬约束)
    energy_power_limit = storage.rated_power_mw - agc_reserve_mw
    # 每时段自放电量 (DC 侧 MWh)
    self_discharge_mwh = storage.self_discharge_rate * 0.25 * storage.rated_capacity_mwh
    # AGC里程比 (每MW容量产生多少里程, 行业经验值)
    mileage_ratio = 0.4

    def da_profit_fn(t: int, p_charge: float, p_discharge: float) -> float:
        """
        利润 = 能量套利 + AGC容量收益 + AGC里程收益
        """
        hours = 0.25
        # 能量套利 (电网侧)
        energy_mwh = (p_discharge - p_charge) * hours
        energy_profit = energy_mwh * lmp_pred[t]

        # AGC容量 = 剩余功率裕量 (扣除能量占用和功率裕量)
        energy_used = max(p_charge, p_discharge)
        agc_cap = min(
            storage.rated_power_mw - energy_used - agc_reserve_mw,
            agc_max_capacity_mw,
        )
        agc_cap = max(0.0, agc_cap)

        # AGC容量收益 (Kp系数简化取1.0)
        agc_cap_rev = agc_cap * agc_prices[t] * hours

        # AGC里程收益
        agc_mileage = agc_cap * mileage_ratio
        agc_mil_rev = agc_mileage * agc_mileage_price

        return energy_profit + agc_cap_rev + agc_mil_rev

    powers, final_soc = _dp_core(
        n_intervals=len(lmp_pred),
        n_states=n_states,
        soc_of=soc_of,
        max_charge_states=max_chg,
        max_discharge_states=max_dis,
        initial_soc=initial_soc,
        final_soc_target=final_soc_target,
        profit_fn=da_profit_fn,
        storage=storage,
        hours=0.25,
        soc_soft_min=storage.soc_soft_min,
        soc_soft_max=storage.soc_soft_max,
        max_charge_power=energy_power_limit,
        max_discharge_power=energy_power_limit,
        self_discharge_mwh=self_discharge_mwh,
    )

    # 拆分正负功率 & 计算AGC容量
    charge_plan = []
    discharge_plan = []
    agc_capacity = []
    for p in powers:
        c = round(abs(p), 4) if p < 0 else 0.0
        d = round(p, 4) if p > 0 else 0.0
        charge_plan.append(c)
        discharge_plan.append(d)
        # AGC容量 = 额定 - 能量占用 - 裕量
        energy_used = max(c, d)
        agc = min(storage.rated_power_mw - energy_used - agc_reserve_mw, agc_max_capacity_mw)
        agc_capacity.append(round(max(0.0, agc), 4))

    return charge_plan, discharge_plan, agc_capacity, round(final_soc, 4)


# ============================================================
# 实时调度 (两段式: AGC指令优先 + AGC=0时段在计划内优化)
# ============================================================


def optimize_real_time(
    storage: StorageParams,
    da_charge: List[float],
    da_discharge: List[float],
    da_agc_cap: List[float],
    rt_lmp: List[float],
    agc_commands: List[float],
    agc_mileage_price: float,
    settlement: SettlementParams,
    initial_soc: float,
    final_soc_target: Optional[float] = None,
    agc_reserve_mw: Optional[float] = None,
    n_states: int = 401,
) -> Tuple[List[float], List[float], List[float], List[float], List[float], List[float], List[float], List[float], float]:
    """
    实时调度: 两段式策略。

    调度规则:
      1. AGC 指令存在时 (AGC != 0):
         - 严格按 AGC 指令执行, 套利功率 = 0
         - 实际放电 = AGC (>0) 或 实际充电 = |AGC| (<0)
      2. AGC = 0 时:
         - 在日前中标计划范围内做实时优化
         - 约束: 0 <= 套利充电 <= da_charge, 0 <= 套利放电 <= da_discharge
         - 目标: 最大化实时电能量套利收益 (不超出日前计划)
      3. 物理安全网:
         - 若 AGC 指令导致 SOC 突破硬边界, 则对 AGC 功率做截断
         - 优化过程中通过 SOC 软边界保留 AGC 能量裕量

    Args:
        storage:        储能参数
        da_charge:      日前能量充电计划 (96点, MW)
        da_discharge:   日前能量放电计划 (96点, MW)
        da_agc_cap:     日前AGC容量计划 (96点, MW, 仅用于接口兼容)
        rt_lmp:         实时LMP (96点, 元/MWh)
        agc_commands:   AGC调控命令 (96点, MW, 正=放电方向)
        agc_mileage_price: AGC里程价格
        settlement:     结算参数 (仅用于接口兼容)
        initial_soc:    初始SOC
        final_soc_target: 日末SOC目标
        agc_reserve_mw: AGC功率裕量 (默认取 storage.agc_reserve_mw)
        n_states:       SOC离散级数

    Returns:
        actual_charge:        实际总充电 (套利+AGC, 96点, MW)
        actual_discharge:     实际总放电 (套利+AGC, 96点, MW)
        arbitrage_charge:     套利充电 (96点, MW)
        arbitrage_discharge:  套利放电 (96点, MW)
        agc_charge:           AGC充电 (96点, MW)
        agc_discharge:        AGC放电 (96点, MW)
        actual_agc_mw:        AGC实际出力 (96点, MW, 有符号)
        agc_mileage:          AGC实际里程 (96点, MW)
        final_soc:            日末SOC
    """
    if agc_reserve_mw is None:
        agc_reserve_mw = storage.agc_reserve_mw

    T = len(rt_lmp)
    N = n_states
    hours = 0.25
    capacity = storage.rated_capacity_mwh
    eta_chg = storage.charging_efficiency
    eta_dis = storage.discharging_efficiency

    soc_of, max_chg, max_dis = _build_soc_table(storage, n_states)

    # 每时段自放电量 (DC 侧 MWh)
    self_discharge_mwh = storage.self_discharge_rate * hours * capacity

    # DP
    init_s = _state_from_soc(initial_soc, storage, N)

    NEG_INF = -1e18
    dp: List[List[Tuple[float, int, float]]] = [
        [(NEG_INF, -1, 0.0) for _ in range(N)] for _ in range(T + 1)
    ]
    dp[0][init_s] = (0.0, -1, 0.0)

    # ---- 前向DP ----
    for t in range(T):
        src = dp[t]
        dst = dp[t + 1]
        agc = agc_commands[t]

        for s in range(N):
            cur_profit = src[s][0]
            if cur_profit <= NEG_INF / 2:
                continue

            # 计算当前 SOC 状态下 AGC 的有效出力 (考虑 SOC 硬边界截断)
            if agc > 0:
                # AGC 放电指令
                max_discharge_dc = max(0.0, (soc_of[s] - storage.soc_min) * capacity)
                plan_discharge_dc = agc * hours / eta_dis
                eff_discharge_dc = min(plan_discharge_dc, max_discharge_dc)
                eff_agc_d = eff_discharge_dc * eta_dis / hours
                agc_delta = -eff_discharge_dc / capacity
                agc_energy_profit = eff_agc_d * hours * rt_lmp[t]
                agc_mil_rev = eff_agc_d * agc_mileage_price
            elif agc < 0:
                # AGC 充电指令
                max_charge_dc = max(0.0, (storage.soc_max - soc_of[s]) * capacity)
                plan_charge_dc = abs(agc) * hours * eta_chg
                eff_charge_dc = min(plan_charge_dc, max_charge_dc)
                eff_agc_c = eff_charge_dc / (hours * eta_chg)
                agc_delta = eff_charge_dc / capacity
                agc_energy_profit = -eff_agc_c * hours * rt_lmp[t]
                agc_mil_rev = eff_agc_c * agc_mileage_price
            else:
                eff_agc_d = 0.0
                eff_agc_c = 0.0
                agc_delta = 0.0
                agc_energy_profit = 0.0
                agc_mil_rev = 0.0

            # 闲置: 只响应 AGC, 套利功率 = 0
            end_soc = soc_of[s] + agc_delta - self_discharge_mwh / capacity
            idle_s = _state_from_soc(end_soc, storage, N)
            idle_profit = agc_energy_profit + agc_mil_rev
            new_p = cur_profit + idle_profit
            if new_p > dst[idle_s][0]:
                dst[idle_s] = (new_p, s, 0.0)

            if agc != 0:
                # AGC 指令存在时, 不允许任何套利充放电
                continue

            # AGC = 0 时, 在日前中标计划范围内优化
            # ---- 充电 (SOC上升) ----
            for ds in range(1, max_chg + 1):
                s_next = s + ds
                if s_next >= N:
                    break
                # 软边界: 套利充电不得越过 soft_max, 保留上部裕量给AGC
                if soc_of[s_next] > storage.soc_soft_max + 1e-9:
                    break
                delta_soc = soc_of[s_next] - soc_of[s]
                p_charge = delta_soc * capacity / (hours * eta_chg)
                # 约束: 套利充电不得超过日前中标充电
                if p_charge > da_charge[t]:
                    break
                if p_charge < 0.01:
                    continue

                profit = -p_charge * hours * rt_lmp[t]  # 充电成本
                new_p = cur_profit + profit
                end_soc = soc_of[s_next] - self_discharge_mwh / capacity
                s_next_eff = _state_from_soc(end_soc, storage, N)
                if new_p > dst[s_next_eff][0]:
                    dst[s_next_eff] = (new_p, s, -round(p_charge, 4))

            # ---- 放电 (SOC下降) ----
            for ds in range(1, max_dis + 1):
                s_next = s - ds
                if s_next < 0:
                    break
                # 软边界: 套利放电不得低于 soft_min, 保留下部裕量给AGC
                if soc_of[s_next] < storage.soc_soft_min - 1e-9:
                    break
                delta_soc = soc_of[s] - soc_of[s_next]
                p_discharge = delta_soc * capacity * eta_dis / hours
                # 约束: 套利放电不得超过日前中标放电
                if p_discharge > da_discharge[t]:
                    break
                if p_discharge < 0.01:
                    continue

                profit = p_discharge * hours * rt_lmp[t]  # 放电收入
                new_p = cur_profit + profit
                end_soc = soc_of[s_next] - self_discharge_mwh / capacity
                s_next_eff = _state_from_soc(end_soc, storage, N)
                if new_p > dst[s_next_eff][0]:
                    dst[s_next_eff] = (new_p, s, round(p_discharge, 4))

    # ---- 选择最优终态 ----
    if final_soc_target is not None:
        target_s = _state_from_soc(final_soc_target, storage, N)
        window = 5
        candidates = range(max(0, target_s - window),
                          min(N, target_s + window + 1))
    else:
        candidates = range(N)

    best_s = max(candidates, key=lambda s: dp[T][s][0])

    # ---- 回溯提取功率路径 ----
    powers = [0.0] * T
    curr_s = best_s
    for t in range(T - 1, -1, -1):
        _, prev_s, power = dp[t + 1][curr_s]
        powers[t] = power
        curr_s = prev_s

    # ---- 组装输出 (含 SOC 物理安全网) ----
    arbitrage_charge = []
    arbitrage_discharge = []
    agc_charge = []
    agc_discharge = []
    actual_charge = []
    actual_discharge = []
    actual_agc = []
    agc_mileages = []
    soc_path = []
    soc = initial_soc
    cap_violations = 0

    for t in range(T):
        soc_path.append(round(soc, 4))
        agc = agc_commands[t]

        # 套利部分 (来自 DP)
        arb_c = round(abs(powers[t]), 4) if powers[t] < 0 else 0.0
        arb_d = round(powers[t], 4) if powers[t] > 0 else 0.0

        # AGC 部分 (按连续 SOC 重新做一次精确截断)
        if agc > 0:
            max_discharge_dc = max(0.0, (soc - storage.soc_min) * capacity)
            plan_discharge_dc = agc * hours / eta_dis
            actual_discharge_dc = min(plan_discharge_dc, max_discharge_dc)
            if actual_discharge_dc < plan_discharge_dc - 1e-9:
                cap_violations += 1
            eff_agc_d = actual_discharge_dc * eta_dis / hours
            agc_discharge.append(round(eff_agc_d, 4))
            agc_charge.append(0.0)
            actual_agc.append(round(eff_agc_d, 4))
            agc_mileages.append(round(eff_agc_d, 4))
        elif agc < 0:
            max_charge_dc = max(0.0, (storage.soc_max - soc) * capacity)
            plan_charge_dc = abs(agc) * hours * eta_chg
            actual_charge_dc = min(plan_charge_dc, max_charge_dc)
            if actual_charge_dc < plan_charge_dc - 1e-9:
                cap_violations += 1
            eff_agc_c = actual_charge_dc / (hours * eta_chg)
            agc_charge.append(round(eff_agc_c, 4))
            agc_discharge.append(0.0)
            actual_agc.append(round(-eff_agc_c, 4))
            agc_mileages.append(round(eff_agc_c, 4))
        else:
            agc_charge.append(0.0)
            agc_discharge.append(0.0)
            actual_agc.append(0.0)
            agc_mileages.append(0.0)

        arbitrage_charge.append(arb_c)
        arbitrage_discharge.append(arb_d)
        actual_charge.append(round(arb_c + agc_charge[-1], 4))
        actual_discharge.append(round(arb_d + agc_discharge[-1], 4))

        # 更新 SOC (含自放电)
        if actual_agc[t] > 0:
            soc += -actual_agc[t] * hours / eta_dis / capacity
        elif actual_agc[t] < 0:
            soc += -actual_agc[t] * hours * eta_chg / capacity
        soc += (arb_c * hours * eta_chg - arb_d * hours / eta_dis) / capacity
        soc -= self_discharge_mwh / capacity
        soc = max(storage.soc_min, min(storage.soc_max, soc))

    if cap_violations > 0:
        print(f"[WARN] optimize_real_time: {cap_violations} 个时段因 SOC 硬边界被截断, 未完全按 AGC 指令执行。")

    final_soc = round(soc, 4)

    return (
        actual_charge, actual_discharge,
        arbitrage_charge, arbitrage_discharge,
        agc_charge, agc_discharge,
        actual_agc, agc_mileages, final_soc,
    )


# ============================================================
# 滚动 MPC 实时调度
# ============================================================

def optimize_real_time_mpc(
    storage: StorageParams,
    da_charge: List[float],
    da_discharge: List[float],
    da_agc_cap: List[float],
    da_lmp: List[float],
    rt_lmp: List[float],
    agc_commands: List[float],
    agc_mileage_price: float,
    settlement: SettlementParams,
    initial_soc: float,
    final_soc_target: Optional[float] = None,
    agc_reserve_mw: Optional[float] = None,
    horizon: int = 8,
    n_states: int = 401,
    lmp_bias_decay: float = 0.8,
    agc_forecast_mode: str = "zero",
) -> Tuple[List[float], List[float], List[float], List[float], List[float], List[float], List[float], List[float], float]:
    """
    滚动 MPC 实时调度。

    每个时刻 t 只利用当前已观测信息, 预测未来 H 个时段, 优化后仅执行第一个时段的套利决策。

    信息集 (时刻 t 已知):
      - 当前 SOC
      - rt_lmp[0:t]   (已观测实时 LMP)
      - agc_commands[0:t] (已观测 AGC 指令)
      - da_lmp[t:], da_charge[t:], da_discharge[t:] (日前计划作为预测基线)

    预测方法:
      - LMP: 日前基线 + 最新观测偏差按 lmp_bias_decay 衰减
      - AGC: 默认保守地预测为 0 (mode="zero")

    Args:
        horizon: 预测优化窗口长度 (默认 8 个 15 分钟时段 = 2 小时)
        lmp_bias_decay: 实时 LMP 偏差衰减系数
        agc_forecast_mode: "zero" | "persistence" | "expected"

    Returns:
        与 optimize_real_time 相同的 9 元组
    """
    from forecaster import Forecaster  # 局部导入, 避免循环依赖

    T = len(rt_lmp)
    if T != len(da_lmp):
        raise ValueError("rt_lmp 与 da_lmp 长度必须相同")

    forecaster = Forecaster(
        da_lmp=da_lmp,
        da_agc_cap=da_agc_cap,
        lmp_bias_decay=lmp_bias_decay,
    )

    # 初始化输出序列
    actual_charge = [0.0] * T
    actual_discharge = [0.0] * T
    arbitrage_charge = [0.0] * T
    arbitrage_discharge = [0.0] * T
    agc_charge = [0.0] * T
    agc_discharge = [0.0] * T
    actual_agc = [0.0] * T
    agc_mileage = [0.0] * T

    soc = initial_soc
    hours = 0.25
    capacity = storage.rated_capacity_mwh
    eta_chg = storage.charging_efficiency
    eta_dis = storage.discharging_efficiency
    self_discharge_mwh = storage.self_discharge_rate * hours * capacity

    for t in range(T):
        # 1. 更新预测器观测
        if t > 0:
            forecaster.update(t - 1, rt_lmp[t - 1], agc_commands[t - 1])

        # 2. 确定窗口
        end = min(t + horizon, T)
        H = end - t

        # 3. 生成预测
        lmp_fc = forecaster.predict_lmp(t, H)
        current_agc = agc_commands[t - 1] if t > 0 else 0.0
        agc_fc = forecaster.predict_agc(t, H, current_agc, mode=agc_forecast_mode)
        # 第 0 个时段用实际 AGC 命令
        agc_fc[0] = agc_commands[t]

        # 4. 截取子问题输入
        sub_da_charge = da_charge[t:end]
        sub_da_discharge = da_discharge[t:end]
        sub_da_agc_cap = da_agc_cap[t:end]

        # 5. 对窗口做 DP 优化
        sub_final_soc_target = final_soc_target if end == T else None
        (
            sub_actual_charge, sub_actual_discharge,
            sub_arbitrage_charge, sub_arbitrage_discharge,
            sub_agc_charge, sub_agc_discharge,
            sub_actual_agc, sub_agc_mileage, _,
        ) = optimize_real_time(
            storage=storage,
            da_charge=sub_da_charge,
            da_discharge=sub_da_discharge,
            da_agc_cap=sub_da_agc_cap,
            rt_lmp=lmp_fc,
            agc_commands=agc_fc,
            agc_mileage_price=agc_mileage_price,
            settlement=settlement,
            initial_soc=soc,
            final_soc_target=sub_final_soc_target,
            agc_reserve_mw=agc_reserve_mw,
            n_states=n_states,
        )

        # 6. 只执行第一个时段
        actual_charge[t] = sub_actual_charge[0]
        actual_discharge[t] = sub_actual_discharge[0]
        arbitrage_charge[t] = sub_arbitrage_charge[0]
        arbitrage_discharge[t] = sub_arbitrage_discharge[0]
        agc_charge[t] = sub_agc_charge[0]
        agc_discharge[t] = sub_agc_discharge[0]
        actual_agc[t] = sub_actual_agc[0]
        agc_mileage[t] = sub_agc_mileage[0]

        # 7. 更新 SOC
        chg_dc = actual_charge[t] * hours * eta_chg
        dis_dc = actual_discharge[t] * hours / eta_dis
        soc += (chg_dc - dis_dc - self_discharge_mwh) / capacity
        soc = max(storage.soc_min, min(storage.soc_max, soc))

    final_soc = round(soc, 4)
    return (
        actual_charge, actual_discharge,
        arbitrage_charge, arbitrage_discharge,
        agc_charge, agc_discharge,
        actual_agc, agc_mileage, final_soc,
    )
