'use strict';
/* Editor de video integrado: proyectos, línea de tiempo, historial y exportación. Los paneles están en editor-panels.js */

const fmtT = (s) => { s = Math.max(0, s); const m = Math.floor(s / 60); return m + ':' + (s % 60).toFixed(1).padStart(4, '0'); };

/* ------------------------------------------------------------------ lista de proyectos del editor */
async function pageEditorList() {
  const list = await api('/api/editor/projects');
  const grid = h('div', { class: 'clips' });
  const draw = (items) => grid.replaceChildren(...(items.length ? items.map((p) => h('div', { class: 'clip', onclick: () => { location.hash = '#/editor/' + p.id; } },
    h('div', { class: 'th' }, p.cover ? h('img', { src: `/api/clips/${p.cover}/thumb`, loading: 'lazy' }) : '✂️',
      h('span', { class: 'dur' }, fmtDur(p.duration))),
    h('div', { class: 'b' }, h('div', {}, p.name), h('div', { class: 'm', style: { fontWeight: 400 } }, `${p.segments} clip${p.segments === 1 ? '' : 's'} · ${fmtDate(p.updated_at)}`),
      h('div', { class: 'row', style: { marginTop: '8px' } },
        h('button', { class: 'btn g sm', onclick: async (e) => { e.stopPropagation(); if (confirm('¿Borrar este proyecto del editor? (el clip exportado se conserva)')) { await api('/api/editor/projects/' + p.id, { method: 'DELETE' }); draw(items.filter((x) => x.id !== p.id)); } } }, 'Borrar'))))) :
    [h('div', { class: 'empty', style: { gridColumn: '1/-1' } }, 'Aún no tienes proyectos. Ve a la Biblioteca, selecciona uno o varios clips y pulsa "Abrir en el editor".')]));
  draw(list);
  return h('div', {}, h('h1', {}, 'Editor'), h('p', { class: 'sub' }, 'Corta, sincroniza subtítulos, añade música y overlays, haz zoom y seguimiento. Todo con vista previa en tiempo real.'),
    h('div', { class: 'row', style: { marginBottom: '16px' } }, h('a', { class: 'btn', href: '#/biblioteca' }, '🎬 Elegir clips en la Biblioteca')), grid);
}
async function openInEditor(clipIds) {
  const r = await api('/api/editor/projects', { method: 'POST', body: { clip_ids: clipIds } });
  location.hash = '#/editor/' + r.id;
}
function pageEditor(q) { return q.sub ? pageEditorOpen(parseInt(q.sub, 10)) : pageEditorList(); }

async function pageEditorOpen(pid) {
  const [proj, meta] = await Promise.all([api('/api/editor/projects/' + pid), api('/api/editor/meta')]);
  const ed = new Editor(pid, proj, meta);
  setTimeout(() => ed.mount(), 0);
  window.__ed = ed; window.__edCleanup = () => ed.dispose();
  return ed.root;
}

/* ------------------------------------------------------------------ Editor */
class Editor {
  constructor(pid, proj, meta) {
    this.pid = pid; this.name = proj.name; this.spec = proj.spec;
    this.data = { proxies: proj.proxies, assets: proj.assets || {}, clips: proj.clips || {} };
    this.meta = meta; this.prepError = proj.prep_error || '';
    this.tab = 'clips'; this.sel = null; this.pps = 40; this.history = [JSON.stringify(this.spec)]; this.hi = 0;
    this.saveState = 'saved'; this.saveTimer = null; this.disposed = false; this.totalCache = 0;
    this.keyHandler = (e) => this.onKey(e);
    this.build();
  }
  get themes() { return this.meta.themes; }
  total() { return EdCore.totalDuration(this.spec); }
  selectedSeg() {
    if (this.sel && this.sel.type === 'seg') { const s = this.spec.segments.find((x) => x.uid === this.sel.id); if (s) return s; }
    const r = EdCore.locate(this.spec, this.player ? this.player.t : 0); return r ? r.seg : null;
  }

