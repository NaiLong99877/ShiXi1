"""
主计算器 — 从JSON读取输入, 执行计算, 输出结果JSON
"""
import os
import sys
from models import (
    StorageParams, DayAheadData, RealTimeData,
    AgcPerformance, SettlementParams, DailyResult
)
from settlement import calc_daily_settlement, print_summary

# 工作目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INPUT_DIR = os.path.join(BASE_DIR, "data", "input")
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "output")


def load_all(prefix: str = "") -> dict:
    """
    从 data/input/ 加载所有输入文件
    如果 prefix 非空, 则加载带前缀的文件 (用于多场景)
    """
    def fname(base):
        if prefix:
            # 如果存在带前缀的文件, 使用它; 否则使用默认
            prefixed = os.path.join(INPUT_DIR, f"{prefix}_{base}")
            if os.path.exists(prefixed):
                return prefixed
        return os.path.join(INPUT_DIR, base)

    storage = StorageParams.from_json(fname("storage_params.json"))
    da = DayAheadData.from_json(fname("day_ahead_market.json"))
    # 实时调度数据是优化器的计算结果, 在 output 目录
    rt_path = os.path.join(OUTPUT_DIR, f"{prefix}_real_time_dispatch.json") if prefix \
              else os.path.join(OUTPUT_DIR, "real_time_dispatch.json")
    rt = RealTimeData.from_json(rt_path)
    agc = AgcPerformance.from_json(fname("agc_performance.json"))
    settlement = SettlementParams.from_json(fname("settlement_params.json"))

    # 校验数据完整性
    assert len(da.intervals) == 96, \
        f"日前市场数据应有96个时段, 实际 {len(da.intervals)}"
    assert len(rt.intervals) == 96, \
        f"实时市场数据应有96个时段, 实际 {len(rt.intervals)}"
    # 校验时段一致性
    for i in range(96):
        assert da.intervals[i].time == rt.intervals[i].time, \
            f"时段{i}时间不匹配: {da.intervals[i].time} vs {rt.intervals[i].time}"

    return {
        "storage": storage,
        "da_data": da,
        "rt_data": rt,
        "agc_perf": agc,
        "settlement": settlement,
    }


def run(prefix: str = "", initial_soc: float = None) -> DailyResult:
    """
    执行单日结算计算

    Args:
        prefix: 数据文件前缀 (用于多场景), 空字符串表示使用默认文件
        initial_soc: 当日初始SOC, None则使用storage.initial_soc (跨日结转用)

    Returns:
        DailyResult: 日结算结果
    """
    data = load_all(prefix)

    daily = calc_daily_settlement(
        storage=data["storage"],
        da_data=data["da_data"],
        rt_data=data["rt_data"],
        agc_perf=data["agc_perf"],
        settlement=data["settlement"],
        initial_soc=initial_soc,
    )

    # 保存结果
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_name = f"{prefix}_daily_result.json" if prefix else "daily_result.json"
    output_path = os.path.join(OUTPUT_DIR, output_name)
    daily.to_json(output_path)

    return daily


def discover_dates() -> list:
    """
    扫描 data/input/ 目录, 找到所有日期前缀
    日期格式: YYYY-MM-DD_day_ahead_market.json
    """
    dates = set()
    for fname in os.listdir(INPUT_DIR):
        if "_day_ahead_market.json" in fname:
            prefix = fname.replace("_day_ahead_market.json", "")
            # 验证是否为有效日期格式 (YYYY-MM-DD)
            if len(prefix) == 10 and prefix[4] == "-" and prefix[7] == "-":
                dates.add(prefix)
    return sorted(dates)


def run_all() -> list:
    """
    批量执行所有日期的结算计算 (含跨日SOC结转)

    第一天使用 storage.initial_soc 作为初始SOC,
    之后每天以前一天的日末SOC作为当天的初始SOC。

    Returns:
        list of DailyResult: 按日期排序的结算结果列表
    """
    dates = discover_dates()
    if not dates:
        print("[ERROR] 未找到任何日期数据, 请先运行 generate_sample_data.py")
        return []

    print(f"发现 {len(dates)} 天数据: {dates[0]} ~ {dates[-1]}")
    results = []
    carry_soc = None  # 跨日结转SOC, 第一天为None(使用storage.initial_soc)

    for i, date_str in enumerate(dates):
        try:
            day_init = carry_soc  # 保存本日的初始SOC用于日志
            daily = run(date_str, initial_soc=carry_soc)
            results.append(daily)
            # 下一天的初始SOC = 本日日末SOC
            carry_soc = daily.final_soc

            if i == 0:
                print(f"  [{date_str}] 初始SOC=50.0% (配置) → 日末SOC={carry_soc:.1%}")
            else:
                print(f"  [{date_str}] 初始SOC={day_init:.1%} (结转自前一天) → 日末SOC={carry_soc:.1%}")
        except Exception as e:
            print(f"  [ERROR] {date_str} 计算失败: {e}")
            # 结转中断, 下一天重新从配置开始
            carry_soc = None

    # 汇总多日
    if results:
        total_profits = sum(r.net_profit for r in results)
        print(f"\n{'=' * 60}")
        print(f"  多日汇总: {len(results)}/{len(dates)} 天计算成功")
        print(f"  日均净利润: {total_profits/len(results):,.2f} 元")
        print(f"  累计净利润: {total_profits:,.2f} 元")
        last_soc = results[-1].final_soc if results else 0
        print(f"  首日初始SOC: 50.0% (配置)  →  末日日末SOC: {last_soc:.1%}")
        print("=" * 60)

    return results


def main():
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""

    try:
        if prefix == "--all" or prefix == "":
            # 默认: 批量运行所有日期
            run_all()
        else:
            # 单日模式
            daily = run(prefix)
            print_summary(daily)
    except FileNotFoundError as e:
        print(f"[ERROR] 找不到数据文件: {e}")
        print("请先运行 generate_sample_data.py 生成示例数据")
        sys.exit(1)
    except AssertionError as e:
        print(f"[ERROR] 数据校验失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
