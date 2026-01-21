# 使用 NVIDIA 官方提供的 CUDA 運行環境 (包含 CUDNN)
FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

# 設定環境變數：防止 Python 產生 .pyc 檔、強制輸出 log
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# 設定 Hugging Face 快取目錄至 /tmp (因為 Cloud Run 只有 /tmp 可寫入)
ENV HF_HOME=/tmp/huggingface
ENV XDG_CACHE_HOME=/tmp/cache

# 安裝系統依賴
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-dev \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# 安裝 Python 套件
# faster-whisper: 核心轉錄庫
# fastapi uvicorn: 網頁框架
# google-cloud-storage: 操作 GCS
RUN pip3 install --no-cache-dir \
    faster-whisper \
    fastapi \
    uvicorn \
    google-cloud-storage

# 設定工作目錄
WORKDIR /app

# 複製程式碼
COPY . .

# 建置時預先下載模型 (避免執行時下載失敗)
RUN python3 download_model.py

# 啟動命令 (監聽 8080 端口，這是 Cloud Run 的規定)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]