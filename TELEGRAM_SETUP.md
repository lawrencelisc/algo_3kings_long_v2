# Telegram Bot 設置指南

## 1. 創建Telegram Bot

### 步驟1：聯繫 @BotFather
1. 在Telegram中搜索 `@BotFather`
2. 發送 `/start` 開始對話
3. 發送 `/newbot` 創建新機器人

### 步驟2：設置機器人信息
```
BotFather: Alright, a new bot. How are we going to call it? Please choose a name for your bot.
你: Trading Alert Bot

BotFather: Good. Now let's choose a username for your bot. It must end in `bot`.
你: YourTradingAlertBot
```

### 步驟3：獲取API Token
```
BotFather: Done! Congratulations on your new bot. You will find it at t.me/YourTradingAlertBot. You can now add a description, about section and profile picture for your bot, see /help for a list of commands. By the way, when you've finished creating your cool bot, ping our Bot Support if you want a better username for it. Just keep in mind that usernames are locked for everyone after registration.

Use this token to access the HTTP API:
1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ

Keep your token secure and store it safely, it can be used by anyone to control your bot.
```

## 2. 獲取Chat ID

### 方法A：使用 @userinfobot
1. 搜索 `@userinfobot`
2. 發送 `/start`
3. 複製顯示的 `Id: 987654321`

### 方法B：從API獲取
1. 發送一條訊息到你的Bot
2. 訪問以下URL（替換YOUR_BOT_TOKEN）：
   ```
   https://api.telegram.org/bot1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ/getUpdates
   ```
3. 在JSON響應中找到 `chat.id`

## 3. 配置.env文件

編輯 `.env` 或 `.env.simulation` 文件，添加以下配置：

```env
# Telegram Bot配置
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=987654321
TELEGRAM_NOTIFICATION_LEVEL=trades  # all, trades, alerts, none
```

## 4. 通知級別說明

### `TELEGRAM_NOTIFICATION_LEVEL` 選項：

| 級別 | 通知內容 | 頻率 |
|------|----------|------|
| **all** | 所有通知：交易、市場狀態、系統狀態 | 高頻 |
| **trades** | 只發送交易通知（開倉、平倉） | 中頻 |
| **alerts** | 只發送重要警報（SL/TP、異常） | 低頻 |
| **none** | 完全禁用通知 | 無 |

## 5. 測試設置

運行測試腳本：
```bash
python3 telegram_bot.py
```

如果設置正確，你會收到一條測試訊息。

## 6. 通知類型

### 交易通知
- 🟢 **多單入場**：價格、數量、原因
- 🔴 **多單平倉**：價格、數量、PnL、原因
- ⚠️ **重要警報**：SL/TP觸發、系統異常

### 市場狀態通知
- 📊 **市場狀態更新**：每小時或信號變化時
- 📈 **每日摘要**：餘額、PnL、交易次數、持倉

## 7. 故障排除

### 問題1：收不到通知
- 檢查 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`
- 確保Bot已啟動（發送 `/start` 到你的Bot）
- 檢查網路連接

### 問題2：權限不足
- 確保Bot有發送訊息的權限
- 檢查是否被用戶屏蔽

### 問題3：訊息格式錯誤
- 檢查特殊字符（如 `_`, `*`, `` ` ``）
- 確保Markdown格式正確

## 8. 安全注意事項

1. **保護API Token**：不要分享或提交到公開倉庫
2. **使用環境變數**：不要在代碼中硬編碼Token
3. **限制訪問**：只允許特定Chat ID接收通知
4. **定期更換Token**：如果懷疑洩露，通過 @BotFather 更換

## 9. 進階配置

### 自定義通知格式
編輯 `telegram_bot.py` 中的格式化函數

### 添加更多通知類型
在 `TelegramNotifier` 類中添加新方法

### 設置通知時間間隔
修改 `_last_market_notification_time` 的檢查邏輯