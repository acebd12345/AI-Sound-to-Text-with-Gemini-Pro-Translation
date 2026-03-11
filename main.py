import os
import re
import json
import time
import random
import asyncio
import opencc
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed
import google.generativeai as genai
import google.ai.generativelanguage as glm
from google.api_core import client_options as client_options_lib

load_dotenv()

app = FastAPI()

# --- 設定區 ---
# 支援多 API Key 輪替：環境變數 GEMINI_API_KEYS（逗號分隔）優先，
# 若未設定則退回使用單一 GEMINI_API_KEY
MODEL_NAME = "gemini-3-pro-preview"

_keys_str = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [k.strip() for k in _keys_str.split(",") if k.strip()]
if not GEMINI_API_KEYS:
    _single = os.getenv("GEMINI_API_KEY", "")
    if _single:
        GEMINI_API_KEYS = [_single]

if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEY or GEMINI_API_KEYS not found in environment variables")

print(f"已載入 {len(GEMINI_API_KEYS)} 組 Gemini API Key")

# 為每把 Key 建立獨立的 gRPC client，注入到 GenerativeModel
# genai.configure 是全域的，GenerativeModel 不會綁定 Key，
# 所以必須手動建立獨立 client 注入，才能真正做到多 Key 輪替
genai.configure(api_key=GEMINI_API_KEYS[0])

GEMINI_MODELS = []
for _key in GEMINI_API_KEYS:
    model = genai.GenerativeModel(
        MODEL_NAME,
        system_instruction="你是一個專業的繁體中文（台灣）翻譯專家。你的唯一任務是將傳入的字幕內容，完美且毫無遺漏地翻譯或轉換為台灣慣用的繁體中文。絕對不允許輸出任何簡體字。"
    )
    client_opts = client_options_lib.ClientOptions(
        api_key=_key,
        api_endpoint="generativelanguage.googleapis.com",
    )
    model._client = glm.GenerativeServiceClient(client_options=client_opts)
    model._async_client = glm.GenerativeServiceAsyncClient(client_options=client_opts)
    GEMINI_MODELS.append(model)
    print(f"  Key ...{_key[-4:]} 已綁定獨立 client")

# Key 輪替計數器（Round-Robin）
_model_counter = 0
_model_lock = asyncio.Lock()

async def get_next_model() -> genai.GenerativeModel:
    """Round-Robin 取得下一個 Model（每個有獨立 gRPC client + API Key）"""
    global _model_counter
    async with _model_lock:
        model = GEMINI_MODELS[_model_counter % len(GEMINI_MODELS)]
        _model_counter += 1
        return model

storage_client = storage.Client()
BUCKET_NAME = os.getenv("BUCKET_NAME")

if not BUCKET_NAME:
    raise ValueError("BUCKET_NAME not found in environment variables")

