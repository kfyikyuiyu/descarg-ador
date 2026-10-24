import copy, importlib.metadata as imd, json, os, queue, re, shutil, subprocess, sys, threading, time, urllib.request, uuid, webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL
from yt_dlp.utils import download_range_func
import imageio_ffmpeg

# Si hay un ffmpeg instalado en el sistema se usa ese (trae ffprobe y suele ser más estable);
# si no, el que incluye imageio-ffmpeg.
FFMPEG = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "cookies.txt")
HISTORY_FILE = os.path.join(HERE, "history.json")
CONFIG_FILE = os.path.join(HERE, "config.json")

# Runtimes JS que yt-dlp puede usar para resolver YouTube (si no están instalados, se ignoran).
FAST = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "socket_timeout": 20,
        "concurrent_fragment_downloads": 8, "retries": 5, "fragment_retries": 5,
        "skip_unavailable_fragments": True, "ignoreerrors": False,
        "js_runtimes": {"deno": {}, "node": {}, "bun": {}, "quickjs": {}},
        "remote_components": ["ejs:github"]}
BROWSERS = ["firefox", "chrome", "edge", "brave"]
NEEDS_LOGIN = ("log in", "login", "cookies", "authentication", "comfortable", "sign in",
               "age-restricted", "confirm your age", "private video", "members-only",
               "this video is private")
YT_FALLBACK = {"extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web_safari", "ios"]}}}
EMBED_OK = ("mp3", "m4a", "opus", "flac")      # formatos donde se puede incrustar la carátula
MAX_WORKERS = 8

# Errores típicos de TikTok cuando pide verificación / bloquea la solicitud
TT_ERR = ("unexpected response from webpage", "unable to extract webpage", "universal data",
          "unable to extract challenge", "http error 403", "status code", "unable to extract aweme", "failed to parse json")

good = {"browser": None}
UPDATE = {"state": "idle", "msg": ""}

def impersonation_ok():
    """TikTok necesita 'curl_cffi' compatible para imitar un navegador real."""
    try:
        with YoutubeDL({"quiet": True}) as y:
            return bool(y._get_available_impersonate_targets())
    except Exception:
        return False

def impersonation_reason():
    try:
        import curl_cffi
        return f"curl_cffi {getattr(curl_cffi, '__version__', '?')} no es compatible con esta versión de yt-dlp"
    except Exception:
        return "curl_cffi no está instalado"

def ytdlp_version():
    try:
        return imd.version("yt-dlp")
    except Exception:
        return "?"

LOADED_VERSION = ytdlp_version()
IMPERSONATION = impersonation_ok()
DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "Downloads", "VidBajar")
ITEMS, ORDER, DONE = {}, [], []
LOCK = threading.RLock()
Q = queue.Queue()
INFO_SEM = threading.Semaphore(6)
CANCEL = threading.Event()
SLOT = threading.Condition()
ACTIVE = [0]

# ---------- config ----------
def load_cfg():
    c = {"dest": DEFAULT_DEST, "workers": 3, "theme": "auto", "notify": True, "history": True}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            c.update(json.load(f))
    except Exception:
        pass
    return c

def save_cfg(c):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

CFG = load_cfg()
DEST = CFG.get("dest") or DEFAULT_DEST
try:
    os.makedirs(DEST, exist_ok=True)
except Exception:
    DEST = DEFAULT_DEST
    CFG["dest"] = DEST
    os.makedirs(DEST, exist_ok=True)

# ---------- history ----------
def load_history():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
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

DONE = load_history()

# ---------- helpers ----------
def clean_url(u):
    u = u.strip()
    if "tiktok.com" in u:
        u = u.split("?")[0]
    if "youtu.be/" in u:
        vid = u.split("youtu.be/")[1].split("?")[0].split("&")[0]
        return f"https://www.youtube.com/watch?v={vid}"
    return u

def drop(iid):
    with LOCK:
        ITEMS.pop(iid, None)
        if iid in ORDER:
            ORDER.remove(iid)

