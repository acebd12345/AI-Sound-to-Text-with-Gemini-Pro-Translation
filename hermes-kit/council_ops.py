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


def http_json(url: str, method: str = "GET", headers: dict = None, timeout: int = 30):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        raw = resp.read()
    return _decode_json(raw)


def backend(path: str, method: str = "GET", with_secret: bool = False, timeout: int = 60):
    if not CFG.get("SYSTEM_URL"):
        die("缺少 SYSTEM_URL（設定環境變數或 hermes-kit/.env）")
    headers = {}
    if with_secret:
        secret = CFG.get("AUTO_TRIGGER_SECRET", "")
        if not secret:
            die("缺少 AUTO_TRIGGER_SECRET（設定環境變數或 hermes-kit/secret.txt）")
        headers["X-Trigger-Secret"] = secret
    return http_json(CFG["SYSTEM_URL"] + path, method=method, headers=headers, timeout=timeout)


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
