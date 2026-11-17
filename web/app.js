'use strict';
/* AdvvxClips · interfaz (JS puro, sin compilar) */
const $ = (s, el = document) => el.querySelector(s);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'checked' || k === 'value' || k === 'disabled') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  if (props && props.value !== undefined && el.tagName === 'SELECT') el.value = props.value;
  return el;
}
const state = { settings: null, themes: [], timers: [], me: null };

function toast(msg, kind = '') {
  const t = h('div', { class: 'toast ' + kind }, msg);
  $('#toasts').append(t);
  setTimeout(() => t.remove(), kind === 'err' ? 7000 : 3500);
}
async function api(path, opts = {}) {
  const o = { method: opts.method || 'GET', headers: { 'X-TBX': '1' } };
  if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers['Content-Type'] = 'application/json'; }
  let r;
  try { r = await fetch(path, o); }
  catch (e) { throw new Error('No hay conexión con la app. ¿Sigue abierta la ventana negra de iniciar.bat?'); }
  const d = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  if (r.status === 401) { state.me = null; showAuth('Tu sesión venció. Vuelve a entrar.'); throw new Error('Sesión vencida.'); }
  if (!r.ok) throw new Error((d && d.detail) || 'Error ' + r.status);
  return d;
}
const copy = async (txt, msg = 'Copiado') => {
  try { await navigator.clipboard.writeText(txt); } catch (e) {
    const ta = h('textarea', { value: txt }); document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
  toast(msg, 'ok');
};
const fmtDur = (s) => { s = Math.round(s); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const fmtDate = (ts) => new Date(ts * 1000).toLocaleDateString('es', { day: '2-digit', month: 'short', year: 'numeric' });
const STATUS = { queued: 'En cola', downloading: 'Descargando', transcribing: 'Transcribiendo', analyzing: 'Analizando', rendering: 'Renderizando',
  done: 'Listo', error: 'Error', cancelled: 'Cancelado', waiting: 'Esperando directo', recording: '● EN VIVO', processing: 'Finalizando', stopped: 'Finalizado' };
const scoreClass = (n) => (n >= 80 ? '' : n >= 65 ? 'mid' : n >= 50 ? 'low' : 'bad');
const clearTimers = () => { state.timers.forEach(clearInterval); state.timers = []; };
const every = (fn, ms) => { state.timers.push(setInterval(() => fn().catch(() => {}), ms)); };

/* ------------------------------------------------------------------ cuentas */
function showAuth(note = '') {
  clearTimers();
  const user = h('input', { type: 'text', placeholder: 'usuario', autocomplete: 'username' });
  const pass = h('input', { type: 'password', placeholder: 'contraseña', autocomplete: 'current-password' });
  const name = h('input', { type: 'text', placeholder: 'tu nombre (para tu perfil)' });
  const msg = h('p', { class: 'sub' }, note);
  const title = h('h1', {}, 'AdvvxClips');
  const sub = h('p', { class: 'sub' }, 'Entra con tu cuenta. Si no tienes, crea una: el administrador te aprueba y listo.');
  let mode = 'login';
  const go = async () => {
    try {
      const body = mode === 'login' ? { username: user.value.trim(), password: pass.value }
        : { username: user.value.trim(), password: pass.value, name: name.value.trim() };
      const r = await api(mode === 'login' ? '/api/auth/login' : '/api/auth/register', { method: 'POST', body });
      if (r.approved === false) { msg.textContent = r.detail; return; }
      state.me = r.user;
      toast('Bienvenido, ' + r.user.name, 'ok');
      boot();
    } catch (e) { msg.textContent = e.message; }
  };
  const tabs = h('div', { class: 'tabs' },
    h('button', { class: 'on', onclick: (e) => { mode = 'login'; name.style.display = 'none'; e.target.classList.add('on'); e.target.nextSibling.classList.remove('on'); } }, 'Entrar'),
    h('button', { onclick: (e) => { mode = 'register'; name.style.display = ''; e.target.classList.add('on'); e.target.previousSibling.classList.remove('on'); } }, 'Crear cuenta'));
  name.style.display = 'none';
  $('#app').replaceChildren(h('div', { class: 'center' }, h('div', { class: 'card', style: { maxWidth: '420px', width: '100%' } },
    title, sub, tabs, user, pass, name, msg,
    h('button', { class: 'btn', style: { marginTop: '12px', width: '100%' }, onclick: go }, 'Entrar a AdvvxClips'))));
}
async function pageUsuarios() {
  const me = state.me || {};
  if (!me.is_admin) return h('div', { class: 'note bad' }, 'Solo el administrador ve esta página.');
  const wrap = h('div', {});
  const load = async () => {
    const users = await api('/api/admin/users');
    wrap.replaceChildren(...users.map((u) => h('div', { class: 'job' },
      h('div', { style: { flex: 1 } },
        h('div', { class: 't' }, u.name + '  ', h('span', { class: 'm' }, '@' + u.username)),
        h('div', { class: 'm' }, u.is_admin ? 'Administrador' : u.approved ? 'Aprobado' : '⏳ Pendiente de aprobación')),
      h('div', { class: 'row' },
        !u.approved && h('button', { class: 'btn sm', onclick: async () => { await api(`/api/admin/users/${u.id}/approve`, { method: 'POST' }); toast(u.name + ' aprobado', 'ok'); load(); } }, 'Aprobar'),
        u.approved && !u.is_admin && h('button', { class: 'btn g sm', onclick: async () => { if (confirm('¿Quitarle el acceso a ' + u.name + '?')) { await api(`/api/admin/users/${u.id}/revoke`, { method: 'POST' }); load(); } } }, 'Quitar acceso'),
        !u.is_admin && h('button', { class: 'btn d sm', onclick: async () => { if (confirm('¿Eliminar la cuenta de ' + u.name + '? Sus proyectos se quedan, pero ya no podrá entrar.')) { await api(`/api/admin/users/${u.id}`, { method: 'DELETE' }); load(); } } }, 'Eliminar')))));
  };
  await load();
  return h('div', {}, h('h1', {}, 'Usuarios'), h('p', { class: 'sub' }, 'Tú apruebas quién entra. Cada persona ve solo sus proyectos y sus clips.'), wrap);
}

/* ------------------------------------------------------------------ estructura */
const NAV = [['crear', '➕', 'Crear clips'], ['proyectos', '📂', 'Proyectos'], ['biblioteca', '🎬', 'Biblioteca'], ['editor', '✂️', 'Editor'], ['cuentas', '🔗', 'Cuentas'], ['ajustes', '⚙️', 'Ajustes']];
function shell() {
  const nav = [...NAV];
  if (state.me && state.me.is_admin) nav.push(['usuarios', '👥', 'Usuarios']);
  $('#app').replaceChildren(h('div', { class: 'shell' },
    h('aside', { class: 'side' }, h('div', { class: 'logo' }, 'Advvx', h('b', {}, 'Clips')),
      h('nav', { class: 'nav', id: 'nav' }, nav.map(([k, ic, t]) => h('a', { href: '#/' + k, 'data-k': k }, ic + '  ' + t))),
      h('div', { class: 'foot' }, h('span', { class: 'local-dot' }), (state.me ? state.me.name + ' · ' : '') + (state.me && state.me.is_admin ? 'Administrador' : 'Amigo'), h('br'),
        h('a', { href: '#', onclick: async (e) => { e.preventDefault(); await api('/api/auth/logout', { method: 'POST' }); state.me = null; showAuth(); } }, 'Cerrar sesión'))),
    h('main', { class: 'main', id: 'page' })));
}
function parseHash() {
  const [path, qs] = location.hash.replace(/^#\/?/, '').split('?');
  const [page, sub] = (path || 'crear').split('/');
  return { page, q: { ...Object.fromEntries(new URLSearchParams(qs || '')), ...(sub ? { sub } : {}) } };
}
async function route() {
  clearTimers();
  if (window.__edCleanup) { window.__edCleanup(); window.__edCleanup = null; }
  const { page, q } = parseHash();
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.k === page));
  const fn = PAGES[page] || PAGES.crear;
  const root = $('#page');
  root.replaceChildren(h('p', { class: 'sub' }, 'Cargando…'));
  try { root.replaceChildren(await fn(q)); }
  catch (e) { root.replaceChildren(h('div', { class: 'note bad' }, e.message)); }
}
async function boot() {
  try {
    const me = await api('/api/auth/me');
    if (!me.user) { showAuth(); return; }
    state.me = me.user;
    [state.settings, state.themes] = await Promise.all([api('/api/settings'), api('/api/themes')]);
    shell();
    route();
  } catch (e) { $('#app').replaceChildren(h('div', { class: 'center' }, h('div', { class: 'note bad' }, e.message))); }
}
window.addEventListener('hashchange', () => { if (state.settings) route(); });

/* ------------------------------------------------------------------ componentes */
function jobCard(j, refresh) {
  const log = h('div', { class: 'log', style: { display: 'none' } });
  const isLive = j.kind === 'live';
  const pct = Math.round((j.progress || 0) * 100);
  return h('div', { class: 'job' },
    h('div', { style: { flex: 1, minWidth: 0 } },
      h('div', { class: 't' }, (isLive ? '📡 ' : j.kind === 'channel' ? '📺 ' : '🎞 ') + (j.title || j.source || 'Proyecto ' + j.id)),
      h('div', { class: 'row', style: { marginTop: '6px' } },
        h('span', { class: 'pill ' + j.status }, STATUS[j.status] || j.status),
        h('span', { class: 'm' }, `${j.clips} clips`), j.language && h('span', { class: 'm' }, 'idioma: ' + j.language)),
      h('div', { class: 'm' }, j.message),
      (j.active && !isLive) && h('div', { class: 'bar' }, h('i', { style: { width: pct + '%' } })),
      log),
    h('div', { class: 'row', style: { justifyContent: 'flex-end' } },
      j.clips > 0 && h('a', { class: 'btn sm', href: '#/biblioteca?job=' + j.id }, 'Ver clips'),
      h('button', { class: 'btn g sm', onclick: async () => {
        if (log.style.display === 'none') {
          const d = await api('/api/jobs/' + j.id);
          log.textContent = d.events.map((e) => new Date(e.ts * 1000).toLocaleTimeString() + '  ' + e.text).join('\n') || 'Sin eventos todavía.';
          log.style.display = 'block';
        } else log.style.display = 'none';
      } }, 'Registro'),
      j.active ? h('button', { class: 'btn d sm', onclick: async () => { await api(`/api/jobs/${j.id}/stop`, { method: 'POST' }); toast(isLive ? 'Deteniendo… se procesará lo grabado' : 'Cancelando…'); refresh(); } }, isLive ? 'Detener directo' : 'Cancelar')
        : h('button', { class: 'btn g sm', onclick: async () => { if (confirm('¿Borrar este proyecto y todos sus clips?')) { await api('/api/jobs/' + j.id, { method: 'DELETE' }); refresh(); } } }, 'Borrar')));
}
function jobList(container, onlyActive) {
  const load = async () => {
    const jobs = (await api('/api/jobs')).filter((j) => !onlyActive || j.active || Date.now() / 1000 - j.updated_at < 600).slice(0, onlyActive ? 6 : 100);
    container.replaceChildren(...(jobs.length ? jobs.map((j) => jobCard(j, load)) : [h('div', { class: 'empty' }, 'Aún no hay proyectos. Crea el primero arriba ☝️')]));
  };
  load();
  every(load, 2500);
}
const opt = (v, t) => h('option', { value: v }, t);
function themeSelect(value) { return h('select', { value }, state.themes.map((t) => opt(t.id, t.label))); }

/* ------------------------------------------------------------------ Crear */
async function pageCrear() {
  const s = state.settings, st = await api('/api/stats');
  let mode = 'video', file = null;
  const ctOpts = [['entrevista', '🎙 Entrevista'], ['podcast', '🎧 Podcast'], ['resumen', '📝 Resumen'], ['pelicula', '🎬 Película'], ['gaming', '🎮 Gaming']];
  // «De qué trata el video»: el valor por defecto es "general" (sin
  // preferencia) y NO es una opción del desplegable; si el usuario no
  // toca el desplegable, el proyecto usa el valor guardado (general).
  let ctVal = ctOpts.some(([v]) => v === s.content_type) ? s.content_type : 'general';
  const ctSel = h('select', { value: ctVal === 'general' ? '' : ctVal, onchange: (e) => { ctVal = e.target.value || 'general'; } },
    h('option', { value: '', disabled: true }, '— Elige el tipo de video —'),
    ctOpts.map(([v, t]) => opt(v, t)));
  const wmOpVal = h('span', {}, (s.watermark_opacity ?? 80) + '%');
  const url = h('input', { type: 'url', placeholder: 'https://www.youtube.com/watch?v=…   ·   https://www.youtube.com/@canal', autocomplete: 'off' });
  const drop = h('input', { type: 'file', accept: 'video/*,audio/*,.mkv,.ts', onchange: (e) => { file = e.target.files[0]; } });
  const f = {
    min: h('input', { type: 'number', value: s.clip_min_s, min: 8, max: 180 }), max: h('input', { type: 'number', value: s.clip_max_s, min: 10, max: 300 }),
    n: h('input', { type: 'number', value: s.max_clips, min: 1, max: 40 }), score: h('input', { type: 'number', value: s.min_score, min: 0, max: 100 }),
    lscore: h('input', { type: 'number', value: s.live_min_score, min: 0, max: 100 }), wait: h('input', { type: 'number', value: s.live_wait_minutes, min: 0, max: 720 }),
    maxVideos: h('input', { type: 'number', value: 5, min: 1, max: 20 }),
    layout: h('select', { value: s.layout }, opt('blur', 'Vertical · fondo desenfocado'), opt('fill', 'Vertical · recorte central'), opt('original', 'Original (sin recortar)'), opt('gaming', '🎮 Gaming · cámara en esquina'), opt('duo', '🎬 Dúo / Reacción · personas arriba, contenido abajo'), opt('speaker', '🎙 Entrevista · cámara al que habla'), opt('camara', '📷 Cámara completa · toda la pantalla')),
    cam: h('select', { value: s.cam_corner || 'tl' }, opt('tl', 'Arriba a la izquierda'), opt('tr', 'Arriba a la derecha'), opt('bl', 'Abajo a la izquierda'), opt('br', 'Abajo a la derecha')),
    quality: h('select', { value: s.quality_profile || 'alta' }, opt('rapida', '⚡ Rápida · 30 fps · la más veloz'), opt('equilibrada', 'Equilibrada · 30 fps · buena calidad'), opt('alta', '⭐ Alta · 60 fps · muy buena calidad')),
    theme: themeSelect(s.caption_theme), lang: h('select', { value: s.language }, opt('auto', 'Detectar automático'), opt('es', 'Español'), opt('en', 'English'), opt('pt', 'Português'), opt('fr', 'Français'), opt('it', 'Italiano'), opt('de', 'Deutsch')),
    contentType: ctSel,
    wmMode: h('select', { value: s.watermark_mode || 'none' }, opt('none', 'Sin marca de agua'), opt('logo', 'Imagen (logo)'), opt('text', 'Texto')),
    wmText: h('input', { type: 'text', value: s.watermark_text || 'ClipsViralesRD_', placeholder: 'Texto de la marca de agua' }),
    wmOpacity: h('input', { type: 'range', min: 0, max: 100, value: s.watermark_opacity ?? 80, onchange: (e) => { wmOpVal.textContent = e.target.value + '%'; } }),
    caps: h('input', { type: 'checkbox', checked: s.captions }),
    tiktok: h('input', { type: 'checkbox', checked: s.tiktok_mode !== false }),
    creator: h('input', { type: 'text', placeholder: 'Se detecta solo (canal o streamer). Escríbelo si quieres corregirlo.' }),
  };
  const liveOnly = h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Puntaje mínimo para crear clip solo'), f.lscore, h('div', { class: 'hint' }, 'Más alto = menos clips pero mejores.')),
    h('div', {}, h('label', {}, 'Esperar a que inicie el directo (minutos)'), f.wait));
  const vodOnly = h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Máximo de clips'), f.n), h('div', {}, h('label', {}, 'Puntaje mínimo'), f.score));
  const channelOnly = h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Videos del canal a procesar'), f.maxVideos, h('div', { class: 'hint' }, 'Los más recientes, uno a la vez. Cada video crea sus propios clips.')));
  const modeBtns = [['video', '🔗 Video por URL'], ['channel', '📺 Canal de YouTube'], ['file', '📁 Subir archivo'], ['live', '📡 Directo (Live)']].map(([m, t]) => h('button', { 'data-m': m, onclick: () => setMode(m) }, t));
  const inputBox = h('div', {});
  const btn = h('button', { class: 'btn', style: { marginTop: '16px', padding: '12px 26px' } }, 'Crear clips');
  const hintEl = h('div', { class: 'hint' });
  const progress = h('div', { class: 'bar', style: { display: 'none' } }, h('i', {}));
  function setMode(m) {
    mode = m;
    modeBtns.forEach((b) => b.classList.toggle('on', b.dataset.m === m));
    inputBox.replaceChildren(m === 'file' ? h('div', {}, h('label', {}, 'Archivo de video o audio'), drop, h('div', { class: 'hint' }, 'Se procesa en tu PC; no se sube a internet.')) :
      h('div', {}, h('label', {}, m === 'live' ? 'URL del directo o del canal' : m === 'channel' ? 'URL del canal' : 'URL del video'), url));
    hintEl.textContent = m === 'live' ? 'YouTube (…/watch?v= o …/@canal/live), Kick (kick.com/canal), Twitch o un enlace .m3u8. Los clips se crean solos mientras transmite.'
      : m === 'channel' ? 'Pega el link del canal (p. ej. youtube.com/@canal). Se listan sus videos más recientes y se crean clips de cada uno, uno a la vez.'
      : m === 'video' ? 'YouTube, Kick (VOD), Twitch, TikTok, Facebook, X… (lo que soporte yt-dlp).' : '';
    liveOnly.style.display = m === 'live' ? '' : 'none';
    vodOnly.style.display = m === 'live' ? 'none' : '';
    channelOnly.style.display = m === 'channel' ? '' : 'none';
    btn.textContent = m === 'live' ? '📡 Empezar a vigilar el directo' : m === 'channel' ? '📺 Crear clips del canal' : 'Crear clips';
  }
  btn.onclick = async () => {
    const settings = { clip_min_s: +f.min.value, clip_max_s: +f.max.value, max_clips: +f.n.value, min_score: +f.score.value, live_min_score: +f.lscore.value,
      live_wait_minutes: +f.wait.value, layout: f.layout.value, caption_theme: f.theme.value, captions: f.caps.checked, tiktok_mode: f.tiktok.checked, language: f.lang.value, quality_profile: f.quality.value, cam_corner: f.cam.value,
      content_type: ctVal, watermark_mode: f.wmMode.value, watermark_text: f.wmText.value, watermark_opacity: +f.wmOpacity.value };
    btn.disabled = true;
    try {
      if (settings.clip_min_s >= settings.clip_max_s) throw new Error('La duración mínima debe ser menor que la máxima.');
      if (mode === 'file') {
        if (!file) throw new Error('Elige un archivo.');
        await new Promise((res, rej) => {
          const x = new XMLHttpRequest();
          x.open('POST', '/api/upload?name=' + encodeURIComponent(file.name) + '&settings=' + encodeURIComponent(JSON.stringify(settings)) + '&creator=' + encodeURIComponent(f.creator.value));
          x.setRequestHeader('X-TBX', '1');
          progress.style.display = 'block';
          x.upload.onprogress = (e) => { if (e.lengthComputable) { progress.firstChild.style.width = (e.loaded / e.total * 100) + '%'; btn.textContent = 'Subiendo… ' + Math.round(e.loaded / e.total * 100) + '%'; } };
          x.onload = () => (x.status === 200 ? res() : rej(new Error((JSON.parse(x.responseText || '{}').detail) || 'Error al subir')));
          x.onerror = () => rej(new Error('Falló la subida.'));
          x.send(file);
        });
      } else {
        const body = { url: url.value, settings, creator: f.creator.value };
        if (mode === 'channel') body.max_videos = +f.maxVideos.value;
        await api(mode === 'live' ? '/api/jobs/live' : mode === 'channel' ? '/api/jobs/channel' : '/api/jobs/video', { method: 'POST', body });
      }
      toast(mode === 'live' ? 'Vigilando el directo…' : mode === 'channel' ? 'Procesando canal…' : 'Proyecto creado', 'ok');
      location.hash = '#/proyectos';
    } catch (e) { toast(e.message, 'err'); btn.disabled = false; setMode(mode); }
  };
  const active = h('div', {});
  jobList(active, true);
  const modes = h('div', { class: 'tabs' }, modeBtns);
  setMode('video');
  return h('div', {},
    h('div', { class: 'studio-top' }, h('span', {}, 'ESTUDIO / CREAR CLIPS'), h('span', { class: 'studio-profile' }, 'Hola, ' + (state.me ? state.me.name : ''), h('b', {}, (state.me ? state.me.name : 'A').slice(0, 1).toUpperCase()))),
    h('div', { class: 'studio-intro' }, h('div', {}, h('div', { class: 'studio-tag' }, 'DE VIDEOS A MOMENTOS'), h('h1', {}, 'Tu próximo gran clip empieza aquí.'), h('p', { class: 'sub' }, 'Encuentra los mejores momentos. Dale tu estilo. Comparte.')), h('a', { class: 'btn g sm', href: '#/biblioteca' }, 'Abrir biblioteca →')),
    h('div', { class: 'stats' }, [[st.clips, 'Clips creados'], [st.avg_score || '–', 'Puntaje promedio'], [st.active, 'Proyectos en curso'], [st.live_active, 'Directos vigilados']].map(([n, t]) => h('div', { class: 'stat' }, h('b', {}, n), h('span', {}, t)))),
    h('div', { class: 'studio-work' }, h('div', { class: 'card studio-create' }, h('h2', {}, 'Convierte un video en clips'), h('p', { class: 'sub' }, 'Empieza con un enlace, un canal, un archivo o una transmisión en vivo.'), modes, inputBox, hintEl,
      h('details', { style: { marginTop: '14px' } }, h('summary', { style: { cursor: 'pointer', fontWeight: 700 } }, 'Opciones del proyecto'),
        h('div', { class: 'fld' }, h('label', {}, 'Creador (opcional)'), f.creator, h('div', { class: 'hint' }, 'Sus clips se guardarán en una carpeta con su nombre en la Biblioteca.')),
        h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Duración mínima (s)'), f.min), h('div', {}, h('label', {}, 'Duración máxima (s)'), f.max)),
        vodOnly, liveOnly, channelOnly,
        h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Formato'), f.layout), h('div', {}, h('label', {}, 'De qué trata el video'), f.contentType), h('div', {}, h('label', {}, 'Estilo de subtítulos'), f.theme), h('div', {}, h('label', {}, 'Idioma del audio'), f.lang)),
        h('div', { class: 'fld' }, h('label', {}, 'Marca de agua'),
          h('div', { class: 'grid2' }, h('div', {}, f.wmMode), h('div', {}, f.wmText)),
          h('div', { style: { display: 'flex', gap: '10px', alignItems: 'center', marginTop: '6px' } }, h('label', { style: { margin: 0 } }, 'Opacidad'), f.wmOpacity, wmOpVal),
          h('div', { class: 'hint' }, 'Por defecto: sin marca de agua. La imagen usa el logo o tu imagen personalizada de Ajustes.')),
        h('div', { class: 'fld' }, h('label', {}, 'Esquina de la cámara (solo formato gaming)'), f.cam, h('div', { class: 'hint' }, 'Dónde está la cámara dentro del video original. En el clip se recorta su caja exacta (sin gameplay dentro) y sigue la cara donde se mueva.')),
        h('div', { class: 'fld' }, h('label', {}, 'Calidad de los clips'), f.quality, h('div', { class: 'hint' }, 'Alta = 60 fps y muy buena calidad. Rápida usa el modelo tiny y es la más veloz para videos largos.')),
        h('label', { class: 'sw' }, f.caps, 'Subtítulos quemados en el video'),
        h('label', { class: 'sw' }, f.tiktok, '📱 Modo TikTok (clips de 20–40 s, gancho primero)'), h('div', { class: 'hint' }, 'El clip arranca en el gancho y dura entre 20 y 40 segundos, listo para TikTok.')),
      progress, btn), h('aside', { class: 'studio-tip' }, h('div', { class: 'studio-art', 'aria-hidden': 'true' }, h('i'), h('i'), h('i')), h('div', { class: 'studio-tag' }, 'HECHO PARA VERTICAL'), h('h2', {}, 'Un momento.', h('br'), 'Muchas posibilidades.'), h('p', {}, 'Ajusta el encuadre, añade subtítulos y dale tu toque en el editor.'), h('a', { href: '#/editor' }, 'Explora el editor →'))),
    h('h2', {}, 'En curso'), h('div', { class: 'card' }, active));
}

