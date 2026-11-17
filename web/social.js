'use strict';
/* AdvvxClips · Cuentas sociales (v3.1.3): conectar TikTok, Facebook, Instagram y
   YouTube, y publicar clips sin salir de la página, ahora o programados.
   Nada aquí finge una conexión: solo se muestra "conectada" una cuenta con
   token real devuelto por /api/social/accounts. */
const SOCIAL_LIMITS = {
  youtube: 'Título máx. 100 caracteres · descripción máx. 5000 · programación nativa en YouTube.',
  facebook: 'Reel en tu Página · programación nativa en Facebook.',
  instagram: 'Pie de foto máx. 2200 caracteres · SIN programación nativa: la app publica cuando llega la hora (servidor corriendo) · exige URL pública del video.',
  tiktok: 'Texto máx. 2200 caracteres · sin programación desde la API: programa en TikTok Studio o publica ahora.',
};
const fmtWhen = (ts) => ts ? new Date(ts * 1000).toLocaleString('es', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
const POST_STATUS = { queued: '⏳ En cola / programada', publishing: '📤 Publicando…', published: '✅ Publicada', scheduled: '📅 Programada en la plataforma', failed: '❌ Falló' };

async function pageCuentas(q) {
  const meta = await api('/api/social/platforms');
  const plats = meta.platforms;
  let accounts = await api('/api/social/accounts');
  let queue = await api('/api/social/queue');
  let clips = [];
  try { clips = await api('/api/clips'); } catch (e) { /* sin clips todavía */ }
  const settings = state.settings || await api('/api/settings');

  const byPlat = (p) => accounts.filter((a) => a.platform === p);
  const wrap = h('div', {});
  const credBox = h('div', {});

  function credField(key, label, type) {
    const inp = h('input', { type: type || 'text', value: settings[key] || '', placeholder: 'Pegar aquí…', autocomplete: 'off' });
    inp.dataset.k = key;
    return h('div', {}, h('label', {}, label), inp);
  }
  function drawCreds() {
    if (!meta.is_admin) {
      credBox.replaceChildren(h('div', { class: 'note' }, 'Las claves de desarrollador las configura el administrador. Pídele que pegue las claves de Google Cloud / Meta / TikTok aquí.'));
      return;
    }
    const fields = [
      credField('google_client_id', 'YouTube · Google Client ID'),
      credField('google_client_secret', 'YouTube · Google Client Secret', 'password'),
      credField('meta_app_id', 'Facebook / Instagram · Meta App ID'),
      credField('meta_app_secret', 'Facebook / Instagram · Meta App Secret', 'password'),
      credField('tiktok_client_key', 'TikTok · Client Key'),
      credField('tiktok_client_secret', 'TikTok · Client Secret', 'password'),
      credField('social_public_base_url', 'URL pública de esta app (solo para Instagram, ej. https://tu-app.onrender.com — déjala vacía si la app solo corre en tu PC)'),
    ];
    const saveBtn = h('button', { class: 'btn sm', onclick: async (e) => {
      e.target.disabled = true;
      try {
        const body = {};
        credBox.querySelectorAll('input[data-k]').forEach((i) => { body[i.dataset.k] = i.value.trim(); });
        state.settings = await api('/api/settings', { method: 'PUT', body });
        toast('Credenciales guardadas', 'ok');
      } catch (err) { toast(err.message, 'err'); } finally { e.target.disabled = false; }
    } }, 'Guardar credenciales');
    credBox.replaceChildren(
      h('p', { class: 'sub' }, 'Claves de desarrollador (vacías por defecto). Sin ellas, cada plataforma muestra su guía paso a paso. Solo las ve el administrador; los secretos nunca se muestran de vuelta.'),
      h('div', { class: 'grid2' }, fields), h('div', { style: { marginTop: '10px' } }, saveBtn));
  }

  function platCard(key) {
    const m = plats[key];
    const accs = byPlat(key);
    const card = h('div', { class: 'card' });
    const body = h('div', {});
    const draw = async () => {
      const connected = accs.length > 0;
      body.replaceChildren(
        h('div', { class: 'row', style: { justifyContent: 'space-between' } },
          h('h2', { style: { margin: 0 } }, (key === 'youtube' ? '▶️ ' : key === 'facebook' ? '📘 ' : key === 'instagram' ? '📸 ' : '🎵 ') + m.label),
          h('span', { class: 'pill ' + (connected ? 'done' : 'queued') }, connected ? 'Conectada' : 'Sin conectar')),
        h('p', { class: 'sub' }, SOCIAL_LIMITS[key]),
        connected ? h('div', {}, ...accs.map((a) => h('div', { class: 'job' },
          h('div', { style: { flex: 1 } }, h('div', { class: 't' }, a.account_name),
            h('div', { class: 'm' }, 'Conectada el ' + fmtWhen(a.connected_at) + (a.expired ? ' · ⚠ token caducado: reconecta' : ''))),
          h('button', { class: 'btn d sm', onclick: async () => {
            if (!confirm('¿Desconectar ' + a.account_name + ' de ' + m.label + '? Las publicaciones en cola de esa cuenta se cancelan.')) return;
            await api('/api/social/accounts/' + a.id, { method: 'DELETE' });
            accounts = await api('/api/social/accounts'); queue = await api('/api/social/queue');
            toast('Cuenta desconectada', 'ok'); refreshAll();
          } }, 'Desconectar'))))
        : h('div', { class: 'note warn' }, 'Sin conectar. ' + (m.creds ? 'Claves configuradas: pulsa Conectar y acepta con tu cuenta.' : 'Faltan las claves de desarrollador: sigue la guía de abajo y pégalas en «Credenciales» (solo el administrador).')),
        h('div', { class: 'row', style: { marginTop: '10px' } },
          h('button', { class: 'btn sm', onclick: async (e) => {
            e.target.disabled = true;
            try {
              const r = await api('/api/social/accounts/' + key + '/connect', { method: 'POST', body: {} });
              if (r.needs_credentials) { toast('Faltan las claves: sigue la guía de ' + m.label, 'err'); }
              else location.href = r.auth_url;
            } catch (err) { toast(err.message, 'err'); } finally { e.target.disabled = false; }
          } }, connected ? '＋ Conectar otra cuenta' : '🔗 Conectar ' + m.label),
          key === 'tiktok' && h('a', { class: 'btn g sm', href: 'https://www.tiktok.com/tiktokstudio/upload', target: '_blank', rel: 'noopener' }, '↗ Abrir en TikTok Studio')),
        h('details', { style: { marginTop: '10px' } }, h('summary', { style: { cursor: 'pointer', fontWeight: 700 } }, 'Guía paso a paso para conectar ' + m.label),
          h('ol', { class: 'sub', style: { paddingLeft: '18px' } }, m.guide.map((g) => h('li', {}, g)))));
    };
    draw();
    card.replaceChildren(body);
    return card;
  }

  /* ---------------------------------------------------------- publicar */
  const clipSel = h('select', {}, opt('', 'Elige un clip…'), clips.map((c) => opt(c.id, '#' + c.id + ' · ' + (c.title || 'Clip') + ' (' + fmtDur(c.duration) + ')')));
  const titleIn = h('input', { type: 'text', placeholder: 'Título del video…', maxLength: 200 });
  const tagsIn = h('input', { type: 'text', placeholder: 'Hashtags separados por espacios, ej. rdjavi gta clips (sin # hace falta)' });
  const whenIn = h('input', { type: 'datetime-local' });
  const accChecks = h('div', { class: 'row' });
  const counter = h('div', { class: 'hint' }, '');
  const drawChecks = () => {
    if (!accounts.length) { accChecks.replaceChildren(h('div', { class: 'note warn' }, 'Conecta al menos una cuenta arriba para publicar.')); return; }
    accChecks.replaceChildren(...accounts.map((a) => {
      const cb = h('input', { type: 'checkbox', value: a.id, checked: true });
      return h('label', { class: 'sw' }, cb, plats[a.platform].label + ' · ' + a.account_name);
    }));
  };
  const updateCounter = () => {
    const t = titleIn.value, tags = tagsIn.value;
    counter.textContent = `Título: ${t.length}/100 en YouTube · Texto total (título + hashtags): ${(t + ' ' + tags).length} caracteres · TikTok e Instagram admiten hasta 2200.`;
  };
  [titleIn, tagsIn].forEach((el) => el.addEventListener('input', updateCounter));
  const publishBox = h('div', {});
  async function doPublish(now) {
    const clipId = +clipSel.value;
    if (!clipId) { toast('Elige un clip primero.', 'err'); return; }
    const ids = [...accChecks.querySelectorAll('input:checked')].map((i) => +i.value);
    if (!ids.length) { toast('Elige al menos una cuenta.', 'err'); return; }
    let publish_at = null;
    if (!now) {
      if (!whenIn.value) { toast('Elige fecha y hora, o pulsa «Publicar ahora».', 'err'); return; }
      publish_at = whenIn.value;
    }
    try {
      const r = await api('/api/social/publish', { method: 'POST', body: { clip_id: clipId, accounts: ids, title: titleIn.value, hashtags: tagsIn.value, publish_at } });
      const done = r.posts.filter((p) => p.status === 'published').length;
      const queued = r.posts.filter((p) => p.status === 'queued').length;
      const failed = r.posts.filter((p) => p.status === 'failed');
      toast(done ? done + ' publicado(s) ✅' : queued ? queued + ' programado(s) ⏳' : 'Listo', done ? 'ok' : '');
      if (failed.length) toast('Falló en: ' + failed.map((p) => plats[p.platform].label + ' (' + (p.error || 'error') + ')').join(' · '), 'err');
      queue = await api('/api/social/queue');
      drawQueue();
    } catch (e) { toast(e.message, 'err'); }
  }
  publishBox.replaceChildren(
    h('div', { class: 'fld' }, h('label', {}, 'Clip de la Biblioteca'), clipSel),
    h('div', { class: 'fld' }, h('label', {}, 'Título'), titleIn),
    h('div', { class: 'fld' }, h('label', {}, 'Hashtags'), tagsIn, counter),
    h('div', { class: 'fld' }, h('label', {}, 'Cuentas (conectadas)'), accChecks),
    h('div', { class: 'fld' }, h('label', {}, '¿Cuándo? (vacío = ahora)'), whenIn,
      h('div', { class: 'hint' }, 'Programado: la app publica cuando llega la hora, con el servidor corriendo. YouTube y Facebook lo programan dentro de la propia plataforma; Instagram y TikTok no tienen programación nativa.')),
    h('div', { class: 'row' },
      h('button', { class: 'btn', onclick: () => doPublish(true) }, '🚀 Publicar ahora'),
      h('button', { class: 'btn g', onclick: () => doPublish(false) }, '📅 Programar'),
      h('a', { class: 'btn g sm', href: '#/biblioteca' }, 'Ir a la Biblioteca')));

  /* ---------------------------------------------------------- cola */
  const queueBox = h('div', {});
  function drawQueue() {
    if (!queue.length) { queueBox.replaceChildren(h('div', { class: 'empty' }, 'Aún no hay publicaciones. Publica tu primer clip arriba ☝️')); return; }
    queueBox.replaceChildren(...queue.map((p) => h('div', { class: 'job' },
      h('div', { style: { flex: 1, minWidth: 0 } },
        h('div', { class: 't' }, (p.clip_title || ('Clip #' + p.clip_id)) + '  ', h('span', { class: 'm' }, plats[p.platform].label + ' · ' + p.account_name)),
        h('div', { class: 'row', style: { marginTop: '6px' } },
          h('span', { class: 'pill ' + (p.status === 'published' ? 'done' : p.status === 'failed' ? 'error' : 'queued') }, POST_STATUS[p.status] || p.status),
          h('span', { class: 'm' }, p.publish_at ? 'para el ' + fmtWhen(p.publish_at) : 'ahora')),
        h('div', { class: 'm' }, p.title + (p.hashtags ? '  ' + p.hashtags : '')),
        p.external_url && h('div', {}, h('a', { href: p.external_url, target: '_blank', rel: 'noopener' }, '🔗 Ver publicación')),
        p.error && h('div', { class: 'note bad', style: { marginTop: '6px' } }, 'Error: ' + p.error + ' — no se reintentó automáticamente. Corrige el problema y publica de nuevo.')),
      h('div', { class: 'row' },
        (p.status === 'queued' || p.status === 'scheduled') && h('button', { class: 'btn d sm', onclick: async () => {
          if (!confirm('¿Cancelar esta publicación programada?')) return;
          const r = await api('/api/social/queue/' + p.id, { method: 'DELETE' });
          if (r.warning) toast(r.warning, 'err');
          queue = await api('/api/social/queue'); drawQueue();
        } }, 'Cancelar')))));
  }

  function refreshAll() {
    drawCreds(); drawChecks(); drawQueue(); updateCounter();
  }
  if (q.connected) toast('✅ Cuenta de ' + (plats[q.connected] ? plats[q.connected].label : q.connected) + ' conectada', 'ok');
  if (q.error) toast('No se pudo conectar: ' + decodeURIComponent(q.error), 'err');
  drawCreds(); drawChecks(); drawQueue(); updateCounter();
  every(async () => { queue = await api('/api/social/queue'); drawQueue(); }, 15000);
  return h('div', {},
    h('h1', {}, '🔗 Cuentas'),
    h('p', { class: 'sub' }, 'Conecta TikTok, Facebook, Instagram y YouTube. Una vez conectadas, sube tus clips sin salir de AdvvxClips: elige el clip, escribe título y hashtags, y publica ahora o programa la hora.'),
    h('div', { class: 'note warn' }, '⚠️ Las publicaciones programadas solo salen si la app está abierta cuando llega la hora. Un fallo nunca se reintenta solo: queda visible con su motivo.'),
    h('h2', {}, 'Tus cuentas'),
    h('div', { class: 'grid2' }, ['youtube', 'facebook', 'instagram', 'tiktok'].map(platCard)),
    h('div', { class: 'card' }, h('h2', {}, '🔑 Credenciales de desarrollador'), credBox),
    h('div', { class: 'card' }, h('h2', {}, '🚀 Publicar un clip'), publishBox),
    h('h2', {}, '📅 Publicaciones'), h('div', { class: 'card' }, queueBox));
}