  // ---------------------------------------------------------------- estructura
  build() {
    this.topEl = h('div', { class: 'ed-top' });
    this.tabsEl = h('div', { class: 'ed-tabs' });
    this.panelEl = h('div', { class: 'ed-panel' });
    this.stage = h('div', { class: 'ed-stage' });
    this.stageBox = h('div', { class: 'ed-stagebox' }, this.stage);
    this.transport = h('div', { class: 'ed-transport' });
    this.banner = h('div', { class: 'ed-banner', style: { display: 'none' } });
    this.tlLabels = h('div', { class: 'tl-labels' });
    this.tlInner = h('div', { class: 'tl-inner' });
    this.tlScroll = h('div', { class: 'tl-scroll' }, this.tlInner);
    this.root = h('div', { class: 'ed' },
      this.topEl,
      h('div', { class: 'ed-mid' }, h('div', { class: 'ed-left' }, this.tabsEl, this.panelEl),
        h('div', { class: 'ed-center' }, this.banner, this.stageBox, this.transport)),
      h('div', { class: 'ed-tl' }, this.tlLabels, this.tlScroll));
  }
  mount() {
    if (this.disposed) return;
    document.getElementById('page').classList.add('wide');
    this.player = new EdPlayer(this.stage, {
      pid: this.pid, spec: () => this.spec, meta: () => this.data, themes: () => this.themes,
      onTime: (t, total) => this.onTime(t, total), onEnd: () => this.renderTransport(),
      onSelectOverlay: (id) => this.select({ type: 'ovl', id }), selectedOverlay: () => (this.sel && this.sel.type === 'ovl' ? this.sel.id : null),
      onMoveOverlay: (id, x, y, done) => this.mutate((s) => { const o = s.overlays.find((v) => v.id === id); if (o) { o.x = Math.round(x * 1e4) / 1e4; o.y = Math.round(y * 1e4) / 1e4; } }, { commit: done, panel: done }),
    });
    document.addEventListener('keydown', this.keyHandler);
    this.layoutStage();
    this._ro = new ResizeObserver(() => this.layoutStage()); this._ro.observe(this.stageBox);
    this.renderAll();
    this.waitForProxies();
    this.tlScroll.addEventListener('wheel', (e) => { if (e.ctrlKey) { e.preventDefault(); this.setZoom(this.pps * (e.deltaY < 0 ? 1.15 : 0.87)); } }, { passive: false });
  }
  dispose() {
    this.disposed = true; clearTimeout(this.saveTimer); clearInterval(this.proxyTimer);
    this.flushSave(); document.removeEventListener('keydown', this.keyHandler);
    if (this._ro) this._ro.disconnect(); if (this.player) this.player.dispose();
    const pg = document.getElementById('page'); if (pg) pg.classList.remove('wide');
  }
  layoutStage() {
    const a = this.stageBox.clientHeight, w = this.stageBox.clientWidth; if (!a) return;
    const asp = this.player.canvasAspect();
    let hh = a, ww = hh * asp; if (ww > w) { ww = w; hh = ww / asp; }
    this.stage.style.width = ww + 'px'; this.stage.style.height = hh + 'px';
  }

  // ---------------------------------------------------------------- proxies
  waitForProxies() {
    const pending = () => this.spec.segments.some((s) => !(this.data.proxies[s.clip_id] || {}).ready);
    const upd = () => { this.banner.style.display = pending() ? 'flex' : 'none'; };
    this.banner.replaceChildren(h('span', { class: 'spin' }), ' Preparando la vista previa de tus clips…');
    upd();
    if (!pending()) return;
    this.proxyTimer = setInterval(async () => {
      try {
        const p = await api('/api/editor/projects/' + this.pid);
        this.data.proxies = p.proxies; this.prepError = p.prep_error;
        if (p.prep_error) this.banner.replaceChildren('⚠ ' + p.prep_error);
        if (!pending()) { clearInterval(this.proxyTimer); upd(); this.player.changed(); this.layoutStage(); this.renderAll(); this.player.seek(this.player.t); }
      } catch (e) { /* reintenta */ }
    }, 1500);
  }

