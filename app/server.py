"""Servidor AdvvxClips (solo librería estándar). Sirve la interfaz web y la API.

Acceso: cada persona tiene su cuenta y su perfil. La primera cuenta que
se crea es la del administrador (adelvis): solo él aprueba a los demás.
Sin sesión no se puede usar la API (salvo salud y login/registro)."""
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ai, captions, config, creators, db, editor, live, media, pipeline, social, tracking

ROUTES: list[tuple[str, re.Pattern, callable, bool]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, True))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "TurboxClips"

    def log_message(self, *a):  # silencioso
        pass

    # ---------------------------------------------------------------- helpers
    def send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            raise ApiError(413, "Cuerpo demasiado grande.")
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as e:
            raise ApiError(400, "JSON inválido.") from e

    def send_file(self, path: Path, download_name: str | None = None, cache=False):
        if not path or not Path(path).is_file():
            raise ApiError(404, "Archivo no encontrado.")
        path = Path(path)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:
                    start = max(0, size - int(m.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            from urllib.parse import quote
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 512, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                left -= len(chunk)

    # ---------------------------------------------------------------- despacho
    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if not config.PUBLIC and host not in ("127.0.0.1", "localhost", "::1"):  # anti DNS-rebinding
            self.send_json({"detail": "Host no permitido."}, 403)
            return False
        if self.command not in ("GET", "HEAD") and self.headers.get("X-TBX") != "1":   # anti CSRF
            self.send_json({"detail": "Petición no permitida."}, 403)
            return False
        return True

    def handle_any(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        path, self.query = unquote(u.path), {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if path.startswith("/api/"):
                # Sesión obligatoria en toda la API salvo salud y login/registro.
                if path not in PUBLIC_API:
                    self._user = _session_user(self)
                    if not self._user:
                        raise ApiError(401, "Inicia sesión para entrar a AdvvxClips.")
                for method, rx, fn, _ in ROUTES:
                    if method == self.command or (method == "GET" and self.command == "HEAD"):
                        m = rx.match(path)
                        if m:
                            res = fn(self, *m.groups())
                            if res is not None:
                                self.send_json(res)
                            return
                raise ApiError(404, "Ruta no encontrada.")
            if self.command not in ("GET", "HEAD"):
                raise ApiError(405, "Método no permitido.")
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            base = config.WEB_DIR
            if rel.startswith("fonts/"):          # tipografías de los subtítulos (la vista previa del editor las usa)
                base, rel = config.FONTS_DIR, rel[6:]
            f = (base / rel).resolve()
            if base.resolve() not in f.parents and f != base.resolve():
                raise ApiError(404, "No encontrado.")
            self.send_file(f)
        except ApiError as e:
            self.send_json({"detail": e.detail}, e.code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (media.MediaError, ValueError) as e:
            self.send_json({"detail": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self.send_json({"detail": f"Error interno: {type(e).__name__}: {e}"}, 500)
            except Exception:  # noqa: BLE001
                pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = handle_any


# ======================================================================= API
def public_settings() -> dict:
    s = db.get_settings()
    for k in config.SECRET_KEYS:
        s[k + "_set"] = bool(s.get(k))
        s[k] = ""
    return s


# ======================================================================= cuentas
# Cada persona tiene su cuenta y su perfil. La primera cuenta creada es
# administradora y es la única que puede aprobar a los demás. Las sesiones
# viven en memoria (al reiniciar el servidor hay que volver a entrar).
PUBLIC_API = {"/api/health", "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/logout"}
SESSIONS: dict[str, dict] = {}
SESSION_TTL = 30 * 24 * 3600  # 30 días


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def _public_user(row: dict) -> dict:
    return {"id": row["id"], "username": row["username"], "name": row["name"] or row["username"],
            "is_admin": bool(row["is_admin"]), "approved": bool(row["approved"]),
            "created_at": row["created_at"]}


def _session_user(h) -> dict | None:
    cookie = h.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("advvx_session="):
            token = part.split("=", 1)[1].strip()
            sess = SESSIONS.get(token)
            if sess and sess["exp"] > time.time():
                return sess["user"]
            SESSIONS.pop(token, None)
    return None


def _require_admin(h) -> dict:
    u = getattr(h, "_user", None)
    if not u or not u["is_admin"]:
        raise ApiError(403, "Solo el administrador puede hacer eso.")
    return u


def _new_session(h, user: dict) -> str:
    token = secrets.token_hex(32)
    SESSIONS[token] = {"user": user, "exp": time.time() + SESSION_TTL}
    return token


def _set_session_cookie(h, token: str):
    # Se envía junto con la respuesta JSON (send_json la agrega).
    h._set_cookie = f"advvx_session={token}; HttpOnly; Path=/; Max-Age={SESSION_TTL}; SameSite=Lax"


@route("POST", "/api/auth/register")
def auth_register(h):
    b = h.body_json()
    username = re.sub(r"[^a-z0-9_.-]", "", (b.get("username") or "").strip().lower())[:32]
    name = (b.get("name") or "").strip()[:60]
    password = b.get("password") or ""
    if len(username) < 3:
        raise ApiError(400, "El usuario debe tener al menos 3 letras.")
    if len(password) < 4:
        raise ApiError(400, "La contraseña debe tener al menos 4 caracteres.")
    if db.q1("SELECT id FROM users WHERE username=?", (username,)):
        raise ApiError(400, "Ese usuario ya existe.")
    first = db.q1("SELECT COUNT(*) n FROM users")["n"] == 0
    salt = secrets.token_hex(16)
    uid = db.run("INSERT INTO users(username,name,pass_hash,salt,is_admin,approved,created_at) VALUES(?,?,?,?,?,?,?)",
                 (username, name or username, _hash_pw(password, salt), salt, 1 if first else 0,
                  1 if first else 0, db.now()))
    user = _public_user(db.q1("SELECT * FROM users WHERE id=?", (uid,)))
    if not user["approved"]:
        return {"ok": True, "approved": False,
                "detail": "Cuenta creada. Espera a que el administrador te apruebe para entrar."}
    token = _new_session(h, user)
    _set_session_cookie(h, token)
    return {"ok": True, "approved": True, "user": user}


@route("POST", "/api/auth/login")
def auth_login(h):
    b = h.body_json()
    username = (b.get("username") or "").strip().lower()
    row = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if not row or _hash_pw(b.get("password") or "", row["salt"]) != row["pass_hash"]:
        raise ApiError(401, "Usuario o contraseña incorrectos.")
    user = _public_user(row)
    if not user["approved"]:
        raise ApiError(403, "Tu cuenta todavía no está aprobada por el administrador.")
    token = _new_session(h, user)
    _set_session_cookie(h, token)
    return {"ok": True, "user": user}


@route("POST", "/api/auth/logout")
def auth_logout(h):
    cookie = h.headers.get("Cookie") or ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("advvx_session="):
            SESSIONS.pop(part.split("=", 1)[1].strip(), None)
    h._set_cookie = "advvx_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"
    return {"ok": True}


@route("GET", "/api/auth/me")
def auth_me(h):
    u = _session_user(h)
    return {"user": u}


@route("GET", "/api/admin/users")
def admin_users(h):
    _require_admin(h)
    return [_public_user(r) for r in db.q("SELECT * FROM users ORDER BY id")]


@route("POST", r"/api/admin/users/(\d+)/approve")
def admin_approve(h, uid):
    _require_admin(h)
    if not db.q1("SELECT id FROM users WHERE id=?", (uid,)):
        raise ApiError(404, "Usuario no encontrado.")
    db.run("UPDATE users SET approved=1 WHERE id=?", (uid,))
    return {"ok": True}


@route("POST", r"/api/admin/users/(\d+)/revoke")
def admin_revoke(h, uid):
    me = _require_admin(h)
    if int(uid) == me["id"]:
        raise ApiError(400, "No puedes quitarte el acceso a ti mismo.")
    db.run("UPDATE users SET approved=0 WHERE id=?", (uid,))
    for tok, s in list(SESSIONS.items()):
        if s["user"]["id"] == int(uid):
            SESSIONS.pop(tok, None)
    return {"ok": True}


@route("DELETE", r"/api/admin/users/(\d+)")
def admin_delete_user(h, uid):
    me = _require_admin(h)
    if int(uid) == me["id"]:
        raise ApiError(400, "No puedes eliminarte a ti mismo.")
    db.run("DELETE FROM users WHERE id=?", (uid,))
    for tok, s in list(SESSIONS.items()):
        if s["user"]["id"] == int(uid):
            SESSIONS.pop(tok, None)
    return {"ok": True}


@route("GET", "/api/health")
def health(h):
    return {"ok": True, "version": config.VERSION}


@route("GET", "/api/settings")
def settings_get(h):
    return public_settings()


@route("PUT", "/api/settings")
def settings_put(h):
    _require_admin(h)  # los ajustes son de toda la plataforma; cada amigo puede cambiarlos por trabajo
    patch = h.body_json()
    for k in list(patch):
        if k.endswith("_set"):
            patch.pop(k)
    for k in config.SECRET_KEYS:
        if not patch.get(k):
            patch.pop(k, None)
    if patch.get("clip_min_s") and patch.get("clip_max_s") and int(patch["clip_min_s"]) >= int(patch["clip_max_s"]):
        raise ApiError(400, "La duración mínima debe ser menor que la máxima.")
    if patch.get("content_type") is not None and str(patch["content_type"]).lower() not in pipeline.CONTENT_TYPES:
        raise ApiError(400, "Tipo de contenido no válido (Entrevista, Podcast, Resumen, Película o Gaming).")
    if patch.get("watermark_opacity") is not None:
        try:
            patch["watermark_opacity"] = max(0, min(100, int(patch["watermark_opacity"])))
        except (TypeError, ValueError):
            raise ApiError(400, "La opacidad de la marca de agua debe ser un número de 0 a 100.") from None
    db.set_settings(patch)
    return public_settings()


def _multipart_file(h, max_bytes: int = 5 * 1024 * 1024) -> tuple[str, bytes]:
    """Lee un cuerpo multipart/form-data con un solo archivo y devuelve
    (nombre_original, bytes). Sin dependencias externas: se busca el
    boundary del Content-Type y se recorta el contenido del primer
    part con filename."""
    ctype = h.headers.get("Content-Type") or ""
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        raise ApiError(400, "Falta el boundary multipart (sube el archivo desde el formulario).")
    boundary = m.group(1).strip().strip('"').encode()
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0 or n > max_bytes + 65536:
        raise ApiError(400, "Archivo vacío o demasiado grande (máx. 5 MB).")
    raw = h.rfile.read(n)
    parts = raw.split(b"--" + boundary)
    for part in parts:
        if b"filename=" not in part:
            continue
        head, sep, body = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        fname = re.search(rb'filename="([^"]*)"', head)
        name = (fname.group(1).decode("utf-8", "replace") if fname else "imagen")[:120]
        body = body.rsplit(b"\r\n", 1)[0] if body.endswith(b"\r\n") else body
        if len(body) > max_bytes:
            raise ApiError(400, "Archivo demasiado grande (máx. 5 MB).")
        return name, body
    raise ApiError(400, "No se encontró ningún archivo en la subida.")


@route("GET", "/api/settings/watermark")
def watermark_get(h):
    _require_admin(h)
    custom = config.DATA_DIR / "watermark.png"
    s = db.get_settings()
    return {"custom": custom.exists(), "file": "watermark.png" if custom.exists() else None,
            "watermark_image": s.get("watermark_image", "")}


@route("POST", "/api/settings/watermark")
def watermark_post(h):
    _require_admin(h)  # como los ajustes: solo el administrador sube la imagen de marca
    name, data = _multipart_file(h)
    ext = Path(name).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg"):
        raise ApiError(400, "Solo se aceptan imágenes PNG o JPG.")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        pass
    elif data[:2] == b"\xff\xd8":
        pass
    else:
        raise ApiError(400, "El archivo no es una imagen PNG o JPG válida.")
    dest = config.DATA_DIR / "watermark.png"
    dest.write_bytes(data)
    db.set_settings({"watermark_image": "watermark.png"})
    return {"ok": True, "custom": True, "file": "watermark.png"}


# ------------------------------------------------------------------ cookies.txt
# v3.1.5: subir el archivo cookies.txt (exportado del navegador con la
# sesión de YouTube iniciada) para que el servidor descargue con esa
# sesión. En un servidor no hay navegador, así que "Cookies del
# navegador" no sirve ahí. Solo el administrador sube/quita el archivo.
# El archivo NUNCA se devuelve por ningún endpoint: el GET solo dice
# si hay uno cargado y cuándo se subió.
COOKIES_FILE = "cookies.txt"
COOKIES_MAX_BYTES = 1024 * 1024  # 1 MB


def _looks_like_cookies(data: bytes) -> bool:
    """True si los bytes parecen un archivo de cookies Netscape
    (cookies.txt exportado de un navegador)."""
    text = data.decode("utf-8", "ignore")
    if "# Netscape HTTP Cookie File" in text:
        return True
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7 and parts[4].strip().isdigit():
            return True
    return False


def _cookies_status() -> dict:
    p = config.DATA_DIR / COOKIES_FILE
    if not p.exists():
        return {"loaded": False, "uploaded_at": None}
    return {"loaded": True,
            "uploaded_at": time.strftime("%d/%m/%Y %H:%M", time.localtime(p.stat().st_mtime))}


@route("GET", "/api/settings/cookies")
def cookies_get(h):
    _require_admin(h)
    return _cookies_status()


@route("POST", "/api/settings/cookies")
def cookies_post(h):
    _require_admin(h)  # como los ajustes: solo el administrador sube sus cookies de YouTube
    name, data = _multipart_file(h)
    if not data:
        raise ApiError(400, "El archivo está vacío.")
    if len(data) > COOKIES_MAX_BYTES:
        raise ApiError(400, "El archivo de cookies es demasiado grande (máx. 1 MB).")
    if not _looks_like_cookies(data):
        raise ApiError(400, "Eso no parece un archivo cookies.txt (formato Netscape). Expórtalo desde tu navegador con la sesión de YouTube iniciada.")
    dest = config.DATA_DIR / COOKIES_FILE
    dest.write_bytes(data)
    db.set_settings({"cookies_file": str(dest)})
    return {"ok": True, **_cookies_status()}


@route("DELETE", "/api/settings/cookies")
def cookies_delete(h):
    _require_admin(h)
    (config.DATA_DIR / COOKIES_FILE).unlink(missing_ok=True)
    db.set_settings({"cookies_file": ""})
    return {"ok": True, "loaded": False, "uploaded_at": None}


def _version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=15, **config.NOWIN)
        return (out.stdout or out.stderr).decode("utf-8", "replace").strip().splitlines()[0][:80]
    except Exception:  # noqa: BLE001
        return ""


@route("GET", "/api/system")
def system(h):
    import importlib.util
    s = db.get_settings()
    from . import transcribe
    model, device, _ = transcribe.engine.plan(s) if hasattr(transcribe.engine, "plan") else ("?", "?", "")
    return {"ffmpeg": _version([config.ffmpeg(), "-version"]), "ytdlp": _version([config.ytdlp(), "--version"]),
            "whisper_installed": importlib.util.find_spec("faster_whisper") is not None,
            "whisper_plan": f"{model} · {device}", "node": bool(shutil.which("node")),
            "deno": bool(shutil.which("deno")), "opencv": tracking.available(), "ai": ai.status(s), "version": config.VERSION,
            "data_dir": str(config.DATA_DIR)}


@route("POST", "/api/system/update-ytdlp")
def update_ytdlp(h):
    _require_admin(h)
    try:
        out = subprocess.run([config.ytdlp(), "-U"], capture_output=True, timeout=180, **config.NOWIN)
        media._help_cache.clear()
        return {"ok": out.returncode == 0, "output": (out.stdout + out.stderr).decode("utf-8", "replace")[-400:]}
    except Exception as e:  # noqa: BLE001
        raise ApiError(400, f"No se pudo actualizar yt-dlp: {e}")


# ------------------------------------------------------------------ IA
@route("GET", "/api/ai/status")
def ai_status(h):
    return {**ai.status(db.get_settings()), "pull": dict(_pull)}


@route("POST", "/api/ai/test")
def ai_test(h):
    s = db.get_settings()
    try:
        p = ai.resolve(s)
        if p == "builtin":
            return {"ok": True, "message": "Motor integrado activo (gratis, sin IA externa). "
                                           "Instala Ollama para títulos y puntajes mucho mejores."}
        out = ai.chat(s, "Responde solo con la palabra OK.", "Di OK", json_mode=False, max_tokens=10)
        return {"ok": True, "message": f"La IA responde correctamente ({ai.status(s)['engine']}): {out.strip()[:30]}"}
    except ai.AIError as e:
        return {"ok": False, "message": f"{e} — mientras tanto se usa el motor integrado."}


_pull = {"running": False, "status": "", "completed": 0, "total": 0, "error": ""}


def _do_pull(url: str, model: str):
    _pull.update(running=True, status="Iniciando…", completed=0, total=0, error="")
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/pull", method="POST",
                                     data=json.dumps({"model": model, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3600) as r:
            for line in r:
                d = json.loads(line.decode())
                if d.get("error"):
                    raise RuntimeError(d["error"])
                _pull.update(status=d.get("status", ""), completed=d.get("completed", 0) or 0, total=d.get("total", 0) or 0)
        _pull.update(status="Modelo listo", completed=1, total=1)
    except Exception as e:  # noqa: BLE001
        _pull["error"] = str(e)[:300] or type(e).__name__
    finally:
        _pull["running"] = False


@route("POST", "/api/ai/pull")
def ai_pull(h):
    s = db.get_settings()
    if _pull["running"]:
        return {"ok": True}
    if not ai.ollama_status(s)["running"]:
        raise ApiError(400, "Ollama no está encendido. Instálalo desde ollama.com/download y ábrelo.")
    threading.Thread(target=_do_pull, args=(s["ollama_url"], s["ollama_model"]), daemon=True).start()
    return {"ok": True}


# ------------------------------------------------------------------ jobs
def _me(h) -> dict:
    return getattr(h, "_user", None) or {"id": 0, "is_admin": False}


def _own_job(h, jid: int) -> dict:
    j = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise ApiError(404, "Proyecto no encontrado.")
    me = _me(h)
    if not me["is_admin"] and j.get("user_id", 0) not in (0, me["id"]):
        raise ApiError(403, "Ese proyecto no es tuyo.")
    return j


def _new_job(kind: str, source: str, title: str, opts: dict, user_id: int = 0) -> int:
    clean = {k: v for k, v in (opts.get("settings") or {}).items() if k in pipeline.OVERRIDABLE}
    if clean.get("content_type") is not None and str(clean["content_type"]).lower() not in pipeline.CONTENT_TYPES:
        raise ApiError(400, "Tipo de contenido no válido (Entrevista, Podcast, Resumen, Película o Gaming).")
    given = (opts.get("creator") or "").strip()
    extra = {"settings": clean, "creator_given": bool(given)}
    if opts.get("max_videos") is not None:
        try:
            extra["max_videos"] = max(1, min(20, int(opts["max_videos"])))
        except (TypeError, ValueError):
            pass
    jid = db.run("INSERT INTO jobs(kind,title,source,status,options,user_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                 (kind, title, source, "queued", json.dumps(extra), user_id,
                  db.now(), db.now()))
    creators.detect_for_new_job(jid, source if source.startswith("http") else "", given)
    return jid


def _channel_clips(jid: int) -> int:
    """Clips de un proyecto de canal = suma de los clips de sus videos hijos."""
    ids = pipeline.channel_children(jid)
    if not ids:
        return 0
    row = db.q1(f"SELECT COUNT(*) n FROM clips WHERE job_id IN ({','.join('?' * len(ids))})", ids)
    return row["n"] if row else 0


def _check_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        raise ApiError(400, "Pega una URL completa que empiece con http:// o https://")
    return url


@route("POST", "/api/jobs/video")
def job_video(h):
    b = h.body_json()
    url = _check_url(b.get("url"))
    jid = _new_job("video", url, url[:120], b, _me(h)["id"])
    pipeline.start_video(jid)
    return {"id": jid}


@route("POST", "/api/jobs/channel")
def job_channel(h):
    """Modo canal: lista los videos más recientes del canal y crea clips de cada uno."""
    b = h.body_json()
    url = _check_url(b.get("url"))
    try:
        max_videos = max(1, min(20, int(b.get("max_videos") or 5)))
    except (TypeError, ValueError):
        raise ApiError(400, "Cantidad de videos no válida (1 a 20).")
    jid = _new_job("channel", url, url[:120], {**b, "max_videos": max_videos}, _me(h)["id"])
    pipeline.start_channel(jid)
    return {"id": jid}


@route("POST", "/api/jobs/live")
def job_live(h):
    b = h.body_json()
    url = _check_url(b.get("url"))
    jid = _new_job("live", url, url[:120], b, _me(h)["id"])
    live.start_live(jid)
    return {"id": jid}


@route("POST", "/api/upload")
def upload(h):
    name = re.sub(r"[^\w.\- ]", "_", h.query.get("name", "video.mp4"))[-120:]
    ext = Path(name).suffix.lower()
    if ext not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mp3", ".wav", ".m4a", ".ts", ".flv"):
        raise ApiError(400, "Formato no soportado (usa mp4, mov, mkv, webm, avi, mp3, wav…).")
    try:
        opts = json.loads(h.query.get("settings", "{}"))
    except json.JSONDecodeError:
        opts = {}
    jid = _new_job("video", "", Path(name).stem[:120], {"settings": opts, "creator": h.query.get("creator", "")}, _me(h)["id"])
    dest = pipeline.job_dir(jid) / ("source" + ext)
    left = int(h.headers.get("Content-Length") or 0)
    if left <= 0:
        raise ApiError(400, "Archivo vacío.")
    with open(dest, "wb") as f:
        while left > 0:
            chunk = h.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left > 0:
        dest.unlink(missing_ok=True)
        db.run("DELETE FROM jobs WHERE id=?", (jid,))
        raise ApiError(400, "La subida se interrumpió.")
    db.job_update(jid, source=str(dest))
    pipeline.start_video(jid)
    return {"id": jid}


ACTIVE = ("queued", "downloading", "transcribing", "analyzing", "rendering", "waiting", "recording", "processing")


@route("GET", "/api/jobs")
def jobs_list(h):
    me = _me(h)
    where = "" if me["is_admin"] else "WHERE j.user_id=? "
    args = [] if me["is_admin"] else [me["id"]]
    rows = db.q("SELECT j.*, (SELECT COUNT(*) FROM clips c WHERE c.job_id=j.id) AS clips FROM jobs j "
                + where + "ORDER BY j.id DESC LIMIT 100", args)
    for r in rows:
        if r["kind"] == "channel":
            r["clips"] = _channel_clips(r["id"])
        r.pop("options", None)
        r["active"] = r["status"] in ACTIVE
    return rows


@route("GET", r"/api/jobs/(\d+)")
def job_get(h, jid):
    _own_job(h, int(jid))
    j = db.q1("SELECT j.*, (SELECT COUNT(*) FROM clips c WHERE c.job_id=j.id) AS clips FROM jobs j WHERE id=?", (jid,))
    if not j:
        raise ApiError(404, "Proyecto no encontrado.")
    if j["kind"] == "channel":
        j["clips"] = _channel_clips(j["id"])
        j["channel_children"] = pipeline.channel_children(j["id"])
    j.pop("options", None)
    j["active"] = j["status"] in ACTIVE
    j["events"] = db.q("SELECT ts,text FROM events WHERE job_id=? ORDER BY id DESC LIMIT 40", (jid,))[::-1]
    return j


@route("POST", r"/api/jobs/(\d+)/stop")
def job_stop(h, jid):
    _own_job(h, int(jid))
    pipeline.cancel_event(int(jid)).set()
    live.stop_live(int(jid))
    return {"ok": True}


@route("DELETE", r"/api/jobs/(\d+)")
def job_delete(h, jid):
    _own_job(h, int(jid))
    j = db.q1("SELECT kind, status FROM jobs WHERE id=?", (jid,))
    if not j:
        raise ApiError(404, "Proyecto no encontrado.")
    if j["status"] in ACTIVE:
        raise ApiError(400, "Detén el proyecto antes de borrarlo.")
    if j["kind"] == "channel":
        for cid in pipeline.channel_children(int(jid)):
            cj = db.q1("SELECT status FROM jobs WHERE id=?", (cid,))
            if cj and cj["status"] in ACTIVE:
                raise ApiError(400, "Detén el proyecto antes de borrarlo.")
            db.run("DELETE FROM clips WHERE job_id=?", (cid,))
            db.run("DELETE FROM events WHERE job_id=?", (cid,))
            db.run("DELETE FROM jobs WHERE id=?", (cid,))
            shutil.rmtree(config.JOBS_DIR / str(cid), ignore_errors=True)
    db.run("DELETE FROM clips WHERE job_id=?", (jid,))
    db.run("DELETE FROM events WHERE job_id=?", (jid,))
    db.run("DELETE FROM jobs WHERE id=?", (jid,))
    shutil.rmtree(config.JOBS_DIR / str(jid), ignore_errors=True)
    return {"ok": True}


@route("GET", "/api/stats")
def stats(h):
    me = _me(h)
    uj = "" if me["is_admin"] else "WHERE j.user_id=%d " % me["id"]
    row = db.q1(f"SELECT COUNT(*) n, COALESCE(AVG(score),0) a FROM clips c JOIN jobs j ON j.id=c.job_id {uj}")
    return {"clips": row["n"], "avg_score": round(row["a"]),
            "jobs": db.q1(f"SELECT COUNT(*) n FROM jobs j {uj}")["n"],
            "active": db.q1(f"SELECT COUNT(*) n FROM jobs j {uj} {'AND' if uj else 'WHERE'} status IN ({','.join('?' * len(ACTIVE))})", ACTIVE)["n"],
            "live_active": len(live.SESSIONS)}


# ------------------------------------------------------------------ clips
def _clip_row(c: dict, full=False) -> dict:
    d = {k: c[k] for k in ("id", "job_id", "title", "start", "end", "score", "reason", "theme", "layout", "created_at")}
    d["duration"] = round(c["end"] - c["start"], 1)
    d["has_thumb"] = bool(c["thumb"])
    d["creator"], d["creator_key"] = c.get("cname", ""), c.get("ckey", "")
    d["origin"], d["editor_id"] = c.get("origin", "") or "", c.get("editor_id", 0) or 0
    # v3.1: tipo de momento y resumen (los pone el detector de momentos; "" si aún no existen)
    d["moment"] = c.get("moment") or ""
    d["summary"] = c.get("summary") or ""
    d["job_title"] = c.get("job_title") or ""
    d["job_kind"] = c.get("job_kind") or ""
    if full:
        d["transcript"] = c["transcript"]
    return d


def _clip(h, cid) -> dict:
    ek, en = creators.EFFECTIVE
    c = db.q1(f"SELECT c.*, j.user_id AS juser, ({ek}) AS ckey, ({en}) AS cname FROM clips c "
              "LEFT JOIN jobs j ON j.id=c.job_id WHERE c.id=?", (cid,))
    if not c:
        raise ApiError(404, "Clip no encontrado.")
    me = _me(h)
    if not me["is_admin"] and c.get("juser", 0) not in (0, me["id"]):
        raise ApiError(403, "Ese clip no es tuyo.")
    return c
    return c


@route("GET", "/api/clips")
def clips_list(h):
    ek, en = creators.EFFECTIVE
    where, args = [], []
    me = _me(h)
    if not me["is_admin"]:
        where.append("j.user_id=?")
        args.append(me["id"])
    if h.query.get("job_id"):
        where.append("c.job_id=?")
        args.append(int(h.query["job_id"]))
    if h.query.get("min_score"):
        where.append("c.score>=?")
        args.append(int(h.query["min_score"]))
    if h.query.get("q"):
        where.append("(c.title LIKE ? OR c.transcript LIKE ?)")
        args += [f"%{h.query['q']}%"] * 2
    if "creator" in h.query:
        where.append(f"({ek})=?")
        args.append("" if h.query["creator"] == "none" else h.query["creator"])
    sql = (f"SELECT c.*, j.title AS job_title, j.kind AS job_kind, ({ek}) AS ckey, ({en}) AS cname FROM clips c LEFT JOIN jobs j ON j.id=c.job_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY c.id DESC LIMIT 400")
    return [_clip_row(c) for c in db.q(sql, args) if c["path"]]


@route("GET", "/api/creators")
def creators_list(h):
    return creators.list_creators()


@route("PUT", r"/api/creators/([a-z0-9]+)")
def creators_rename(h, key):
    return {"key": creators.rename(key, h.body_json().get("name", ""))}


@route("POST", "/api/clips/move-creator")
def clips_move_creator(h):
    b = h.body_json()
    ids = [int(i) for i in (b.get("ids") or [])]
    if not ids:
        raise ApiError(400, "Selecciona al menos un clip.")
    creators.move_clips(ids, b.get("creator", ""))
    return {"ok": True, "moved": len(ids)}


@route("GET", r"/api/clips/(\d+)")
def clip_get(h, cid):
    return _clip_row(_clip(h, cid), full=True)


@route("PATCH", r"/api/clips/(\d+)")
def clip_patch(h, cid):
    title = (h.body_json().get("title") or "").strip()[:120]
    if not title:
        raise ApiError(400, "El título no puede estar vacío.")
    _clip(h, cid)
    db.run("UPDATE clips SET title=? WHERE id=?", (title, cid))
    return {"ok": True}


@route("DELETE", r"/api/clips/(\d+)")
def clip_delete(h, cid):
    c = _clip(h, cid)
    db.run("DELETE FROM clips WHERE id=?", (cid,))
    shutil.rmtree(Path(c["path"]).parent, ignore_errors=True)
    return {"ok": True}


def _no_edit_origin(c: dict):
    if c.get("origin") == "edit":
        raise ApiError(400, "Este clip se creó en el editor: ábrelo en el editor para modificarlo.")


def _safe_name(title: str) -> str:
    return (re.sub(r'[\\/:*?"<>|]+', "", title).strip()[:80] or "clip") + ".mp4"


@route("GET", r"/api/clips/(\d+)/video")
def clip_video(h, cid):
    h.send_file(Path(_clip(h, cid)["path"]))


@route("GET", r"/api/clips/(\d+)/download")
def clip_download(h, cid):
    c = _clip(h, cid)
    h.send_file(Path(c["path"]), download_name=_safe_name(c["title"]))


@route("GET", r"/api/clips/(\d+)/thumb")
def clip_thumb(h, cid):
    h.send_file(Path(_clip(h, cid)["thumb"] or ""), cache=True)


@route("GET", "/api/themes")
def themes(h):
    return [{"id": k, "label": v["label"]} for k, v in captions.THEMES.items()]


@route("GET", r"/api/clips/(\d+)/captions")
def clip_captions(h, cid):
    c = _clip(h, cid)
    words = json.loads(c["words"] or "[]")
    return {"chunks": captions.chunk_texts(words, vertical=c["layout"] != "original"), "theme": c["theme"],
            "layout": c["layout"], "caption": ai.caption_text(db.get_settings(), c["title"], c["transcript"])
            if h.query.get("caption") else ""}


@route("PUT", r"/api/clips/(\d+)/captions")
def clip_captions_put(h, cid):
    c = _clip(h, cid)
    _no_edit_origin(c)
    texts = h.body_json().get("texts")
    if not isinstance(texts, list):
        raise ApiError(400, "Faltan los textos.")
    words = captions.apply_edited_texts(json.loads(c["words"] or "[]"), [str(t) for t in texts],
                                        vertical=c["layout"] != "original")
    pipeline.rerender_clip(int(cid), words=words)
    return {"ok": True}


@route("POST", r"/api/clips/(\d+)/rerender")
def clip_rerender(h, cid):
    b = h.body_json()
    _no_edit_origin(_clip(h, cid))
    if b.get("theme") and b["theme"] not in captions.THEMES:
        raise ApiError(400, "Tema desconocido.")
    if b.get("layout") and b["layout"] not in ("blur", "fill", "original", "gaming", "duo", "speaker", "camara"):
        raise ApiError(400, "Formato desconocido.")
    pipeline.rerender_clip(int(cid), b.get("theme"), b.get("layout"), b.get("captions"))
    return {"ok": True}


# ------------------------------------------------------------------ editor de video
def _pid(h, pid) -> int:
    pid = int(pid)
    row = db.q1("SELECT id, user_id FROM editor_projects WHERE id=?", (pid,))
    if not row:
        raise ApiError(404, "Proyecto del editor no encontrado.")
    me = _me(h)
    if not me["is_admin"] and row.get("user_id", 0) not in (0, me["id"]):
        raise ApiError(403, "Ese proyecto no es tuyo.")
    return pid


def _project_payload(pid: int) -> dict:
    p = editor.get_project(pid)
    spec = p["spec"]
    used = {s["clip_id"] for s in spec["segments"]}
    assets = {}
    ids = [o["asset"] for o in spec["overlays"] if o["type"] == "image"] + ([spec["music"]["asset"]] if spec["music"] else [])
    for a in ids:
        r = editor.get_asset(a)
        if r:
            assets[a] = {"id": a, "name": r["name"], "kind": r["kind"], "duration": r["duration"]}
    clips = {}
    for cid in used:
        c = db.q1("SELECT id,title,job_id,score FROM clips WHERE id=?", (cid,))
        if c:
            clips[cid] = c
    return {**p, "proxies": editor.proxies_status(spec), "prep_error": editor.prep_error(pid), "assets": assets,
            "clips": clips, "total": editor.total_duration(spec)}


@route("GET", "/api/editor/meta")
def editor_meta(h):
    return {"themes": [{"id": k, **{x: v[x] for x in ("label", "font", "upper", "size", "primary", "active", "outline",
                                                        "outline_k", "mode", "pop")}, "box": bool(v.get("box")),
                        "dim": v.get("dim", v["primary"])} for k, v in captions.THEMES.items()],
            "animations": [{"id": k, "label": v} for k, v in captions.ANIMATIONS.items()],
            "tracking": tracking.available()}


@route("POST", "/api/editor/projects")
def editor_create(h):
    b = h.body_json()
    ids = [int(i) for i in (b.get("clip_ids") or [])]
    if not ids:
        raise ApiError(400, "Selecciona al menos un clip.")
    for cid in ids:
        _clip(h, cid)  # solo clips propios
    return {"id": editor.create_project(ids, b.get("name", ""), _me(h)["id"])}


@route("GET", "/api/editor/projects")
def editor_list(h):
    me = _me(h)
    return editor.list_projects(me["id"], me["is_admin"])


@route("GET", r"/api/editor/projects/(\d+)")
def editor_get(h, pid):
    return _project_payload(_pid(h, pid))


@route("PUT", r"/api/editor/projects/(\d+)")
def editor_save(h, pid):
    b = h.body_json()
    editor.save_project(_pid(h, pid), b.get("spec") or {}, b.get("name"))
    return {"ok": True}


@route("DELETE", r"/api/editor/projects/(\d+)")
def editor_delete(h, pid):
    editor.delete_project(_pid(h, pid))
    return {"ok": True}


@route("POST", r"/api/editor/projects/(\d+)/clips")
def editor_add_clip(h, pid):
    editor.add_clip(_pid(h, pid), int(h.body_json().get("clip_id", 0)))
    return _project_payload(int(pid))


@route("GET", r"/api/editor/projects/(\d+)/proxy/(\d+)")
def editor_proxy(h, pid, cid):
    info = editor.proxy_info(int(cid))
    if not info:
        raise ApiError(404, "La vista previa de este clip aún se está preparando.")
    h.send_file(Path(info["path"]))


@route("POST", r"/api/editor/projects/(\d+)/track")
def editor_track(h, pid):
    b = h.body_json()
    spec = editor.sanitize(b.get("spec") or editor.get_project(_pid(h, pid))["spec"])
    kind = b.get("kind")
    try:
        if kind == "face":
            tid, tr = editor.analyze_face(spec, str(b.get("seg", "")))
        elif kind == "object":
            tid, tr = editor.analyze_object(spec, str(b.get("seg", "")), float(b.get("t", 0)), b.get("box") or {},
                                            str(b.get("label") or "Objeto"))
        else:
            raise ApiError(400, "Tipo de seguimiento desconocido.")
    except tracking.TrackingUnavailable as e:
        raise ApiError(400, str(e))
    return {"id": tid, "track": tr}


@route("POST", r"/api/editor/projects/(\d+)/resync")
def editor_resync(h, pid):
    b = h.body_json()
    spec = editor.sanitize(b.get("spec") or editor.get_project(_pid(h, pid))["spec"])
    try:
        seg = editor.resync_segment(spec, str(b.get("seg", "")), db.get_settings())
    except ImportError:
        raise ApiError(400, "Whisper no está instalado: ejecuta iniciar.bat.")
    return {"words": seg["words"]}


@route("POST", r"/api/editor/projects/(\d+)/export")
def editor_export(h, pid):
    b = h.body_json()
    pid = _pid(h, pid)
    if b.get("spec"):
        editor.save_project(pid, b["spec"], b.get("name"))
    quality = "draft" if b.get("quality") == "draft" else "final"
    editor.start_export(pid, quality)
    return {"ok": True}


@route("POST", r"/api/editor/projects/(\d+)/export/cancel")
def editor_export_cancel(h, pid):
    editor.cancel_export(_pid(h, pid))
    return {"ok": True}


@route("GET", r"/api/editor/projects/(\d+)/draft")
def editor_draft(h, pid):
    h.send_file(editor.draft_path(_pid(h, pid)))


@route("POST", "/api/editor/assets")
def editor_asset_upload(h):
    name = re.sub(r"[^\w.\- ]", "_", h.query.get("name", "archivo"))[-100:]
    left = int(h.headers.get("Content-Length") or 0)
    if left <= 0 or left > 300 * 1024 * 1024:
        raise ApiError(400, "Archivo vacío o demasiado grande (máx. 300 MB).")
    tmp = editor.ASSET_DIR / ("up_" + editor.uid("t") + Path(name).suffix.lower())
    with open(tmp, "wb") as f:
        while left > 0:
            chunk = h.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    if left > 0:
        tmp.unlink(missing_ok=True)
        raise ApiError(400, "La subida se interrumpió.")
    return editor.add_asset(tmp, name)


@route("GET", "/api/editor/assets")
def editor_assets(h):
    rows = db.q("SELECT id,name,kind,duration FROM editor_assets ORDER BY created_at DESC LIMIT 100")
    return [r for r in rows if editor.get_asset(r["id"])]


@route("GET", r"/api/editor/assets/([0-9a-f]{12})")
def editor_asset_file(h, aid):
    a = editor.get_asset(aid)
    if not a:
        raise ApiError(404, "Archivo no encontrado.")
    h.send_file(Path(a["path"]), cache=True)


# ------------------------------------------------------------------ cuentas sociales (v3.1.3)
def _own_account(h, aid: int) -> dict:
    row = db.q1("SELECT * FROM social_accounts WHERE id=?", (aid,))
    if not row:
        raise ApiError(404, "Cuenta no encontrada.")
    if row["user_id"] != _me(h)["id"]:
        raise ApiError(403, "Esa cuenta no es tuya.")
    return row


def _own_post(h, pid: int) -> dict:
    row = db.q1("SELECT * FROM social_posts WHERE id=?", (pid,))
    if not row:
        raise ApiError(404, "Publicación no encontrada.")
    if row["user_id"] != _me(h)["id"]:
        raise ApiError(403, "Esa publicación no es tuya.")
    return row


@route("GET", "/api/social/platforms")
def social_platforms(h):
    s = db.get_settings()
    host = h.headers.get("Host") or "127.0.0.1:8765"
    out = {}
    for key, meta in social.PLATFORMS.items():
        out[key] = {"label": meta["label"], "title_limit": meta["title_limit"],
                    "caption_limit": meta["caption_limit"],
                    "native_scheduling": meta["native_scheduling"],
                    "creds": social.platform_status(s)[key]["creds"],
                    "guide": [g.format(redirect_uri=social.redirect_uri_for(host)) for g in meta["guide"]]}
    return {"platforms": out, "is_admin": _me(h)["is_admin"],
            "public_base_url": s.get("social_public_base_url") or "",
            "mock": social.MOCK}


@route("GET", "/api/social/accounts")
def social_accounts(h):
    me = _me(h)
    return [social.public_account(r) for r in
            db.q("SELECT * FROM social_accounts WHERE user_id=? ORDER BY platform", (me["id"],))]


@route("POST", r"/api/social/accounts/([a-z]+)/connect")
def social_connect(h, platform):
    s = db.get_settings()
    host = h.headers.get("Host") or "127.0.0.1:8765"
    try:
        return social.start_connect(_me(h)["id"], platform, s, host)
    except ValueError as e:
        raise ApiError(400, str(e))


@route("GET", "/api/social/oauth/callback")
def social_callback(h):
    code = h.query.get("code", "")
    state = h.query.get("state", "")
    err = h.query.get("error", "")
    dest = "/#/cuentas"
    if err:
        dest += "?error=" + err
    elif not code or not state:
        dest += "?error=no_code"
    else:
        try:
            acc = social.finish_oauth(code, state, db.get_settings())
            dest += "?connected=" + acc["platform"]
        except Exception as e:  # noqa: BLE001
            dest += "?error=" + urllib.parse.quote(str(e)[:200])
    h.send_response(302)
    h.send_header("Location", dest)
    h.send_header("Content-Length", "0")
    h.end_headers()
    return None


@route("DELETE", r"/api/social/accounts/(\d+)")
def social_account_delete(h, aid):
    row = _own_account(h, int(aid))
    db.run("DELETE FROM social_posts WHERE account_id=? AND status IN ('queued','scheduled')", (row["id"],))
    db.run("DELETE FROM social_accounts WHERE id=?", (row["id"],))
    return {"ok": True}


@route("GET", "/api/social/queue")
def social_queue(h):
    me = _me(h)
    return [social.public_post(r) for r in
            db.q("SELECT * FROM social_posts WHERE user_id=? ORDER BY id DESC LIMIT 200", (me["id"],))]


def _parse_publish_at(v):
    if v in (None, "", 0):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    from datetime import datetime
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.timestamp())


@route("POST", "/api/social/publish")
def social_publish(h):
    b = h.body_json()
    me = _me(h)
    clip = _clip(h, int(b.get("clip_id") or 0))
    title = (b.get("title") or clip["title"] or "").strip()[:200]
    if not title:
        raise ApiError(400, "Escribe un título.")
    hashtags = (b.get("hashtags") or "").strip()[:500]
    publish_at = _parse_publish_at(b.get("publish_at"))
    if publish_at and publish_at < int(time.time()) - 60:
        raise ApiError(400, "La fecha y hora ya pasaron. Elige una hora futura o publica ahora.")
    account_ids = [int(a) for a in (b.get("accounts") or [])]
    if not account_ids:
        plats = [str(p) for p in (b.get("platforms") or [])]
        if not plats:
            raise ApiError(400, "Elige al menos una cuenta.")
        rows = db.q("SELECT id FROM social_accounts WHERE user_id=? AND platform IN (%s)"
                    % ",".join("?" * len(plats)), (me["id"], *plats))
        account_ids = [r["id"] for r in rows]
    if not account_ids:
        raise ApiError(400, "No tienes cuentas conectadas en esas plataformas.")
    created = []
    for aid in account_ids:
        acc = _own_account(h, aid)
        pid = db.run("INSERT INTO social_posts(user_id,clip_id,platform,account_id,title,hashtags,"
                     "publish_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (me["id"], clip["id"], acc["platform"], aid, title, hashtags,
                      publish_at, "queued", db.now(), db.now()))
        row = db.q1("SELECT * FROM social_posts WHERE id=?", (pid,))
        if not publish_at:
            row = social.publish_post(pid)  # publicar ahora, en esta misma petición
        created.append(social.public_post(row))
    return {"ok": True, "posts": created}


@route("GET", r"/api/social/queue/(\d+)")
def social_queue_get(h, pid):
    return social.public_post(_own_post(h, int(pid)))


@route("DELETE", r"/api/social/queue/(\d+)")
def social_queue_delete(h, pid):
    row = _own_post(h, int(pid))
    if row["status"] not in ("queued", "scheduled"):
        raise ApiError(400, "Ya no se puede cancelar: está " + row["status"] + ".")
    if row["status"] == "scheduled" and row["external_id"]:
        # Programación nativa (YouTube/Facebook): borrar aquí no la quita de la
        # plataforma; el usuario debe cancelarla allí. Se avisa, no se oculta.
        db.run("DELETE FROM social_posts WHERE id=?", (row["id"],))
        return {"ok": True, "warning": "Se eliminó de AdvvxClips, pero si ya se programó en la plataforma, cancélala también allí."}
    db.run("DELETE FROM social_posts WHERE id=?", (row["id"],))
    return {"ok": True}


# ======================================================================= arranque
class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):   # el navegador cierra conexiones keep-alive: no es un error
        if issubclass(sys.exc_info()[0] or Exception, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def mark_stale_jobs():
    for st in ACTIVE:
        db.run("UPDATE jobs SET status='stopped', message='Interrumpido al cerrar la aplicación.' WHERE status=?", (st,))


def serve(port: int = 8765, open_browser: bool = True, host: str | None = None):
    db.init()
    social.start_scheduler()  # publica las programadas cada 60 s (mientras la app está abierta)
    creators.backfill()
    mark_stale_jobs()
    # En tu PC: 127.0.0.1. En un servidor (para el link de tus amigos):
    # TBX_PUBLIC=1 y --host 0.0.0.0. La API siempre exige iniciar sesión.
    host = host or ("0.0.0.0" if config.PUBLIC else "127.0.0.1")
    display = "127.0.0.1" if host == "0.0.0.0" else host
    try:
        httpd = QuietServer((host, port), Handler)
    except OSError:
        try:   # ya hay una instancia: solo abre el navegador
            with urllib.request.urlopen(f"http://{display}:{port}/api/health", timeout=2):
                print(f"AdvvxClips ya está abierto en http://{display}:{port}")
                if open_browser:
                    webbrowser.open(f"http://{display}:{port}")
                return
        except Exception:  # noqa: BLE001
            raise SystemExit(f"El puerto {port} está ocupado por otro programa. Usa: python -m app --port 8800")
    httpd.daemon_threads = True
    url = f"http://{display}:{port}"
    print(f"\n  AdvvxClips listo → {url}\n  (deja esta ventana abierta; ciérrala para salir)\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for jid in list(live.SESSIONS):
            live.stop_live(jid)