# CORS 設定：透過環境變數 ALLOWED_ORIGINS 指定允許的來源（逗號分隔）
# 未設定時預設只允許同源請求
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "").split(",")
ALLOWED_ORIGINS = [o.strip() for o in ALLOWED_ORIGINS if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# HSTS：強制瀏覽器使用 HTTPS 連線（max-age=1 年）
class HSTSMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(HSTSMiddleware)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/")
async def read_index():
    return FileResponse('index.html')

UPLOAD_DIR = "/tmp"

# 全域 Semaphore：限制所有翻譯任務共享的 Gemini API 併發數
# 多人同時翻譯時，總併發不會超過此上限，避免觸發 Rate Limit
GEMINI_SEMAPHORE = asyncio.Semaphore(8)

# 驗證 file_id 防止路徑穿越攻擊
FILE_ID_PATTERN = re.compile(r'^[\w\-\.]+$')

def validate_file_id(file_id: str) -> str:
    if not file_id or not FILE_ID_PATTERN.match(file_id):
        raise HTTPException(status_code=400, detail="Invalid file_id: only alphanumeric, underscore, hyphen, and dot are allowed")
    if '..' in file_id:
        raise HTTPException(status_code=400, detail="Invalid file_id: path traversal not allowed")
    return file_id

def format_timestamp(seconds: float) -> str:
    total_milli = int(seconds * 1000)
    hours = total_milli // 3600000
    total_milli %= 3600000
    minutes = total_milli // 60000
    total_milli %= 60000
    secs = total_milli // 1000
    millis = total_milli % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

# --- Gemini 分段翻譯函式 (Async + Retry + Key 輪替) ---
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # 秒，exponential backoff 基底
API_TIMEOUT = 120  # 秒，單次 API 呼叫超時時間（給 Gemini 充足時間處理）

# 初始化 OpenCC 轉換器 (s2twp.json 代表: 簡體轉繁體台灣，包含慣用語轉換)
cc = opencc.OpenCC('s2twp.json')

async def translate_segment_pro(srt_content, index):
    """使用 Gemini Pro 翻譯單一區塊 (SRT)，含重試與 Key 輪替"""
    
    # 預處理：在送給 Gemini 之前，先強制用 OpenCC 將所有的簡體字與慣用語轉為台灣繁體
    # 這樣一來，Gemini 收到的文本就已經是繁體了，它的任務單純變成「潤飾」與「除錯」。
    # 就算 API 徹底失敗而退回原文，出來的也會是繁體字！
    preprocessed_srt = cc.convert(srt_content)

    async with GEMINI_SEMAPHORE:
        prompt = f"""You are a professional subtitle translator and Traditional Chinese localization expert.
Task: Translate and Convert the following SRT subtitle content into Traditional Chinese (Taiwan) (繁體中文).

CRITICAL RULES:
1. KEEP the numeric indices and timestamps EXACTLY as they are. Do NOT modify them.
2. ONLY translate/rewrite the subtitle text lines.
3. Output the result in standard SRT format.
4. ENSURE ALL TEXT IS IN TRADITIONAL CHINESE (Taiwan). Convert any Simplified Chinese characters or foreign terms into standard Taiwan Traditional Chinese.
5. ABSOLUTELY NO SIMPLIFIED CHINESE. You must not leave any Simplified Chinese characters (簡體字) in the output.
6. DETECT HALLUCINATIONS: If a subtitle line appears to be an ASR hallucination (e.g., repetitive nonsense, "Subscribe", "Thanks for watching", or random characters unrelated to context), replace the text with "..." or leave it blank.
7. Do not include any explanation or markdown formatting (like ```srt). Just the raw SRT content.

{preprocessed_srt}"""

        for attempt in range(MAX_RETRIES):
            model = await get_next_model()
            try:
                print(f"[Gemini Pro] 翻譯第 {index} 段 (嘗試 {attempt + 1}/{MAX_RETRIES})...")
                response = await asyncio.wait_for(
                    model.generate_content_async(
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            temperature=0.2,
                        )
                    ),
                    timeout=API_TIMEOUT
                )
                return response.text.strip()
            except asyncio.TimeoutError:
                print(f"第 {index} 段翻譯超時 ({API_TIMEOUT}s) (嘗試 {attempt + 1})")
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                    print(f"  → {delay:.1f} 秒後換 Key 重試...")
                    await asyncio.sleep(delay)
            except Exception as e:
                print(f"第 {index} 段翻譯失敗 (嘗試 {attempt + 1}): {e}")
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                    print(f"  → {delay:.1f} 秒後重試...")
                    await asyncio.sleep(delay)

        print(f"⚠ 第 {index} 段翻譯全部重試失敗，回傳原文")
        return preprocessed_srt

# --- 上傳接口 (保持不變) ---
@app.post("/upload_chunk")
async def upload_chunk(
    file_chunk: UploadFile,
    chunk_index: int = Form(...),
    total_chunks: int = Form(...),
    file_id: str = Form(...),
    mode: str = Form("speech")
):
    validate_file_id(file_id)

    try:
        bucket = storage_client.bucket(BUCKET_NAME)

        # 如果是第一塊，順便儲存 metadata
        if chunk_index == 0:
            meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
            meta_blob.upload_from_string(json.dumps({"mode": mode}))

        blob = bucket.blob(f"raw_audio/{file_id}/{chunk_index}")
        blob.upload_from_file(file_chunk.file)

        return {"status": "uploaded", "index": chunk_index}
    except Exception as e:
        print(f"上傳失敗 (file_id={file_id}, chunk={chunk_index}): {e}")
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)}")

