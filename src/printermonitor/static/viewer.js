/*
 * PrinterMonitor — 3D preview (STL + G-code).
 *
 * Standalone ES module. Exports `class Viewer` and sets `window.PMViewer = Viewer`.
 *
 * three.js r165 (0.165.0), vendored in ./vendor/three/ (MIT license, see vendor/three/LICENSE):
 *   https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js
 *   https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/controls/OrbitControls.js
 *   https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/loaders/STLLoader.js
 * In OrbitControls.js and STLLoader.js, `from 'three'` was rewritten to
 * `from '../three.module.js'`: no import map is needed.
 *
 * Coordinates: the printer is Z-up (X right, Y back, Z up). three.js is Y-up:
 *   three.x = X - bedX/2,  three.y = Z,  three.z = -(Y - bedY/2)
 */
import * as THREE from './vendor/three/three.module.js';
import { OrbitControls } from './vendor/three/addons/OrbitControls.js';
import { STLLoader } from './vendor/three/addons/STLLoader.js';

/* ------------------------------------------------------------------ */
/* G-code line types                                                    */
/* ------------------------------------------------------------------ */

// Canonical keys (index = code sent by the worker), labels, colors.
const FEATURES = [
  { key: 'other',        label: 'Other',               color: '#cfd6e4' },
  { key: 'outer',        label: 'Outer wall',          color: '#ff8a4c' },
  { key: 'inner',        label: 'Inner wall',          color: '#ffd25c' },
  { key: 'overhang',     label: 'Overhang wall',       color: '#4c8dff' },
  { key: 'sparse',       label: 'Sparse infill',       color: '#c0504d' },
  { key: 'solid',        label: 'Solid infill',        color: '#b05cff' },
  { key: 'top',          label: 'Top surface',         color: '#ff5c7a' },
  { key: 'bottom',       label: 'Bottom surface',      color: '#7aa9ff' },
  { key: 'bridge',       label: 'Bridge',              color: '#4cc3ff' },
  { key: 'gap',          label: 'Gap infill',          color: '#f2f2f2' },
  { key: 'support',      label: 'Support',             color: '#34c38f' },
  { key: 'supportIface', label: 'Support interface',   color: '#1f8f6a' },
  { key: 'skirt',        label: 'Skirt / brim',        color: '#9bd17a' },
  { key: 'prime',        label: 'Prime tower',         color: '#8d7bd6' },
  { key: 'custom',       label: 'Custom',              color: '#7f889a' },
];

// Orca / Bambu / Creality Print, PrusaSlicer, Cura names (lowercase) -> key.
const FEATURE_ALIASES = {
  'outer wall': 'outer', 'external perimeter': 'outer', 'wall-outer': 'outer', 'perimeter outer': 'outer',
  'inner wall': 'inner', 'perimeter': 'inner', 'wall-inner': 'inner',
  'overhang wall': 'overhang', 'overhang perimeter': 'overhang',
  'sparse infill': 'sparse', 'internal infill': 'sparse', 'fill': 'sparse', 'infill': 'sparse',
  'internal solid infill': 'solid', 'solid infill': 'solid',
  'top surface': 'top', 'top solid infill': 'top', 'skin': 'top', 'ironing': 'top',
  'bottom surface': 'bottom',
  'bridge': 'bridge', 'internal bridge': 'bridge', 'bridge infill': 'bridge',
  'gap infill': 'gap', 'gap fill': 'gap',
  'support': 'support', 'support material': 'support', 'support transition': 'support',
  'support interface': 'supportIface', 'support material interface': 'supportIface',
  'skirt': 'skirt', 'brim': 'skirt', 'skirt/brim': 'skirt', 'skirt / brim': 'skirt',
  'prime tower': 'prime', 'wipe tower': 'prime',
  'custom': 'custom',
};

/* ------------------------------------------------------------------ */
/* G-code parsing worker (serialized as a Blob URL)                     */
/* ------------------------------------------------------------------ */

