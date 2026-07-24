#!/usr/bin/env python3
"""council_ops.py — 議會自動錄製值班工具（給 Hermes Agent 的手腳）

只用 Python 標準庫，無需 pip install。設定讀取順序：環境變數 → 同目錄 .env。

子命令：
  duty                          自動值班單趟（偵測→追蹤→合併→寄信→日報，cron 用）
  health                        後端健康檢查
  trigger                       打 /auto_record_check（掃直播+VOD，冪等）
  status <file_id>              查單一 file_id 的轉錄翻譯進度
  wait <file_id...>             輪詢多個 file_id 直到全部完成（觸發翻譯靠這個）
  collect <輸出名> <file_id...>  等待+合併多切段字幕（時間軸自動位移），輸出 .srt/.txt
  autostatus <vdvno>            查某 vdvno 的處理全貌（marker/session/失敗記錄）
  rescue <vdvno> [--follow]     YouTube 直播補救：本機解直連網址餵後端開錄
  recstatus <session_id>        查錄製 session 狀態（含 file_ids）
  today                         今天的直播與 VOD 概況（議會公開 API）
  fetchvod <vdvno>              指名補抓單場 VOD（繞過今天+昨天的掃描窗）
  mail --to a@b --subject S --body B [--attach f ...]   寄信（SMTP）

所有子命令輸出 JSON（stdout），失敗時 exit code != 0 並在 stderr 說明。
"""

import argparse
import json
import mimetypes
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

KIT_DIR = Path(__file__).resolve().parent
PORTAL_API = "https://live.tcc.gov.tw/iSharePortalWeb/api/"
PORTAL_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://live.tcc.gov.tw/iSharePortalWeb/User/Default.aspx",
}
TW_TZ = timezone(timedelta(hours=8))
SEGMENT_SECONDS = 1800  # 系統切段長度（30 分鐘），合併字幕時的時間軸位移單位


# ---------- 設定 ----------

def load_config() -> dict:
    """環境變數優先，缺的從同目錄 .env 補，密鑰再退回 secret.txt。"""
    cfg = {}
    env_file = KIT_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    cfg.update({k: v for k, v in os.environ.items() if k in {
        "SYSTEM_URL", "AUTO_TRIGGER_SECRET",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_FROM",
        "ADMIN_EMAILS", "RESULT_EMAILS",
    }})
    if not cfg.get("AUTO_TRIGGER_SECRET"):
        secret_file = KIT_DIR / "secret.txt"
        if secret_file.exists():
            cfg["AUTO_TRIGGER_SECRET"] = secret_file.read_text().strip()
    return cfg


CFG = load_config()


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# ---------- HTTP ----------

def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # macOS 的 python.org 安裝檔常缺 CA；有 certifi 就用，否則試系統憑證檔
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        for p in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(p):
                ctx.load_verify_locations(p)
                break
    return ctx


def _decode_json(raw: bytes):
    """解析 JSON，容忍 BOM/UTF-16 與伺服器強制回傳的 gzip/deflate 壓縮。"""
    import gzip
    import zlib
    candidates = [raw]
    for fn in (gzip.decompress, zlib.decompress, lambda b: zlib.decompress(b, -15)):
        try:
            candidates.append(fn(raw))
        except Exception:
            pass
    for data in candidates:
        for enc in ("utf-8-sig", "utf-16", "utf-8"):
            try:
                return json.loads(data.decode(enc))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    raise ValueError(f"無法解析回應: {raw[:200]!r}")


def http_json(url: str, method: str = "GET", headers: dict = None, timeout: int = 30,
              json_body: dict = None):
    """json_body 非 None 時以 UTF-8 JSON 送出，並自動帶 Content-Type。"""
    headers = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read()
    return _decode_json(raw)


def backend(path: str, method: str = "GET", with_secret: bool = False, timeout: int = 60,
            json_body: dict = None):
    if not CFG.get("SYSTEM_URL"):
        die("缺少 SYSTEM_URL（設定環境變數或 hermes-kit/.env）")
    headers = {}
    if with_secret:
        secret = CFG.get("AUTO_TRIGGER_SECRET", "")
        if not secret:
            die("缺少 AUTO_TRIGGER_SECRET（設定環境變數或 hermes-kit/secret.txt）")
        headers["X-Trigger-Secret"] = secret
    return http_json(CFG["SYSTEM_URL"] + path, method=method, headers=headers, timeout=timeout,
                     json_body=json_body)


