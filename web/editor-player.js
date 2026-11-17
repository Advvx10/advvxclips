'use strict';
/* Reproductor de la vista previa del editor: compone en el navegador, en tiempo real, lo mismo que hará ffmpeg al exportar
   (video con zoom/seguimiento, fondo desenfocado, overlays, subtítulos animados y música con atenuación). */
class EdPlayer {
  constructor(stage, ctx) {
    this.stage = stage; this.ctx = ctx;           // ctx: {pid, spec(), meta(), themes(), onTime(t), onEnd(), onSelectOverlay(id), onMoveOverlay(id,x,y,done)}
    this.t = 0; this.playing = false; this.mode = 'normal'; this.disposed = false; this.version = 0;
    this.vids = [this._mkVideo(), this._mkVideo()];
    this.cur = 0; this.loaded = [null, null];    // clip cargado en cada <video>
    this.bg = document.createElement('canvas'); this.bg.width = 48; this.bg.height = 86; this.bg.className = 'ed-bg';
    this.vwrap = h('div', { class: 'ed-vwrap' }, this.vids);
    this.ovl = h('div', { class: 'ed-ovl' });
    this.sub = h('div', { class: 'ed-sub' });
    this.pick = h('div', { class: 'ed-pick', style: { display: 'none' } });
    this.stage.replaceChildren(this.bg, this.vwrap, this.ovl, this.sub, this.pick);
    this.music = new Audio(); this.music.preload = 'auto'; this.musicId = null;
    this.ovEls = new Map(); this.chunkKey = ''; this.words = []; this.chunks = []; this.wordsVersion = -1;
    this._raf = () => { if (!this.disposed) { this.tick(); requestAnimationFrame(this._raf); } };
    requestAnimationFrame(this._raf);
  }
  _mkVideo() {
    const v = document.createElement('video'); v.playsInline = true; v.preload = 'auto'; v.className = 'ed-video'; v.style.display = 'none';
    return v;
  }
  dispose() { this.disposed = true; this.pause(); this.vids.forEach((v) => { v.removeAttribute('src'); v.load(); }); this.music.removeAttribute('src'); }
  get spec() { return this.ctx.spec(); }
  get meta() { return this.ctx.meta(); }
  changed() { this.version++; }

  // ------------------------------------------------------------ medidas del lienzo
  canvasAspect() {
    const sp = this.spec; const first = sp.segments[0]; if (!first) return 9 / 16;
    if (sp.layout !== 'original') return 9 / 16;
    const px = this.meta.proxies[first.clip_id];
    return px && px.ready ? px.src_w / px.src_h : 16 / 9;
  }
  size() { return [this.stage.clientWidth || 1, this.stage.clientHeight || 1]; }

  // ------------------------------------------------------------ transporte
  seek(t, keepPlaying = false) {
    const total = EdCore.totalDuration(this.spec);
    this.t = EdCore.clamp(t, 0, Math.max(0, total - 0.001));
    const was = this.playing && keepPlaying;
    if (!was) this.pauseMedia();
    this.playing = was;
    this.syncVideo(true);
    if (was) this.vids[this.cur].play().catch(() => {});
  }
  play() {
    const total = EdCore.totalDuration(this.spec);
    if (!total) return;
    if (this.t >= total - 0.05) this.t = 0;
    this.playing = true; this.syncVideo(true);
    this.vids[this.cur].play().catch(() => { this.playing = false; });
  }
  pause() { this.playing = false; this.pauseMedia(); }
  pauseMedia() { this.vids.forEach((v) => v.pause()); this.music.pause(); }
  toggle() { this.playing ? this.pause() : this.play(); }

