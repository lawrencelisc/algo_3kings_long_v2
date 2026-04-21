#!/usr/bin/env python3
"""
簡化版模擬交易啟動腳本
直接設置環境變數並運行原型系統
"""

import os
import sys

# 強制設置為模擬模式
os.environ['SIMULATION_MODE'] = 'true'
os.environ['SIM_BALANCE'] = '1000.0'

# 如果有 .env 文件，也設置 API 密鑰
env_file = '.env'
if not os.path.exists(env_file):
    env_file = '.env.simulation'

if os.path.exists(env_file):
    # 簡單讀取 .env 文件
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

print("=" * 70)
print("🔵 加密貨幣交易系統 - 模擬模式")
print("=" * 70)
print("版本: V6.7 BugFixed")
print("模式: 模擬交易 (SIMULATION)")
print(f"初始資金: ${os.environ.get('SIM_BALANCE', '1000.0')}")
print("=" * 70)

# 檢查必要的模塊
try:
    import ccxt
    import pandas as pd
    import numpy as np
    print("✅ 核心模塊檢查通過:")
    print(f"   ccxt: {ccxt.__version__}")
    print(f"   pandas: {pd.__version__}")
    print(f"   numpy: {np.__version__}")
except ImportError as e:
    print(f"❌ 模塊缺失: {e}")
    print("請安裝所需模塊: pip install ccxt pandas numpy python-dotenv")
    sys.exit(1)

# 檢查 API 密鑰（模擬模式下可以為空）
api_key = os.environ.get('BYBIT_API_KEY')
api_secret = os.environ.get('BYBIT_SECRET')

if not api_key or api_key == 'your_bybit_api_key_here':
    print("⚠️  警告: Bybit API 密鑰未設置或使用默認值")
    print("   模擬模式下仍可運行，但需要真實行情數據時可能受限")
    print("   請在 .env 文件中設置 BYBIT_API_KEY 和 BYBIT_SECRET")
else:
    print("✅ API 密鑰已設置")

print("=" * 70)
print("🎯 模擬功能說明:")
print("   1. 使用真實市場數據（K線、Ticker、交易記錄）")
print("   2. 本地模擬帳本（不連接交易所私有 API）")
print("   3. 自動計算手續費（Bybit taker fee 0.055%）")
print("   4. 定期績效報告（每5分鐘）")
print("   5. 完整交易日誌記錄（sim_long_log.csv）")
print("=" * 70)

# 導入並運行主系統
try:
    print("\n🚀 正在啟動交易系統...\n")
    import prototype_long_v2 as trading_system
    
    # 運行主程序
    trading_system.main()
    
except KeyboardInterrupt:
    print("\n\n👋 模擬交易已手動終止")
    print("感謝使用！")
    sys.exit(0)
except Exception as e:
    print(f"\n❌ 啟動失敗: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)