def _curl_json(url: str, headers: dict, timeout: int = 20):
    """curl fallback：部分環境的 Python 驗不過政府憑證鏈，curl（系統信任庫）可以。"""
    import subprocess
    cmd = ["curl", "-s", "-m", str(timeout), "--compressed"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    out = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if out.returncode != 0:
        raise RuntimeError(f"curl 失敗 (exit {out.returncode}): {out.stderr.decode()[:200]}")
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return json.loads(out.stdout.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"無法解析 curl 回應: {out.stdout[:200]!r}")


def portal(endpoint: str, params: dict = None):
    """議會公開 API（唯讀、不帶任何密鑰）。

    優先走 curl（同時解決部分環境驗不過政府憑證鏈、與伺服器強制壓縮
    兩個問題），沒有 curl 才用 urllib。"""
    import shutil
    url = PORTAL_API + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    if shutil.which("curl"):
        try:
            return _curl_json(url, PORTAL_HEADERS)
        except Exception:
            pass  # 退回 urllib 再試一次
    return http_json(url, headers=PORTAL_HEADERS, timeout=20)


# ---------- 子命令 ----------

def cmd_health(_args):
    print(json.dumps(backend("/health"), ensure_ascii=False))


def _trigger() -> dict:
    return backend("/auto_record_check", method="POST", with_secret=True, timeout=120)


def cmd_trigger(_args):
    print(json.dumps(_trigger(), ensure_ascii=False, indent=1))


def _fetch_vod(vdvno: str) -> dict:
    """指名補抓單場 VOD（不受 auto_record_check 的今天+昨天掃描窗限制）。

    409（已在處理中）視為成功的冪等結果，不是錯誤。
    """
    if not VDVNO_RE.match(vdvno or ""):
        die(f"vdvno 格式不符（需 8-64 碼英數或 hyphen）: {vdvno!r}")
    try:
        return backend(f"/fetch_vod/{urllib.parse.quote(vdvno)}",
                       method="POST", with_secret=True)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return {"queued": False, "already_queued": True, "vdvno": vdvno}
        raise


def cmd_fetchvod(args):
    print(json.dumps(_fetch_vod(args.vdvno), ensure_ascii=False, indent=1))


def _check_status(file_id: str) -> dict:
    """查單一 file_id。**呼叫本身即觸發該段的翻譯**（後端在此排背景任務）。"""
    return backend(f"/check_status/{urllib.parse.quote(file_id)}?total_chunks=1")


def cmd_status(args):
    r = _check_status(args.file_id)
    # 完成時字幕內容很大，status 命令只回摘要；要拿內容用 collect
    if r.get("status") == "completed":
        out = {"status": "completed",
               "srt_chars": len(r.get("srt_text", "")),
               "plain_chars": len(r.get("plain_text", ""))}
        # 保留後端回傳的翻譯統計（若有）
        for k in ("total_batches", "untranslated_batches"):
            if r.get(k) is not None:
                out[k] = r[k]
        r = out
    print(json.dumps(r, ensure_ascii=False))


def _wait_all(file_ids, interval: int, max_minutes: int) -> dict:
    """輪詢直到全部 completed 或超時。回傳 {file_id: result_dict}。"""
    deadline = time.time() + max_minutes * 60
    results = {}
    pending = list(file_ids)
    while pending and time.time() < deadline:
        still = []
        for fid in pending:
            try:
                r = _check_status(fid)
            except Exception as e:
                print(f"[wait] {fid} 查詢失敗: {e}", file=sys.stderr)
                still.append(fid)
                continue
            if r.get("status") == "completed":
                results[fid] = r
                print(f"[wait] {fid} 完成", file=sys.stderr)
            else:
                still.append(fid)
        pending = still
        if pending:
            time.sleep(interval)
    for fid in pending:
        results[fid] = {"status": "timeout"}
    return results


def cmd_wait(args):
    results = _wait_all(args.file_ids, args.interval, args.max_minutes)
    summary = {fid: r.get("status") for fid, r in results.items()}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if any(s != "completed" for s in summary.values()):
        sys.exit(2)


TS_LINE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})")


def _shift_srt(srt: str, offset_seconds: int) -> str:
    """SRT 時間軸整體位移 offset_seconds。"""
    def shift(m):
        vals = list(map(int, m.groups()))
        out = []
        for h, mi, s, ms in (vals[:4], vals[4:]):
            total = (h * 3600 + mi * 60 + s) * 1000 + ms + offset_seconds * 1000
            total = max(total, 0)
            out.append(f"{total // 3600000:02d}:{total % 3600000 // 60000:02d}:"
                       f"{total % 60000 // 1000:02d},{total % 1000:03d}")
        return f"{out[0]} --> {out[1]}"
    return TS_LINE.sub(shift, srt)


def _merge_srt(parts: list) -> str:
    """合併多段 SRT（各段時間軸已位移），重新編號序號。"""
    blocks = []
    for part in parts:
        for block in re.split(r"\n\s*\n", part.strip()):
            lines = block.strip().splitlines()
            if len(lines) >= 2 and "-->" in lines[1 if lines[0].strip().isdigit() else 0]:
                if lines[0].strip().isdigit():
                    lines = lines[1:]  # 去掉舊序號
                blocks.append("\n".join(lines))
    return "\n\n".join(f"{i + 1}\n{b}" for i, b in enumerate(blocks)) + "\n"


SEG_NUM = re.compile(r"seg(\d+)$")


def _collect_from_results(name: str, file_ids: list, out_dir, results: dict) -> dict:
    """把已取得的 check_status 結果合併成 <name>.srt / <name>.txt。

    不做任何等待或網路呼叫——results 由呼叫端備妥（cmd_collect 用 _wait_all
    阻塞取得；duty 用單趟 check_status 取得）。沒有任何一段完成時回傳
    segments_done=0（由呼叫端決定是報錯還是略過）。
    """
    ordered = sorted(file_ids,
                     key=lambda f: int(SEG_NUM.search(f).group(1)) if SEG_NUM.search(f) else 0)
    srt_parts, txt_parts, missing = [], [], []
    total_batches = 0
    untranslated_batches = 0
    for fid in ordered:
        r = results.get(fid, {})
        if r.get("status") != "completed":
            missing.append(fid)
            continue
        n = int(SEG_NUM.search(fid).group(1)) if SEG_NUM.search(fid) else 0
        srt_parts.append(_shift_srt(r.get("srt_text", ""), n * SEGMENT_SECONDS))
        txt_parts.append(r.get("plain_text", "").strip())
        # 彙總各段翻譯統計（後端有回才累加）
        total_batches += r.get("total_batches") or 0
        untranslated_batches += r.get("untranslated_batches") or 0

    if not srt_parts:
        return {"srt": None, "txt": None, "segments_done": 0, "segments_missing": missing,
                "partial": True, "total_batches": 0, "untranslated_batches": 0,
                "partial_translation": False}

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w一-鿿.-]+', '_', name)[:80]
    srt_path = out_dir / f"{safe}.srt"
    txt_path = out_dir / f"{safe}.txt"
    srt_path.write_text(_merge_srt(srt_parts), encoding="utf-8")
    txt_path.write_text("\n\n".join(txt_parts) + "\n", encoding="utf-8")
    return {
        "srt": str(srt_path), "txt": str(txt_path),
        "segments_done": len(srt_parts), "segments_missing": missing,
        "partial": bool(missing),
        "total_batches": total_batches,
        "untranslated_batches": untranslated_batches,
        "partial_translation": untranslated_batches > 0,
    }


