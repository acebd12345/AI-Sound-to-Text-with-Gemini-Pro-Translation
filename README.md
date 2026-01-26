# AI 語音轉錄與翻譯系統 (Pro 版)

這是一個強大的語音轉文字 (Speech-to-Text) 與翻譯系統，結合了 **OpenAI Whisper (faster-whisper)** 的精準轉錄能力與 **Google Gemini 3 Pro (Preview)** 的高品質翻譯能力。

本系統專為處理長錄音檔設計，支援自動分段、斷點續傳，並利用 Google Cloud Platform (GCP) 的 GPU 加速轉錄過程，最後輸出繁體中文 (台灣) 的 SRT 字幕檔。

## ✨ 主要功能

*   **高精準度轉錄**：使用 `faster-whisper` (Large-v3-turbo 模型) 進行語音識別，支援多語言輸入。
*   **專業級翻譯**：整合 Google Gemini 3 Pro (Preview) 模型，將轉錄內容翻譯成流暢的繁體中文 (台灣)。
*   **雙模式支援**：
    *   **🗣️ 一般對話/會議模式**：自動過濾靜音，適合訪談、會議記錄。
    *   **🎵 歌曲/歌詞模式**：保留人聲細節與時間軸，適合製作歌詞字幕。
*   **長音檔支援**：前端自動將大檔案切片上傳，後端分段處理，無懼數小時的錄音檔。
*   **雲端架構**：設計為部署於 GCP Cloud Run，利用 Eventarc 實現自動化流水線 (Pipeline)。

## 🏗️ 系統架構

系統主要由三個部分組成：

1.  **前端與 API 伺服器 (`main.py`)**：
    *   提供 Web 介面 (`index.html`) 供使用者上傳檔案與查看進度。
    *   負責檔案切片上傳至 Google Cloud Storage (GCS)。
    *   協調最終的翻譯流程 (當所有分段轉錄完成後，呼叫 Gemini API)。
2.  **GPU Worker (`gpu-worker/`)**：
    *   一個獨立的服務，建議運行在支援 GPU 的環境 (如 GCP Cloud Run GPU)。
    *   監聽 GCS 的檔案上傳事件 (Eventarc)。
    *   使用 Whisper 模型將音訊轉錄為文字 (JSON 格式)。
3.  **Google Cloud Storage (GCS)**：
    *   作為中間存儲，存放原始音檔 (`raw_audio/`)、轉錄中間檔 (`transcripts/`) 與最終結果 (`final_results/`)。

## 🚀 快速開始 (本地開發)

雖然本系統是為雲端部署設計，但您也可以在本地進行部分測試。

### 前置需求

*   Python 3.8+
*   Google Cloud Platform (GCP) 帳號與專案。
*   GCS Bucket (存儲桶)。
*   Google Gemini API Key (可於 Google AI Studio 申請)。
*   (選用) NVIDIA GPU 與 CUDA 環境 (若要在本地運行 Worker)。

### 安裝步驟

1.  **複製專案**
    ```bash
    git clone [repo_url]
    cd soundtotext
    ```

2.  **設定環境變數**
    複製 `.env.example` 並重新命名為 `.env`，填入您的設定：
    ```bash
    cp .env.example .env
    ```
    編輯 `.env` 檔案：
    ```env
    GEMINI_API_KEY=您的_Gemini_API_Key
    BUCKET_NAME=您的_GCS_Bucket_名稱
    ```

3.  **安裝依賴**
    ```bash
    pip install -r requirements.txt
    # 若沒有 requirements.txt，請參考 main.py 的 import 安裝：
    # pip install fastapi uvicorn python-dotenv google-cloud-storage google-generativeai python-multipart
    ```

4.  **設定 GCP 認證**
    確保您的環境已登入 GCP 並且有存取該 Bucket 的權限：
    ```bash
    gcloud auth application-default login
    ```

### 啟動服務

**1. 啟動 API 伺服器 (Frontend + Backend)**

```bash
python main.py
```
伺服器將在 `http://localhost:8000` 啟動。

**2. 關於 GPU Worker**

GPU Worker (`gpu-worker/main.py`) 設計為由 Eventarc 觸發。若要在本地測試 Worker，您需要模擬 Eventarc 的 POST 請求，並且您的電腦需要有 NVIDIA GPU 與 CUDA 環境。

若您只是想測試前端流程，可以手動上傳檔案到 GCS，然後手動觸發 Worker (或略過 Worker 直接測試 API 邏輯，但 API 會等待 Worker 的產出結果)。

## ☁️ 部署至 Google Cloud Platform

本專案已針對 GCP Cloud Run 進行優化，支援 GPU 加速與 Serverless 架構。

詳細部署步驟請參閱：[**DEPLOY_GCP.md**](./DEPLOY_GCP.md)

部署概略：
1.  建立 GCS Bucket。
2.  部署 `main.py` 到 Cloud Run (CPU)。
3.  部署 `gpu-worker/` 到 Cloud Run (GPU)。
4.  設定 Eventarc 觸發器，連接 GCS 與 GPU Worker。

## 📂 目錄結構

```
.
├── DEPLOY_GCP.md       # GCP 部署教學文件
├── Dockerfile          # API Server 的 Dockerfile
├── README.md           # 專案說明文件
├── gpu-worker/         # GPU Worker 相關程式碼
│   ├── Dockerfile      # GPU Worker 的 Dockerfile (含 CUDA)
│   ├── download_model.py # 預下載 Whisper 模型腳本
│   └── main.py         # Worker 主程式 (Whisper 推論)
├── index.html          # 前端介面
├── main.py             # API Server 主程式 (FastAPI + Gemini)
└── requirements.txt    # (如果有) 專案依賴列表
```

## 📝 注意事項

*   **成本控制**：Cloud Run GPU 與 Gemini Pro API 可能會產生費用，請留意您的 GCP 帳單與配額。
*   **檔案清理**：GCS 上的暫存檔案 (`raw_audio/`, `transcripts/`) 目前不會自動刪除，建議設定 GCS Lifecycle 規則定期清理。
*   **模型載入**：GPU Worker 啟動時需要載入 Whisper 模型，第一次請求可能會有 Cold Start 延遲。

## 授權

MIT License
