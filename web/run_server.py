#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键启动本地HTTP服务器

用法:
    python web/run_server.py

然后在浏览器中访问: http://localhost:8080/web/dashboard.html

服务器会自动在浏览器中打开看板页面。
"""
import os
import sys
import json
import time
import http.server
import socketserver
import webbrowser
import threading
from urllib.parse import urlparse, parse_qs

# Windows 控制台默认 GBK，强制 UTF-8 输出避免中文乱码
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

# 工作目录: 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGC_CURVE_DIR = os.path.join(BASE_DIR, "data", "agc_curves")
CURVE_DIR = os.path.join(BASE_DIR, "data", "curves")
BATCH_BACKTEST_DIR = os.path.join(BASE_DIR, "data", "output", "batch_backtest")
BACKTEST_DIR = os.path.join(BASE_DIR, "data", "output", "backtest")

# 切换到项目根目录
os.chdir(BASE_DIR)

# 确保 src 在路径中
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

from revenue_analytics import (
    multi_day_revenue_breakdown,
    monthly_revenue_forecast,
    monthly_revenue_forecast_by_days,
    annual_target_tracking,
    annual_target_tracking_by_days,
    asset_performance_ranking,
    run_analytics_by_selected_days,
    load_daily_results_with_imputation,
    _generate_dates,
)
from excel_export import (
    export_daily_excel,
    export_monthly_excel,
    export_annual_excel,
)
from models import SettlementParams

PORT = int(os.environ.get("PORT", "8080"))


class MyHandler(http.server.SimpleHTTPRequestHandler):
    """自定义处理器: 支持 CORS、API 和正确的 MIME 类型"""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _send_json(self, data: dict, status: int = 200):
        """发送 JSON 响应。"""
        response = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def _is_safe_path(self, target_path: str) -> bool:
        """防止路径穿越：确保目标路径在项目根目录内。"""
        abs_target = os.path.abspath(target_path)
        return abs_target.startswith(os.path.abspath(BASE_DIR))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/analytics/multi_day_breakdown":
            self._handle_analytics_multi_day(parsed.query)
        elif parsed.path == "/api/analytics/asset_ranking":
            self._handle_analytics_asset_ranking(parsed.query)
        elif parsed.path == "/api/analytics/monthly_forecast":
            self._handle_analytics_monthly(parsed.query)
        elif parsed.path == "/api/analytics/annual_target":
            self._handle_analytics_annual(parsed.query)
        elif parsed.path == "/api/agc/list":
            self._handle_agc_list()
        elif parsed.path == "/api/agc/load":
            self._handle_agc_load(parsed.query)
        elif parsed.path == "/api/curve/list":
            self._handle_curve_list()
        elif parsed.path == "/api/curve/load":
            self._handle_curve_load(parsed.query)
        elif parsed.path == "/api/vpp/configs":
            self._handle_vpp_configs()
        elif parsed.path == "/api/batch-backtest/list":
            self._handle_batch_backtest_list()
        elif parsed.path == "/api/batch-backtest/load":
            self._handle_batch_backtest_load(parsed.query)
        else:
            super().do_GET()

    def _handle_vpp_configs(self):
        """GET /api/vpp/configs: 返回 VPP 配置列表及整体聚合"""
        try:
            from vpp_config import load_vpp_configs, build_aggregate_config
            configs = load_vpp_configs()
            aggregate = build_aggregate_config(configs)
            self._send_json({
                "configs": configs,
                "overall": aggregate,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_batch_backtest_list(self):
        """GET /api/batch-backtest/list: 列出批量回测保存和旧版策略回测报告"""
        items = []
        try:
            os.makedirs(BATCH_BACKTEST_DIR, exist_ok=True)
            for fname in sorted(os.listdir(BATCH_BACKTEST_DIR)):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(BATCH_BACKTEST_DIR, fname)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    meta = data.get("meta", {})
                    name = meta.get("name") or fname
                    created_at = meta.get("created_at", "")
                    items.append({
                        "type": "batch",
                        "file": fname,
                        "label": f"{name} ({created_at})",
                        "created_at": created_at,
                    })
                except Exception:
                    pass
        except FileNotFoundError:
            pass

        try:
            for fname in sorted(os.listdir(BACKTEST_DIR)):
                if fname.endswith(".json") and fname.startswith("backtest_"):
                    items.append({
                        "type": "legacy",
                        "file": fname,
                        "label": fname.replace("backtest_", "").replace(".json", ""),
                        "created_at": "",
                    })
        except FileNotFoundError:
            pass

        self._send_json({"items": items})

    def _handle_batch_backtest_load(self, query: str):
        """GET /api/batch-backtest/load?type=batch|legacy&file=..."""
        qs = parse_qs(query)
        item_type = qs.get("type", ["batch"])[0]
        fname = qs.get("file", [None])[0]
        if not fname:
            self._send_json({"error": "file 参数必填"}, status=400)
            return

        base_dir = BATCH_BACKTEST_DIR if item_type == "batch" else BACKTEST_DIR
        path = os.path.join(base_dir, os.path.basename(fname))
        if not self._is_safe_path(path) or not os.path.exists(path):
            self._send_json({"error": f"文件 '{fname}' 不存在"}, status=404)
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_batch_backtest_save(self):
        """POST /api/batch-backtest/save: 保存批量回测设定和结果"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            meta = params.get("meta") or {}
            settings = params.get("settings") or {}
            result = params.get("result") or {}

            name = (meta.get("name") or "未命名").strip()
            if not name:
                raise ValueError("名称不能为空")

            safe_name = "".join(c for c in name if c.isalnum() or c in ("_", "-")).strip()
            if not safe_name:
                safe_name = "untitled"
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            fname = f"batch_{safe_name}_{timestamp}.json"

            os.makedirs(BATCH_BACKTEST_DIR, exist_ok=True)
            path = os.path.join(BATCH_BACKTEST_DIR, fname)
            if not self._is_safe_path(path):
                raise ValueError("无效的文件路径")

            payload = {
                "meta": {
                    "name": name,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "settings": settings,
                "result": result,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            self._send_json({"success": True, "file": fname})
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_analytics_multi_day(self, query: str):
        """GET /api/analytics/multi_day_breakdown"""
        qs = parse_qs(query)
        start_date = qs.get("start_date", [None])[0]
        end_date = qs.get("end_date", [None])[0]
        weekdays = self._parse_weekdays(qs.get("weekdays", [""])[0])
        impute = qs.get("impute", ["0"])[0] in ("1", "true", "True")
        try:
            result = multi_day_revenue_breakdown(
                start_date, end_date, weekdays=weekdays, impute=impute
            )
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_analytics_asset_ranking(self, query: str):
        """GET /api/analytics/asset_ranking"""
        qs = parse_qs(query)
        start_date = qs.get("start_date", [None])[0]
        end_date = qs.get("end_date", [None])[0]
        weekdays = self._parse_weekdays(qs.get("weekdays", [""])[0])
        impute = qs.get("impute", ["0"])[0] in ("1", "true", "True")
        group_by = qs.get("group_by", ["vpp"])[0]
        method = qs.get("method", ["proration"])[0]
        try:
            from vpp_config import load_vpp_configs
            configs = load_vpp_configs()
            selected_dates = _generate_dates(start_date, end_date, weekdays)
            sample_rows = load_daily_results_with_imputation(
                selected_dates, impute_by_weekday=impute
            )
            result = asset_performance_ranking(
                sample_rows, configs, group_by=group_by, method=method
            )
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_analytics_monthly(self, query: str):
        """GET /api/analytics/monthly_forecast"""
        qs = parse_qs(query)
        year_month = qs.get("year_month", [None])[0]
        if not year_month:
            self._send_json({"error": "year_month 参数必填，格式 YYYY-MM"}, status=400)
            return
        selected_dates = qs.get("selected_dates", [None])[0]
        weekdays = self._parse_weekdays(qs.get("weekdays", [""])[0])
        impute = qs.get("impute", ["0"])[0] in ("1", "true", "True")
        try:
            if selected_dates:
                selected_dates = selected_dates.split(",")
                sample_rows = load_daily_results_with_imputation(
                    selected_dates, impute_by_weekday=impute
                )
                result = monthly_revenue_forecast_by_days(year_month, sample_rows)
            else:
                result = monthly_revenue_forecast(year_month)
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_analytics_annual(self, query: str):
        """GET /api/analytics/annual_target"""
        qs = parse_qs(query)
        year = qs.get("year", [None])[0]
        if not year:
            self._send_json({"error": "year 参数必填，格式 YYYY"}, status=400)
            return
        selected_dates = qs.get("selected_dates", [None])[0]
        weekdays = self._parse_weekdays(qs.get("weekdays", [""])[0])
        impute = qs.get("impute", ["0"])[0] in ("1", "true", "True")
        try:
            settlement = SettlementParams.from_json(
                os.path.join(BASE_DIR, "data", "input", "settlement_params.json")
            )
            if selected_dates:
                selected_dates = selected_dates.split(",")
                sample_rows = load_daily_results_with_imputation(
                    selected_dates, impute_by_weekday=impute
                )
                result = annual_target_tracking_by_days(year, sample_rows, settlement)
            else:
                result = annual_target_tracking(year, settlement)
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _parse_weekdays(self, s: str):
        """解析逗号分隔的星期数字，空字符串返回 None。"""
        if not s:
            return None
        try:
            return [int(x.strip()) for x in s.split(",") if x.strip() != ""]
        except Exception:
            return None

    def _handle_analytics_by_selected_days(self):
        """POST /api/analytics/by_selected_days"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            selected_dates = params.get("selected_dates", [])
            year_month = params.get("year_month")
            year = params.get("year")
            impute = bool(params.get("impute", False))
            group_by = params.get("group_by", "vpp")
            method = params.get("method", "proration")

            if not selected_dates or not year_month or not year:
                self._send_json({"error": "selected_dates, year_month, year 均必填"}, status=400)
                return

            from vpp_config import load_vpp_configs
            configs = load_vpp_configs()
            settlement = SettlementParams.from_json(
                os.path.join(BASE_DIR, "data", "input", "settlement_params.json")
            )

            result = run_analytics_by_selected_days(
                selected_dates=selected_dates,
                year_month=year_month,
                year=year,
                configs=configs,
                settlement=settlement,
                impute=impute,
                group_by=group_by,
                method=method,
            )
            self._send_json(result)
        except Exception as e:
            import traceback
            self._send_json({"error": str(e), "traceback": traceback.format_exc()}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/backtest/single_day":
            self._handle_single_day_backtest()
        elif parsed.path == "/api/backtest/batch":
            self._handle_batch_backtest()
        elif parsed.path == "/api/day_ahead/optimize":
            self._handle_day_ahead_optimize()
        elif parsed.path == "/api/batch-backtest/save":
            self._handle_batch_backtest_save()
        elif parsed.path == "/api/agc/save":
            self._handle_agc_save()
        elif parsed.path == "/api/agc/delete":
            self._handle_agc_delete()
        elif parsed.path == "/api/curve/save":
            self._handle_curve_save()
        elif parsed.path == "/api/curve/delete":
            self._handle_curve_delete()
        elif parsed.path == "/api/export/excel":
            self._handle_export_excel()
        elif parsed.path == "/api/analytics/by_selected_days":
            self._handle_analytics_by_selected_days()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _handle_day_ahead_optimize(self):
        """POST /api/day_ahead/optimize: 按指定调频比例运行日前优化"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            date = params.get("date")
            agc_ratio = float(params.get("agc_ratio", 0.3))
            initial_soc = params.get("initial_soc")
            lmp_overrides = params.get("lmp_overrides")
            if lmp_overrides is not None and "da_values" in lmp_overrides:
                lmp_overrides["da_values"] = [float(x) for x in lmp_overrides["da_values"]]

            if not date:
                raise ValueError("date 参数不能为空")

            from backtest import _load_day_ahead, _load_storage, _build_scenario_storage, _simulate_day_ahead_soc
            from optimizer import optimize_day_ahead

            base_storage = _load_storage()
            scenario_storage = _build_scenario_storage(base_storage, agc_ratio)
            if initial_soc is None:
                initial_soc = base_storage.initial_soc
            else:
                initial_soc = float(initial_soc)

            base_da = _load_day_ahead(date)
            lmp_pred = [it.lmp_yuan_per_mwh for it in base_da.intervals]
            custom_da_lmp = lmp_overrides.get("da_values") if lmp_overrides else None
            if custom_da_lmp is not None:
                lmp_pred = [float(x) for x in custom_da_lmp]

            charge_plan, discharge_plan, agc_capacity, final_soc = optimize_day_ahead(
                storage=scenario_storage,
                lmp_pred=lmp_pred,
                agc_prices=[it.agc_capacity_price for it in base_da.intervals],
                agc_mileage_price=base_da.agc_mileage_price,
                initial_soc=initial_soc,
                final_soc_target=base_storage.initial_soc,
                agc_reserve_mw=scenario_storage.agc_reserve_mw,
                agc_max_capacity_mw=scenario_storage.agc_max_capacity_mw,
            )
            da_soc = _simulate_day_ahead_soc(scenario_storage, charge_plan, discharge_plan, initial_soc)

            self._send_json({
                "date": date,
                "agc_ratio": agc_ratio,
                "charge_plan": charge_plan,
                "discharge_plan": discharge_plan,
                "power_plan": [round(d - c, 4) for c, d in zip(charge_plan, discharge_plan)],
                "agc_capacity": agc_capacity,
                "soc": da_soc,
                "final_soc": final_soc,
            })
        except Exception as e:
            import traceback
            self._send_json({"error": str(e), "traceback": traceback.format_exc()}, status=500)

    def _handle_single_day_backtest(self):
        """处理单日回测请求"""
        import time
        start = time.time()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            date = params.get("date")
            agc_ratio = float(params.get("agc_ratio", 0.3))
            mode = params.get("mode", "scale")
            use_mpc = params.get("use_mpc", True)
            if isinstance(use_mpc, str):
                use_mpc = use_mpc.lower() in ("true", "1", "yes", "mpc")
            else:
                use_mpc = bool(use_mpc)

            # AGC 指令来源
            agc_source = params.get("agc_source", "historical")
            if agc_source not in ("historical", "random", "manual"):
                raise ValueError("agc_source 必须是 historical/random/manual 之一")
            agc_hourly = bool(params.get("agc_hourly", False))
            agc_seed = int(params.get("agc_seed", 42))
            agc_commands = params.get("agc_commands")
            if agc_commands is not None:
                agc_commands = [float(x) for x in agc_commands]

            # 自定义 LMP 与日前中标曲线
            lmp_overrides = params.get("lmp_overrides")
            bid_overrides = params.get("bid_overrides")
            if lmp_overrides is not None:
                if "da_values" in lmp_overrides:
                    lmp_overrides["da_values"] = [float(x) for x in lmp_overrides["da_values"]]
                if "rt_values" in lmp_overrides:
                    lmp_overrides["rt_values"] = [float(x) for x in lmp_overrides["rt_values"]]
            if bid_overrides is not None:
                if "power_plan" in bid_overrides:
                    bid_overrides["power_plan"] = [float(x) for x in bid_overrides["power_plan"]]
                # 保留对旧字段的兼容处理（若前端仍传入）
                for key in ("charge_plan", "discharge_plan", "agc_capacity", "agc_capacity_price", "soc"):
                    if key in bid_overrides:
                        bid_overrides[key] = [float(x) for x in bid_overrides[key]]

            # VPP 配置选择
            config_id = params.get("config_id", "overall")

            if not date:
                raise ValueError("date 参数不能为空")

            from backtest import run_single_day_backtest
            from vpp_config import load_vpp_configs, get_config_by_id, build_storage_params
            from models import StorageParams

            storage_override = None
            if config_id != "overall":
                try:
                    config_id = int(config_id)
                except ValueError:
                    raise ValueError("config_id 必须是整数或 'overall'")
                configs = load_vpp_configs()
                config = get_config_by_id(config_id, configs)
                if config is None:
                    raise ValueError(f"找不到 config_id={config_id} 的 VPP 配置")
                base_storage = StorageParams.from_json(
                    os.path.join(BASE_DIR, "data", "input", "storage_params.json")
                )
                storage_override = build_storage_params(config, base_storage)

            result = run_single_day_backtest(
                date, agc_ratio, mode=mode, use_mpc=use_mpc,
                agc_source=agc_source,
                agc_commands=agc_commands,
                agc_hourly=agc_hourly,
                agc_seed=agc_seed,
                storage=storage_override,
                config_id=str(config_id),
                lmp_overrides=lmp_overrides,
                bid_overrides=bid_overrides,
            )

            elapsed = time.time() - start
            print(f"[API] /api/backtest/single_day 完成: date={date}, ratio={agc_ratio}, mode={mode}, rt_mode={'mpc' if use_mpc else 'hindsight'}, agc_source={agc_source}, config_id={config_id}, 耗时={elapsed:.2f}s")

            response = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(response)
        except Exception as e:
            import traceback
            error = {"error": str(e), "traceback": traceback.format_exc()}
            response = json.dumps(error, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(response)

    def _handle_batch_backtest(self):
        """处理批量回测请求"""
        import time
        start = time.time()
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            dates = params.get("dates")
            if not dates or not isinstance(dates, list):
                raise ValueError("dates 参数必须是非空日期列表")

            agc_ratios = params.get("agc_ratios", [0.3])
            if isinstance(agc_ratios, str):
                agc_ratios = [float(x.strip()) for x in agc_ratios.split(",") if x.strip()]
            else:
                agc_ratios = [float(x) for x in agc_ratios]

            mode = params.get("mode", "scale")
            use_mpc = params.get("use_mpc", True)
            if isinstance(use_mpc, str):
                use_mpc = use_mpc.lower() in ("true", "1", "yes", "mpc")
            else:
                use_mpc = bool(use_mpc)

            initial_soc = params.get("initial_soc")
            if initial_soc is not None:
                initial_soc = float(initial_soc)

            config_id = params.get("config_id", "overall")

            daily_agc_configs = params.get("daily_agc_configs") or {}
            # 规范化 daily_agc_configs 中的字段类型
            for d, cfg in daily_agc_configs.items():
                cmds = cfg.get("commands")
                if cmds is not None:
                    cfg["commands"] = [float(x) for x in cmds]
                if cfg.get("initial_soc") is not None:
                    cfg["initial_soc"] = float(cfg["initial_soc"])

            daily_lmp_overrides = params.get("daily_lmp_overrides") or {}
            daily_bid_overrides = params.get("daily_bid_overrides") or {}
            # 规范化 LMP 与中标曲线覆盖中的数值类型
            for d, cfg in daily_lmp_overrides.items():
                if "da_values" in cfg:
                    cfg["da_values"] = [float(x) for x in cfg["da_values"]]
                if "rt_values" in cfg:
                    cfg["rt_values"] = [float(x) for x in cfg["rt_values"]]
            for d, cfg in daily_bid_overrides.items():
                for ratio, bid in cfg.items():
                    if "power_plan" in bid:
                        bid["power_plan"] = [float(x) for x in bid["power_plan"]]
                    for key in ("charge_plan", "discharge_plan", "agc_capacity", "agc_capacity_price", "soc"):
                        if key in bid:
                            bid[key] = [float(x) for x in bid[key]]

            from backtest import run_backtest
            from vpp_config import load_vpp_configs, get_config_by_id, build_storage_params
            from models import StorageParams

            storage_override = None
            if config_id != "overall":
                try:
                    config_id = int(config_id)
                except ValueError:
                    raise ValueError("config_id 必须是整数或 'overall'")
                configs = load_vpp_configs()
                config = get_config_by_id(config_id, configs)
                if config is None:
                    raise ValueError(f"找不到 config_id={config_id} 的 VPP 配置")
                base_storage = StorageParams.from_json(
                    os.path.join(BASE_DIR, "data", "input", "storage_params.json")
                )
                storage_override = build_storage_params(config, base_storage)

            report = run_backtest(
                dates=dates,
                agc_ratios=agc_ratios,
                mode=mode,
                use_mpc=use_mpc,
                initial_soc=initial_soc,
                storage=storage_override,
                daily_agc_configs=daily_agc_configs,
                daily_lmp_overrides=daily_lmp_overrides,
                daily_bid_overrides=daily_bid_overrides,
            )

            elapsed = time.time() - start
            print(f"[API] /api/backtest/batch 完成: dates={dates[0]}~{dates[-1]}, ratios={agc_ratios}, mode={mode}, rt_mode={'mpc' if use_mpc else 'hindsight'}, config_id={config_id}, 耗时={elapsed:.2f}s")

            self._send_json(report.to_dict())
        except Exception as e:
            import traceback
            error = {"error": str(e), "traceback": traceback.format_exc()}
            response = json.dumps(error, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(response)

    def _handle_agc_list(self):
        """GET /api/agc/list: 列出已保存的 AGC 曲线"""
        os.makedirs(AGC_CURVE_DIR, exist_ok=True)
        files = sorted(
            f for f in os.listdir(AGC_CURVE_DIR)
            if f.endswith(".json")
        )
        curves = []
        for fname in files:
            path = os.path.join(AGC_CURVE_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                curves.append({
                    "name": data.get("name", fname[:-5]),
                    "created_at": data.get("created_at", ""),
                    "hourly": data.get("hourly", False),
                    "points": len(data.get("commands", [])),
                })
            except Exception:
                pass
        self._send_json({"curves": curves})

    def _handle_agc_load(self, query: str):
        """GET /api/agc/load?name=...: 加载指定 AGC 曲线"""
        qs = parse_qs(query)
        name = qs.get("name", [None])[0]
        if not name:
            self._send_json({"error": "name 参数不能为空"}, status=400)
            return
        safe_name = os.path.basename(name) + ".json"
        path = os.path.join(AGC_CURVE_DIR, safe_name)
        if not self._is_safe_path(path) or not os.path.exists(path):
            self._send_json({"error": f"曲线 '{name}' 不存在"}, status=404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_agc_save(self):
        """POST /api/agc/save: 保存 AGC 曲线"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            name = params.get("name")
            commands = params.get("commands")
            hourly = bool(params.get("hourly", False))
            if not name:
                self._send_json({"error": "name 不能为空"}, status=400)
                return
            if not isinstance(commands, list) or len(commands) == 0:
                self._send_json({"error": "commands 必须是 nonempty list"}, status=400)
                return

            safe_name = os.path.basename(name) + ".json"
            path = os.path.join(AGC_CURVE_DIR, safe_name)
            if not self._is_safe_path(path):
                self._send_json({"error": "非法路径"}, status=400)
                return

            os.makedirs(AGC_CURVE_DIR, exist_ok=True)
            data = {
                "name": name,
                "commands": [float(x) for x in commands],
                "hourly": hourly,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._send_json({"success": True, "name": name})
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_agc_delete(self):
        """POST /api/agc/delete: 删除 AGC 曲线"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))
            name = params.get("name")
            if not name:
                self._send_json({"error": "name 不能为空"}, status=400)
                return
            safe_name = os.path.basename(name) + ".json"
            path = os.path.join(AGC_CURVE_DIR, safe_name)
            if self._is_safe_path(path) and os.path.exists(path):
                os.remove(path)
            self._send_json({"success": True})
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_curve_list(self):
        """GET /api/curve/list: 列出已保存的通用曲线预设"""
        os.makedirs(CURVE_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(CURVE_DIR) if f.endswith(".json"))
        curves = []
        for fname in files:
            path = os.path.join(CURVE_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                curves.append({
                    "name": data.get("name", fname[:-5]),
                    "type": data.get("type", "unknown"),
                    "created_at": data.get("created_at", ""),
                })
            except Exception:
                pass
        self._send_json({"curves": curves})

    def _handle_curve_load(self, query: str):
        """GET /api/curve/load?name=...: 加载指定曲线预设"""
        qs = parse_qs(query)
        name = qs.get("name", [None])[0]
        if not name:
            self._send_json({"error": "name 参数不能为空"}, status=400)
            return
        safe_name = os.path.basename(name) + ".json"
        path = os.path.join(CURVE_DIR, safe_name)
        if not self._is_safe_path(path) or not os.path.exists(path):
            self._send_json({"error": f"曲线 '{name}' 不存在"}, status=404)
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._send_json(data)
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_curve_save(self):
        """POST /api/curve/save: 保存通用曲线预设"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            name = params.get("name")
            curve_type = params.get("type")
            data_payload = params.get("data")
            if not name:
                self._send_json({"error": "name 不能为空"}, status=400)
                return
            if curve_type not in ("da_lmp", "rt_lmp", "da_bid", "agc"):
                self._send_json({"error": "type 必须是 da_lmp/rt_lmp/da_bid/agc 之一"}, status=400)
                return
            if not isinstance(data_payload, dict):
                self._send_json({"error": "data 必须是对象"}, status=400)
                return

            safe_name = os.path.basename(name) + ".json"
            path = os.path.join(CURVE_DIR, safe_name)
            if not self._is_safe_path(path):
                self._send_json({"error": "非法路径"}, status=400)
                return

            os.makedirs(CURVE_DIR, exist_ok=True)
            data = {
                "name": name,
                "type": curve_type,
                "data": data_payload,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._send_json({"success": True, "name": name})
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_curve_delete(self):
        """POST /api/curve/delete: 删除通用曲线预设"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))
            name = params.get("name")
            if not name:
                self._send_json({"error": "name 不能为空"}, status=400)
                return
            safe_name = os.path.basename(name) + ".json"
            path = os.path.join(CURVE_DIR, safe_name)
            if self._is_safe_path(path) and os.path.exists(path):
                os.remove(path)
            self._send_json({"success": True})
        except Exception as e:
            self._send_json({"error": str(e)}, status=500)

    def _handle_export_excel(self):
        """POST /api/export/excel"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            params = json.loads(body.decode("utf-8"))

            report_type = params.get("report_type")
            output_path = params.get("output_path")

            if report_type not in ("daily", "monthly", "annual"):
                self._send_json({"error": "report_type 必须是 daily/monthly/annual"}, status=400)
                return
            if not output_path:
                self._send_json({"error": "output_path 不能为空"}, status=400)
                return

            output_path = os.path.abspath(os.path.join(BASE_DIR, output_path))
            if not self._is_safe_path(output_path):
                self._send_json({"error": "output_path 必须在项目目录内"}, status=400)
                return

            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            if report_type == "daily":
                date = params.get("date")
                if not date:
                    self._send_json({"error": "daily 报表需要 date 参数"}, status=400)
                    return
                export_daily_excel(date, output_path)
            elif report_type == "monthly":
                year_month = params.get("year_month")
                if not year_month:
                    self._send_json({"error": "monthly 报表需要 year_month 参数"}, status=400)
                    return
                sample_rows = params.get("sample_rows")
                ranking = params.get("ranking")
                export_monthly_excel(year_month, output_path, sample_rows=sample_rows, ranking=ranking)
            else:
                year = params.get("year")
                if not year:
                    self._send_json({"error": "annual 报表需要 year 参数"}, status=400)
                    return
                settlement = SettlementParams.from_json(
                    os.path.join(BASE_DIR, "data", "input", "settlement_params.json")
                )
                sample_rows = params.get("sample_rows")
                ranking = params.get("ranking")
                export_annual_excel(year, output_path, settlement, sample_rows=sample_rows, ranking=ranking)

            # 转换为相对 URL 供前端下载
            rel_url = "/" + os.path.relpath(output_path, BASE_DIR).replace("\\", "/")
            self._send_json({
                "success": True,
                "output_path": output_path,
                "download_url": rel_url,
            })
        except Exception as e:
            import traceback
            self._send_json({"error": str(e), "traceback": traceback.format_exc()}, status=500)

    def log_message(self, format, *args):
        # 简化日志输出 (跳过错误日志, 只记录正常请求)
        if args and isinstance(args[0], str):
            if "/data/" in args[0] or ".html" in args[0] or "/api/" in args[0]:
                print(f"  [REQUEST] {args[0]}")


def open_browser(url: str):
    """延迟打开浏览器"""
    import time
    time.sleep(1)
    print(f"\n  正在打开浏览器: {url}")
    webbrowser.open(url)


def main():
    print("=" * 60)
    print("  储能电站日收益看板 — 本地服务器")
    print(f"  端口: {PORT}")
    print(f"  访问地址: http://localhost:{PORT}/web/dashboard.html")
    print("  按 Ctrl+C 停止服务器")
    print("=" * 60)

    # 先执行计算, 确保有最新数据
    print("\n[1] 计算日结算结果...")
    from calculator import run_all
    try:
        results = run_all()
        if results:
            avg = sum(r.net_profit for r in results) / len(results)
            print(f"    {len(results)} 天计算完成, 日均净利润: {avg:,.2f} 元")
    except Exception as e:
        print(f"    [WARN] 计算失败: {e}")
        print("    将使用已有的 data/output/*_daily_result.json")

    # 启动服务器
    print(f"\n[2] 启动 HTTP 服务器 (端口 {PORT})...")

    url = f"http://localhost:{PORT}/web/dashboard.html"

    # 自动打开浏览器
    threading.Thread(target=open_browser, args=(url,), daemon=True).start()

    try:
        with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
            print(f"    [OK] 服务器已启动!\n")
            httpd.serve_forever()
    except OSError as e:
        # 中文/英文 Windows 错误信息不同，用错误码兜底
        if "Address already in use" in str(e) or getattr(e, 'winerror', None) == 10048:
            print(f"    [WARN] 端口 {PORT} 已被占用, 尝试端口 {PORT+1}...")
            with socketserver.TCPServer(("", PORT + 1), MyHandler) as httpd:
                alt_url = f"http://localhost:{PORT+1}/web/dashboard.html"
                # 端口变了，重新打开浏览器用正确地址
                threading.Thread(target=open_browser, args=(alt_url,), daemon=True).start()
                print(f"    [OK] 服务器已启动! 请访问: {alt_url}")
                httpd.serve_forever()
        else:
            raise
    except KeyboardInterrupt:
        print("\n  服务器已停止。")


if __name__ == "__main__":
    main()