  srcUrl(clipId) { return `/api/editor/projects/${this.ctx.pid}/proxy/${clipId}`; }
  /** Deja listo un <video> con el clip del segmento en el tiempo deseado. Devuelve el índice del <video>. */
  ensureVideo(seg, want, hardSeek) {
    let idx = this.loaded.indexOf(seg.clip_id);
    if (idx < 0) {
      idx = 1 - this.cur;
      const v = this.vids[idx];
      this.loaded[idx] = seg.clip_id; v.src = this.srcUrl(seg.clip_id); v.load();
      v.addEventListener('loadedmetadata', () => { try { v.currentTime = want(); } catch (e) { /* aún no */ } }, { once: true });
    }
    const v = this.vids[idx];
    if (v.readyState >= 1) {
      const target = want();
      if (hardSeek || Math.abs(v.currentTime - target) > 0.25) { try { v.currentTime = target; } catch (e) { /* aún no */ } }
    }
    return idx;
  }
  syncVideo(hard) {
    const r = EdCore.locate(this.spec, this.t); if (!r) return;
    const px = this.meta.proxies[r.seg.clip_id]; if (!px || !px.ready) return;
    const want = () => r.seg.src_in + (this.t - r.t0) - px.base;
    const idx = this.ensureVideo(r.seg, want, hard);
    if (idx !== this.cur) { this.vids[this.cur].pause(); this.cur = idx; if (this.playing) this.vids[idx].play().catch(() => {}); }
    this.vids[this.cur].volume = EdCore.clamp(r.seg.volume, 0, 1);
    // precarga del siguiente tramo (otro clip) para un corte sin parpadeo
    const next = this.spec.segments[r.idx + 1];
    if (next && next.clip_id !== r.seg.clip_id && r.t1 - this.t < 2.5) {
      const pn = this.meta.proxies[next.clip_id];
      if (pn && pn.ready && this.loaded.indexOf(next.clip_id) < 0) {
        const o = 1 - this.cur; this.loaded[o] = next.clip_id; this.vids[o].src = this.srcUrl(next.clip_id); this.vids[o].load();
        this.vids[o].addEventListener('loadedmetadata', () => { try { this.vids[o].currentTime = next.src_in - pn.base; } catch (e) { /* ok */ } }, { once: true });
      }
    }
  }

  // ------------------------------------------------------------ bucle
  tick() {
    const sp = this.spec;
    if (!sp || !sp.segments.length) { this.renderEmpty(); return; }
    const total = EdCore.totalDuration(sp);
    if (this.playing) {
      const r = EdCore.locate(sp, this.t);
      const px = this.meta.proxies[r.seg.clip_id];
      const v = this.vids[this.cur];
      if (px && px.ready && this.loaded[this.cur] === r.seg.clip_id && v.readyState >= 2) {
        const t = r.t0 + (v.currentTime + px.base - r.seg.src_in);
        if (t >= r.t1 - 0.02) {                        // fin del segmento: pasa al siguiente
          const nx = sp.segments[r.idx + 1];
          if (!nx) { this.t = total; this.pause(); this.ctx.onEnd && this.ctx.onEnd(); }
          else { this.t = r.t1 + 0.001; this.syncVideo(true); this.vids[this.cur].play().catch(() => {}); }
        } else this.t = t;
      } else if (px && px.ready) { this.syncVideo(false); }   // esperando a que cargue
    }
    this.syncVideoLight();
    this.renderFrame(total);
    this.ctx.onTime && this.ctx.onTime(this.t, total);
  }
  syncVideoLight() {
    if (this.playing) return;
    const r = EdCore.locate(this.spec, this.t); if (!r) return;
    const px = this.meta.proxies[r.seg.clip_id];
    if (px && px.ready && this.loaded[this.cur] !== r.seg.clip_id) this.syncVideo(true);
  }
  renderEmpty() { this.vids.forEach((v) => { v.style.display = 'none'; }); this.sub.replaceChildren(); this.ovl.replaceChildren(); this.ovEls.clear(); }

  // ------------------------------------------------------------ composición
  renderFrame(total) {
    const sp = this.spec, [W, H] = this.size();
    const r = EdCore.locate(sp, this.t); if (!r) return;
    const px = this.meta.proxies[r.seg.clip_id];
    const v = this.vids[this.cur];
    this.vids.forEach((x, i) => { x.style.display = i === this.cur && this.loaded[i] === r.seg.clip_id ? 'block' : 'none'; });
    this.pick.style.display = this.mode === 'pick' ? 'block' : 'none';
    this.stage.classList.toggle('picking', this.mode === 'pick');
    if (px && px.ready) {
      const iw = px.src_w, ih = px.src_h, local = this.t - r.t0;
      let g;
      if (this.mode === 'pick') {               // modo marcar objeto/foco: video completo sin efectos
        const k = Math.min(W / iw, H / ih); g = { ow: iw * k, oh: ih * k, ox: (W - iw * k) / 2, oy: (H - ih * k) / 2 };
      } else g = EdCore.geometry(sp, r.seg, local, this.t, W, H, iw, ih);
      v.style.width = g.ow + 'px'; v.style.height = g.oh + 'px'; v.style.left = g.ox + 'px'; v.style.top = g.oy + 'px';
      this.lastGeom = g;
      const blurBg = sp.layout === 'blur' && this.mode !== 'pick';
      this.bg.style.display = blurBg ? 'block' : 'none';
      if (blurBg && v.readyState >= 2) {
        try {
          const c = this.bg.getContext('2d'), cw = this.bg.width, ch = this.bg.height, s = Math.max(cw / v.videoWidth, ch / v.videoHeight);
          c.drawImage(v, (cw - v.videoWidth * s) / 2, (ch - v.videoHeight * s) / 2, v.videoWidth * s, v.videoHeight * s);
        } catch (e) { /* frame no disponible aún */ }
      }
    }
    if (this.mode === 'pick') { this.ovl.style.display = 'none'; this.sub.style.display = 'none'; }
    else { this.ovl.style.display = 'block'; this.sub.style.display = 'block'; this.renderOverlays(W, H, total); this.renderSubs(W, H); }
    this.syncMusic(total);
  }