  // ---------------------------------------------------------------- estado / historial / guardado
  mutate(fn, o = {}) {
    const { commit = true, panel = true, timeline = true } = o;
    fn(this.spec);
    this.player && this.player.changed();
    if (commit) this.pushHistory();
    this.markDirty();
    if (timeline) this.renderTimeline();
    if (panel) this.renderPanel();
    this.renderTransport();
    if (this.player && o.relayout) this.layoutStage();
  }
  pushHistory() {
    const snap = JSON.stringify(this.spec);
    if (snap === this.history[this.hi]) return;
    this.history = this.history.slice(0, this.hi + 1); this.history.push(snap); if (this.history.length > 80) this.history.shift(); this.hi = this.history.length - 1;
    this.renderTop();
  }
  undo() { if (this.hi > 0) { this.hi--; this.restore(); } }
  redo() { if (this.hi < this.history.length - 1) { this.hi++; this.restore(); } }
  restore() { this.spec = JSON.parse(this.history[this.hi]); this.player.changed(); this.markDirty(); this.renderAll(); this.layoutStage(); }
  markDirty() { this.saveState = 'dirty'; clearTimeout(this.saveTimer); this.saveTimer = setTimeout(() => this.flushSave(), 1200); this.renderTop(true); }
  async flushSave() {
    clearTimeout(this.saveTimer);
    if (this.saveState !== 'dirty') return;
    this.saveState = 'saving'; this.renderTop(true);
    try { await api('/api/editor/projects/' + this.pid, { method: 'PUT', body: { spec: this.spec, name: this.name } }); this.saveState = this.saveState === 'saving' ? 'saved' : this.saveState; }
    catch (e) { this.saveState = 'error'; }
    this.renderTop(true);
  }
  select(sel) { this.sel = sel; this.renderTimeline(); if (sel && sel.type !== 'seg' && sel.type !== 'chunk') this.setTab(this.tabFor(sel.type)); else this.renderPanel(); }
  tabFor(type) { return { ovl: 'overlays', zoom: 'zoom', music: 'audio', chunk: 'subs', seg: 'clips' }[type] || this.tab; }
  setTab(t) { this.tab = t; this.renderTabs(); this.renderPanel(); }
  setZoom(pps) { this.pps = EdCore.clamp(pps, 8, 240); this.renderTimeline(); }

