// Détection en direct des 4 repères carrés de la feuille, sur une petite image en niveaux de gris
// (aperçu caméra réduit à ~480 px). Pur calcul, sans DOM : testable hors navigateur.
//
// Géométrie (app/sheet.py) : repères pleins de 7 mm centrés à 12 mm des bords d'une page A4,
// soit un rectangle de 186 × 273 mm entre centres (rapport 1,47).

const SPAN_SHORT = 186; // mm entre centres des repères
const SPAN_LONG = 273;
const MARKER_MM = 7;
const MAX_CANDIDATES = 24;

/** Image RGBA (ImageData.data) -> niveaux de gris. */
export function toGray(rgba, w, h) {
  const g = new Uint8Array(w * h);
  for (let i = 0, j = 0; i < g.length; i++, j += 4) g[i] = (rgba[j] * 77 + rgba[j + 1] * 150 + rgba[j + 2] * 29) >> 8;
  return g;
}

/** Pixels sombres par rapport à leur voisinage (seuillage adaptatif par image intégrale). */
function darkMask(gray, w, h) {
  const W = w + 1;
  const integ = new Float64Array(W * (h + 1));
  for (let y = 0; y < h; y++) {
    let row = 0;
    for (let x = 0; x < w; x++) {
      row += gray[y * w + x];
      integ[(y + 1) * W + x + 1] = integ[y * W + x + 1] + row;
    }
  }
  const r = Math.max(6, Math.round(Math.min(w, h) / 10));
  const mask = new Uint8Array(w * h);
  for (let y = 0; y < h; y++) {
    const y0 = Math.max(0, y - r), y1 = Math.min(h, y + r + 1);
    for (let x = 0; x < w; x++) {
      const x0 = Math.max(0, x - r), x1 = Math.min(w, x + r + 1);
      const sum = integ[y1 * W + x1] - integ[y0 * W + x1] - integ[y1 * W + x0] + integ[y0 * W + x0];
      const mean = sum / ((x1 - x0) * (y1 - y0));
      const v = gray[y * w + x];
      if (v < mean - 18 && v < 150) mask[y * w + x] = 1;
    }
  }
  return mask;
}

/** Composantes connexes sombres, pleines et à peu près carrées. */
function squareBlobs(mask, w, h) {
  const seen = new Uint8Array(w * h);
  const stack = new Int32Array(w * h);
  const out = [];
  const minArea = 10;
  const maxArea = (w * h) / 150;
  for (let start = 0; start < mask.length; start++) {
    if (!mask[start] || seen[start]) continue;
    let sp = 0, n = 0, sx = 0, sy = 0;
    let minx = w, maxx = 0, miny = h, maxy = 0;
    stack[sp++] = start;
    seen[start] = 1;
    while (sp) {
      const p = stack[--sp];
      const x = p % w, y = (p - x) / w;
      n++; sx += x; sy += y;
      if (x < minx) minx = x; if (x > maxx) maxx = x;
      if (y < miny) miny = y; if (y > maxy) maxy = y;
      if (x > 0 && mask[p - 1] && !seen[p - 1]) { seen[p - 1] = 1; stack[sp++] = p - 1; }
      if (x < w - 1 && mask[p + 1] && !seen[p + 1]) { seen[p + 1] = 1; stack[sp++] = p + 1; }
      if (y > 0 && mask[p - w] && !seen[p - w]) { seen[p - w] = 1; stack[sp++] = p - w; }
      if (y < h - 1 && mask[p + w] && !seen[p + w]) { seen[p + w] = 1; stack[sp++] = p + w; }
    }
    if (n < minArea || n > maxArea) continue;
    const bw = maxx - minx + 1, bh = maxy - miny + 1;
    const aspect = bw / bh;
    const fill = n / (bw * bh);
    // carré plein, éventuellement tourné (un carré à 30° remplit encore ~60 % de sa boîte)
    if (aspect < 0.55 || aspect > 1.8 || fill < 0.55) continue;
    out.push({ x: sx / n, y: sy / n, area: n });
  }
  out.sort((a, b) => b.area - a.area);
  return out.slice(0, MAX_CANDIDATES);
}

const dist = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);

/** Ordonne 4 points autour de leur centre (sens horaire en coordonnées écran), haut-gauche en premier. */
function orderQuad(pts) {
  const cx = pts.reduce((s, p) => s + p.x, 0) / 4;
  const cy = pts.reduce((s, p) => s + p.y, 0) / 4;
  const sorted = [...pts].sort((a, b) => Math.atan2(a.y - cy, a.x - cx) - Math.atan2(b.y - cy, b.x - cx));
  let k = 0;
  sorted.forEach((p, i) => { if (p.x + p.y < sorted[k].x + sorted[k].y) k = i; });
  return [...sorted.slice(k), ...sorted.slice(0, k)];
}

function quadArea(q) {
  let a = 0;
  for (let i = 0; i < 4; i++) { const p = q[i], r = q[(i + 1) % 4]; a += p.x * r.y - r.x * p.y; }
  return Math.abs(a) / 2;
}

