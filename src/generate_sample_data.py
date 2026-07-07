"""
生成示例输入数据 — 模拟山东电力市场典型场景
生成典型的峰谷电价曲线和储能充放电策略
支持多日数据生成 (默认7天)
"""
import json
import math
import os
import random

from models import StorageParams, SettlementParams, RealTimeInterval
from optimizer import optimize_day_ahead, optimize_real_time, optimize_real_time_mpc, generate_agc_commands
from energy_market import simulate_soc
from validation import validate_dispatch_constraints

import argparse

# 工作目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = os.path.join(BASE_DIR, "data", "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "output")

# 要生成的日期列表 (7天: 周一到周日)
DATES = [
    "2026-06-24",  # 周三
    "2026-06-25",  # 周四
    "2026-06-26",  # 周五
    "2026-06-27",  # 周六
    "2026-06-28",  # 周日
    "2026-06-29",  # 周一
    "2026-06-30",  # 周二
]


def ensure_dir(d):
    os.makedirs(d, exist_ok=True)


def generate_time_labels():
    """生成96个15分钟时段的标签 HH:MM"""
    labels = []
    for i in range(96):
        h = i // 4
        m = (i % 4) * 15
        labels.append(f"{h:02d}:{m:02d}")
    return labels


def seed_from_date(date_str, offset=0):
    """从日期字符串生成确定性的随机种子"""
    return int(date_str.replace("-", "")) + offset


def generate_lmp_curve(rng: random.Random, is_weekend: bool):
    """
    生成典型的日前LMP电价曲线
    模拟山东现货市场特征:
    - 凌晨低谷(0-6h): 200-280 元/MWh
    - 早高峰(7-9h): 380-500 元/MWh
    - 白天平段(10-16h): 320-400 元/MWh
    - 晚高峰(17-20h): 420-550 元/MWh
    - 夜间(21-23h): 280-350 元/MWh

    周末: 整体电价下降 5-10%, 峰谷差缩小
    """
    times = generate_time_labels()
    lmp = []

    weekend_scale = 0.92 if is_weekend else 1.0  # 周末电价整体打92折

    for i, t in enumerate(times):
        h = i / 4.0  # 转换为小时 (浮点)
        base = 300
        morning_peak = 180 * math.exp(-((h - 8.5) / 2) ** 2)
        evening_peak = 220 * math.exp(-((h - 19) / 2.5) ** 2)
        solar_dip = -40 * math.exp(-((h - 12.5) / 1.5) ** 2)
        night_valley = -60 * math.exp(-((h - 3) / 3) ** 2)

        price = base + morning_peak + evening_peak + solar_dip + night_valley
        price *= weekend_scale

        # 加日期相关的随机噪声 (±8 元)
        noise = rng.uniform(-8, 8)
        lmp.append(round(price + noise, 2))
    return times, lmp