  // ---------------------------------------------------------------- render general
  renderAll() { this.renderTop(); this.renderTabs(); this.renderPanel(); this.renderTimeline(); this.renderTransport(); }
  renderTop(light) {
    const st = { saved: '✓ Guardado', dirty: '● Cambios sin guardar', saving: 'Guardando…', error: '⚠ Error al guardar' }[this.saveState];
    if (light && this.stEl) { this.stEl.textContent = st; this.undoB.disabled = this.hi <= 0; this.redoB.disabled = this.hi >= this.history.length - 1; return; }
    this.stEl = h('span', { class: 'ed-st' }, st);
    const name = h('input', { type: 'text', value: this.name, class: 'ed-name', onchange: (e) => { this.name = e.target.value.trim() || this.name; this.saveState = 'dirty'; this.flushSave(); } });
    this.undoB = h('button', { class: 'btn g sm', title: 'Deshacer (Ctrl+Z)', disabled: this.hi <= 0, onclick: () => this.undo() }, '↶');
    this.redoB = h('button', { class: 'btn g sm', title: 'Rehacer (Ctrl+Y)', disabled: this.hi >= this.history.length - 1, onclick: () => this.redo() }, '↷');
    this.topEl.replaceChildren(h('a', { class: 'btn g sm', href: '#/editor' }, '← Proyectos'), name, this.stEl, h('span', { style: { flex: 1 } }), this.undoB, this.redoB,
      h('button', { class: 'btn g', onclick: () => this.exportProject('draft') }, '👁 Vista previa exacta'),
      h('button', { class: 'btn', onclick: () => this.exportProject('final') }, '⬆ Exportar a la Biblioteca'));
  }
  renderTabs() {
    const tabs = [['clips', '🎞 Clips'], ['subs', '💬 Subtítulos'], ['audio', '🎵 Audio'], ['overlays', '🖼 Overlays'], ['zoom', '🔍 Zoom y seguimiento']];
    this.tabsEl.replaceChildren(...tabs.map(([k, t]) => h('button', { class: this.tab === k ? 'on' : '', onclick: () => this.setTab(k) }, t)));
  }
  renderPanel() {
    if (!this.panelEl) return;
    const scroll = this.panelEl.scrollTop;
    const fn = { clips: this.panelClips, subs: this.panelSubs, audio: this.panelAudio, overlays: this.panelOverlays, zoom: this.panelZoom }[this.tab];
    this.panelEl.replaceChildren(fn.call(this));
    this.panelEl.scrollTop = scroll;
  }
  renderTransport() {
    const total = this.total();
    this.timeEl = this.timeEl || h('span', { class: 'ed-time' });
    const playing = this.player && this.player.playing;
    this.transport.replaceChildren(
      h('button', { class: 'btn g sm', title: 'Inicio', onclick: () => this.player.seek(0) }, '⏮'),
      h('button', { class: 'btn', style: { minWidth: '54px' }, title: 'Reproducir / pausar (Espacio)', onclick: () => { this.player.toggle(); this.renderTransport(); } }, playing ? '⏸' : '▶'),
      this.timeEl, h('span', { class: 'm' }, ' / ' + fmtT(total)),
      h('button', { class: 'btn g sm', title: 'Dividir el clip en el cursor (S)', onclick: () => this.splitAtCursor() }, '✂ Dividir'),
      h('span', { style: { flex: 1 } }),
      h('label', { class: 'm', style: { display: 'flex', gap: '6px', alignItems: 'center', margin: 0 } }, 'Zoom línea',
        h('input', { type: 'range', min: 8, max: 240, value: this.pps, style: { width: '110px' }, oninput: (e) => this.setZoom(+e.target.value) })));
  }
  onTime(t, total) {
    if (this.timeEl) this.timeEl.textContent = fmtT(t);
    if (this._lastPlaying !== this.player.playing) { this._lastPlaying = this.player.playing; this.renderTransport(); }
    if (this.ph) {
      this.ph.style.left = (t * this.pps) + 'px';
      if (this.player.playing) { const x = t * this.pps, sl = this.tlScroll.scrollLeft, w = this.tlScroll.clientWidth; if (x > sl + w - 60 || x < sl) this.tlScroll.scrollLeft = Math.max(0, x - 80); }
    }
    this.totalCache = total;
  }

