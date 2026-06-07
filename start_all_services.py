#!/usr/bin/env python3
"""
医疗智能助手 - 完整启动脚本
支持新功能：智能预约问诊 + AI慢病管家
"""

import os
import sys
import subprocess
import time
import signal

def run_service(port, script_path, name):
    """启动单个服务"""
    print(f"[启动] {name} (端口 {port})...")
    try:
        process = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        time.sleep(2)  # 给服务启动时间
        print(f"✓ {name} 已启动 (PID {process.pid})")
        return process
    except Exception as e:
        print(f"✗ {name} 启动失败: {e}")
        return None

def main():
    """启动所有服务"""
    print("""
    ╔═══════════════════════════════════════════════════════════╗
    ║  医疗智能助手 - 完整启动                                   ║
    ║  功能：预约问诊 + 病例结构化 + 慢病管理 + 智能提醒         ║
    ╚═══════════════════════════════════════════════════════════╝
    """)
    
    processes = []
    
    # 1. 数据库迁移
    print("\n[1/4] 运行数据库迁移...")
    try:
        from db_migrate import migrate_db
        migrate_db()
        print("✓ 数据库迁移完成")
    except Exception as e:
        print(f"✗ 数据库迁移失败: {e}")
        sys.exit(1)
    
    # 2. 启动电子病历服务（EMR Service）
    print("\n[2/4] 启动电子病历服务...")
    emr_process = run_service(
        5001,
        "medical-agent-proto/services/emr_service/main.py",
        "EMR Service"
    )
    if not emr_process:
        print("警告：EMR Service 启动失败，部分功能不可用")
    else:
        processes.append(emr_process)
    time.sleep(1)
    
    # 3. 启动慢病管家服务（Chronic Disease Service）
    print("\n[3/4] 启动慢病管家服务...")
    chronic_process = run_service(
        5002,
        "medical-agent-proto/services/chronic_disease_service/main.py",
        "Chronic Disease Service"
    )
    if not chronic_process:
        print("警告：Chronic Disease Service 启动失败，部分功能不可用")
    else:
        processes.append(chronic_process)
    time.sleep(1)
    
    # 4. 启动主应用（Flask）
    print("\n[4/4] 启动主应用...")
    try:
        print("启动 Flask 应用...")
        subprocess.run([sys.executable, "app.py"])
    except KeyboardInterrupt:
        print("\n\n[关闭] 收到关闭信号...")
    finally:
        # 清理子进程
        for proc in processes:
            if proc and proc.poll() is None:
                print(f"[清理] 关闭 PID {proc.pid}...")
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()

if __name__ == "__main__":
    main()
