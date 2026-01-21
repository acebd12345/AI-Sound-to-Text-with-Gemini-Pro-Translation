# Google Cloud Platform (GCP) 完整部署指南

本指南將教您如何將此語音轉錄系統部署至 Google Cloud。

## 架構概觀
1. **Frontend + Backend (Cloud Run)**: 託管 `index.html` 與 `main.py`，負責使用者介面與流程控制。
2. **GPU Worker (Cloud Run)**: 負責執行 Whisper 模型進行轉錄 (需使用 GPU)。
3. **Cloud Storage (GCS)**: 儲存音檔與轉錄結果。
4. **Eventarc**: 當 GCS 有新檔案時，自動觸發 GPU Worker。

---

## 第一步：準備工作

### 1. 安裝 Google Cloud CLI
請確保您已安裝並登入 `gcloud` 工具：
```bash
gcloud auth login
gcloud config set project [您的專案ID]
```

### 2. 啟用必要 API
執行以下指令啟用所需的 Google Cloud 服務：
```bash
gcloud services enable \
  run.googleapis.com \
  storage.googleapis.com \
  eventarc.googleapis.com \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com
```

### 3. 建立 Storage Bucket
建立一個用於存檔的 Bucket (請將名稱替換為全域唯一的名稱)：
```bash
export BUCKET_NAME="doit-digiinnova-soundtotext"
export LOCATION="us-central1"

gcloud storage buckets create gs://$BUCKET_NAME --location=$LOCATION
```

---

## 第二步：部署後端 (Backend)

負責網頁介面與協調。

1. **部署到 Cloud Run**
   請在專案根目錄執行 (將 `[您的API_KEY]` 替換為真實 Key)：

   ```bash
   gcloud run deploy sound-to-text-web \
     --source . \
     --region $LOCATION \
     --allow-unauthenticated \
     --set-env-vars GEMINI_API_KEY=[您的API_KEY] \
     --set-env-vars BUCKET_NAME=$BUCKET_NAME
   ```

2. **驗證**
   部署完成後，會顯示一個 URL (例如 `https://sound-to-text-web-xyz.a.run.app`)。
   點擊該 URL，您應該能看到網頁介面。

---

## 第三步：部署 GPU Worker

負責實際的語音轉錄。**注意：Cloud Run GPU 目前僅在特定區域可用 (如 us-central1)。**

1. **進入 Worker 目錄**
   ```bash
   cd gpu-worker
   ```

2. **部署至 Cloud Run (需 GPU)**
   這一步可能需要幾分鐘。這裡我們使用 NVIDIA L4 GPU (需配額)。

   ```bash
   gcloud run deploy gpu-whisper-worker \
     --source . \
     --region $LOCATION \
     --no-allow-unauthenticated \
     --gpu 1 \
     --gpu-type nvidia-l4 \
     --memory 16Gi \
     --cpu 4 \
     --concurrency 1 \
     --timeout 3600
   ```
   *若遇到配額不足錯誤，請嘗試申請配額或切換區域。*

---

## 第四步：設定 Eventarc 觸發器

這是最關鍵的一步：將 GCS 的「檔案上傳事件」連接到「GPU Worker」。

1. **授權 GCS 發布事件**
   ```bash
   SERVICE_ACCOUNT=$(gcloud storage service-agent)
   
   gcloud projects add-iam-policy-binding [您的專案ID] \
     --member serviceAccount:$SERVICE_ACCOUNT \
     --role roles/pubsub.publisher
   ```

