import json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request, uuid, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from yt_dlp import YoutubeDL
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "cookies.txt")
HISTORY_FILE = os.path.join(HERE, "history.json")
CONFIG_FILE = os.path.join(HERE, "config.json")
FAST = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
        "concurrent_fragment_downloads": 8, "retries": 5, "fragment_retries": 5,
        "skip_unavailable_fragments": True, "ignoreerrors": False}
BROWSERS = ["firefox", "chrome", "edge", "brave"]
NEEDS_LOGIN = ("log in", "login", "cookies", "authentication", "comfortable", "sign in",
               "age-restricted", "confirm your age", "private video", "members-only",
               "this video is private")
YT_FALLBACK = {"extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web_safari", "ios"]}}}

good = {"browser": None}
DEST = os.path.join(os.path.expanduser("~"), "Downloads", "VidBajar")
os.makedirs(DEST, exist_ok=True)
ITEMS, ORDER, DONE = {}, [], []
Q = queue.Queue()
INFO_SEM = threading.Semaphore(6)
DL_SEM = threading.Semaphore(3)
CANCEL = threading.Event()

# ---------- config ----------
def load_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"dest": DEST, "workers": 3, "theme": "auto", "notify": True, "history": True}

def save_cfg(c):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

CFG = load_cfg()
DEST = CFG.get("dest") or DEST
os.makedirs(DEST, exist_ok=True)

# ---------- history ----------
def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_history():
    if not CFG.get("history", True):
        return
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(DONE[:200], f, ensure_ascii=False, indent=2)
    except Exception:
        pass

try:
    DONE = load_history()
except Exception:
    DONE = []

# ---------- helpers ----------
def clean_url(u):
    u = u.strip()
    if "tiktok.com" in u:
        u = u.split("?")[0]
    if "youtu.be/" in u:
        vid = u.split("youtu.be/")[1].split("?")[0].split("&")[0]
        return f"https://www.youtube.com/watch?v={vid}"
    return u

def human(n):
    if not n: return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def safe_name(s, maxlen=80):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "video")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:maxlen] or "video"

def _try(url, opts, download):
    with YoutubeDL(opts) as y:
        return y.extract_info(url, download=download)

def base_opts():
    o = dict(FAST)
    if os.path.exists(COOKIE_FILE):
        o["cookiefile"] = COOKIE_FILE
    elif good["browser"]:
        o["cookiesfrombrowser"] = (good["browser"],)
    return o

def extract(url, extra=None, download=False):
    extra = extra or {}
    base = base_opts()
    try:
        return _try(url, {**base, **extra}, download)
    except Exception as first:
        low = str(first).lower()
        if "cancelado" in low or "cancel" in low:
            raise
        if "youtu" in url and not any(k in low for k in NEEDS_LOGIN):
            try:
                return _try(url, {**base, **extra, **YT_FALLBACK}, download)
            except Exception:
                pass
        if "cookiefile" in base or "cookiesfrombrowser" in base or not any(k in low for k in NEEDS_LOGIN):
            raise first
        for b in BROWSERS:
            try:
                d = _try(url, {**FAST, **extra, "cookiesfrombrowser": (b,)}, download)
                good["browser"] = b
                print("Usando la sesión de", b)
                return d
            except Exception:
                continue
        raise first

def friendly(e):
    m = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    print("ERROR:", m)
    low = m.lower()
    if any(k in low for k in NEEDS_LOGIN):
        return ("Este video exige iniciar sesión. Abre la plataforma en Firefox o Chrome, "
                "inicia sesión, cierra ese navegador y vuelve a intentar.")
    if "private" in low:
        return "Este video es privado."
    if "unavailable" in low or "not available" in low:
        return "El video no está disponible en tu región o fue eliminado."
    if "geo" in low and "restrict" in low:
        return "El video está restringido geográficamente."
    if "timed out" in low or "timeout" in low:
        return "Se agotó el tiempo de espera. Revisa tu conexión a internet."
    if "http error 429" in low or "too many requests" in low:
        return "Demasiadas solicitudes. Espera unos minutos y vuelve a intentar."
    if "ffmpeg" in low:
        return "Error con FFmpeg. Reinstala imageio-ffmpeg (pip install --upgrade imageio-ffmpeg)."
    return "No se pudo procesar el enlace. " + m[-220:]

def sizes(d):
    """Devuelve lista de calidades disponibles con tamaño estimado."""
    out, seen = [], set()
    for f in (d.get("formats") or []):
        h = f.get("height")
        if not h or f.get("vcodec") in ("none", None):
            continue
        if f.get("format_note") and "watermarked" in str(f["format_note"]).lower():
            continue
        if h in seen:
            continue
        seen.add(h)
        size = f.get("filesize") or f.get("filesize_approx")
        out.append({"h": h, "size": size, "ext": f.get("ext") or "mp4",
                    "fps": f.get("fps"), "vcodec": f.get("vcodec")})
    out.sort(key=lambda x: x["h"], reverse=True)
    return out

