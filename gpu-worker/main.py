import os
import json
import traceback
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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

# --- pyannote 講者辨識模型 ---
diarization_pipeline = None
hf_token = os.environ.get("HF_TOKEN", "")
if hf_token:
    print("正在載入 pyannote speaker-diarization 模型...")
    try:
        import torch
        from pyannote.audio import Pipeline
        diarization_pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=hf_token
        )
        diarization_pipeline.to(torch.device("cuda"))
        print("pyannote 模型載入完成！")
    except Exception as e:
        print(f"pyannote 模型載入失敗 (講者辨識將不可用): {e}")
else:
    print("HF_TOKEN 未設定，講者辨識功能不可用。")


def run_diarization(audio_path):
    """執行 pyannote 講者辨識，回傳 [(start, end, speaker), ...] 列表"""
    if diarization_pipeline is None:
        return []
    output = diarization_pipeline(audio_path)
    results = []
    # pyannote.audio 4.x: 使用 exclusive_speaker_diarization（每個時間點只有一位講者）
    if hasattr(output, 'speaker_diarization'):
        for turn, speaker in output.speaker_diarization:
            results.append((turn.start, turn.end, speaker))
    # pyannote.audio 3.x fallback
    elif hasattr(output, 'itertracks'):
        for turn, _, speaker in output.itertracks(yield_label=True):
            results.append((turn.start, turn.end, speaker))
    else:
        print(f"警告: 無法解析 diarization 輸出類型: {type(output).__name__}")
    return results


def assign_speakers_to_segments(segments, diarization_results):
    """
    將 pyannote 的講者標籤分配到 whisper segments。
    比對方式：找出與 segment 時間重疊最多的 diarization turn。
    """
    if not diarization_results:
        return segments

    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        best_speaker = None
        best_overlap = 0.0

        for turn_start, turn_end, speaker in diarization_results:
            # 計算重疊時間
            overlap_start = max(seg_start, turn_start)
            overlap_end = min(seg_end, turn_end)
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = speaker

        if best_speaker is not None:
            seg["speaker"] = best_speaker

    return segments

storage_client = storage.Client()

@app.get("/health")
async def health_check():
    return {"status": "ok", "model_loaded": model is not None}

@app.post("/")
async def handle_event(request: Request):
    """
    Eventarc 觸發入口：
    當 GCS 有新檔案寫入時，GCP 會將事件資料 POST 到這裡。
    """
    print("收到觸發請求...")

    if model is None:
        print("錯誤：Whisper 模型未載入，無法處理請求")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": "Whisper model not loaded. GPU environment may be unavailable."}
        )

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

        # 跳過 metadata.json，它不是音訊檔案
        if file_name.endswith("metadata.json"):
            print(f"跳過 metadata 檔案: {file_name}")
            return {"status": "skipped"}

        print(f"開始處理檔案: gs://{bucket_name}/{file_name}")

        # 解析 file_id 與 chunk_index
        path_parts = file_name.split('/')
        file_id = path_parts[-2] if len(path_parts) >= 3 else "unknown"
        chunk_index = path_parts[-1] if len(path_parts) >= 3 else os.path.basename(file_name)

        # 2. 下載檔案到暫存區 (/tmp)，用 file_id + chunk_index 確保路徑唯一
        local_input_path = f"/tmp/{file_id}_{chunk_index}"
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(file_name)
        blob.download_to_filename(local_input_path)

        # 預設參數 (Speech)
        use_vad = True
        temp = 0.2
        prompt_text = "以下是繁體中文的字幕。"
        enable_diarize = False

        # 讀取 Metadata 調整參數
        try:
            meta_blob = bucket.blob(f"raw_audio/{file_id}/metadata.json")
            if meta_blob.exists():
                meta = json.loads(meta_blob.download_as_text())
                if meta.get("mode") == "song":
                    print("🎵 模式偵測: 歌曲 (VAD=False, Temp=0)")
                    use_vad = False
                    temp = 0
                    prompt_text = "以下是繁體中文的歌詞。"
                enable_diarize = meta.get("diarize", False)
        except Exception as e:
            print(f"Metadata 讀取失敗 (使用預設值): {e}")

        # 3. 執行 GPU 轉錄
        # beam_size=1 最快；beam_size=5 較準。這裡用 1 追求極速
        # language="zh": 強制中文，避免亂跳語言
        # condition_on_previous_text=False: 避免重複上一句 (鬼打牆)
        # word_timestamps=True: 提高時間軸精準度
        segments, info = model.transcribe(
            local_input_path,
            beam_size=5,
            vad_filter=use_vad,
            vad_parameters=dict(min_silence_duration_ms=500),
            initial_prompt=prompt_text,
            language="zh",
            condition_on_previous_text=False,
            word_timestamps=True,
            temperature=temp
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

        # 4.5 講者辨識 (pyannote)
        if enable_diarize and diarization_pipeline is not None:
            print(f"執行講者辨識 (pyannote)...")
            try:
                diarization_results = run_diarization(local_input_path)
                segment_list = assign_speakers_to_segments(segment_list, diarization_results)
                print(f"講者辨識完成，共偵測到 {len(set(r[2] for r in diarization_results))} 位講者")
            except Exception as e:
                print(f"講者辨識失敗 (繼續不帶講者標籤): {e}")
        elif enable_diarize:
            print("講者辨識已啟用但 pyannote 模型未載入 (HF_TOKEN 未設定?)")

        # 5. 上傳結果到 GCS (transcripts 資料夾)
        # 檔名轉換: raw_audio/xyz/1 -> transcripts/xyz_part_1.json
        # file_id 和 chunk_index 已在上方解析
        if len(path_parts) >= 3:
            result_blob_name = f"transcripts/{file_id}_part_{chunk_index}.json"
        else:
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