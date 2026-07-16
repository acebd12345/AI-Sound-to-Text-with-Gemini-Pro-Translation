"""tests/test_rescue.py — YouTube 直播補救流程測試（全 mock，不碰網路/GCS/真 yt-dlp）

用法：python3 tests/test_rescue.py
與 scratchpad_smoke_test.py 同風格（標準庫 + 自製 check()，本專案未裝 pytest）。

涵蓋：
  ⓪ VDVNO_PATTERN 共用驗證規格
  ① /start_recording 的 vdvno 參數（secret 強制、marker 防重複、失敗必釋放）
     ＋ claim_live_with_stale_recovery 重構後陳舊 marker 回收情境重跑
"""
import asyncio
import json
import os
import sys
import types
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# --- 在 import main 之前，注入環境變數並 mock 外部依賴 ---
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("BUCKET_NAME", "test-bucket")

_fake_opencc = types.ModuleType("opencc")
_fake_opencc.OpenCC = lambda *a, **k: types.SimpleNamespace(convert=lambda s: s)
sys.modules["opencc"] = _fake_opencc

import google.cloud.storage as gcs  # noqa: E402
from google.api_core.exceptions import NotFound, PreconditionFailed  # noqa: E402


class _FakeBucket:
    """記憶體版 GCS bucket，支援 generation（條件寫入/刪除）語意。"""

    def __init__(self):
        self.blobs = {}   # name -> (data, generation)
        self._gen = 0

    def blob(self, name):
        return _FakeBlob(self, name)

    def get_blob(self, name):
        if name not in self.blobs:
            return None
        b = _FakeBlob(self, name)
        b.generation = self.blobs[name][1]
        return b

    def reset(self):
        self.blobs.clear()


class _FakeBlob:
    def __init__(self, bucket, name):
        self.bucket = bucket
        self.name = name
        self.generation = None

    def upload_from_string(self, data, if_generation_match=None):
        if if_generation_match == 0 and self.name in self.bucket.blobs:
            raise PreconditionFailed("already exists")
        self.bucket._gen += 1
        self.bucket.blobs[self.name] = (data, self.bucket._gen)

    def upload_from_filename(self, path):
        self.upload_from_string(f"<file:{path}>")

    def download_as_text(self):
        if self.name not in self.bucket.blobs:
            raise NotFound(self.name)
        return self.bucket.blobs[self.name][0]

    def delete(self, if_generation_match=None):
        if self.name not in self.bucket.blobs:
            raise NotFound(self.name)
        if if_generation_match is not None and self.bucket.blobs[self.name][1] != if_generation_match:
            raise PreconditionFailed("generation mismatch")
        self.bucket.blobs.pop(self.name)


class _FakeStorageClient:
    def __init__(self, *a, **k):
        self._bucket = _FakeBucket()

    def bucket(self, name):
        return self._bucket


gcs.Client = _FakeStorageClient

from google import genai  # noqa: E402
genai.Client = lambda *a, **k: types.SimpleNamespace(aio=None)

import main  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

BUCKET = main.storage_client.bucket("test-bucket")

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


def section(title):
    print(f"\n[{title}]")


