# Hermes Kit — 議會自動錄製值班包

把這整個資料夾複製到 Hermes Agent 所在的機器上即可使用。

## 內容物

| 檔案 | 用途 |
|---|---|
| `council_ops.py` | 工具 CLI（Hermes 的手腳）：觸發、追蹤、合併字幕、寄信 |
| `HERMES_AGENT.md` | 給 Hermes 的任務指令（大腦的工作說明） |
| `.env.example` | 設定範本 → 複製為 `.env` 填入實際值 |
| `secret.txt` | 觸發密鑰（不在 git 裡；遺失時向管理者索取） |

## 安裝步驟

1. 複製整個 `hermes-kit/` 到 Hermes 機器（確認 `secret.txt` 有跟到）。
2. `cp .env.example .env`，填入 SMTP 帳密與收件人。
3. 測試（只需 Python 3.8+，無需 pip install）：
   ```bash
   python3 council_ops.py health    # 應回 {"status": "ok"}
   python3 council_ops.py trigger   # 應回掃描結果 JSON（沒開會時為空）
   python3 council_ops.py today     # 今天的直播/VOD 概況
   ```
4. 把 `HERMES_AGENT.md` 的內容設為 Hermes 的任務指令（skill / 常駐任務），
   並確保 Hermes 的工作目錄能存取本資料夾。

## 產出位置

合併完成的字幕輸出到 `output/`（`collect` 命令自動建立），
寄信後可清理。
