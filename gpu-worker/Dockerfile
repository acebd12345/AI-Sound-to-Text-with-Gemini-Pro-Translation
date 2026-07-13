# 使用 Python 基底映像檔 (CUDA runtime 由 pip wheels 提供)
# faster-whisper (CTranslate2 4.x) 和 PyTorch cu121 wheels 已內建 CUDA libraries
# Cloud Run GPU 實例會自動掛載 NVIDIA 驅動程式
FROM python:3.10-slim

# 設定環境變數：防止 Python 產生 .pyc 檔、強制輸出 log
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# 設定 Hugging Face 快取目錄至 /tmp (因為 Cloud Run 只有 /tmp 可寫入)
ENV HF_HOME=/tmp/huggingface
ENV XDG_CACHE_HOME=/tmp/cache

# 安裝系統依賴 (只需 ffmpeg 處理音訊、git 供部分 pip 套件)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Python 套件
# faster-whisper: 核心轉錄庫 (CTranslate2 內建 CUDA)
# fastapi uvicorn: 網頁框架
# google-cloud-storage: 操作 GCS
# torch + pyannote.audio: 講者辨識
RUN pip install --no-cache-dir \
    faster-whisper \
    fastapi \
    uvicorn \
    google-cloud-storage \
    "huggingface_hub<0.24" \
    torch==2.3.1+cu121 torchaudio==2.3.1+cu121 \
    pyannote.audio \
    --extra-index-url https://download.pytorch.org/whl/cu121

# 設定工作目錄
WORKDIR /app

# 複製程式碼
COPY . .

# 建置時預先下載模型 (避免執行時下載失敗)
# ARG 放在靠近使用處，確保 build-arg 正確傳入
ARG HF_TOKEN
RUN HF_TOKEN="${HF_TOKEN}" python download_model.py

# 啟動命令 (監聽 8080 端口，這是 Cloud Run 的規定)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