# --- 核心：狀態檢查與自動合併接口 ---
@app.get("/check_status/{file_id}")
async def check_status(file_id: str, total_chunks: int, background_tasks: BackgroundTasks):
    validate_file_id(file_id)
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
        srt_content = final_blob.download_as_text()
        plain_blob = bucket.blob(f"final_results/{file_id}_TW_PlainText.txt")
        plain_content = plain_blob.download_as_text() if plain_blob.exists() else ""
        return {"status": "completed", "srt_text": srt_content, "plain_text": plain_content}
    
    # 3. 嘗試取得 Lock（原子性操作，防止競態條件）
    lock_blob = bucket.blob(f"locks/{file_id}")
    LOCK_TTL_SECONDS = 1800  # Lock 過期時間：30 分鐘

    # 檢查是否有現存的 lock
    if lock_blob.exists():
        try:
            lock_data = json.loads(lock_blob.download_as_text())
            lock_time = lock_data.get("locked_at", 0)
            if time.time() - lock_time < LOCK_TTL_SECONDS:
                return {"status": "processing", "progress": "AI 正在翻譯中 (請稍候)..."}
            else:
                # Lock 過期，刪除後重新嘗試取得
                print(f"[{file_id}] Lock 已過期 (超過 {LOCK_TTL_SECONDS}s)，重新啟動翻譯...")
                try:
                    lock_blob.delete()
                except Exception:
                    pass
        except Exception:
            # Lock 資料格式錯誤，嘗試刪除並重新取得
            try:
                lock_blob.delete()
            except Exception:
                pass

    # 4. 嘗試原子性建立 Lock（if_generation_match=0 確保物件不存在時才寫入）
    print("啟動後台翻譯任務...")
    try:
        lock_blob.upload_from_string(
            json.dumps({"locked_at": time.time()}),
            if_generation_match=0
        )
    except PreconditionFailed:
        # 其他請求已搶先建立 lock，不重複啟動翻譯
        return {"status": "processing", "progress": "AI 正在翻譯中 (請稍候)..."}
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
        translation_tasks = []
        ordered_batches = [] # 用來存放順序資訊 (chunk_index, batch_index)
        
        current_time_offset = 0.0
        current_srt_index = 1
        BATCH_SIZE = 20  # 小批次翻譯，提高精準度

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
                    task = translate_segment_pro(batch_content, f"{i}_{k}")
                    translation_tasks.append(task)
                    ordered_batches.append((i, k)) # 紀錄順序

        # 3. 分波執行翻譯（每波最多 8 個，波與波之間間隔 2 秒，避免瞬間 burst）
        WAVE_SIZE = 8
        WAVE_DELAY = 2  # 秒
        translated_results = []

        if translation_tasks:
            total_waves = (len(translation_tasks) + WAVE_SIZE - 1) // WAVE_SIZE
            print(f"[{file_id}] 啟動 {len(translation_tasks)} 個翻譯任務，分 {total_waves} 波執行 (每波 {WAVE_SIZE} 個)...")

            for w in range(0, len(translation_tasks), WAVE_SIZE):
                wave = translation_tasks[w:w + WAVE_SIZE]
                wave_num = w // WAVE_SIZE + 1
                print(f"[{file_id}] 第 {wave_num}/{total_waves} 波 ({len(wave)} 個任務)...")
                wave_results = await asyncio.gather(*wave)
                translated_results.extend(wave_results)

                # 波與波之間延遲，最後一波不用等
                if w + WAVE_SIZE < len(translation_tasks):
                    await asyncio.sleep(WAVE_DELAY)
            
            # 4. 組合結果
            full_translated_text = ""
            for res in translated_results:
                full_translated_text += res + "\n\n"
        else:
            full_translated_text = ""

        # 存檔 (SRT)
        final_blob = bucket.blob(f"final_results/{file_id}_TW_Complete.txt")
        final_blob.upload_from_string(full_translated_text)

        # 解析 SRT 產生純文字版本（去除序號與時間戳）
        plain_lines = []
        for line in full_translated_text.splitlines():
            stripped = line.strip()
            if not stripped:
                if plain_lines and plain_lines[-1] != '':
                    plain_lines.append('')
                continue
            if stripped.isdigit():
                continue
            if '-->' in stripped:
                continue
            plain_lines.append(stripped)

        plain_text = '\n'.join(plain_lines).strip() + '\n'
        plain_blob = bucket.blob(f"final_results/{file_id}_TW_PlainText.txt")
        plain_blob.upload_from_string(plain_text)

        print(f"[{file_id}] 翻譯完成並存檔！")

    except Exception as e:
        print(f"[{file_id}] 後台翻譯失敗: {e}")
    finally:
        # 解鎖
        try:
            bucket.blob(f"locks/{file_id}").delete()
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)