  renderOverlays(W, H, total) {
    const live = new Set();
    for (const o of this.spec.overlays) {
      const end = o.end == null ? total : o.end;
      if (this.t < o.start || this.t > end) continue;
      live.add(o.id);
      let el = this.ovEls.get(o.id);
      if (!el || el.dataset.ver !== String(this.version)) {
        if (el) el.remove();
        el = o.type === 'image' ? h('img', { src: `/api/editor/assets/${o.asset}`, draggable: 'false' }) : h('div', { class: 'ed-text' }, o.text);
        el.classList.add('ed-ov'); el.dataset.id = o.id; el.dataset.ver = String(this.version);
        el.addEventListener('pointerdown', (e) => this.dragOverlay(e, o.id));
        this.ovl.append(el); this.ovEls.set(o.id, el);
      }
      el.style.left = (o.x * 100) + '%'; el.style.top = (o.y * 100) + '%'; el.style.opacity = o.opacity;
      if (o.type === 'image') el.style.width = (o.w * 100) + '%';
      else {
        const fs = o.size * H;
        Object.assign(el.style, { fontSize: fs + 'px', color: '#' + o.color, background: o.bg ? '#101014' : 'transparent', padding: o.bg ? `${fs * 0.14}px ${fs * 0.3}px` : '0',
          textShadow: o.bg ? 'none' : this.outline(Math.max(1, fs * 0.07), '#000') });
      }
      el.classList.toggle('sel', this.ctx.selectedOverlay && this.ctx.selectedOverlay() === o.id);
    }
    for (const [id, el] of this.ovEls) if (!live.has(id)) { el.remove(); this.ovEls.delete(id); }
  }
  dragOverlay(e, id) {
    e.preventDefault(); e.stopPropagation();
    this.ctx.onSelectOverlay && this.ctx.onSelectOverlay(id);
    const rect = this.stage.getBoundingClientRect(), o = this.spec.overlays.find((x) => x.id === id); if (!o) return;
    const sx = e.clientX, sy = e.clientY, ox = o.x, oy = o.y;
    const move = (ev) => this.ctx.onMoveOverlay(id, EdCore.clamp(ox + (ev.clientX - sx) / rect.width, -0.2, 1.2), EdCore.clamp(oy + (ev.clientY - sy) / rect.height, -0.2, 1.2), false);
    const up = (ev) => { document.removeEventListener('pointermove', move); document.removeEventListener('pointerup', up); this.ctx.onMoveOverlay(id, EdCore.clamp(ox + (ev.clientX - sx) / rect.width, -0.2, 1.2), EdCore.clamp(oy + (ev.clientY - sy) / rect.height, -0.2, 1.2), true); };
    document.addEventListener('pointermove', move); document.addEventListener('pointerup', up);
  }
  outline(px, color) {
    const parts = []; const n = 12;
    for (let i = 0; i < n; i++) { const a = (i / n) * Math.PI * 2; parts.push(`${(Math.cos(a) * px).toFixed(1)}px ${(Math.sin(a) * px).toFixed(1)}px 0 ${color}`); }
    return parts.join(',');
  }

