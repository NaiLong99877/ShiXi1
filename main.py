#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
储能电站日收益计算 — 主入口
基于《山东电力市场规则（试行）（2026年修订版）》

用法:
    python main.py              # 使用默认数据
    python main.py scenario_1   # 使用 scenario_1_*.json 数据
"""
import sys
import os

# 确保 src 在 Python 路径中
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from calculator import run, run_all, print_summary


if __name__ == "__main__":
    prefix = sys.argv[1] if len(sys.argv) > 1 else ""

    print("=" * 60)
    print("  储能电站日收益计算器")
    print("  基于《山东电力市场规则（试行）（2026年修订版）》")
    print("=" * 60)

    if prefix and prefix != "--all":
        # 单日模式
        daily = run(prefix)
        print_summary(daily)
    else:
        # 批量模式 (默认)
        run_all()