def cmd_collect(args):
    """等待所有切段完成 → 時間軸位移 → 合併 → 寫出 <name>.srt / <name>.txt"""
    results = _wait_all(args.file_ids, args.interval, args.max_minutes)
    out = _collect_from_results(args.name, args.file_ids, args.out_dir, results)
    if not out["segments_done"]:
        die(f"沒有任何切段完成: {out['segments_missing']}", 2)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if out["segments_missing"]:
        sys.exit(2)


def _autostatus(vdvno: str) -> dict:
    return backend(f"/auto_status/{urllib.parse.quote(vdvno)}", with_secret=True)


def cmd_autostatus(args):
    """查某 vdvno 的處理全貌（marker / session / 失敗記錄）。"""
    print(json.dumps(_autostatus(args.vdvno), ensure_ascii=False, indent=1))


# ---------- rescue（YouTube 直播補救）----------
# 背景：走 YouTube 的場次，後端（Cloud Run）的 yt-dlp 會被 YouTube 以
# 資料中心 IP 擋下，無法解出串流。但在本機/VM 執行 yt-dlp -g 可以解出
# googlevideo.com 的直連 HLS manifest（有效期約 6 小時），再餵給後端
# /start_recording，後端就能照普通 HLS 直播錄下去。

VDVNO_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")  # 與 main.py 的 VDVNO_PATTERN 同義
YTDLP_TIMEOUT = 60
# googlevideo 的 manifest 兩種寫法：/expire/<epoch>/ 與 ?expire=<epoch>
EXPIRE_PATH_RE = re.compile(r"/expire/(\d+)")
EXPIRE_QS_RE = re.compile(r"[?&]expire=(\d+)")


def _is_youtube_url(url: str) -> bool:
    """以 hostname 判定（不是字串包含），避免 evil.com/youtube.com 之類誤判。"""
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return host in ("youtube.com", "youtu.be") or host.endswith(".youtube.com")


def _find_ytdlp() -> str:
    import shutil
    path = shutil.which("yt-dlp")
    if path:
        return path
    local = Path.home() / ".local" / "bin" / "yt-dlp"
    if local.exists():
        return str(local)
    die("找不到 yt-dlp。請先安裝：pip install --user yt-dlp")


