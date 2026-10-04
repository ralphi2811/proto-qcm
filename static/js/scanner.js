import * as store from './store.js';
import { decryptQr, keyFromExam } from './crypto.js';
import { grade } from './grading.js';
import { h, icon, toast, api, ask } from './ui.js';

const L = (o) => String.fromCharCode(65 + o);

async function downscale(file, max = 2400) {
  const bmp = await createImageBitmap(file); // applique l'orientation EXIF
  const s = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = h('canvas', { width: Math.round(bmp.width * s), height: Math.round(bmp.height * s) });
  c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
  return new Promise((res) => c.toBlob(res, 'image/jpeg', 0.9));
}

export function renderScanner(view) {
  const exams = store.listExams();
  const fallbackSel = h('select', {},
    h('option', { value: '' }, 'Automatique (QR code)'),
    exams.map((e) => h('option', { value: e.id }, e.title)),
  );
  const queue = h('div', { class: 'scan-results' });
  const busy = h('div', { class: 'busy', hidden: true }, h('span', { class: 'spinner' }), h('span', { class: 'busy-text' }));
  const copies = []; // copies en cours d'assemblage (non enregistrées)

  let pending = Promise.resolve();
  const enqueue = (files) => {
    for (const f of files) pending = pending.then(() => processFile(f)).catch(() => {});
  };

  const fallbackParams = (fallback) => fallback && JSON.stringify({
    n_questions: fallback.questions.length,
    n_options: Math.max(...fallback.questions.map((q) => q.options.length)),
    style: fallback.boxSize === 'large' ? 1 : 0,
  });

  const isPdf = (file) => file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '');

  async function processFile(file) {
    busy.hidden = false;
    const busyText = busy.querySelector('.busy-text');
    busyText.textContent = `Analyse de ${file.name || 'la photo'}…`;
    const fallback = fallbackSel.value ? store.getExam(fallbackSel.value) : null;
    try {
      const fd = new FormData();
      if (fallback) fd.append('params', fallbackParams(fallback));
      if (isPdf(file)) {
        fd.append('file', file, file.name || 'scan.pdf');
        const res = await api('/api/scan-pdf', { method: 'POST', body: fd });
        for await (const line of ndjson(res)) {
          busyText.textContent = `Analyse de ${file.name} : page ${line.page}/${line.n_pages}…`;
          const label = `${file.name} p. ${line.page}`;
          if (line.error) reportError(label, line.error);
          else await handleScan(line.scan, fallback, label);
        }
      } else {
        fd.append('image', await downscale(file), 'copie.jpg');
        const scan = await (await api('/api/scan', { method: 'POST', body: fd })).json();
        await handleScan(scan, fallback, file.name || 'Photo');
      }
    } catch (e) {
      reportError(file.name || 'Photo', e.message);
    } finally {
      busy.hidden = true;
    }
  }

  const reportError = (label, msg) =>
    queue.prepend(h('div', { class: 'card alert error' }, h('strong', {}, label), ' : ', msg));

  async function handleScan(scan, fallback, label) {
    try {
      let key = null, source = '', localExam = null;
      if (scan.qr) {
        try {
          key = await decryptQr(scan.qr, store.allKeys());
          localExam = store.findExamByQrId(key.examId);
          source = 'QR';
        } catch (e) {
          if (!fallback) throw e;
          toast(`${e.message} — corrigé local utilisé`, 'warn');
        }
      }
      if (!key) {
        if (!fallback) throw new Error('QR code introuvable sur la page');
        key = { ...keyFromExam(fallback), examId: scan.header?.exam_id ?? null };
        localExam = fallback;
        source = 'QCM local';
      }
      attach(scan, key, localExam, source);
    } catch (e) {
      reportError(label, e.message);
    }
  }

  /** Range une page lue dans la bonne copie (nouvelle ou en cours d'assemblage). */
  function attach(scan, key, localExam, source) {
    const open = copies.filter((c) => !c.saved && !c.discarded && c.examId === key.examId);
    let copy = null;
    const p = scan.page;
    if (p && p.copy > 0) {
      copy = open.find((c) => c.copyNo === p.copy) ?? null; // exemplaire numéroté
    } else if (p && p.page > 1) {
      // non numéroté : la page rejoint la dernière copie commencée à laquelle elle manque
      copy = open.filter((c) => c.copyNo === 0 && !c.pages.has(p.page)).at(-1) ?? null;
    }
    if (!copy) {
      copy = new Copy({ key, localExam, source, copyNo: p?.copy ?? 0, nPages: p?.n_pages ?? 1, onClose: (c) => c.card.remove() });
      copies.push(copy);
    }
    copy.addPage(p?.page ?? 1, scan);
    queue.prepend(copy.card);
  }

  const camera = h('input', { type: 'file', accept: 'image/*', capture: 'environment', hidden: true, onchange: (e) => { enqueue([...e.target.files]); e.target.value = ''; } });
  const gallery = h('input', { type: 'file', accept: 'image/*,application/pdf,.pdf', multiple: true, hidden: true, onchange: (e) => { enqueue([...e.target.files]); e.target.value = ''; } });

  view.replaceChildren(
    h('section', { class: 'page' },
      h('div', { class: 'page-head' }, h('h1', {}, 'Corriger')),
      h('div', { class: 'card scan-start' },
        h('button', { class: 'btn primary big', onclick: () => camera.click() }, icon('camera'), 'Photographier une page'),
        h('button', { class: 'btn', onclick: () => gallery.click() }, icon('image'), 'Importer photos ou PDF'),
        h('p', { class: 'muted small' },
          'Une photo par page, les 4 carrés noirs visibles, à plat, sans reflet. Un PDF issu du scanner (toutes les copies à la suite) est aussi accepté. Les pages d\'une même copie sont regroupées automatiquement ',
          '(sujet non numéroté : photographiez les pages dans l\'ordre, page 1 en premier). Touchez une case pour corriger la lecture.'),
        h('label', { class: 'field' }, h('span', {}, 'Grille séparée au QR illisible : corriger avec'), fallbackSel),
        camera, gallery,
      ),
      busy,
      queue,
    ),
  );
  return () => {};
}