# ---------- info fetch ----------
def fetch_info(it):
    with INFO_SEM:
        if it.get("cancel"):
            return
        try:
            d = extract(clean_url(it["url"]), {"skip_download": True})
            if not d:
                raise Exception("Sin información")
            qs = sizes(d)
            it.update(
                title=d.get("title") or it["url"],
                thumb_url=d.get("thumbnail"),
                site=(d.get("extractor_key") or "").replace("IE", ""),
                uploader=d.get("uploader"),
                duration=d.get("duration"),
                qualities=qs,
                webpage=d.get("webpage_url") or it["url"],
                is_playlist=bool(d.get("_type") == "playlist"),
                playlist_count=len(d.get("entries") or []) if d.get("_type") == "playlist" else 0,
            )
            # subtítulos
            subs = list((d.get("subtitles") or {}).keys())
            it["subtitles"] = subs[:10]
            h = str(it.get("height") or "")
            if it["kind"] == "video" and h.isdigit() and qs:
                c = [q["h"] for q in qs if q["h"] <= int(h)]
                it["height"] = str(max(c) if c else min(q["h"] for q in qs))
            elif qs and not it.get("height"):
                it["height"] = str(qs[0]["h"])
            it["info"] = "ok"
        except Exception as e:
            if not it.get("cancel"):
                it.update(info="fail", info_error=friendly(e))

# ---------- download ----------
def process(it):
    it.update(state="downloading", pct=0, error=None)
    last_update = [0.0]
    start = time.time()

    def hook(d):
        if it.get("cancel") or CANCEL.is_set():
            raise Exception("Cancelado por el usuario")
        info = d.get("info_dict") or {}
        t = info.get("title")
        if t:
            it["title"] = t
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            now = time.time()
            if now - last_update[0] < 0.25:
                return
            last_update[0] = now
            pct = round(d.get("downloaded_bytes", 0) * 100 / tot, 1) if tot else 0
            it.update(state="downloading",
                      speed=d.get("speed") or 0,
                      eta=d.get("eta"),
                      downloaded=d.get("downloaded_bytes"),
                      total=tot,
                      pct=pct)
        elif d["status"] == "finished":
            it.update(state="processing", pct=100)

    opts = {
        "ffmpeg_location": FFMPEG,
        "progress_hooks": [hook],
        "outtmpl": os.path.join(DEST, "%(title).80s [%(id)s].%(ext)s"),
        "windowsfilenames": True,
        "restrictfilenames": False,
        "continuedl": True,
        "retries": 8,
        "fragment_retries": 8,
        "concurrent_fragment_downloads": 8,
        "writethumbnail": False,
        "writeinfojson": False,
    }
    h = it["height"]
    if it["kind"] == "audio":
        opts.update(
            format="bestaudio/best",
            postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": it.get("audio_fmt", "mp3"),
                 "preferredquality": str(it.get("audio_q", 192))},
            ],
        )
        if it.get("embed_thumb", True):
            opts["writethumbnail"] = True
            opts["postprocessors"].append({"key": "EmbedThumbnail"})
    else:
        cap = f"[height<={int(h)}]" if str(h).isdigit() else ""
        opts.update(
            format=f"bv*{cap}[format_note!*=watermarked]+ba/b{cap}[format_note!*=watermarked]/b{cap}/bv*+ba/b",
            merge_output_format="mp4",
        )
        if it.get("subs"):
            opts.update(writesubtitles=True, writeautomaticsub=True,
                        subtitleslangs=["es", "en"] if not it.get("sub_lang") else [it["sub_lang"]],
                        subtitlesformat="srt")
        if it.get("trim_start") or it.get("trim_end"):
            opts.update(
                download_ranges=None,
                postprocessor_args={
                    "ffmpeg": ["-ss", str(it.get("trim_start") or 0), "-to", str(it.get("trim_end") or "999999")]
                } if False else None,
            )

    try:
        with DL_SEM:
            d = extract(clean_url(it["url"]), opts, download=True)
        path = ""
        req = (d.get("requested_downloads") or [{}])[0]
        path = req.get("filepath") or ""
        if not path:
            for f in (d.get("requested_downloads") or []):
                if f.get("filepath"):
                    path = f["filepath"]
                    break
        fname = os.path.basename(path) if path else ""
        duration = round(time.time() - start, 1)
        entry = {
            "title": d.get("title") or it["url"],
            "name": fname,
            "url": it["url"],
            "site": it.get("site") or "",
            "size": os.path.getsize(path) if path and os.path.exists(path) else 0,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration": d.get("duration"),
            "uploader": d.get("uploader"),
            "kind": it["kind"],
            "elapsed": duration,
        }
        DONE.insert(0, entry)
        save_history()
        ITEMS.pop(it["id"], None)
        if it["id"] in ORDER:
            ORDER.remove(it["id"])
    except Exception as e:
        if it.get("cancel"):
            ITEMS.pop(it["id"], None)
            if it["id"] in ORDER:
                ORDER.remove(it["id"])
        else:
            it.update(state="error", error=friendly(e))