  // ---------------------------------------------------------------- línea de tiempo
  renderTimeline() {
    if (!this.tlInner) return;
    const sp = this.spec, total = this.total(), pps = this.pps;
    const lanes = (items) => { const ends = []; return items.map((it) => { let l = ends.findIndex((e) => e <= it.a + 0.01); if (l < 0) { l = ends.length; ends.push(0); } ends[l] = it.b; return l; }); };
    const ovItems = sp.overlays.map((o) => ({ a: o.start, b: o.end == null ? total : o.end }));
    const ovLane = lanes(ovItems), nOv = Math.max(1, ovLane.length ? Math.max(...ovLane) + 1 : 1);
    const zItems = sp.zooms.map((z) => ({ a: z.start, b: z.end })), zLane = lanes(zItems), nZ = Math.max(1, zLane.length ? Math.max(...zLane) + 1 : 1);
    const T = { ruler: 0, vid: 30, sub: 94, mus: 126, ov: 158 }; T.zoom = T.ov + nOv * 28 + 4; const height = T.zoom + nZ * 28 + 10;
    const width = Math.max(this.tlScroll.clientWidth || 600, (total + 8) * pps);
    this.tlInner.style.width = width + 'px'; this.tlInner.style.height = height + 'px';
    this.tlScroll.style.height = height + 'px'; this.tlLabels.style.height = height + 'px';
    const kids = [];
    // regla
    const step = pps >= 100 ? 1 : pps >= 50 ? 2 : pps >= 25 ? 5 : pps >= 12 ? 10 : 30;
    const ruler = h('div', { class: 'tl-ruler', onpointerdown: (e) => this.scrub(e) });
    for (let s = 0; s * 1 <= width / pps; s += step) ruler.append(h('div', { class: 'tick', style: { left: s * pps + 'px' } }, h('span', {}, fmtDur(s))));
    kids.push(ruler);
    const block = (cls, a, b, top, hgt, label, extra = {}) => h('div', { class: 'tl-b ' + cls, style: { left: a * pps + 'px', width: Math.max(6, (b - a) * pps) + 'px', top: top + 'px', height: hgt + 'px' }, ...extra }, label);
    // video
    sp.segments.forEach((seg, i) => {
      const r = EdCore.timeline(sp)[i];
      const selected = this.sel && this.sel.type === 'seg' && this.sel.id === seg.uid;
      const b = block('seg' + (selected ? ' sel' : ''), r.t0, r.t1, T.vid, 58, h('span', { class: 'lbl' }, (this.data.clips[seg.clip_id] || {}).title || seg.title || 'Clip'));
      b.append(h('i', { class: 'hl' }), h('i', { class: 'hr' }));
      b.addEventListener('pointerdown', (e) => this.dragSeg(e, seg));
      kids.push(b);
    });
    // subtítulos
    if (sp.subtitles.enabled) {
      const chunks = EdCore.buildChunks(EdCore.timelineWords(sp), sp.layout !== 'original');
      chunks.forEach((ch, ci) => {
        const a = ch[0].s, bb = ch[ch.length - 1].e;
        const selected = this.sel && this.sel.type === 'chunk' && this.sel.id === ci;
        const b = block('sub' + (selected ? ' sel' : ''), a, bb, T.sub, 24, h('span', { class: 'lbl' }, ch.map((w) => w.w).join(' ')));
        b.addEventListener('pointerdown', (e) => { e.stopPropagation(); this.sel = { type: 'chunk', id: ci }; this.player.seek(a + 0.01); this.setTab('subs'); this.renderTimeline(); });
        kids.push(b);
      });
    }
    // música
    if (sp.music) {
      const a = (this.data.assets[sp.music.asset] || {}).name || 'Música';
      const selected = this.sel && this.sel.type === 'music';
      const b = block('mus' + (selected ? ' sel' : ''), sp.music.start, Math.max(sp.music.start + 0.5, total), T.mus, 24, h('span', { class: 'lbl' }, '🎵 ' + a));
      b.addEventListener('pointerdown', (e) => this.dragTimes(e, { kind: 'music' }));
      kids.push(b);
    }
    // overlays
    sp.overlays.forEach((o, i) => {
      const selected = this.sel && this.sel.type === 'ovl' && this.sel.id === o.id;
      const b = block('ovl' + (selected ? ' sel' : ''), o.start, o.end == null ? total : o.end, T.ov + ovLane[i] * 28, 24, h('span', { class: 'lbl' }, (o.type === 'image' ? '🖼 ' : 'T ') + (o.type === 'text' ? o.text : (this.data.assets[o.asset] || {}).name || 'Imagen')));
      b.append(h('i', { class: 'hl' }), h('i', { class: 'hr' }));
      b.addEventListener('pointerdown', (e) => this.dragTimes(e, { kind: 'ovl', id: o.id }));
      kids.push(b);
    });
    // zooms
    sp.zooms.forEach((z, i) => {
      const selected = this.sel && this.sel.type === 'zoom' && this.sel.id === z.id;
      const b = block('zm ' + z.focus.mode + (selected ? ' sel' : ''), z.start, z.end, T.zoom + zLane[i] * 28, 24, h('span', { class: 'lbl' }, `🔍 ${z.scale.toFixed(1)}×` + ({ face: ' rostro', track: ' objeto', manual: '' }[z.focus.mode])));
      b.append(h('i', { class: 'hl' }), h('i', { class: 'hr' }));
      b.addEventListener('pointerdown', (e) => this.dragTimes(e, { kind: 'zoom', id: z.id }));
      kids.push(b);
    });
    this.ph = h('div', { class: 'tl-ph', style: { left: (this.player ? this.player.t : 0) * pps + 'px' } }, h('i', {}));
    kids.push(this.ph);
    this.tlInner.replaceChildren(...kids);
    this.tlInner.onpointerdown = (e) => { if (e.target === this.tlInner) { this.select(null); this.scrub(e); } };
    this.tlLabels.replaceChildren(h('div', { style: { height: '30px' } }), ...[['🎞 Video', T.vid, 58], ['💬 Subtítulos', T.sub, 24], ['🎵 Música', T.mus, 24], ['🖼 Overlays', T.ov, nOv * 28 - 4], ['🔍 Zoom', T.zoom, nZ * 28 - 4]]
      .map(([t, top, hh]) => h('div', { class: 'tl-lab', style: { top: top + 'px', height: hh + 'px' } }, t)));
  }
  timeFromEvent(e) { const r = this.tlInner.getBoundingClientRect(); return Math.max(0, (e.clientX - r.left) / this.pps); }
  scrub(e) {
    e.preventDefault();
    const was = this.player.playing; if (was) this.player.pause();
    const go = (ev) => { this.player.seek(EdCore.clamp(this.timeFromEvent(ev), 0, this.total())); this.onTime(this.player.t, this.total()); };
    go(e);
    const up = () => { document.removeEventListener('pointermove', go); document.removeEventListener('pointerup', up); };
    document.addEventListener('pointermove', go); document.addEventListener('pointerup', up);
  }