/** Lit une réponse NDJSON au fil de l'eau (une ligne JSON par page). */
async function* ndjson(res) {
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (value) buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (line) yield JSON.parse(line);
    }
    if (done) break;
  }
  if (buf.trim()) yield JSON.parse(buf);
}

// --------------------------------------------------------------------- copie

class Copy {
  constructor({ key, localExam, source, copyNo, nPages, onClose }) {
    Object.assign(this, { key, localExam, source, copyNo, nPages, onClose });
    this.examId = key.examId;
    this.title = localExam?.title || `QCM ${key.examId ?? ''}`;
    this.pages = new Map(); // n° de page -> { scan, canvas, img }
    this.marks = new Map(); // "q:o" -> bool
    this.overridden = new Set();
    this.ident = {};
    this.build();
  }

  cellsOf(page) { return this.pages.get(page).scan.cells.filter((c) => c.o < (this.key.questions[c.q]?.nOpt ?? 0)); }
  allCells() { return [...this.pages.keys()].flatMap((p) => this.cellsOf(p)); }

  build() {
    this.scoreBox = h('div', { class: 'score' });
    this.pagesBox = h('div', { class: 'pages-status' });
    this.alerts = h('div');
    this.identBox = h('div', { class: 'ident' });
    this.canvases = h('div', { class: 'canvases' });
    this.card = h('article', { class: 'card result-card' },
      h('div', { class: 'result-head' },
        h('div', {},
          h('h3', {}, this.title, this.copyNo ? h('span', { class: 'muted' }, ` · n° ${this.copyNo}`) : null),
          h('p', { class: 'muted small' }, `Corrigé : ${this.source} · mode ${this.key.mode === 'partial' ? 'partiel' : 'tout ou rien'}`),
        ),
        this.scoreBox,
      ),
      this.pagesBox,
      this.alerts,
      this.identBox,
      h('div', { class: 'legend small' },
        h('span', { class: 'lg ok' }, 'juste'), h('span', { class: 'lg ko' }, 'fausse'),
        h('span', { class: 'lg miss' }, 'oubliée'), h('span', { class: 'lg unsure' }, 'douteuse / entourée')),
      this.canvases,
      h('div', { class: 'row end' },
        h('button', { class: 'btn ghost', onclick: () => { this.discarded = true; this.onClose(this); } }, 'Ignorer'),
        h('button', { class: 'btn primary', onclick: () => this.save() }, icon('check'), 'Enregistrer'),
      ),
    );
  }