/* ------------------------------------------------------------------ Proyectos */
async function pageProyectos() {
  const box = h('div', { class: 'card' });
  jobList(box, false);
  return h('div', {}, h('h1', {}, 'Proyectos'), h('p', { class: 'sub' }, 'Descargas, directos y su progreso en tiempo real.'), box);
}

/* ------------------------------------------------------------------ Biblioteca (por creadores) */
async function pageBiblioteca(q) {
  const jobs = await api('/api/jobs');
  let creators = await api('/api/creators');
  let cur = q.creator !== undefined ? q.creator : '*';          // '*' todos · 'none' sin creador · clave del creador
  if (cur !== '*' && cur !== 'none' && !creators.some((c) => c.key === cur)) cur = '*';
  const picked = new Set(); let selecting = false, clipsNow = [];
  const f = { q: h('input', { type: 'text', placeholder: 'Buscar por título o palabra dicha…' }), score: h('select', { value: '0' }, opt('0', 'Todos los puntajes'), opt('50', '50+'), opt('65', '65+'), opt('80', '80+ (los mejores)')),
    job: h('select', { value: q.job || '' }, opt('', 'Todos los proyectos'), jobs.filter((j) => j.clips || j.kind === 'live').map((j) => opt(j.id, (j.title || j.source || 'Proyecto').slice(0, 50)))) };
  const lives = jobs.filter((j) => j.kind === 'live');
  const livesSection = h('div', { class: 'card', style: { marginBottom: '16px' } }, h('h3', { style: { marginTop: 0 } }, '📡 Tus Lives'),
    ...(lives.length ? lives.map((j) => h('div', { class: 'row', style: { justifyContent: 'space-between', cursor: 'pointer', padding: '6px 0' }, onclick: () => { f.job.value = String(j.id); load(true); } },
      h('span', {}, (j.title || j.source || 'Directo ' + j.id)), h('span', { class: 'm' }, `${j.clips} clip${j.clips === 1 ? '' : 's'} · ${STATUS[j.status] || j.status} →`)))
      : [h('div', { class: 'm' }, 'Aún no hay directos. Crea uno en Crear clips → Directo (Live).')]));
  const side = h('div', { class: 'crs' }), head = h('div', { class: 'crhead' }), grid = h('div', { class: 'clips' }), bar = h('div');
  const ckey = (c) => c.key || 'none';
  const crName = () => (cur === '*' ? 'Todos los clips' : (creators.find((c) => ckey(c) === cur) || {}).name || 'Sin creador');
  const total = () => creators.reduce((a, c) => a + c.clips, 0);
  function drawSide() {
    const item = (key, name, count, cover, extra) => h('div', { class: 'cr' + (cur === key ? ' on' : ''), onclick: () => { cur = key; picked.clear(); history.replaceState(null, '', '#/biblioteca?creator=' + key); drawAll(); load(true); } },
      h('div', { class: 'av', style: cover ? { backgroundImage: `url(/api/clips/${cover}/thumb)` } : {} }, cover ? '' : (name[0] || '?').toUpperCase()),
      h('div', { style: { minWidth: 0 } }, h('div', { class: 'nm' }, name), h('div', { class: 'ct' }, `${count} clip${count === 1 ? '' : 's'}` + (extra ? ' · ' + extra : ''))));
    side.replaceChildren(h('h3', {}, 'Creadores'), item('*', 'Todos', total(), null), ...creators.map((c) => item(ckey(c), c.name, c.clips, c.cover, c.platforms.join(', '))));
  }
  function drawHead() {
    const c = creators.find((x) => ckey(x) === cur);
    head.replaceChildren(h('div', {}, h('h1', { style: { marginBottom: 0 } }, crName()), h('div', { class: 'sub', style: { margin: 0 } },
      cur === '*' ? `${total()} clips de ${creators.length} creador${creators.length === 1 ? '' : 'es'}` : c ? `${c.clips} clips · puntaje promedio ${c.avg_score} · ${c.projects} proyecto${c.projects === 1 ? '' : 's'}` : '')),
      h('div', { class: 'row' }, c && c.key && h('button', { class: 'btn g sm', onclick: renameCreator }, '✏ Renombrar creador'),
        h('button', { class: 'btn ' + (selecting ? '' : 'g') + ' sm', onclick: () => { selecting = !selecting; if (!selecting) picked.clear(); drawHead(); drawBar(); paint(); } }, selecting ? '✓ Seleccionando…' : '☑ Seleccionar clips')));
  }
  async function renameCreator() {
    const c = creators.find((x) => ckey(x) === cur); if (!c) return;
    const name = prompt('Nuevo nombre del creador (si ya existe otro con ese nombre, se fusionan):', c.name); if (!name || !name.trim()) return;
    try { const r = await api('/api/creators/' + c.key, { method: 'PUT', body: { name } }); toast('Creador actualizado', 'ok'); history.replaceState(null, '', '#/biblioteca?creator=' + r.key); route(); } catch (e) { toast(e.message, 'err'); }
  }
  function drawBar() {
    if (!selecting) { bar.replaceChildren(); return; }
    const n = picked.size;
    bar.replaceChildren(h('div', { class: 'selbar' }, h('b', {}, n ? `${n} seleccionado${n === 1 ? '' : 's'}` : 'Toca los clips para seleccionarlos'),
      h('button', { class: 'btn g sm', onclick: () => { clipsNow.forEach((c) => picked.add(c.id)); drawBar(); paint(); } }, 'Todos los visibles'),
      h('button', { class: 'btn sm', disabled: !n, onclick: async (e) => { e.target.disabled = true; try { await openInEditor([...picked]); } catch (err) { toast(err.message, 'err'); e.target.disabled = false; } } }, '✂ Abrir en el editor'),
      h('button', { class: 'btn g sm', disabled: !n, onclick: moveSelected }, '👤 Mover a creador…'),
      h('button', { class: 'btn d sm', disabled: !n, onclick: deleteSelected }, '🗑 Eliminar'),
      h('button', { class: 'btn g sm', onclick: () => { selecting = false; picked.clear(); drawHead(); drawBar(); paint(); } }, 'Cancelar')));
  }
  function moveSelected() {
    const inp = h('input', { type: 'text', list: 'crlist', placeholder: 'Nombre del creador (vacío = sin creador)' });
    const modal = h('div', { class: 'modal', onclick: (e) => { if (e.target === modal) modal.remove(); } }, h('div', { class: 'card', style: { maxWidth: '460px', width: '100%' } },
      h('h2', {}, `Mover ${picked.size} clip(s) a un creador`), inp, h('datalist', { id: 'crlist' }, creators.filter((c) => c.key).map((c) => h('option', { value: c.name }))),
      h('div', { class: 'row', style: { marginTop: '14px' } }, h('button', { class: 'btn', onclick: async () => {
        try { await api('/api/clips/move-creator', { method: 'POST', body: { ids: [...picked], creator: inp.value } }); modal.remove(); picked.clear(); toast('Clips movidos', 'ok'); creators = await api('/api/creators'); drawAll(); load(true); } catch (e) { toast(e.message, 'err'); }
      } }, 'Mover'), h('button', { class: 'btn g', onclick: () => modal.remove() }, 'Cancelar'))));
    document.body.append(modal); inp.focus();
  }
  async function deleteSelected() {
    if (!confirm(`¿Eliminar ${picked.size} clip(s)? No se puede deshacer.`)) return;
    for (const id of [...picked]) { try { await api('/api/clips/' + id, { method: 'DELETE' }); } catch (e) { /* ya no existe */ } }
    picked.clear(); toast('Clips eliminados', 'ok'); creators = await api('/api/creators'); if (cur !== '*' && !creators.some((c) => ckey(c) === cur)) cur = '*'; drawAll(); load(true);
  }
  function clipCard(c) {
    return h('div', { class: 'clip' + (picked.has(c.id) ? ' picked' : ''), onclick: () => {
      if (selecting) { picked.has(c.id) ? picked.delete(c.id) : picked.add(c.id); drawBar(); paint(); } else openClip(c.id, () => { load(true); });
    } }, h('div', { class: 'th' }, c.has_thumb ? h('img', { src: `/api/clips/${c.id}/thumb?v=${c.created_at}`, loading: 'lazy' }) : '🎬',
      h('span', { class: 'score ' + scoreClass(c.score) }, '🔥 ' + c.score),
      h('span', { class: 'dur', style: { top: '8px', bottom: 'auto' } }, fmtDur(c.duration)),
      h('div', { style: { position: 'absolute', left: 0, right: 0, bottom: 0, padding: '28px 10px 8px', background: 'linear-gradient(to top, rgba(0,0,0,.88), rgba(0,0,0,0))', color: '#fde047', fontWeight: 800, fontSize: '15px', lineHeight: 1.15, textTransform: 'uppercase', textAlign: 'left', display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical', overflow: 'hidden' } }, c.title),
      selecting && h('span', { class: 'pickbox' }, picked.has(c.id) ? '✓' : ''), !selecting && c.origin === 'edit' && h('span', { class: 'badge ed' }, '✂ Editado')),
      h('div', { class: 'b' },
        c.moment && h('span', { style: { display: 'inline-block', background: '#fde04722', color: '#fde047', border: '1px solid #fde04755', borderRadius: '999px', fontSize: '11px', fontWeight: 700, padding: '2px 8px', marginBottom: '6px' } }, c.moment),
        c.summary && h('div', { class: 'm', style: { fontWeight: 400, marginBottom: '6px' } }, c.summary),
        cur === '*' && h('div', { class: 'm', style: { fontWeight: 400 } }, c.creator || 'Sin creador'),
        h('div', { class: 'row', style: { marginTop: '8px' } },
          h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); openClip(c.id, () => { load(true); }); } }, '✏ Editar'),
          h('a', { class: 'btn g sm', href: `/api/clips/${c.id}/download`, onclick: (e) => e.stopPropagation() }, '⬇ Exportar'))));
  }
  function paint() {
    if (!clipsNow.length) { grid.replaceChildren(h('div', { class: 'empty', style: { gridColumn: '1/-1' } }, 'No hay clips con estos filtros.')); return; }
    const groups = new Map();
    for (const c of clipsNow) {
      if (!groups.has(c.job_id)) groups.set(c.job_id, { job_id: c.job_id, title: c.job_title || ('Proyecto ' + c.job_id), kind: c.job_kind || '', clips: [] });
      groups.get(c.job_id).clips.push(c);
    }
    const ordered = [...groups.values()].sort((a, b) => (a.kind === 'live' ? 0 : 1) - (b.kind === 'live' ? 0 : 1));
    const els = [];
    for (const g of ordered) {
      els.push(h('h3', { style: { gridColumn: '1/-1', margin: '18px 0 0' } }, (g.kind === 'live' ? '📡 Clips de este Live — ' : '🎞 ') + g.title));
      els.push(...g.clips.map(clipCard));
    }
    grid.replaceChildren(...els);
  }
  let lastSig = '';
  const load = async (force) => {
    const p = new URLSearchParams(); if (f.q.value) p.set('q', f.q.value); if (+f.score.value) p.set('min_score', f.score.value); if (f.job.value) p.set('job_id', f.job.value);
    if (cur !== '*') p.set('creator', cur);
    clipsNow = await api('/api/clips?' + p);
    const sig = clipsNow.map((c) => c.id + ':' + c.title + ':' + c.created_at).join('|') + [...picked].join(',');
    if (!force && sig === lastSig) return;
    lastSig = sig; paint(); drawBar();
  };
  function drawAll() { drawSide(); drawHead(); drawBar(); }
  [f.q, f.score, f.job].forEach((el) => el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', () => load(true)));
  drawAll(); await load(true);
  every(async () => {                                              // clips nuevos (directos) y contadores de creadores
    const fresh = await api('/api/creators');
    if (JSON.stringify(fresh) !== JSON.stringify(creators)) { creators = fresh; drawSide(); drawHead(); }
    await load(false);
  }, 5000);
  return h('div', { class: 'lib' }, side, h('div', { style: { minWidth: 0 } }, head, h('p', { class: 'sub' }, 'Cada creador tiene su carpeta. Haz clic en un clip para verlo y corregirlo, o usa "Seleccionar clips" para llevar varios al editor.'),
    livesSection,
    h('div', { class: 'row', style: { marginBottom: '16px' } }, h('div', { style: { flex: 2, minWidth: '220px' } }, f.q), h('div', { style: { flex: 1, minWidth: '160px' } }, f.score), h('div', { style: { flex: 1, minWidth: '180px' } }, f.job)), grid, bar));
}