def generate_one_day(date_str: str, initial_soc: float = 0.50, use_mpc: bool = True, mpc_horizon: int = 8) -> dict:
    """
    生成单日的市场数据 (优化版 + AGC)

    - 日前: DP优化能量套利+AGC容量收益, 永远留1MW AGC裕量
    - 实时: AGC命令随机下发(必须响应);
            use_mpc=True 时, 采用滚动 MPC 优化 (默认);
            use_mpc=False 时, 采用全局事后 (hindsight) 优化。

    Args:
        date_str: 日期字符串 YYYY-MM-DD
        initial_soc: 当日初始SOC (跨日结转, 第一天使用 storage_params 中的 initial_soc)
        use_mpc: 是否使用滚动 MPC 实时调度
        mpc_horizon: MPC 预测窗口长度 (默认 8 个 15 分钟时段 = 2 小时)

    Returns:
        { "day_ahead": {...}, "real_time": {...}, "est_final_soc": float }
    """
    from datetime import date
    weekday = date.fromisoformat(date_str).weekday()
    is_weekend = weekday >= 5

    seed = seed_from_date(date_str)
    rng = random.Random(seed)

    times, lmp_curve = generate_lmp_curve(rng, is_weekend)

    # ---- 构建参数对象 (统一从 storage_params.json 读取) ----
    storage = StorageParams.from_json(os.path.join(INPUT_DIR, "storage_params.json"))
    # 跨日结转时, 用传入的 initial_soc 覆盖配置中的初始SOC
    storage = StorageParams(**{**storage.__dict__, "initial_soc": initial_soc})
    settlement = SettlementParams.from_json(os.path.join(INPUT_DIR, "settlement_params.json"))

    # ---- 生成AGC价格 (日前市场输入) ----
    agc_mileage_price = round(40 + rng.uniform(0, 20), 2)  # AGC里程价格 元/MW
    agc_prices = []
    for i in range(96):
        h = i / 4.0
        # AGC容量报价: 高峰时段略高
        if 6 <= h < 10 or 16 <= h < 21:
            agc_prices.append(round(6 + rng.uniform(0, 1.5), 2))
        else:
            agc_prices.append(round(5 + rng.uniform(0, 1.5), 2))

    # ---- 日前优化: 能量套利 + AGC容量 (功率裕量与容量上限均来自 storage_params) ----
    charges, discharges, agc_caps, da_final_soc = optimize_day_ahead(
        storage, lmp_curve, agc_prices, agc_mileage_price, initial_soc,
        final_soc_target=storage.initial_soc,  # 鼓励回到初始SOC附近(20%~80%软区间内)
    )

    # ---- 日前SOC仿真 + AGC容量/报价按SOC位置缩放 ----
    soc = initial_soc
    da_soc_path = []
    adj_agc_caps = []
    adj_agc_prices = []
    soc_range = storage.soc_max - storage.soc_min
    for i in range(96):
        da_soc_path.append(round(soc, 4))
        # 容量因子: SOC居中→1.0, SOC靠边→0
        room_up   = max(0.0, (storage.soc_max - soc) / soc_range)
        room_down = max(0.0, (soc - storage.soc_min) / soc_range)
        factor = min(1.0, room_up, room_down)
        # AGC容量固定为配置上限(本场景10MW), 不再随SOC缩放
        adj_agc_caps.append(storage.agc_max_capacity_mw)
        # 报价: factor低时涨价 (factor=1→原价, factor=0.25→1.75倍, factor→0→2倍)
        adj_agc_prices.append(round(agc_prices[i] * (2.0 - factor), 2))
        # 更新SOC (含自放电)
        if i < 95:
            chg_mwh = charges[i] * 0.25 * storage.charging_efficiency
            dis_mwh = discharges[i] * 0.25 / storage.discharging_efficiency
            self_dis = storage.self_discharge_rate * 0.25 * storage.rated_capacity_mwh
            soc = max(storage.soc_min, min(storage.soc_max,
                soc + (chg_mwh - dis_mwh - self_dis) / storage.rated_capacity_mwh))

    # ---- 生成AGC调控命令 (实时, 稀疏下发: 约15%时段) ----
    # AGC 指令最大幅值与日前中标容量一致 (不超过中标容量)
    agc_commands = generate_agc_commands(
        n_intervals=96, max_agc_mw=storage.agc_max_capacity_mw, seed=seed + 7777,
        sparse=True,  # 大部分时段为0
    )

    # ---- 实时LMP (日前+噪声) ----
    rng_rt = random.Random(seed + 9999)
    rt_lmp_curve = [round(lmp_curve[i] + rng_rt.uniform(-6, 6), 2) for i in range(96)]

    # ---- 实时调度: AGC命令必须响应, 套利只能在日前中标范围内行权或放弃 ----
    if use_mpc:
        (
            rt_actual_charge, rt_actual_discharge,
            rt_arbitrage_charge, rt_arbitrage_discharge,
            rt_agc_charge, rt_agc_discharge,
            rt_agc_mw, rt_agc_miles, _
        ) = optimize_real_time_mpc(
            storage, charges, discharges, adj_agc_caps,
            lmp_curve, rt_lmp_curve, agc_commands, agc_mileage_price,
            settlement, initial_soc,
            final_soc_target=storage.initial_soc,
            horizon=mpc_horizon,
            agc_forecast_mode="zero",
        )
    else:
        (
            rt_actual_charge, rt_actual_discharge,
            rt_arbitrage_charge, rt_arbitrage_discharge,
            rt_agc_charge, rt_agc_discharge,
            rt_agc_mw, rt_agc_miles, _
        ) = optimize_real_time(
            storage, charges, discharges, adj_agc_caps,
            rt_lmp_curve, agc_commands, agc_mileage_price,
            settlement, initial_soc,
            final_soc_target=storage.initial_soc,
        )

    # ---- 用统一的SOC仿真计算实际运行轨迹(含自放电), 保证与结算端一致 ----
    rt_intervals_for_soc = [
        RealTimeInterval(
            time=times[i],
            rt_lmp_yuan_per_mwh=rt_lmp_curve[i],
            actual_charge_mw=rt_actual_charge[i],
            actual_discharge_mw=rt_actual_discharge[i],
            arbitrage_charge_mw=rt_arbitrage_charge[i],
            arbitrage_discharge_mw=rt_arbitrage_discharge[i],
            agc_charge_mw=rt_agc_charge[i],
            agc_discharge_mw=rt_agc_discharge[i],
            actual_agc_mw=rt_agc_mw[i],
            agc_mileage_mw=rt_agc_miles[i],
        )
        for i in range(96)
    ]
    rt_soc_path, _, _ = simulate_soc(storage, rt_intervals_for_soc, initial_soc)
    est_final_soc = rt_soc_path[-1]

    # ---- 组装日前数据 ----
    day_ahead = {
        "date": date_str,
        "node_name": "节点A",
        "agc_mileage_price": agc_mileage_price,
        "intervals": [
            {
                "time": times[i],
                "lmp_yuan_per_mwh": lmp_curve[i],
                "win_charge_mw": charges[i],
                "win_discharge_mw": discharges[i],
                "win_agc_capacity_mw": adj_agc_caps[i],
                "agc_capacity_price": adj_agc_prices[i],
                "soc": da_soc_path[i],
            }
            for i in range(96)
        ],
    }

    # ---- 组装实时数据 ----
    real_time = {
        "date": date_str,
        "intervals": [
            {
                "time": times[i],
                "rt_lmp_yuan_per_mwh": rt_lmp_curve[i],
                "actual_charge_mw": rt_actual_charge[i],
                "actual_discharge_mw": rt_actual_discharge[i],
                "arbitrage_charge_mw": rt_arbitrage_charge[i],
                "arbitrage_discharge_mw": rt_arbitrage_discharge[i],
                "agc_charge_mw": rt_agc_charge[i],
                "agc_discharge_mw": rt_agc_discharge[i],
                "actual_agc_mw": rt_agc_mw[i],
                "agc_mileage_mw": rt_agc_miles[i],
                "soc": rt_soc_path[i],
            }
            for i in range(96)
        ],
    }

    # ---- 硬约束校验 ----
    violations = validate_dispatch_constraints(
        storage_params=storage.__dict__,
        day_ahead_intervals=day_ahead["intervals"],
        real_time_intervals=real_time["intervals"],
        initial_soc=initial_soc,
    )
    if violations:
        print(f"  [WARN] {date_str} 硬约束校验发现 {len(violations)} 处违规:")
        for v in violations[:5]:
            print(f"    - {v}")
        if len(violations) > 5:
            print(f"    ... 还有 {len(violations) - 5} 处未显示")

    return {
        "day_ahead": day_ahead,
        "real_time": real_time,
        "est_final_soc": round(est_final_soc, 4),
    }


