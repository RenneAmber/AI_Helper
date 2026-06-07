#!/usr/bin/env python3
"""
医疗智能助手 v2.0 - API 测试脚本
用于快速验证新功能是否正常工作
"""

import requests
import json
from typing import Dict, Any

BASE_URL = "http://localhost:5000"
EMR_URL = "http://localhost:5001"
CHRONIC_URL = "http://localhost:5002"

def test_emr_service():
    """测试EMR服务"""
    print("\n" + "="*60)
    print("测试 1: EMR 医疗病例采集完善服务")
    print("="*60)
    
    payload = {
        "patient_id": "P001",
        "chief_complaint": "胸闷、呼吸困难3天",
        "symptoms": ["胸闷", "呼吸困难", "心悸"],
        "medical_history": ["高血压5年"],
        "current_medications": ["阿司匹林 100mg"],
        "vital_signs": {
            "bp": "160/95",
            "hr": 85,
            "temp": 37.0,
            "rr": 18
        }
    }
    
    try:
        resp = requests.post(f"{EMR_URL}/intake", json=payload, timeout=5)
        print(f"✓ 请求成功 (状态码: {resp.status_code})")
        data = resp.json()
        print(f"✓ 病历ID: {data.get('emr_id')}")
        print(f"✓ 严重程度: {data.get('severity', {}).get('level')} (分数: {data.get('severity', {}).get('score')})")
        print(f"✓ 推荐科室: {data.get('recommended_dept')}")
        print(f"✓ 推荐医生级别: {data.get('recommended_doctor_level')}")
        print(f"✓ 建议检查: {', '.join(data.get('suggested_tests', [])[:3])}")
        return True
    except Exception as e:
        print(f"✗ 请求失败: {e}")
        return False

def test_chronic_disease_service():
    """测试慢病管家服务"""
    print("\n" + "="*60)
    print("测试 2: 慢病管家服务")
    print("="*60)
    
    # 测试生成提醒
    payload = {
        "patient_id": "P001",
        "disease_name": "高血压",
        "check_interval_days": 30,
        "last_checkup_date": "2026-03-06",
        "preferred_channels": ["SMS", "APP", "EMAIL"]
    }
    
    try:
        resp = requests.post(
            f"{CHRONIC_URL}/chronic/generate-reminders",
            json=payload,
            timeout=5
        )
        print(f"✓ 请求成功 (状态码: {resp.status_code})")
        data = resp.json()
        
        if isinstance(data, list):
            print(f"✓ 生成提醒数量: {len(data)}")
            for i, reminder in enumerate(data, 1):
                print(f"  [{i}] {reminder.get('reminder_type')}: {reminder.get('title')}")
                print(f"      应发送于: {reminder.get('due_date')}")
        else:
            print(f"✓ 响应: {json.dumps(data, ensure_ascii=False, indent=2)[:200]}")
        return True
    except Exception as e:
        print(f"✗ 请求失败: {e}")
        return False

def test_chat_endpoint_emr():
    """测试 /chat 端点 - 智能预约问诊"""
    print("\n" + "="*60)
    print("测试 3: /chat 端点 - 智能预约问诊")
    print("="*60)
    
    payload = {
        "message": "我最近3天胸闷呼吸困难，血压160/95，有高血压史，想挂号",
        "patient_id": "P001"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/chat", json=payload, timeout=10)
        print(f"✓ 请求成功 (状态码: {resp.status_code})")
        data = resp.json()
        
        print(f"✓ 响应类型: {data.get('type')}")
        print(f"✓ 路由到: {data.get('routed_to')}")
        
        if data.get('routed_to') == 'medical_agent':
            print(f"✓ 医疗任务精查结果:")
            if 'json' in data:
                answer = data.get('json', {}).get('answer', '')[:200]
                print(f"  回复: {answer}...")
                print(f"  置信度: {data.get('json', {}).get('confidence')}")
        
        if 'session_id' in data:
            print(f"✓ 会话ID: {data.get('session_id')}")
        
        return True
    except Exception as e:
        print(f"✗ 请求失败: {e}")
        return False

def test_chat_endpoint_chronic():
    """测试 /chat 端点 - 慢病管理"""
    print("\n" + "="*60)
    print("测试 4: /chat 端点 - 慢病档案建立")
    print("="*60)
    
    payload = {
        "message": "我有高血压5年，要建立档案并设置定期提醒",
        "patient_id": "P001"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/chat", json=payload, timeout=10)
        print(f"✓ 请求成功 (状态码: {resp.status_code})")
        data = resp.json()
        
        print(f"✓ 响应类型: {data.get('type')}")
        if 'json' in data:
            answer = data.get('json', {}).get('answer', '')[:150]
            print(f"  回复: {answer}...")
        
        return True
    except Exception as e:
        print(f"✗ 请求失败: {e}")
        return False

def test_database():
    """测试数据库"""
    print("\n" + "="*60)
    print("测试 5: 数据库表")
    print("="*60)
    
    import sqlite3
    import os
    
    db_path = os.getenv("DB_PATH", "chat_history.db")
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        tables_to_check = [
            'patient_profiles',
            'emr_records',
            'chronic_diseases_config',
            'chronic_disease_records',
            'chronic_disease_reminders',
            'reminder_logs'
        ]
        
        for table in tables_to_check:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            print(f"✓ {table}: {count} 行")
        
        # 检查chronic_diseases_config是否有初始数据
        cursor.execute("SELECT COUNT(*) FROM chronic_diseases_config WHERE disease_id IN ('HYPERTENSION', 'DIABETES')")
        disease_count = cursor.fetchone()[0]
        print(f"✓ 预定义慢病数量: {disease_count}")
        
        conn.close()
        return True
    except Exception as e:
        print(f"✗ 检查失败: {e}")
        return False

def main():
    """运行所有测试"""
    print("""
╔════════════════════════════════════════════════════════════╗
║  医疗智能助手 v2.0 - API 功能测试                           ║
║  测试前确保所有服务已启动:                                  ║
║  - EMR Service (5001)                                       ║
║  - Chronic Disease Service (5002)                           ║
║  - Main App (5000)                                          ║
╚════════════════════════════════════════════════════════════╝
    """)
    
    results = {
        "EMR服务": False,
        "慢病管家": False,
        "聊天EMR": False,
        "聊天慢病": False,
        "数据库": False,
    }
    
    # 运行测试
    results["数据库"] = test_database()
    
    print("\n【注意】以下测试需要外部服务在线...")
    results["EMR服务"] = test_emr_service()
    results["慢病管家"] = test_chronic_disease_service()
    results["聊天EMR"] = test_chat_endpoint_emr()
    results["聊天慢病"] = test_chat_endpoint_chronic()
    
    # 汇总
    print("\n" + "="*60)
    print("测试汇总")
    print("="*60)
    for name, passed in results.items():
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{name:20} {status}")
    
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n总体: {passed}/{total} 项通过")
    
    if passed == total:
        print("\n🎉 所有测试通过！新功能已就绪。")
    else:
        print(f"\n⚠️  有 {total - passed} 项测试未通过，请检查服务状态。")

if __name__ == "__main__":
    main()