async function openClip(id, onClose) {
  const c = await api('/api/clips/' + id);
  const isEdit = c.origin === 'edit';
  const cap = await api(`/api/clips/${id}/captions`);
  const video = h('video', { controls: true, autoplay: true, playsinline: true, src: `/api/clips/${id}/video?v=${Date.now()}` });
  const title = h('input', { type: 'text', value: c.title });
  const inputs = cap.chunks.map((ch) => h('input', { type: 'text', value: ch.text }));
  let theme = cap.theme;
  const themeBox = h('div', { class: 'themes' });
  const drawThemes = () => themeBox.replaceChildren(...state.themes.map((t) => h('div', { class: 'theme' + (t.id === theme ? ' on' : ''), onclick: () => { theme = t.id; drawThemes(); } }, t.label)));
  drawThemes();
  const layout = h('select', { value: cap.layout }, opt('blur', 'Vertical · fondo desenfocado'), opt('fill', 'Vertical · recorte central'), opt('original', 'Original'), opt('gaming', '🎮 Gaming · cámara en esquina'), opt('duo', '🎬 Dúo / Reacción · personas arriba, contenido abajo'), opt('speaker', '🎙 Entrevista · cámara al que habla'), opt('camara', '📷 Cámara completa · toda la pantalla'));
  const descr = h('div', {});
  const close = () => { modal.remove(); onClose && onClose(); };
  const busy = async (btn, label, fn) => {
    const old = btn.textContent; btn.disabled = true; btn.textContent = label;
    try { await fn(); video.src = `/api/clips/${id}/video?v=${Date.now()}`; toast('Clip actualizado', 'ok'); }
    catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; btn.textContent = old; }
  };
  const saveSubs = h('button', { class: 'btn', onclick: (e) => busy(e.target, 'Renderizando…', () => api(`/api/clips/${id}/captions`, { method: 'PUT', body: { texts: inputs.map((i) => i.value) } })) }, 'Guardar subtítulos y re-renderizar');
  const applyStyle = h('button', { class: 'btn g', onclick: (e) => busy(e.target, 'Renderizando…', () => api(`/api/clips/${id}/rerender`, { method: 'POST', body: { theme, layout: layout.value, captions: true } })) }, 'Aplicar estilo');
  const editBtn = h('button', { class: 'btn', style: { background: 'linear-gradient(90deg,#22d3ee,#7c5cff)' }, onclick: async (e) => {
    e.target.disabled = true;
    try { close(); if (isEdit && c.editor_id) location.hash = '#/editor/' + c.editor_id; else await openInEditor([id]); } catch (err) { toast(err.message, 'err'); }
  } }, isEdit ? '✂ Reabrir en el editor' : '✂ Editar en el editor');
  const modal = h('div', { class: 'modal', onclick: (e) => { if (e.target === modal) close(); } }, h('div', { class: 'mbox' },
    h('div', {}, video, h('div', { class: 'row', style: { marginTop: '12px' } },
      h('a', { class: 'btn', href: `/api/clips/${id}/download` }, '⬇ Descargar'), editBtn,
      h('button', { class: 'btn g', onclick: () => copy(title.value, 'Título copiado') }, 'Copiar título'))),
    h('div', {},
      h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('h2', { style: { margin: 0 } }, 'Clip #' + id), h('button', { class: 'btn g sm', onclick: close }, 'Cerrar ✕')),
      h('div', { class: 'row', style: { margin: '8px 0' } }, h('span', { class: 'score ' + scoreClass(c.score), style: { position: 'static' } }, c.score), h('span', { class: 'm' }, `${c.duration}s · ${c.creator || 'Sin creador'} · ${c.reason}`)),
      h('label', {}, 'Título'), h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, title, h('button', { class: 'btn g sm', onclick: async () => { try { await api('/api/clips/' + id, { method: 'PATCH', body: { title: title.value } }); toast('Título guardado', 'ok'); } catch (e) { toast(e.message, 'err'); } } }, 'Guardar')),
      h('label', {}, 'Descripción para publicar'), h('button', { class: 'btn g sm', onclick: async (e) => { e.target.disabled = true; try { const d = await api(`/api/clips/${id}/captions?caption=1`); descr.replaceChildren(h('textarea', { value: d.caption }), h('button', { class: 'btn g sm', onclick: () => copy(d.caption) }, 'Copiar')); } finally { e.target.disabled = false; } } }, 'Generar con IA'), descr,
      isEdit ? h('div', { class: 'note', style: { marginTop: '14px' } }, '✂ Este clip se creó en el editor. Para cambiar subtítulos, estilo, música o recortes, reábrelo en el editor.') : [
        h('label', {}, 'Subtítulos (corrige cualquier palabra: los tiempos se conservan)'),
        h('div', { style: { maxHeight: '220px', overflow: 'auto', paddingRight: '4px' } }, cap.chunks.map((ch, i) => h('div', { class: 'chunk' }, h('small', {}, fmtDur(ch.start)), inputs[i]))),
        h('div', { style: { marginTop: '8px' } }, saveSubs),
        h('label', {}, 'Estilo de subtítulos'), themeBox,
        h('label', {}, 'Formato'), layout, h('div', { style: { marginTop: '8px' } }, applyStyle)],
      h('div', { style: { marginTop: '18px' } }, h('button', { class: 'btn d sm', onclick: async () => { if (confirm('¿Borrar este clip?')) { await api('/api/clips/' + id, { method: 'DELETE' }); close(); } } }, 'Eliminar clip')))));
  document.body.append(modal);
}

