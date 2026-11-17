'use strict';
/* Matemática del editor (sin DOM). Debe dar los MISMOS números que app/editor.py y app/captions.py:
   la vista previa en tiempo real y el render final con ffmpeg comparten estas fórmulas. */
const EdCore = (() => {
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const segDur = (s) => Math.max(0.2, s.src_out - s.src_in);

  function timeline(spec) {
    let t = 0;
    return spec.segments.map((seg, idx) => { const d = segDur(seg); const r = { seg, idx, t0: t, t1: t + d }; t += d; return r; });
  }
  const totalDuration = (spec) => spec.segments.reduce((a, s) => a + segDur(s), 0);
  function locate(spec, t) {
    const tl = timeline(spec);
    for (const r of tl) if (t < r.t1) return r;
    return tl.length ? tl[tl.length - 1] : null;
  }
  const clean = (w) => String(w == null ? '' : w).replace(/\\/g, '').replace(/\{/g, '(').replace(/\}/g, ')').replace(/\n/g, ' ').trim();

  /** Palabras en tiempo de línea de tiempo (recortes + desfase). ref = [índice de segmento, índice de palabra]. */
  function timelineWords(spec) {
    const off = spec.subtitles.offset || 0;
    const out = [];
    for (const { seg, idx, t0, t1 } of timeline(spec)) {
      seg.words.forEach((w, wi) => {
        const mid = (w.s + w.e) / 2;
        if (!(seg.src_in <= mid && mid < seg.src_out)) return;
        let s = t0 + Math.max(0, w.s - seg.src_in) + off;
        const e = t0 + Math.min(w.e - seg.src_in, t1 - t0) + off;
        if (e <= 0) return;
        s = Math.max(0, s);
        out.push({ w: w.w, s: Math.round(s * 1000) / 1000, e: Math.round(Math.max(e, s + 0.05) * 1000) / 1000, ref: [idx, wi] });
      });
    }
    return out.sort((a, b) => a.s - b.s);
  }

  // ---- reencuadre
  const smoothstep = (x) => { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); };
  function trackAt(samples, t) {
    if (!samples || !samples.length) return [0.5, 0.5];
    if (t <= samples[0][0]) return [samples[0][1], samples[0][2]];
    const last = samples[samples.length - 1];
    if (t >= last[0]) return [last[1], last[2]];
    let lo = 0, hi = samples.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (samples[mid][0] <= t) lo = mid; else hi = mid; }
    const a = samples[lo], b = samples[hi];
    const f = b[0] > a[0] ? (t - a[0]) / (b[0] - a[0]) : 0;
    return [a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  }
  function focusAt(spec, seg, localT, tlT) {
    let zoom = null;
    for (const z of spec.zooms) if (z.start <= tlT && tlT <= z.end) zoom = z;
    if (!zoom) return [1, 0.5, 0.5];
    const e = Math.max(0.01, Math.min(zoom.ease, (zoom.end - zoom.start) / 2));
    const env = smoothstep((tlT - zoom.start) / e) * smoothstep((zoom.end - tlT) / e);
    const f = zoom.focus;
    let px = f.x, py = f.y;
    if (f.mode === 'face' || f.mode === 'track') {
      const tid = f.mode === 'face' ? 'face:' + seg.uid : (f.track || '');
      const tr = spec.tracks[tid];
      if (tr && (f.mode === 'face' || tr.seg === seg.uid)) [px, py] = trackAt(tr.samples, seg.src_in + localT);
      else { px = 0.5; py = 0.5; }
    }
    return [1 + (zoom.scale - 1) * env, 0.5 + (px - 0.5) * env, 0.5 + (py - 0.5) * env];
  }
  const baseScale = (layout, W, H, iw, ih) => (layout === 'blur' ? Math.min(W / iw, H / ih) : Math.max(W / iw, H / ih));
  const axisOffset = (canvas, size, f) => (size >= canvas ? Math.max(canvas - size, Math.min(0, canvas / 2 - f * size)) : (canvas - size) / 2);
  function geometry(spec, seg, localT, tlT, W, H, iw, ih) {
    const [z, fx, fy] = focusAt(spec, seg, localT, tlT);
    const k = baseScale(spec.layout, W, H, iw, ih);
    const ow = iw * k * z, oh = ih * k * z;
    return { z, fx, fy, ow, oh, ox: axisOffset(W, ow, fx), oy: axisOffset(H, oh, fy) };
  }

  // ---- subtítulos: mismos bloques que captions.build_chunks
  const END = /[.!?…]+["')»”]*$/;
  const SOFT = /[,;:–—]+$/;
  function buildChunks(words, vertical = true) {
    const [maxWords, maxChars] = vertical ? [3, 17] : [6, 38];
    const chunks = [];
    let cur = [];
    for (const w of words) {
      if (!clean(w.w)) continue;
      if (cur.length) {
        const gap = w.s - cur[cur.length - 1].e;
        const txt = cur.concat([w]).map((x) => clean(x.w)).join(' ');
        const last = clean(cur[cur.length - 1].w);
        if (cur.length >= maxWords || txt.length > maxChars || gap > 0.5 || END.test(last) || (SOFT.test(last) && cur.length >= 2)) {
          chunks.push(cur); cur = [];
        }
      }
      cur.push(w);
    }
    if (cur.length) chunks.push(cur);
    return chunks;
  }
  const lineLimit = (width, size, upper) => Math.max(8, Math.floor((width * 0.86) / (size * (upper ? 0.70 : 0.585))));
  function lineBreak(texts, limit) {
    if (texts.length < 2 || texts.join(' ').length <= limit) return -1;
    let best = -1, bd = 1e9;
    for (let i = 0; i < texts.length - 1; i++) {
      const d = Math.abs(texts.slice(0, i + 1).join(' ').length - texts.slice(i + 1).join(' ').length);
      if (d < bd) { best = i; bd = d; }
    }
    return best;
  }
  /** Bloque activo en el instante t: {words, activeIndex, start, end, nextStart} (mismo criterio que el render). */
  function chunkAt(chunks, t) {
    for (let ci = 0; ci < chunks.length; ci++) {
      const ch = chunks[ci];
      const nxt = ci + 1 < chunks.length ? chunks[ci + 1][0].s : null;
      let holdEnd = ch[ch.length - 1].e + 0.28;
      if (nxt !== null) holdEnd = Math.min(holdEnd, nxt);
      for (let i = 0; i < ch.length; i++) {
        const t0 = i ? ch[i].s : Math.max(0, ch[i].s - 0.04);
        let t1 = i + 1 < ch.length ? ch[i + 1].s : holdEnd;
        if (t1 - t0 < 0.05) t1 = t0 + 0.05;
        if (t >= t0 && t < t1) return { chunk: ch, index: i, ci, t0 };
      }
    }
    return null;
  }

  return { clamp, segDur, timeline, totalDuration, locate, clean, timelineWords, smoothstep, trackAt, focusAt, baseScale, axisOffset,
           geometry, buildChunks, lineLimit, lineBreak, chunkAt };
})();
if (typeof module !== 'undefined') module.exports = EdCore;
