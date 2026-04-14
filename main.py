import os
import re
import json
import time
import random
import asyncio
import opencc
from urllib.parse import urljoin
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, Form, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from google.cloud import storage
from google.api_core.exceptions import PreconditionFailed
import google.generativeai as genai
import google.ai.generativelanguage as glm
from google.api_core import client_options as client_options_lib
import httpx
from bs4 import BeautifulSoup

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
cc = opencc.OpenCC('s2twp')

async def translate_segment_pro(srt_content, index, diarize=False, known_names=""):
    """使用 Gemini Pro 翻譯單一區塊 (SRT)，含重試與 Key 輪替"""

    # 預處理：在送給 Gemini 之前，先強制用 OpenCC 將所有的簡體字與慣用語轉為台灣繁體
    # 這樣一來，Gemini 收到的文本就已經是繁體了，它的任務單純變成「潤飾」與「除錯」。
    # 就算 API 徹底失敗而退回原文，出來的也會是繁體字！
    preprocessed_srt = cc.convert(srt_content)

    diarize_rules = ""
    if diarize:
        known_names_rule = ""
        if known_names:
            known_names_rule = f"""
9. KNOWN SPEAKERS: The user has provided the following known person names: {known_names}
   - Use these names to replace `[講者 N]` labels when you can identify the speaker from context, speech content, or how they are addressed.
   - Format: replace `[講者 1]: text` with `[王小明]: text` if you can determine who is speaking.
   - If you cannot confidently identify a speaker, keep the original `[講者 N]` label.
   - Do NOT invent names that are not in the provided list.
"""
            diarize_rules = f"""
8. SPEAKER LABELS: The subtitle lines may contain speaker labels like `[講者 1]: text`.
   - Keep the bracket format for speaker labels.
   - If a line does NOT have a speaker label, do NOT add one.
{known_names_rule}"""
        else:
            diarize_rules = """
8. SPEAKER LABELS: The subtitle lines may contain speaker labels like `[講者 1]: text`.
   - KEEP these speaker labels EXACTLY as they are. Do NOT modify, remove, renumber, or replace them with names.
   - Do NOT guess or infer speaker names. Always keep `[講者 N]` format unchanged.
   - If a line does NOT have a speaker label, do NOT add one.
"""

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
8. PRESERVE PROPER NOUNS: Do NOT change or "correct" names of people, places, organizations, or titles based on your own knowledge. The transcript reflects what was actually spoken — keep it faithful to the original even if it contradicts your training data. Your knowledge may be outdated.{diarize_rules}

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
    mode: str = Form("speech"),
    diarize: bool = Form(False),
    known_names: str = Form("")
):
    validate_file_id(file_id)

    try:
        bucket = storage_client.bucket(BUCKET_NAME)

        # 如果是第一塊，順便儲存 metadata
        if chunk_index == 0:
            meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
            meta_data = {"mode": mode, "diarize": diarize, "known_names": known_names}
            meta_blob.upload_from_string(json.dumps(meta_data))

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
        
        # 讀取 metadata 取得 diarize 設定
        diarize = False
        known_names = ""
        try:
            meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
            if meta_blob.exists():
                meta_data = json.loads(meta_blob.download_as_text())
                diarize = meta_data.get("diarize", False)
                known_names = meta_data.get("known_names", "")
                print(f"[{file_id}] Diarize 設定: {diarize}, 已知人名: {known_names or '(無)'}")
        except Exception as e:
            print(f"[{file_id}] 讀取 metadata 失敗: {e}")

        current_time_offset = 0.0
        current_srt_index = 1
        BATCH_SIZE = 20  # 小批次翻譯，提高精準度

        # 講者編號對照表：將 pyannote 的 SPEAKER_XX 統一映射為遞增編號
        speaker_map = {}
        speaker_counter = 1

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

                # 若有講者標籤，加上 [講者 N] 前綴
                speaker = seg.get("speaker")
                if speaker and diarize:
                    if speaker not in speaker_map:
                        speaker_map[speaker] = f"講者 {speaker_counter}"
                        speaker_counter += 1
                    text = f"[{speaker_map[speaker]}]: {text}"

                chunk_srt_lines.append(f"{current_srt_index}\n{start} --> {end}\n{text}")
                current_srt_index += 1

            current_time_offset += chunk_duration
            
            # 分批建立 Task
            if chunk_srt_lines:
                for k in range(0, len(chunk_srt_lines), BATCH_SIZE):
                    batch_lines = chunk_srt_lines[k:k+BATCH_SIZE]
                    batch_content = "\n\n".join(batch_lines)
                    
                    # 建立 Async Task
                    task = translate_segment_pro(batch_content, f"{i}_{k}", diarize=diarize, known_names=known_names)
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

