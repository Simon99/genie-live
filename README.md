# genie-live

即時會議監控:雙窗口語音轉寫(10s fast + 30s refined 修正)、LLM 即時分析(主題/重點/爭議)、問題清單驅動的答案搜集、時間軸整場整理。Flask + SocketIO 即時推送到瀏覽器 UI。

## 需求

- **macOS**(錄音走 avfoundation)+ `ffmpeg`
- genie-core(`[mlx]`)+ flask + flask-socketio
- LM Studio 文字模型(建議 qwen3.6-35b-a3b-turboquant,非 thinking)

## 啟動

```bash
# 必須在「GUI session 的 Terminal」跑(麥克風權限綁 TCC,ssh 進來的 shell 拿不到)
export HF_HUB_OFFLINE=1          # whisper 模型已快取時,跳過每 chunk 的 HF 網路檢查
genie-live --audio-device 0 --port 5200
```

| 參數 | 預設 | 說明 |
|---|---|---|
| `--host` | 127.0.0.1 | **勿改綁 0.0.0.0**——這個 server 能開麥克風/螢幕錄製 |
| `--port` | 5200 | |
| `--url` | `http://localhost:1234/v1` | LM Studio API |
| `--text-model` | 自動挑選 | 分析用文字模型 |
| `--audio-device` | 0 | avfoundation 音訊裝置 index(見下) |

瀏覽器開 `http://127.0.0.1:5200` → 按「開始錄製」。

### 音訊裝置選擇

```bash
ffmpeg -f avfoundation -list_devices true -i ""    # 列出裝置
```

- 錄「會議聲音」選會議 app 的虛擬裝置(如 `WeMeet Audio Device`);錄「現場」選內建麥克風
- **裝置 index 會漂**:插拔耳機/iPhone 會改變編號,啟動前先確認
- **坑:帶 mic 的耳機插入 3.5mm 會讓會議 app 自動切到(通常靜音的)耳機 mic**,會議突然沒聲音先查這個

## UI 功能

- **即時逐字稿**:fast(淡色斜體)→ 30 秒後被 refined(正常字)覆蓋;自動捲動,上捲即暫停
- **當前主題與重點** + **整場整理(時間軸)**:按時間分塊(議題+時間範圍+points+決議),已結束的塊凍結不改寫
- **問題清單**:會前/會中加入想確認的問題,講到即標 answered 並附答案
- **爭議偵測**:偵測到不同立場時顯示各方觀點
- **ASR 詞彙表**:專有名詞熱詞(即設即生效);`錯詞=正詞` 格式強制取代;會議中自動學習新術語(虛線 chip,× 可移除並黑名單);落盤 `~/.genie/live_vocabulary.json` 跨場繼承
- **靜音閘門**:音量條 + 自動/手動閾值;自動模式用雙峰分布偵測,手動建議 -50 ~ -25 dB

## 自動保護

- **連續 10 分鐘無語音自動停止錄製**(會議結束忘記關的保護),UI 顯示黃色橫幅
- whisper 幻聽三層過濾(機率過濾 + 段內/跨段重複摺疊)
- LLM 分析與轉寫分離佇列,分析慢不會堵字幕

## API(自動化用)

| Endpoint | 說明 |
|---|---|
| `POST /api/start` `{questions?, audio_device?}` | 開始錄製(會 reset 上一場) |
| `POST /api/stop` | 停止錄製 |
| `GET /api/state` | 即時狀態(最近字幕/分析/問題/閘門/詞彙) |
| `GET /api/transcript` | 完整逐字稿 |
| `POST /api/questions` `{questions:[...]}` | 更新問題清單 |
| `POST /api/vocabulary` `{vocabulary:[...]}` | 更新 user 詞彙 |
| `POST /api/vocabulary/blacklist` `{term}` | 移除並黑名單一個自動學習詞 |
| `POST /api/gate` `{mode, threshold_db?}` | 靜音閘門設定 |

## 模擬測試(不用開真會議)

```bash
python tests/simulate_meeting.py recording.mp4    # 用既有錄音模擬即時 chunk 流
```

## 已知限制

- 詞彙黑名單只在 session 內有效(重啟後若再被講到會重新學)
- 自動學習詞可能學到轉寫錯字(已限制只從 refined 段抽取),看到怪詞按 × 即可