def save_json(data, filename):
    path = os.path.join(INPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  [OK] {filename}")


def main(use_mpc: bool = True, mpc_horizon: int = 8):
    ensure_dir(INPUT_DIR)
    ensure_dir(OUTPUT_DIR)
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    print("=" * 60)
    print(f"  生成 {len(DATES)} 天示例数据 ({DATES[0]} ~ {DATES[-1]})")
    mode_str = "滚动 MPC" if use_mpc else "全局 hindsight"
    print(f"  实时调度模式: {mode_str} (horizon={mpc_horizon})")
    print("=" * 60)

    # ===== 共用文件 (只生成一份) =====
    print("\n[共用参数]")

    storage_params = {
        "_comment_file": "储能电站聚合模型参数。所有比例类参数均为小数(如0.10表示10%)。",
        "name": "示例储能电站",
        "rated_power_mw": 100,
        "rated_capacity_mwh": 200,
        "charging_efficiency": 0.92,
        "discharging_efficiency": 0.92,
        "soc_min": 0.10,
        "_comment_soc_min": "SOC 物理下限(硬边界), 放电时不得低于该值",
        "soc_max": 0.90,
        "_comment_soc_max": "SOC 物理上限(硬边界), 充电时不得高于该值",
        "agc_soc_reserve": 0.10,
        "_comment_agc_soc_reserve": "AGC 能量裕量比例: 在 SOC 上下硬边界各预留 10% 给 AGC 调用",
        "soc_soft_min": 0.20,
        "_comment_soc_soft_min": "套利放电软下限 = soc_min + agc_soc_reserve = 0.20, 低于此值时常规放电暂停,仅响应 AGC 放电",
        "soc_soft_max": 0.80,
        "_comment_soc_soft_max": "套利充电软上限 = soc_max - agc_soc_reserve = 0.80, 高于此值时常规充电暂停,仅响应 AGC 充电",
        "ramp_rate_mw_per_min": 10,
        "self_discharge_rate": 0.001,
        "initial_soc": 0.50,
        "cycles_per_day": 2,
        "agc_reserve_mw": 1.0,
        "_comment_agc_reserve_mw": "AGC 功率裕量: 额定功率中永远预留 1 MW 不参与能量套利,专门用于 AGC 上调/下调",
        "agc_max_capacity_mw": 30.0,
        "_comment_agc_max_capacity_mw": "AGC 中标容量上限,本场景固定为 30 MW",
    }
    save_json(storage_params, "storage_params.json")

    agc_perf = {
        "date": DATES[-1],
        "k1_speed": 1.15,
        "k1_base": 1.0,
        "k2_precision": 1.08,
        "k2_base": 1.0,
        "k3_response": 0.95,
        "k3_base": 1.0,
        "weight_k1": 0.35,
        "weight_k2": 0.40,
        "weight_k3": 0.25,
        "kp_avg": 1.05,
    }
    save_json(agc_perf, "agc_performance.json")

    settlement = {
        "deviation_tolerance": 0.03,
        "deviation_penalty_rate": 0.10,
        "transmission_fee_rate": 0.03,
        "gov_fund_rate": 0.019,
        "ancillary_cost_rate": 0.005,
        "agc_unqualified_penalty": 500.0,
        "battery_degradation_per_mwh": 0.0,
        "self_consumption_rate": 0.0,
        "daily_om_cost_yuan": 0.0,
    }
    save_json(settlement, "settlement_params.json")

    # ===== 逐日生成 (含跨日SOC结转) =====
    print(f"\n[逐日市场数据]")
    from datetime import date as dt_date
    # 第一天从 storage_params.json 中的 initial_soc 开始
    carry_soc = storage_params["initial_soc"]

    for i, date_str in enumerate(DATES):
        wd = dt_date.fromisoformat(date_str).weekday()
        tag = f"{weekday_names[wd]}{'(周末)' if wd >= 5 else ''}"
        print(f"\n  {date_str} {tag}  初始SOC={carry_soc:.1%}")

        data = generate_one_day(date_str, initial_soc=carry_soc, use_mpc=use_mpc, mpc_horizon=mpc_horizon)
        save_json(data["day_ahead"], f"{date_str}_day_ahead_market.json")
        # 实时调度结果 → data/output/ (它是优化器的计算结果, 不是市场输入)
        out_path = os.path.join(OUTPUT_DIR, f"{date_str}_real_time_dispatch.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data["real_time"], f, ensure_ascii=False, indent=2)
        print(f"  [OK] {date_str}_real_time_dispatch.json → output/")

        # 下一天的初始SOC = 本日估算日末SOC
        carry_soc = data["est_final_soc"]
        print(f"    → 估算日末SOC={carry_soc:.1%} (结转至下一天)")

    print(f"\n{'=' * 60}")
    print(f"  全部完成! 共生成 {len(DATES)} 天数据")
    print(f"  文件位置: {INPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成储能电站示例输入数据")
    parser.add_argument(
        "--no-mpc", action="store_true",
        help="禁用滚动 MPC, 使用全局 hindsight 优化生成实时调度",
    )
    parser.add_argument(
        "--horizon", type=int, default=8,
        help="MPC 预测窗口长度 (默认 8, 即 2 小时)",
    )
    args = parser.parse_args()
    main(use_mpc=not args.no_mpc, mpc_horizon=args.horizon)
