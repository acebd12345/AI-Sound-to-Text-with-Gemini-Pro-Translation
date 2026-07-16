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


if __name__ == "__main__":
    asyncio.run(main_async())
    print(f"\n結果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