  addPage(n, scan) {
    const replacing = this.pages.has(n);
    let entry = this.pages.get(n);
    if (!entry) {
      const canvas = h('canvas', { class: 'sheet-canvas' });
      entry = { canvas, img: new Image(), label: h('div', { class: 'page-label muted small' }) };
      canvas.addEventListener('click', (e) => this.onClick(n, e));
      entry.wrap = h('div', { class: 'canvas-wrap', 'data-page': n }, canvas);
      // insertion dans l'ordre des pages
      const after = [...this.canvases.children].find((el) => Number(el.dataset.page) > n);
      const block = h('div', { 'data-page': n }, this.nPages > 1 ? entry.label : null, entry.wrap);
      this.canvases.insertBefore(block, after ?? null);
      this.pages.set(n, entry);
    }
    entry.scan = scan;
    entry.label.textContent = `Page ${n} / ${this.nPages}${replacing ? ' (remplacée)' : ''}`;
    for (const c of this.cellsOf(n)) {
      const k = `${c.q}:${c.o}`;
      this.overridden.delete(k);
      this.marks.set(k, c.state === 'marked');
    }
    if (scan.name_fields && Object.keys(scan.name_fields).length) this.renderIdent(scan.name_fields);
    entry.img.onload = () => this.update();
    entry.img.src = `data:image/jpeg;base64,${scan.preview.jpeg}`;
    this.update();
  }

  renderIdent(fields) {
    // l'écriture de l'élève (recadrée par le serveur) au-dessus de chaque champ ; un OCR
    // pourra pré-remplir ces champs plus tard
    this.identBox.replaceChildren(...Object.entries(fields).map(([k, f]) => {
      const input = this.ident[k] ?? h('input', { placeholder: f.label, 'aria-label': f.label, autocapitalize: 'characters' });
      this.ident[k] = input;
      return h('label', { class: 'ident-row' },
        h('span', { class: 'ident-label muted small' }, f.label),
        h('img', { class: 'handwriting', src: `data:image/jpeg;base64,${f.jpeg}`, alt: `${f.label} manuscrit` }),
        input);
    }));
  }

  identity() {
    return Object.fromEntries(Object.entries(this.ident).map(([k, el]) => [k, el.value.trim()]));
  }

  missingPages() {
    return Array.from({ length: this.nPages }, (_, i) => i + 1).filter((p) => !this.pages.has(p));
  }

  update() {
    const m = this.key.questions.map((kq, q) => Array.from({ length: kq.nOpt }, (_, o) => !!this.marks.get(`${q}:${o}`)));
    this.result = grade(this.key, m);
    const r = this.result;
    this.scoreBox.replaceChildren(...[
      h('span', { class: 'big-score' }, r.note != null ? `${r.note} / ${r.noteSur}` : `${r.total} / ${r.max}`),
      r.note != null ? h('span', { class: 'muted' }, `${r.total} / ${r.max} pts`) : null,
    ].filter(Boolean));

    const missing = this.missingPages();
    this.pagesBox.replaceChildren(...(this.nPages > 1
      ? Array.from({ length: this.nPages }, (_, i) => i + 1).map((p) =>
          h('span', { class: `page-chip ${this.pages.has(p) ? 'ok' : 'todo'}` }, `${this.pages.has(p) ? '✓' : '…'} page ${p}`))
      : []));

    const flagged = (pred) => this.allCells().filter((c) => pred(c) && !this.overridden.has(`${c.q}:${c.o}`)).map((c) => `${c.q + 1}${L(c.o)}`);
    const circled = flagged((c) => c.circled);
    const unsure = flagged((c) => c.state === 'unsure' && !c.circled);
    const warnings = [...this.pages.values()].flatMap((e) => e.scan.warnings || []);
    this.alerts.replaceChildren(...[
      missing.length ? h('div', { class: 'alert warn' },
        `Page${missing.length > 1 ? 's' : ''} manquante${missing.length > 1 ? 's' : ''} : ${missing.join(', ')} — photographiez-la${missing.length > 1 ? 's' : ''} (questions comptées 0 sinon).`) : null,
      warnings.length ? h('div', { class: 'alert warn' }, [...new Set(warnings)].join(' · ')) : null,
      circled.length ? h('div', { class: 'alert warn' },
        `Cases qui semblent entourées (en jaune) : ${circled.join(', ')}. Elles ne sont pas comptées : touchez-les pour les valider.`) : null,
      unsure.length ? h('div', { class: 'alert warn' }, `Cases douteuses (en jaune) : ${unsure.join(', ')}. Touchez une case pour inverser.`) : null,
    ].filter(Boolean));

    for (const n of this.pages.keys()) this.draw(n);
  }

