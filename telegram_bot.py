#!/usr/bin/env python3
"""
Telegram Bot 通知系統
用於發送交易通知和市場狀態更新
"""

import os
import requests
import json
import time
from datetime import datetime
import logging

# 配置
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# 設置日誌
logger = logging.getLogger('TelegramBot')

class TelegramNotifier:
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if self.enabled:
            logger.info("✅ Telegram Bot 已啟用")
        else:
            logger.warning("⚠️ Telegram Bot 未啟用，請設置 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
    
    def send_message(self, text, parse_mode='Markdown', disable_notification=False):
        """發送訊息到Telegram"""
        if not self.enabled:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_notification': disable_notification
            }
            
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            
            if result.get('ok'):
                logger.debug(f"✅ Telegram訊息已發送: {text[:50]}...")
                return True
            else:
                logger.error(f"❌ Telegram發送失敗: {result}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Telegram發送異常: {e}")
            return False
    
    def send_trade_alert(self, symbol, action, price, amount, reason="", pnl=0.0):
        """發送交易警報"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        if action == 'LONG_ENTRY':
            emoji = "🟢"
            title = "多單入場"
        elif action == 'LONG_EXIT':
            emoji = "🔴" if pnl < 0 else "🟢"
            title = "多單平倉"
            pnl_text = f"PnL: ${pnl:+.4f}"
        else:
            emoji = "ℹ️"
            title = "交易通知"
        
        # 構建訊息
        lines = [
            f"{emoji} *{title}*",
            f"▪️ 幣種: `{symbol}`",
            f"▪️ 時間: {timestamp}",
            f"▪️ 價格: ${price:.4f}",
            f"▪️ 數量: {amount:.4f}"
        ]
        
        if pnl != 0:
            lines.append(f"▪️ {pnl_text}")
        
        if reason:
            lines.append(f"▪️ 原因: {reason}")
        
        message = "\n".join(lines)
        return self.send_message(message)
    
    def send_market_status(self, regime_data):
        """發送市場狀態更新"""
        if not regime_data:
            return False
        
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        lines = [
            "📊 *市場狀態更新*",
            f"▪️ 時間: {timestamp}",
            f"▪️ 信號: {regime_data.get('signal_names', '未知')}",
            f"▪️ ADX: {regime_data.get('mean_adx', 0):.1f}",
            f"▪️ 分數: {regime_data.get('market_score', 0):.3f}",
            f"▪️ 高波動: {'是' if regime_data.get('is_highvol') else '否'}",
            f"▪️ 熊市: {'開啟' if regime_data.get('is_bear') else '關閉'}"
        ]
        
        if 'btc_price' in regime_data:
            lines.insert(2, f"▪️ BTC: ${regime_data['btc_price']:.0f}")
        
        message = "\n".join(lines)
        return self.send_message(message, disable_notification=True)
    
    def send_daily_summary(self, positions, balance, total_pnl, trade_count):
        """發送每日摘要"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        
        lines = [
            "📈 *每日交易摘要*",
            f"▪️ 時間: {timestamp}",
            f"▪️ 餘額: ${balance:.2f}",
            f"▪️ 總PnL: ${total_pnl:+.4f}",
            f"▪️ 交易次數: {trade_count}",
            f"▪️ 當前持倉: {len(positions)}個"
        ]
        
        if positions:
            lines.append("▪️ 持倉列表:")
            for symbol in list(positions.keys())[:5]:  # 最多顯示5個
                lines.append(f"   - `{symbol}`")
            if len(positions) > 5:
                lines.append(f"   ... 等{len(positions)}個")
        
        message = "\n".join(lines)
        return self.send_message(message)

# 全局實例
telegram_notifier = TelegramNotifier()

# 測試函數
if __name__ == "__main__":
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    
    # 從.env讀取配置
    from dotenv import load_dotenv
    load_dotenv()
    
    notifier = TelegramNotifier()
    
    if notifier.enabled:
        print("測試Telegram Bot連接...")
        
        # 測試訊息
        success = notifier.send_message(
            "🤖 *交易系統啟動測試*\\n"
            "時間: " + datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC") + "\\n"
            "狀態: ✅ 連接正常"
        )
        
        if success:
            print("✅ Telegram Bot測試成功！")
        else:
            print("❌ Telegram Bot測試失敗")
    else:
        print("❌ Telegram Bot未配置")
        print("請在.env文件中添加：")
        print("TELEGRAM_BOT_TOKEN=你的機器人token")
        print("TELEGRAM_CHAT_ID=你的聊天ID")