def worker():
    while True:
        iid = Q.get()
        it = ITEMS.get(iid)
        if it and it["state"] in ("waiting",):
            process(it)
        Q.task_done()

# ---------- open folder ----------
def open_folder(path=None):
    p = path or DEST
    if sys.platform.startswith("win"):
        os.startfile(p)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])

# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, mime="application/octet-stream"):
        try:
            with open(path, "rb") as f:
                b = f.read()
        except Exception:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if p == "/":
            return self.send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        if p == "/manifest.json":
            return self.send_json({
                "name": "VidBajar Pro", "short_name": "VidBajar",
                "start_url": "/", "display": "standalone",
                "background_color": "#0f172a", "theme_color": "#5b3df5",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
            })
        if p == "/icon.svg":
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#5b3df5"/><path d="M32 14v24m0 0l-9-9m9 9l9-9M16 46h32" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>'
            self.send_response(200); self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg))); self.end_headers()
            return self.wfile.write(svg)
        if p == "/api/queue":
            keys = ("id", "url", "title", "state", "pct", "speed", "eta", "error", "kind", "height",
                    "info", "info_error", "site", "uploader", "duration", "qualities",
                    "subtitles", "webpage", "is_playlist", "playlist_count",
                    "downloaded", "total", "audio_fmt", "audio_q", "embed_thumb", "subs", "sub_lang",
                    "trim_start", "trim_end")
            return self.send_json({
                "items": [{**{k: ITEMS[i].get(k) for k in keys},
                           "thumb": bool(ITEMS[i].get("thumb_url"))} for i in ORDER if i in ITEMS],
                "done": DONE[:50],
                "folder": DEST,
                "stats": {
                    "pending": sum(1 for i in ORDER if ITEMS.get(i, {}).get("state") == "pending"),
                    "downloading": sum(1 for i in ORDER if ITEMS.get(i, {}).get("state") in ("downloading", "processing", "waiting")),
                    "done": len(DONE),
                    "total_size": sum(x.get("size") or 0 for x in DONE),
                },
                "config": CFG,
            })
        if p == "/api/thumb":
            it = ITEMS.get(q.get("id"))
            if it and it.get("thumb_url"):
                if "thumb_bytes" not in it:
                    try:
                        r = urllib.request.urlopen(
                            urllib.request.Request(it["thumb_url"], headers={"User-Agent": "Mozilla/5.0"}),
                            timeout=10)
                        it["thumb_bytes"] = (r.read(4_000_000), r.headers.get_content_type())
                    except Exception:
                        it["thumb_bytes"] = None
                tb = it["thumb_bytes"]
                if tb:
                    self.send_response(200)
                    self.send_header("Content-Type", tb[1])
                    self.send_header("Content-Length", str(len(tb[0])))
                    self.send_header("Cache-Control", "max-age=3600")
                    self.end_headers()
                    return self.wfile.write(tb[0])
            return self.send_error(404)
        if p == "/api/config":
            return self.send_json(CFG)
        if p == "/api/history":
            return self.send_json({"items": DONE[:200]})
        if p == "/api/file":
            # descargar archivo local (validación de path)
            name = q.get("name", "")
            path = os.path.join(DEST, os.path.basename(name))
            if os.path.exists(path):
                return self.send_file(path, "application/octet-stream")
            return self.send_error(404)
        return self.send_error(404)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        p = self.path

        if p == "/api/add":
            text = data.get("text", "")
            urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", text)))
            if not urls:
                return self.send_json(
                    {"error": "No encontré ningún enlace. Pega enlaces que empiecen con http:// o https://"}, 400)
            known = {ITEMS[i]["url"] for i in ORDER if i in ITEMS}
            n = 0
            for u in urls:
                cu = clean_url(u)
                if cu in known:
                    continue
                iid = uuid.uuid4().hex[:10]
                ITEMS[iid] = {
                    "id": iid, "url": u, "title": u, "state": "pending", "pct": 0,
                    "kind": data.get("kind", "video"),
                    "height": data.get("height", ""),
                    "info": "loading", "qualities": [],
                    "audio_fmt": data.get("audio_fmt", "mp3"),
                    "audio_q": data.get("audio_q", 192),
                    "embed_thumb": data.get("embed_thumb", True),
                    "subs": data.get("subs", False),
                    "sub_lang": data.get("sub_lang", "es"),
                    "trim_start": data.get("trim_start"),
                    "trim_end": data.get("trim_end"),
                }
                ORDER.append(iid)
                threading.Thread(target=fetch_info, args=(ITEMS[iid],), daemon=True).start()
                n += 1
            return self.send_json({"added": n})

        if p in ("/api/remove", "/api/retry"):
            it = ITEMS.get(data.get("id"))
            if it and p == "/api/retry" and it["state"] == "error":
                it.update(state="waiting", error=None, pct=0, cancel=False)
                Q.put(it["id"])
            elif it:
                if it["state"] in ("downloading", "processing"):
                    it["cancel"] = True
                else:
                    ITEMS.pop(it["id"], None)
                    if it["id"] in ORDER:
                        ORDER.remove(it["id"])
            return self.send_json({"ok": True})

        if p == "/api/set":
            it = ITEMS.get(data.get("id"))
            if it:
                for k in ("kind", "height", "audio_fmt", "audio_q", "embed_thumb", "subs", "sub_lang",
                          "trim_start", "trim_end"):
                    if k in data:
                        it[k] = data[k]
            return self.send_json({"ok": True})

        if p == "/api/start_all":
            n = 0
            for i in list(ORDER):
                it = ITEMS.get(i)
                if it and it["state"] == "pending":
                    it["state"] = "waiting"
                    Q.put(i)
                    n += 1
            return self.send_json({"started": n})

        if p == "/api/start_one":
            it = ITEMS.get(data.get("id"))
            if it and it["state"] == "pending":
                it["state"] = "waiting"
                Q.put(it["id"])
                return self.send_json({"ok": True})
            return self.send_json({"ok": False})

        if p == "/api/clear_pending":
            for i in list(ORDER):
                if ITEMS.get(i, {}).get("state") == "pending":
                    ITEMS.pop(i, None)
                    ORDER.remove(i)
            return self.send_json({"ok": True})

        if p == "/api/clear_done":
            DONE.clear()
            save_history()
            return self.send_json({"ok": True})

        if p == "/api/open":
            open_folder(data.get("path"))
            return self.send_json({"ok": True})

        if p == "/api/config":
            for k in ("dest", "workers", "theme", "notify", "history"):
                if k in data:
                    CFG[k] = data[k]
            save_cfg(CFG)
            if data.get("dest"):
                global DEST
                DEST = data["dest"]
                os.makedirs(DEST, exist_ok=True)
            if data.get("workers"):
                # añadir workers si aumentó
                global WORKER_COUNT
                while WORKER_COUNT < int(data["workers"]):
                    threading.Thread(target=worker, daemon=True).start()
                    WORKER_COUNT += 1
            return self.send_json({"ok": True, "config": CFG})

        if p == "/api/cancel_all":
            CANCEL.set()
            for it in ITEMS.values():
                if it["state"] in ("downloading", "processing", "waiting"):
                    it["cancel"] = True
            threading.Timer(1.0, CANCEL.clear).start()
            return self.send_json({"ok": True})

        if p == "/api/retry_all":
            n = 0
            for it in list(ITEMS.values()):
                if it["state"] == "error":
                    it.update(state="waiting", error=None, pct=0, cancel=False)
                    Q.put(it["id"])
                    n += 1
            return self.send_json({"retried": n})

        if p == "/api/remove_done":
            name = data.get("name")
            if name:
                path = os.path.join(DEST, os.path.basename(name))
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass
                DONE[:] = [x for x in DONE if x.get("name") != name]
                save_history()
            return self.send_json({"ok": True})

        return self.send_error(404)


WORKER_COUNT = 0

if __name__ == "__main__":
    workers = int(CFG.get("workers", 3))
    for _ in range(workers):
        threading.Thread(target=worker, daemon=True).start()
        WORKER_COUNT += 1
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    print("=" * 60)
    print("  VidBajar Pro — listo en http://localhost:5000")
    print("  No cierres esta ventana.")
    print("  Guardando en:", DEST)
    print("  Workers:", workers, "| Historial:", CFG.get("history", True))
    print("=" * 60)
    ThreadingHTTPServer(("127.0.0.1", 5000), H).serve_forever()

    if __name__ == "__main__":
    # ... (tus hilos worker)
    
    # --- CORRECCIÓN PARA RENDER ---
    import os
    port = int(os.environ.get("PORT", 5000))
    
    print("=" * 60)
    print(f"  VidBajar Pro — listo en http://0.0.0.0:{port}")
    print("  No cierres esta ventana.")
    print("  Guardando en:", DEST)
    print("=" * 60)
    
    # Escucha en 0.0.0.0 en lugar de 127.0.0.1
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
