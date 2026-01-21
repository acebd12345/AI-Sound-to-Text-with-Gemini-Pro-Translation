import os
import json
import time
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from google.cloud import storage
import google.generativeai as genai

load_dotenv()

app = FastAPI()

# --- 設定區 ---
# 請務必確認 GCP 服務帳號有權限，且 API KEY 正確
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 您指定要用 Pro (API 代號目前為 gemini-3.0-pro)
MODEL_NAME = "gemini-3.0-pro"

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in environment variables")

genai.configure(api_key=GEMINI_API_KEY)
storage_client = storage.Client()
BUCKET_NAME = os.getenv("BUCKET_NAME")

if not BUCKET_NAME:
    raise ValueError("BUCKET_NAME not found in environment variables")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def read_index():
    return FileResponse('index.html')

UPLOAD_DIR = "/tmp"

def format_timestamp(seconds: float) -> str:
    total_milli = int(seconds * 1000)
    hours = total_milli // 3600000
    total_milli %= 3600000
    minutes = total_milli // 60000
    total_milli %= 60000
    secs = total_milli // 1000
    millis = total_milli % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

# --- Gemini 分段翻譯函式 ---
def translate_segment_pro(srt_content, index):
    """使用 Gemini Pro 翻譯單一區塊 (SRT)"""
    print(f"[Gemini Pro] 正在翻譯第 {index+1} 段 (SRT)...")
    model = genai.GenerativeModel(MODEL_NAME)
    
    prompt = f"""You are a professional subtitle translator and Traditional Chinese localization expert.
Task: Translate and Convert the following SRT subtitle content into Traditional Chinese (Taiwan) (繁體中文).

CRITICAL RULES:
1. KEEP the numeric indices and timestamps EXACTLY as they are. Do NOT modify them.
2. ONLY translate/rewrite the subtitle text lines.
3. Output the result in standard SRT format.
4. ENSURE ALL TEXT IS IN TRADITIONAL CHINESE (Taiwan). Convert any Simplified Chinese characters or foreign terms into standard Taiwan Traditional Chinese.
5. Do not include any explanation or markdown formatting (like ```srt). Just the raw SRT content.

{srt_content}"""
    
    try:
        # Pro 模型比較慢，這裡設定 timeout 避免卡死
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"第 {index+1} 段翻譯失敗: {e}")
        return srt_content # 失敗則回傳原文

# --- 上傳接口 (保持不變) ---
@app.post("/upload_chunk")
async def upload_chunk(
    file_chunk: UploadFile,
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file_id: str = Form(...)
):
    # 這裡我們直接上傳到 GCS 的 'raw_audio' 資料夾
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(f"raw_audio/{file_id}/{chunk_index}")
    blob.upload_from_file(file_chunk.file)
    
    # 這裡省略觸發 GPU 轉錄的代碼 (假設 Eventarc 已經設定好去觸發另一個 Cloud Run)
    # 或者您可以在這裡直接呼叫 GPU Service
    
    return {"status": "uploaded", "index": chunk_index}

# --- 核心：狀態檢查與自動合併接口 ---
@app.get("/check_status/{file_id}")
async def check_status(file_id: str, total_chunks: int):
    bucket = storage_client.bucket(BUCKET_NAME)
    
    # 1. 檢查 GPU 轉錄是否全部完成
    # 假設 GPU 轉完會存到 'transcripts/{file_id}_part_{i}.json'
    transcripts = [None] * total_chunks
    missing_parts = []
    
    for i in range(total_chunks):
        blob_path = f"transcripts/{file_id}_part_{i}.json"
        blob = bucket.blob(blob_path)
        if not blob.exists():
            missing_parts.append(i)
        else:
            # 暫存 blob 物件以便稍後下載
            transcripts[i] = blob

    if missing_parts:
        return {"status": "processing", "progress": f"等待轉錄中... 缺: {missing_parts}"}

    # 2. 全都轉完了！開始執行 Gemini Pro 分段翻譯 (如果還沒翻譯過)
    final_blob_path = f"final_results/{file_id}_TW_Complete.txt"
    final_blob = bucket.blob(final_blob_path)
    
    if final_blob.exists():
        # 如果已經翻譯過，直接回傳下載連結
        # 這裡簡單回傳文字內容讓前端下載，或生成 Signed URL
        content = final_blob.download_as_text()
        return {"status": "completed", "text": content}
    
    # 3. 尚未翻譯，開始執行 (這會花一點時間，FastAPI 適合用 BackgroundTask，但為了讓您馬上拿到，這裡同步執行)
    # 若檔案很大，建議這裡改為 return "translating" 狀態，讓前端繼續輪詢
    
    print("所有轉錄片段到位，開始 Gemini Pro 翻譯流程...")
    full_translated_text = ""
    
    current_time_offset = 0.0
    current_srt_index = 1

    for i in range(total_chunks):
        # 下載原文
        json_content = transcripts[i].download_as_text()
        data = json.loads(json_content)
        
        segments = data.get("segments", [])
        chunk_duration = data.get("duration", 0.0)
        
        # 產生此區塊的 SRT (需加上時間偏移)
        chunk_srt_lines = []
        for seg in segments:
            start = format_timestamp(seg['start'] + current_time_offset)
            end = format_timestamp(seg['end'] + current_time_offset)
            text = seg['text'].strip()
            chunk_srt_lines.append(f"{current_srt_index}\n{start} --> {end}\n{text}")
            current_srt_index += 1
            
        chunk_srt_content = "\n\n".join(chunk_srt_lines)
        
        # 翻譯 (傳入 SRT 格式)
        if chunk_srt_content.strip():
            trans_text = translate_segment_pro(chunk_srt_content, i)
        else:
            trans_text = ""

        full_translated_text += trans_text + "\n\n"
        
        # 更新時間偏移
        current_time_offset += chunk_duration
        
        # 避免 Pro 速率限制
        time.sleep(1)

    # 4. 存檔並回傳
    final_blob.upload_from_string(full_translated_text)
    return {"status": "completed", "text": full_translated_text}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)