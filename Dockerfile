FROM python:3.10-slim

WORKDIR /app

# 安裝系統依賴 (如果需要)
# RUN apt-get update && apt-get install -y ...

# 複製需求檔
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 複製程式碼與前端
COPY . .

# Cloud Run 預設監聽 8080，但這裡我們用 uvicorn 啟動
# 注意: Cloud Run 會透過 $PORT 環境變數傳入端口，通常是 8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
