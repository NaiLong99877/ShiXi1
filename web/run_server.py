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
import http.server
import socketserver
import webbrowser
import threading

# 工作目录: 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 切换到项目根目录
os.chdir(BASE_DIR)

# 确保 src 在路径中
sys.path.insert(0, os.path.join(BASE_DIR, "src"))

PORT = 8080


class MyHandler(http.server.SimpleHTTPRequestHandler):
    """自定义处理器: 支持 CORS 和正确的 MIME 类型"""

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        # 简化日志输出 (跳过错误日志, 只记录正常请求)
        if args and isinstance(args[0], str):
            if "/data/" in args[0] or ".html" in args[0]:
                print(f"  [REQUEST] {args[0]}")


def open_browser():
    """延迟打开浏览器"""
    import time
    time.sleep(1)
    url = f"http://localhost:{PORT}/web/dashboard.html"
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

    # 自动打开浏览器
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        with socketserver.TCPServer(("", PORT), MyHandler) as httpd:
            print(f"    [OK] 服务器已启动!\n")
            httpd.serve_forever()
    except OSError as e:
        if "Address already in use" in str(e):
            print(f"    [WARN] 端口 {PORT} 已被占用, 尝试端口 {PORT+1}...")
            with socketserver.TCPServer(("", PORT + 1), MyHandler) as httpd:
                alt_url = f"http://localhost:{PORT+1}/web/dashboard.html"
                print(f"    [OK] 服务器已启动! 请访问: {alt_url}")
                webbrowser.open(alt_url)
                httpd.serve_forever()
        else:
            raise
    except KeyboardInterrupt:
        print("\n  服务器已停止。")


if __name__ == "__main__":
    main()
