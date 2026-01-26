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
# 您指定要用 Pro (API 代號目前為 gemini-3-pro-preview)
MODEL_NAME = "gemini-3-pro-preview"

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

# --- Gemini 分段翻譯函式 (Async) ---
async def translate_segment_pro(srt_content, index, semaphore):
    """使用 Gemini Pro 翻譯單一區塊 (SRT)"""
    async with semaphore:
        print(f"[Gemini Pro] 正在翻譯第 {index} 段 (SRT)...")
        model = genai.GenerativeModel(MODEL_NAME)
        
        prompt = f"""You are a professional subtitle translator and Traditional Chinese localization expert.
Task: Translate and Convert the following SRT subtitle content into Traditional Chinese (Taiwan) (繁體中文).

CRITICAL RULES:
1. KEEP the numeric indices and timestamps EXACTLY as they are. Do NOT modify them.
2. ONLY translate/rewrite the subtitle text lines.
3. Output the result in standard SRT format.
4. ENSURE ALL TEXT IS IN TRADITIONAL CHINESE (Taiwan). Convert any Simplified Chinese characters or foreign terms into standard Taiwan Traditional Chinese.
5. DETECT HALLUCINATIONS: If a subtitle line appears to be an ASR hallucination (e.g., repetitive nonsense, "Subscribe", "Thanks for watching", or random characters unrelated to context), replace the text with "..." or leave it blank.
6. Do not include any explanation or markdown formatting (like ```srt). Just the raw SRT content.

{srt_content}"""
        
        try:
            # 使用非同步生成
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e:
            print(f"第 {index} 段翻譯失敗: {e}")
            return srt_content # 失敗則回傳原文

# --- 上傳接口 (保持不變) ---
@app.post("/upload_chunk")
async def upload_chunk(
    file_chunk: UploadFile,
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file_id: str = Form(...),
    mode: str = Form("speech")
):
    # 這裡我們直接上傳到 GCS 的 'raw_audio' 資料夾
    bucket = storage_client.bucket(BUCKET_NAME)
    
    # 如果是第一塊，順便儲存 metadata
    if chunk_index == 0:
        meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
        meta_blob.upload_from_string(json.dumps({"mode": mode}))

    blob = bucket.blob(f"raw_audio/{file_id}/{chunk_index}")
    blob.upload_from_file(file_chunk.file)
    
    # 這裡省略觸發 GPU 轉錄的代碼 (假設 Eventarc 已經設定好去觸發另一個 Cloud Run)
    # 或者您可以在這裡直接呼叫 GPU Service
    
    return {"status": "uploaded", "index": chunk_index}

# --- 核心：狀態檢查與自動合併接口 ---
@app.get("/check_status/{file_id}")
async def check_status(file_id: str, total_chunks: int, background_tasks: BackgroundTasks):
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
        content = final_blob.download_as_text()
        return {"status": "completed", "text": content}
    
    # 3. 檢查是否正在翻譯中 (Lock)
    lock_blob = bucket.blob(f"locks/{file_id}")
    if lock_blob.exists():
        return {"status": "processing", "progress": "AI 正在翻譯中 (請稍候)..."}

    # 4. 尚未翻譯，啟動後台任務
    print("啟動後台翻譯任務...")
    lock_blob.upload_from_string("locked") # 上鎖
    background_tasks.add_task(run_translation_background, file_id, total_chunks, bucket)
    
    return {"status": "processing", "progress": "已排入翻譯佇列..."}

async def run_translation_background(file_id, total_chunks, bucket):
    try:
        print(f"[{file_id}] 開始後台翻譯...")
        
        # 1. 平行下載所有 Transcript (IO Bound)
        print(f"[{file_id}] 正在下載所有轉錄檔...")
        loop = asyncio.get_running_loop()
        blob_names = [f"transcripts/{file_id}_part_{i}.json" for i in range(total_chunks)]
        blobs = [bucket.blob(name) for name in blob_names]
        
        # 使用 run_in_executor 讓 blocking IO 不卡住 event loop
        download_tasks = [loop.run_in_executor(None, blob.download_as_text) for blob in blobs]
        results_json = await asyncio.gather(*download_tasks)
        
        # 2. 準備翻譯任務
        print(f"[{file_id}] 準備翻譯任務...")
        semaphore = asyncio.Semaphore(8) # 限制同時 8 個請求以免被 Rate Limit
        translation_tasks = []
        ordered_batches = [] # 用來存放順序資訊 (chunk_index, batch_index)
        
        current_time_offset = 0.0
        current_srt_index = 1
        BATCH_SIZE = 50

        # 先預處理所有 SRT 文本
        for i, json_content in enumerate(results_json):
            data = json.loads(json_content)
            segments = data.get("segments", [])
            chunk_duration = data.get("duration", 0.0)
            
            chunk_srt_lines = []
            for seg in segments:
                start = format_timestamp(seg['start'] + current_time_offset)
                end = format_timestamp(seg['end'] + current_time_offset)
                text = seg['text'].strip()
                chunk_srt_lines.append(f"{current_srt_index}\n{start} --> {end}\n{text}")
                current_srt_index += 1
            
            current_time_offset += chunk_duration
            
            # 分批建立 Task
            if chunk_srt_lines:
                for k in range(0, len(chunk_srt_lines), BATCH_SIZE):
                    batch_lines = chunk_srt_lines[k:k+BATCH_SIZE]
                    batch_content = "\n\n".join(batch_lines)
                    
                    # 建立 Async Task
                    task = translate_segment_pro(batch_content, f"{i}_{k}", semaphore)
                    translation_tasks.append(task)
                    ordered_batches.append((i, k)) # 紀錄順序

        # 3. 平行執行翻譯 (Network Bound)
        if translation_tasks:
            print(f"[{file_id}] 啟動 {len(translation_tasks)} 個翻譯任務 (併發)...")
            translated_results = await asyncio.gather(*translation_tasks)
            
            # 4. 組合結果
            full_translated_text = ""
            for res in translated_results:
                full_translated_text += res + "\n\n"
        else:
            full_translated_text = ""

        # 存檔
        final_blob = bucket.blob(f"final_results/{file_id}_TW_Complete.txt")
        final_blob.upload_from_string(full_translated_text)
        print(f"[{file_id}] 翻譯完成並存檔！")

    except Exception as e:
        print(f"[{file_id}] 後台翻譯失敗: {e}")
    finally:
        # 解鎖
        bucket.blob(f"locks/{file_id}").delete(ignore_errors=True)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)