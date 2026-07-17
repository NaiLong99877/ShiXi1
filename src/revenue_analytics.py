"""
运营分析与收益预测模块
========================
基于已有的 data/output/*_daily_result.json 进行多日聚合、月度预测、年度目标追踪
以及聚合单元/台区资产绩效排名。

注意：本模块只做只读分析，不重新运行优化或结算（除非显式调用回测模式做排名）。
"""
import calendar
import json
import math
import os
import statistics
from datetime import date, timedelta
from typing import Dict, List, Optional

from models import SettlementParams


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "data", "output")


# ============================================================
# 数据发现
# ============================================================

def discover_daily_results(output_dir: str = OUTPUT_DIR) -> List[dict]:
    """
    扫描输出目录，返回所有普通日结算结果（排除 _highnoise / _extremenoise 变体）。
    结果按日期升序排列。
    """
    results = []
    if not os.path.isdir(output_dir):
        return results
    for fname in sorted(os.listdir(output_dir)):
        if not fname.endswith("_daily_result.json"):
            continue
        # 排除噪声测试变体
        if "_highnoise_" in fname or "_extremenoise_" in fname:
            continue
        prefix = fname.replace("_daily_result.json", "")
        if len(prefix) != 10 or prefix[4] != "-" or prefix[7] != "-":
            continue
        path = os.path.join(output_dir, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_path"] = path
            results.append(data)
        except Exception:
            continue
    return results


def _filter_by_date_range(
    results: List[dict],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[dict]:
    """按日期范围过滤结果。"""
    filtered = results
    if start_date:
        filtered = [r for r in filtered if r.get("date", "") >= start_date]
    if end_date:
        filtered = [r for r in filtered if r.get("date", "") <= end_date]
    return filtered


# ============================================================
# 日期与星期工具
# ============================================================

def _parse_date(date_str: str) -> date:
    """解析 YYYY-MM-DD。"""
    y, m, d = map(int, date_str.split("-"))
    return date(y, m, d)


def _weekday(date_str: str) -> int:
    """返回星期，0=周一（与 Python datetime 一致）。"""
    return _parse_date(date_str).weekday()


def _generate_dates(
    start_date: Optional[str],
    end_date: Optional[str],
    weekdays: Optional[List[int]] = None,
) -> List[str]:
    """
    生成 [start_date, end_date] 内所有日期；若传 weekdays 则只保留匹配星期。
    默认 start_date/end_date 为实际数据的最小/最大日期。
    """
    all_results = discover_daily_results()
    if not all_results:
        return []

    if start_date is None:
        start_date = all_results[0].get("date", "")
    if end_date is None:
        end_date = all_results[-1].get("date", "")

    if not start_date or not end_date:
        return []

    start = _parse_date(start_date)
    end = _parse_date(end_date)
    if start > end:
        return []

    dates = []
    cur = start
    while cur <= end:
        d_str = cur.strftime("%Y-%m-%d")
        if weekdays is None or cur.weekday() in weekdays:
            dates.append(d_str)
        cur += timedelta(days=1)
    return dates


# ============================================================
# 带星期补全的样本加载
# ============================================================

def _summary_to_row(r: dict, imputed: bool = False) -> dict:
    """把单日结果转成分析用行。"""
    s = r.get("summary", {})
    return {
        "date": r.get("date", ""),
        "node_name": r.get("node_name", ""),
        "storage_name": r.get("storage_name", ""),
        "energy_revenue": round(s.get("total_energy_revenue", 0.0), 2),
        "agc_capacity_revenue": round(s.get("total_agc_capacity_revenue", 0.0), 2),
        "agc_mileage_revenue": round(s.get("total_agc_mileage_revenue", 0.0), 2),
        "gross_revenue": round(s.get("total_gross_revenue", 0.0), 2),
        "net_profit": round(s.get("net_profit", 0.0), 2),
        "total_cost": round(s.get("total_cost", 0.0), 2),
        "total_deviation_penalty": round(s.get("total_deviation_penalty", 0.0), 2),
        "kp": s.get("kp", 1.0),
        "total_charge_mwh": round(s.get("total_charge_mwh", 0.0), 2),
        "total_discharge_mwh": round(s.get("total_discharge_mwh", 0.0), 2),
        "imputed": imputed,
    }


def load_daily_results_with_imputation(
    selected_dates: List[str],
    output_dir: str = OUTPUT_DIR,
    impute_by_weekday: bool = True,
) -> List[dict]:
    """
    按 selected_dates 构建样本；缺失日期用同星期均值补全，无同星期则 fallback 全局均值。
    返回带 imputed 标记的 sample_rows。
    """
    actual = {r.get("date", ""): r for r in discover_daily_results(output_dir)}

    if not actual:
        return []

    # 实际样本行（用于 fallback 均值）
    actual_rows = [_summary_to_row(r, imputed=False) for r in actual.values()]
    global_mean = _row_mean(actual_rows)

    sample_rows = []
    for d in selected_dates:
        if d in actual:
            sample_rows.append(_summary_to_row(actual[d], imputed=False))
            continue

        if not impute_by_weekday:
            imputed = dict(global_mean)
            imputed["date"] = d
            imputed["imputed"] = True
            sample_rows.append(imputed)
            continue

        # 同星期均值
        wd = _weekday(d)
        same_weekday_rows = [r for r in actual_rows if _weekday(r["date"]) == wd]
        if same_weekday_rows:
            base = _row_mean(same_weekday_rows)
        else:
            base = global_mean
        imputed = dict(base)
        imputed["date"] = d
        imputed["imputed"] = True
        sample_rows.append(imputed)

    return sample_rows


def _row_mean(rows: List[dict]) -> dict:
    """对 rows 求各数值字段均值。"""
    numeric_keys = [
        "energy_revenue", "agc_capacity_revenue", "agc_mileage_revenue",
        "gross_revenue", "net_profit", "total_cost", "total_deviation_penalty",
        "kp", "total_charge_mwh", "total_discharge_mwh",
    ]
    if not rows:
        return {k: 0.0 for k in numeric_keys}
    result = {"date": "", "node_name": "", "storage_name": "", "imputed": True}
    for k in numeric_keys:
        vals = [r.get(k, 0.0) for r in rows if isinstance(r.get(k, 0.0), (int, float))]
        result[k] = round(sum(vals) / len(vals), 2) if vals else 0.0
    return result


# ============================================================
# 多日收益拆分分析
# ============================================================

def multi_day_revenue_breakdown(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    weekdays: Optional[List[int]] = None,
    impute: bool = False,
    selected_dates: Optional[List[str]] = None,
    output_dir: str = OUTPUT_DIR,
) -> dict:
    """
    聚合指定日期范围内的日结算结果，输出收益拆分。

    Args:
        start_date, end_date: 日期范围（YYYY-MM-DD）。
        weekdays: 可选星期过滤，[0,1,2,3,4,5,6]，0=周一。
        impute: 是否对缺失日期按同星期补全。
        selected_dates: 若直接传入，则优先使用该列表。

    Returns:
        {
            "start_date": str,
            "end_date": str,
            "days": int,
            "actual_days": int,
            "imputed_days": int,
            "daily": [ {...}, ... ],
            "totals": { ... },
            "component_pct": { "energy": float, "agc_capacity": float, "agc_mileage": float },
            "averages": { ... },
        }
    """
    if selected_dates is None:
        selected_dates = _generate_dates(start_date, end_date, weekdays)

    if not selected_dates:
        return {
            "start_date": start_date or "",
            "end_date": end_date or "",
            "days": 0,
            "actual_days": 0,
            "imputed_days": 0,
            "daily": [],
            "totals": {},
            "component_pct": {},
            "averages": {},
        }

    daily_rows = load_daily_results_with_imputation(
        selected_dates, output_dir, impute_by_weekday=impute
    )

    if not daily_rows:
        return {
            "start_date": selected_dates[0],
            "end_date": selected_dates[-1],
            "days": len(selected_dates),
            "actual_days": 0,
            "imputed_days": 0,
            "daily": [],
            "totals": {},
            "component_pct": {},
            "averages": {},
        }

    keys = [
        "energy_revenue", "agc_capacity_revenue", "agc_mileage_revenue",
        "gross_revenue", "net_profit", "total_cost", "total_deviation_penalty",
    ]
    totals = {k: round(sum(r[k] for r in daily_rows), 2) for k in keys}
    averages = {k: round(totals[k] / len(daily_rows), 2) for k in keys}

    gross = totals.get("gross_revenue", 0.0)
    component_pct = {
        "energy": round(totals.get("energy_revenue", 0.0) / gross * 100, 2) if gross else 0.0,
        "agc_capacity": round(totals.get("agc_capacity_revenue", 0.0) / gross * 100, 2) if gross else 0.0,
        "agc_mileage": round(totals.get("agc_mileage_revenue", 0.0) / gross * 100, 2) if gross else 0.0,
    }

    actual_days = sum(1 for r in daily_rows if not r.get("imputed"))
    imputed_days = sum(1 for r in daily_rows if r.get("imputed"))

    return {
        "start_date": selected_dates[0],
        "end_date": selected_dates[-1],
        "days": len(daily_rows),
        "actual_days": actual_days,
        "imputed_days": imputed_days,
        "daily": daily_rows,
        "totals": totals,
        "component_pct": component_pct,
        "averages": averages,
    }


# ============================================================
# 月度收益预测
# ============================================================

def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: List[float]) -> float:
    if len(values) >= 2:
        return statistics.stdev(values)
    return 0.0


def _forecast_from_samples(
    sample_rows: List[dict],
    keys: List[str],
    total_days: int,
    z_score: float = 1.96,
) -> dict:
    """通用预测：样本已执行 + 日均均值推演剩余天数。"""
    executed_days = len(sample_rows)
    if executed_days == 0:
        empty = {k: 0.0 for k in keys}
        return {
            "executed_days": 0,
            "remaining_days": total_days,
            "executed_total": empty,
            "daily_mean": empty,
            "daily_std": empty,
            "forecast": empty,
            "confidence_interval": {
                "confidence": round((z_score / 1.96) * 0.95, 4),
                "z": z_score,
                "net_profit": {"low": 0.0, "high": 0.0},
                "gross_revenue": {"low": 0.0, "high": 0.0},
            },
            "note": "无样本数据，无法预测。",
        }

    daily_values = {k: [] for k in keys}
    executed_total = {k: 0.0 for k in keys}
    for r in sample_rows:
        executed_total["energy_revenue"] += r.get("energy_revenue", 0.0)
        executed_total["agc_capacity_revenue"] += r.get("agc_capacity_revenue", 0.0)
        executed_total["agc_mileage_revenue"] += r.get("agc_mileage_revenue", 0.0)
        executed_total["gross_revenue"] += r.get("gross_revenue", 0.0)
        executed_total["net_profit"] += r.get("net_profit", 0.0)
        executed_total["total_cost"] += r.get("total_cost", 0.0)
        for k in keys:
            daily_values[k].append(r.get(k, 0.0))

    for k in keys:
        executed_total[k] = round(executed_total[k], 2)

    daily_mean = {k: round(_mean(daily_values[k]), 2) for k in keys}
    daily_std = {k: round(_std(daily_values[k]), 2) for k in keys}

    remaining_days = max(0, total_days - executed_days)
    forecast = {
        k: round(executed_total[k] + daily_mean[k] * remaining_days, 2)
        for k in keys
    }

    confidence_interval = {
        "confidence": round((z_score / 1.96) * 0.95, 4),
        "z": z_score,
    }
    for metric in ["net_profit", "gross_revenue"]:
        margin = z_score * daily_std[metric] * math.sqrt(remaining_days) if remaining_days > 0 else 0.0
        confidence_interval[metric] = {
            "low": round(forecast[metric] - margin, 2),
            "high": round(forecast[metric] + margin, 2),
        }

    note = (
        f"基于 {executed_days} 天样本数据预测，其中含 {sum(1 for r in sample_rows if r.get('imputed'))} 天补全数据。"
        if any(r.get("imputed") for r in sample_rows)
        else f"基于 {executed_days} 天样本数据预测。"
    )

    return {
        "executed_days": executed_days,
        "remaining_days": remaining_days,
        "executed_total": executed_total,
        "daily_mean": daily_mean,
        "daily_std": daily_std,
        "forecast": forecast,
        "confidence_interval": confidence_interval,
        "note": note,
    }


def monthly_revenue_forecast(
    year_month: str,
    output_dir: str = OUTPUT_DIR,
    z_score: float = 1.96,
) -> dict:
    """
    基于当月已执行数据预测本月总收益（旧接口，保持兼容）。
    """
    year, month = map(int, year_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]

    all_results = discover_daily_results(output_dir)
    month_results = [
        r for r in all_results
        if r.get("date", "").startswith(year_month)
    ]
    sample_rows = [_summary_to_row(r) for r in month_results]

    result = _forecast_from_samples(sample_rows, [
        "energy_revenue", "agc_capacity_revenue", "agc_mileage_revenue",
        "gross_revenue", "net_profit", "total_cost",
    ], days_in_month, z_score)
    result["year_month"] = year_month
    result["days_in_month"] = days_in_month
    return result


def monthly_revenue_forecast_by_days(
    year_month: str,
    sample_rows: List[dict],
    z_score: float = 1.96,
) -> dict:
    """
    基于用户选定的 sample_rows 中落在目标月的行，预测该月总收益。
    """
    year, month = map(int, year_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    month_rows = [r for r in sample_rows if r.get("date", "").startswith(year_month)]

    result = _forecast_from_samples(month_rows, [
        "energy_revenue", "agc_capacity_revenue", "agc_mileage_revenue",
        "gross_revenue", "net_profit", "total_cost",
    ], days_in_month, z_score)
    result["year_month"] = year_month
    result["days_in_month"] = days_in_month
    return result


# ============================================================
# 年度收益目标追踪
# ============================================================

def annual_target_tracking(
    year: str,
    settlement: SettlementParams,
    output_dir: str = OUTPUT_DIR,
    z_score: float = 1.96,
) -> dict:
    """
    相对年度收益目标的完成进度追踪（旧接口，保持兼容）。
    """
    all_results = discover_daily_results(output_dir)
    year_results = [r for r in all_results if r.get("date", "").startswith(year)]
    sample_rows = [_summary_to_row(r) for r in year_results]
    return _annual_target_from_samples(year, sample_rows, settlement, z_score)


def annual_target_tracking_by_days(
    year: str,
    sample_rows: List[dict],
    settlement: SettlementParams,
    z_score: float = 1.96,
) -> dict:
    """
    基于用户选定的 sample_rows 中落在目标年的行，分析该年目标进度。
    """
    year_rows = [r for r in sample_rows if r.get("date", "").startswith(year)]
    return _annual_target_from_samples(year, year_rows, settlement, z_score)


def _annual_target_from_samples(
    year: str,
    sample_rows: List[dict],
    settlement: SettlementParams,
    z_score: float = 1.96,
) -> dict:
    keys = [
        "energy_revenue", "agc_capacity_revenue", "agc_mileage_revenue",
        "gross_revenue", "net_profit", "total_cost",
    ]
    executed_days = len(sample_rows)

    ytd = {k: 0.0 for k in keys}
    for r in sample_rows:
        for k in keys:
            ytd[k] += r.get(k, 0.0)
    for k in keys:
        ytd[k] = round(ytd[k], 2)

    target = settlement.annual_target_yuan
    progress_pct = round(ytd["net_profit"] / target * 100, 4) if target > 0 else 0.0

    projected_annual = {
        "net_profit": round(ytd["net_profit"] / executed_days * 365, 2) if executed_days else 0.0,
        "gross_revenue": round(ytd["gross_revenue"] / executed_days * 365, 2) if executed_days else 0.0,
    }
    gap_to_target = round(projected_annual["net_profit"] - target, 2)

    # 月度预测仍用全年已有数据或样本数据
    monthly_forecasts = []
    for month in range(1, 13):
        ym = f"{year}-{month:02d}"
        month_rows = [r for r in sample_rows if r.get("date", "").startswith(ym)]
        fc = _forecast_from_samples(month_rows, keys, calendar.monthrange(int(year), month)[1], z_score)
        monthly_forecasts.append({
            "year_month": ym,
            "executed_days": fc["executed_days"],
            "forecast_net_profit": fc["forecast"].get("net_profit", 0.0),
            "forecast_gross_revenue": fc["forecast"].get("gross_revenue", 0.0),
        })

    if target <= 0:
        note = "未配置年度收益目标（annual_target_yuan=0），仅展示样本累计。"
    elif progress_pct >= 100:
        note = "年度目标已达成（基于样本推演）。"
    elif gap_to_target >= 0:
        note = f"按当前日均收益推演，全年预计可完成目标，超额 {gap_to_target:,.2f} 元。"
    else:
        note = (
            f"按当前日均收益推演，全年预计缺口 {-gap_to_target:,.2f} 元，"
            f"建议提高 AGC 申报比例或优化套利策略。"
        )

    return {
        "year": year,
        "annual_target_yuan": target,
        "executed_days": executed_days,
        "ytd": ytd,
        "progress_pct": progress_pct,
        "projected_annual": projected_annual,
        "gap_to_target": gap_to_target,
        "monthly_forecasts": monthly_forecasts,
        "note": note,
    }


# ============================================================
# 资产绩效排名
# ============================================================

def _load_district_mapping() -> Dict[str, str]:
    """加载可选的台区映射；缺失则返回空字典。"""
    path = os.path.join(BASE_DIR, "data", "input", "district_mapping.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _synthetic_district(vpp_name: str, config_id) -> str:
    """无映射时生成合成台区名。"""
    return f"{vpp_name}台区"


def _sample_total_net_profit(sample_rows: List[dict]) -> float:
    return round(sum(r.get("net_profit", 0.0) for r in sample_rows), 2)


def asset_performance_ranking(
    sample_rows: List[dict],
    configs: List[dict],
    group_by: str = "vpp",
    method: str = "proration",
    output_dir: str = OUTPUT_DIR,
) -> dict:
    """
    资产绩效排名：按单 MW 净利润降序。

    Args:
        sample_rows: 已选样本行（可含补全）。
        configs: VPP 配置列表。
        group_by: "vpp" 或 "district"。
        method: "proration"（按功率分摊）或 "backtest"（逐日回测）。
        output_dir: 用于缓存 backtest 结果。

    Returns:
        {
            "group_by": str,
            "method": str,
            "total_net_profit": float,
            "ranking": [
                {
                    "rank": int,
                    "id": str/int,
                    "name": str,
                    "rated_power_mw": float,
                    "net_profit": float,
                    "net_profit_per_mw": float,
                    "share_pct": float,
                }, ...
            ],
            "note": str,
        }
    """
    if not configs:
        return {"group_by": group_by, "method": method, "total_net_profit": 0.0, "ranking": [], "note": "无 VPP 配置。"}

    total_net_profit = _sample_total_net_profit(sample_rows)
    mapping = _load_district_mapping()

    if method == "backtest":
        unit_results = _compute_unit_results_by_backtest(sample_rows, configs, output_dir)
    else:
        unit_results = _compute_unit_results_by_proration(sample_rows, configs, total_net_profit)

    # group_by 聚合
    groups: Dict[str, dict] = {}
    for cfg in configs:
        cid = cfg["config_id"]
        vpp_name = cfg.get("vpp_name", f"VPP{cid}")
        district = mapping.get(vpp_name) or _synthetic_district(vpp_name, cid)
        key = vpp_name if group_by == "vpp" else district

        if key not in groups:
            groups[key] = {
                "id": cid if group_by == "vpp" else district,
                "name": key,
                "rated_power_mw": 0.0,
                "net_profit": 0.0,
            }
        u = unit_results.get(cid, {"net_profit": 0.0})
        groups[key]["rated_power_mw"] += cfg.get("rated_power_mw", 0.0)
        groups[key]["net_profit"] += u.get("net_profit", 0.0)

    ranking = []
    for g in groups.values():
        power = g["rated_power_mw"]
        profit = round(g["net_profit"], 2)
        per_mw = round(profit / power, 2) if power > 0 else 0.0
        share_pct = round(profit / total_net_profit * 100, 2) if total_net_profit else 0.0
        ranking.append({
            "id": g["id"],
            "name": g["name"],
            "rated_power_mw": round(power, 2),
            "net_profit": profit,
            "net_profit_per_mw": per_mw,
            "share_pct": share_pct,
        })

    ranking.sort(key=lambda x: x["net_profit_per_mw"], reverse=True)
    for i, item in enumerate(ranking, 1):
        item["rank"] = i

    note = (
        f"按 {('聚合单元' if group_by == 'vpp' else '台区')} 排名，"
        f"方法：{'容量分摊估算' if method == 'proration' else '逐日回测'}。"
    )

    return {
        "group_by": group_by,
        "method": method,
        "total_net_profit": total_net_profit,
        "ranking": ranking,
        "note": note,
    }


def _compute_unit_results_by_proration(
    sample_rows: List[dict],
    configs: List[dict],
    total_net_profit: float,
) -> Dict:
    """按额定功率占比将总净利润分摊到各 VPP。"""
    total_power = sum(c.get("rated_power_mw", 0.0) for c in configs)
    unit_results = {}
    for cfg in configs:
        cid = cfg["config_id"]
        power = cfg.get("rated_power_mw", 0.0)
        share = power / total_power if total_power > 0 else 0.0
        unit_results[cid] = {
            "net_profit": round(total_net_profit * share, 2),
            "rated_power_mw": power,
        }
    return unit_results


def _compute_unit_results_by_backtest(
    sample_rows: List[dict],
    configs: List[dict],
    output_dir: str,
) -> Dict:
    """
    对每个 VPP、每个样本日期运行单日回测并缓存。
    注意：计算量较大，仅当用户选择时启用。
    """
    cache_dir = os.path.join(output_dir, "per_unit_ranking_cache")
    os.makedirs(cache_dir, exist_ok=True)

    try:
        from backtest import run_single_day_backtest
    except Exception as e:
        raise RuntimeError(f"无法导入回测引擎: {e}")

    unit_results = {cfg["config_id"]: {"net_profit": 0.0, "rated_power_mw": cfg.get("rated_power_mw", 0.0)} for cfg in configs}
    dates = [r["date"] for r in sample_rows if not r.get("imputed")]

    for cfg in configs:
        cid = cfg["config_id"]
        agc_ratio = cfg.get("agc_ratio", 0.1)
        for d in dates:
            cache_file = os.path.join(cache_dir, f"{d}_config_{cid}.json")
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        dr = json.load(f)
                    unit_results[cid]["net_profit"] += dr.get("summary", {}).get("net_profit", 0.0)
                    continue
                except Exception:
                    pass

            try:
                result = run_single_day_backtest(
                    date=d,
                    agc_ratio=agc_ratio,
                    mode="scale",
                    use_mpc=False,
                    agc_source="historical",
                    config_id=str(cid),
                    initial_soc=cfg.get("initial_soc", 0.5),
                )
                dr = result.get("daily_result", {})
                unit_results[cid]["net_profit"] += dr.get("summary", {}).get("net_profit", 0.0)
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(dr, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[WARN] 回测排名 {d} config {cid} 失败: {e}")

    for cid in unit_results:
        unit_results[cid]["net_profit"] = round(unit_results[cid]["net_profit"], 2)

    return unit_results


# ============================================================
# 统一入口
# ============================================================

def run_analytics_by_selected_days(
    selected_dates: List[str],
    year_month: str,
    year: str,
    configs: List[dict],
    settlement: SettlementParams,
    impute: bool = False,
    group_by: str = "vpp",
    method: str = "proration",
    output_dir: str = OUTPUT_DIR,
    z_score: float = 1.96,
) -> dict:
    """
    根据用户选择的日期，统一返回：
      - 多日收益拆分
      - 资产绩效排名
      - 月度收益预测
      - 年度目标追踪
    """
    sample_rows = load_daily_results_with_imputation(
        selected_dates, output_dir, impute_by_weekday=impute
    )

    breakdown = multi_day_revenue_breakdown(
        selected_dates=selected_dates, impute=impute, output_dir=output_dir
    )
    ranking = asset_performance_ranking(
        sample_rows, configs, group_by=group_by, method=method, output_dir=output_dir
    )
    monthly = monthly_revenue_forecast_by_days(
        year_month, sample_rows, z_score
    )
    annual = annual_target_tracking_by_days(
        year, sample_rows, settlement, z_score
    )

    return {
        "selected_dates": selected_dates,
        "year_month": year_month,
        "year": year,
        "breakdown": breakdown,
        "ranking": ranking,
        "monthly": monthly,
        "annual": annual,
    }


# ============================================================
# 便捷入口
# ============================================================

def run_all_analytics(
    year_month: str,
    year: str,
    settlement: SettlementParams,
    output_dir: str = OUTPUT_DIR,
) -> dict:
    """一次性返回多日拆分、月度预测、年度目标三组分析结果（旧入口）。"""
    return {
        "multi_day_breakdown": multi_day_revenue_breakdown(
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
            output_dir=output_dir,
        ),
        "monthly_forecast": monthly_revenue_forecast(year_month, output_dir),
        "annual_target": annual_target_tracking(year, settlement, output_dir),
    }


if __name__ == "__main__":
    # 简单自测
    settlement = SettlementParams.from_json(
        os.path.join(BASE_DIR, "data", "input", "settlement_params.json")
    )
    print(json.dumps(multi_day_revenue_breakdown(), ensure_ascii=False, indent=2))
    print(json.dumps(monthly_revenue_forecast("2026-06"), ensure_ascii=False, indent=2))
    print(json.dumps(annual_target_tracking("2026", settlement), ensure_ascii=False, indent=2))
    print(json.dumps(multi_day_revenue_breakdown(weekdays=[1, 3, 5], impute=True), ensure_ascii=False, indent=2))