function gcodeWorkerMain() {
  /* eslint-disable no-restricted-globals */
  let ALIASES = {};
  let KEYS = [];
  let PALETTE = null; // Uint8Array rgb per code

  class F32 {
    constructor(n) { this.a = new Float32Array(n); this.n = 0; }
    push6(a, b, c, d, e, f) {
      if (this.n + 6 > this.a.length) { const b2 = new Float32Array(this.a.length * 2); b2.set(this.a); this.a = b2; }
      const A = this.a, i = this.n;
      A[i] = a; A[i + 1] = b; A[i + 2] = c; A[i + 3] = d; A[i + 4] = e; A[i + 5] = f;
      this.n = i + 6;
    }
    out() { return this.a.slice(0, this.n); }
  }
  class U8 {
    constructor(n) { this.a = new Uint8Array(n); this.n = 0; }
    push(v) {
      if (this.n >= this.a.length) { const b2 = new Uint8Array(this.a.length * 2); b2.set(this.a); this.a = b2; }
      this.a[this.n++] = v;
    }
    out() { return this.a.slice(0, this.n); }
  }

  function parse(text, opts) {
    const t0 = performance.now();
    const ox = opts.bedX / 2, oy = opts.bedY / 2;
    const ext = new F32(1 << 18);     // extrusion segments (2 vertices × xyz)
    const extType = new U8(1 << 15);  // type per segment
    const trv = new F32(1 << 16);     // travel moves
    const layerStart = [];            // extrusion segment index at the start of each layer
    const travelLayerStart = [];
    const layerZ = [];
    const typesSeen = new Set();

    let absXYZ = true, absE = true;
    let x = 0, y = 0, z = 0, e = 0;
    let curType = 0;
    let curLayerZ = -Infinity;
    let minX = Infinity, minY = Infinity, minZ = Infinity, maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    let filament = 0, moves = 0;
    const customType = KEYS.indexOf('custom');
    const cb = { minX: Infinity, minY: Infinity, minZ: Infinity, maxX: -Infinity, maxY: -Infinity, maxZ: -Infinity,
      push(a, b, c, d, e, f) {
        this.minX = Math.min(this.minX, a, d); this.maxX = Math.max(this.maxX, a, d);
        this.minY = Math.min(this.minY, b, e); this.maxY = Math.max(this.maxY, b, e);
        this.minZ = Math.min(this.minZ, c, f); this.maxZ = Math.max(this.maxZ, c, f);
      } };

    const len = text.length;
    let pos = 0, lastReport = 0;

    function seg(x0, y0, z0, x1, y1, z1, extruding) {
      if (extruding) {
        if (layerStart.length === 0 || z1 > curLayerZ + 1e-3) {
          curLayerZ = z1;
          layerStart.push(ext.n / 6);
          travelLayerStart.push(trv.n / 6);
          layerZ.push(z1);
        }
        ext.push6(x0 - ox, z0, oy - y0, x1 - ox, z1, oy - y1);
        extType.push(curType);
        typesSeen.add(curType);
        if (curType === customType) {
          // purge lines / start G-code: separate bounds
          cb.push(x0, y0, z0, x1, y1, z1);
          return;
        }
        if (x1 < minX) minX = x1; if (x1 > maxX) maxX = x1;
        if (y1 < minY) minY = y1; if (y1 > maxY) maxY = y1;
        if (z1 < minZ) minZ = z1; if (z1 > maxZ) maxZ = z1;
        if (x0 < minX) minX = x0; if (x0 > maxX) maxX = x0;
        if (y0 < minY) minY = y0; if (y0 > maxY) maxY = y0;
      } else {
        trv.push6(x0 - ox, z0, oy - y0, x1 - ox, z1, oy - y1);
      }
    }

    // Read a line's parameters (comment stripped).
    const P = { X: NaN, Y: NaN, Z: NaN, E: NaN, I: NaN, J: NaN, R: NaN };
    function readParams(line, start) {
      P.X = P.Y = P.Z = P.E = P.I = P.J = P.R = NaN;
      const n = line.length;
      let i = start;
      while (i < n) {
        let c = line.charCodeAt(i);
        if (c >= 97 && c <= 122) c -= 32;
        if (c === 88 || c === 89 || c === 90 || c === 69 || c === 73 || c === 74 || c === 82) {
          let j = i + 1;
          while (j < n) { const d = line.charCodeAt(j); if (d === 32 || d === 9) break; j++; }
          const v = parseFloat(line.substring(i + 1, j));
          P[String.fromCharCode(c)] = v;
          i = j;
        } else i++;
      }
    }

    while (pos < len) {
      let nl = text.indexOf('\n', pos);
      if (nl === -1) nl = len;
      let line = text.substring(pos, nl);
      pos = nl + 1;

      if (pos - lastReport > 524288) { lastReport = pos; postMessage({ kind: 'progress', value: Math.min(pos / len, 1) * 0.95 }); }

      let s = 0;
      while (s < line.length && (line.charCodeAt(s) === 32 || line.charCodeAt(s) === 9)) s++;
      const c0 = line.charCodeAt(s);
      if (c0 === 59) { // ';'
        if (line.startsWith(';TYPE:', s)) {
          const name = line.substring(s + 6).trim().toLowerCase();
          const key = ALIASES[name];
          curType = key ? KEYS.indexOf(key) : 0;
          if (curType < 0) curType = 0;
        }
        continue;
      }
      if (c0 !== 71 && c0 !== 103 && c0 !== 77 && c0 !== 109) continue; // G / M
      const sc = line.indexOf(';', s);
      if (sc !== -1) line = line.substring(0, sc);
      // command number
      let k = s + 1, num = 0, hasNum = false;
      while (k < line.length) { const d = line.charCodeAt(k); if (d < 48 || d > 57) break; num = num * 10 + (d - 48); hasNum = true; k++; }
      if (!hasNum) continue;
      if (line.charCodeAt(k) === 46) continue; // G29.1 etc.: ignored
      const isG = c0 === 71 || c0 === 103;

      if (isG) {
        if (num === 0 || num === 1 || num === 2 || num === 3) {
          readParams(line, k);
          let nx = x, ny = y, nz = z;
          if (!isNaN(P.X)) nx = absXYZ ? P.X : x + P.X;
          if (!isNaN(P.Y)) ny = absXYZ ? P.Y : y + P.Y;
          if (!isNaN(P.Z)) nz = absXYZ ? P.Z : z + P.Z;
          let de = 0;
          if (!isNaN(P.E)) { if (absE) { de = P.E - e; e = P.E; } else { de = P.E; e += P.E; } }
          const moved = nx !== x || ny !== y || nz !== z;
          if (moved) {
            moves++;
            const extruding = de > 1e-6 && (nx !== x || ny !== y);
            if (extruding) filament += de;
            if ((num === 2 || num === 3) && (!isNaN(P.I) || !isNaN(P.J))) {
              // Arc: relative center (I, J), split into ~1 mm segments.
              const cx = x + (isNaN(P.I) ? 0 : P.I), cy = y + (isNaN(P.J) ? 0 : P.J);
              const r = Math.hypot(x - cx, y - cy);
              const a0 = Math.atan2(y - cy, x - cx);
              let a1 = Math.atan2(ny - cy, nx - cx);
              let sweep = a1 - a0;
              if (num === 2) { if (sweep >= -1e-9) sweep -= 2 * Math.PI; }
              else { if (sweep <= 1e-9) sweep += 2 * Math.PI; }
              const n = Math.max(1, Math.min(128, Math.ceil(Math.abs(sweep) * r / 1.0)));
              let px = x, py = y, pz = z;
              for (let i = 1; i <= n; i++) {
                const t = i / n;
                let qx, qy;
                if (i === n) { qx = nx; qy = ny; }
                else { const a = a0 + sweep * t; qx = cx + r * Math.cos(a); qy = cy + r * Math.sin(a); }
                const qz = z + (nz - z) * t;
                seg(px, py, pz, qx, qy, qz, extruding);
                px = qx; py = qy; pz = qz;
              }
            } else {
              seg(x, y, z, nx, ny, nz, extruding);
            }
            x = nx; y = ny; z = nz;
          }
        } else if (num === 90) absXYZ = true;
        else if (num === 91) absXYZ = false;
        else if (num === 92) {
          readParams(line, k);
          if (!isNaN(P.X)) x = P.X; if (!isNaN(P.Y)) y = P.Y; if (!isNaN(P.Z)) z = P.Z;
          if (!isNaN(P.E)) e = P.E;
          if (isNaN(P.X) && isNaN(P.Y) && isNaN(P.Z) && isNaN(P.E)) e = 0;
        } else if (num === 28) {
          readParams(line, k);
          const all = isNaN(P.X) && isNaN(P.Y) && isNaN(P.Z);
          if (all || !isNaN(P.X)) x = 0; if (all || !isNaN(P.Y)) y = 0; if (all || !isNaN(P.Z)) z = 0;
        }
      } else {
        if (num === 82) absE = true;
        else if (num === 83) absE = false;
      }
    }

    // Colors
    const nSeg = ext.n / 6;
    const types = extType.out();
    const colors = new Uint8Array(nSeg * 6);
    const hasTypes = [...typesSeen].some((t) => t !== 0);
    const nLayers = layerStart.length;
    let layer = 0;
    for (let i = 0; i < nSeg; i++) {
      while (layer + 1 < nLayers && layerStart[layer + 1] <= i) layer++;
      let r, g, b;
      if (hasTypes) {
        const t = types[i] * 3; r = PALETTE[t]; g = PALETTE[t + 1]; b = PALETTE[t + 2];
      } else {
        // height gradient: blue -> cyan -> green -> yellow -> orange
        const h = nLayers > 1 ? layer / (nLayers - 1) : 0;
        const stops = [[76, 141, 255], [76, 195, 255], [52, 195, 143], [240, 210, 80], [255, 138, 76]];
        const f = h * (stops.length - 1), i0 = Math.min(stops.length - 2, Math.floor(f)), u = f - i0;
        r = stops[i0][0] + (stops[i0 + 1][0] - stops[i0][0]) * u;
        g = stops[i0][1] + (stops[i0 + 1][1] - stops[i0][1]) * u;
        b = stops[i0][2] + (stops[i0 + 1][2] - stops[i0][2]) * u;
        // sRGB -> linear (three.js treats vertex colors as linear)
        r = 255 * Math.pow(r / 255, 2.2); g = 255 * Math.pow(g / 255, 2.2); b = 255 * Math.pow(b / 255, 2.2);
      }
      // slight brightness alternation to tell layers apart
      const shade = (layer & 1) ? 0.84 : 1.0;
      r = r * shade; g = g * shade; b = b * shade;
      const o = i * 6;
      colors[o] = colors[o + 3] = r; colors[o + 1] = colors[o + 4] = g; colors[o + 2] = colors[o + 5] = b;
    }

    const extPos = ext.out(), trvPos = trv.out();
    const res = {
      kind: 'done',
      extPos, colors, trvPos,
      layerStart: Int32Array.from(layerStart),
      travelLayerStart: Int32Array.from(travelLayerStart),
      layerZ: Float32Array.from(layerZ),
      typesSeen: hasTypes ? [...typesSeen].sort((a, b) => a - b) : [],
      bounds: !nSeg ? null : (minX <= maxX ? { minX, minY, minZ, maxX, maxY, maxZ }
        : { minX: cb.minX, minY: cb.minY, minZ: cb.minZ, maxX: cb.maxX, maxY: cb.maxY, maxZ: cb.maxZ }),
      moves, filament,
      parseMs: performance.now() - t0,
    };
    postMessage(res, [extPos.buffer, colors.buffer, trvPos.buffer, res.layerStart.buffer, res.travelLayerStart.buffer, res.layerZ.buffer]);
  }

  self.onmessage = async (ev) => {
    const m = ev.data;
    try {
      ALIASES = m.aliases; KEYS = m.keys; PALETTE = m.palette;
      let text = m.text;
      if (text == null && m.blob) text = await m.blob.text();
      postMessage({ kind: 'progress', value: 0.02 });
      parse(text, m.opts);
    } catch (err) {
      postMessage({ kind: 'error', message: String(err && err.message || err) });
    }
  };
}