  // ------------------------------------------------------------ subtítulos
  renderSubs(W, H) {
    const sp = this.spec, sub = sp.subtitles;
    if (this.wordsVersion !== this.version) { this.words = sub.enabled ? EdCore.timelineWords(sp) : []; this.chunks = EdCore.buildChunks(this.words, sp.layout !== 'original'); this.wordsVersion = this.version; this.chunkKey = ''; }
    const hit = EdCore.chunkAt(this.chunks, this.t);
    if (!hit) { if (this.chunkKey) { this.sub.replaceChildren(); this.chunkKey = ''; } return; }
    const th = (this.ctx.themes().find((x) => x.id === sub.theme)) || this.ctx.themes()[0];
    const vertical = sp.layout !== 'original';
    const fs = th.size * (vertical ? H : H * 1.35) * sub.size;
    const key = `${hit.ci}|${sub.theme}|${sub.anim}|${sub.position}|${sub.size}|${Math.round(H)}|${this.version}`;
    const texts = hit.chunk.map((w) => { const c = EdCore.clean(w.w); return th.upper ? c.toUpperCase() : c; });
    if (key !== this.chunkKey) {                       // bloque nuevo: reconstruye y dispara la animación de entrada
      this.chunkKey = key;
      const brk = EdCore.lineBreak(texts, EdCore.lineLimit(W, fs, th.upper));
      const line = h('div', { class: 'ed-line anim-' + (sub.anim === 'pop' ? 'none' : sub.anim) });
      line.style.setProperty('--pop', String(th.pop > 100 ? th.pop / 100 : 1));
      texts.forEach((tx, j) => { line.append(h('span', { class: 'w' }, tx)); if (j === brk) line.append(h('br')); else if (j < texts.length - 1) line.append(' '); });
      const stroke = th.box ? 0 : Math.max(1, fs * th.outline_k * 0.9);
      Object.assign(line.style, { fontSize: fs + 'px', fontFamily: 'Poppins, sans-serif', fontWeight: 700, color: '#' + th.primary, letterSpacing: '0.5px',
        textShadow: th.box ? 'none' : this.outline(stroke, '#' + th.outline) + ',0 3px 6px rgba(0,0,0,.5)', lineHeight: 1.18, textAlign: 'center' });
      if (th.box) { line.style.background = '#101014'; line.style.padding = `${fs * 0.2}px ${fs * 0.45}px`; line.style.borderRadius = '6px'; line.style.display = 'inline-block'; }
      const pos = { low: { bottom: 0.26 * H + 'px' }, center: { top: '50%', transform: 'translateY(-50%)' }, top: { top: 0.16 * H + 'px' } }[sub.position] || {};
      this.sub.replaceChildren(h('div', { class: 'ed-subwrap', style: pos }, line));
      this.sub.firstChild.style.cssText += ';position:absolute;left:0;right:0;text-align:center;';
      this.lineEl = line; this.lastIdx = -1;
    }
    if (hit.index !== this.lastIdx) {                 // palabra activa nueva
      this.lastIdx = hit.index;
      const spans = this.lineEl.querySelectorAll('.w');
      spans.forEach((sp2, j) => {
        sp2.className = 'w';
        const act = j === hit.index;
        let col = '#' + th.primary;
        if (th.mode === 'progressive') col = act ? '#' + th.active : (j < hit.index ? '#' + th.primary : '#' + th.dim);
        else if (act) col = '#' + th.active;
        sp2.style.color = col;
        if (sub.anim === 'typewriter' && j > hit.index) sp2.style.visibility = 'hidden'; else sp2.style.visibility = 'visible';
        if (act) { void sp2.offsetWidth; sp2.classList.add('act', 'wanim-' + (sub.anim === 'pop' && th.pop <= 100 ? 'none' : sub.anim)); }
      });
    }
  }

  // ------------------------------------------------------------ música (con atenuación bajo la voz)
  syncMusic(total) {
    const m = this.spec.music, a = this.music;
    if (!m) { if (this.musicId) { a.pause(); a.removeAttribute('src'); this.musicId = null; } return; }
    if (this.musicId !== m.asset) { this.musicId = m.asset; a.src = `/api/editor/assets/${m.asset}`; a.load(); }
    a.loop = !!m.loop;
    const pos = this.t - m.start;
    const dur = isFinite(a.duration) && a.duration > 0 ? a.duration : 0;
    let vol = m.volume;
    const mdur = total - m.start;
    if (pos >= 0 && mdur > 0) {
      if (m.fade_in > 0 && pos < m.fade_in) vol *= pos / m.fade_in;
      if (m.fade_out > 0 && pos > mdur - m.fade_out) vol *= Math.max(0, (mdur - pos) / m.fade_out);
    }
    if (m.duck && this.chunks.length && EdCore.chunkAt(this.chunks, this.t)) vol *= 0.35;   // aproximación de la atenuación del render
    a.volume = EdCore.clamp(vol, 0, 1);
    if (!this.playing || pos < 0) { if (!a.paused) a.pause(); return; }
    let want = pos + (m.offset || 0);
    if (dur) want = m.loop ? want % dur : Math.min(want, dur - 0.01);
    if (Math.abs(a.currentTime - want) > 0.3) { try { a.currentTime = want; } catch (e) { /* ok */ } }
    if (a.paused && (!dur || m.loop || want < dur - 0.05)) a.play().catch(() => {});
  }
}
if (typeof window !== 'undefined') window.EdPlayer = EdPlayer;
