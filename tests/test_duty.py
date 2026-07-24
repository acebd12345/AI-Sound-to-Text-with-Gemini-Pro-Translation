"""tests/test_duty.py — duty 自動值班協調器 + 殘段煞車 + /fetch_vod 測試

用法：python3 tests/test_duty.py
與 test_rescue.py 同風格（標準庫 + 自製 check()，本專案未裝 pytest）。
全 mock：不碰網路、GCS、SMTP、ffmpeg、真檔案（除 duty_state.json 用暫存目錄）。

涵蓋：
  ① duty 單趟：VOD 入列→段數→全完成→collect+mail，第二趟冪等
  ② 殭屍防護：onair 空不告警不計數；onair 非空連續 2 輪才告警
  ③ rescue session：stopped+onair 非空 → 重錄；onair 空 → 收尾
  ④ 6 小時 partial 交付
  ⑤ 日報：22:00 後首次才發
  ⑥ 告警節流
  ⑦ state 檔損毀重建不 crash
  ⑧ 殘段煞車（媒體時長換算 + 連續 2 段 <60s 停止且不上傳）
  ⑨ /fetch_vod：403 / 400 / 409 / 成功排入
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("BUCKET_NAME", "test-bucket")

_fake_opencc = types.ModuleType("opencc")
_fake_opencc.OpenCC = lambda *a, **k: types.SimpleNamespace(convert=lambda s: s)
sys.modules["opencc"] = _fake_opencc

import google.cloud.storage as gcs  # noqa: E402
from google.api_core.exceptions import NotFound, PreconditionFailed  # noqa: E402


class _FakeBucket:
    def __init__(self):
        self.blobs = {}
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

sys.path.insert(0, str(REPO / "hermes-kit"))
import council_ops as ops  # noqa: E402

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
    def __init__(self, data=None):
        super().__init__({k.lower(): v for k, v in (data or {}).items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _FakeReq:
    def __init__(self, headers=None):
        self.headers = _CIHeaders(headers)


SECRET = "s3cr3t"
VDVNO = "d45ff137-32ff-447e-b6f8-5498f2019bc8"   # 7/15 掃描窗孤兒（實戰案例）
TW = ops.TW_TZ


# ============ duty 測試骨架 ============

class _Harness:
    """duty 的全 mock 依賴集。每個外部呼叫都記錄，供斷言。"""

    def __init__(self, tmpdir, now=None):
        self.state_path = Path(tmpdir) / "duty_state.json"
        self.out_dir = Path(tmpdir) / "output"
        self.now_dt = now or datetime(2026, 7, 20, 14, 30, tzinfo=TW)
        self.trigger_result = {}
        self.autostatus_map = {}
        self.recstatus_map = {}
        self.status_map = {}
        self.onair = []
        self.rescue_impl = None
        self.fetch_vod_calls = []
        self.mails = []
        self.collects = []
        self.rescue_calls = []
        self.collect_result = None
        self.mail_error = None

    # --- deps ---
    def _collect(self, name, file_ids, out_dir, results):
        self.collects.append({"name": name, "file_ids": list(file_ids), "results": results})
        if self.collect_result is not None:
            return dict(self.collect_result)
        done = [f for f in file_ids if results.get(f, {}).get("status") == "completed"]
        missing = [f for f in file_ids if f not in done]
        return {"srt": str(self.out_dir / f"{name}.srt"), "txt": str(self.out_dir / f"{name}.txt"),
                "segments_done": len(done), "segments_missing": missing,
                "partial": bool(missing), "total_batches": 4,
                "untranslated_batches": 0, "partial_translation": False}

    def _mail(self, to, subject, body, attach=None):
        if self.mail_error:
            raise RuntimeError(self.mail_error)
        self.mails.append({"to": list(to) if not isinstance(to, str) else [to],
                           "subject": subject, "body": body, "attach": attach or []})
        return {"sent": True}

    def _rescue(self, vdvno, title=""):
        self.rescue_calls.append(vdvno)
        if self.rescue_impl:
            return self.rescue_impl(vdvno, title)
        return {"status": "recording_started", "session_id": "rec_new"}

    def _fetch_vod(self, vdvno):
        self.fetch_vod_calls.append(vdvno)
        return {"queued": True}

    def deps(self):
        return {
            "now": lambda: self.now_dt,
            "trigger": lambda: dict(self.trigger_result),
            "autostatus": lambda v: self.autostatus_map.get(v, {}),
            "today": lambda: {"onair": list(self.onair)},
            "rescue": self._rescue,
            "recstatus": lambda s: self.recstatus_map.get(s, {}),
            "check_status": lambda f: self.status_map.get(f, {"status": "processing"}),
            "collect": self._collect,
            "mail": self._mail,
            "fetch_vod": self._fetch_vod,
            "state_path": self.state_path,
            "out_dir": self.out_dir,
        }

    def run(self):
        return ops._duty_run(self.deps())

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


def _with_emails(fn):
    """duty 讀 CFG 取收件人清單——暫時注入測試用地址。"""
    def wrapper(*a, **kw):
        orig = dict(ops.CFG)
        ops.CFG["ADMIN_EMAILS"] = "admin@example.com"
        ops.CFG["RESULT_EMAILS"] = "result@example.com"
        try:
            return fn(*a, **kw)
        finally:
            ops.CFG.clear()
            ops.CFG.update(orig)
    return wrapper


# ============ ① duty 單趟：入列 → 段數 → 交付 → 冪等 ============

@_with_emails
def test_duty_full_cycle():
    section("① duty 單趟完整流程與冪等")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "台北市議會第14屆第3次大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["vod_d45ff137_seg0",
                                                              "vod_d45ff137_seg1"]}}
        h.status_map = {"vod_d45ff137_seg0": {"status": "completed"},
                        "vod_d45ff137_seg1": {"status": "completed"}}

        out = h.run()
        check("本輪追蹤到 VOD", out["vods_tracked"] == 1)
        check("取得 2 段", out["segments_learned"] == 2)
        check("collect 被呼叫一次", len(h.collects) == 1)
        check("每個 file_id 都打過 check_status（觸發翻譯）",
              h.collects[0]["file_ids"] == ["vod_d45ff137_seg0", "vod_d45ff137_seg1"])
        check("寄出一封結果信", len(h.mails) == 1)
        check("收件人是 RESULT_EMAILS", h.mails[0]["to"] == ["result@example.com"])
        check("主旨格式正確",
              h.mails[0]["subject"] == "[議會字幕] 台北市議會第14屆第3次大會 2026/07/20")
        check("附件含 srt 與 txt", len(h.mails[0]["attach"]) == 2)
        check("delivered 計數為 1", out["delivered"] == 1)

        st = h.state()
        check("state 已轉入 done", len(st["done"]) == 1 and st["done"][0]["vdvno"] == VDVNO)
        check("state 不再追蹤該場", VDVNO not in st["tracking"])

        # 第二趟：trigger 仍可能回同一筆（後端冪等），但不得重寄
        out2 = h.run()
        check("第二趟不重複寄信", len(h.mails) == 1)
        check("第二趟不重複 collect", len(h.collects) == 1)
        check("第二趟 delivered 為 0", out2["delivered"] == 0)


@_with_emails
def test_duty_partial_not_delivered_early():
    section("① 部分完成且未逾時 → 不寄")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["a_seg0", "a_seg1"]}}
        h.status_map = {"a_seg0": {"status": "completed"}, "a_seg1": {"status": "processing"}}
        out = h.run()
        check("未全部完成不寄信", len(h.mails) == 0)
        check("未全部完成不 collect", len(h.collects) == 0)
        check("仍在追蹤中", out["tracking"] == 1)


# ============ ② 殭屍防護 ============

@_with_emails
def test_zombie_protection_no_onair():
    section("② 殭屍防護：無 onair 時 rescue 失敗不告警不計數")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td, now=datetime(2026, 7, 20, 7, 30, tzinfo=TW))
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "尚未開播的場次",
             "last_failure": {"reason": "YouTube 直播無法後端錄製"}}]}
        h.onair = []          # 早晨：還沒開播
        h.rescue_impl = lambda v, t: (_ for _ in ()).throw(SystemExit("yt-dlp 未解出任何網址"))

        out1 = h.run()
        out2 = h.run()
        check("兩輪皆無告警信", len(h.mails) == 0)
        check("alerts_sent 為 0", out1["alerts_sent"] == 0 and out2["alerts_sent"] == 0)
        st = h.state()
        check("失敗計數維持 0", st["tracking"][VDVNO]["rescue_fail_streak"] == 0)
        check("標記為尚未開播", st["tracking"][VDVNO]["pending_not_started"] is True)
        check("仍在追蹤（下輪再試）", VDVNO in st["tracking"])


@_with_emails
def test_zombie_protection_with_onair_alerts_after_two():
    section("② onair 非空：連續 2 輪失敗才告警")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "進行中的大會",
             "last_failure": {"reason": "YouTube 直播無法後端錄製"}}]}
        h.onair = [{"vdv_vdvno": VDVNO}]      # 確實在直播
        h.rescue_impl = lambda v, t: (_ for _ in ()).throw(RuntimeError("解析失敗"))

        h.run()
        check("第 1 輪失敗不告警", len(h.mails) == 0)
        check("第 1 輪計數為 1", h.state()["tracking"][VDVNO]["rescue_fail_streak"] == 1)

        h.run()
        check("第 2 輪告警", len(h.mails) == 1)
        check("告警寄給 ADMIN", h.mails[0]["to"] == ["admin@example.com"])
        check("告警主旨含 rescue", "rescue" in h.mails[0]["subject"])
        check("第 2 輪計數為 2", h.state()["tracking"][VDVNO]["rescue_fail_streak"] == 2)


@_with_emails
def test_rescue_success_resets_streak():
    section("② rescue 成功 → 計數歸零、session 入 state")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "大會",
             "last_failure": {"reason": "YouTube 直播無法後端錄製"}}]}
        h.onair = [{"vdv_vdvno": VDVNO}]
        h.rescue_impl = lambda v, t: (_ for _ in ()).throw(RuntimeError("boom"))
        h.run()
        check("先累積 1 次失敗", h.state()["tracking"][VDVNO]["rescue_fail_streak"] == 1)

        h.rescue_impl = None       # 這輪成功
        h.recstatus_map["rec_new"] = {"status": "recording", "file_ids": []}
        out = h.run()
        st = h.state()
        check("計數歸零", st["tracking"][VDVNO]["rescue_fail_streak"] == 0)
        check("session 記入 state", st["tracking"][VDVNO]["sessions"] == ["rec_new"])
        check("rescued 計數", out["rescued"] == 1)


# ============ ③ rescue session 監控 ============

@_with_emails
def test_session_stopped_onair_still_live_restarts():
    section("③ session stopped ＋ onair 非空 → 重新 rescue（斷線接續）")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "大會",
             "last_failure": {"reason": "YouTube 直播無法後端錄製"}}]}
        h.onair = [{"vdv_vdvno": VDVNO}]
        h.rescue_impl = lambda v, t: {"status": "recording_started", "session_id": "rec_1"}
        h.recstatus_map["rec_1"] = {"status": "recording", "file_ids": []}
        h.run()
        check("首輪開錄 rec_1", h.state()["tracking"][VDVNO]["sessions"] == ["rec_1"])

        # 下一輪：rec_1 斷線停止，但會議還在開
        h.trigger_result = {}
        h.recstatus_map["rec_1"] = {"status": "stopped", "file_ids": ["rec_1_seg0"],
                                    "error": "manifest 過期"}
        h.rescue_impl = lambda v, t: {"status": "recording_started", "session_id": "rec_2"}
        out = h.run()
        st = h.state()["tracking"][VDVNO]
        check("斷線後重新 rescue", "rec_2" in st["sessions"])
        check("舊 session 的段先收進來", st["file_ids"] == ["rec_1_seg0"])
        check("舊 session 標記完成", "rec_1" in st["finished_sessions"])
        check("重錄期間不交付", len(h.mails) == 0)
        check("sessions_finalized 計數", out["sessions_finalized"] == 1)


@_with_emails
def test_session_stopped_onair_empty_finalizes():
    section("③ session stopped ＋ onair 空 → 收尾並交付")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "散會的大會",
             "last_failure": {"reason": "YouTube 直播無法後端錄製"}}]}
        h.onair = [{"x": 1}]
        h.rescue_impl = lambda v, t: {"status": "recording_started", "session_id": "rec_1"}
        h.recstatus_map["rec_1"] = {"status": "recording", "file_ids": []}
        h.run()

        h.trigger_result = {}
        h.onair = []          # 散會了
        h.recstatus_map["rec_1"] = {"status": "stopped",
                                    "file_ids": ["rec_1_seg0", "rec_1_seg1"], "error": None}
        h.status_map = {"rec_1_seg0": {"status": "completed"},
                        "rec_1_seg1": {"status": "completed"}}
        out = h.run()
        check("不再重新 rescue", h.rescue_calls == [VDVNO])
        check("收尾後交付", out["delivered"] == 1)
        check("寄出結果信", len(h.mails) == 1 and "散會的大會" in h.mails[0]["subject"])
        check("移入 done", VDVNO not in h.state()["tracking"])


# ============ ④ 6 小時 partial 交付 ============

@_with_emails
def test_partial_delivery_after_six_hours():
    section("④ 滿 6 小時的部分完成 → 照寄並註記 + 告警")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td, now=datetime(2026, 7, 20, 10, 0, tzinfo=TW))
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "卡住的大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["b_seg0", "b_seg1"]}}
        h.status_map = {"b_seg0": {"status": "completed"}, "b_seg1": {"status": "processing"}}
        h.run()
        check("6 小時內不寄", len(h.mails) == 0)

        # 6 小時又 1 分鐘後
        h.now_dt = datetime(2026, 7, 20, 16, 1, tzinfo=TW)
        h.trigger_result = {}
        out = h.run()
        result_mails = [m for m in h.mails if m["to"] == ["result@example.com"]]
        admin_mails = [m for m in h.mails if m["to"] == ["admin@example.com"]]
        check("逾時後照寄", len(result_mails) == 1)
        check("主旨註記部分結果", "（部分結果）" in result_mails[0]["subject"])
        check("內文說明缺哪段", "b_seg1" in result_mails[0]["body"])
        check("另發 ADMIN 告警", len(admin_mails) == 1)
        check("移入 done", VDVNO not in h.state()["tracking"])
        check("delivered 計數", out["delivered"] == 1)


@_with_emails
def test_partial_translation_subject_note():
    section("④ partial_translation 主旨註記")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["c_seg0"]}}
        h.status_map = {"c_seg0": {"status": "completed"}}
        h.collect_result = {"srt": "/tmp/x.srt", "txt": "/tmp/x.txt", "segments_done": 1,
                            "segments_missing": [], "partial": False, "total_batches": 10,
                            "untranslated_batches": 3, "partial_translation": True}
        h.run()
        check("主旨註記含未翻譯段落", "（含未翻譯段落）" in h.mails[0]["subject"])
        check("主旨無部分結果註記", "（部分結果）" not in h.mails[0]["subject"])
        check("內文說明未翻譯批次", "3/10" in h.mails[0]["body"])


@_with_emails
def test_both_subject_notes():
    section("④ partial ＋ partial_translation 兩個註記都加")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["d_seg0"]}}
        h.status_map = {"d_seg0": {"status": "completed"}}
        h.collect_result = {"srt": "/tmp/x.srt", "txt": "/tmp/x.txt", "segments_done": 1,
                            "segments_missing": ["d_seg1"], "partial": True, "total_batches": 5,
                            "untranslated_batches": 1, "partial_translation": True}
        h.run()
        subj = h.mails[0]["subject"]
        check("兩個註記都在", "（部分結果）" in subj and "（含未翻譯段落）" in subj)


# ============ ⑤ 日報 ============

@_with_emails
def test_daily_report_after_22():
    section("⑤ 日報：22:00 後首次執行才發，當日第二次不發")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td, now=datetime(2026, 7, 20, 21, 30, tzinfo=TW))
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": []},
                                   "vod_failure": {"reason": "官方尚未上架 HLS（YouTube URL）"}}
        out = h.run()
        check("21:30 不發日報", out["report_sent"] == 0 and len(h.mails) == 0)

        h.now_dt = datetime(2026, 7, 20, 22, 5, tzinfo=TW)
        h.trigger_result = {}
        out = h.run()
        reports = [m for m in h.mails if "日報" in m["subject"]]
        check("22:05 發日報", out["report_sent"] == 1 and len(reports) == 1)
        check("日報寄給 ADMIN", reports[0]["to"] == ["admin@example.com"])
        check("日報列出待處理", "官方尚未上架" in reports[0]["body"])
        check("state 記錄日期", h.state()["last_report_date"] == "2026-07-20")

        h.now_dt = datetime(2026, 7, 20, 23, 0, tzinfo=TW)
        out = h.run()
        check("同日第二次不重發", out["report_sent"] == 0
              and len([m for m in h.mails if "日報" in m["subject"]]) == 1)

        h.now_dt = datetime(2026, 7, 21, 22, 5, tzinfo=TW)
        out = h.run()
        check("隔日照發", out["report_sent"] == 1)


# ============ ⑥ 告警節流 ============

@_with_emails
def test_alert_throttle():
    section("⑥ 告警節流：同一事由每日最多 2 封")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)

        def _boom():
            raise RuntimeError("後端 503")
        h.deps_trigger_fail = True
        deps = h.deps()
        deps["trigger"] = _boom

        for _ in range(5):
            ops._duty_run(deps)
        alerts = [m for m in h.mails if "trigger" in m["subject"]]
        check("5 輪只發 2 封", len(alerts) == 2)

        # 隔日重置
        h.now_dt = datetime(2026, 7, 21, 14, 30, tzinfo=TW)
        deps2 = h.deps()
        deps2["trigger"] = _boom
        ops._duty_run(deps2)
        check("隔日重置後可再發", len([m for m in h.mails if "trigger" in m["subject"]]) == 3)


# ============ ⑦ state 檔損毀 ============

def test_corrupt_state_rebuilds():
    section("⑦ state 檔損毀 → 重建不 crash")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "duty_state.json"

        p.write_text("{ 這不是 JSON", encoding="utf-8")
        st = ops._duty_load_state(p)
        check("壞 JSON 回空狀態", st["tracking"] == {} and st["done"] == [])

        p.write_text('["不是 dict"]', encoding="utf-8")
        check("非 dict 回空狀態", ops._duty_load_state(p)["tracking"] == {})

        p.write_text('{"tracking": "應該是 dict", "done": 5}', encoding="utf-8")
        st = ops._duty_load_state(p)
        check("型別不符欄位丟棄", st["tracking"] == {} and st["done"] == [])

        p.write_text('{"tracking": {"a": "壞掉的筆", "b": {"vdvno": "b"}}}', encoding="utf-8")
        st = ops._duty_load_state(p)
        check("單筆毀損只丟該筆", list(st["tracking"]) == ["b"])

        check("缺檔回空狀態", ops._duty_load_state(Path(td) / "nope.json")["tracking"] == {})
        check("版本欄位存在", ops._duty_load_state(p)["version"] == ops.DUTY_STATE_VERSION)


@_with_emails
def test_corrupt_state_full_run():
    section("⑦ 損毀 state 下 duty 整趟仍可跑完")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.state_path.write_text("<<<壞掉的檔案>>>", encoding="utf-8")
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "大會"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["e_seg0"]}}
        h.status_map = {"e_seg0": {"status": "completed"}}
        out = h.run()
        check("不 crash 且完成交付", out["delivered"] == 1)
        check("state 檔已重建為合法 JSON", h.state()["version"] == ops.DUTY_STATE_VERSION)


# ============ ⑧ 孤兒補抓 ============

@_with_emails
def test_orphan_refetch():
    section("⑧ 掃描窗孤兒：持續呼叫 fetch_vod 重試")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"vods_queued": [{"vdvno": VDVNO, "title": "7/15 未上架場次"}]}
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": []},
                                   "vod_failure": {"reason": "官方尚未上架 HLS（YouTube URL）"}}
        out = h.run()
        check("記錄 vod_failure 但不告警", len(h.mails) == 0)
        check("呼叫 fetch_vod 重試", h.fetch_vod_calls == [VDVNO])
        check("orphans_retried 計數", out["orphans_retried"] == 1)

        # 下一輪：掉出掃描窗（trigger 不再回報），仍要繼續重試
        h.trigger_result = {}
        h.run()
        check("掉出掃描窗仍重試", h.fetch_vod_calls == [VDVNO, VDVNO])

        # 官方終於上架
        h.autostatus_map[VDVNO] = {"vod_marker": {"file_ids": ["f_seg0"]}}
        h.status_map = {"f_seg0": {"status": "completed"}}
        out = h.run()
        check("上架後正常交付", out["delivered"] == 1)
        check("交付後不再重試", h.fetch_vod_calls == [VDVNO, VDVNO])


@_with_emails
def test_non_youtube_live_failed_ignored():
    section("⑧ 非 YouTube 的 live_failed 不觸發 rescue")
    with tempfile.TemporaryDirectory() as td:
        h = _Harness(td)
        h.trigger_result = {"live_failed": [
            {"vdvno": VDVNO, "title": "其他失敗",
             "last_failure": {"reason": "開錄失敗：連線逾時"}}]}
        h.run()
        check("不呼叫 rescue", h.rescue_calls == [])
        check("不告警", len(h.mails) == 0)


# ============ ⑨ 殘段煞車（main.recording_loop）============

def test_wav_media_seconds():
    section("⑨ 媒體時長換算")
    check("32000 bytes = 1 秒", abs(main._wav_media_seconds(32000) - 1.0) < 0.01)
    check("6 秒殘段 ≈ 192000 bytes", abs(main._wav_media_seconds(192000) - 6.0) < 0.01)
    check("30 分鐘 = 1800 秒", abs(main._wav_media_seconds(1800 * 32000) - 1800.0) < 0.01)
    check("殘段判定：6 秒 < 60 秒門檻",
          main._wav_media_seconds(192000) < main.SHORT_SEGMENT_SECONDS)
    check("正常段不誤判：1800 秒 >= 門檻",
          main._wav_media_seconds(1800 * 32000) >= main.SHORT_SEGMENT_SECONDS)
    check("連續門檻為 2", main.SHORT_SEGMENT_CONSECUTIVE_LIMIT == 2)


class _RecordingLoopHarness:
    """把 recording_loop 的外部依賴（ffmpeg / os / GCS）全換掉。

    每段 ffmpeg「產出」的檔案大小由 sizes 清單決定，用來模擬散會殘段。
    """

    def __init__(self, sizes, wall_elapsed=1800):
        self.sizes = list(sizes)
        self.wall_elapsed = wall_elapsed
        self.calls = 0
        self.uploaded = []
        self.removed = []

    def install(self):
        self._undo = []

        async def _fake_exec(*a, **k):
            class _P:
                async def communicate(_self):
                    return (b"", b"ffmpeg done")
            return _P()

        def _fake_getsize(path):
            idx = min(self.calls - 1, len(self.sizes) - 1)
            return self.sizes[idx]

        def _fake_exists(path):
            return True

        def _fake_remove(path):
            self.removed.append(path)

        clock = {"t": 0.0}

        def _fake_time():
            # 每次呼叫推進 wall_elapsed/2，使 seg_wall_elapsed == wall_elapsed
            clock["t"] += self.wall_elapsed / 2
            return clock["t"]

        harness = self

        class _FakeBlobRec:
            def __init__(self, name):
                self.name = name

            def upload_from_string(self, data, **k):
                pass

            def upload_from_filename(self, path):
                harness.uploaded.append(self.name)

        class _FakeBucketRec:
            def blob(self, name):
                return _FakeBlobRec(name)

        orig_exec = asyncio.create_subprocess_exec

        async def _counting_exec(*a, **k):
            self.calls += 1
            return await _fake_exec(*a, **k)

        for obj, name, val in (
            (asyncio, "create_subprocess_exec", _counting_exec),
            (main.os.path, "getsize", _fake_getsize),
            (main.os.path, "exists", _fake_exists),
            (main.os, "remove", _fake_remove),
            (main.time, "time", _fake_time),
        ):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        self._undo.append((main, "storage_client", main.storage_client))
        main.storage_client = types.SimpleNamespace(bucket=lambda n: _FakeBucketRec())
        self._undo.append((main, "_write_session_state", main._write_session_state))
        main._write_session_state = lambda *a, **k: None
        return self

    def restore(self):
        for obj, name, val in reversed(self._undo):
            setattr(obj, name, val)


async def _run_recording_loop(sizes, wall_elapsed=1800):
    sid = "rec_test_short"
    main.active_recordings[sid] = {
        "status": "recording", "stream_url": "https://x/y.m3u8", "title": "測試",
        "mode": "speech", "diarize": False, "known_names": "", "segments": [],
        "stop": False, "started_at": 0, "error": None,
    }
    h = _RecordingLoopHarness(sizes, wall_elapsed).install()
    try:
        await main.recording_loop(sid, "https://x/y.m3u8", "speech", False, "")
    finally:
        h.restore()
    rec = main.active_recordings.pop(sid)
    return rec, h


async def test_short_segment_brake():
    section("⑨ 散會殘段煞車：連續 2 段 <60s → 停止且不上傳")
    # 第 0 段正常（30 分鐘），第 1、2 段是 6 秒殘段
    rec, h = await _run_recording_loop([1800 * 32000, 6 * 32000, 6 * 32000])
    check("正常段有上傳", "raw_audio/rec_test_short_seg0/0" in h.uploaded)
    check("第 1 個殘段上傳（尚未達連續門檻）",
          "raw_audio/rec_test_short_seg1/0" in h.uploaded)
    check("第 2 個殘段不上傳（觸發煞車）",
          "raw_audio/rec_test_short_seg2/0" not in h.uploaded)
    check("總共只上傳 2 段", len(h.uploaded) == 2)
    check("狀態為 stopped", rec["status"] == "stopped")
    check("error 註明殘段偵測", "串流結束（殘段偵測）" in (rec["error"] or ""))
    check("觸發煞車的殘段暫存已刪除", len(h.removed) >= 1)
    check("segments 只記錄實際上傳的段", len(rec["segments"]) == 2)


async def test_short_segment_streak_resets():
    section("⑨ 殘段計數會被正常段重置（不誤停）")
    # 短、正常、短、正常… 永遠不會連續 2 段短 → 由段數上限收尾
    sizes = [6 * 32000, 1800 * 32000] * 25
    rec, h = await _run_recording_loop(sizes)
    check("非連續的短段不觸發殘段煞車",
          "殘段偵測" not in (rec["error"] or ""))
    check("由段數上限正常收尾", "段數上限" in (rec["error"] or ""))
    check("上傳段數達上限", len(h.uploaded) == main.MAX_SEGMENTS_PER_SESSION)


async def test_normal_recording_unaffected():
    section("⑨ 正常錄製不受新煞車影響（回歸）")
    rec, h = await _run_recording_loop([1800 * 32000] * 5)
    check("正常段全部上傳（至段數上限）", len(h.uploaded) == main.MAX_SEGMENTS_PER_SESSION)
    check("非殘段停止原因", "殘段偵測" not in (rec["error"] or ""))


async def test_tiny_file_brake_still_works():
    section("⑨ 既有 <10KB 煞車仍優先生效（回歸）")
    rec, h = await _run_recording_loop([5000])
    check("過小檔案不上傳", h.uploaded == [])
    check("error 為既有的檔案過小訊息", "串流已結束或錄製失敗" in (rec["error"] or ""))


# ============ ⑩ /fetch_vod 端點 ============

def _reset_backend():
    BUCKET.reset()
    os.environ["AUTO_TRIGGER_SECRET"] = SECRET


async def test_fetch_vod_endpoint():
    section("⑩ POST /fetch_vod/{vdvno}")
    _reset_backend()

    # 403：secret 錯誤
    try:
        await main.fetch_vod(VDVNO, _FakeReq({"X-Trigger-Secret": "wrong"}), BackgroundTasks())
        check("錯誤 secret 應 403", False)
    except HTTPException as e:
        check("錯誤 secret 回 403", e.status_code == 403)

    # 503：未設定 secret
    saved = os.environ.pop("AUTO_TRIGGER_SECRET")
    try:
        await main.fetch_vod(VDVNO, _FakeReq({}), BackgroundTasks())
        check("未啟用應 503", False)
    except HTTPException as e:
        check("未設定 secret 回 503", e.status_code == 503)
    os.environ["AUTO_TRIGGER_SECRET"] = saved

    req = _FakeReq({"X-Trigger-Secret": SECRET})

    # 400：vdvno 格式不符
    for bad in ("short", "../../etc/passwd", "has_underscore", "a" * 65, ""):
        try:
            await main.fetch_vod(bad, req, BackgroundTasks())
            check(f"非法 vdvno {bad!r} 應 400", False)
        except HTTPException as e:
            check(f"非法 vdvno {bad!r} 回 400", e.status_code == 400)

    # 成功排入
    bg = BackgroundTasks()
    r = await main.fetch_vod(VDVNO, req, bg)
    check("回傳 queued", r["queued"] is True)
    check("回傳 file_id_prefix", r["file_id_prefix"] == f"vod_{VDVNO[:8]}_seg")
    check("marker 已建立", f"auto_state/vod/{VDVNO}" in BUCKET.blobs)
    check("背景任務已排入", len(bg.tasks) == 1)
    check("背景任務是 fetch_vod_background", bg.tasks[0].func is main.fetch_vod_background)

    # 409：重複呼叫（marker 仍在）
    try:
        await main.fetch_vod(VDVNO, req, BackgroundTasks())
        check("重複呼叫應 409", False)
    except HTTPException as e:
        check("重複呼叫回 409", e.status_code == 409)


def test_kit_fetchvod_command():
    section("⑩ kit fetchvod 子命令")
    import urllib.error

    sent = {}

    def _backend_ok(path, method="GET", with_secret=False, timeout=60, json_body=None):
        sent.update(path=path, method=method, with_secret=with_secret)
        return {"queued": True, "file_id_prefix": f"vod_{VDVNO[:8]}_seg"}

    orig = ops.backend
    ops.backend = _backend_ok
    try:
        r = ops._fetch_vod(VDVNO)
        check("打對端點", sent["path"] == f"/fetch_vod/{VDVNO}")
        check("用 POST", sent["method"] == "POST")
        check("帶 secret", sent["with_secret"] is True)
        check("回傳 queued", r["queued"] is True)

        def _backend_409(*a, **k):
            raise urllib.error.HTTPError("u", 409, "conflict", {}, None)

        ops.backend = _backend_409
        r = ops._fetch_vod(VDVNO)
        check("409 視為冪等成功（不拋例外）", r["already_queued"] is True)

        def _backend_500(*a, **k):
            raise urllib.error.HTTPError("u", 500, "boom", {}, None)

        ops.backend = _backend_500
        try:
            ops._fetch_vod(VDVNO)
            check("500 應往上拋", False)
        except urllib.error.HTTPError:
            check("500 往上拋", True)

        try:
            ops._fetch_vod("bad")
            check("非法 vdvno 應 exit", False)
        except SystemExit:
            check("非法 vdvno 在本機就擋掉", True)
    finally:
        ops.backend = orig


# ============ 執行 ============

async def main_async():
    test_duty_full_cycle()
    test_duty_partial_not_delivered_early()
    test_zombie_protection_no_onair()
    test_zombie_protection_with_onair_alerts_after_two()
    test_rescue_success_resets_streak()
    test_session_stopped_onair_still_live_restarts()
    test_session_stopped_onair_empty_finalizes()
    test_partial_delivery_after_six_hours()
    test_partial_translation_subject_note()
    test_both_subject_notes()
    test_daily_report_after_22()
    test_alert_throttle()
    test_corrupt_state_rebuilds()
    test_corrupt_state_full_run()
    test_orphan_refetch()
    test_non_youtube_live_failed_ignored()
    test_wav_media_seconds()
    await test_short_segment_brake()
    await test_short_segment_streak_resets()
    await test_normal_recording_unaffected()
    await test_tiny_file_brake_still_works()
    await test_fetch_vod_endpoint()
    test_kit_fetchvod_command()


if __name__ == "__main__":
    asyncio.run(main_async())
    print(f"\n結果：{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