/* ------------------------------------------------------------------ Ajustes */
async function pageAjustes() {
  const s = await api('/api/settings'), sys = await api('/api/system');
  const g = {};  // getters
  const num = (k, label, step) => { const i = h('input', { type: 'number', value: s[k], step: step || 1 }); g[k] = () => +i.value; return h('div', {}, h('label', {}, label), i); };
  const sel = (k, label, opts, hint) => { const i = h('select', { value: s[k] }, opts.map(([v, t]) => opt(v, t))); g[k] = () => i.value; return h('div', {}, h('label', {}, label), i, hint && h('div', { class: 'hint' }, hint)); };
  const txt = (k, label, type, hint, area) => { const i = h(area ? 'textarea' : 'input', { type: area ? undefined : (type || 'text'), value: s[k] }); g[k] = () => i.value; return h('div', {}, h('label', {}, label), i, hint && h('div', { class: 'hint' }, hint)); };
  const chk = (k, label) => { const i = h('input', { type: 'checkbox', checked: s[k] }); g[k] = () => i.checked; return h('label', { class: 'sw' }, i, label); };
  const themeInput = themeSelect(s.caption_theme); g.caption_theme = () => themeInput.value;
  const aiBox = h('div', {});
  const presets = { groq: ['https://api.groq.com/openai/v1', 'openai/gpt-oss-20b'], gemini: ['https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-2.5-flash'],
    openrouter: ['https://openrouter.ai/api/v1', 'meta-llama/llama-3.3-70b-instruct:free'], openai: ['https://api.openai.com/v1', 'gpt-4o-mini'] };
  const baseUrl = h('input', { type: 'text', value: s.openai_base_url }), model = h('input', { type: 'text', value: s.openai_model });
  g.openai_base_url = () => baseUrl.value; g.openai_model = () => model.value;
  const key = h('input', { type: 'password', placeholder: s.openai_key_set ? '•••••• (guardada; deja vacío para conservar)' : 'API key', autocomplete: 'off' }); g.openai_key = () => key.value;
  const preset = h('select', { onchange: (e) => { const p = presets[e.target.value]; if (p) { baseUrl.value = p[0]; model.value = p[1]; } } },
    opt('', 'Elegir proveedor…'), opt('groq', 'Groq (plan gratis con límite diario)'), opt('gemini', 'Google Gemini (plan gratis con límite)'), opt('openrouter', 'OpenRouter (modelos :free)'), opt('openai', 'OpenAI (de pago)'));
  const ollamaModel = h('select', { value: s.ollama_model }, [['qwen2.5:3b', 'qwen2.5:3b · recomendado (~2 GB)'], ['qwen2.5:7b', 'qwen2.5:7b · mejor calidad (~4.7 GB)'], ['llama3.2:3b', 'llama3.2:3b (~2 GB)'], ['gemma2:2b', 'gemma2:2b · ligero (~1.6 GB)'], ['qwen2.5:14b', 'qwen2.5:14b · máxima calidad, necesita 16 GB RAM']].map(([v, t]) => opt(v, t)));
  g.ollama_model = () => ollamaModel.value;
  const aiProv = h('select', { value: s.ai_provider }, opt('auto', 'Automático (Ollama si está, si no motor integrado) — recomendado'), opt('ollama', 'Ollama local (gratis e ilimitado)'), opt('openai', 'API compatible con OpenAI (plan gratis con límites)'), opt('builtin', 'Solo motor integrado (sin IA)'));
  g.ai_provider = () => aiProv.value;
  const aiStatus = h('div', { class: 'note' }, 'Comprobando IA…');
  const aiBtns = h('div', { class: 'row', style: { marginTop: '10px' } });
  async function refreshAI() {
    const a = await api('/api/ai/status');
    const o = a.ollama;
    aiStatus.className = 'note' + (a.provider === 'builtin' ? ' warn' : '');
    aiStatus.replaceChildren(h('b', {}, 'Motor activo: ' + a.engine), h('div', {}, `Ollama: ${o.running ? 'encendido' : 'no detectado'} · modelo ${a.model}: ${o.installed ? 'instalado ✔' : 'sin descargar'}`),
      a.pull && a.pull.running && h('div', {}, `Descargando modelo: ${a.pull.status} ${a.pull.total ? Math.round(a.pull.completed / a.pull.total * 100) + '%' : ''}`), a.pull && a.pull.error && h('div', { style: { color: 'var(--bad)' } }, a.pull.error),
      a.provider === 'builtin' && h('div', {}, 'Funciona sin instalar nada. Con Ollama (gratis) los títulos y puntajes mejoran mucho.'));
    aiBtns.replaceChildren(!o.running && h('a', { class: 'btn g sm', href: 'https://ollama.com/download', target: '_blank', rel: 'noopener' }, '1 · Instalar Ollama (gratis)'),
      o.running && !o.installed && h('button', { class: 'btn sm', onclick: async () => { try { await save(true); await api('/api/ai/pull', { method: 'POST' }); toast('Descargando modelo…'); refreshAI(); } catch (e) { toast(e.message, 'err'); } } }, '2 · Descargar modelo'),
      h('button', { class: 'btn g sm', onclick: async (e) => { e.target.disabled = true; try { await save(true); const r = await api('/api/ai/test', { method: 'POST' }); toast(r.message, r.ok ? 'ok' : 'err'); refreshAI(); } finally { e.target.disabled = false; } } }, 'Probar IA'));
  }
  refreshAI(); every(refreshAI, 4000);
  async function save(silent) {
    const body = {}; for (const k of Object.keys(g)) body[k] = g[k]();
    state.settings = await api('/api/settings', { method: 'PUT', body });
    if (!silent) toast('Ajustes guardados', 'ok');
  }
  const ytBtn = h('button', { class: 'btn g sm', onclick: async (e) => { e.target.disabled = true; e.target.textContent = 'Actualizando…'; try { const r = await api('/api/system/update-ytdlp', { method: 'POST' }); toast(r.ok ? 'yt-dlp actualizado' : r.output, r.ok ? 'ok' : 'err'); } catch (err) { toast(err.message, 'err'); } e.target.disabled = false; e.target.textContent = 'Actualizar yt-dlp'; } }, 'Actualizar yt-dlp');
  const diag = (ok, t) => h('div', {}, (ok ? '✅ ' : '❌ ') + t);
  return h('div', {}, h('h1', {}, 'Ajustes'), h('p', { class: 'sub' }, 'Los cambios se aplican a los proyectos nuevos.'),
    h('div', { class: 'card' }, h('h2', {}, '🎙 Transcripción (Whisper local)'), h('div', { class: 'grid2' },
      sel('whisper_model', 'Modelo', [['auto', 'Automático (GPU: large-v3-turbo · CPU: base)'], ['tiny', 'tiny · muy rápido, poco preciso'], ['base', 'base'], ['small', 'small · buen equilibrio en CPU'], ['medium', 'medium · más preciso, lento en CPU'], ['large-v3-turbo', 'large-v3-turbo · recomendado con GPU'], ['large-v3', 'large-v3 · máxima precisión']], 'Más grande = más preciso y más lento. La primera vez descarga el modelo.'),
      sel('whisper_device', 'Procesador', [['auto', 'Automático'], ['cpu', 'CPU'], ['cuda', 'GPU NVIDIA (CUDA)']]),
      sel('language', 'Idioma', [['auto', 'Detectar automático'], ['es', 'Español'], ['en', 'English'], ['pt', 'Português'], ['fr', 'Français'], ['it', 'Italiano'], ['de', 'Deutsch']], 'Por defecto: español. Fijar el idioma mejora la precisión si siempre hablas el mismo.'),
      chk('parallel_transcribe', 'Transcripción en paralelo (videos largos, en PCs con 4 o más núcleos: parte el audio en trozos y usa todos los núcleos)')),
      txt('vocabulary', 'Vocabulario propio (nombres, marcas, jerga)', 'text', 'Ej: Turbox, Kick, Ibai, ElRubius, cripto, streamer. Ayuda a escribir bien nombres raros.', true)),
    h('div', { class: 'card' }, h('h2', {}, '🤖 IA para títulos y puntaje viral'), h('label', {}, 'Proveedor'), aiProv, aiStatus, aiBtns,
      h('div', { class: 'grid2' }, h('div', {}, h('label', {}, 'Modelo de Ollama'), ollamaModel)),
      h('details', { style: { marginTop: '12px' } }, h('summary', { style: { cursor: 'pointer', fontWeight: 700 } }, 'API en la nube (opcional, planes gratis con límites)'),
        h('label', {}, 'Atajo'), preset, h('label', {}, 'Base URL'), baseUrl, h('label', {}, 'Modelo'), model, h('label', {}, 'API key'), key,
        h('div', { class: 'hint' }, 'Elige "API compatible con OpenAI" arriba para usarla. Si se agota la cuota se usa el motor integrado automáticamente.'))),
    h('div', { class: 'card' }, h('h2', {}, '✂️ Clips'), h('div', { class: 'grid2' }, num('clip_min_s', 'Duración mínima (s)'), num('clip_max_s', 'Duración máxima (s)'), num('max_clips', 'Máx. clips por video'), num('min_score', 'Puntaje mínimo'), num('pad_before', 'Margen antes (s)', 0.05), num('pad_after', 'Margen después (s)', 0.05),
      (() => { // «De qué trata el video» (por defecto): "general" no es opción; si no se toca, se conserva el valor guardado
        const vals = ['entrevista', 'podcast', 'resumen', 'pelicula', 'gaming'];
        let v = vals.includes(s.content_type) ? s.content_type : 'general';
        const i = h('select', { value: v === 'general' ? '' : v, onchange: (e) => { v = e.target.value || 'general'; } },
          h('option', { value: '', disabled: true }, '— Elige el tipo de video —'),
          [['entrevista', '🎙 Entrevista'], ['podcast', '🎧 Podcast'], ['resumen', '📝 Resumen'], ['pelicula', '🎬 Película'], ['gaming', '🎮 Gaming']].map(([x, t]) => opt(x, t)));
        g.content_type = () => v;
        return h('div', {}, h('label', {}, 'De qué trata el video (por defecto)'), i, h('div', { class: 'hint' }, 'Inclina la selección de momentos: entrevista/podcast premian la discusión, resumen las ventanas narrativas largas, gaming la energía alta.'));
      })()), chk('tiktok_mode', '📱 Modo TikTok (clips de 20–40 s, gancho primero)')),
    h('div', { class: 'card' }, h('h2', {}, '🎨 Formato y subtítulos'), h('div', { class: 'grid2' },
      sel('layout', 'Formato', [['blur', 'Vertical · fondo desenfocado'], ['fill', 'Vertical · recorte central'], ['original', 'Original'], ['gaming', '🎮 Gaming · cámara en esquina'], ['duo', '🎬 Dúo / Reacción · personas arriba, contenido abajo'], ['speaker', '🎙 Entrevista · cámara al que habla'], ['camara', '📷 Cámara completa · toda la pantalla']]),
      sel('cam_corner', 'Esquina de la cámara (gaming)', [['tl', 'Arriba a la izquierda'], ['tr', 'Arriba a la derecha'], ['bl', 'Abajo a la izquierda'], ['br', 'Abajo a la derecha']], 'Dónde está la cámara dentro del video original. En el clip se recorta su caja exacta (sin gameplay dentro) y sigue la cara donde se mueva.'),
      sel('quality_profile', 'Calidad de los clips', [['rapida', '⚡ Rápida · 30 fps · la más veloz'], ['equilibrada', 'Equilibrada · 30 fps · buena calidad'], ['alta', '⭐ Alta · 60 fps · muy buena calidad']], 'Alta es el perfil por defecto. Rápida usa el modelo tiny y es la mejor para videos largos.'),
      h('div', {}, h('label', {}, 'Estilo de subtítulos'), themeInput),
      sel('caption_position', 'Posición', [['low', 'Abajo (recomendado)'], ['center', 'Centro'], ['top', 'Arriba']])), chk('captions', 'Quemar subtítulos en el video'), chk('loudnorm', 'Normalizar el volumen (recomendado)')),
    h('div', { class: 'card' }, h('h2', {}, '💧 Marca de agua'), h('div', { class: 'grid2' },
      sel('watermark_mode', 'Marca de agua', [['logo', 'Logo ClipsViralesRD_'], ['text', 'Texto'], ['none', 'Ninguna']], 'El logo se usa tal cual, con su fondo azul que degrada a negro. Por defecto: ninguna.'),
      sel('watermark_position', 'Posición', [['br', 'Abajo a la derecha'], ['bl', 'Abajo a la izquierda'], ['tr', 'Arriba a la derecha'], ['tl', 'Arriba a la izquierda']]),
      txt('watermark_text', 'Texto de la marca de agua', 'text', 'Se usa cuando el modo es Texto.'),
      num('watermark_opacity', 'Opacidad de la marca (%)')),
      h('div', { class: 'hint' }, 'La marca de agua se quema en los clips nuevos.'),
      (() => { // imagen personalizada para la marca (v3.1.4): PNG/JPG, máx. 5 MB
        const note = h('div', { class: 'hint' }, 'Comprobando imagen personalizada…');
        const file = h('input', { type: 'file', accept: 'image/png,image/jpeg' });
        const btn = h('button', { class: 'btn g sm', onclick: async (e) => {
          if (!file.files[0]) { toast('Elige una imagen PNG o JPG.', 'err'); return; }
          e.target.disabled = true;
          try {
            const fd = new FormData(); fd.append('file', file.files[0]);
            const r = await fetch('/api/settings/watermark', { method: 'POST', headers: { 'X-TBX': '1' }, body: fd });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || ('Error ' + r.status));
            toast('Imagen personalizada subida', 'ok');
            note.textContent = 'Imagen personalizada activa: watermark.png (se usa en vez del logo por defecto).';
          } catch (err) { toast(err.message, 'err'); } finally { e.target.disabled = false; }
        } }, 'Subir imagen personalizada');
        api('/api/settings/watermark').then((r) => { note.textContent = r.custom ? 'Imagen personalizada activa: watermark.png (se usa en vez del logo por defecto).' : 'Sin imagen personalizada: se usa el logo por defecto.'; }).catch(() => { note.textContent = 'No se pudo comprobar la imagen personalizada.'; });
        return h('div', { style: { marginTop: '10px' } }, h('label', {}, 'Imagen personalizada (PNG o JPG, máx. 5 MB)'), file, h('div', { style: { marginTop: '6px' } }, btn), note);
      })()),
    h('div', { class: 'card' }, h('h2', {}, '⬇ Descarga de videos y directos'), h('div', { class: 'grid2' },
      sel('cookies_browser', 'Cookies del navegador', [['', 'No usar'], ['firefox', 'Firefox (recomendado)'], ['chrome', 'Chrome'], ['edge', 'Edge'], ['brave', 'Brave'], ['opera', 'Opera']], 'Si YouTube o Kick bloquean la descarga ("no eres un bot", 403), inicia sesión en ese navegador y elígelo aquí.'),
      (() => { // subir cookies.txt (v3.1.5): solo el administrador; el servidor descarga de YouTube con esa sesión
        if (!state.me || !state.me.is_admin) return h('div', { class: 'hint' }, 'Solo el administrador puede subir el archivo de cookies.');
        const note = h('div', { class: 'hint' }, 'Comprobando archivo de cookies…');
        const file = h('input', { type: 'file', accept: '.txt,text/plain' });
        const up = h('button', { class: 'btn g sm', onclick: async (e) => {
          if (!file.files[0]) { toast('Elige tu archivo cookies.txt.', 'err'); return; }
          e.target.disabled = true;
          try {
            const fd = new FormData(); fd.append('file', file.files[0]);
            const r = await fetch('/api/settings/cookies', { method: 'POST', headers: { 'X-TBX': '1' }, body: fd });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || ('Error ' + r.status));
            toast('Cookies subidas: YouTube descargará con tu sesión', 'ok');
            note.textContent = 'Cookies cargadas (subidas ' + (d.uploaded_at || 'ahora') + '). YouTube descargará con tu sesión.';
          } catch (err) { toast(err.message, 'err'); } finally { e.target.disabled = false; }
        } }, 'Subir cookies.txt');
        const rm = h('button', { class: 'btn d sm', onclick: async (e) => {
          if (!confirm('¿Quitar el archivo de cookies? YouTube volverá a descargar sin sesión.')) return;
          e.target.disabled = true;
          try {
            const r = await fetch('/api/settings/cookies', { method: 'DELETE', headers: { 'X-TBX': '1' } });
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.detail || ('Error ' + r.status));
            toast('Archivo de cookies eliminado', 'ok');
            note.textContent = 'Sin archivo de cookies: YouTube descarga sin sesión (puede pedir verificar que no eres un bot).';
          } catch (err) { toast(err.message, 'err'); } finally { e.target.disabled = false; }
        } }, 'Quitar');
        api('/api/settings/cookies').then((r) => { note.textContent = r.loaded ? 'Cookies cargadas (subidas ' + (r.uploaded_at || '') + '). YouTube descargará con tu sesión.' : 'Sin archivo de cookies: YouTube descarga sin sesión (puede pedir verificar que no eres un bot).'; }).catch(() => { note.textContent = 'No se pudo comprobar el archivo de cookies.'; });
        return h('div', {}, h('label', {}, 'Subir cookies.txt (YouTube)'), file, h('div', { style: { marginTop: '6px', display: 'flex', gap: '8px' } }, up, rm), note, h('div', { class: 'hint' }, 'Exporta cookies.txt desde Firefox/Chrome con tu sesión de YouTube iniciada (extensión "Get cookies.txt LOCALLY" o similar). El archivo queda en el servidor y se usa para todas las descargas; en el servidor gratis se borra al reiniciar.'));
      })(), sel('max_height', 'Calidad máxima de descarga', [[720, '720p'], [1080, '1080p'], [1440, '1440p'], [2160, '4K']])), h('div', { style: { marginTop: '12px' } }, ytBtn)),
    h('div', { class: 'card' }, h('h2', {}, '📡 Directos (Live)'), h('div', { class: 'grid2' }, num('live_segment_s', 'Tamaño de segmento (s)'), num('live_min_score', 'Puntaje mínimo para auto-clip'), num('live_max_clips_per_hour', 'Máx. clips por hora'), num('live_wait_minutes', 'Esperar inicio del directo (min)')),
      h('div', { class: 'hint' }, 'Segmentos más cortos = clips más rápidos tras el momento, pero más carga de transcripción.')),
    h('div', { class: 'card' }, h('h2', {}, '🩺 Diagnóstico'), diag(!!sys.ffmpeg, 'ffmpeg: ' + (sys.ffmpeg || 'NO encontrado')), diag(!!sys.ytdlp, 'yt-dlp: ' + (sys.ytdlp || 'NO encontrado')),
      diag(sys.whisper_installed, 'faster-whisper: ' + (sys.whisper_installed ? 'instalado · ' + sys.whisper_plan : 'no instalado (ejecuta iniciar.bat)')), diag(sys.opencv, 'Seguimiento de rostros/objetos (OpenCV): ' + (sys.opencv ? 'instalado' : 'no instalado (ejecuta iniciar.bat)')), diag(sys.node || sys.deno, 'Runtime JS para YouTube: ' + (sys.deno ? 'deno' : sys.node ? 'node' : 'ninguno (instala Node.js)')), h('div', { class: 'hint' }, 'Datos en: ' + sys.data_dir + ' · versión ' + sys.version)),
    h('button', { class: 'btn', onclick: () => save(false).catch((e) => toast(e.message, 'err')) }, 'Guardar ajustes'));
}

const PAGES = { crear: pageCrear, proyectos: pageProyectos, biblioteca: pageBiblioteca, editor: pageEditor, cuentas: pageCuentas, ajustes: pageAjustes, usuarios: pageUsuarios };
boot();