# --- 直播錄製功能 ---

# 活躍錄製 session（記憶體內）
active_recordings: dict = {}

# URL 白名單驗證（防止 SSRF）
ALLOWED_STREAM_DOMAINS = [
    "live.tcc.gov.tw",
]

def validate_stream_url(url: str) -> str:
    """驗證串流 URL，防止 SSRF 攻擊"""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="僅支援 http/https URL")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="無效的 URL")
    # 允許白名單內的網域，或 m3u8/直接串流 URL
    is_whitelisted = any(parsed.hostname.endswith(d) for d in ALLOWED_STREAM_DOMAINS)
    is_direct_stream = any(url.lower().endswith(ext) for ext in ['.m3u8', '.mp4', '.flv', '.ts'])
    if not is_whitelisted and not is_direct_stream:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的網域：{parsed.hostname}。請使用白名單內的網域或直接提供 m3u8 URL。"
        )
    return url


# --- iShare Portal API 整合 ---
# 從列表頁 URL 推導 API base URL
# 例：https://live.tcc.gov.tw/iSharePortalWeb/User/VideoList.aspx?category=3
#   → https://live.tcc.gov.tw/iSharePortalWeb/api/

def get_api_base(page_url: str) -> str:
    """從 iShare Portal 頁面 URL 推導 API base URL"""
    from urllib.parse import urlparse
    parsed = urlparse(page_url)
    # 找 /User/ 或 /iSharePortalWeb/ 路徑
    path = parsed.path
    if "/User/" in path:
        base_path = path[:path.index("/User/")] + "/api/"
    elif "/iSharePortalWeb/" in path:
        base_path = path[:path.index("/iSharePortalWeb/") + len("/iSharePortalWeb/")] + "api/"
    else:
        base_path = "/iSharePortalWeb/api/"
    return f"{parsed.scheme}://{parsed.netloc}{base_path}"


async def extract_live_streams(list_url: str) -> list:
    """透過 iShare Portal API 取得正在直播的影片列表"""
    api_base = get_api_base(list_url)
    live_api = f"{api_base}SPW024_VideoLive"
    print(f"[爬取] 呼叫直播 API: {live_api}")

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(live_api, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": list_url,
        })
        resp.raise_for_status()

    data = resp.json()
    print(f"[爬取] API 回傳 {len(data)} 筆直播資料")

    lives = []
    from datetime import datetime, timezone, timedelta
    # 直播時間是台灣時間 (UTC+8)
    tw_tz = timezone(timedelta(hours=8))
    now = datetime.now(tw_tz).replace(tzinfo=None)

    for item in data:
        title = item.get("vdv_title", "") or item.get("vdt_title", "")
        vdvno = item.get("vdv_vdvno", "")
        vdv_url = item.get("vdv_url", "")
        live_date = item.get("LiveDate", "")
        live_btime = item.get("LiveBTime", "")
        live_etime = item.get("LiveETime", "")

        if not vdvno:
            continue

        # 判斷是否正在直播
        status = "unknown"
        try:
            start_dt = datetime.strptime(f"{live_date} {live_btime}", "%Y/%m/%d %H:%M")
            end_dt = datetime.strptime(f"{live_date} {live_etime}", "%Y/%m/%d %H:%M")
            if now < start_dt:
                status = "upcoming"
            elif now > end_dt:
                status = "ended"
            else:
                status = "live"
        except Exception:
            status = "live"  # 無法判斷就當作直播中

        # 建構 VideoData 頁面 URL
        video_page_url = urljoin(list_url, f"VideoData.aspx?vdvno={vdvno}")

        time_str = f"{live_btime}~{live_etime}" if live_btime else ""

        lives.append({
            "title": title,
            "url": video_page_url,
            "vdvno": vdvno,
            "vdv_url": vdv_url,
            "status": status,
            "time": time_str,
        })

    print(f"[爬取] 最終找到 {len(lives)} 個直播項目")
    return lives