function isConvex(q) {
  let sign = 0;
  for (let i = 0; i < 4; i++) {
    const a = q[i], b = q[(i + 1) % 4], c = q[(i + 2) % 4];
    const z = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x);
    if (z === 0) return false;
    if (sign && Math.sign(z) !== sign) return false;
    sign = Math.sign(z);
  }
  return true;
}

/** Les 4 candidats forment-ils les repères d'une page A4 (vue en perspective modérée) ? Score = aire. */
function scoreQuad(cands) {
  const areas = cands.map((c) => c.area);
  if (Math.max(...areas) / Math.min(...areas) > 3.5) return 0;
  const q = orderQuad(cands);
  if (!isConvex(q)) return 0;
  // angles proches de l'angle droit (sinon : cases noircies alignées, pas des coins de page)
  for (let i = 0; i < 4; i++) {
    const a = q[(i + 3) % 4], b = q[i], c = q[(i + 1) % 4];
    const cos = ((a.x - b.x) * (c.x - b.x) + (a.y - b.y) * (c.y - b.y)) / (dist(a, b) * dist(c, b));
    if (Math.abs(cos) > 0.5) return 0; // hors de 60°-120°
  }
  const s = [0, 1, 2, 3].map((i) => dist(q[i], q[(i + 1) % 4]));
  // côtés opposés comparables (perspective limitée)
  if (Math.max(s[0], s[2]) / Math.min(s[0], s[2]) > 1.7) return 0;
  if (Math.max(s[1], s[3]) / Math.min(s[1], s[3]) > 1.7) return 0;
  const a = (s[0] + s[2]) / 2, b = (s[1] + s[3]) / 2;
  const ratio = Math.max(a, b) / Math.min(a, b);
  if (ratio < 1.15 || ratio > 1.9) return 0;
  // taille des repères cohérente avec l'écartement (7 mm pour 186 mm)
  const expected = (Math.min(a, b) * MARKER_MM) / SPAN_SHORT;
  const side = Math.sqrt(areas.reduce((x, y) => x + y, 0) / 4);
  if (side < expected * 0.45 || side > expected * 2.2) return 0;
  return quadArea(q);
}

/** Meilleur quadruplet de repères, ou null. */
function findQuad(cands) {
  let best = null, bestScore = 0;
  const n = cands.length;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) for (let k = j + 1; k < n; k++) for (let l = k + 1; l < n; l++) {
    const sc = scoreQuad([cands[i], cands[j], cands[k], cands[l]]);
    if (sc > bestScore) { bestScore = sc; best = [cands[i], cands[j], cands[k], cands[l]]; }
  }
  return best ? orderQuad(best) : null;
}

/** Luminosité, reflets et netteté (énergie du laplacien) dans une zone. */
function quality(gray, w, h, box) {
  const x0 = Math.max(1, Math.floor(box.x0)), x1 = Math.min(w - 1, Math.ceil(box.x1));
  const y0 = Math.max(1, Math.floor(box.y0)), y1 = Math.min(h - 1, Math.ceil(box.y1));
  let sum = 0, n = 0, bright = 0, lap = 0;
  for (let y = y0; y < y1; y += 2) {
    for (let x = x0; x < x1; x += 2) {
      const p = y * w + x;
      const v = gray[p];
      sum += v; n++;
      if (v >= 250) bright++;
      const l = 4 * v - gray[p - 1] - gray[p + 1] - gray[p - w] - gray[p + w];
      lap += l * l;
    }
  }
  if (!n) return { brightness: 0, glare: 0, sharpness: 0 };
  return { brightness: sum / n, glare: bright / n, sharpness: lap / n };
}

/**
 * Analyse d'une image d'aperçu. Renvoie :
 *   corners : 4 points {x, y} (haut-gauche, puis sens horaire) ou null ;
 *   found : nombre de repères plausibles vus (pour guider) ;
 *   coverage : part de l'image occupée par la feuille ;
 *   brightness (0-255), glare (part de pixels saturés), sharpness (relative).
 */
export function analyzeFrame(gray, w, h) {
  const cands = squareBlobs(darkMask(gray, w, h), w, h);
  const corners = cands.length >= 4 ? findQuad(cands) : null;
  const box = corners
    ? {
        x0: Math.min(...corners.map((p) => p.x)), x1: Math.max(...corners.map((p) => p.x)),
        y0: Math.min(...corners.map((p) => p.y)), y1: Math.max(...corners.map((p) => p.y)),
      }
    : { x0: w * 0.15, x1: w * 0.85, y0: h * 0.15, y1: h * 0.85 };
  return {
    corners,
    found: corners ? 4 : Math.min(3, cands.length),
    coverage: corners ? quadArea(corners) / (w * h) : 0,
    ...quality(gray, w, h, box),
  };
}

export const SHEET_RATIO = SPAN_LONG / SPAN_SHORT;
