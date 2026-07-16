#!/usr/bin/env python3
"""council_ops.py — 議會自動錄製值班工具（給 Hermes Agent 的手腳）

只用 Python 標準庫，無需 pip install。設定讀取順序：環境變數 → 同目錄 .env。

子命令：
  health                        後端健康檢查
  trigger                       打 /auto_record_check（掃直播+VOD，冪等）
  status <file_id>              查單一 file_id 的轉錄翻譯進度
  wait <file_id...>             輪詢多個 file_id 直到全部完成（觸發翻譯靠這個）
  collect <輸出名> <file_id...>  等待+合併多切段字幕（時間軸自動位移），輸出 .srt/.txt
  autostatus <vdvno>            查某 vdvno 的處理全貌（marker/session/失敗記錄）
  rescue <vdvno> [--follow]     YouTube 直播補救：本機解直連網址餵後端開錄
  today                         今天的直播與 VOD 概況（議會公開 API）
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


def cmd_trigger(_args):
    result = backend("/auto_record_check", method="POST", with_secret=True, timeout=120)
    print(json.dumps(result, ensure_ascii=False, indent=1))


def cmd_status(args):
    r = backend(f"/check_status/{urllib.parse.quote(args.file_id)}?total_chunks=1")
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
                r = backend(f"/check_status/{urllib.parse.quote(fid)}?total_chunks=1")
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


def cmd_collect(args):
    """等待所有切段完成 → 時間軸位移 → 合併 → 寫出 <name>.srt / <name>.txt"""
    results = _wait_all(args.file_ids, args.interval, args.max_minutes)
    ordered = sorted(args.file_ids,
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
        die(f"沒有任何切段完成: {missing}", 2)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^\w一-鿿.-]+', '_', args.name)[:80]
    srt_path = out_dir / f"{safe}.srt"
    txt_path = out_dir / f"{safe}.txt"
    srt_path.write_text(_merge_srt(srt_parts), encoding="utf-8")
    txt_path.write_text("\n\n".join(txt_parts) + "\n", encoding="utf-8")
    print(json.dumps({
        "srt": str(srt_path), "txt": str(txt_path),
        "segments_done": len(srt_parts), "segments_missing": missing,
        "partial": bool(missing),
        "total_batches": total_batches,
        "untranslated_batches": untranslated_batches,
        "partial_translation": untranslated_batches > 0,
    }, ensure_ascii=False, indent=1))
    if missing:
        sys.exit(2)


def cmd_autostatus(args):
    """查某 vdvno 的處理全貌（marker / session / 失敗記錄）。"""
    r = backend(f"/auto_status/{urllib.parse.quote(args.vdvno)}", with_secret=True)
    print(json.dumps(r, ensure_ascii=False, indent=1))


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


def cmd_today(_args):
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
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_mail(args):
    host = CFG.get("SMTP_HOST")
    if not host:
        die("缺少 SMTP 設定（SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM）")
    port = int(CFG.get("SMTP_PORT", "587"))
    user = CFG.get("SMTP_USER", "")
    # Gmail 應用程式密碼顯示時帶空格但實際不含空格，貼上時常誤帶——自動去除
    password = CFG.get("SMTP_PASS", "").replace(" ", "")
    sender = CFG.get("MAIL_FROM", user)
    recipients = [a.strip() for a in args.to.split(",") if a.strip()]
    if not recipients:
        die("--to 沒有有效收件人")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(recipients), args.subject
    body = args.body
    if body == "-":
        body = sys.stdin.read()
    msg.set_content(body)

    for path in args.attach or []:
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
    print(json.dumps({"sent": True, "to": recipients,
                      "attachments": [Path(a).name for a in (args.attach or [])]},
                     ensure_ascii=False))


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

    sub.add_parser("today").set_defaults(func=cmd_today)

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