async def get_stream_url(video_page_url: str) -> str:
    """從 iShare Portal 影片頁面提取串流 URL"""
    from urllib.parse import urlparse, parse_qs

    # 方法 1: 如果是 iShare VideoData 頁面，呼叫 SPW010 API 取得串流 URL
    if "VideoData.aspx" in video_page_url and "vdvno=" in video_page_url:
        parsed = urlparse(video_page_url)
        vdvno = parse_qs(parsed.query).get("vdvno", [""])[0]
        if vdvno:
            api_base = get_api_base(video_page_url)
            video_api = f"{api_base}SPW010_VideoData?vdv_vdvno={vdvno}"
            print(f"[串流提取] 呼叫 API: {video_api}")
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(video_api, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": video_page_url,
                    })
                    resp.raise_for_status()
                data = resp.json()
                # API 可能回傳 array
                if isinstance(data, list) and data:
                    data = data[0]
                # 優先用 VideoURLList（多畫質）
                url_list = data.get("VideoURLList", [])
                if url_list:
                    stream = url_list[0].get("src", "") if isinstance(url_list[0], dict) else str(url_list[0])
                    if stream:
                        print(f"[串流提取] 從 VideoURLList 取得: {stream}")
                        return stream
                # 其次用 vdv_url
                vdv_url = data.get("vdv_url", "")
                if vdv_url:
                    print(f"[串流提取] 從 vdv_url 取得: {vdv_url}")
                    return vdv_url
            except Exception as e:
                print(f"[串流提取] iShare API 失敗: {e}")

    # 方法 2: 爬網頁找 m3u8
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(video_page_url)
            resp.raise_for_status()
        page_text = resp.text
        m3u8_matches = re.findall(r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)', page_text, re.IGNORECASE)
        if m3u8_matches:
            print(f"[串流提取] 從 HTML 找到 m3u8: {m3u8_matches[0]}")
            return m3u8_matches[0]
    except Exception as e:
        print(f"[串流提取] 網頁爬取失敗: {e}")

    # 方法 3: yt-dlp（適用於 YouTube 等平台）
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--get-url", "--no-warnings", "--no-check-certificates",
            video_page_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0 and stdout.strip():
            return stdout.decode().strip().split('\n')[0]
    except Exception as e:
        print(f"[串流提取] yt-dlp 失敗: {e}")

    raise HTTPException(
        status_code=400,
        detail="無法自動提取串流 URL。請從瀏覽器 F12 → Network 找到 .m3u8 網址，使用「直接錄製」。"
    )


class FindStreamsRequest(BaseModel):
    list_url: str

class StartRecordingRequest(BaseModel):
    stream_url: str
    title: str = ""
    mode: str = "speech"
    diarize: bool = False
    known_names: str = ""


@app.post("/find_live_streams")
async def find_live_streams(req: FindStreamsRequest):
    """搜尋列表頁中正在直播的影片"""
    validate_stream_url(req.list_url)
    try:
        streams = await extract_live_streams(req.list_url)
        hint = ""
        if not streams:
            hint = "未找到直播。若確定有直播中，請直接貼上影片頁面 URL（含 vdvno 參數）或 m3u8 串流網址，使用「直接錄製」。"
        return {"streams": streams, "hint": hint}
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"無法存取列表頁: {e}")


@app.post("/start_recording")
async def start_recording(req: StartRecordingRequest, background_tasks: BackgroundTasks):
    """開始錄製串流（每 30 分鐘切一段自動處理）"""
    stream_url = req.stream_url

    # 判斷 URL 類型：如果是影片頁面（非直接串流），先用 yt-dlp 提取
    is_direct_stream = any(stream_url.lower().endswith(ext) for ext in ['.m3u8', '.mp4', '.flv', '.ts'])
    if not is_direct_stream and 'VideoData.aspx' in stream_url:
        validate_stream_url(stream_url)
        stream_url = await get_stream_url(stream_url)
    elif not is_direct_stream:
        # 嘗試用 yt-dlp 提取
        validate_stream_url(stream_url)
        stream_url = await get_stream_url(stream_url)

    # 驗證 ffmpeg 能否連上串流
    probe_proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-headers", f"Referer: {stream_url}\r\n",
        "-i", stream_url, "-t", "2", "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    _, probe_stderr = await asyncio.wait_for(probe_proc.communicate(), timeout=20)
    probe_output = probe_stderr.decode(errors='replace')
    print(f"[串流探測] ffmpeg probe 輸出 (末 300 字): {probe_output[-300:]}")
    if "Server returned" in probe_output and "404" in probe_output:
        raise HTTPException(status_code=400, detail="串流 URL 無效 (404)")
    if "Connection refused" in probe_output:
        raise HTTPException(status_code=400, detail="無法連線到串流伺服器")
    if "Invalid data found" in probe_output and "Output" not in probe_output:
        raise HTTPException(status_code=400, detail=f"串流格式無法辨識: {probe_output[-200:]}")

    suffix = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=6))
    session_id = f"rec_{int(time.time())}_{suffix}"

    active_recordings[session_id] = {
        "status": "recording",
        "stream_url": stream_url,
        "title": req.title or "直播錄製",
        "mode": req.mode,
        "diarize": req.diarize,
        "known_names": req.known_names,
        "segments": [],
        "stop": False,
        "started_at": time.time(),
        "error": None,
    }

    background_tasks.add_task(
        recording_loop, session_id, stream_url,
        req.mode, req.diarize, req.known_names
    )

    return {
        "status": "recording_started",
        "session_id": session_id,
        "stream_url": stream_url,
    }