  draw(n) {
    const { canvas, img, scan } = this.pages.get(n);
    if (!img.complete || !img.naturalWidth) return;
    const ctx = canvas.getContext('2d');
    const ppm = scan.preview.px_per_mm;
    canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    ctx.drawImage(img, 0, 0);
    ctx.lineWidth = 2.5;
    for (const c of this.cellsOf(n)) {
      const kq = this.key.questions[c.q];
      const k = `${c.q}:${c.o}`;
      const [x, y, w, hh] = c.rect.map((v) => v * ppm);
      const marked = this.marks.get(k), ok = kq.correct[c.o];
      if (marked) {
        ctx.fillStyle = ok ? 'rgba(22,163,74,.45)' : 'rgba(220,38,38,.45)';
        ctx.fillRect(x - 2, y - 2, w + 4, hh + 4);
      } else if (ok) {
        ctx.strokeStyle = 'rgba(234,88,12,.95)';
        ctx.strokeRect(x - 2, y - 2, w + 4, hh + 4);
      }
      if (c.state === 'unsure' && !this.overridden.has(k)) {
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = 'rgba(202,138,4,1)';
        if (c.circled) {
          ctx.beginPath();
          ctx.ellipse(x + w / 2, y + hh / 2, w * 0.95, hh * 0.95, 0, 0, 2 * Math.PI);
          ctx.stroke();
        } else ctx.strokeRect(x - 5, y - 5, w + 10, hh + 10);
        ctx.setLineDash([]);
      }
    }
  }

  onClick(n, e) {
    const { canvas, scan } = this.pages.get(n);
    const r = canvas.getBoundingClientRect();
    const ppm = scan.preview.px_per_mm;
    const mx = ((e.clientX - r.left) * (canvas.width / r.width)) / ppm;
    const my = ((e.clientY - r.top) * (canvas.height / r.height)) / ppm;
    const pad = 1.2;
    const hit = this.cellsOf(n).find(({ rect: [x, y, w, hh] }) => mx >= x - pad && mx <= x + w + pad && my >= y - pad && my <= y + hh + pad);
    if (!hit) return;
    const k = `${hit.q}:${hit.o}`;
    this.marks.set(k, !this.marks.get(k));
    this.overridden.add(k);
    this.update();
  }

  async save() {
    const missing = this.missingPages();
    if (missing.length && !(await ask({
      title: `Page${missing.length > 1 ? 's' : ''} manquante${missing.length > 1 ? 's' : ''}`,
      message: `La copie n'a pas de page ${missing.join(', ')} : les questions de ${missing.length > 1 ? 'ces pages' : 'cette page'} compteront 0. Enregistrer quand même ?`,
      confirmLabel: 'Enregistrer quand même', cancelLabel: 'Photographier la page', kind: 'warn',
    }))) return;
    const r = this.result;
    const id = this.identity();
    store.addResult({
      id: store.uid(), date: Date.now(),
      examId: this.examId ?? null, examTitle: this.title, copyNo: this.copyNo || null,
      ...id,
      total: r.total, max: r.max, note: r.note, noteSur: r.noteSur,
      details: r.details.map((d) => ({ marked: d.marked, correct: d.correct, points: d.points, max: d.max })),
      corrections: this.overridden.size,
      pages: { received: this.pages.size, total: this.nPages },
    });
    const who = [id.nom, id.prenom].filter(Boolean).join(' ') || (this.copyNo ? `copie n° ${this.copyNo}` : 'copie');
    toast(`Enregistré : ${who} — ${r.note ?? r.total}`, 'ok');
    this.saved = true;
    this.onClose(this);
  }
}
