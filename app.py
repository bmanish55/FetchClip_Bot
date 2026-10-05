"""FetchClip - Telegram media fetcher, Vercel (webhook) edition."""
import asyncio
import hmac
import html
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from telegram import (
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup as Markup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("mediabot")

# ---------- Settings ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
BOT_NAME = "FetchClip"
MAX_MB = int(os.getenv("MAX_MB", "100"))  # Internal yt-dlp ceiling raised
LIMIT = MAX_MB * 1024 * 1024
LOW_HEIGHT = int(os.getenv("LOW_HEIGHT", "720")) # 720p minimum required for Reddit
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "2"))
USER_COOLDOWN = int(os.getenv("USER_COOLDOWN", "10"))
CHECK_COOLDOWN = int(os.getenv("CHECK_COOLDOWN", "3"))
COOKIES_FILE = "/tmp/cookies.txt"
_yt_cookies = os.getenv("YT_COOKIES", "").strip()
if _yt_cookies:
    if "\n" not in _yt_cookies and "\\n" in _yt_cookies:
        _yt_cookies = _yt_cookies.replace("\\n", "\n")
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as _f:
            _f.write(_yt_cookies + "\n")
    except OSError:
        pass
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
INLINE_PROCESSING = os.getenv("INLINE_PROCESSING", "0") == "1"

ALLOWED_DOMAINS = [d.strip().lower() for d in os.getenv("ALLOWED_DOMAINS", "").split(",") if d.strip()]

FETCH_TIMEOUT = 40
DOWNLOAD_TIMEOUT = 150
UPLOAD_TIMEOUT = 100

UNKNOWN_BYTES_PER_SEC = 100_000
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
sem = asyncio.Semaphore(MAX_PARALLEL)
active_users: set[int] = set()
last_download: dict[int, float] = {}
last_check: dict[int, float] = {}
seen_updates: deque[int] = deque(maxlen=500)


def find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None

    def works(path: str) -> bool:
        try:
            return subprocess.run([path, "-version"], capture_output=True, timeout=10).returncode == 0
        except Exception:
            return False

    if works(src):
        return src
    try:
        dst = os.path.join(tempfile.gettempdir(), "ffmpeg-bin")
        shutil.copy(src, dst)
        os.chmod(dst, 0o755)
        return dst if works(dst) else None
    except Exception:
        return None


FFMPEG = find_ffmpeg()
HAVE_FFMPEG = FFMPEG is not None
log.info("ffmpeg available: %s", HAVE_FFMPEG)


class UserError(Exception):
    pass


class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): log.debug("yt-dlp: %s", msg)


# ---------- URL helpers ----------

def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,]") if m else None

def _ip_ok(ip) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)

def is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host or host == "localhost" or host.endswith((".local", ".internal")):
            return False
        try:
            return _ip_ok(ipaddress.ip_address(host))
        except ValueError:
            return True
    except ValueError:
        return False

def resolves_public(url: str) -> bool:
    host = urlparse(url).hostname or ""
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if not _ip_ok(ipaddress.ip_address(sockaddr[0])):
                return False
    except (socket.gaierror, ValueError):
        return True
    return True

def is_allowed_domain(url: str) -> bool:
    if not ALLOWED_DOMAINS:
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)

def clean_error(err: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(err))
    low = msg.lower()
    if any(k in low for k in ("cloudflare", "anti-bot", "http error 403", "forbidden", "not a bot")):
        return "YouTube is blocking my server right now. Please try again later."
    if any(k in low for k in ("login", "sign in", "cookies", "private", "authentication")):
        return "This link needs a login or is private, so I can't download it."
    if "unsupported url" in low:
        return "This link isn't supported."
    if "javascript runtime" in low or "not available" in low:
        return "This link is currently not working with my server. Try another one."
    if "unable to download webpage" in low or "timed out" in low:
        return "Couldn't reach that link. Check it and try again."
    return "Couldn't read that link. It may be removed, private or blocked."

# ---------- Multi-Instance Cobalt Helper ----------