  /** Arrastre genérico con listeners en document (el DOM de la línea de tiempo se reconstruye mientras se arrastra). */
  drag(e, onMove, onEnd) {
    e.preventDefault(); e.stopPropagation();
    const sx = e.clientX; let moved = false;
    const mv = (ev) => { const dx = (ev.clientX - sx) / this.pps; if (Math.abs(ev.clientX - sx) > 3) moved = true; onMove(dx, ev, moved); };
    const up = (ev) => { document.removeEventListener('pointermove', mv); document.removeEventListener('pointerup', up); onEnd(moved, ev); };
    document.addEventListener('pointermove', mv); document.addEventListener('pointerup', up);
  }
  dragSeg(e, seg) {
    const edge = e.target.classList.contains('hl') ? 'l' : e.target.classList.contains('hr') ? 'r' : 'move';
    const px = this.data.proxies[seg.clip_id] || { ready: false };
    const lo = px.ready ? px.base : 0, hi = px.ready ? px.base + px.duration : 1e9;
    const a0 = seg.src_in, b0 = seg.src_out, idx0 = this.spec.segments.indexOf(seg);
    this.sel = { type: 'seg', id: seg.uid };
    const clickT = this.timeFromEvent(e);
    const tl0 = EdCore.timeline(this.spec).map((r) => (r.t0 + r.t1) / 2);
    let target = idx0;
    this.drag(e, (dx, ev, moved) => {
      if (edge === 'l') this.mutate((s) => { const g = s.segments.find((x) => x.uid === seg.uid); g.src_in = Math.round(EdCore.clamp(a0 + dx, lo, g.src_out - 0.3) * 1000) / 1000; }, { commit: false, panel: false });
      else if (edge === 'r') this.mutate((s) => { const g = s.segments.find((x) => x.uid === seg.uid); g.src_out = Math.round(EdCore.clamp(b0 + dx, g.src_in + 0.3, hi) * 1000) / 1000; }, { commit: false, panel: false });
      else if (moved) {
        const pos = this.timeFromEvent(ev); target = tl0.filter((m, i) => i !== idx0 && m < pos).length;
        this.tlInner.querySelectorAll('.tl-b.seg')[idx0].classList.add('dragging');
      }
    }, (moved) => {
      if (edge === 'move' && moved && target !== idx0) this.mutate((s) => { const [it] = s.segments.splice(idx0, 1); s.segments.splice(target, 0, it); });
      else if (edge !== 'move') this.mutate(() => {});
      else if (!moved) { this.player.seek(clickT); this.renderTimeline(); this.renderPanel(); }
      this.renderTimeline(); this.renderPanel();
    });
  }
  dragTimes(e, what) {
    const edge = e.target.classList.contains('hl') ? 'l' : e.target.classList.contains('hr') ? 'r' : 'move';
    const find = (s) => (what.kind === 'ovl' ? s.overlays.find((o) => o.id === what.id) : what.kind === 'zoom' ? s.zooms.find((z) => z.id === what.id) : null);
    const it0 = what.kind === 'music' ? this.spec.music : find(this.spec); if (!it0) return;
    const a0 = it0.start, b0 = it0.end == null ? this.total() : it0.end;
    this.sel = { type: what.kind === 'ovl' ? 'ovl' : what.kind, id: what.id };
    this.drag(e, (dx) => this.mutate((s) => {
      if (what.kind === 'music') { s.music.start = Math.max(0, Math.round((a0 + dx) * 100) / 100); return; }
      const it = find(s), r = (v) => Math.round(v * 100) / 100;
      if (edge === 'l') it.start = r(EdCore.clamp(a0 + dx, 0, b0 - 0.2));
      else if (edge === 'r') it.end = r(Math.max(a0 + 0.2, b0 + dx));
      else { const len = b0 - a0; it.start = r(Math.max(0, a0 + dx)); if (it.end != null || what.kind === 'zoom') it.end = r(it.start + len); }
    }, { commit: false, panel: false }), (moved) => { this.mutate(() => {}, { panel: false }); this.setTab(this.tabFor(this.sel.type)); });
  }