class _CIHeaders(dict):
    """大小寫不敏感的 header 容器（比照 starlette 的 request.headers）。"""

    def __init__(self, data=None):
        super().__init__({k.lower(): v for k, v in (data or {}).items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeReq:
    def __init__(self, headers=None):
        self.headers = _CIHeaders(headers)


SECRET = "s3cr3t"
VDVNO = "vdv12345678"
HLS_URL = "https://r1---sn-abc.googlevideo.com/videoplayback/manifest/hls_playlist/x.m3u8"


def _secret_req():
    return _FakeReq({"X-Trigger-Secret": SECRET})


def _fake_probe(output: bytes = b"Stream #0:0: Audio: aac\nOutput #0"):
    """把 ffmpeg 探測換成回傳指定 stderr 的假 process。"""
    class _P:
        async def communicate(self):
            return (b"", output)

    async def _exec(*a, **k):
        return _P()
    return _exec


def _live_marker(vdvno=VDVNO):
    return json.loads(BUCKET.blobs[f"auto_state/live/{vdvno}"][0])


def _reset():
    BUCKET.reset()
    main.active_recordings.clear()
    os.environ["AUTO_TRIGGER_SECRET"] = SECRET


async def _start(req, background=None, **kw):
    """呼叫 /start_recording，kw 直接進 StartRecordingRequest。"""
    body = {"stream_url": HLS_URL, "title": "測試場次"}
    body.update(kw)
    return await main.start_recording(
        main.StartRecordingRequest(**body), background or BackgroundTasks(), req
    )


# ============ ⓪ VDVNO_PATTERN 共用驗證規格 ============

def test_vdvno_pattern():
    section("⓪ VDVNO_PATTERN")
    m = main.VDVNO_PATTERN.match
    check("接受純數字 8 碼", bool(m("12345678")))
    check("接受英數混合", bool(m("vdv12345678")))
    check("接受含 hyphen", bool(m("abc-1234-5678")))
    check("接受 64 碼上限", bool(m("a" * 64)))
    check("拒絕 7 碼（過短）", not m("1234567"))
    check("拒絕 65 碼（過長）", not m("a" * 65))
    check("拒絕空字串", not m(""))
    check("拒絕底線（比 FILE_ID_PATTERN 收斂）", not m("vdv_12345678"))
    check("拒絕點號", not m("vdv.12345678"))
    check("拒絕路徑穿越 ..", not m("../../etc/passwd"))
    check("拒絕斜線", not m("abc/12345678"))
    check("FILE_ID_PATTERN 會放行的 vdvno 被 VDVNO_PATTERN 擋下（收緊）",
          bool(main.FILE_ID_PATTERN.match("vdv_1.2")) and not m("vdv_1.2"))


async def test_auto_status_uses_vdvno_pattern():
    section("⓪ /auto_status 改用 VDVNO_PATTERN")
    _reset()
    try:
        await main.auto_status("vdv_12345678", _secret_req())
        check("含底線的 vdvno → 400", False)
    except HTTPException as e:
        check("含底線的 vdvno → 400", e.status_code == 400)

    try:
        await main.auto_status("short", _secret_req())
        check("過短 vdvno → 400", False)
    except HTTPException as e:
        check("過短 vdvno → 400", e.status_code == 400)

    # 合法 vdvno 但查無資料 → 404（代表通過格式檢查）
    try:
        await main.auto_status(VDVNO, _secret_req())
        check("合法 vdvno 無資料 → 404", False)
    except HTTPException as e:
        check("合法 vdvno 無資料 → 404", e.status_code == 404)

    # 驗證仍在（secret 錯 → 403）
    try:
        await main.auto_status(VDVNO, _FakeReq({"X-Trigger-Secret": "wrong"}))
        check("錯 secret → 403", False)
    except HTTPException as e:
        check("錯 secret → 403", e.status_code == 403)


# ============ ① /start_recording 的 vdvno 參數 ============

async def test_vdvno_requires_secret():
    section("① 帶 vdvno 必須有 X-Trigger-Secret")
    _reset()
    orig_origins = main.ALLOWED_ORIGINS
    main.ALLOWED_ORIGINS = ["https://front.example"]
    try:
        # 僅同源（前端網頁）→ 403，且不可搶到 marker
        req = _FakeReq({"Origin": "https://front.example"})
        try:
            await _start(req, vdvno=VDVNO)
            check("同源但無 secret + vdvno → 403", False)
        except HTTPException as e:
            check("同源但無 secret + vdvno → 403", e.status_code == 403)
        check("403 時未建立 live marker", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

        # 對照組：同源、不帶 vdvno → 維持原行為（可錄）
        orig_exec = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = _fake_probe()
        try:
            r = await _start(_FakeReq({"Origin": "https://front.example"}))
            check("同源不帶 vdvno → 仍可開錄（行為不變）", r["status"] == "recording_started")
        finally:
            asyncio.create_subprocess_exec = orig_exec
    finally:
        main.ALLOWED_ORIGINS = orig_origins


async def test_vdvno_success_path():
    section("① 帶 vdvno + secret 成功開錄")
    _reset()
    # 預埋一筆舊的失敗記錄，驗證成功開錄後被清除
    main._record_live_failure(BUCKET, VDVNO, "YouTube 直播無法後端錄製", source_type="youtube")
    check("前置：live_failure 已存在", f"auto_state/live_failures/{VDVNO}" in BUCKET.blobs)

    orig_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = _fake_probe()
    try:
        bg = BackgroundTasks()
        r = await _start(_secret_req(), bg, vdvno=VDVNO)
    finally:
        asyncio.create_subprocess_exec = orig_exec

    check("回傳 recording_started", r["status"] == "recording_started")
    sid = r["session_id"]
    check("已建立 live marker", f"auto_state/live/{VDVNO}" in BUCKET.blobs)
    marker = _live_marker()
    check("marker.session_id 指向本 session", marker.get("session_id") == sid)
    check("marker.status = recording", marker.get("status") == "recording")
    check("session 帶 auto_vdvno", main.active_recordings[sid].get("auto_vdvno") == VDVNO)
    check("live_failure 已清除", f"auto_state/live_failures/{VDVNO}" not in BUCKET.blobs)
    check("背景任務已排入 recording_loop",
          any(t.func is main.recording_loop for t in bg.tasks))


async def test_duplicate_vdvno_409():
    section("① 重複 vdvno → 409")
    _reset()
    orig_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = _fake_probe()
    try:
        r1 = await _start(_secret_req(), vdvno=VDVNO)
        check("第一次成功", r1["status"] == "recording_started")
        try:
            await _start(_secret_req(), vdvno=VDVNO)
            check("第二次 → 409", False)
        except HTTPException as e:
            check("第二次 → 409", e.status_code == 409)
            check("409 訊息正確", "已有錄製進行中" in e.detail)
        check("marker 仍屬第一個 session", _live_marker().get("session_id") == r1["session_id"])
    finally:
        asyncio.create_subprocess_exec = orig_exec


async def test_failure_paths_release_marker():
    section("① claim 後每個失敗出口都要釋放 marker")

    # (a) probe 回 404 → 400
    _reset()
    orig_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = _fake_probe(b"Server returned 404 Not Found")
    try:
        await _start(_secret_req(), vdvno=VDVNO)
        check("probe 404 → 400", False)
    except HTTPException as e:
        check("probe 404 → 400", e.status_code == 400)
    finally:
        asyncio.create_subprocess_exec = orig_exec
    check("probe 404 後 marker 已釋放", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

    # (b) probe 連線被拒 → 400
    _reset()
    asyncio.create_subprocess_exec = _fake_probe(b"Connection refused")
    try:
        await _start(_secret_req(), vdvno=VDVNO)
        check("probe 連線被拒 → 400", False)
    except HTTPException as e:
        check("probe 連線被拒 → 400", e.status_code == 400)
    finally:
        asyncio.create_subprocess_exec = orig_exec
    check("probe 連線被拒後 marker 已釋放", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

    # (c) VOD 網址 → 400
    _reset()
    try:
        await _start(_secret_req(), vdvno=VDVNO,
                     stream_url="https://tccstr2.tcc.gov.tw/tccvod/smil:x.smil/playlist.m3u8")
        check("VOD 網址 → 400", False)
    except HTTPException as e:
        check("VOD 網址 → 400", e.status_code == 400 and "VOD" in e.detail)
    check("VOD 網址被拒後 marker 已釋放", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

    # (d) 非直接串流 → get_stream_url 失敗（StreamExtractError）→ 400
    _reset()
    orig_get = main.get_stream_url

    async def _boom(url):
        raise main.StreamExtractError("YouTube 直播無法後端錄製",
                                      source_type="youtube", source_url=url)
    main.get_stream_url = _boom
    try:
        await _start(_secret_req(), vdvno=VDVNO,
                     stream_url="https://live.tcc.gov.tw/iSharePortalWeb/User/VideoData.aspx?vdvno=x")
        check("get_stream_url 失敗 → 400", False)
    except HTTPException as e:
        check("get_stream_url 失敗 → 400", e.status_code == 400)
    finally:
        main.get_stream_url = orig_get
    check("get_stream_url 失敗後 marker 已釋放", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

    # (e) probe 逾時（非 HTTPException 的例外路徑）也要釋放
    _reset()

    async def _hang(*a, **k):
        class _P:
            async def communicate(self):
                await asyncio.sleep(10)
                return (b"", b"")
        return _P()
    asyncio.create_subprocess_exec = _hang
    orig_wait = asyncio.wait_for

    async def _instant_timeout(coro, timeout=None):
        coro.close()
        raise asyncio.TimeoutError()
    asyncio.wait_for = _instant_timeout
    try:
        await _start(_secret_req(), vdvno=VDVNO)
        check("probe 逾時 → 例外往外拋", False)
    except asyncio.TimeoutError:
        check("probe 逾時 → 例外往外拋", True)
    finally:
        asyncio.create_subprocess_exec = orig_exec
        asyncio.wait_for = orig_wait
    check("probe 逾時後 marker 已釋放", f"auto_state/live/{VDVNO}" not in BUCKET.blobs)

    # (f) vdvno 格式錯 → 400，且不留任何 marker
    _reset()
    try:
        await _start(_secret_req(), vdvno="bad_vdvno")
        check("vdvno 格式錯 → 400", False)
    except HTTPException as e:
        check("vdvno 格式錯 → 400", e.status_code == 400)
    check("vdvno 格式錯未留下任何 marker",
          not [k for k in BUCKET.blobs if k.startswith("auto_state/live/")])


async def test_no_vdvno_untouched():
    section("① 不帶 vdvno → 完全不碰 marker")
    _reset()
    orig_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = _fake_probe()
    try:
        r = await _start(_FakeReq(), vdvno="")
    finally:
        asyncio.create_subprocess_exec = orig_exec
    check("不帶 vdvno 仍可開錄", r["status"] == "recording_started")
    check("未建立任何 auto_state", not BUCKET.blobs)
    check("session 無 auto_vdvno", "auto_vdvno" not in main.active_recordings[r["session_id"]])


# ============ ① claim_live_with_stale_recovery（重構） ============

async def test_claim_live_with_stale_recovery():
    section("① claim_live_with_stale_recovery 陳舊回收")

    _reset()
    check("無 marker → 搶到", main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t") is True)
    check("marker 新鮮（無 session）→ 搶不到",
          main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t") is False)

    # session 顯示 stopped → 視為陳舊，回收後 re-claim
    _reset()
    main.claim_auto_state(BUCKET, "live", VDVNO, "t")
    main.update_auto_state(BUCKET, "live", VDVNO, session_id="rec_old", status="recording")
    BUCKET.blob("auto_state/sessions/rec_old").upload_from_string(
        json.dumps({"status": "stopped", "segments": 3}))
    check("session 已 stopped → 回收並重新搶到",
          main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t2") is True)
    check("回收後 marker 為新的（title 已更新）", _live_marker().get("title") == "t2")

    # session 仍在錄 → 不可回收
    _reset()
    main.claim_auto_state(BUCKET, "live", VDVNO, "t")
    main.update_auto_state(BUCKET, "live", VDVNO, session_id="rec_live", status="recording")
    BUCKET.blob("auto_state/sessions/rec_live").upload_from_string(
        json.dumps({"status": "recording", "segments": 1}))
    check("session 仍 recording → 搶不到",
          main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t2") is False)

    # 無 session 檔且 started_at 超過 1 小時 → 陳舊
    _reset()
    BUCKET.blob(f"auto_state/live/{VDVNO}").upload_from_string(
        json.dumps({"started_at": 0, "title": "ancient", "status": "processing"}))
    check("marker 逾時（>1h）且無 session → 回收並重新搶到",
          main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t3") is True)

    # generation 不符（別人剛動過）→ 不 re-claim
    _reset()
    BUCKET.blob(f"auto_state/live/{VDVNO}").upload_from_string(
        json.dumps({"started_at": 0, "title": "ancient", "status": "processing"}))
    orig_del = main._conditional_delete_marker
    main._conditional_delete_marker = lambda *a, **k: False
    try:
        check("條件刪除失敗（generation 不符）→ 搶不到",
              main.claim_live_with_stale_recovery(BUCKET, VDVNO, "t4") is False)
    finally:
        main._conditional_delete_marker = orig_del
    check("搶不到時原 marker 保留", f"auto_state/live/{VDVNO}" in BUCKET.blobs)


async def test_start_recording_recovers_stale_marker():
    section("① /start_recording 亦享有陳舊 marker 回收")
    _reset()
    main.claim_auto_state(BUCKET, "live", VDVNO, "舊場次")
    main.update_auto_state(BUCKET, "live", VDVNO, session_id="rec_dead", status="recording")
    BUCKET.blob("auto_state/sessions/rec_dead").upload_from_string(
        json.dumps({"status": "stopped", "segments": 2, "error": "串流已結束"}))

    orig_exec = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = _fake_probe()
    try:
        r = await _start(_secret_req(), vdvno=VDVNO)
    finally:
        asyncio.create_subprocess_exec = orig_exec
    check("陳舊 marker 被回收，補救得以開錄", r["status"] == "recording_started")
    check("marker 已指向新 session", _live_marker().get("session_id") == r["session_id"])


# ============ 回歸：auto_record_check 陳舊回收情境（重構後重跑） ============

async def test_auto_record_check_stale_recovery_regression():
    section("回歸 /auto_record_check 陳舊 marker 回收（重構後）")
    _reset()

    async def _fake_ishare(endpoint, params=None):
        if endpoint == "SPW003_OnAirList":
            return [{"vdv_vdvno": VDVNO, "vdv_title": "大會直播"}]
        return []

    orig_ishare, orig_get = main._ishare_get, main.get_stream_url
    main._ishare_get = _fake_ishare

    async def _fake_stream(url):
        return HLS_URL
    main.get_stream_url = _fake_stream

    orig_validate = main.validate_stream_url
    main.validate_stream_url = lambda u: u
    try:
        # 預埋陳舊 marker（前一實例重啟、finally 沒跑）
        main.claim_auto_state(BUCKET, "live", VDVNO, "大會直播")
        main.update_auto_state(BUCKET, "live", VDVNO, session_id="rec_dead", status="recording")
        BUCKET.blob("auto_state/sessions/rec_dead").upload_from_string(
            json.dumps({"status": "stopped", "segments": 5}))

        result = await main.auto_record_check(_secret_req(), BackgroundTasks())
        check("陳舊 marker 回收後重新開錄", len(result["live_started"]) == 1)
        check("live_started 帶 vdvno", result["live_started"][0]["vdvno"] == VDVNO)
        check("marker 指向新 session",
              _live_marker().get("session_id") == result["live_started"][0]["session_id"])

        # 新鮮 marker（同一輪再跑）→ 不重複開錄
        result2 = await main.auto_record_check(_secret_req(), BackgroundTasks())
        check("marker 新鮮 → 不重複開錄", len(result2["live_started"]) == 0)
        check("計入 skipped", result2["skipped"] >= 1)
    finally:
        main._ishare_get, main.get_stream_url = orig_ishare, orig_get
        main.validate_stream_url = orig_validate


async def test_auto_record_check_skips_bad_vdvno():
    section("⓪ /auto_record_check 內部改用 VDVNO_PATTERN")
    _reset()

    async def _fake_ishare(endpoint, params=None):
        if endpoint == "SPW003_OnAirList":
            return [{"vdv_vdvno": "bad_vdvno!", "vdv_title": "壞資料"}]
        return []

    orig_ishare = main._ishare_get
    main._ishare_get = _fake_ishare
    try:
        result = await main.auto_record_check(_secret_req(), BackgroundTasks())
        check("不合法 vdvno 不開錄", len(result["live_started"]) == 0)
        check("不合法 vdvno 計入 skipped", result["skipped"] >= 1)
        check("不合法 vdvno 不建立 marker",
              not [k for k in BUCKET.blobs if k.startswith("auto_state/live/")])
    finally:
        main._ishare_get = orig_ishare


# ============ ②' session 落地與 /recording_status 補 file_ids ============

async def test_session_state_file_ids():
    section("②' _write_session_state 落地 file_ids")
    _reset()
    rec = {
        "status": "recording",
        "title": "大會",
        "segments": [
            {"file_id": "rec_1_seg0", "total_chunks": 1, "segment_num": 0, "status": "uploaded"},
            {"file_id": "rec_1_seg1", "total_chunks": 1, "segment_num": 1, "status": "uploaded"},
        ],
        "error": None,
    }
    main._write_session_state(BUCKET, "rec_1", rec)
    landed = json.loads(BUCKET.blobs["auto_state/sessions/rec_1"][0])
    check("落地含 file_ids", landed.get("file_ids") == ["rec_1_seg0", "rec_1_seg1"])
    check("仍保留段數欄位（不破壞既有格式）", landed.get("segments") == 2)
    check("仍保留 status/title/error",
          landed.get("status") == "recording" and landed.get("title") == "大會"
          and "error" in landed and "updated_at" in landed)

    # 尚未有任何段落 → 空清單而非缺欄位
    main._write_session_state(BUCKET, "rec_empty", {"status": "recording", "segments": []})
    check("無段落時 file_ids 為空清單",
          json.loads(BUCKET.blobs["auto_state/sessions/rec_empty"][0]).get("file_ids") == [])


async def test_recording_status_file_ids():
    section("②' /recording_status 兩種來源都回 file_ids")

    # (a) 記憶體來源
    _reset()
    main.active_recordings["rec_mem"] = {
        "status": "recording",
        "title": "大會",
        "started_at": main.time.time(),
        "segments": [
            {"file_id": "rec_mem_seg0", "total_chunks": 1, "segment_num": 0, "status": "uploaded"},
        ],
        "error": None,
    }
    r = await main.recording_status("rec_mem")
    check("記憶體來源回 file_ids", r.get("file_ids") == ["rec_mem_seg0"])
    check("記憶體來源仍保留 segments 明細", len(r.get("segments", [])) == 1)

    # (b) GCS 落地來源（實例重啟情境）
    _reset()
    BUCKET.blob("auto_state/sessions/rec_gcs").upload_from_string(json.dumps({
        "status": "stopped", "title": "大會", "segments": 2,
        "file_ids": ["rec_gcs_seg0", "rec_gcs_seg1"], "error": None,
    }))
    r = await main.recording_status("rec_gcs")
    check("GCS 來源回 file_ids", r.get("file_ids") == ["rec_gcs_seg0", "rec_gcs_seg1"])
    check("GCS 來源標記 note", "restarted" in r.get("note", ""))

    # (c) 舊格式落地（無 file_ids 欄位）→ 補空清單，欄位恆存在
    _reset()
    BUCKET.blob("auto_state/sessions/rec_old").upload_from_string(json.dumps({
        "status": "stopped", "title": "舊格式", "segments": 1, "error": None,
    }))
    r = await main.recording_status("rec_old")
    check("舊格式落地補上空 file_ids", r.get("file_ids") == [])

    # (d) 查無 session → 仍 404
    _reset()
    try:
        await main.recording_status("rec_missing")
        check("查無 session → 404", False)
    except HTTPException as e:
        check("查無 session → 404", e.status_code == 404)


# ============ ② kit rescue（council_ops） ============
# council_ops.py 在 hermes-kit/ 下，import 時會讀該目錄的 .env（唯讀，不改動）。
sys.path.insert(0, str(REPO / "hermes-kit"))
import council_ops as ops  # noqa: E402

YT_URL = "https://www.youtube.com/watch?v=abc123"


class _Args:
    """模擬 argparse 的 namespace。"""

    def __init__(self, **kw):
        self.vdvno = VDVNO
        self.url = None
        self.title = None
        self.follow = False
        self.until = "19:00"
        self.interval = 600
        self.__dict__.update(kw)


class _FakeCompleted:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _patch(obj, name, value):
    """回傳 (還原用 callable)，讓測試結束後復原。"""
    orig = getattr(obj, name)
    setattr(obj, name, value)
    return lambda: setattr(obj, name, orig)


def _run_rescue_once(args, ytdlp_stdout, backend_impl, ytdlp_path="/usr/bin/yt-dlp",
                     portal_impl=None):
    """在全 mock 環境下跑 _rescue_once，回傳 (result, 送出的 subprocess cmd)。"""
    import subprocess
    seen = {}

    def _fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["timeout"] = kw.get("timeout")
        return _FakeCompleted(stdout=ytdlp_stdout)

    def _fake_portal(endpoint, params=None):
        seen["portal"] = (endpoint, params)
        if portal_impl:
            return portal_impl(endpoint, params)
        return [{"vdv_url": YT_URL, "vdv_title": "台北市議會大會"}]

    undo = [
        _patch(subprocess, "run", _fake_run),
        _patch(ops, "portal", _fake_portal),
        _patch(ops, "_find_ytdlp", lambda: ytdlp_path),
        _patch(ops, "backend", backend_impl),
    ]
    try:
        return ops._rescue_once(args), seen
    finally:
        for fn in undo:
            fn()


def test_rescue_happy_path():
    section("② rescue 正常解出並開錄")
    sent = {}

    def _backend(path, method="GET", with_secret=False, timeout=60, json_body=None):
        sent.update(path=path, method=method, with_secret=with_secret, body=json_body)
        return {"status": "recording_started", "session_id": "rec_999_abcdef"}

    # 多行 stdout（yt-dlp 影音分離時常見）：要挑到 HLS 那行，不是第一行
    stdout = (b"https://rr1---sn-x.googlevideo.com/videoplayback?expire=1784200000&other=1\n"
              b"https://rr1---sn-x.googlevideo.com/api/manifest/hls_playlist/expire/1784207000/x/index.m3u8\n")
    r, seen = _run_rescue_once(_Args(), stdout, _backend)

    check("回傳 recording_started", r["status"] == "recording_started")
    check("帶回 session_id", r["session_id"] == "rec_999_abcdef")
    check("帶回 vdvno", r["vdvno"] == VDVNO)
    check("title 取自 vdv_title", r["title"] == "台北市議會大會")
    check("打 SPW010 取 vdv_url", seen["portal"] == ("SPW010_VideoData", {"vdv_vdvno": VDVNO}))
    check("yt-dlp 命令為 -g --no-warnings 且不帶 -f",
          seen["cmd"] == ["/usr/bin/yt-dlp", "-g", "--no-warnings", YT_URL])
    check("yt-dlp timeout 60s", seen["timeout"] == 60)
    check("挑到 HLS 那行（非第一行）", "hls_playlist" in sent["body"]["stream_url"])
    check("POST /start_recording", sent["path"] == "/start_recording" and sent["method"] == "POST")
    check("帶 secret", sent["with_secret"] is True)
    check("body 含 vdvno/mode/title",
          sent["body"]["vdvno"] == VDVNO and sent["body"]["mode"] == "speech"
          and sent["body"]["title"] == "台北市議會大會")

    # --title 優先於 vdv_title
    r2, _ = _run_rescue_once(_Args(title="自訂標題"), stdout, _backend)
    check("--title 優先於 vdv_title", r2["title"] == "自訂標題")

    # 沒有 HLS 特徵時退回第一個 https 行
    r3, _ = _run_rescue_once(_Args(), b"https://example.com/stream/xyz\n", _backend)
    check("無 HLS 特徵 → 退回第一個 https 行",
          sent["body"]["stream_url"] == "https://example.com/stream/xyz")


def test_rescue_expire_parsing():
    section("② manifest 到期時間解析（兩種形式）")
    from datetime import datetime as _dt
    epoch = 1784207000
    expected = _dt.fromtimestamp(epoch, ops.TW_TZ).strftime("%Y/%m/%d %H:%M:%S")

    path_form = f"https://x.googlevideo.com/api/manifest/hls_playlist/expire/{epoch}/x/index.m3u8"
    qs_form = f"https://x.googlevideo.com/videoplayback?id=1&expire={epoch}&sig=2"
    check("解析 /expire/<epoch>/ 形式", ops._manifest_expires_taiwan(path_form) == expected)
    check("解析 ?expire=<epoch> 形式", ops._manifest_expires_taiwan(qs_form) == expected)
    check("格式為 YYYY/MM/DD HH:MM:SS",
          bool(__import__("re").match(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}$", expected)))
    check("無 expire → None",
          ops._manifest_expires_taiwan("https://x.googlevideo.com/x.m3u8") is None)
    check("expire 非數字/畸形 → None",
          ops._manifest_expires_taiwan("https://x/?expire=abc") is None)

    # 端到端：rescue 輸出帶 manifest_expires_taiwan
    def _backend(path, method="GET", with_secret=False, timeout=60, json_body=None):
        return {"session_id": "rec_1"}
    r, _ = _run_rescue_once(_Args(), (path_form + "\n").encode(), _backend)
    check("rescue 輸出含 manifest_expires_taiwan", r["manifest_expires_taiwan"] == expected)


def test_rescue_rejects_non_youtube():
    section("② 非 YouTube hostname 一律拒絕")
    check("接受 youtube.com", ops._is_youtube_url("https://youtube.com/watch?v=1"))
    check("接受 www.youtube.com", ops._is_youtube_url("https://www.youtube.com/watch?v=1"))
    check("接受 m.youtube.com", ops._is_youtube_url("https://m.youtube.com/watch?v=1"))
    check("接受 youtu.be", ops._is_youtube_url("https://youtu.be/abc"))
    check("拒絕 evil.com（路徑含 youtube.com 也不行）",
          not ops._is_youtube_url("https://evil.com/youtube.com/x"))
    check("拒絕 notyoutube.com", not ops._is_youtube_url("https://notyoutube.com/x"))
    check("拒絕 youtube.com.evil.com", not ops._is_youtube_url("https://youtube.com.evil.com/x"))
    check("拒絕議會 HLS（rescue 不是萬用下載器）",
          not ops._is_youtube_url("https://tccstr2.tcc.gov.tw/live/x.m3u8"))

    def _backend(*a, **k):
        raise AssertionError("不該打到後端")

    # (a) vdv_url 非 YouTube → die（SystemExit）
    try:
        _run_rescue_once(_Args(), b"", _backend,
                         portal_impl=lambda e, p: [{"vdv_url": "https://tccstr2.tcc.gov.tw/live/x.m3u8",
                                                    "vdv_title": "一般場次"}])
        check("vdv_url 非 YouTube → 報錯", False)
    except SystemExit as e:
        check("vdv_url 非 YouTube → 報錯", e.code == 1)

    # (b) --url 非 YouTube → 同樣拒絕
    try:
        _run_rescue_once(_Args(url="https://vimeo.com/123"), b"", _backend)
        check("--url 非 YouTube → 報錯", False)
    except SystemExit as e:
        check("--url 非 YouTube → 報錯", e.code == 1)

    # (c) vdvno 格式不符 → 報錯
    try:
        _run_rescue_once(_Args(vdvno="bad_vdvno"), b"", _backend)
        check("vdvno 格式不符 → 報錯", False)
    except SystemExit as e:
        check("vdvno 格式不符 → 報錯", e.code == 1)


def test_rescue_ytdlp_missing():
    section("② yt-dlp 缺失報錯")
    import shutil
    undo = [_patch(shutil, "which", lambda n: None),
            _patch(ops.Path, "home", staticmethod(lambda: Path("/nonexistent-home")))]
    try:
        ops._find_ytdlp()
        check("找不到 yt-dlp → 報錯", False)
    except SystemExit as e:
        check("找不到 yt-dlp → 報錯", e.code == 1)
    finally:
        for fn in undo:
            fn()


def test_rescue_409_is_success():
    section("② 後端 409 → already_recording 且 exit 0")

    def _backend_409(path, method="GET", with_secret=False, timeout=60, json_body=None):
        raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)

    stdout = b"https://x.googlevideo.com/api/manifest/hls_playlist/expire/1784207000/x/index.m3u8\n"
    r, _ = _run_rescue_once(_Args(), stdout, _backend_409)
    check("409 → already_recording", r["status"] == "already_recording")
    check("409 → 帶 vdvno", r["vdvno"] == VDVNO)

    # cmd_rescue 不得 exit 非 0（冪等：重複補救不是錯誤）
    import subprocess
    undo = [
        _patch(subprocess, "run", lambda cmd, **kw: _FakeCompleted(stdout=stdout)),
        _patch(ops, "portal", lambda e, p=None: [{"vdv_url": YT_URL, "vdv_title": "大會"}]),
        _patch(ops, "_find_ytdlp", lambda: "/usr/bin/yt-dlp"),
        _patch(ops, "backend", _backend_409),
    ]
    try:
        ops.cmd_rescue(_Args())
        check("cmd_rescue 遇 409 不 exit（=exit 0）", True)
    except SystemExit:
        check("cmd_rescue 遇 409 不 exit（=exit 0）", False)
    finally:
        for fn in undo:
            fn()

    # 其他 HTTP 錯誤仍要往外拋（不可吞掉）
    def _backend_500(path, method="GET", with_secret=False, timeout=60, json_body=None):
        raise urllib.error.HTTPError(path, 500, "Server Error", {}, None)
    try:
        _run_rescue_once(_Args(), stdout, _backend_500)
        check("500 仍往外拋", False)
    except urllib.error.HTTPError as e:
        check("500 仍往外拋", e.code == 500)


def test_http_json_body_encoding():
    section("② http_json 的 json_body 編碼與 Content-Type")
    captured = {}

    class _Resp:
        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=None, context=None):
        captured["req"] = req
        return _Resp()

    undo = [_patch(ops.urllib.request, "urlopen", _fake_urlopen),
            _patch(ops, "_ssl_context", lambda: None)]
    try:
        r = ops.http_json("https://x/api", method="POST",
                          headers={"X-Trigger-Secret": "s"},
                          json_body={"title": "大會直播", "vdvno": VDVNO})
        req = captured["req"]
        check("回傳解析後 JSON", r == {"ok": True})
        check("method 為 POST", req.get_method() == "POST")
        check("body 為 UTF-8 JSON",
              json.loads(req.data.decode("utf-8")) == {"title": "大會直播", "vdvno": VDVNO})
        check("中文以 UTF-8 位元組送出（非 \\u 逃脫）", "大會直播".encode("utf-8") in req.data)
        check("自動帶 Content-Type: application/json",
              req.get_header("Content-type") == "application/json")
        check("既有 header 保留", req.get_header("X-trigger-secret") == "s")

        # 不帶 json_body → 行為與過去相同（無 body、無 Content-Type）
        ops.http_json("https://x/api", headers={"A": "b"})
        req2 = captured["req"]
        check("不帶 json_body → 無 body", req2.data is None)
        check("不帶 json_body → 無 Content-Type", req2.get_header("Content-type") is None)
    finally:
        for fn in undo:
            fn()


# ============ ② --follow 監控迴圈（可注入 clock / backend） ============

class _FakeClock:
    """假時鐘：sleep 直接推進時間，不真的等待。"""

    def __init__(self, start):
        self.now = start
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def _follow(args, clock, rescue_results, status_script):
    """跑 _rescue_follow，rescue_results / status_script 為依序回傳的腳本。"""
    calls = {"rescue": 0, "status": 0}

    def _rescue(_a):
        r = rescue_results[min(calls["rescue"], len(rescue_results) - 1)]
        calls["rescue"] += 1
        return r

    def _status(_sid):
        r = status_script[min(calls["status"], len(status_script) - 1)]
        calls["status"] += 1
        return r

    out = ops._rescue_follow(args, deps={
        "now": clock, "sleep": clock.sleep,
        "rescue_once": _rescue, "recording_status": _status,
    })
    return out, calls


def test_follow_normal_stop():
    section("② --follow 正常 stopped 收尾")
    clock = _FakeClock(datetime(2026, 7, 16, 14, 0, tzinfo=ops.TW_TZ))
    out, calls = _follow(
        _Args(follow=True, until="19:00", interval=600),
        clock,
        [{"status": "recording_started", "session_id": "rec_A"}],
        [
            {"status": "recording", "file_ids": ["rec_A_seg0"]},
            {"status": "recording", "file_ids": ["rec_A_seg0", "rec_A_seg1"]},
            {"status": "stopped", "file_ids": ["rec_A_seg0", "rec_A_seg1"], "error": None},
        ],
    )
    check("正常結束 → stop_reason=stopped", out["stop_reason"] == "stopped")
    check("彙整所有 file_ids", out["file_ids"] == ["rec_A_seg0", "rec_A_seg1"])
    check("無重啟", out["restarts"] == 0)
    check("正常結束即收手，不再輪詢", calls["status"] == 3)
    check("未到 until 就結束（省時）", clock.now.hour < 19)


def test_follow_restarts_on_error():
    section("② --follow 中斷後自動重新 rescue")
    clock = _FakeClock(datetime(2026, 7, 16, 14, 0, tzinfo=ops.TW_TZ))
    out, calls = _follow(
        _Args(follow=True, until="19:00", interval=600),
        clock,
        [{"status": "recording_started", "session_id": "rec_A"},
         {"status": "recording_started", "session_id": "rec_B"}],
        [
            {"status": "recording", "file_ids": ["rec_A_seg0"]},
            # 斷線（manifest 到期）→ 應重新 rescue 拿到 rec_B
            {"status": "stopped", "file_ids": ["rec_A_seg0"], "error": "串流已結束或錄製失敗"},
            {"status": "recording", "file_ids": ["rec_B_seg0"]},
            {"status": "stopped", "file_ids": ["rec_B_seg0"], "error": None},
        ],
    )
    check("中斷後重新 rescue（restarts=1）", out["restarts"] == 1)
    check("收尾時 session 為新的 rec_B", out["session_id"] == "rec_B"),
    check("新舊 session 的 file_ids 都收齊",
          out["file_ids"] == ["rec_A_seg0", "rec_B_seg0"])
    check("最終正常收尾", out["stop_reason"] == "stopped")
    check("rescue 共呼叫 2 次（首次＋重錄）", calls["rescue"] == 2)


def test_follow_until_reached():
    section("② --follow 到 --until 收尾")
    clock = _FakeClock(datetime(2026, 7, 16, 18, 30, tzinfo=ops.TW_TZ))
    out, _ = _follow(
        _Args(follow=True, until="19:00", interval=600),  # 30 分鐘 → 3 輪
        clock,
        [{"status": "recording_started", "session_id": "rec_A"}],
        [{"status": "recording", "file_ids": ["rec_A_seg0"]}],
    )
    check("到 until → stop_reason=until_reached", out["stop_reason"] == "until_reached")
    check("到 until 即停止（不超過 19:00 太多）", clock.now.hour == 19 and clock.now.minute == 0)
    check("仍回報已觀察到的 file_ids", out["file_ids"] == ["rec_A_seg0"])
    check("總結含 until_taiwan", out["until_taiwan"] == "2026/07/16 19:00")

    # until 已過 → 單次 rescue 後立即收尾
    clock2 = _FakeClock(datetime(2026, 7, 16, 19, 30, tzinfo=ops.TW_TZ))
    out2, calls2 = _follow(
        _Args(follow=True, until="19:00", interval=600), clock2,
        [{"status": "recording_started", "session_id": "rec_A"}],
        [{"status": "recording", "file_ids": []}],
    )
    check("until 已過 → 不進入監控迴圈", calls2["status"] == 0)
    check("until 已過 → 仍有做一次 rescue", calls2["rescue"] == 1)


def test_follow_409_then_takeover():
    section("② --follow 首次 409（已在錄）→ 續監控直到接手")
    clock = _FakeClock(datetime(2026, 7, 16, 14, 0, tzinfo=ops.TW_TZ))
    out, calls = _follow(
        _Args(follow=True, until="15:00", interval=600),
        clock,
        # 首次 409 → 無 session_id；下一輪 rescue 拿到 session
        [{"status": "already_recording", "vdvno": VDVNO},
         {"status": "recording_started", "session_id": "rec_C"}],
        [{"status": "stopped", "file_ids": ["rec_C_seg0"], "error": None}],
    )
    check("409 不中止，續監控並接手", out["session_id"] == "rec_C")
    check("接手計入 restarts", out["restarts"] == 1)
    check("接手後正常收尾", out["stop_reason"] == "stopped")
    check("收齊 file_ids", out["file_ids"] == ["rec_C_seg0"])


def test_follow_status_error_tolerated():
    section("② --follow 查詢失敗不中斷監控")
    clock = _FakeClock(datetime(2026, 7, 16, 14, 0, tzinfo=ops.TW_TZ))
    calls = {"n": 0}

    def _status(_sid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("後端 502")
        return {"status": "stopped", "file_ids": ["rec_A_seg0"], "error": None}

    out = ops._rescue_follow(_Args(follow=True, until="19:00", interval=600), deps={
        "now": clock, "sleep": clock.sleep,
        "rescue_once": lambda a: {"status": "recording_started", "session_id": "rec_A"},
        "recording_status": _status,
    })
    check("查詢失敗後續輪仍繼續", calls["n"] == 2)
    check("最終正常收尾", out["stop_reason"] == "stopped")


async def main_async():
    test_vdvno_pattern()
    await test_auto_status_uses_vdvno_pattern()
    await test_vdvno_requires_secret()
    await test_vdvno_success_path()
    await test_duplicate_vdvno_409()
    await test_failure_paths_release_marker()
    await test_no_vdvno_untouched()
    await test_claim_live_with_stale_recovery()
    await test_start_recording_recovers_stale_marker()
    await test_auto_record_check_stale_recovery_regression()
    await test_auto_record_check_skips_bad_vdvno()
    await test_session_state_file_ids()
    await test_recording_status_file_ids()
    test_rescue_happy_path()
    test_rescue_expire_parsing()
    test_rescue_rejects_non_youtube()
    test_rescue_ytdlp_missing()
    test_rescue_409_is_success()
    test_http_json_body_encoding()
    test_follow_normal_stop()
    test_follow_restarts_on_error()
    test_follow_until_reached()
    test_follow_409_then_takeover()
    test_follow_status_error_tolerated()


if __name__ == "__main__":
    asyncio.run(main_async())
    print(f"\n結果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
