"""Detección y organización por creador (streamer / canal).

Orden de confianza: nombre dado por el usuario > metadatos de yt-dlp (canal/uploader) > URL > archivo.
Los clips se agrupan por una clave normalizada ("Ibai", "@ibai" e "ibai" son el mismo creador).
"""
import re
import unicodedata
from urllib.parse import urlparse

from . import db, media

RESERVED = {"video", "videos", "clip", "clips", "categories", "category", "directory", "browse", "search", "watch",
            "live", "channel", "c", "user", "u", "shorts", "playlist", "results", "feed", "embed", "e", "following",
            "downloads", "settings", "p", "t", "www", "m"}
PLATFORMS = {"kick.com": "kick", "twitch.tv": "twitch", "youtube.com": "youtube", "youtu.be": "youtube",
             "tiktok.com": "tiktok", "facebook.com": "facebook", "fb.watch": "facebook", "x.com": "x",
             "twitter.com": "x", "instagram.com": "instagram", "rumble.com": "rumble"}


def slug(name: str) -> str:
    """Clave estable: sin acentos, minúsculas, sin @, espacios ni símbolos."""
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c)).lower().lstrip("@")
    return re.sub(r"[^a-z0-9]+", "", n)


def platform_of(url: str) -> str:
    host = media.host_of(url or "")
    for dom, plat in PLATFORMS.items():
        if host == dom or host.endswith("." + dom):
            return plat
    return ""


def from_url(url: str) -> tuple[str, str]:
    """(nombre, plataforma) deducidos solo de la URL; nombre vacío si no se puede."""
    if not url or not url.startswith(("http://", "https://")):
        return "", ""
    plat = platform_of(url)
    host = media.host_of(url)
    parts = [p for p in urlparse(url).path.split("/") if p]
    name = ""
    if plat == "youtube":
        for i, p in enumerate(parts):
            if p.startswith("@"):
                name = p
                break
            if p in ("c", "user") and i + 1 < len(parts):
                name = parts[i + 1]
                break
    elif plat in ("kick", "twitch"):
        if host.startswith("clips.") and plat == "twitch":
            name = ""
        elif parts and parts[0].lower() not in RESERVED:
            name = parts[0]
    elif plat in ("tiktok", "instagram", "x", "rumble"):
        if parts and (parts[0].startswith("@") or plat in ("instagram", "x")) and parts[0].lower() not in RESERVED:
            name = parts[0]
    name = name.lstrip("@")
    return (name, plat) if slug(name) else ("", plat)


def from_info(info: dict) -> tuple[str, str]:
    """(nombre para mostrar, clave) a partir del JSON de yt-dlp."""
    name = (info.get("channel") or info.get("uploader") or info.get("creator") or info.get("uploader_id") or "").strip()
    handle = (info.get("uploader_id") or "").strip()
    key = slug(handle) if slug(handle) and not handle.startswith("UC") else slug(name)
    return name, key or slug(name)


def clean_display(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lstrip("@"))[:60]


def set_for_job(job_id: int, name: str, key: str = "", platform: str = "", force: bool = False):
    """Asigna creador al proyecto (y por tanto a todos sus clips sin creador propio)."""
    name = clean_display(name)
    key = key or slug(name)
    if not key:
        return
    job = db.q1("SELECT creator_key FROM jobs WHERE id=?", (job_id,))
    if not job or (job["creator_key"] and not force):
        return
    # si ya existe ese creador con otro nombre de pantalla, conserva el existente (evita duplicados visuales)
    known = db.q1("SELECT creator FROM jobs WHERE creator_key=? AND creator!='' ORDER BY id DESC LIMIT 1", (key,))
    db.run("UPDATE jobs SET creator=?, creator_key=?, platform=CASE WHEN ?!='' THEN ? ELSE platform END WHERE id=?",
           ((known or {}).get("creator") or name, key, platform, platform, job_id))


def detect_for_new_job(job_id: int, url: str, given: str = ""):
    """Al crear un proyecto: nombre dado > URL."""
    name, plat = from_url(url)
    if given and slug(given):
        set_for_job(job_id, given, platform=plat, force=True)
    elif name:
        set_for_job(job_id, name, platform=plat)
    elif plat:
        db.run("UPDATE jobs SET platform=? WHERE id=?", (plat, job_id))


def refine_from_info(job_id: int, info: dict):
    """Tras descargar: los metadatos del sitio son más fiables que la URL (nombre real del canal)."""
    job = db.q1("SELECT creator_key, options FROM jobs WHERE id=?", (job_id,))
    if not job:
        return
    import json
    if (json.loads(job.get("options") or "{}") or {}).get("creator_given"):
        return                                      # el usuario lo escribió: no se toca
    name, key = from_info(info)
    if key:
        old_key = job["creator_key"]
        if old_key and old_key != key:
            # la URL daba otra clave (p. ej. un id numérico): unifica en la del sitio
            db.run("UPDATE jobs SET creator='', creator_key='' WHERE id=?", (job_id,))
        set_for_job(job_id, name, key, force=not old_key or old_key != key)


