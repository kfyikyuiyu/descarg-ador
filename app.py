import json, os, queue, re, subprocess, sys, threading, urllib.request, uuid, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote
from yt_dlp import YoutubeDL
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "cookies.txt")
FAST = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
        "concurrent_fragment_downloads": 5, "retries": 5}
BROWSERS = ["firefox", "chrome", "edge", "brave"]
NEEDS_LOGIN = ("log in", "login", "cookies", "authentication", "comfortable", "sign in",
               "age-restricted", "confirm your age", "private video", "members-only")
YT_FALLBACK = {"extractor_args": {"youtube": {"player_client": ["tv", "mweb", "web_safari"]}}}
good = {"browser": None}
DEST = os.path.join(os.path.expanduser("~"), "Downloads", "VidBajar")
os.makedirs(DEST, exist_ok=True)
ITEMS, ORDER, DONE = {}, [], []
Q = queue.Queue()
INFO_SEM = threading.Semaphore(3)


def clean_url(u):
    return u.split("?")[0] if "tiktok.com" in u else u


def _try(url, opts, download):
    with YoutubeDL(opts) as y:
        return y.extract_info(url, download=download)


def extract(url, extra=None, download=False):
    """Prueba normal -> variantes de YouTube -> sesión de tu navegador si piden login."""
    extra = extra or {}
    base = dict(FAST)
    if os.path.exists(COOKIE_FILE):
        base["cookiefile"] = COOKIE_FILE
    elif good["browser"]:
        base["cookiesfrombrowser"] = (good["browser"],)
    try:
        return _try(url, {**base, **extra}, download)
    except Exception as first:
        low = str(first).lower()
        if "cancelado" in low:
            raise
        if "youtu" in url and not any(k in low for k in NEEDS_LOGIN):
            try:
                return _try(url, {**base, **extra, **YT_FALLBACK}, download)
            except Exception:
                pass
        if "cookiefile" in base or "cookiesfrombrowser" in base or not any(k in low for k in NEEDS_LOGIN):
            raise
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
    if any(k in m.lower() for k in NEEDS_LOGIN):
        return ("Este video exige iniciar sesión. Abre la plataforma en Firefox o Chrome, "
                "inicia sesión, cierra ese navegador y vuelve a intentar.")
    return "No se pudo procesar el enlace. " + m[-200:]


def fetch_info(it):
    """Lee portada, título y calidades disponibles del enlace."""
    with INFO_SEM:
        try:
            d = extract(clean_url(it["url"]), {"skip_download": True})
            qs = sizes(d)
            it.update(title=d.get("title") or it["url"], thumb_url=d.get("thumbnail"),
                      site=(d.get("extractor_key") or "").replace("IE", ""),
                      uploader=d.get("uploader"), duration=d.get("duration"), qualities=qs)
            h = str(it["height"])
            if it["kind"] == "video" and h.isdigit() and qs:
                c = [q["h"] for q in qs if q["h"] <= int(h)]
                it["height"] = str(max(c) if c else min(q["h"] for q in qs))
            it["info"] = "ok"
        except Exception as e:
            it.update(info="fail", info_error=friendly(e))


def process(it):
    it.update(state="downloading", pct=0)

    def hook(d):
        if it.get("cancel"):
            raise Exception("Cancelado")
        t = (d.get("info_dict") or {}).get("title")
        if t:
            it["title"] = t
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            it.update(state="downloading", speed=d.get("speed") or 0, eta=d.get("eta"),
                      pct=round(d.get("downloaded_bytes", 0) * 100 / tot, 1) if tot else 0)
        elif d["status"] == "finished":
            it.update(state="processing", pct=100)

    opts = {"ffmpeg_location": FFMPEG, "progress_hooks": [hook],
            "outtmpl": os.path.join(DEST, "%(title).70s [%(id)s].%(ext)s")}
    h = it["height"]
    if it["kind"] == "audio":
        opts.update(format="bestaudio/best",
                    postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}])
    else:
        cap = f"[height<={int(h)}]" if str(h).isdigit() else ""
        # Evita los formatos de TikTok con marca de agua.
        opts.update(format=f"bv*{cap}[format_note!*=watermarked]+ba/b{cap}[format_note!*=watermarked]/b{cap}",
                    merge_output_format="mp4")
    try:
        d = extract(clean_url(it["url"]), opts, download=True)
        path = (d.get("requested_downloads") or [{}])[0].get("filepath") or ""
        DONE.insert(0, {"title": d.get("title") or it["url"], "name": os.path.basename(path)})
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
        it = ITEMS.get(Q.get())
        if it and it["state"] == "waiting":
            process(it)