  // ---------------------------------------------------------------- edición
  splitAtCursor() {
    const r = EdCore.locate(this.spec, this.player.t); if (!r) return;
    const local = this.player.t - r.t0;
    if (local < 0.3 || r.t1 - this.player.t < 0.3) { toast('Coloca el cursor dentro del clip (a más de 0.3 s de los bordes).', 'err'); return; }
    this.mutate((s) => {
      const seg = s.segments[r.idx], cut = Math.round((seg.src_in + local) * 1000) / 1000;
      const second = JSON.parse(JSON.stringify(seg)); second.uid = 's' + Math.random().toString(36).slice(2, 8); second.src_in = cut; seg.src_out = cut;
      s.segments.splice(r.idx + 1, 0, second);
      const ft = s.tracks['face:' + seg.uid]; if (ft) s.tracks['face:' + second.uid] = { ...JSON.parse(JSON.stringify(ft)), seg: second.uid };
      this.sel = { type: 'seg', id: second.uid };
    });
    toast('Clip dividido', 'ok');
  }
  deleteSelected() {
    const s = this.sel; if (!s) return;
    if (s.type === 'seg') { if (this.spec.segments.length <= 1) return toast('El proyecto necesita al menos un clip.', 'err'); this.mutate((sp) => { sp.segments = sp.segments.filter((x) => x.uid !== s.id); }); }
    else if (s.type === 'ovl') this.mutate((sp) => { sp.overlays = sp.overlays.filter((x) => x.id !== s.id); });
    else if (s.type === 'zoom') this.mutate((sp) => { sp.zooms = sp.zooms.filter((x) => x.id !== s.id); });
    else if (s.type === 'music') this.mutate((sp) => { sp.music = null; });
    else return;
    this.sel = null; this.renderAll();
  }
  onKey(e) {
    const tag = (e.target.tagName || '').toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag)) return;
    if (e.code === 'Space') { e.preventDefault(); this.player.toggle(); this.renderTransport(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'y') { e.preventDefault(); this.redo(); }
    else if (e.key === 's' || e.key === 'S') this.splitAtCursor();
    else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); this.deleteSelected(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); this.player.seek(this.player.t - (e.shiftKey ? 1 : 1 / 30)); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); this.player.seek(this.player.t + (e.shiftKey ? 1 : 1 / 30)); }
  }

  // ---------------------------------------------------------------- exportación
  async exportProject(quality) {
    await this.flushSave();
    const modal = h('div', { class: 'modal' });
    const bar = h('i', { style: { width: '0%' } });
    const msg = h('div', { class: 'm' }, 'Preparando…');
    const body = h('div', { class: 'card', style: { maxWidth: '560px', width: '100%' } }, h('h2', {}, quality === 'draft' ? 'Vista previa exacta (render rápido)' : 'Exportando a la Biblioteca'), msg, h('div', { class: 'bar' }, bar));
    modal.append(body); document.body.append(modal);
    let timer = null;
    const close = () => { clearInterval(timer); modal.remove(); };
    const cancelBtn = h('button', { class: 'btn d sm', style: { marginTop: '12px' }, onclick: async () => { await api(`/api/editor/projects/${this.pid}/export/cancel`, { method: 'POST' }); close(); } }, 'Cancelar');
    body.append(cancelBtn);
    try { await api(`/api/editor/projects/${this.pid}/export`, { method: 'POST', body: { spec: this.spec, name: this.name, quality } }); }
    catch (e) { toast(e.message, 'err'); return close(); }
    this.saveState = 'saved'; this.renderTop(true);
    timer = setInterval(async () => {
      try {
        const p = await api('/api/editor/projects/' + this.pid);
        bar.style.width = Math.round(p.progress * 100) + '%'; msg.textContent = p.message;
        if (p.status === 'exporting') return;
        clearInterval(timer); cancelBtn.remove();
        if (p.status === 'error') { msg.textContent = p.message; msg.style.color = 'var(--bad)'; body.append(h('button', { class: 'btn g', onclick: close }, 'Cerrar')); }
        else if (p.status === 'draft') {
          body.replaceChildren(h('h2', {}, 'Vista previa exacta'), h('video', { controls: true, autoplay: true, src: `/api/editor/projects/${this.pid}/draft?v=${Date.now()}`, style: { maxHeight: '70vh', maxWidth: '100%', borderRadius: '12px', background: '#000' } }),
            h('div', { class: 'm' }, 'Así se verá el clip final (calidad reducida para renderizar rápido).'), h('button', { class: 'btn g', style: { marginTop: '10px' }, onclick: close }, 'Cerrar'));
        } else if (p.status === 'done') {
          body.replaceChildren(h('h2', {}, '✅ Exportado'), h('p', { class: 'm' }, 'El clip ya está en tu Biblioteca, dentro de la carpeta de su creador.'),
            h('div', { class: 'row' }, h('a', { class: 'btn', href: `/api/clips/${p.export_clip_id}/download` }, '⬇ Descargar'), h('a', { class: 'btn g', href: '#/biblioteca', onclick: close }, 'Ver en la Biblioteca'), h('button', { class: 'btn g', onclick: close }, 'Seguir editando')));
        } else close();
      } catch (e) { /* reintenta */ }
    }, 900);
  }
}