async def call_cobalt(url: str, mode: str) -> str:
    """Cycles through Cobalt instances to bypass Turnstile bot protection and returns direct URL."""
    instances = [
        "https://cobalt-api.hyper.lol/",
        "https://cobalt.api.timelessnesses.me/",
        "https://api.cobalt.tools/"
    ]
    req_body = {
        "url": url, 
        "videoQuality": "720", 
        "downloadMode": "audio" if mode == "audio" else "auto"
    }
    headers = {
        "Accept": "application/json", 
        "Content-Type": "application/json",
        "User-Agent": "FetchClip/1.1"
    }
    
    last_err = "Unknown error"
    
    # NOTE: verify=False strictly ignores self-signed certificate errors from community servers
    async with httpx.AsyncClient(timeout=25.0, verify=False) as client:
        for api_url in instances:
            try:
                res = await client.post(api_url, json=req_body, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("status") in ["tunnel", "redirect"] and data.get("url"):
                        return data["url"]
                    elif data.get("status") == "error":
                        last_err = data.get("error", {}).get("code", "Blocked by Bot Protection")
            except Exception as e:
                last_err = str(e)
                continue
                
    raise UserError(f"All API servers are currently blocking requests. (Last error: {last_err})")


# ---------- Size check & quality choice ----------

def has_v(f: dict) -> bool:
    return f.get("vcodec") != "none"

def has_a(f: dict) -> bool:
    return f.get("acodec") != "none"

def fsize(f: dict, duration: float) -> int | None:
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None

def qual(f: dict) -> int:
    w, h = f.get("width"), f.get("height")
    return min(w, h) if w and h else (h or 0)

def pick_video(cands: list[dict]) -> dict | None:
    if not cands: return None
    def avc(f): return (f.get("vcodec") or "").startswith("avc1")
    low = [f for f in cands if 0 < qual(f) <= LOW_HEIGHT]
    if low:
        return max(low, key=lambda f: (qual(f), avc(f), f.get("ext") == "mp4", f.get("tbr") or 0))
    return None

def pick_audio(fmts: list[dict]) -> dict | None:
    a = [f for f in fmts if has_a(f) and not has_v(f)]
    if not a: return None
    def br(f): return f.get("abr") or f.get("tbr") or 0
    ok = [f for f in a if br(f) <= 130]
    if ok:
        return max(ok, key=lambda f: (f.get("ext") == "m4a", br(f)))
    return min(a, key=lambda f: (br(f), f.get("ext") != "m4a"))

def plan_video(info: dict) -> dict | None:
    dur = info.get("duration") or 0
    fmts = info.get("formats") or []
    plans = []
    m = pick_video([f for f in fmts if has_v(f) and has_a(f)])
    if m: plans.append((qual(m), [m]))
    if HAVE_FFMPEG:
        v = pick_video([f for f in fmts if has_v(f) and not has_a(f)])
        a = pick_audio(fmts)
        if v and a: plans.append((qual(v), [v, a]))
    if not plans: return None
    def key(p): return (0, -p[0]) if 0 < p[0] <= LOW_HEIGHT else (1, p[0])
    plans.sort(key=key)
    height, parts = plans[0]
    sizes = [fsize(f, dur) for f in parts]
    est = sum(sizes) if all(s is not None for s in sizes) else None
    if est is None and dur: est = int(dur * UNKNOWN_BYTES_PER_SEC)
    return {"fmt": "+".join(str(f["format_id"]) for f in parts), "height": height, "est": est}

def plan_audio(info: dict) -> dict | None:
    dur = info.get("duration") or 0
    fmts = info.get("formats") or []
    a = pick_audio(fmts)
    if a:
        est = fsize(a, dur) or (int(dur * 16_000) if dur else None)
        return {"fmt": str(a["format_id"]), "est": est, "extract": False}
    if HAVE_FFMPEG:
        muxed = [f for f in fmts if has_v(f) and has_a(f)]
        if muxed:
            m = min(muxed, key=lambda f: fsize(f, dur) or 10**12)
            est = fsize(m, dur) or (int(dur * UNKNOWN_BYTES_PER_SEC) if dur else None)
            return {"fmt": str(m["format_id"]), "est": est, "extract": True}
    return None

def size_txt(est: int | None) -> str:
    if est is None: return "size unknown"
    mb = est / 1e6
    return f"~{mb:.1f} MB" if mb < 10 else f"~{mb:.0f} MB"

def dur_txt(sec: float | None) -> str:
    if not sec: return ""
    s = int(sec)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ---------- yt-dlp ----------

def base_opts(outdir: str | None = None) -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "logger": QuietLogger(),
        "cachedir": False,
    }
    if FFMPEG: opts["ffmpeg_location"] = FFMPEG
    if outdir: opts["outtmpl"] = f"{outdir}/%(id)s.%(ext)s"
    if os.path.exists(COOKIES_FILE): opts["cookiefile"] = COOKIES_FILE
    return opts

def fetch_info(url: str) -> dict:
    opts = base_opts()
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        info = entries[0] if entries else {}
    return info