def to_float(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None

def _try(url, opts, download, jar_out=None):
    with YoutubeDL(opts) as y:
        info = y.extract_info(url, download=download)
        if jar_out is not None:
            try:
                jar_out[:] = list(y.cookiejar)
            except Exception:
                pass
        return info

def base_opts():
    o = dict(FAST)
    if os.path.exists(COOKIE_FILE):
        o["cookiefile"] = COOKIE_FILE
    elif good["browser"]:
        o["cookiesfrombrowser"] = (good["browser"],)
    return o

def extract(url, extra=None, download=False, jar_out=None):
    extra = extra or {}
    base = base_opts()
    tiktok = "tiktok.com" in url
    try:
        return _try(url, {**base, **extra}, download, jar_out)
    except Exception as first:
        low = str(first).lower()
        if "cancelado" in low:
            raise
        if "youtu" in url and not any(k in low for k in NEEDS_LOGIN):
            try:
                return _try(url, {**base, **extra, **YT_FALLBACK}, download, jar_out)
            except Exception as e2:
                if "cancelado" in str(e2).lower():
                    raise
        if tiktok and any(k in low for k in TT_ERR):
            time.sleep(2.5)                      # TikTok a veces responde bien al segundo intento
            try:
                return _try(url, {**base, **extra}, download, jar_out)
            except Exception as e2:
                if "cancelado" in str(e2).lower():
                    raise
        needs = any(k in low for k in NEEDS_LOGIN) or (tiktok and any(k in low for k in TT_ERR))
        if "cookiefile" in base or "cookiesfrombrowser" in base or not needs:
            raise first
        for b in BROWSERS:
            try:
                d = _try(url, {**FAST, **extra, "cookiesfrombrowser": (b,)}, download, jar_out)
                good["browser"] = b
                print("Usando la sesión de", b)
                return d
            except Exception as e3:
                if "cancelado" in str(e3).lower():
                    raise
                continue
        raise first

def friendly(e):
    m = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    print("ERROR:", m)
    m = re.split(r";? ?please report this issue", m)[0].strip()
    low = m.lower()
    if "tiktok" in low and any(k in low for k in TT_ERR) or "unexpected response from webpage" in low:
        if not IMPERSONATION:
            return ("TikTok necesita el componente «curl_cffi» (" + impersonation_reason() + "). "
                    "Pulsa ⬆ Actualizar (arriba a la derecha) y espera: se instala y se reinicia solo.")
        return ("TikTok bloqueó la solicitud (error 403: acceso denegado / pide verificación). Prueba, en este orden: "
                "1) pulsa ⬆ Actualizar y reinicia el programa; "
                "2) abre tiktok.com en Firefox, inicia sesión, cierra Firefox y pulsa Reintentar; "
                "3) si usas VPN, apágala o cámbiala de país.")
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
    if "unable to download" in low and ("getaddrinfo" in low or "name or service" in low or "connection" in low):
        return "Sin conexión a internet o el sitio no responde."
    if "ffmpeg" in low or "ffprobe" in low:
        return "Error con FFmpeg. Reinstala imageio-ffmpeg (pip install --upgrade imageio-ffmpeg)."
    return "No se pudo procesar el enlace. " + m[-220:]

def sizes(d):
    out, seen = [], set()
    for f in (d.get("formats") or []):
        h = f.get("height")
        if not h or f.get("vcodec") == "none":      # "none" = solo audio; None = códec desconocido (HLS), es válido
            continue
        if f.get("format_note") and "watermarked" in str(f["format_note"]).lower():
            continue
        if h in seen:
            continue
        seen.add(h)
        size = f.get("filesize") or f.get("filesize_approx")
        w = f.get("width") or 0
        out.append({"h": h, "s": min(h, w) if w else h, "size": size, "ext": f.get("ext") or "mp4",
                    "fps": f.get("fps"), "vcodec": f.get("vcodec")})
    out.sort(key=lambda x: x["h"], reverse=True)
    return out

# ---------- info fetch ----------
def fetch_info(it):
    with INFO_SEM:
        if it.get("cancel"):
            return
        try:
            jar = []
            d = extract(clean_url(it["url"]), {"skip_download": True}, jar_out=jar)
            if not d:
                raise Exception("Sin información")
            if d.get("_type") != "playlist":
                it["_info"], it["_jar"], it["_info_time"] = d, jar, time.time()
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
            it["subtitles"] = list((d.get("subtitles") or {}).keys())[:10]
            h = str(it.get("height") or "")
            if it["kind"] == "video" and h.isdigit() and qs:
                c = [q["h"] for q in qs if q["s"] <= int(h)]
                it["height"] = str(max(c) if c else min(q["h"] for q in qs))
                if it.get("upscale") and int(h) > max(q["s"] for q in qs):
                    it["scale_to"] = min(int(h), MAX_SCALE)
            elif qs and not it.get("height"):
                it["height"] = str(qs[0]["h"])
            it["info"] = "ok"
        except Exception as e:
            if not it.get("cancel"):
                it.update(info="fail", info_error=friendly(e))

# ---------- download ----------
def acquire_slot(it):
    """Espera un hueco según el nº de descargas simultáneas de los Ajustes."""
    with SLOT:
        while ACTIVE[0] >= max(1, min(MAX_WORKERS, int(CFG.get("workers", 3) or 3))):
            if it.get("cancel") or CANCEL.is_set():
                return False
            SLOT.wait(0.5)
        ACTIVE[0] += 1
    return True

def release_slot():
    with SLOT:
        ACTIVE[0] -= 1
        SLOT.notify_all()

MAX_SCALE = 2160     # límite del escalado (4K); H.264 no se reproduce bien por encima

def probe_size(path):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True, timeout=60)
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", r.stderr)
        return (int(m.group(1)), int(m.group(2))) if m else None
    except Exception:
        return None