def _resolve_manifest(ytdlp: str, url: str) -> str:
    """yt-dlp -g 解出直連串流網址。

    不帶 -f：實測預設格式即可成功；直播場景釘特定 -f 反而有
    format-not-available 的風險。多行輸出（影音分離）時挑第一個 HLS。
    """
    import subprocess
    try:
        out = subprocess.run([ytdlp, "-g", "--no-warnings", url],
                             capture_output=True, timeout=YTDLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        die(f"yt-dlp 逾時（{YTDLP_TIMEOUT}s）未解出網址: {url}")
    lines = [ln.strip() for ln in out.stdout.decode(errors="replace").splitlines()
             if ln.strip().startswith("https://")]
    for ln in lines:
        if "/manifest/hls_playlist/" in ln or ".m3u8" in ln:
            return ln
    if lines:
        return lines[0]  # 沒有明顯的 HLS 特徵時退回第一個網址
    die(f"yt-dlp 未解出任何網址（exit {out.returncode}）: "
        f"{out.stderr.decode(errors='replace')[-300:].strip()}")


def _manifest_expires_taiwan(url: str):
    """從 manifest 網址解出到期時間（台灣時區字串）；解析不到回 None。"""
    m = EXPIRE_PATH_RE.search(url) or EXPIRE_QS_RE.search(url)
    if not m:
        return None
    try:
        return datetime.fromtimestamp(int(m.group(1)), TW_TZ).strftime("%Y/%m/%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return None


def _portal_video(vdvno: str) -> tuple:
    """打 SPW010_VideoData 取 (vdv_url, vdv_title)。"""
    data = portal("SPW010_VideoData", {"vdv_vdvno": vdvno})
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        die(f"SPW010_VideoData 查無 {vdvno} 的資料")
    return data.get("vdv_url", "") or "", data.get("vdv_title", "") or ""


def _rescue_once(args) -> dict:
    """解 YouTube 直連網址 → 餵後端開錄。回傳輸出用的 dict（不印出）。"""
    if not VDVNO_RE.match(args.vdvno or ""):
        die(f"vdvno 格式不符（需 8-64 碼英數或 hyphen）: {args.vdvno!r}")

    url, vdv_title = (args.url, "") if args.url else _portal_video(args.vdvno)
    if not url:
        die(f"{args.vdvno} 查無 vdv_url，無法補救")
    if not _is_youtube_url(url):
        die(f"非 YouTube 網址（{url}）——rescue 只處理 YouTube 直播；"
            f"一般場次請走 trigger 流程由後端自動錄製。")

    manifest = _resolve_manifest(_find_ytdlp(), url)
    title = args.title or vdv_title or "議會直播補救"
    body = {"stream_url": manifest, "title": title, "vdvno": args.vdvno, "mode": "speech"}
    try:
        r = backend("/start_recording", method="POST", with_secret=True, json_body=body)
    except urllib.error.HTTPError as e:
        if e.code == 409:
            # 冪等：已有錄製進行中（含自動錄製剛接手）不是錯誤
            return {"status": "already_recording", "vdvno": args.vdvno}
        raise
    return {
        "status": "recording_started",
        "session_id": r.get("session_id"),
        "vdvno": args.vdvno,
        "title": title,
        "manifest_expires_taiwan": _manifest_expires_taiwan(manifest),
    }


def _parse_until(hhmm: str, now: datetime) -> datetime:
    """把 HH:MM 解成「今天該時刻」（台灣時區）。"""
    try:
        h, m = (int(x) for x in hhmm.split(":", 1))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        die(f"--until 格式須為 HH:MM: {hhmm!r}")


def _recording_status(session_id: str) -> dict:
    return backend(f"/recording_status/{urllib.parse.quote(session_id)}")


def _rescue_follow(args, deps: dict = None) -> dict:
    """單次 rescue ＋ 監控迴圈：斷線自動重解重錄，到 --until 或正常結束收尾。

    deps 供測試注入（now / sleep / rescue_once / recording_status）。
    回傳總結 dict（由呼叫端印出）。
    """
    deps = deps or {}
    now_fn = deps.get("now", lambda: datetime.now(TW_TZ))
    sleep_fn = deps.get("sleep", time.sleep)
    rescue_fn = deps.get("rescue_once", _rescue_once)
    status_fn = deps.get("recording_status", _recording_status)

    deadline = _parse_until(args.until, now_fn())
    file_ids, restarts = [], 0
    stop_reason = "until_reached"

    def note(msg):
        print(f"[rescue][{now_fn().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)

    first = rescue_fn(args)
    session_id = first.get("session_id")
    note(f"{first.get('status')} session={session_id or '-'}")

    while True:
        if now_fn() >= deadline:
            break
        sleep_fn(args.interval)
        if now_fn() >= deadline:
            break

        if not session_id:
            # 409（別人在錄）或上輪停止後尚未接手 → 每輪重試直到拿到 session
            r = rescue_fn(args)
            if r.get("session_id"):
                session_id, restarts = r["session_id"], restarts + 1
                note(f"重新接手 session={session_id}")
            else:
                note(f"{r.get('status')}（等待中）")
            continue

        try:
            st = status_fn(session_id)
        except Exception as e:
            note(f"查詢 {session_id} 失敗: {e}")
            continue

        for fid in st.get("file_ids") or []:
            if fid not in file_ids:
                file_ids.append(fid)
        note(f"session={session_id} 段數={len(st.get('file_ids') or [])} 狀態={st.get('status')}")

        if st.get("status") != "stopped":
            continue
        if not st.get("error"):
            stop_reason = "stopped"  # 正常收尾（直播結束）
            break
        # 斷線（manifest 到期／串流中斷）→ 重解新網址重錄。
        # 不做「到期前搶跑」：舊 session 還持有 marker 必吃 409，只在停止後補。
        note(f"session={session_id} 中斷（{st.get('error')}）→ 重新補救")
        r = rescue_fn(args)
        if r.get("session_id"):
            session_id, restarts = r["session_id"], restarts + 1
            note(f"已重新開錄 session={session_id}")
        else:
            session_id = None  # 409：marker 尚未釋放，下輪再試

    return {
        "status": "follow_finished",
        "vdvno": args.vdvno,
        "stop_reason": stop_reason,
        "session_id": session_id,
        "restarts": restarts,
        "file_ids": file_ids,
        "until_taiwan": deadline.strftime("%Y/%m/%d %H:%M"),
    }


def cmd_rescue(args):
    if args.follow:
        print(json.dumps(_rescue_follow(args), ensure_ascii=False, indent=1))
        return
    print(json.dumps(_rescue_once(args), ensure_ascii=False, indent=1))


def cmd_recstatus(args):
    """查錄製 session 狀態（含 file_ids，供 wait / collect）。"""
    print(json.dumps(_recording_status(args.session_id), ensure_ascii=False, indent=1))


def _today() -> dict:
    """今天概況：頻道直播旗標、OnAir 清單、今日+昨日 VOD。"""
    now = datetime.now(TW_TZ)
    out = {"now_taiwan": now.strftime("%Y/%m/%d %H:%M"), "onair": [], "channels_live": [], "vods": []}
    channels = portal("SPW002_vdoTypeList")
    out["channels_live"] = [c["vdt_title"] for c in channels if c.get("vdt_islive") == "Y"]
    try:
        out["onair"] = portal("SPW003_OnAirList")
    except Exception as e:
        out["onair_error"] = str(e)
    for d in (now, now - timedelta(days=1)):
        date_str = d.strftime("%Y/%m/%d")
        for ch in channels:
            try:
                vods = portal("SPW046_VideoList",
                              {"vdt_vdtno": ch["vdt_vdtno"], "vdv_opendate": date_str})
            except Exception:
                continue
            for v in vods or []:
                out["vods"].append({
                    "date": date_str, "channel": ch["vdt_title"],
                    "vdvno": v.get("vdv_vdvno"), "title": v.get("vdv_title"),
                })
    return out


def cmd_today(_args):
    print(json.dumps(_today(), ensure_ascii=False, indent=1))


def _send_mail(to, subject: str, body: str, attach: list = None) -> dict:
    """寄信（SMTP）。to 可以是字串（逗號分隔）或清單。回傳結果 dict。"""
    host = CFG.get("SMTP_HOST")
    if not host:
        die("缺少 SMTP 設定（SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM）")
    port = int(CFG.get("SMTP_PORT", "587"))
    user = CFG.get("SMTP_USER", "")
    # Gmail 應用程式密碼顯示時帶空格但實際不含空格，貼上時常誤帶——自動去除
    password = CFG.get("SMTP_PASS", "").replace(" ", "")
    sender = CFG.get("MAIL_FROM", user)
    raw = to if isinstance(to, str) else ",".join(to)
    recipients = [a.strip() for a in raw.split(",") if a.strip()]
    if not recipients:
        die("沒有有效收件人")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), subject
    msg.set_content(body)

    for path in attach or []:
        p = Path(path)
        if not p.exists():
            die(f"附件不存在: {path}")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)

    ctx = _ssl_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)
    return {"sent": True, "to": recipients,
            "attachments": [Path(a).name for a in (attach or [])]}


def cmd_mail(args):
    body = args.body
    if body == "-":
        body = sys.stdin.read()
    print(json.dumps(_send_mail(args.to, args.subject, body, args.attach),
                     ensure_ascii=False))