def backfill():
    """Una sola vez: asigna creador a proyectos creados antes de esta función."""
    if db.kv_get("creators_backfilled"):
        return
    for j in db.q("SELECT id, source, title FROM jobs WHERE creator_key=''"):
        detect_for_new_job(j["id"], j["source"] or "")
    db.kv_set("creators_backfilled", True)


# ------------------------------------------------------------------ consulta
EFFECTIVE = ("CASE WHEN c.creator_key='-' THEN '' ELSE COALESCE(NULLIF(c.creator_key,''), j.creator_key, '') END",
             "CASE WHEN c.creator_key='-' THEN '' ELSE COALESCE(NULLIF(c.creator,''), j.creator, '') END")


def list_creators() -> list[dict]:
    rows = db.q(f"""SELECT {EFFECTIVE[0]} AS ckey, {EFFECTIVE[1]} AS cname, c.id AS cid, c.score, c.created_at,
                   c.job_id, j.platform AS plat, (c.thumb!='') AS has_thumb
                   FROM clips c LEFT JOIN jobs j ON j.id=c.job_id WHERE c.path!='' ORDER BY c.id DESC""")
    out: dict[str, dict] = {}
    for r in rows:
        k = r["ckey"] or ""
        d = out.setdefault(k, {"key": k, "name": r["cname"] or "Sin creador", "clips": 0, "score_sum": 0, "jobs": set(),
                               "platforms": set(), "cover": 0, "last": 0})
        d["clips"] += 1
        d["score_sum"] += r["score"] or 0
        d["jobs"].add(r["job_id"])
        if r["plat"]:
            d["platforms"].add(r["plat"])
        if r["has_thumb"] and not d["cover"]:
            d["cover"] = r["cid"]
        d["last"] = max(d["last"], r["created_at"] or 0)
        if r["cname"] and d["name"] == "Sin creador":
            d["name"] = r["cname"]
    res = []
    for d in out.values():
        res.append({"key": d["key"], "name": d["name"], "clips": d["clips"], "avg_score": round(d["score_sum"] / d["clips"]),
                    "projects": len(d["jobs"]), "platforms": sorted(d["platforms"]), "cover": d["cover"], "last": d["last"]})
    res.sort(key=lambda x: (x["key"] == "", -x["last"]))
    return res


def rename(key: str, new_name: str) -> str:
    """Renombra (o fusiona con otro existente) a un creador. Devuelve la nueva clave."""
    new_name = clean_display(new_name)
    nk = slug(new_name)
    if not nk:
        raise ValueError("Escribe un nombre válido.")
    existing = db.q1("SELECT creator FROM jobs WHERE creator_key=? AND creator!='' LIMIT 1", (nk,)) if nk != key else None
    shown = (existing or {}).get("creator") or new_name
    db.run("UPDATE jobs SET creator=?, creator_key=? WHERE creator_key=?", (shown, nk, key))
    db.run("UPDATE clips SET creator=?, creator_key=? WHERE creator_key=? AND creator_key!=''", (shown, nk, key))
    return nk


def move_clips(ids: list[int], name: str):
    """Mueve clips concretos a un creador (o a 'Sin creador' si name vacío)."""
    name = clean_display(name)
    key = slug(name)
    if name and not key:
        raise ValueError("Nombre de creador no válido.")
    if key:
        known = db.q1("SELECT creator FROM jobs WHERE creator_key=? AND creator!='' LIMIT 1", (key,)) or \
            db.q1("SELECT creator FROM clips WHERE creator_key=? AND creator!='' LIMIT 1", (key,))
        name = (known or {}).get("creator") or name
    for cid in ids:
        # clave especial "-" = forzar 'sin creador' aunque el proyecto tenga uno
        db.run("UPDATE clips SET creator=?, creator_key=? WHERE id=?", (name if key else "", key or "-", int(cid)))


def detect_live_async(job_id: int, url: str, settings: dict):
    """Directos de YouTube/otros sin el canal en la URL: pregunta a yt-dlp quién transmite (en segundo plano)."""
    import subprocess
    import threading

    def work():
        try:
            j = db.q1("SELECT creator_key FROM jobs WHERE id=?", (job_id,))
            if not j or j["creator_key"]:
                return
            from . import config
            cmd = media.ytdlp_base(settings) + ["--skip-download", "--print", "%(channel)s|%(uploader_id)s|%(uploader)s", url]
            out = subprocess.run(cmd, capture_output=True, timeout=60, **config.NOWIN).stdout.decode("utf-8", "replace")
            line = next((l for l in reversed(out.splitlines()) if l.count("|") == 2), "")
            if line:
                ch, uid, up = [x.strip() for x in line.split("|")]
                clean = lambda v: "" if v in ("NA", "None") else v  # noqa: E731
                refine_from_info(job_id, {"channel": clean(ch), "uploader_id": clean(uid), "uploader": clean(up)})
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=work, daemon=True, name=f"creator-{job_id}").start()