2. **建立觸發器**
   當檔案上傳至 `raw_audio/` 資料夾時觸發。

   **檢查變數設定 (重要)**
   執行以下指令，確認 Bucket 名稱前後沒有多餘空白：
   ```bash
   # 去除可能存在的空白
   export BUCKET_NAME=$(echo $BUCKET_NAME | xargs)
   echo "Bucket 名稱: '$BUCKET_NAME'"
   ```
   *如果顯示的名稱包含引號外的空白 (例如 ' name ')，請重新設定 BUCKET_NAME。*

   **設定 Service Account**
   自動取得您的預設 Compute Engine Service Account：
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format="value(projectNumber)")
   SERVICE_ACCOUNT="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
   
   echo "將使用 Service Account: $SERVICE_ACCOUNT"
   ```

   **建立觸發器** (請直接複製貼上執行)：
   ```bash
   gcloud eventarc triggers create trigger-whisper \
     --location=$LOCATION \
     --destination-run-service=gpu-whisper-worker \
     --destination-run-region=$LOCATION \
     --event-filters="type=google.cloud.storage.object.v1.finalized" \
     --event-filters="bucket=$BUCKET_NAME" \
     --service-account=$SERVICE_ACCOUNT
   ```
   *(注意：如果 Eventarc 報錯，可能需要賦予 Eventarc Service Agent 權限，請參考 GCP 錯誤訊息提示)*

---

## 第五步：測試

1. 開啟 **第二步** 獲得的網頁 URL。
2. 上傳一個測試音檔。
3. 觀察網頁日誌。
   - 狀態會先顯示「上傳完成」。
   - 然後 Eventarc 會觸發 Worker，Worker 開始轉錄。
   - 最後 `main.py` 偵測到轉錄完成，進行翻譯並顯示下載按鈕。

---

## 常見問題 (Troubleshooting)

- **一直顯示「處理中」**：
  - 檢查 GCS Bucket `raw_audio/` 是否有檔案。
  - 檢查 Cloud Run `gpu-whisper-worker` 的日誌 (Logs)，看是否有被觸發。
  - 如果沒被觸發，檢查 Eventarc 設定。

- **GPU 部署失敗**：
  - 確保您的專案有 GPU (L4) 配額。
  - 嘗試使用 `us-central1` 區域。

## 如何排查問題 (Debugging)

若網頁一直卡在「處理中」或沒有反應，請依照以下步驟檢查：

### 1. 檢查檔案是否上傳成功
執行以下指令查看 GCS Bucket：
```bash
gcloud storage ls gs://$BUCKET_NAME/raw_audio/
```
*如果有看到檔案，代表上傳成功，問題出在觸發或轉錄。*

### 2. 檢查 GPU Worker 是否有被觸發
查看 `gpu-whisper-worker` 的最新日誌：
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=gpu-whisper-worker" --limit 20 --format="value(textPayload)"
```
*   **正常**：應該看到 "收到觸發請求...", "開始處理檔案...", "轉錄成功！"。
*   **沒反應**：代表 Eventarc 設定有誤，或配額不足導致容器無法啟動。
*   **錯誤**：如果有 "Permission denied"，代表 Service Account 權限不足。

### 3. 檢查 Eventarc 觸發狀態
前往 Google Cloud Console > Eventarc > Triggers，檢查 `trigger-whisper` 的狀態是否為綠色打勾。

### 4. 檢查後端日誌
查看 `sound-to-text-web` 的日誌：
```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=sound-to-text-web" --limit 20 --format="value(textPayload)"
```

### 5. 常見錯誤對照表

#### 🔴 錯誤：`ValueError: Bucket names must start and end with a number or letter`
*   **現象**：在後端日誌中看到此錯誤，且網頁卡在「處理中」。
*   **原因**：`BUCKET_NAME` 環境變數中包含空白鍵 (例如 `"my-bucket "`)。
*   **解決方法**：
    1. 修正本地變數：`export BUCKET_NAME=$(echo $BUCKET_NAME | xargs)`
    2. 更新 Cloud Run 服務：
       ```bash
       gcloud run services update sound-to-text-web --region=$LOCATION --update-env-vars BUCKET_NAME=$BUCKET_NAME
       ```

#### 🔴 錯誤：`ModuleNotFoundError: No module named 'fastapi'` (GPU Worker)
*   **現象**：GPU Worker 啟動失敗，日誌顯示找不到模組，或出現 `gunicorn` 相關錯誤。
*   **原因**：Cloud Run 忽略了 `Dockerfile.txt`，改用自動建置 (Buildpacks)，導致沒有安裝正確的依賴。
*   **解決方法**：
    1. 確保 `gpu-worker/` 目錄下的檔案名稱確切為 `Dockerfile` (沒有 .txt 副檔名)。
    2. 重新執行 GPU Worker 的部署指令。

#### 🔴 錯誤：`Missing required argument [--clear-base-image]`
*   **現象**：重新部署 GPU Worker 時出現此錯誤。
*   **原因**：因為第一次部署失敗時使用了自動建置 (Buildpacks)，現在改用 Dockerfile 需要清除舊的建置設定。
*   **解決方法**：在部署指令最後加上 `--clear-base-image` 參數。

#### 🔴 錯誤：`Quota violated` 或 `Max instances must be set to X`
*   **現象**：部署失敗，顯示配額不足 (requested: 10 allowed: 3)。
*   **原因**：Cloud Run 對於 GPU 實例有嚴格的數量限制 (預設通常很低，如 1 或 3)。
*   **解決方法**：將 `--max-instances` 參數調低 (例如改為 1 或 3)，或是向 Google 申請增加 GPU 配額。