# ---------- duty（自動值班協調器）----------
# 系統的手腳（trigger/rescue/collect/mail）本來各自獨立，串聯靠外部 LLM Agent
# 的自覺執行，實證不可靠（錄製成功但寄信每次要人工催）。duty 把「偵測→追蹤→
# 合併→寄信→日報」做成單次執行、冪等、零 LLM 的命令，由 cron 每 15 分鐘呼叫。
#
# 設計要點：
#   * 單趟不阻塞——不用 wait 的輪詢迴圈，每輪只對每個 file_id 打一次
#     check_status（該呼叫本身即觸發翻譯），下輪再看。
#   * 狀態全落地 duty_state.json；讀取失敗一律視為空狀態重建，絕不 crash
#     （狀態檔壞掉的代價是重跑一輪，crash 的代價是值班鏈整條斷掉）。
#   * 所有外部呼叫走 deps 字典注入，測試可全 mock。

DUTY_STATE_VERSION = 1
DUTY_PARTIAL_DELIVER_HOURS = 6    # 部分完成滿此時數仍照寄（partial）
DUTY_RESCUE_FAIL_ALERT = 2        # 連續幾輪 rescue 失敗才告警
DUTY_ALERT_MAX_PER_DAY = 2        # 同一事由每日告警上限
DUTY_DONE_RETENTION_DAYS = 30     # done 清單保留天數（供日報統計）
DUTY_REPORT_HOUR = 22             # 日報時間（台灣，當日 >= 此小時發一次）
DUTY_YT_RESCUE_REASON = "YouTube 直播無法後端錄製"


def _duty_state_path() -> Path:
    return KIT_DIR / "duty_state.json"


def _duty_blank_state() -> dict:
    return {"version": DUTY_STATE_VERSION, "tracking": {}, "done": [],
            "alerts": {}, "last_report_date": None}


def _duty_load_state(path) -> dict:
    """讀取狀態檔。**任何毀損都退回空狀態**（缺檔/壞 JSON/型別不符皆同）。"""
    state = _duty_blank_state()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return state
    if not isinstance(raw, dict):
        return state
    for key in ("tracking", "done", "alerts"):
        if isinstance(raw.get(key), type(state[key])):
            state[key] = raw[key]
    if isinstance(raw.get("last_report_date"), str):
        state["last_report_date"] = raw["last_report_date"]
    # tracking 內每筆也要是 dict，否則丟棄該筆（不讓單筆毀損污染整輪）
    state["tracking"] = {k: v for k, v in state["tracking"].items() if isinstance(v, dict)}
    return state


def _duty_save_state(path, state: dict) -> None:
    """原子寫入（先寫暫存再 replace），避免中途被 kill 留下半截 JSON。"""
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def _duty_emails(key: str) -> list:
    return [a.strip() for a in (CFG.get(key, "") or "").split(",") if a.strip()]


def _rescue_vdvno(vdvno: str, title: str = "") -> dict:
    """給 duty 用的 rescue 包裝：組出 _rescue_once 需要的 args namespace。"""
    import types as _types
    return _rescue_once(_types.SimpleNamespace(
        vdvno=vdvno, url=None, title=title or None, follow=False))


def _duty_default_deps() -> dict:
    return {
        "now": lambda: datetime.now(TW_TZ),
        "trigger": _trigger,
        "autostatus": _autostatus,
        "today": _today,
        "rescue": _rescue_vdvno,
        "recstatus": _recording_status,
        "check_status": _check_status,
        "collect": _collect_from_results,
        "mail": _send_mail,
        "fetch_vod": _fetch_vod,
        "state_path": _duty_state_path(),
        "out_dir": KIT_DIR / "output",
    }