def download(url: str, fmt: str, outdir: str, extract_audio: bool = False) -> Path | None:
    opts = base_opts(outdir)
    opts.update(format=fmt, max_filesize=LIMIT, playlist_items="1")
    if HAVE_FFMPEG: opts["merge_output_format"] = "mp4"
    if extract_audio:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "5"}]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [p for p in Path(outdir).glob("*") if p.is_file() and p.suffix not in (".part", ".ytdl")]
    return max(files, key=lambda p: p.stat().st_size) if files else None

def error_text(e: Exception, uid: int) -> str:
    text = f"❌ {html.escape(clean_error(e))}"
    if uid == OWNER_ID:
        raw = re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:600]
        text += f"\n\n<b>Owner debug:</b>\n<code>{html.escape(raw)}</code>"
    return text

def source_url_from_message(message) -> str | None:
    for entity in getattr(message, "entities", None) or []:
        if entity.type == "text_link" and entity.url:
            return entity.url
    return None

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ---------- Telegram handlers ----------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 <b>{BOT_NAME}</b>\n\n"
        "Send me a link and I'll fetch it for you.\n\n"
        f"🎬 Videos are fetched at standard quality (up to {LOW_HEIGHT}p) so they arrive fast.\n"
        f"📦 Files under 50 MB are sent directly in the chat. For larger files, you'll receive a direct download link.\n\n"
        "Only download content you own or have permission to save.",
        parse_mode=ParseMode.HTML,
    )

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    url = extract_url(msg.text)
    
    if not url:
        await msg.reply_text("Please send a valid link starting with http:// or https://")
        return
    if not is_safe_url(url) or not is_allowed_domain(url) or not await asyncio.to_thread(resolves_public, url):
        await msg.reply_text("I couldn't use that link. Please send a public media link.")
        return
    if time.time() - last_check.get(uid, 0) < CHECK_COOLDOWN:
        await msg.reply_text("⏳ Slow down a little, then send the link again.")
        return
    last_check[uid] = time.time()

    status = await msg.reply_text("🔎 Checking the link…")
    
    # NEW: Instant YouTube bypass
    if "youtube.com" in url or "youtu.be" in url:
        lines = [
            "🔗 <b>YouTube Link Detected</b>", "",
            "🎬 Video: Available via Server",
            "🎵 Audio: Available via Server",
            f'\n<a href="{html.escape(url, quote=True)}">Source</a>'
        ]
        buttons = [Btn("🎬 Video", callback_data="dl:v"), Btn("🎵 Audio", callback_data="dl:a")]
        await status.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=Markup([buttons]),
            link_preview_options=NO_PREVIEW,
        )
        return

    # Standard metadata fetch (Reddit, Insta, FB, etc)
    try:
        async with sem:
            info = await asyncio.wait_for(asyncio.to_thread(fetch_info, url), timeout=FETCH_TIMEOUT)
    except yt_dlp.utils.DownloadError as e:
        log.warning("check failed: %s", e)
        await status.edit_text(error_text(e, uid), parse_mode=ParseMode.HTML)
        return
    except asyncio.TimeoutError:
        await status.edit_text("⏱ The link took too long to respond. Try again.")
        return
    except Exception:
        log.exception("unexpected error while checking")
        await status.edit_text("❌ Something went wrong. Please try again.")
        return

    if not info or not info.get("formats"):
        await status.edit_text("⚠️ I couldn't find any media in that link.")
        return
    if info.get("is_live"):
        await status.edit_text("⚠️ Live streams aren't supported.")
        return

    vp, ap = plan_video(info), plan_audio(info)
    title = html.escape((info.get("title") or "Your link")[:120])
    lines = [f"🔗 <b>{title}</b>"]
    if info.get("duration"):
        lines.append(f"⏱ {dur_txt(info['duration'])}")
    lines.append("")

    buttons = []
    
    if vp:
        h = f"{vp['height']}p · " if vp["height"] else ""
        lines.append(f"🎬 Video: {h}{size_txt(vp['est'])}")
        buttons.append(Btn("🎬 Video", callback_data="dl:v"))
    else:
        lines.append("🎬 Video: not available for this link")

    if ap:
        lines.append(f"🎵 Audio: {size_txt(ap['est'])}")
        buttons.append(Btn("🎵 Audio", callback_data="dl:a"))
    else:
        lines.append("🎵 Audio: not available for this link")

    if not buttons:
        lines.append("\n⚠️ Media cannot be extracted from this link.")
        await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW)
        return

    lines.append(f'\n<a href="{html.escape(url, quote=True)}">Source</a>')
    await status.edit_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=Markup([buttons]),
        link_preview_options=NO_PREVIEW,
    )