def open_folder():
    if sys.platform.startswith("win"):
        os.startfile(DEST)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", DEST])


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if p == "/":
            b = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/api/queue":
            keys = ("id", "url", "title", "state", "pct", "speed", "eta", "error", "kind", "height",
                    "info", "info_error", "site", "uploader", "duration", "qualities")
            self.send_json({"items": [{**{k: ITEMS[i].get(k) for k in keys}, "thumb": bool(ITEMS[i].get("thumb_url"))}
                                      for i in ORDER if i in ITEMS],
                            "done": DONE[:50], "folder": DEST})
        elif p == "/api/thumb":
            it = ITEMS.get(q.get("id"))
            if it and it.get("thumb_url"):
                if "thumb_bytes" not in it:
                    try:
                        r = urllib.request.urlopen(urllib.request.Request(it["thumb_url"], headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
                        it["thumb_bytes"] = (r.read(3_000_000), r.headers.get_content_type())
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
            self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        p = self.path
        if p == "/api/add":
            urls = list(dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", data.get("text", ""))))
            if not urls:
                return self.send_json({"error": "No encontré ningún enlace. Pega enlaces que empiecen con http:// o https://"}, 400)
            known = {ITEMS[i]["url"] for i in ORDER if i in ITEMS}
            n = 0
            for u in urls:
                if u in known:
                    continue
                iid = uuid.uuid4().hex[:10]
                ITEMS[iid] = {"id": iid, "url": u, "title": u, "state": "pending", "pct": 0,
                              "kind": data.get("kind", "video"), "height": data.get("height", ""),
                              "info": "loading", "qualities": []}
                ORDER.append(iid)
                threading.Thread(target=fetch_info, args=(ITEMS[iid],), daemon=True).start()
                n += 1
            self.send_json({"added": n})
        elif p in ("/api/remove", "/api/retry"):
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
            self.send_json({"ok": True})
        elif p == "/api/set":
            it = ITEMS.get(data.get("id"))
            if it and it["state"] == "pending":
                if data.get("kind") in ("video", "audio"):
                    it["kind"] = data["kind"]
                if "height" in data:
                    it["height"] = str(data["height"])
            self.send_json({"ok": True})
        elif p == "/api/start_all":
            n = 0
            for i in list(ORDER):
                it = ITEMS.get(i)
                if it and it["state"] == "pending":
                    it["state"] = "waiting"
                    Q.put(i)
                    n += 1
            self.send_json({"started": n})
        elif p == "/api/clear_pending":
            for i in list(ORDER):
                if ITEMS.get(i, {}).get("state") == "pending":
                    ITEMS.pop(i, None)
                    ORDER.remove(i)
            self.send_json({"ok": True})
        elif p == "/api/clear_done":
            DONE.clear()
            self.send_json({"ok": True})
        elif p == "/api/open":
            open_folder()
            self.send_json({"ok": True})
        else:
            self.send_error(404)


if __name__ == "__main__":
    for _ in range(2):
        threading.Thread(target=worker, daemon=True).start()
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    print("Listo en http://localhost:5000  (no cierres esta ventana)")
    print("Los videos se guardan en:", DEST)
    ThreadingHTTPServer(("127.0.0.1", 5000), H).serve_forever()