class _Duty:
    """單趟值班的執行體。每個步驟獨立 try/except——任一步壞掉不影響其他步驟。"""

    def __init__(self, deps: dict):
        self.deps = deps
        self.now = deps["now"]()
        self.today_str = self.now.strftime("%Y-%m-%d")
        self.state = _duty_load_state(deps["state_path"])
        self.counts = {"vods_tracked": 0, "segments_learned": 0, "rescued": 0,
                       "sessions_finalized": 0, "delivered": 0, "orphans_retried": 0,
                       "alerts_sent": 0, "report_sent": 0, "errors": 0}
        self._onair_cache = None   # None=未查，list=已查（可能為空）
        self._notes = []

    # --- 小工具 ---

    def note(self, msg: str):
        line = f"[duty][{self.now.strftime('%H:%M:%S')}] {msg}"
        self._notes.append(msg)
        print(line, file=sys.stderr)

    def onair(self) -> list:
        """本輪的 onair 清單（快取，每趟最多查一次議會 API）。

        查詢失敗時回傳「非空」哨兵——寧可當作「可能在開會」，也不要把查詢
        失敗誤判成「尚未開播」而讓真正的 rescue 失敗被靜音。
        """
        if self._onair_cache is None:
            try:
                self._onair_cache = (self.deps["today"]() or {}).get("onair") or []
            except Exception as e:
                self.note(f"today 查詢失敗（視為可能開會中）: {e}")
                self._onair_cache = [{"_unknown": True}]
        return self._onair_cache

    def is_done(self, vdvno: str) -> bool:
        """已交付過就不再重新追蹤——這是「不重寄」的關鍵防線。

        trigger 可能因後端 marker 被回收而再次回報同一場次；若照單全收會
        重新追蹤、重新 collect、重新寄信。done 清單保留 30 天正是為此。
        """
        return any(isinstance(d, dict) and d.get("vdvno") == vdvno
                   for d in self.state["done"])

    def track(self, vdvno: str, title: str, source: str):
        if self.is_done(vdvno):
            self.note(f"{vdvno} 已於先前交付（done），略過")
            return None
        t = self.state["tracking"].get(vdvno)
        if t is None:
            t = {"vdvno": vdvno, "title": title or vdvno, "source": source,
                 "first_seen": self.now.timestamp(), "file_ids": [],
                 "file_ids_at": None, "sessions": [], "finished_sessions": [],
                 "rescue_fail_streak": 0, "vod_failure": None}
            self.state["tracking"][vdvno] = t
            self.note(f"開始追蹤 {vdvno}（{t['title']}，來源 {source}）")
        if title and t.get("title") in (None, "", vdvno):
            t["title"] = title
        return t

    def add_file_ids(self, t: dict, file_ids: list):
        added = [f for f in file_ids or [] if f and f not in t["file_ids"]]
        if not added:
            return
        t["file_ids"].extend(added)
        if not t.get("file_ids_at"):
            t["file_ids_at"] = self.now.timestamp()   # 6 小時 partial 時鐘從此起算
        self.counts["segments_learned"] += len(added)
        self.note(f"{t['vdvno']} 取得 {len(added)} 段（累計 {len(t['file_ids'])}）")

    def alert(self, key: str, subject: str, body: str):
        """告警信（節流：同一事由每日最多 DUTY_ALERT_MAX_PER_DAY 封）。"""
        rec = self.state["alerts"].get(key)
        if not isinstance(rec, dict) or rec.get("date") != self.today_str:
            rec = {"date": self.today_str, "count": 0}
        self.state["alerts"][key] = rec
        if rec["count"] >= DUTY_ALERT_MAX_PER_DAY:
            self.note(f"告警 {key} 已達今日上限，略過")
            return False
        admins = _duty_emails("ADMIN_EMAILS")
        if not admins:
            self.note(f"告警 {key} 無 ADMIN_EMAILS 可寄")
            return False
        try:
            self.deps["mail"](admins, subject, body)
            rec["count"] += 1
            self.counts["alerts_sent"] += 1
            self.note(f"已發告警 {key}")
            return True
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"告警 {key} 寄送失敗: {e}")
            return False

    # --- 步驟 ---

    def step_trigger(self) -> dict:
        try:
            result = self.deps["trigger"]() or {}
            self.note(f"trigger 完成：live_started={len(result.get('live_started') or [])} "
                      f"live_failed={len(result.get('live_failed') or [])} "
                      f"vods_queued={len(result.get('vods_queued') or [])}")
            return result
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"trigger 失敗: {e}")
            self.alert("trigger_failed", "[議會值班] trigger 失敗",
                       f"duty 於 {self.now:%Y/%m/%d %H:%M} 執行 trigger 失敗：\n{e}")
            return {}

    def step_track_vods(self, trig: dict):
        """新 VOD 入列；已追蹤但還沒段數的，查 autostatus 補段數。"""
        for entry in trig.get("vods_queued") or []:
            vdvno = entry.get("vdvno")
            if not vdvno:
                continue
            if self.track(vdvno, entry.get("title") or "", "vod"):
                self.counts["vods_tracked"] += 1

        # 直播開錄的場次也追蹤：否則自動錄成功的直播永遠沒人收尾寄信
        for entry in trig.get("live_started") or []:
            vdvno = entry.get("vdvno")
            if not vdvno:
                continue
            t = self.track(vdvno, entry.get("title") or "", "live_auto")
            if not t:
                continue
            sid = entry.get("session_id")
            if sid and sid not in t["sessions"]:
                t["sessions"].append(sid)

        for vdvno, t in list(self.state["tracking"].items()):
            if t.get("file_ids") or t.get("source") != "vod":
                continue
            try:
                st = self.deps["autostatus"](vdvno) or {}
            except Exception as e:
                self.note(f"{vdvno} autostatus 查詢失敗: {e}")
                continue
            fids = (st.get("vod_marker") or {}).get("file_ids") or []
            if fids:
                self.add_file_ids(t, fids)
                t["vod_failure"] = None
                continue
            failure = st.get("vod_failure")
            if failure:
                # 不告警：官方尚未上架等原因系統會自動重試，屬正常等待
                t["vod_failure"] = failure.get("reason") or str(failure)
                self.note(f"{vdvno} VOD 尚未取得段數：{t['vod_failure']}")

    def step_rescue(self, trig: dict):
        """live_failed 中的 YouTube 場次補救，含殭屍防護。"""
        for entry in trig.get("live_failed") or []:
            reason = ((entry.get("last_failure") or {}).get("reason") or "")
            if not reason.startswith(DUTY_YT_RESCUE_REASON):
                self.note(f"{entry.get('vdvno')} live_failed（非 YouTube）：{reason}")
                continue
            vdvno = entry.get("vdvno")
            if not vdvno:
                continue
            t = self.track(vdvno, entry.get("title") or "", "live_rescue")
            if t:
                self.do_rescue(t)

    def do_rescue(self, t: dict) -> bool:
        vdvno = t["vdvno"]
        try:
            r = self.deps["rescue"](vdvno, t.get("title") or "")
        except (Exception, SystemExit) as e:
            self.on_rescue_failed(t, e)
            return False
        t["rescue_fail_streak"] = 0
        t["pending_not_started"] = False
        sid = r.get("session_id") if isinstance(r, dict) else None
        if sid and sid not in t["sessions"]:
            t["sessions"].append(sid)
        self.counts["rescued"] += 1
        self.note(f"{vdvno} rescue：{(r or {}).get('status')} session={sid or '-'}")
        return True

    def on_rescue_failed(self, t: dict, err):
        """殭屍防護：沒有任何 onair ⇒ 場次根本還沒開播，安靜等下輪，不算失敗。

        早晨 trigger 的 live_failed 會列出當天稍晚才開播的場次，對其 rescue
        必然失敗（yt-dlp 解不出未開始的串流）。把這種必然失敗計入告警只會
        製造每日噪音，讓真正的失敗被淹沒。
        """
        vdvno = t["vdvno"]
        if not self.onair():
            t["pending_not_started"] = True
            self.note(f"{vdvno} rescue 失敗但目前無任何 onair → 尚未開播，下輪再試（不計失敗）")
            return
        t["pending_not_started"] = False
        t["rescue_fail_streak"] = int(t.get("rescue_fail_streak") or 0) + 1
        streak = t["rescue_fail_streak"]
        self.note(f"{vdvno} rescue 失敗（連續第 {streak} 輪）: {err}")
        if streak >= DUTY_RESCUE_FAIL_ALERT:
            self.alert(
                f"rescue_failed:{vdvno}",
                f"[議會值班] rescue 連續失敗 {streak} 輪：{t.get('title') or vdvno}",
                f"vdvno: {vdvno}\n標題: {t.get('title')}\n"
                f"時間: {self.now:%Y/%m/%d %H:%M}\n錯誤: {err}\n\n"
                f"直播確實在進行中（onair 非空）但補救失敗，請人工確認。",
            )

    def step_sessions(self):
        """監控 rescue / 自動錄製開出的 session：斷線接續，結束收尾。"""
        for vdvno, t in list(self.state["tracking"].items()):
            for sid in list(t.get("sessions") or []):
                if sid in (t.get("finished_sessions") or []):
                    continue
                try:
                    st = self.deps["recstatus"](sid) or {}
                except Exception as e:
                    self.note(f"{sid} recstatus 查詢失敗: {e}")
                    continue
                if st.get("status") == "recording":
                    self.note(f"{sid} 錄製中（{len(st.get('file_ids') or [])} 段）")
                    continue
                # stopped：已產出的段先收進來（無論是否要續錄）
                self.add_file_ids(t, st.get("file_ids") or [])
                t.setdefault("finished_sessions", []).append(sid)
                self.counts["sessions_finalized"] += 1
                if self.onair() and t.get("source") == "live_rescue":
                    self.note(f"{sid} 已停止但會議仍在進行 → 重新補救接續")
                    self.do_rescue(t)
                else:
                    self.note(f"{sid} 已停止且會議已結束 → 收尾")

    def step_deliver(self):
        """對每個追蹤中場次打 check_status（同時觸發翻譯），完成即合併寄出。"""
        for vdvno, t in list(self.state["tracking"].items()):
            file_ids = t.get("file_ids") or []
            if not file_ids:
                continue
            # 錄製中的場次段數還會增加，等 session 收尾再交付，避免寄出半場
            if any(s not in (t.get("finished_sessions") or []) for s in (t.get("sessions") or [])):
                self.note(f"{vdvno} 仍有 session 進行中，暫不交付")
                continue

            results = {}
            for fid in file_ids:
                try:
                    results[fid] = self.deps["check_status"](fid) or {}
                except Exception as e:
                    results[fid] = {"status": "error"}
                    self.note(f"{fid} check_status 失敗: {e}")
            done = [f for f in file_ids if results.get(f, {}).get("status") == "completed"]
            all_done = len(done) == len(file_ids)

            started = t.get("file_ids_at") or self.now.timestamp()
            aged = (self.now.timestamp() - started) >= DUTY_PARTIAL_DELIVER_HOURS * 3600
            if not all_done and not (done and aged):
                self.note(f"{vdvno} 進度 {len(done)}/{len(file_ids)}，等下輪")
                continue

            self.deliver(t, results, all_done, aged)

    def deliver(self, t: dict, results: dict, all_done: bool, aged: bool):
        vdvno, title = t["vdvno"], (t.get("title") or vdvno)
        try:
            out = self.deps["collect"](title, t["file_ids"], self.deps["out_dir"], results)
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"{vdvno} collect 失敗: {e}")
            self.alert(f"collect_failed:{vdvno}", f"[議會值班] collect 失敗：{title}",
                       f"vdvno: {vdvno}\n錯誤: {e}")
            return
        if not out.get("segments_done"):
            self.note(f"{vdvno} 無任何完成切段，暫不交付")
            return

        subject = f"[議會字幕] {title} {self.now:%Y/%m/%d}"
        if out.get("partial"):
            subject += "（部分結果）"
        if out.get("partial_translation"):
            subject += "（含未翻譯段落）"

        body = [f"會議標題：{title}", f"日期：{self.now:%Y/%m/%d}",
                f"vdvno：{vdvno}",
                f"切段數：{out['segments_done']}/{len(t['file_ids'])}"]
        if out.get("partial"):
            body.append(f"缺少切段：{', '.join(out.get('segments_missing') or [])}"
                        + ("（已達 6 小時上限，先寄出已完成部分）" if aged and not all_done else ""))
        if out.get("partial_translation"):
            body.append(f"未翻譯批次：{out.get('untranslated_batches')}/{out.get('total_batches')}"
                        f"（該部分已附原文，非缺件）")
        body.append("\n本信由自動值班程式 duty 寄出。")

        attach = [p for p in (out.get("srt"), out.get("txt")) if p]
        try:
            self.deps["mail"](_duty_emails("RESULT_EMAILS"), subject, "\n".join(body), attach)
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"{vdvno} 寄送失敗（保留追蹤，下輪重試）: {e}")
            self.alert(f"mail_failed:{vdvno}", f"[議會值班] 字幕寄送失敗：{title}",
                       f"vdvno: {vdvno}\n錯誤: {e}")
            return

        self.counts["delivered"] += 1
        self.note(f"{vdvno} 已寄出：{subject}")
        self.state["done"].append({
            "vdvno": vdvno, "title": title, "done_at": self.now.timestamp(),
            "date": self.today_str, "segments": out["segments_done"],
            "partial": bool(out.get("partial")),
            "partial_translation": bool(out.get("partial_translation")),
        })
        self.state["tracking"].pop(vdvno, None)

        if aged and not all_done:
            self.alert(f"partial_deliver:{vdvno}",
                       f"[議會值班] 逾時部分交付：{title}",
                       f"vdvno: {vdvno}\n已滿 {DUTY_PARTIAL_DELIVER_HOURS} 小時仍未全部完成，"
                       f"已先寄出 {out['segments_done']}/{len(t['file_ids'])} 段。\n"
                       f"缺少：{', '.join(out.get('segments_missing') or [])}")

    def step_orphans(self):
        """掃描窗孤兒：VOD 已排入但官方遲遲未上架的場次，指名重試不受窗限制。"""
        for vdvno, t in list(self.state["tracking"].items()):
            if t.get("file_ids") or not t.get("vod_failure"):
                continue
            try:
                r = self.deps["fetch_vod"](vdvno)
                self.counts["orphans_retried"] += 1
                self.note(f"{vdvno} 孤兒補抓：{r}")
            except (Exception, SystemExit) as e:
                self.note(f"{vdvno} 孤兒補抓失敗: {e}")

    def step_report(self):
        """每日日報：當日 22:00 後首次執行才發。"""
        if self.now.hour < DUTY_REPORT_HOUR:
            return
        if self.state.get("last_report_date") == self.today_str:
            return
        today_done = [d for d in self.state["done"] if d.get("date") == self.today_str]
        pending = []
        for vdvno, t in self.state["tracking"].items():
            bits = [f"{t.get('title') or vdvno}（{vdvno}）"]
            if t.get("file_ids"):
                bits.append(f"{len(t['file_ids'])} 段處理中")
            elif t.get("vod_failure"):
                bits.append(f"等待上架：{t['vod_failure']}")
            elif t.get("pending_not_started"):
                bits.append("尚未開播，待補救")
            else:
                bits.append("等待段數")
            pending.append(" — ".join(bits))
        lines = [
            f"📋 議會錄製日報 {self.now:%Y/%m/%d}",
            f"- 追蹤中場次：{len(self.state['tracking'])}",
            f"- 今日字幕寄出：{len(today_done)} 部"
            + (f"（{'、'.join(d['title'] for d in today_done)}）" if today_done else ""),
            f"- 待處理/異常：{len(pending)}",
        ]
        lines += [f"  · {p}" for p in pending] or ["  · 無"]
        lines.append("\n本報表由自動值班程式 duty 產生。")
        admins = _duty_emails("ADMIN_EMAILS")
        if not admins:
            self.note("無 ADMIN_EMAILS，略過日報")
            return
        try:
            self.deps["mail"](admins, f"[議會值班] 日報 {self.now:%Y/%m/%d}", "\n".join(lines))
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"日報寄送失敗: {e}")
            return
        self.state["last_report_date"] = self.today_str
        self.counts["report_sent"] = 1
        self.note("日報已寄出")

    def prune(self):
        cutoff = self.now.timestamp() - DUTY_DONE_RETENTION_DAYS * 86400
        self.state["done"] = [d for d in self.state["done"]
                              if isinstance(d, dict) and (d.get("done_at") or 0) >= cutoff]
        self.state["alerts"] = {k: v for k, v in self.state["alerts"].items()
                                if isinstance(v, dict) and v.get("date") == self.today_str}

    def run(self) -> dict:
        trig = self.step_trigger()
        for step in (lambda: self.step_track_vods(trig), lambda: self.step_rescue(trig),
                     self.step_sessions, self.step_deliver, self.step_orphans,
                     self.step_report):
            try:
                step()
            except Exception as e:
                self.counts["errors"] += 1
                self.note(f"步驟 {getattr(step, '__name__', 'step')} 例外: {e}")
        self.prune()
        try:
            _duty_save_state(self.deps["state_path"], self.state)
        except Exception as e:
            self.counts["errors"] += 1
            self.note(f"狀態寫入失敗: {e}")
        return {"now_taiwan": self.now.strftime("%Y/%m/%d %H:%M"),
                "tracking": len(self.state["tracking"]), **self.counts}