async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    mode = "audio" if q.data == "dl:a" else "video" if q.data == "dl:v" else None
    if not mode:
        await q.answer()
        return

    uid = update.effective_user.id
    wait = USER_COOLDOWN - (time.time() - last_download.get(uid, 0))
    if uid in active_users or wait > 0:
        await q.answer("⏳ Please wait a moment before the next download.", show_alert=True)
        return
    await q.answer()

    url = source_url_from_message(q.message)
    if not url or not is_safe_url(url) or not is_allowed_domain(url):
        await q.edit_message_text("⌛ This request expired. Please send the link again.")
        return

    chat_id = update.effective_chat.id
    active_users.add(uid)
    last_download[uid] = time.time()

    # ----------------------------------------------------
    # COBALT FALLBACK BLOCK (For YouTube)
    # ----------------------------------------------------
    if "youtube.com" in url or "youtu.be" in url:
        try:
            await q.edit_message_text("⏳ Processing YouTube link securely...")
            direct_url = await call_cobalt(url, mode)
            
            # verify=False bypasses strict SSL checks on the destination link size check
            async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
                head_res = await client.head(direct_url, follow_redirects=True)
                size_bytes = int(head_res.headers.get("Content-Length", 0))
                size_mb = size_bytes / 1e6 if size_bytes else 0
                
            if 0 < size_mb <= 49.0 or size_bytes == 0:
                await q.edit_message_text(f"📤 Uploading natively via Telegram... ({size_mb:.1f} MB)" if size_mb else "📤 Uploading natively via Telegram...")
                if mode == "audio":
                    await ctx.bot.send_audio(chat_id=chat_id, audio=direct_url, read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT)
                else:
                    await ctx.bot.send_video(chat_id=chat_id, video=direct_url, read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT)
                await q.message.delete()
            else:
                markup = Markup([[Btn(f"⬇️ Download {'Audio' if mode == 'audio' else 'Video'} ({size_mb:.1f} MB)", url=direct_url)]])
                await q.edit_message_text(
                    f"This file is {size_mb:.1f} MB, which exceeds Telegram's 50MB bot upload limit.\n\nClick below to download it directly to your device:",
                    reply_markup=markup
                )
        except UserError as e:
            await q.edit_message_text(f"⚠️ {e}")
        except Exception as e:
            log.warning(f"Cobalt processing failed: {e}")
            await q.edit_message_text("❌ Failed to process YouTube link.")
        finally:
            active_users.discard(uid)
        return

    # ----------------------------------------------------
    # STANDARD YT-DLP BLOCK (Reddit, Insta, FB)
    # ----------------------------------------------------
    outdir = tempfile.mkdtemp(dir=tempfile.gettempdir())
    try:
        await q.edit_message_text("🔎 Rechecking the link…")
        async with sem:
            info = await asyncio.wait_for(asyncio.to_thread(fetch_info, url), timeout=FETCH_TIMEOUT)

        vp, ap = plan_video(info), plan_audio(info)
        plan = vp if mode == "video" else ap
        if not plan:
            await q.edit_message_text("⚠️ This format is not available.")
            return
            
        est_mb = (plan["est"] or 0) / 1e6

        if est_mb > 49.5:
            await q.edit_message_text(f"Estimated size is ~{est_mb:.1f} MB (Too large for Telegram). Generating direct link...")
            try:
                direct_url = await call_cobalt(url, mode)
                markup = Markup([[Btn(f"⬇️ Download {'Audio' if mode == 'audio' else 'Video'}", url=direct_url)]])
                await q.edit_message_text(
                    f"This file is estimated around ~{est_mb:.0f} MB, which exceeds Telegram's bot upload limit.\n\nClick below to download it directly to your device:",
                    reply_markup=markup
                )
            except UserError:
                await q.edit_message_text(f"Estimated size is ~{est_mb:.0f} MB (too large for Telegram), and a direct link could not be generated.")
            return

        await q.edit_message_text("⏳ Downloading…")
        extract = mode == "audio" and plan.get("extract", False)
        fallback = "worstaudio" if mode == "audio" else "worst"
        fmt = f"{plan['fmt']}/{fallback}"
        async with sem:
            path = await asyncio.wait_for(
                asyncio.to_thread(download, url, fmt, outdir, extract), timeout=DOWNLOAD_TIMEOUT
            )

        if not path:
            raise UserError(f"Failed to download this media.")
        size = path.stat().st_size
        size_mb = size / 1e6
        
        if size_mb > 49.5:
            await q.edit_message_text(f"File downloaded but is {size_mb:.1f} MB (Too large to upload to Telegram). Generating direct link...")
            try:
                direct_url = await call_cobalt(url, mode)
                markup = Markup([[Btn(f"⬇️ Download {'Audio' if mode == 'audio' else 'Video'} ({size_mb:.1f} MB)", url=direct_url)]])
                await q.edit_message_text(
                    f"This file is {size_mb:.1f} MB, which exceeds Telegram's bot upload limit.\n\nClick below to download it directly to your device:",
                    reply_markup=markup
                )
            except UserError:
                await q.edit_message_text(f"File is {size_mb:.1f} MB (too large for Telegram), and a direct link could not be generated.")
            return

        await q.edit_message_text(f"📤 Uploading… ({size_mb:.1f} MB)")
        t0 = time.time()
        await ctx.bot.send_chat_action(
            chat_id, ChatAction.UPLOAD_VIDEO if mode == "video" else ChatAction.UPLOAD_VOICE
        )

        title = html.escape((info.get("title") or "")[:180])
        kw = dict(
            chat_id=chat_id,
            caption=f"<b>{title}</b>" if title else None,
            parse_mode=ParseMode.HTML,
            read_timeout=UPLOAD_TIMEOUT,
            write_timeout=UPLOAD_TIMEOUT,
            connect_timeout=30,
        )
        with open(path, "rb") as f:
            suffix = path.suffix.lower()
            if mode == "audio" and suffix in (".m4a", ".mp3"):
                await ctx.bot.send_audio(audio=f, title=info.get("title") or None, **kw)
            elif mode == "audio" or suffix not in (".mp4", ".mov", ".m4v"):
                await ctx.bot.send_document(document=f, **kw)
            else:
                await ctx.bot.send_video(video=f, supports_streaming=True, **kw)
        log.info("Uploaded %.1f MB in %.0fs", size_mb, time.time() - t0)

        try:
            await q.message.delete()
        except BadRequest:
            pass
    except UserError as e:
        await q.edit_message_text(f"⚠️ {e}")
    except asyncio.TimeoutError:
        await q.edit_message_text("⏱ That took too long and was cancelled. Please try again.")
    except yt_dlp.utils.DownloadError as e:
        log.warning("download failed: %s", e)
        await q.edit_message_text(error_text(e, uid), parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("unexpected error")
        await q.edit_message_text("❌ Something went wrong. Please try again.")
    finally:
        active_users.discard(uid)
        shutil.rmtree(outdir, ignore_errors=True)

async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("handler error", exc_info=ctx.error)

# ---------- Telegram application (created lazily, once per server instance) ----------

def build_tg(request=None) -> Application:
    builder = Application.builder().token(BOT_TOKEN).updater(None).job_queue(None)
    if request is not None:
        builder = builder.request(request)
    tg = builder.build()
    tg.add_handler(CommandHandler("start", cmd_start))
    tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    tg.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl:"))
    tg.add_error_handler(on_error)
    return tg

_tg: Application | None = None
_tg_lock = asyncio.Lock()

async def get_tg() -> Application:
    global _tg
    if _tg is None:
        async with _tg_lock:
            if _tg is None:
                tg = build_tg()
                await tg.initialize()
                _tg = tg
    return _tg

async def run_update(tg: Application, update: Update):
    try:
        await tg.process_update(update)
    except Exception:
        log.exception("update processing failed")


# ---------- Web app (what Vercel runs) ----------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

@app.get("/")
@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "bot_token_set": bool(BOT_TOKEN),
        "webhook_secret_set": bool(WEBHOOK_SECRET),
        "ffmpeg": HAVE_FFMPEG,
    }

@app.post("/api/webhook")
async def webhook(request: Request, background: BackgroundTasks):
    if not BOT_TOKEN or not WEBHOOK_SECRET:
        log.error("BOT_TOKEN or TELEGRAM_WEBHOOK_SECRET is not set")
        raise HTTPException(status_code=500, detail="Server is not configured")

    secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not hmac.compare_digest(secret.encode(), WEBHOOK_SECRET.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    tg = await get_tg()
    update = Update.de_json(body, tg.bot)
    if update is None or update.update_id in seen_updates:
        return {"ok": True}
    seen_updates.append(update.update_id)

    if INLINE_PROCESSING:
        await run_update(tg, update)
    else:
        background.add_task(run_update, tg, update)
    return {"ok": True}