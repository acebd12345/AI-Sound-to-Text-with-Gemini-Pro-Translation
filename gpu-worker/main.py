import os
import json
import traceback
from fastapi import FastAPI, Request
from faster_whisper import WhisperModel
from google.cloud import storage

app = FastAPI()

# --- 全域模型載入 (Cold Start 時執行一次) ---
# 使用 large-v3-turbo 模型，速度快且精度高
# device="cuda" 是關鍵，沒有 GPU 這行會報錯
print("正在載入 Whisper 模型 (Large-v3-turbo)...")
try:
    # 從映像檔中預先下載的目錄載入模型
    model_path = "model"
    print(f"從 {model_path} 載入模型...")
    model = WhisperModel(model_path, device="cuda", compute_type="float16")
    print("模型載入完成！")
except Exception as e:
    print(f"模型載入失敗 (請檢查是否有 GPU 環境): {e}")
    model = None

storage_client = storage.Client()

@app.post("/")
async def handle_event(request: Request):
    """
    Eventarc 觸發入口：
    當 GCS 有新檔案寫入時，GCP 會將事件資料 POST 到這裡。
    """
    print("收到觸發請求...")
    
    # 1. 解析 CloudEvent 資料
    event = await request.json()
    
    # 兼容性處理：Eventarc 的資料結構有時在 body，有時在 message.data
    bucket_name = None
    file_name = None

    try:
        if 'bucket' in event:
            bucket_name = event['bucket']
            file_name = event['name']
        elif 'message' in event and 'data' in event['message']:
            # Pub/Sub 格式需要 base64 解碼 (很少見，但以防萬一)
            import base64
            decoded = base64.b64decode(event['message']['data']).decode('utf-8')
            data_json = json.loads(decoded)
            bucket_name = data_json['bucket']
            file_name = data_json['name']
        
        if not bucket_name or not file_name:
            print("非 GCS 事件或解析失敗，略過。")
            return {"status": "ignored"}

        # 防止無限迴圈的重要過濾
        # 我們只處理 'raw_audio/' 資料夾內的檔案
        # 如果是 'transcripts/' 或其他資料夾，直接忽略
        if "raw_audio/" not in file_name:
            print(f"跳過非 raw_audio 檔案: {file_name}")
            return {"status": "skipped"}

        print(f"開始處理檔案: gs://{bucket_name}/{file_name}")

        # 2. 下載檔案到暫存區 (/tmp)
        local_input_path = f"/tmp/{os.path.basename(file_name)}"
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        blob.download_to_filename(local_input_path)

        # 3. 執行 GPU 轉錄
        # beam_size=1 最快；beam_size=5 較準。這裡用 1 追求極速
        # 加入 VAD 過濾靜音區段，避免幻覺 (重複輸出)
        segments, info = model.transcribe(
            local_input_path,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            initial_prompt="以下是繁體中文的字幕。"
        )

        # 4. 整理結果
        full_text = ""
        segment_list = []
        for segment in segments:
            full_text += segment.text
            segment_list.append({
                "start": segment.start,
                "end": segment.end,
                "text": segment.text
            })

        # 5. 上傳結果到 GCS (transcripts 資料夾)
        # 檔名轉換: raw_audio/xyz/1 -> transcripts/xyz_part_1.json
        # 這裡做一個簡單的檔名處理，確保對應得到
        # 假設 file_name 是 "raw_audio/my_meeting_id/3" (第4個切片)
        path_parts = file_name.split('/')
        if len(path_parts) >= 3:
            file_id = path_parts[-2] # my_meeting_id
            chunk_index = path_parts[-1] # 3
            result_blob_name = f"transcripts/{file_id}_part_{chunk_index}.json"
        else:
            # Fallback 命名
            safe_name = file_name.replace('/', '_')
            result_blob_name = f"transcripts/{safe_name}.json"

        result_data = {
            "text": full_text,
            "segments": segment_list,
            "duration": info.duration
        }

        output_blob = bucket.blob(result_blob_name)
        output_blob.upload_from_string(
            json.dumps(result_data, ensure_ascii=False),
            content_type="application/json"
        )
        
        print(f"轉錄成功！已儲存至: {result_blob_name}")

        # 清理
        os.remove(local_input_path)
        return {"status": "success", "output": result_blob_name}

    except Exception as e:
        error_msg = traceback.format_exc()
        print(f"處理發生錯誤: {error_msg}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)