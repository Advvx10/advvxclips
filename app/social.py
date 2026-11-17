"""Cuentas sociales (v3.1.3): conectar TikTok, Facebook, Instagram y YouTube y
publicar clips sin salir de la página, ahora o programados.

Honestidad ante todo:
- YouTube y Facebook: implementación real (OAuth + subida + programación nativa).
- Instagram: implementación real vía Graph API, PERO exige que el video esté en
  una URL pública (la API no acepta subir el archivo directamente). Sin URL
  pública configurada, publicar en Instagram falla con un mensaje claro.
- TikTok: implementación real del Content Posting API cuando hay credenciales
  aprobadas; sin ellas la UI muestra la guía y el handoff a TikTok Studio.
- NADA de esto se probó contra cuentas reales (no hay credenciales del usuario
  aquí). Ninguna cuenta se muestra como "conectada" sin un intercambio de token
  real. Los tokens se guardan por usuario en la base local (mismo nivel de
  confianza que los ajustes) y NUNCA se registran en logs ni se devuelven por API.
- El programador solo publica mientras el servidor está corriendo. No hay
  reintentos automáticos: un fallo queda visible con su motivo.
- Modo de pruebas: solo con la variable de entorno TBX_SOCIAL_MOCK=1 el
  publicador devuelve un resultado falso sin tocar la red. No hay ningún
  interruptor de mock en los ajustes ni en la UI.
"""
import json
import mimetypes
import os
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, db

PLATFORMS = {
    "youtube": {
        "label": "YouTube",
        "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "title_limit": 100, "caption_limit": 5000,
        "native_scheduling": True,
        "guide": [
            "Ve a Google Cloud Console (console.cloud.google.com) y crea un proyecto.",
            "En «APIs y servicios → Biblioteca», busca y habilita «YouTube Data API v3».",
            "En «APIs y servicios → Pantalla de consentimiento OAuth», crea la app (tipo Externo) y añade tu correo como usuario de prueba.",
            "En «Credenciales → Crear credenciales → ID de cliente OAuth», elige tipo «Aplicación de escritorio».",
            "Copia el ID de cliente y el secreto y pégalos abajo (solo el administrador).",
            "En «URIs de redireccionamiento autorizados» añade exactamente: {redirect_uri}",
            "Pulsa «Conectar YouTube» y acepta con tu cuenta de Google.",
        ],
    },
    "facebook": {
        "label": "Facebook",
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts"],
        "auth_url": "https://www.facebook.com/v19.0/dialog/oauth",
        "token_url": "https://graph.facebook.com/v19.0/oauth/access_token",
        "title_limit": 0, "caption_limit": 63206,
        "native_scheduling": True,
        "guide": [
            "Ve a Meta for Developers (developers.facebook.com) y crea una app de tipo «Otro».",
            "En el panel de la app, añade el producto «Facebook Login» (y «Instagram» si también quieres Instagram).",
            "En «Facebook Login → Configuración», en «URIs de redireccionamiento OAuth válidos» añade exactamente: {redirect_uri}",
            "Copia el «ID de la app» y la «Clave secreta de la app» y pégalos abajo (solo el administrador).",
            "Pulsa «Conectar Facebook», entra con tu cuenta y elige la Página donde se publicarán los Reels.",
            "La Página debe existir y tú debes ser administrador de ella.",
        ],
    },
    "instagram": {
        "label": "Instagram",
        "scopes": ["pages_show_list", "pages_read_engagement", "pages_manage_posts",
                   "instagram_basic", "instagram_content_publish"],
        "auth_url": "https://www.facebook.com/v19.0/dialog/oauth",
        "token_url": "https://graph.facebook.com/v19.0/oauth/access_token",
        "title_limit": 0, "caption_limit": 2200,
        "native_scheduling": False,
        "guide": [
            "Usa la misma app de Meta for Developers que para Facebook (mismo ID y secreto).",
            "Tu cuenta de Instagram debe ser de Creador o de Empresa y estar vinculada a una Página de Facebook.",
            "En «URIs de redireccionamiento OAuth válidos» añade exactamente: {redirect_uri}",
            "Pulsa «Conectar Instagram», entra con Facebook y elige la Página vinculada a tu Instagram.",
            "IMPORTANTE: Instagram no acepta subir el archivo directamente; el video debe estar en una URL pública. "
            "Configura abajo la URL pública de esta app (con TBX_PUBLIC=1) o la publicación fallará con ese aviso.",
            "Instagram no tiene programación nativa: las publicaciones programadas las publica esta app cuando llega la hora, con el servidor corriendo.",
        ],
    },
    "tiktok": {
        "label": "TikTok",
        "scopes": ["user.info.basic", "video.upload"],
        "auth_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "title_limit": 0, "caption_limit": 2200,
        "native_scheduling": False,
        "guide": [
            "Ve a TikTok for Developers (developers.tiktok.com) y crea una app.",
            "En «Login Kit» añade como URI de redireccionamiento exactamente: {redirect_uri}",
            "Solicita los permisos «user.info.basic» y «Video Upload» (video.upload). TikTok debe APROBAR la app (revisión) para subir videos: sin aprobación no se puede conectar.",
            "Copia el «Client key» y el «Client secret» y pégalos abajo (solo el administrador).",
            "Pulsa «Conectar TikTok» y acepta con tu cuenta.",
            "Sin app aprobada, usa el botón «Abrir en TikTok Studio»: se descarga el clip y lo subes tú en studio.tiktok.com (eso no es una conexión, es un atajo).",
        ],
    },
}

