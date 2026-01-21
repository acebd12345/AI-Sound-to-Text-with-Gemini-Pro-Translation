# AI 語音轉錄翻譯系統 - 啟動指南

本文件說明如何配置與啟動此專案。本系統包含兩個服務：
1. **API 後端 (main.py)**: 處理檔案上傳、與 Gemini Pro 整合進行翻譯。
2. **GPU 轉錄服務 (gpu-worker)**: 部署於 Google Cloud Run，負責接收上傳的音檔並使用 Whisper 模型進行轉錄。

## 前置需求

在開始之前，請確保您擁有以下資源：
- **Python 3.8+**
- **Google Cloud Platform (GCP) 帳號**
  - 已建立專案 (Project)
  - 已啟用 Cloud Storage, Cloud Run, Eventarc, Vertex AI (Gemini) API
- **Gemini API Key**: 用於調用翻譯模型
- **Google Cloud Storage (GCS) Bucket**: 用於儲存音檔與轉錄結果

## 快速啟動 (本機開發環境)

### 1. 安裝 Python 套件

請執行以下指令安裝所需套件：

```bash
pip install -r requirements.txt
```

### 2. 環境變數設定

本專案使用 `.env` 檔案管理敏感資訊，請勿將金鑰直接寫入程式碼。

1. 複製範例檔案：
   ```bash
   cp .env.example .env
   ```
2. 編輯 `.env` 檔案，填入您的資訊：
   ```properties
   GEMINI_API_KEY=您的_API_KEY
   BUCKET_NAME=您的_GCS_BUCKET_名稱
   ```

### 3. 設定 GCP 認證

若您是在本機執行，需要授權程式存取 GCP 資源 (GCS Bucket)：

```bash
gcloud auth application-default login
```
請依照瀏覽器指示完成登入。

### 4. 啟動後端伺服器

執行以下指令啟動 FastAPI 伺服器：

```bash
python main.py
```
伺服器預設於 `http://localhost:8000` 運行。

### 5. 使用前端介面

直接使用瀏覽器開啟專案目錄下的 `index.html` 檔案即可開始使用。

---

## 關於 GPU 轉錄服務 (Worker)

本系統採非同步架構。當您透過前端上傳檔案後，檔案會被存入 GCS Bucket 的 `raw_audio/` 目錄。

**注意**：`main.py` 僅負責上傳檔案與最後的翻譯。中間的「語音轉錄」步驟需要由 `gpu-worker` 執行。

若您尚未部署 Worker，系統將無法完成轉錄，前端會一直顯示「處理中」。

### 部署 Worker 至 Cloud Run

請參閱 `gpu-worker/` 目錄下的 `ai_studio_code.sh` 腳本進行部署。您需要配置 Eventarc 觸發器，當 GCS 有新檔案建立時，自動呼叫此 Cloud Run 服務。

若您希望在本機進行測試且擁有 GPU，您需要自行修改程式邏輯，直接在本機呼叫 Whisper 模型，而非依賴雲端觸發。
