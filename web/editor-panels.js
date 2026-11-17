'use strict';
/* Paneles del editor. Se añaden al prototipo de Editor (ver editor.js). */
Object.assign(Editor.prototype, {
  // ---------------------------------------------------------------- utilidades de formulario
  slider(label, get, min, max, step, set, fmt = (v) => v) {
    const out = h('span', { class: 'm' }, fmt(get()));
    const inp = h('input', { type: 'range', min, max, step, value: get(),
      oninput: (e) => { const v = +e.target.value; out.textContent = fmt(v); this.mutate((s) => set(s, v), { commit: false, panel: false }); },
      onchange: () => this.mutate(() => {}, { panel: false }) });
    return h('div', { class: 'fld' }, h('label', {}, label + ' ', out), inp);
  },
  numf(label, get, set, o = {}) {
    const inp = h('input', { type: 'number', value: get(), step: o.step || 0.1, min: o.min, max: o.max, onchange: (e) => { const v = parseFloat(e.target.value); if (!isNaN(v)) this.mutate((s) => set(s, v)); } });
    return h('div', { class: 'fld' }, h('label', {}, label), inp);
  },
  selectf(label, get, options, set) {
    return h('div', { class: 'fld' }, h('label', {}, label), h('select', { value: get(), onchange: (e) => this.mutate((s) => set(s, e.target.value), { relayout: true }) }, options.map(([v, t]) => h('option', { value: v }, t))));
  },
  checkf(label, get, set) { return h('label', { class: 'sw' }, h('input', { type: 'checkbox', checked: get(), onchange: (e) => this.mutate((s) => set(s, e.target.checked)) }), label); },
  sectionTitle(t) { return h('h3', { class: 'pn-t' }, t); },
  async busy(btn, label, fn) {
    const old = btn.textContent; btn.disabled = true; btn.textContent = label;
    try { return await fn(); } catch (e) { toast(e.message, 'err'); } finally { btn.disabled = false; btn.textContent = old; }
  },
  async uploadAsset(file) {
    const r = await fetch('/api/editor/assets?name=' + encodeURIComponent(file.name), { method: 'POST', headers: { 'X-TBX': '1' }, body: file });
    const d = await r.json(); if (!r.ok) throw new Error(d.detail || 'No se pudo subir el archivo.');
    this.data.assets[d.id] = d; return d;
  },
  fileButton(label, accept, onFile) {
    const inp = h('input', { type: 'file', accept, style: { display: 'none' }, onchange: async (e) => { const f = e.target.files[0]; e.target.value = ''; if (f) await this.busy(btn, 'Subiendo…', () => onFile(f)); } });
    const btn = h('button', { class: 'btn g sm', onclick: () => inp.click() }, label);
    return h('span', {}, btn, inp);
  },

  // ---------------------------------------------------------------- CLIPS
  panelClips() {
    const sp = this.spec, tl = EdCore.timeline(sp), w = h('div', { class: 'pn' });
    w.append(this.selectf('Formato del video', () => sp.layout, [['blur', 'Vertical · fondo desenfocado'], ['fill', 'Vertical · recorte central'], ['original', 'Original (sin recortar)']], (s, v) => { s.layout = v; }));
    w.append(this.sectionTitle('Clips del proyecto'));
    tl.forEach(({ seg, idx, t0, t1 }) => {
      const sel = this.sel && this.sel.type === 'seg' && this.sel.id === seg.uid;
      w.append(h('div', { class: 'li' + (sel ? ' sel' : ''), onclick: () => { this.sel = { type: 'seg', id: seg.uid }; this.player.seek(t0 + 0.01); this.renderTimeline(); this.renderPanel(); } },
        h('span', { class: 'n' }, idx + 1), h('span', { class: 't' }, (this.data.clips[seg.clip_id] || {}).title || seg.title || 'Clip'), h('span', { class: 'm' }, fmtT(t1 - t0)),
        h('button', { class: 'btn g sm', title: 'Subir', disabled: idx === 0, onclick: (e) => { e.stopPropagation(); this.mutate((s) => { const [it] = s.segments.splice(idx, 1); s.segments.splice(idx - 1, 0, it); }); } }, '▲'),
        h('button', { class: 'btn g sm', title: 'Bajar', disabled: idx === tl.length - 1, onclick: (e) => { e.stopPropagation(); this.mutate((s) => { const [it] = s.segments.splice(idx, 1); s.segments.splice(idx + 1, 0, it); }); } }, '▼'),
        h('button', { class: 'btn d sm', title: 'Quitar', disabled: tl.length <= 1, onclick: (e) => { e.stopPropagation(); this.sel = { type: 'seg', id: seg.uid }; this.deleteSelected(); } }, '✕')));
    });
    w.append(h('button', { class: 'btn g', style: { marginTop: '8px' }, onclick: () => this.addClipModal() }, '＋ Añadir otro clip de la Biblioteca'));
    const seg = this.selectedSeg();
    if (seg) {
      const px = this.data.proxies[seg.clip_id] || {}, lo = px.ready ? px.base : 0, hi = px.ready ? px.base + px.duration : seg.src_out;
      w.append(this.sectionTitle('Recorte del clip seleccionado'));
      w.append(h('div', { class: 'hint' }, `Puedes recortar o ampliar dentro del material disponible (${fmtT(lo)} – ${fmtT(hi)} del video original). También arrastra los bordes en la línea de tiempo.`));
      w.append(h('div', { class: 'grid2' },
        this.numf('Inicio (s del original)', () => seg.src_in, (s, v) => { const g = s.segments.find((x) => x.uid === seg.uid); g.src_in = EdCore.clamp(v, lo, g.src_out - 0.3); }, { step: 0.1 }),
        this.numf('Fin (s del original)', () => seg.src_out, (s, v) => { const g = s.segments.find((x) => x.uid === seg.uid); g.src_out = EdCore.clamp(v, g.src_in + 0.3, hi); }, { step: 0.1 })));
      w.append(this.slider('Volumen del clip', () => Math.round(seg.volume * 100), 0, 200, 5, (s, v) => { s.segments.find((x) => x.uid === seg.uid).volume = v / 100; }, (v) => v + '%'));
      if (seg.volume > 1) w.append(h('div', { class: 'hint' }, 'La vista previa limita el volumen al 100 %; el video exportado sí lo amplifica.'));
      w.append(h('div', { class: 'row', style: { marginTop: '8px' } }, h('button', { class: 'btn g sm', onclick: () => this.splitAtCursor() }, '✂ Dividir en el cursor (S)')));
    }
    return w;
  },
  async addClipModal() {
    await this.flushSave();
    const clips = await api('/api/clips');
    const grid = h('div', { class: 'clips', style: { maxHeight: '60vh', overflow: 'auto' } }, clips.filter((c) => c.origin !== 'edit').map((c) => h('div', { class: 'clip', onclick: async () => {
      try {
        const p = await api(`/api/editor/projects/${this.pid}/clips`, { method: 'POST', body: { clip_id: c.id } });
        this.spec = p.spec; this.data.proxies = p.proxies; this.data.clips = p.clips; this.data.assets = p.assets;
        this.history = [JSON.stringify(this.spec)]; this.hi = 0; this.player.changed(); modal.remove(); this.renderAll(); this.waitForProxies(); toast('Clip añadido', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    } }, h('div', { class: 'th' }, c.has_thumb ? h('img', { src: `/api/clips/${c.id}/thumb`, loading: 'lazy' }) : '🎬', h('span', { class: 'dur' }, fmtDur(c.duration))),
      h('div', { class: 'b' }, h('div', {}, c.title), h('div', { class: 'm', style: { fontWeight: 400 } }, c.creator || 'Sin creador')))));
    const modal = h('div', { class: 'modal', onclick: (e) => { if (e.target === modal) modal.remove(); } }, h('div', { class: 'card', style: { maxWidth: '900px', width: '100%' } },
      h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('h2', {}, 'Añadir clip al proyecto'), h('button', { class: 'btn g sm', onclick: () => modal.remove() }, 'Cerrar ✕')), clips.length ? grid : h('div', { class: 'empty' }, 'No hay clips en la Biblioteca.')));
    document.body.append(modal);
  },

  // ---------------------------------------------------------------- SUBTÍTULOS
  panelSubs() {
    const sp = this.spec, sub = sp.subtitles, w = h('div', { class: 'pn' });
    w.append(this.checkf('Mostrar subtítulos', () => sub.enabled, (s, v) => { s.subtitles.enabled = v; }));
    if (!sub.enabled) return w;
    w.append(this.sectionTitle('Estilo'));
    w.append(h('div', { class: 'themes' }, this.themes.map((t) => h('div', { class: 'theme' + (t.id === sub.theme ? ' on' : ''), onclick: () => this.mutate((s) => { s.subtitles.theme = t.id; }),
      style: { background: t.box ? '#101014' : '#1b2236', textShadow: t.box ? 'none' : `0 0 3px #${t.outline}, 0 0 3px #${t.outline}, 0 0 3px #${t.outline}` } },
      h('span', { style: { color: '#' + t.primary } }, t.upper ? 'HOLA ' : 'Hola '), h('span', { style: { color: '#' + t.active } }, t.upper ? 'MUNDO' : 'mundo')))));
    w.append(h('div', { class: 'grid2' },
      this.selectf('Animación', () => sub.anim, this.meta.animations.map((a) => [a.id, a.label]), (s, v) => { s.subtitles.anim = v; }),
      this.selectf('Posición', () => sub.position, [['low', 'Abajo'], ['center', 'Centro'], ['top', 'Arriba']], (s, v) => { s.subtitles.position = v; })));
    w.append(this.slider('Tamaño', () => Math.round(sub.size * 100), 60, 180, 5, (s, v) => { s.subtitles.size = v / 100; }, (v) => v + '%'));
    w.append(this.sectionTitle('Sincronía'));
    w.append(this.slider('Desfase de todos los subtítulos', () => sub.offset, -3, 3, 0.05, (s, v) => { s.subtitles.offset = Math.round(v * 100) / 100; }, (v) => (v > 0 ? '+' : '') + Number(v).toFixed(2) + ' s'));
    w.append(h('div', { class: 'hint' }, 'Positivo = los subtítulos salen más tarde. Para ajustes finos usa ‹ › en cada bloque.'));
    w.append(h('div', { class: 'row', style: { margin: '8px 0' } },
      h('button', { class: 'btn g sm', onclick: (e) => this.resync(e.target, false) }, '🎯 Re-sincronizar clip seleccionado'),
      h('button', { class: 'btn g sm', onclick: (e) => this.resync(e.target, true) }, 'Todos los clips')));
    w.append(h('div', { class: 'hint' }, 'Vuelve a escuchar el audio con Whisper y ajusta los tiempos de TUS palabras corregidas al audio real.'));
    w.append(this.sectionTitle('Texto (corrige lo que quieras)'));
    const words = EdCore.timelineWords(sp), chunks = EdCore.buildChunks(words, sp.layout !== 'original');
    const list = h('div', { class: 'chunks' });
    chunks.forEach((ch, ci) => {
      const sel = this.sel && this.sel.type === 'chunk' && this.sel.id === ci;
      const inp = h('input', { type: 'text', value: ch.map((x) => EdCore.clean(x.w)).join(' '), onfocus: () => { this.player.seek(ch[0].s + 0.01); }, onchange: (e) => this.editChunkText(ch, e.target.value) });
      const row = h('div', { class: 'chunk' + (sel ? ' sel' : '') }, h('small', { onclick: () => this.player.seek(ch[0].s + 0.01) }, fmtT(ch[0].s)), inp,
        h('button', { class: 'btn g sm', title: 'Adelantar 0.1 s', onclick: () => this.shiftChunk(ch, -0.1) }, '‹'), h('button', { class: 'btn g sm', title: 'Retrasar 0.1 s', onclick: () => this.shiftChunk(ch, 0.1) }, '›'),
        h('button', { class: 'btn g sm', title: 'Afinar palabra por palabra', onclick: () => { this.sel = { type: 'chunk', id: ci }; this.renderPanel(); } }, '⏱'),
        h('button', { class: 'btn d sm', title: 'Eliminar bloque', onclick: () => this.deleteChunk(ch) }, '✕'));
      list.append(row);
      if (sel) list.append(h('div', { class: 'wtimes' }, ch.map((x) => h('div', { class: 'wt' }, h('span', {}, EdCore.clean(x.w)),
        h('input', { type: 'number', step: 0.01, value: x.s.toFixed(2), title: 'Inicio', onchange: (e) => this.setWordTime(x, 's', parseFloat(e.target.value)) }),
        h('input', { type: 'number', step: 0.01, value: x.e.toFixed(2), title: 'Fin', onchange: (e) => this.setWordTime(x, 'e', parseFloat(e.target.value)) })))));
    });
    w.append(chunks.length ? list : h('div', { class: 'empty' }, 'No hay palabras transcritas en este tramo.'));
    return w;
  },
  tlToSrc(x, key) {   // tiempo de línea de tiempo (con desfase) -> tiempo del video original para una palabra con ref
    const seg = this.spec.segments[x.ref[0]], r = EdCore.timeline(this.spec)[x.ref[0]];
    return seg.src_in + (x[key] - this.spec.subtitles.offset - r.t0);
  },
  setWordTime(x, key, val) {
    if (isNaN(val)) return;
    this.mutate((s) => {
      const seg = s.segments[x.ref[0]], wd = seg.words[x.ref[1]], r = EdCore.timeline(s)[x.ref[0]];
      const src = seg.src_in + (val - s.subtitles.offset - r.t0);
      wd[key] = Math.round(src * 1000) / 1000; if (wd.e <= wd.s) wd.e = wd.s + 0.05;
    });
  },
  shiftChunk(ch, d) {
    this.mutate((s) => ch.forEach((x) => { const wd = s.segments[x.ref[0]].words[x.ref[1]]; wd.s = Math.max(0, Math.round((wd.s + d) * 1000) / 1000); wd.e = Math.round((wd.e + d) * 1000) / 1000; }), { panel: false });
    this.renderPanel();
  },
  deleteChunk(ch) {
    this.mutate((s) => { const by = {}; ch.forEach((x) => { (by[x.ref[0]] = by[x.ref[0]] || []).push(x.ref[1]); });
      Object.entries(by).forEach(([si, idxs]) => idxs.sort((a, b) => b - a).forEach((i) => s.segments[si].words.splice(i, 1))); });
  },
  editChunkText(ch, text) {
    const toks = text.trim().split(/\s+/).filter(Boolean);
    if (!toks.length) return this.deleteChunk(ch);
    const segIdx = new Set(ch.map((x) => x.ref[0]));
    this.mutate((s) => {
      if (toks.length === ch.length) { ch.forEach((x, i) => { s.segments[x.ref[0]].words[x.ref[1]].w = toks[i]; }); return; }
      if (segIdx.size > 1) { toast('Este bloque cruza dos clips: corrige palabra por palabra (mismo número de palabras).', 'err'); return; }
      const si = ch[0].ref[0], seg = s.segments[si], idxs = ch.map((x) => x.ref[1]).sort((a, b) => a - b);
      const t0 = seg.words[idxs[0]].s, t1 = seg.words[idxs[idxs.length - 1]].e, total = toks.reduce((a, t) => a + t.length + 1, 0);
      let cur = t0; const fresh = toks.map((t) => { const d = (t1 - t0) * (t.length + 1) / total; const o = { w: t, s: Math.round(cur * 1000) / 1000, e: Math.round((cur + d) * 1000) / 1000 }; cur += d; return o; });
      seg.words.splice(idxs[0], idxs.length, ...fresh); seg.words.sort((a, b) => a.s - b.s);
    });
  },
  async resync(btn, all) {
    const segs = all ? this.spec.segments : [this.selectedSeg()].filter(Boolean);
    await this.busy(btn, 'Escuchando el audio…', async () => {
      for (const seg of segs) {
        const r = await api(`/api/editor/projects/${this.pid}/resync`, { method: 'POST', body: { seg: seg.uid, spec: this.spec } });
        this.mutate((s) => { s.segments.find((x) => x.uid === seg.uid).words = r.words; }, { panel: false });
      }
      this.renderPanel(); toast('Subtítulos sincronizados con el audio', 'ok');
    });
  },

  // ---------------------------------------------------------------- AUDIO
  panelAudio() {
    const sp = this.spec, m = sp.music, w = h('div', { class: 'pn' });
    w.append(this.sectionTitle('Música de fondo'));
    const pickAsset = (a) => this.mutate((s) => { s.music = { asset: a.id, volume: 0.25, start: 0, loop: true, fade_in: 0.5, fade_out: 1.5, duck: true, offset: 0 }; this.data.assets[a.id] = a; this.sel = { type: 'music' }; });
    w.append(h('div', { class: 'row' }, this.fileButton('⬆ Subir música (mp3, wav, m4a…)', 'audio/*', async (f) => { const a = await this.uploadAsset(f); pickAsset(a); toast('Música añadida', 'ok'); })));
    const prev = h('select', { onchange: (e) => { const a = this.knownAssets.find((x) => x.id === e.target.value); if (a) pickAsset(a); } }, h('option', { value: '' }, 'Elegir una subida anterior…'));
    api('/api/editor/assets').then((list) => { this.knownAssets = list; list.filter((a) => a.kind === 'audio').forEach((a) => prev.append(h('option', { value: a.id }, a.name))); }).catch(() => {});
    w.append(h('div', { class: 'fld' }, prev));
    if (m) {
      w.append(h('div', { class: 'note', style: { margin: '10px 0' } }, '🎵 ' + ((this.data.assets[m.asset] || {}).name || 'Música')));
      w.append(this.slider('Volumen de la música', () => Math.round(m.volume * 100), 0, 100, 1, (s, v) => { s.music.volume = v / 100; }, (v) => v + '%'));
      w.append(this.checkf('Bajar la música automáticamente cuando hablan', () => m.duck, (s, v) => { s.music.duck = v; }));
      w.append(this.checkf('Repetir en bucle', () => m.loop, (s, v) => { s.music.loop = v; }));
      w.append(h('div', { class: 'grid2' }, this.numf('Empieza en (s)', () => m.start, (s, v) => { s.music.start = Math.max(0, v); }), this.numf('Saltar los primeros (s)', () => m.offset, (s, v) => { s.music.offset = Math.max(0, v); })));
      w.append(h('div', { class: 'grid2' }, this.numf('Aparecer (s)', () => m.fade_in, (s, v) => { s.music.fade_in = Math.max(0, v); }), this.numf('Desvanecer al final (s)', () => m.fade_out, (s, v) => { s.music.fade_out = Math.max(0, v); })));
      w.append(h('button', { class: 'btn d sm', onclick: () => { this.sel = null; this.mutate((s) => { s.music = null; }); } }, 'Quitar música'));
    } else w.append(h('div', { class: 'hint' }, 'Sube una canción o pista de fondo. Se mezcla con la voz y se baja sola cuando alguien habla. Usa solo música que tengas derecho a usar.'));
    w.append(this.sectionTitle('Voz de cada clip'));
    w.append(h('div', { class: 'hint' }, 'El volumen de cada clip se ajusta en la pestaña "Clips".'));
    return w;
  },

  // ---------------------------------------------------------------- OVERLAYS
  panelOverlays() {
    const sp = this.spec, w = h('div', { class: 'pn' }), now = () => Math.round(this.player.t * 100) / 100;
    w.append(h('div', { class: 'row' },
      this.fileButton('＋ Imagen / logo', 'image/png,image/jpeg,image/webp,image/gif', async (f) => {
        const a = await this.uploadAsset(f); const id = 'o' + Math.random().toString(36).slice(2, 7);
        this.mutate((s) => { s.overlays.push({ id, type: 'image', asset: a.id, x: 0.5, y: 0.15, w: 0.3, start: 0, end: null, opacity: 1 }); }); this.select({ type: 'ovl', id });
      }),
      h('button', { class: 'btn g sm', onclick: () => { const id = 'o' + Math.random().toString(36).slice(2, 7); const t = now();
        this.mutate((s) => { s.overlays.push({ id, type: 'text', text: 'Tu texto', x: 0.5, y: 0.8, size: 0.05, color: 'FFFFFF', bg: false, start: t, end: Math.round((t + 4) * 100) / 100, opacity: 1 }); }); this.select({ type: 'ovl', id }); } }, '＋ Texto')));
    w.append(h('div', { class: 'hint' }, 'Arrastra el overlay directamente sobre el video para colocarlo. En la línea de tiempo arrastra el bloque o sus bordes para cambiar cuándo aparece.'));
    sp.overlays.forEach((o) => {
      const sel = this.sel && this.sel.type === 'ovl' && this.sel.id === o.id;
      w.append(h('div', { class: 'li' + (sel ? ' sel' : ''), onclick: () => this.select({ type: 'ovl', id: o.id }) }, h('span', { class: 'n' }, o.type === 'image' ? '🖼' : 'T'),
        h('span', { class: 't' }, o.type === 'text' ? o.text : (this.data.assets[o.asset] || {}).name || 'Imagen'), h('span', { class: 'm' }, `${fmtT(o.start)}–${o.end == null ? 'fin' : fmtT(o.end)}`),
        h('button', { class: 'btn d sm', onclick: (e) => { e.stopPropagation(); this.mutate((s) => { s.overlays = s.overlays.filter((x) => x.id !== o.id); }); } }, '✕')));
    });
    const o = this.sel && this.sel.type === 'ovl' ? sp.overlays.find((x) => x.id === this.sel.id) : null;
    if (o) {
      const set = (fn) => (s, v) => fn(s.overlays.find((x) => x.id === o.id), v);
      w.append(this.sectionTitle('Propiedades'));
      if (o.type === 'text') {
        w.append(h('div', { class: 'fld' }, h('label', {}, 'Texto'), h('textarea', { value: o.text, rows: 2, onchange: (e) => this.mutate((s) => { s.overlays.find((x) => x.id === o.id).text = e.target.value.slice(0, 120) || 'Texto'; }) })));
        w.append(this.slider('Tamaño', () => Math.round(o.size * 1000), 15, 150, 1, set((x, v) => { x.size = v / 1000; }), (v) => (v / 10).toFixed(1) + '%'));
        w.append(h('div', { class: 'fld' }, h('label', {}, 'Color'), h('input', { type: 'color', value: '#' + o.color, onchange: (e) => this.mutate((s) => { s.overlays.find((x) => x.id === o.id).color = e.target.value.slice(1).toUpperCase(); }) })));
        w.append(this.checkf('Con caja de fondo', () => o.bg, set((x, v) => { x.bg = v; })));
      } else w.append(this.slider('Ancho', () => Math.round(o.w * 100), 2, 100, 1, set((x, v) => { x.w = v / 100; }), (v) => v + '%'));
      w.append(this.slider('Opacidad', () => Math.round(o.opacity * 100), 0, 100, 1, set((x, v) => { x.opacity = v / 100; }), (v) => v + '%'));
      w.append(h('div', { class: 'grid2' }, this.slider('Posición X', () => Math.round(o.x * 100), 0, 100, 1, set((x, v) => { x.x = v / 100; }), (v) => v + '%'), this.slider('Posición Y', () => Math.round(o.y * 100), 0, 100, 1, set((x, v) => { x.y = v / 100; }), (v) => v + '%')));
      w.append(h('div', { class: 'grid2' }, this.numf('Aparece en (s)', () => o.start, set((x, v) => { x.start = Math.max(0, v); })), this.numf('Desaparece en (s, 0 = hasta el final)', () => o.end || 0, set((x, v) => { x.end = v > x.start ? v : null; }))));
    }
    return w;
  },

  // ---------------------------------------------------------------- ZOOM Y SEGUIMIENTO
  panelZoom() {
    const sp = this.spec, w = h('div', { class: 'pn' }), total = this.total();
    w.append(h('div', { class: 'row' }, h('button', { class: 'btn', onclick: () => this.addZoom() }, '＋ Zoom en el cursor'), h('button', { class: 'btn g sm', onclick: (e) => this.followFace(e.target) }, '👤 Seguir rostro todo el video')));
    w.append(h('div', { class: 'hint' }, 'Un zoom acerca la imagen (y puede seguir a una persona u objeto). Con escala 1× y seguimiento, el video horizontal se reencuadra solo en vertical siguiendo al sujeto.'));
    sp.zooms.forEach((z) => {
      const sel = this.sel && this.sel.type === 'zoom' && this.sel.id === z.id;
      w.append(h('div', { class: 'li' + (sel ? ' sel' : ''), onclick: () => { this.select({ type: 'zoom', id: z.id }); this.player.seek(z.start + 0.01); } }, h('span', { class: 'n' }, '🔍'),
        h('span', { class: 't' }, `${z.scale.toFixed(1)}× · ${{ manual: 'punto fijo', face: 'rostro', track: 'objeto' }[z.focus.mode]}`), h('span', { class: 'm' }, `${fmtT(z.start)}–${fmtT(z.end)}`),
        h('button', { class: 'btn d sm', onclick: (e) => { e.stopPropagation(); this.mutate((s) => { s.zooms = s.zooms.filter((x) => x.id !== z.id); }); } }, '✕')));
    });
    const z = this.sel && this.sel.type === 'zoom' ? sp.zooms.find((x) => x.id === this.sel.id) : null;
    if (z) {
      const set = (fn) => (s, v) => fn(s.zooms.find((x) => x.id === z.id), v);
      w.append(this.sectionTitle('Propiedades del zoom'));
      w.append(this.slider('Acercamiento', () => Math.round(z.scale * 100), 100, 400, 5, set((x, v) => { x.scale = v / 100; }), (v) => (v / 100).toFixed(2) + '×'));
      w.append(this.slider('Suavidad de entrada/salida', () => Math.round(z.ease * 100), 0, 200, 5, set((x, v) => { x.ease = v / 100; }), (v) => (v / 100).toFixed(2) + ' s'));
      w.append(h('div', { class: 'grid2' }, this.numf('Empieza (s)', () => z.start, set((x, v) => { x.start = Math.max(0, v); if (x.end <= x.start) x.end = x.start + 0.5; })), this.numf('Termina (s)', () => z.end, set((x, v) => { x.end = Math.max(x.start + 0.3, v); }))));
      w.append(this.selectf('Qué enfoca', () => z.focus.mode, [['manual', 'Un punto fijo'], ['face', 'Seguir rostro'], ['track', 'Seguir un objeto marcado']], (s, v) => { s.zooms.find((x) => x.id === z.id).focus.mode = v; if (v === 'track' && !s.zooms.find((x) => x.id === z.id).focus.track) { const t = Object.keys(s.tracks).find((k) => s.tracks[k].kind === 'object'); if (t) s.zooms.find((x) => x.id === z.id).focus.track = t; } }));
      if (z.focus.mode === 'manual') {
        w.append(h('div', { class: 'grid2' }, this.slider('Punto X', () => Math.round(z.focus.x * 100), 0, 100, 1, set((x, v) => { x.focus.x = v / 100; }), (v) => v + '%'), this.slider('Punto Y', () => Math.round(z.focus.y * 100), 0, 100, 1, set((x, v) => { x.focus.y = v / 100; }), (v) => v + '%')));
        w.append(h('button', { class: 'btn g sm', onclick: () => this.startPick('point', (p) => this.mutate((s) => { const f = s.zooms.find((x) => x.id === z.id).focus; f.x = Math.round(p.x * 1e3) / 1e3; f.y = Math.round(p.y * 1e3) / 1e3; })) }, '🎯 Elegir el punto sobre el video'));
      } else if (z.focus.mode === 'face') {
        const segs = EdCore.timeline(sp).filter((r) => r.t1 > z.start && r.t0 < z.end).map((r) => r.seg), miss = segs.filter((s) => !sp.tracks['face:' + s.uid]);
        w.append(miss.length ? h('div', { class: 'note warn' }, 'Falta detectar el rostro en ' + miss.length + ' clip(s) de este zoom.') : h('div', { class: 'note' }, '✓ Rostro detectado en todos los clips de este zoom.'));
        if (miss.length) w.append(h('button', { class: 'btn', onclick: (e) => this.busy(e.target, 'Buscando rostros…', async () => { for (const s of miss) if (!(await this.detectFace(s.uid))) break; }) }, '👤 Detectar rostro'));
      } else {
        const objs = Object.entries(sp.tracks).filter(([, t]) => t.kind === 'object');
        w.append(this.selectf('Objeto', () => z.focus.track || '', [['', objs.length ? 'Elige…' : 'Aún no marcaste objetos'], ...objs.map(([k, t]) => [k, t.label || k])], (s, v) => { s.zooms.find((x) => x.id === z.id).focus.track = v || undefined; }));
        w.append(h('button', { class: 'btn', onclick: () => this.markObject(z.id) }, '🎯 Marcar un objeto nuevo en el video'));
      }
    }
    const tr = Object.entries(sp.tracks);
    if (tr.length) {
      w.append(this.sectionTitle('Seguimientos guardados'));
      tr.forEach(([id, t]) => w.append(h('div', { class: 'li' }, h('span', { class: 'n' }, t.kind === 'face' ? '👤' : '🎯'), h('span', { class: 't' }, t.label || id), h('span', { class: 'm' }, t.samples.length + ' puntos'),
        h('button', { class: 'btn d sm', onclick: () => this.mutate((s) => { delete s.tracks[id]; s.zooms.forEach((q) => { if (q.focus.track === id) { q.focus.mode = 'manual'; delete q.focus.track; } }); }) }, '✕'))));
    }
    return w;
  },
  addZoom() {
    const t = Math.round(this.player.t * 100) / 100, total = this.total(), id = 'z' + Math.random().toString(36).slice(2, 7);
    this.mutate((s) => { s.zooms.push({ id, start: t, end: Math.min(total, Math.round((t + 3) * 100) / 100), scale: 1.6, ease: 0.4, focus: { mode: 'manual', x: 0.5, y: 0.4 } }); s.zooms.sort((a, b) => a.start - b.start); });
    this.select({ type: 'zoom', id });
  },
  async detectFace(segUid) {
    try {
      const r = await api(`/api/editor/projects/${this.pid}/track`, { method: 'POST', body: { kind: 'face', seg: segUid, spec: this.spec } });
      this.mutate((s) => { s.tracks[r.id] = r.track; }); toast(`Rostro detectado en el ${Math.round(r.track.detected * 100)}% del clip`, 'ok'); return true;
    } catch (e) { toast(e.message, 'err'); return false; }
  },
  async followFace(btn) {
    await this.busy(btn, 'Buscando rostros…', async () => {
      for (const s of this.spec.segments) if (!this.spec.tracks['face:' + s.uid] && !(await this.detectFace(s.uid))) return;
      const id = 'z' + Math.random().toString(36).slice(2, 7), total = this.total();
      this.mutate((sp) => { sp.zooms = sp.zooms.filter((z) => !(z.focus.mode === 'face' && z.start <= 0.01 && z.end >= total - 0.01)); sp.zooms.push({ id, start: 0, end: total, scale: 1.0, ease: 0.6, focus: { mode: 'face' } }); sp.zooms.sort((a, b) => a.start - b.start); });
      this.select({ type: 'zoom', id }); toast('El encuadre seguirá al rostro durante todo el video', 'ok');
    });
  },

  // ---------------------------------------------------------------- marcar puntos / objetos sobre el video
  startPick(kind, done) {
    this.player.pause(); this.renderTransport(); this.player.mode = 'pick';
    const pick = this.player.pick; pick.replaceChildren(); let rect = null, box = null;
    const norm = (e) => {
      const sr = this.stage.getBoundingClientRect(), g = this.player.lastGeom || { ox: 0, oy: 0, ow: sr.width, oh: sr.height };
      return { x: EdCore.clamp((e.clientX - sr.left - g.ox) / g.ow, 0, 1), y: EdCore.clamp((e.clientY - sr.top - g.oy) / g.oh, 0, 1) };
    };
    const end = () => { this.player.mode = 'normal'; pick.onpointerdown = null; pick.replaceChildren(); this.banner.style.display = 'none'; this.waitForProxies(); };
    const msg = kind === 'point' ? 'Haz clic en el punto que quieres enfocar.' : 'Dibuja un recuadro sobre el objeto que quieres seguir (en este instante del video).';
    this.banner.style.display = 'flex'; this.banner.replaceChildren(h('b', {}, '🎯 ' + msg), h('button', { class: 'btn g sm', onclick: end }, 'Cancelar'));
    pick.onpointerdown = (e) => {
      e.preventDefault(); const a = norm(e);
      if (kind === 'point') { end(); done(a); return; }
      const sr = this.stage.getBoundingClientRect(); rect = h('div', { class: 'ed-rect' }); pick.replaceChildren(rect);
      const mv = (ev) => { const b = norm(ev); box = { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y) };
        const g = this.player.lastGeom; Object.assign(rect.style, { left: g.ox + box.x * g.ow + 'px', top: g.oy + box.y * g.oh + 'px', width: box.w * g.ow + 'px', height: box.h * g.oh + 'px' }); };
      const up = () => { document.removeEventListener('pointermove', mv); document.removeEventListener('pointerup', up);
        if (!box || box.w < 0.015 || box.h < 0.015) { toast('Dibuja un recuadro un poco más grande.', 'err'); return; }
        this.banner.replaceChildren(h('b', {}, 'Objeto marcado.'), h('button', { class: 'btn sm', onclick: () => { end(); done(box); } }, '▶ Seguir este objeto'), h('button', { class: 'btn g sm', onclick: () => { pick.replaceChildren(); box = null; } }, 'Repetir'), h('button', { class: 'btn g sm', onclick: end }, 'Cancelar')); };
      document.addEventListener('pointermove', mv); document.addEventListener('pointerup', up);
    };
  },
  markObject(zoomId) {
    const r = EdCore.locate(this.spec, this.player.t); if (!r) return;
    const seg = r.seg, srcT = seg.src_in + (this.player.t - r.t0);
    this.startPick('object', async (box) => {
      this.banner.style.display = 'flex'; this.banner.replaceChildren(h('span', { class: 'spin' }), ' Siguiendo el objeto…');
      try {
        const label = 'Objeto ' + (Object.values(this.spec.tracks).filter((t) => t.kind === 'object').length + 1);
        const res = await api(`/api/editor/projects/${this.pid}/track`, { method: 'POST', body: { kind: 'object', seg: seg.uid, t: srcT, box, label, spec: this.spec } });
        this.mutate((s) => { s.tracks[res.id] = res.track; if (zoomId) { const z = s.zooms.find((x) => x.id === zoomId); if (z) { z.focus.mode = 'track'; z.focus.track = res.id; } } });
        toast(`Seguimiento listo (confianza ${Math.round(res.track.confidence * 100)}%)`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
      this.banner.style.display = 'none';
    });
  },
});