# Estados OAuth pendientes (en memoria): state -> {user_id, platform, redirect_uri}
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()

MOCK = os.environ.get("TBX_SOCIAL_MOCK") == "1"


def public_account(row: dict) -> dict:
    """Una cuenta sin ningún secreto: los tokens nunca salen del servidor."""
    return {"id": row["id"], "platform": row["platform"], "account_name": row["account_name"],
            "account_ref": row["account_ref"], "scopes": row.get("scopes") or "",
            "connected_at": row["connected_at"],
            "expires_at": row.get("expires_at") or 0,
            "expired": bool(row.get("expires_at")) and row["expires_at"] < int(time.time())}


def platform_status(settings: dict) -> dict:
    """Qué credenciales de desarrollador hay configuradas (solo sí/no, sin valores)."""
    return {
        "youtube": {"creds": bool(settings.get("google_client_id") and settings.get("google_client_secret"))},
        "facebook": {"creds": bool(settings.get("meta_app_id") and settings.get("meta_app_secret"))},
        "instagram": {"creds": bool(settings.get("meta_app_id") and settings.get("meta_app_secret"))},
        "tiktok": {"creds": bool(settings.get("tiktok_client_key") and settings.get("tiktok_client_secret"))},
    }


# ------------------------------------------------------------------ HTTP
def _http_json(method: str, url: str, data=None, headers=None, timeout=120):
    body = None
    hdrs = dict(headers or {})
    if data is not None:
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, bytes):
            body = data
        else:
            body = str(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(raw).get("error", {})
            msg = detail.get("message") or detail.get("error_description") or raw
        except Exception:  # noqa: BLE001
            msg = raw or f"HTTP {e.code}"
        raise RuntimeError(f"{e.code}: {msg}")


def _http_form(url: str, fields: dict, timeout=120):
    return _http_json("POST", url, urllib.parse.urlencode(fields).encode("utf-8"),
                      {"Content-Type": "application/x-www-form-urlencoded"}, timeout)


def _multipart(fields: dict, file_field: str, file_path: Path):
    boundary = "----advvx" + secrets.token_hex(12)
    buf = b""
    for k, v in fields.items():
        buf += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{file_path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    buf += file_path.read_bytes()
    buf += f"\r\n--{boundary}--\r\n".encode()
    return buf, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


# ------------------------------------------------------------------ OAuth
def redirect_uri_for(host: str) -> str:
    scheme = "http"
    return f"{scheme}://{host}/api/social/oauth/callback"


def start_connect(user_id: int, platform: str, settings: dict, host: str) -> dict:
    if platform not in PLATFORMS:
        raise ValueError("Plataforma desconocida.")
    meta = PLATFORMS[platform]
    redirect_uri = redirect_uri_for(host)
    guide = [g.format(redirect_uri=redirect_uri) for g in meta["guide"]]
    creds = platform_status(settings)[platform]["creds"]
    if not creds:
        return {"platform": platform, "needs_credentials": True, "auth_url": None,
                "guide": guide, "redirect_uri": redirect_uri}
    state = secrets.token_urlsafe(24)
    with _PENDING_LOCK:
        _PENDING[state] = {"user_id": user_id, "platform": platform,
                           "redirect_uri": redirect_uri, "created_at": int(time.time())}
    if platform == "youtube":
        params = {"client_id": settings["google_client_id"], "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": " ".join(meta["scopes"]),
                  "access_type": "offline", "prompt": "consent", "state": state}
    elif platform in ("facebook", "instagram"):
        params = {"client_id": settings["meta_app_id"], "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": ",".join(meta["scopes"]), "state": state}
    else:  # tiktok
        params = {"client_key": settings["tiktok_client_key"], "redirect_uri": redirect_uri,
                  "response_type": "code", "scope": ",".join(meta["scopes"]), "state": state}
    return {"platform": platform, "needs_credentials": False,
            "auth_url": meta["auth_url"] + "?" + urllib.parse.urlencode(params),
            "guide": guide, "redirect_uri": redirect_uri}


def _store_account(user_id: int, platform: str, name: str, ref: str,
                   access: str, refresh: str, expires_in, scopes: str) -> dict:
    expires_at = int(time.time()) + int(expires_in or 0) if expires_in else 0
    existing = db.q1("SELECT id FROM social_accounts WHERE user_id=? AND platform=? AND account_ref=?",
                     (user_id, platform, ref))
    if existing:
        db.run("UPDATE social_accounts SET account_name=?, access_token=?, refresh_token=?, "
               "expires_at=?, scopes=?, connected_at=? WHERE id=?",
               (name, access, refresh or "", expires_at, scopes, db.now(), existing["id"]))
        row = db.q1("SELECT * FROM social_accounts WHERE id=?", (existing["id"],))
    else:
        aid = db.run("INSERT INTO social_accounts(user_id,platform,account_name,account_ref,"
                     "access_token,refresh_token,expires_at,scopes,connected_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (user_id, platform, name, ref, access, refresh or "", expires_at, scopes, db.now()))
        row = db.q1("SELECT * FROM social_accounts WHERE id=?", (aid,))
    return public_account(row)


def finish_oauth(code: str, state: str, settings: dict) -> dict:
    with _PENDING_LOCK:
        pending = _PENDING.pop(state, None)
    if not pending:
        raise ValueError("La conexión caducó o ya se usó. Vuelve a pulsar «Conectar».")
    platform, user_id = pending["platform"], pending["user_id"]
    redirect_uri = pending["redirect_uri"]
    meta = PLATFORMS[platform]
    if platform == "youtube":
        tok = _http_form(meta["token_url"], {"code": code, "client_id": settings["google_client_id"],
                                             "client_secret": settings["google_client_secret"],
                                             "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
        access, refresh = tok["access_token"], tok.get("refresh_token", "")
        me = _http_json("GET", "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
                        headers={"Authorization": f"Bearer {access}"})
        item = (me.get("items") or [{}])[0]
        return _store_account(user_id, "youtube", item.get("snippet", {}).get("title", "Canal de YouTube"),
                              item.get("id", "youtube"), access, refresh,
                              tok.get("expires_in"), " ".join(meta["scopes"]))
    if platform in ("facebook", "instagram"):
        tok = _http_form(meta["token_url"], {"client_id": settings["meta_app_id"],
                                             "redirect_uri": redirect_uri,
                                             "client_secret": settings["meta_app_secret"],
                                             "code": code})
        access = tok["access_token"]
        try:  # token de larga duración (60 días) en vez del de horas
            long_tok = _http_json("GET", "https://graph.facebook.com/v19.0/oauth/access_token?" + urllib.parse.urlencode(
                {"grant_type": "fb_exchange_token", "client_id": settings["meta_app_id"],
                 "client_secret": settings["meta_app_secret"], "fb_exchange_token": access}))
            access, expires_in = long_tok["access_token"], long_tok.get("expires_in", 0)
        except Exception:  # noqa: BLE001
            expires_in = tok.get("expires_in", 0)
        pages = _http_json("GET", "https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token",
                           headers={"Authorization": f"Bearer {access}"})
        data = pages.get("data") or []
        if not data:
            raise ValueError("No se encontró ninguna Página de Facebook en esta cuenta. Crea una Página y vuelve a conectar.")
        if platform == "facebook":
            page = data[0]
            return _store_account(user_id, "facebook", page["name"], page["id"],
                                  page.get("access_token") or access, "", expires_in,
                                  ",".join(meta["scopes"]))
        # instagram: buscar la cuenta IG vinculada a una de las páginas
        for page in data:
            ig = _http_json("GET", f"https://graph.facebook.com/v19.0/{page['id']}?fields=instagram_business_account",
                            headers={"Authorization": f"Bearer {access}"})
            ig_id = (ig.get("instagram_business_account") or {}).get("id")
            if ig_id:
                info = _http_json("GET", f"https://graph.facebook.com/v19.0/{ig_id}?fields=username",
                                  headers={"Authorization": f"Bearer {access}"})
                return _store_account(user_id, "instagram", "@" + info.get("username", ig_id), ig_id,
                                      page.get("access_token") or access, "", expires_in,
                                      ",".join(meta["scopes"]))
        raise ValueError("Ninguna Página tiene una cuenta de Instagram de Creador o Empresa vinculada. Vincúlala en Facebook y vuelve a conectar.")
    # tiktok
    tok = _http_form(meta["token_url"], {"client_key": settings["tiktok_client_key"],
                                         "client_secret": settings["tiktok_client_secret"],
                                         "code": code, "grant_type": "authorization_code",
                                         "redirect_uri": redirect_uri})
    access, refresh = tok["access_token"], tok.get("refresh_token", "")
    me = _http_json("GET", "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name",
                    headers={"Authorization": f"Bearer {access}"})
    u = (me.get("data") or {}).get("user", {})
    return _store_account(user_id, "tiktok", u.get("display_name", "TikTok"),
                          u.get("open_id", "tiktok"), access, refresh,
                          tok.get("expires_in"), ",".join(meta["scopes"]))


# ------------------------------------------------------------------ tokens
def _valid_token(account: dict, settings: dict) -> str:
    """Devuelve un access_token vigente; refresca el de YouTube/TikTok si caducó."""
    if MOCK:
        return "mock-token"
    tok = account["access_token"]
    if not account.get("expires_at") or account["expires_at"] > int(time.time()) + 60:
        return tok
    platform = account["platform"]
    if platform == "youtube" and account.get("refresh_token"):
        data = _http_form("https://oauth2.googleapis.com/token",
                          {"client_id": settings["google_client_id"],
                           "client_secret": settings["google_client_secret"],
                           "refresh_token": account["refresh_token"], "grant_type": "refresh_token"})
        db.run("UPDATE social_accounts SET access_token=?, expires_at=? WHERE id=?",
               (data["access_token"], int(time.time()) + int(data.get("expires_in", 3600)), account["id"]))
        return data["access_token"]
    if platform == "tiktok" and account.get("refresh_token"):
        data = _http_form("https://open.tiktokapis.com/v2/oauth/token/",
                          {"client_key": settings["tiktok_client_key"],
                           "client_secret": settings["tiktok_client_secret"],
                           "grant_type": "refresh_token", "refresh_token": account["refresh_token"]})
        db.run("UPDATE social_accounts SET access_token=?, refresh_token=?, expires_at=? WHERE id=?",
               (data["access_token"], data.get("refresh_token", account["refresh_token"]),
                int(time.time()) + int(data.get("expires_in", 86400)), account["id"]))
        return data["access_token"]
    raise RuntimeError("El token caducó. Desconecta la cuenta y vuelve a conectarla.")


# ------------------------------------------------------------------ subida por plataforma
def _caption(title: str, hashtags: str) -> str:
    tags = " ".join("#" + t.lstrip("#") for t in (hashtags or "").split() if t.strip())
    return (title + ("\n\n" + tags if tags else "")).strip()


def _upload_youtube(account: dict, settings: dict, video: Path, title: str,
                    hashtags: str, publish_at: int | None) -> tuple[str, str]:
    if MOCK:
        return "mock-yt-id", "https://www.youtube.com/watch?v=mock-yt-id"
    tok = _valid_token(account, settings)
    status = {"privacyStatus": "private"}
    if publish_at:
        status["publishAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(publish_at))
    meta = {"snippet": {"title": title[:100], "description": _caption(title, hashtags)[:5000],
                        "tags": [t.lstrip("#") for t in (hashtags or "").split() if t.strip()][:15],
                        "categoryId": "22"},
            "status": status}
    return _youtube_resumable(tok, video, meta)


def _youtube_resumable(tok: str, video: Path, meta: dict) -> tuple[str, str]:
    size = video.stat().st_size
    req = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode(), method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
                 "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            loc = r.headers.get("Location")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"YouTube ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
    if not loc:
        raise RuntimeError("YouTube no devolvió la URL de subida.")
    chunk = 8 * 1024 * 1024
    with open(video, "rb") as f:
        offset = 0
        while True:
            data = f.read(chunk)
            if not data:
                break
            end = offset + len(data) - 1
            req = urllib.request.Request(loc, data=data, method="PUT", headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {offset}-{end}/{size}"})
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    resp = json.loads(r.read().decode())
                    vid = resp["id"]
                    return vid, f"https://www.youtube.com/watch?v={vid}"
            except urllib.error.HTTPError as e:
                if e.code == 308:  # sube el siguiente trozo
                    offset = end + 1
                    continue
                raise RuntimeError(f"YouTube ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
            offset = end + 1
    raise RuntimeError("YouTube no confirmó la subida.")


def _upload_facebook(account: dict, settings: dict, video: Path, title: str,
                     hashtags: str, publish_at: int | None) -> tuple[str, str]:
    if MOCK:
        return "mock-fb-id", "https://www.facebook.com/mock-fb-id"
    tok = _valid_token(account, settings)
    fields = {"title": title[:255], "description": _caption(title, hashtags)}
    if publish_at:
        fields["published"] = "false"
        fields["scheduled_publish_time"] = str(publish_at)
    body, hdrs = _multipart(fields, "source", video)
    data = _http_json("POST", f"https://graph.facebook.com/v19.0/{account['account_ref']}/videos",
                      body, hdrs, timeout=900)
    vid = data.get("id")
    if not vid:
        raise RuntimeError("Facebook no devolvió el ID del video.")
    return vid, f"https://www.facebook.com/{vid}"


def _upload_instagram(account: dict, settings: dict, video: Path, title: str,
                      hashtags: str, publish_at: int | None) -> tuple[str, str]:
    if MOCK:
        return "mock-ig-id", "https://www.instagram.com/p/mock-ig-id"
    base = (settings.get("social_public_base_url") or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("Instagram exige una URL pública del video. Configura la URL pública de esta app "
                           "en Cuentas (con TBX_PUBLIC=1) y vuelve a intentarlo.")
    tok = _valid_token(account, settings)
    clip_id = video.parent.name  # el clip vive en .../clips/<id>/clip.mp4
    video_url = f"{base}/api/clips/{clip_id}/video"
    ig_id = account["account_ref"]
    cont = _http_json("POST", f"https://graph.facebook.com/v19.0/{ig_id}/media",
                      {"media_type": "REELS", "video_url": video_url,
                       "caption": _caption(title, hashtags)[:2200], "access_token": tok}, timeout=120)
    container = cont.get("id")
    if not container:
        raise RuntimeError("Instagram no creó el contenedor de medios.")
    for _ in range(30):  # esperar a que el video se procese
        st = _http_json("GET", f"https://graph.facebook.com/v19.0/{container}?fields=status_code&access_token={tok}")
        if st.get("status_code") == "FINISHED":
            break
        if st.get("status_code") == "ERROR":
            raise RuntimeError("Instagram no pudo procesar el video (revisa la URL pública).")
        time.sleep(10)
    pub = _http_json("POST", f"https://graph.facebook.com/v19.0/{ig_id}/media_publish",
                     {"creation_id": container, "access_token": tok}, timeout=120)
    mid = pub.get("id")
    if not mid:
        raise RuntimeError("Instagram no confirmó la publicación.")
    return mid, f"https://www.instagram.com/reel/{mid}"


def _upload_tiktok(account: dict, settings: dict, video: Path, title: str,
                   hashtags: str, publish_at: int | None) -> tuple[str, str]:
    if MOCK:
        return "mock-tt-id", "https://www.tiktok.com/@mock/video/mock-tt-id"
    if publish_at:
        raise RuntimeError("TikTok no permite programar desde la API: publica ahora o usa TikTok Studio.")
    tok = _valid_token(account, settings)
    size = video.stat().st_size
    init = _http_json("POST", "https://open.tiktokapis.com/v2/post/publish/video/init/",
                      {"post_info": {"title": _caption(title, hashtags)[:2200],
                                     "privacy_level": "SELF_ONLY" if False else "PUBLIC_TO_EVERYONE",
                                     "disable_duet": False, "disable_comment": False,
                                     "disable_stitch": False},
                       "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                       "chunk_size": min(size, 10 * 1024 * 1024),
                                       "total_chunk_count": max(1, -(-size // (10 * 1024 * 1024)))}},
                      {"Authorization": f"Bearer {tok}"}, timeout=120)
    data = init.get("data") or {}
    if init.get("error", {}).get("code") != "ok":
        raise RuntimeError("TikTok: " + str(init.get("error", {}).get("message", "no se pudo iniciar la subida")))
    upload_url = data.get("upload_url")
    chunk_size = min(size, 10 * 1024 * 1024)
    with open(video, "rb") as f:
        for i in range(max(1, -(-size // chunk_size))):
            chunk = f.read(chunk_size)
            req = urllib.request.Request(upload_url, data=chunk, method="PUT", headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes {i * chunk_size}-{i * chunk_size + len(chunk) - 1}/{size}"})
            try:
                urllib.request.urlopen(req, timeout=600).read()
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"TikTok ({e.code}): {e.read().decode('utf-8', 'replace')[:300]}")
    return data.get("publish_id", "tiktok"), "https://www.tiktok.com/tiktokstudio"


_UPLOADERS = {"youtube": _upload_youtube, "facebook": _upload_facebook,
              "instagram": _upload_instagram, "tiktok": _upload_tiktok}


# ------------------------------------------------------------------ publicar una fila
def publish_post(post_id: int) -> dict:
    """Publica (o programa) UNA fila de social_posts. Sin reintentos: si falla,
    queda en failed con el motivo. Devuelve la fila actualizada."""
    post = db.q1("SELECT * FROM social_posts WHERE id=?", (post_id,))
    if not post:
        raise ValueError("Publicación no encontrada.")
    db.run("UPDATE social_posts SET status='publishing', updated_at=? WHERE id=?", (db.now(), post_id))
    try:
        account = db.q1("SELECT * FROM social_accounts WHERE id=?", (post["account_id"],))
        if not account:
            raise RuntimeError("La cuenta ya no existe. Desconectada.")
        clip = db.q1("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
        if not clip or not clip.get("path"):
            raise RuntimeError("El clip ya no existe.")
        video = Path(clip["path"])
        if not video.is_file():
            raise RuntimeError("El archivo del clip ya no está en el disco.")
        settings = db.get_settings()
        publish_at = post["publish_at"] or None
        fn = _UPLOADERS[post["platform"]]
        ext_id, ext_url = fn(account, settings, video, post["title"], post["hashtags"], publish_at)
        if publish_at and PLATFORMS[post["platform"]]["native_scheduling"]:
            status = "scheduled"
        else:
            status = "published"
        db.run("UPDATE social_posts SET status=?, external_id=?, external_url=?, error='', updated_at=? WHERE id=?",
               (status, ext_id, ext_url, db.now(), post_id))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.run("UPDATE social_posts SET status='failed', error=?, updated_at=? WHERE id=?",
               (str(e)[:500], db.now(), post_id))
    return db.q1("SELECT * FROM social_posts WHERE id=?", (post_id,))


def public_post(row: dict) -> dict:
    acc = db.q1("SELECT account_name FROM social_accounts WHERE id=?", (row["account_id"],))
    clip = db.q1("SELECT title FROM clips WHERE id=?", (row["clip_id"],))
    return {"id": row["id"], "clip_id": row["clip_id"], "clip_title": (clip or {}).get("title", ""),
            "platform": row["platform"], "account_id": row["account_id"],
            "account_name": (acc or {}).get("account_name", "Cuenta eliminada"),
            "title": row["title"], "hashtags": row["hashtags"],
            "publish_at": row["publish_at"], "status": row["status"],
            "external_id": row["external_id"], "external_url": row["external_url"],
            "error": row["error"], "created_at": row["created_at"]}


# ------------------------------------------------------------------ programador
_SCHEDULER_STARTED = False


def _scheduler_loop():
    while True:
        try:
            now = int(time.time())
            due = db.q("SELECT id FROM social_posts WHERE status='queued' AND publish_at>0 AND publish_at<=? "
                       "ORDER BY publish_at LIMIT 5", (now,))
            for row in due:
                publish_post(row["id"])
            # Publicar ahora: filas queued con publish_at=0 creadas por /publish
            # ya se publican en la propia petición; aquí solo las vencidas.
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(60)


def start_scheduler():
    global _SCHEDULER_STARTED
    if _SCHEDULER_STARTED:
        return
    _SCHEDULER_STARTED = True
    threading.Thread(target=_scheduler_loop, daemon=True, name="social-scheduler").start()