@app.post("/stop_recording/{session_id}")
async def stop_recording(session_id: str):
    """停止錄製"""
    rec = active_recordings.get(session_id)
    if not rec:
        raise HTTPException(status_code=404, detail="錄製 session 不存在")
    if rec["status"] != "recording":
        return {"status": rec["status"], "message": "錄製已結束"}

    rec["stop"] = True
    # 嘗試終止正在執行的 ffmpeg
    ffmpeg_proc = rec.get("_ffmpeg_proc")
    if ffmpeg_proc and ffmpeg_proc.returncode is None:
        try:
            ffmpeg_proc.terminate()
        except Exception:
            pass

    return {"status": "stopping", "message": "正在停止，等待當前片段完成..."}


@app.get("/recording_status/{session_id}")
async def recording_status(session_id: str):
    """查詢錄製狀態"""
    rec = active_recordings.get(session_id)
    if not rec:
        raise HTTPException(status_code=404, detail="錄製 session 不存在")

    elapsed = time.time() - rec["started_at"]
    return {
        "status": rec["status"],
        "title": rec["title"],
        "elapsed_seconds": int(elapsed),
        "segments": rec["segments"],
        "error": rec.get("error"),
    }


SEGMENT_DURATION = 1800  # 30 分鐘

async def recording_loop(session_id, stream_url, mode, diarize, known_names):
    """背景任務：持續錄製串流，每 30 分鐘切一段上傳"""
    rec = active_recordings[session_id]
    segment_num = 0
    bucket = storage_client.bucket(BUCKET_NAME)

    try:
        while not rec["stop"]:
            file_id = f"{session_id}_seg{segment_num}"
            local_path = f"/tmp/{file_id}.wav"

            print(f"[錄製] {session_id} 開始錄製第 {segment_num} 段...")

            # ffmpeg 錄製 30 分鐘（-vn 去影片，轉 16kHz mono WAV 給 Whisper）
            # 加上 headers 以支援需要 Referer 的串流平台
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-headers", f"Referer: {stream_url}\r\n",
                "-i", stream_url,
                "-t", str(SEGMENT_DURATION),
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            rec["_ffmpeg_proc"] = proc

            try:
                _, stderr_data = await proc.communicate()
                ffmpeg_log = stderr_data.decode(errors='replace')[-500:]
                print(f"[錄製] {session_id} ffmpeg 輸出 (末 500 字): {ffmpeg_log}")
            except Exception as e:
                print(f"[錄製] {session_id} ffmpeg 異常: {e}")
                break

            # 檢查是否產生了有效的音檔（至少 10KB，避免空檔）
            file_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if file_size < 10240:
                print(f"[錄製] {session_id} 第 {segment_num} 段錄製失敗或檔案過小 ({file_size} bytes)")
                if os.path.exists(local_path):
                    os.remove(local_path)
                rec["error"] = f"串流已結束或錄製失敗（檔案 {file_size} bytes）"
                break

            print(f"[錄製] {session_id} 第 {segment_num} 段錄製完成，上傳至 GCS...")

            # 上傳 metadata
            loop = asyncio.get_running_loop()
            meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
            meta_content = json.dumps({"mode": mode, "diarize": diarize, "known_names": known_names})
            await loop.run_in_executor(None, meta_blob.upload_from_string, meta_content)

            # 上傳音檔（作為 chunk 0）
            audio_blob = bucket.blob(f"raw_audio/{file_id}/0")
            await loop.run_in_executor(None, audio_blob.upload_from_filename, local_path)

            # 清理暫存檔
            os.remove(local_path)

            # 記錄片段
            rec["segments"].append({
                "file_id": file_id,
                "total_chunks": 1,
                "segment_num": segment_num,
                "status": "uploaded",
            })
            print(f"[錄製] {session_id} 第 {segment_num} 段已上傳 (file_id={file_id})")

            segment_num += 1

            # 如果 ffmpeg 被 terminate 了（用戶按停止），不繼續
            if rec["stop"]:
                break

    except Exception as e:
        print(f"[錄製] {session_id} 錄製迴圈錯誤: {e}")
        rec["error"] = str(e)
    finally:
        rec["status"] = "stopped"
        rec.pop("_ffmpeg_proc", None)
        print(f"[錄製] {session_id} 錄製結束，共 {len(rec['segments'])} 段")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)