def _duty_run(deps: dict = None) -> dict:
    d = _duty_default_deps()
    d.update(deps or {})
    return _Duty(d).run()


def cmd_duty(args):
    deps = {}
    if getattr(args, "state", None):
        deps["state_path"] = args.state
    if getattr(args, "out_dir", None):
        deps["out_dir"] = args.out_dir
    print(json.dumps(_duty_run(deps), ensure_ascii=False))


# ---------- 進入點 ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health").set_defaults(func=cmd_health)
    sub.add_parser("trigger").set_defaults(func=cmd_trigger)

    p = sub.add_parser("status")
    p.add_argument("file_id")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("wait")
    p.add_argument("file_ids", nargs="+")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--max-minutes", type=int, default=240)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("collect")
    p.add_argument("name", help="輸出檔名（通常用會議標題）")
    p.add_argument("file_ids", nargs="+")
    p.add_argument("--out-dir", default=str(KIT_DIR / "output"))
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--max-minutes", type=int, default=240)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("autostatus")
    p.add_argument("vdvno")
    p.set_defaults(func=cmd_autostatus)

    p = sub.add_parser("rescue", help="YouTube 直播補救（本機解直連網址餵後端開錄）")
    p.add_argument("vdvno")
    p.add_argument("--url", help="直接指定 YouTube 網址（預設打 SPW010 取 vdv_url）")
    p.add_argument("--title", help="會議標題（預設用 vdv_title）")
    p.add_argument("--follow", action="store_true",
                   help="前景監控：斷線自動重解重錄，到 --until 或直播結束才收尾")
    p.add_argument("--until", default="19:00", help="--follow 的停止時間 HH:MM（台灣，預設 19:00）")
    p.add_argument("--interval", type=int, default=600, help="--follow 的監控間隔秒（預設 600）")
    p.set_defaults(func=cmd_rescue)

    p = sub.add_parser("recstatus", help="查錄製 session 狀態（含 file_ids）")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_recstatus)

    sub.add_parser("today").set_defaults(func=cmd_today)

    p = sub.add_parser("fetchvod", help="指名補抓單場 VOD（不受今天+昨天掃描窗限制）")
    p.add_argument("vdvno")
    p.set_defaults(func=cmd_fetchvod)

    p = sub.add_parser("duty", help="自動值班單趟（cron 每 15 分鐘呼叫；冪等）")
    p.add_argument("--state", help=f"狀態檔路徑（預設 {_duty_state_path()}）")
    p.add_argument("--out-dir", dest="out_dir", help="字幕輸出目錄（預設 output/）")
    p.set_defaults(func=cmd_duty)

    p = sub.add_parser("mail")
    p.add_argument("--to", required=True, help="收件人（逗號分隔）")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True, help="內文；'-' 表示從 stdin 讀")
    p.add_argument("--attach", action="append", help="附件路徑（可重複）")
    p.set_defaults(func=cmd_mail)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