let workerUrl = null;
function makeWorker() {
  if (!workerUrl) {
    const src = '(' + gcodeWorkerMain.toString() + ')();';
    workerUrl = URL.createObjectURL(new Blob([src], { type: 'text/javascript' }));
  }
  return new Worker(workerUrl);
}

/* ------------------------------------------------------------------ */
/* Utilities                                                            */
/* ------------------------------------------------------------------ */

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const fmt = (v, d = 1) => Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtInt = (v) => Number(v).toLocaleString(undefined);

async function fetchOk(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`Download failed (${r.status}): ${url}`);
  return r;
}

/* ------------------------------------------------------------------ */
/* Viewer                                                               */
/* ------------------------------------------------------------------ */

export class Viewer {
  /**
   * @param {HTMLElement} container
   * @param {{ onProgress?: (f:number)=>void, bed?: {x:number,y:number} }} [options]
   */
  constructor(container, options = {}) {
    this.container = container;
    this.options = options;
    this.onProgress = options.onProgress || null;
    this.bed = { x: 260, y: 260, ...(options.bed || {}) };
    this.info = null;
    this.layerCount = 0;
    this._layer = { min: 0, max: 0 };
    this._showTravel = false;
    this._disposed = false;
    this._worker = null;
    this._loadToken = 0;
    this._model = null;      // group of the current object
    this._gcode = null;      // { ext, trv, layerStart, travelLayerStart, layerZ }

    container.classList.add('pmv');
    this._buildDom();

    try {
      this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
    } catch (err) {
      this._setMessage('WebGL is not available on this device.');
      this.renderer = null;
      return;
    }
    const r = this.renderer;
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    const bg = (getComputedStyle(container).getPropertyValue('--bg') || '').trim() || '#0b0d12';
    r.setClearColor(new THREE.Color(bg), 1);
    r.domElement.className = 'pmv-canvas';
    this.root.prepend(r.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.5, 6000);
    this.camera.position.set(260, 260, 340);

    this.controls = new OrbitControls(this.camera, r.domElement);
    this.controls.screenSpacePanning = true;
    this.controls.maxDistance = 3000;
    this.controls.minDistance = 5;
    this._onChange = () => this.render();
    this.controls.addEventListener('change', this._onChange);

    // Lights
    this.scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x1a1f2a, 1.4));
    this._keyLight = new THREE.DirectionalLight(0xffffff, 2.0);
    this._keyLight.position.set(0.6, 1, 0.8);
    this.camera.add(this._keyLight);
    const fill = new THREE.DirectionalLight(0x9db8ff, 0.6);
    fill.position.set(-1, -0.3, -0.6);
    this.camera.add(fill);
    this.scene.add(this.camera);

    this._buildPlate();

    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(container);
    this._resize();
    this.frame(null);
  }

  /* ---------------- DOM / overlays ---------------- */

  _buildDom() {
    const root = this.root = el('div', 'pmv-root');

    this.$info = el('div', 'pmv-info');
    this.$info.hidden = true;

    this.$progress = el('div', 'pmv-progress');
    this.$progress.hidden = true;
    this.$progressBar = el('div', 'pmv-progress-bar');
    this.$progressLabel = el('div', 'pmv-progress-label', 'Loading…');
    const track = el('div', 'pmv-progress-track');
    track.append(this.$progressBar);
    this.$progress.append(this.$progressLabel, track);

    this.$msg = el('div', 'pmv-msg');
    this.$msg.hidden = true;

    this.$legend = el('div', 'pmv-legend');
    this.$legend.hidden = true;

    this.$reset = el('button', 'pmv-btn pmv-reset', 'Recenter');
    this.$reset.type = 'button';
    this.$reset.title = 'Recenter the view';
    this.$reset.addEventListener('click', () => this.frame(this._bounds || null));

    // Layer bar
    const bar = this.$layers = el('div', 'pmv-layers');
    bar.hidden = true;
    this.$layerLabel = el('span', 'pmv-layer-label', 'Layer 0 / 0');
    this.$slider = el('input', 'pmv-slider');
    this.$slider.type = 'range';
    this.$slider.min = '0'; this.$slider.max = '0'; this.$slider.step = '1'; this.$slider.value = '0';
    this.$slider.setAttribute('aria-label', 'Displayed layer');
    this.$slider.addEventListener('input', () => this.setLayerRange(+this.$slider.value));
    const tgl = el('label', 'pmv-toggle');
    this.$travel = el('input');
    this.$travel.type = 'checkbox';
    this.$travel.addEventListener('change', () => this.setTravelVisible(this.$travel.checked));
    tgl.append(this.$travel, el('span', null, 'Travel moves'));
    const head = el('div', 'pmv-layers-head');
    head.append(this.$layerLabel, tgl);
    bar.append(head, this.$slider);

    root.append(this.$info, this.$reset, this.$legend, this.$layers, this.$progress, this.$msg);
    this.container.append(root);
  }

  _setMessage(text) {
    this.$msg.textContent = text || '';
    this.$msg.hidden = !text;
  }

  _setProgress(f, label) {
    if (f == null) { this.$progress.hidden = true; return; }
    this.$progress.hidden = false;
    if (label) this.$progressLabel.textContent = label;
    this.$progressBar.style.width = `${Math.round(Math.max(0, Math.min(1, f)) * 100)}%`;
    if (this.onProgress) { try { this.onProgress(f); } catch (_) { /* ignore */ } }
  }

  _setInfo(lines) {
    this.$info.replaceChildren(...lines.map(([k, v]) => {
      const row = el('div', 'pmv-info-row');
      row.append(el('span', 'pmv-k', k), el('span', 'pmv-v', v));
      return row;
    }));
    this.$info.hidden = !lines.length;
  }

  /* ---------------- Scene ---------------- */

  _buildPlate() {
    const { x: bx, y: by } = this.bed;
    const g = new THREE.Group();
    g.name = 'plate';
    const plate = new THREE.Mesh(
      new THREE.PlaneGeometry(bx, by),
      new THREE.MeshBasicMaterial({ color: 0x121620 }),
    );
    plate.rotation.x = -Math.PI / 2;
    plate.position.y = -0.05;
    g.add(plate);

    // 10 mm grid + bolder lines every 50 mm
    const minor = [], major = [];
    for (let v = 0; v <= bx + 1e-6; v += 10) (Math.round(v) % 50 === 0 ? major : minor).push(v - bx / 2, 0, -by / 2, v - bx / 2, 0, by / 2);
    for (let v = 0; v <= by + 1e-6; v += 10) (Math.round(v) % 50 === 0 ? major : minor).push(-bx / 2, 0, v - by / 2, bx / 2, 0, v - by / 2);
    const mk = (arr, color, opacity) => {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.Float32BufferAttribute(arr, 3));
      return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
    };
    g.add(mk(minor, 0x262c38, 0.9));
    g.add(mk(major, 0x323a4a, 1));
    // outline
    const o = [-bx / 2, 0, -by / 2, bx / 2, 0, -by / 2, bx / 2, 0, -by / 2, bx / 2, 0, by / 2,
      bx / 2, 0, by / 2, -bx / 2, 0, by / 2, -bx / 2, 0, by / 2, -bx / 2, 0, -by / 2];
    g.add(mk(o, 0x4c8dff, 0.55));
    // origin (front left): small X (red) and Y (green) axes
    const ax = [-bx / 2, 0.02, by / 2, -bx / 2 + 20, 0.02, by / 2];
    const ay = [-bx / 2, 0.02, by / 2, -bx / 2, 0.02, by / 2 - 20];
    g.add(mk(ax, 0xf0605a, 1));
    g.add(mk(ay, 0x34c38f, 1));
    this.scene.add(g);
    this._plate = g;
  }

  _resize() {
    if (!this.renderer || this._disposed) return;
    const w = Math.max(1, this.container.clientWidth);
    const h = Math.max(1, this.container.clientHeight);
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() {
    if (!this.renderer || this._disposed) return;
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      if (this._disposed) return;
      this.renderer.render(this.scene, this.camera);
    });
  }

  /** Frames the camera on a box (three coordinates) or on the bed. */
  frame(box) {
    if (!this.renderer) return;
    let center, radius;
    if (box && !box.isEmpty()) {
      const s = box.getBoundingSphere(new THREE.Sphere());
      center = s.center; radius = Math.max(s.radius, 5);
    } else {
      center = new THREE.Vector3(0, 0, 0);
      radius = Math.hypot(this.bed.x, this.bed.y) / 2;
    }
    const fov = THREE.MathUtils.degToRad(this.camera.fov);
    const fit = Math.min(fov, 2 * Math.atan(Math.tan(fov / 2) * this.camera.aspect));
    const dist = (radius / Math.sin(fit / 2)) * 1.05;
    const dir = new THREE.Vector3(0.55, 0.62, 0.95).normalize();
    this.camera.position.copy(center).addScaledVector(dir, dist);
    this.camera.near = Math.max(0.1, dist / 200);
    this.camera.far = dist * 20 + 2000;
    this.camera.updateProjectionMatrix();
    this.controls.target.copy(center);
    this.controls.update();
    this.render();
  }

  _clearModel() {
    this._loadToken++;
    if (this._worker) { this._worker.terminate(); this._worker = null; }
    if (this._model) {
      this.scene.remove(this._model);
      this._model.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
      });
      this._model = null;
    }
    this._gcode = null;
    this._bounds = null;
    this.info = null;
    this.layerCount = 0;
    this.$layers.hidden = true;
    this.$legend.hidden = true;
    this._setInfo([]);
    this._setMessage('');
    this.render();
    return this._loadToken;
  }

  /** Clears the scene (keeps the bed). */
  clear() { this._clearModel(); }

  /** Loads a file based on its extension (.stl / .gcode / .gco / .g). */
  async load(source, name) {
    const n = (name || (source && source.name) || (typeof source === 'string' ? source.split('?')[0] : '') || '').toLowerCase();
    if (n.endsWith('.stl')) return this.loadSTL(source);
    return this.loadGcode(source);
  }

  /* ---------------- STL ---------------- */

  async loadSTL(source) {
    if (!this.renderer) throw new Error('WebGL not available');
    const token = this._clearModel();
    this._setProgress(0, 'Loading STL…');
    try {
      let buf;
      if (source instanceof ArrayBuffer) buf = source;
      else if (ArrayBuffer.isView(source)) buf = source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength);
      else if (source instanceof Blob) buf = await source.arrayBuffer();
      else if (typeof source === 'string') buf = await (await fetchOk(source)).arrayBuffer();
      else throw new Error('Unsupported STL source');
      if (token !== this._loadToken) return null;
      this._setProgress(0.5, 'Parsing STL…');
      await new Promise((r) => setTimeout(r, 0));

      const geo = new STLLoader().parse(buf);
      if (token !== this._loadToken) { geo.dispose(); return null; }
      geo.computeBoundingBox();
      const pb = geo.boundingBox; // printer coordinates (Z up)
      const size = { x: pb.max.x - pb.min.x, y: pb.max.y - pb.min.y, z: pb.max.z - pb.min.z };
      // Z-up -> Y-up: (x, y, z) -> (x, z, -y)
      geo.rotateX(-Math.PI / 2);
      geo.computeBoundingBox();
      const b = geo.boundingBox;
      geo.translate(-(b.min.x + b.max.x) / 2, -b.min.y, -(b.min.z + b.max.z) / 2);
      if (!geo.attributes.normal) geo.computeVertexNormals();
      geo.computeBoundingBox();
      geo.computeBoundingSphere();

      const hasColors = !!geo.hasColors;
      const mat = new THREE.MeshStandardMaterial({
        color: hasColors ? 0xffffff : 0x7aa9ff, vertexColors: hasColors,
        roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(geo, mat);
      const group = new THREE.Group();
      group.add(mesh);
      this._model = group;
      this.scene.add(group);

      const triangles = geo.index ? geo.index.count / 3 : geo.attributes.position.count / 3;
      const fits = size.x <= this.bed.x && size.y <= this.bed.y;
      this.info = { type: 'stl', size, triangles, fitsBed: fits };
      const lines = [
        ['Dimensions', `${fmt(size.x)} × ${fmt(size.y)} × ${fmt(size.z)} mm`],
        ['Triangles', fmtInt(triangles)],
      ];
      if (!fits) lines.push(['Warning', 'exceeds the bed']);
      this._setInfo(lines);
      this._bounds = geo.boundingBox.clone();
      this.frame(this._bounds);
      this._setProgress(1);
      return this.info;
    } catch (err) {
      if (token === this._loadToken) this._setMessage(`Error: ${err.message || err}`);
      throw err;
    } finally {
      if (token === this._loadToken) this._setProgress(null);
    }
  }

  /* ---------------- G-code ---------------- */

  async loadGcode(source) {
    if (!this.renderer) throw new Error('WebGL not available');
    const token = this._clearModel();
    this._setProgress(0, 'Loading G-code…');
    const t0 = performance.now();
    try {
      const msg = {
        opts: { bedX: this.bed.x, bedY: this.bed.y },
        aliases: Object.fromEntries(Object.entries(FEATURE_ALIASES)),
        keys: FEATURES.map((f) => f.key),
        palette: Uint8Array.from(FEATURES.flatMap((f) => { const c = new THREE.Color(f.color); return [c.r * 255, c.g * 255, c.b * 255].map(Math.round); })),
      };
      if (source instanceof Blob) msg.blob = source;
      else if (source instanceof ArrayBuffer || ArrayBuffer.isView(source)) msg.blob = new Blob([source]);
      else if (typeof source === 'string' && /[\n\r]/.test(source)) msg.text = source;
      else if (typeof source === 'string') msg.blob = await this._fetchWithProgress(source, token);
      else throw new Error('Unsupported G-code source');
      if (token !== this._loadToken) return null;
      const tFetched = performance.now();

      this._setProgress(0, 'Parsing G-code…');
      const res = await new Promise((resolve, reject) => {
        const w = this._worker = makeWorker();
        w.onmessage = (ev) => {
          const d = ev.data;
          if (d.kind === 'progress') { if (token === this._loadToken) this._setProgress(d.value, 'Parsing G-code…'); }
          else if (d.kind === 'done') resolve(d);
          else if (d.kind === 'error') reject(new Error(d.message));
        };
        w.onerror = (e) => reject(new Error(e.message || 'Worker error'));
        w.postMessage(msg);
      });
      if (this._worker) { this._worker.terminate(); this._worker = null; }
      if (token !== this._loadToken) return null;
      if (!res.layerStart.length) throw new Error('No extrusion found in this G-code');

      this._buildGcode(res);
      const b = res.bounds;
      const size = { x: b.maxX - b.minX, y: b.maxY - b.minY, z: b.maxZ };
      this.info = {
        type: 'gcode', layers: this.layerCount, size,
        segments: res.extPos.length / 6, travels: res.trvPos.length / 6, moves: res.moves,
        filamentMm: res.filament, parseMs: Math.round(res.parseMs),
        totalMs: Math.round(performance.now() - t0), fetchMs: Math.round(tFetched - t0),
        features: res.typesSeen.map((i) => FEATURES[i].key),
      };
      this._setInfo([
        ['Dimensions', `${fmt(size.x)} × ${fmt(size.y)} × ${fmt(size.z)} mm`],
        ['Layers', fmtInt(this.layerCount)],
        ['Filament', `${fmt(res.filament / 1000, 2)} m`],
      ]);
      this._buildLegend(res.typesSeen);
      this._setProgress(1);
      return this.info;
    } catch (err) {
      if (token === this._loadToken) this._setMessage(`Error: ${err.message || err}`);
      throw err;
    } finally {
      if (token === this._loadToken) this._setProgress(null);
    }
  }

  async _fetchWithProgress(url, token) {
    const r = await fetchOk(url);
    const total = +r.headers.get('content-length') || 0;
    if (!r.body || !total) return r.blob();
    const reader = r.body.getReader();
    const parts = []; let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value); got += value.length;
      if (token === this._loadToken) this._setProgress(got / total * 0.5, 'Downloading G-code…');
      else { reader.cancel(); break; }
    }
    return new Blob(parts);
  }

  _buildGcode(res) {
    const group = new THREE.Group();
    group.name = 'gcode';

    const eg = new THREE.BufferGeometry();
    eg.setAttribute('position', new THREE.BufferAttribute(res.extPos, 3));
    eg.setAttribute('color', new THREE.BufferAttribute(res.colors, 3, true));
    eg.computeBoundingBox(); eg.computeBoundingSphere();
    const ext = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ vertexColors: true }));
    group.add(ext);

    const tg = new THREE.BufferGeometry();
    tg.setAttribute('position', new THREE.BufferAttribute(res.trvPos, 3));
    if (res.trvPos.length) { tg.computeBoundingSphere(); }
    const trv = new THREE.LineSegments(tg, new THREE.LineBasicMaterial({ color: 0x4c8dff, transparent: true, opacity: 0.35, depthWrite: false }));
    trv.visible = this._showTravel;
    group.add(trv);

    this._model = group;
    this.scene.add(group);
    this._gcode = {
      ext, trv,
      layerStart: res.layerStart, travelLayerStart: res.travelLayerStart, layerZ: res.layerZ,
      extSegs: res.extPos.length / 6, trvSegs: res.trvPos.length / 6,
    };
    this.layerCount = res.layerStart.length;
    const b = res.bounds, ox = this.bed.x / 2, oy = this.bed.y / 2;
    this._bounds = new THREE.Box3(
      new THREE.Vector3(b.minX - ox, 0, oy - b.maxY),
      new THREE.Vector3(b.maxX - ox, b.maxZ, oy - b.minY));

    this.$slider.max = String(this.layerCount - 1);
    this.$layers.hidden = false;
    this.$travel.checked = this._showTravel;
    this.setLayerRange(this.layerCount - 1);
    this.frame(this._bounds);
  }

  _buildLegend(typesSeen) {
    if (!typesSeen.length) {
      this.$legend.replaceChildren(el('div', 'pmv-legend-title', 'Height'), el('div', 'pmv-gradient'));
      this.$legend.hidden = false;
      return;
    }
    const items = typesSeen.filter((i) => FEATURES[i].key !== 'custom' || typesSeen.length === 1).map((i) => {
      const f = FEATURES[i];
      const row = el('div', 'pmv-legend-item');
      const sw = el('span', 'pmv-swatch');
      sw.style.background = f.color;
      row.append(sw, el('span', null, f.label));
      return row;
    });
    this.$legend.replaceChildren(...items);
    this.$legend.hidden = !items.length;
  }

  /** Shows layers min..max (0-based index). */
  setLayerRange(max, min = 0) {
    const g = this._gcode;
    if (!g) return;
    const N = this.layerCount;
    max = Math.max(0, Math.min(N - 1, Math.round(+max)));
    min = Math.max(0, Math.min(max, Math.round(+min || 0)));
    this._layer = { min, max };
    const eStart = g.layerStart[min];
    const eEnd = max + 1 < N ? g.layerStart[max + 1] : g.extSegs;
    g.ext.geometry.setDrawRange(eStart * 2, (eEnd - eStart) * 2);
    const tStart = g.travelLayerStart[min];
    const tEnd = max + 1 < N ? g.travelLayerStart[max + 1] : g.trvSegs;
    g.trv.geometry.setDrawRange(tStart * 2, Math.max(0, tEnd - tStart) * 2);
    if (+this.$slider.value !== max) this.$slider.value = String(max);
    const z = g.layerZ[max];
    this.$layerLabel.textContent = `Layer ${max + 1} / ${N}`;
    this.$layerLabel.title = `Z = ${fmt(z, 2)} mm`;
    this.$slider.style.setProperty('--pmv-fill', `${N > 1 ? (max / (N - 1)) * 100 : 100}%`);
    this.render();
  }

  get layerRange() { return { ...this._layer }; }

  /** Shows/hides travel moves. */
  setTravelVisible(on) {
    this._showTravel = !!on;
    if (this.$travel.checked !== this._showTravel) this.$travel.checked = this._showTravel;
    if (this._gcode) this._gcode.trv.visible = this._showTravel;
    this.render();
  }

  /**
   * PNG thumbnail (data URL) of the loaded STL, slicer-style: transparent background,
   * 3/4 front-left-top view, ~85% framing, light gray material.
   * Rendered off-screen (dedicated renderer): the visible view is not modified.
   */
  async snapshot(size = 300) {
    if (this._disposed) throw new Error('Viewer disposed');
    const mesh = this._model && this._model.children.find((o) => o.isMesh);
    if (!mesh) throw new Error('No STL loaded');
    size = Math.max(16, Math.round(size));
    const ss = size <= 128 ? 4 : 2;           // supersampling
    const px = size * ss;

    if (!this._snap) {
      const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
      r.setPixelRatio(1);
      r.setClearColor(0x000000, 0);
      const scene = new THREE.Scene();
      scene.add(new THREE.HemisphereLight(0xffffff, 0x50565f, 1.5));
      const cam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10000);
      const key = new THREE.DirectionalLight(0xffffff, 2.2); key.position.set(-0.4, 0.9, 0.7);
      const rim = new THREE.DirectionalLight(0xdfe6ff, 0.7); rim.position.set(0.8, 0.3, -0.6);
      cam.add(key, rim);
      scene.add(cam);
      const mat = new THREE.MeshStandardMaterial({ color: 0xc4c8ce, roughness: 0.62, metalness: 0.0, side: THREE.DoubleSide });
      this._snap = { r, scene, cam, mat };
    }
    const { r, scene, cam, mat } = this._snap;
    r.setSize(px, px, false);

    const m = new THREE.Mesh(mesh.geometry, mat);
    scene.add(m);
    try {
      const box = mesh.geometry.boundingBox || (mesh.geometry.computeBoundingBox(), mesh.geometry.boundingBox);
      const center = box.getCenter(new THREE.Vector3());
      const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
      // front = +z (three), left = -x, top = +y
      const dir = new THREE.Vector3(-1, 0.85, 1.25).normalize();
      cam.position.copy(center).addScaledVector(dir, radius * 4);
      cam.up.set(0, 1, 0);
      cam.lookAt(center);
      cam.updateMatrixWorld(true);
      // project the 8 corners into camera space for exact framing
      const inv = cam.matrixWorldInverse;
      let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      const v = new THREE.Vector3();
      for (let i = 0; i < 8; i++) {
        v.set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z).applyMatrix4(inv);
        x0 = Math.min(x0, v.x); x1 = Math.max(x1, v.x); y0 = Math.min(y0, v.y); y1 = Math.max(y1, v.y);
      }
      const half = Math.max(x1 - x0, y1 - y0) / 2 / 0.85;
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      cam.left = cx - half; cam.right = cx + half; cam.top = cy + half; cam.bottom = cy - half;
      cam.near = 0.1; cam.far = radius * 10;
      cam.updateProjectionMatrix();
      r.render(scene, cam);

      const out = document.createElement('canvas');
      out.width = out.height = size;
      const ctx = out.getContext('2d');
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(r.domElement, 0, 0, size, size);
      return out.toDataURL('image/png');
    } finally {
      scene.remove(m);
    }
  }

  dispose() {
    if (this._disposed) return;
    if (this._snap) {
      this._snap.mat.dispose();
      this._snap.r.dispose();
      this._snap.r.forceContextLoss();
      this._snap = null;
    }
    this._clearModel();
    this._disposed = true;
    if (this._raf) cancelAnimationFrame(this._raf);
    if (this._ro) this._ro.disconnect();
    if (this.controls) { this.controls.removeEventListener('change', this._onChange); this.controls.dispose(); }
    if (this.scene) {
      this.scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
      });
    }
    if (this.renderer) {
      this.renderer.dispose();
      this.renderer.forceContextLoss();
    }
    this.root.remove();
    this.container.classList.remove('pmv');
  }
}

window.PMViewer = Viewer;
export default Viewer;