def scale_label(t):
    return {2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD"}.get(t, f"{t}p")

def upscale_file(it, path, target):
    """Escala el video para que su lado corto sea `target`, conservando orientación y proporción."""
    size = probe_size(path)
    if not size:
        return path
    w, h = size
    if min(w, h) >= target:
        return path
    vf = f"scale={target}:-2:flags=lanczos" if h >= w else f"scale=-2:{target}:flags=lanczos"
    tmp = path + ".scaled.mp4"
    it.update(state="processing", pct=100, stage=f"Escalando a {scale_label(target)} ({target}p)… puede tardar")
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", path, "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-movflags", "+faststart", "-f", "mp4", tmp]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    while True:
        try:
            _, err = p.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if it.get("cancel") or CANCEL.is_set():
                p.kill()
                p.communicate()
                try: os.remove(tmp)
                except Exception: pass
                raise Exception("Cancelado por el usuario")
    if p.returncode != 0:
        try: os.remove(tmp)
        except Exception: pass
        raise Exception("ffmpeg no pudo escalar el video: " + (err or "").strip()[-150:])
    os.replace(tmp, path)
    return path

def process(it):
    if not acquire_slot(it):
        drop(it["id"])
        return
    try:
        if it.get("cancel") or ITEMS.get(it["id"]) is not it:
            drop(it["id"])
            return
        run_download(it)
    finally:
        release_slot()

def run_download(it):
    it.update(state="downloading", pct=0, error=None, stage=None)
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
            pct = min(100, round(d.get("downloaded_bytes", 0) * 100 / tot, 1)) if tot else 0
            it.update(state="downloading", speed=d.get("speed") or 0, eta=d.get("eta"),
                      downloaded=d.get("downloaded_bytes"), total=tot, pct=pct)
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
    h = it.get("height", "")
    if it["kind"] == "audio":
        fmt = it.get("audio_fmt", "mp3")
        pp = [{"key": "FFmpegExtractAudio", "preferredcodec": fmt,
               "preferredquality": str(it.get("audio_q", 192))}]
        if it.get("embed_thumb", True) and fmt in EMBED_OK:
            opts["writethumbnail"] = True
            pp.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
            pp.append({"key": "EmbedThumbnail"})
        opts.update(format="bestaudio/best", postprocessors=pp)
    else:
        cap = f"[height<={int(h)}]" if str(h).isdigit() else ""
        opts.update(
            format=f"bv*{cap}[format_note!*=watermarked]+ba/b{cap}[format_note!*=watermarked]/b{cap}/bv*+ba/b",
            merge_output_format="mp4",
            format_sort=["res", "vcodec:h264", "acodec:aac"],
        )
        if it.get("subs"):
            opts.update(writesubtitles=True, writeautomaticsub=True,
                        subtitleslangs=["es", "en"] if not it.get("sub_lang") else [it["sub_lang"]],
                        subtitlesformat="srt")

    # Recorte (segundo inicio / fin)
    ts, te = to_float(it.get("trim_start")) or 0.0, to_float(it.get("trim_end"))
    if ts > 0 or (te and te > 0):
        if te and te <= ts:
            te = None
        opts["download_ranges"] = download_range_func(None, [(ts, te if te else float("inf"))])
        opts["force_keyframes_at_cuts"] = True

    try:
        d = None
        info = it.get("_info")
        if info and time.time() - it.get("_info_time", 0) < 20 * 60:
            # Reutiliza lo que ya se obtuvo al agregar el enlace: así no se vuelve a pedir la página
            # (TikTok bloquea con 403 las solicitudes repetidas).
            try:
                with YoutubeDL({**base_opts(), **opts}) as y:
                    for c in it.get("_jar") or []:
                        y.cookiejar.set_cookie(c)
                    d = y.process_ie_result(copy.copy(info), download=True)
            except Exception as e0:
                if it.get("cancel") or CANCEL.is_set() or "cancelado" in str(e0).lower():
                    raise
                print("Reutilizar datos falló, se vuelve a pedir:", str(e0)[:150])
                d = None
        if d is None:
            d = extract(clean_url(it["url"]), opts, download=True)
        it.pop("_info", None); it.pop("_jar", None)
        if it.get("cancel"):
            raise Exception("Cancelado por el usuario")
        path = ""
        for f in (d.get("requested_downloads") or []):
            if f.get("filepath"):
                path = f["filepath"]
                break
        if path and it["kind"] == "video" and it.get("scale_to") and os.path.exists(path):
            upscale_file(it, path, int(it["scale_to"]))
        entry = {
            "title": d.get("title") or it["url"],
            "name": os.path.basename(path) if path else "",
            "url": it["url"],
            "site": it.get("site") or "",
            "size": os.path.getsize(path) if path and os.path.exists(path) else 0,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration": d.get("duration"),
            "uploader": d.get("uploader"),
            "kind": it["kind"],
            "elapsed": round(time.time() - start, 1),
        }
        DONE.insert(0, entry)
        save_history()
        drop(it["id"])
    except Exception as e:
        if it.get("cancel") or CANCEL.is_set():
            drop(it["id"])
        else:
            it.update(state="error", error=friendly(e))

def worker():
    while True:
        iid = Q.get()
        try:
            it = ITEMS.get(iid)
            if it and it["state"] == "waiting":
                process(it)
        except Exception as e:
            print("Error en worker:", e)
        finally:
            Q.task_done()

# ---------- actualizar yt-dlp ----------
def do_update():
    UPDATE.update(state="running", msg="Descargando la última versión de yt-dlp…")
    py = sys.executable
    steps = [
        [py, "-m", "pip", "install", "-U", "--pre", "--no-deps", "--disable-pip-version-check", "yt-dlp"],
        [py, "-m", "pip", "install", "-U", "--prefer-binary", "--disable-pip-version-check",
         "yt-dlp[default,curl-cffi]", "imageio-ffmpeg"],
    ]
    try:
        for cmd in steps:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if r.returncode != 0 and "externally-managed" in (r.stderr or "").lower():
                # Python del sistema (Linux/Mac): pip exige esta opción
                r = subprocess.run(cmd + ["--break-system-packages"], capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["error desconocido"]
                UPDATE.update(state="error", msg="No se pudo actualizar: " + tail[0][:200])
                return
        new = ytdlp_version()
        if not sys.platform.startswith("win"):
            UPDATE.update(state="done", msg=f"Actualizado a yt-dlp {new}. Reiniciando el programa… (unos segundos)")
            os.environ["VIDBAJAR_HEALED"] = "1"          # evita bucles de reinicio
            time.sleep(2.5)                              # da tiempo a que la página lea el mensaje
            try:
                sys.stdout.flush()
            except Exception:
                pass
            os.execv(sys.executable, [sys.executable] + sys.argv)
        UPDATE.update(state="done", msg=f"Actualizado a yt-dlp {new}. Cierra la ventana negra y vuelve a abrir el programa.")
    except Exception as e:
        UPDATE.update(state="error", msg="No se pudo actualizar: " + str(e)[:200])

# ---------- open folder ----------
def open_folder(path=None):
    p = path or DEST
    try:
        if sys.platform.startswith("win"):
            os.startfile(p)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass

QUEUE_KEYS = ("id", "url", "title", "state", "pct", "speed", "eta", "error", "kind", "height",
              "info", "info_error", "site", "uploader", "duration", "qualities",
              "subtitles", "webpage", "is_playlist", "playlist_count",
              "downloaded", "total", "audio_fmt", "audio_q", "embed_thumb", "subs", "sub_lang",
              "trim_start", "trim_end", "scale_to", "stage")

def queue_snapshot():
    items = []
    for i in list(ORDER):
        it = ITEMS.get(i)
        if not it:
            continue
        row = {k: it.get(k) for k in QUEUE_KEYS}
        row["thumb"] = bool(it.get("thumb_url"))
        items.append(row)
    return {
        "items": items,
        "done": DONE[:50],
        "folder": DEST,
        "stats": {
            "pending": sum(1 for x in items if x["state"] == "pending"),
            "downloading": sum(1 for x in items if x["state"] in ("downloading", "processing", "waiting")),
            "done": len(DONE),
            "total_size": sum(x.get("size") or 0 for x in DONE),
        },
        "config": CFG,
    }

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
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("ERROR GET", self.path, repr(e))
            try: self.send_json({"error": str(e)}, 500)
            except Exception: pass

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("ERROR POST", self.path, repr(e))
            try: self.send_json({"error": str(e)}, 500)
            except Exception: pass

    def _get(self):
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
            return self.send_json(queue_snapshot())
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
        if p == "/api/status":
            cur = ytdlp_version()
            return self.send_json({"version": cur, "loaded": LOADED_VERSION, "restart": cur != LOADED_VERSION,
                                   "impersonation": IMPERSONATION, "update": UPDATE})
        if p == "/api/history":
            return self.send_json({"items": DONE[:200]})
        if p == "/api/file":
            path = os.path.join(DEST, os.path.basename(q.get("name", "")))
            if os.path.isfile(path):
                return self.send_file(path, "application/octet-stream")
            return self.send_error(404)
        return self.send_error(404)

    def _post(self):
        global DEST
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(data, dict):
                data = {}
        except Exception:
            return self.send_json({"error": "Petición inválida"}, 400)
        p = self.path

        if p == "/api/add":
            text = data.get("text", "") or ""
            urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", text)))
            if not urls:
                return self.send_json(
                    {"error": "No encontré ningún enlace. Pega enlaces que empiecen con http:// o https://"}, 400)
            known = {clean_url(ITEMS[i]["url"]) for i in list(ORDER) if i in ITEMS}
            n = 0
            for u in urls:
                cu = clean_url(u)
                if cu in known:
                    continue
                known.add(cu)
                iid = uuid.uuid4().hex[:10]
                try:
                    aq = int(data.get("audio_q", 192))
                except Exception:
                    aq = 192
                it = {
                    "id": iid, "url": u, "title": u, "state": "pending", "pct": 0,
                    "kind": "audio" if data.get("kind") == "audio" else "video",
                    "height": str(data.get("height", "") or ""),
                    "upscale": bool(data.get("upscale", True)),
                    "scale_to": None,
                    "info": "loading", "qualities": [],
                    "audio_fmt": data.get("audio_fmt", "mp3"),
                    "audio_q": aq,
                    "embed_thumb": data.get("embed_thumb", True),
                    "subs": data.get("subs", False),
                    "sub_lang": data.get("sub_lang", "es"),
                    "trim_start": data.get("trim_start"),
                    "trim_end": data.get("trim_end"),
                }
                with LOCK:
                    ITEMS[iid] = it
                    ORDER.append(iid)
                threading.Thread(target=fetch_info, args=(it,), daemon=True).start()
                n += 1
            return self.send_json({"added": n})

        if p in ("/api/remove", "/api/retry"):
            it = ITEMS.get(data.get("id"))
            if it and p == "/api/retry":
                if it["state"] == "error":
                    it.update(state="waiting", error=None, pct=0, cancel=False)
                    Q.put(it["id"])
            elif it:
                it["cancel"] = True
                if it["state"] not in ("downloading", "processing"):
                    drop(it["id"])
            return self.send_json({"ok": True})

        if p == "/api/set":
            it = ITEMS.get(data.get("id"))
            if it:
                for k in ("kind", "audio_fmt", "audio_q", "embed_thumb", "subs", "sub_lang",
                          "trim_start", "trim_end"):
                    if k in data:
                        it[k] = data[k]
                if "height" in data:
                    val = str(data["height"] or "")
                    qs = it.get("qualities") or []
                    if val.startswith("up:") and qs:           # elegir una resolución mayor a la original
                        try:
                            it["scale_to"] = min(int(val[3:]), MAX_SCALE)
                            it["height"] = str(qs[0]["h"])
                        except Exception:
                            pass
                    else:
                        it["height"] = val
                        it["scale_to"] = None
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
                    ITEMS[i]["cancel"] = True
                    drop(i)
            return self.send_json({"ok": True})

        if p == "/api/clear_done":
            DONE.clear()
            save_history()
            return self.send_json({"ok": True})

        if p == "/api/update":
            if UPDATE["state"] != "running":
                threading.Thread(target=do_update, daemon=True).start()
                UPDATE.update(state="running", msg="Actualizando…")
            return self.send_json({"ok": True})

        if p == "/api/open":
            open_folder(data.get("path"))
            return self.send_json({"ok": True})

        if p == "/api/config":
            if data.get("dest"):
                try:
                    os.makedirs(data["dest"], exist_ok=True)
                    DEST = data["dest"]
                except Exception:
                    return self.send_json({"error": "No se pudo crear esa carpeta de descarga."}, 400)
            for k in ("dest", "theme", "notify", "history"):
                if k in data and (k != "dest" or data[k]):
                    CFG[k] = data[k]
            if "workers" in data:
                try:
                    CFG["workers"] = max(1, min(MAX_WORKERS, int(data["workers"])))
                except Exception:
                    pass
                with SLOT:
                    SLOT.notify_all()
            save_cfg(CFG)
            return self.send_json({"ok": True, "config": CFG})

        if p == "/api/cancel_all":
            CANCEL.set()
            for it in list(ITEMS.values()):
                if it["state"] in ("downloading", "processing", "waiting"):
                    it["cancel"] = True
                    if it["state"] == "waiting":
                        drop(it["id"])
            threading.Timer(1.5, CANCEL.clear).start()
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
                    if os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass
                DONE[:] = [x for x in DONE if x.get("name") != name]
                save_history()
            return self.send_json({"ok": True})

        return self.send_error(404)


def make_server():
    host = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
    if os.environ.get("PORT"):
        return ThreadingHTTPServer((host, int(os.environ["PORT"])), H), host
    last = None
    for port in range(5000, 5011):
        try:
            return ThreadingHTTPServer((host, port), H), host
        except OSError as e:
            last = e
    raise last


if __name__ == "__main__":
    for _ in range(MAX_WORKERS):
        threading.Thread(target=worker, daemon=True).start()

    if not IMPERSONATION and not sys.platform.startswith("win") and not os.environ.get("VIDBAJAR_HEALED"):
        print("  Falta curl_cffi (necesario para TikTok): instalando automáticamente…")
        threading.Thread(target=do_update, daemon=True).start()

    server, host = make_server()
    port = server.server_address[1]
    if not os.environ.get("RENDER") and not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    print("=" * 60)
    print("  VidBajar Pro — listo")
    print("  No cierres esta ventana.")
    print("  Guardando en:", DEST)
    print("  Descargas simultáneas:", CFG.get("workers", 3), "| Historial:", CFG.get("history", True))
    print("  yt-dlp:", LOADED_VERSION, "| TikTok (curl_cffi):", "OK" if IMPERSONATION else "NO DISPONIBLE -> pulsa Actualizar")
    print(f"  Abre: http://localhost:{port}")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
