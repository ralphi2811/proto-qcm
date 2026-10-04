import * as store from './store.js';
import { encryptExam } from './crypto.js';
import { openAiDialog } from './ai.js';
import { h, icon, toast, modal, download, api, confirmDanger, setChildren } from './ui.js';

const MAX_OPTIONS = 8;
const DENSITIES = [['large', 'Grande'], ['normal', 'Moyenne'], ['compact', 'Petite']];
const densityName = (d) => (DENSITIES.find(([k]) => k === d) || DENSITIES[1])[1];

/** Le QCM tel qu'envoyé au serveur : sans les bonnes réponses. */
const serverExam = (exam) => ({
  title: exam.title, subtitle: exam.subtitle, instructions: exam.instructions,
  questions: exam.questions.map((q) => ({ html: q.html, points: q.points, options: q.options.map((o) => ({ html: o.html })) })),
});
const letter = (i) => String.fromCharCode(65 + i);

function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = () => { clearTimeout(t); fn(); };
  return d;
}

async function resizeImage(file, max = 1000) {
  const bmp = await createImageBitmap(file);
  const s = Math.min(1, max / Math.max(bmp.width, bmp.height));
  const c = h('canvas', { width: Math.round(bmp.width * s), height: Math.round(bmp.height * s) });
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.85);
}

// ------------------------------------------------------------------ champ riche

let activeField = null;
let savedRange = null;

document.addEventListener('selectionchange', () => {
  const sel = document.getSelection();
  if (!sel.rangeCount) return;
  const node = sel.anchorNode;
  const field = node && (node.nodeType === 1 ? node : node.parentElement)?.closest?.('.rich');
  if (field) { activeField = field; savedRange = sel.getRangeAt(0).cloneRange(); }
});

function restoreSelection() {
  if (!activeField) return false;
  activeField.focus();
  if (savedRange) { const s = document.getSelection(); s.removeAllRanges(); s.addRange(savedRange); }
  return true;
}

function insertImageDataUrl(url) {
  if (!restoreSelection()) return toast("Placez d'abord le curseur dans un champ", 'warn');
  document.execCommand('insertHTML', false, `<img src="${url}" alt="">`);
}

function richField(value, onChange, { placeholder = '', cls = '', onKeyDown } = {}) {
  const el = h('div', {
    class: `rich ${cls}`, contenteditable: 'true', 'data-placeholder': placeholder, html: value || '',
    spellcheck: 'true',
  });
  const update = () => {
    // un champ "vide" garde parfois un <br> : on normalise pour le placeholder
    if (el.innerHTML === '<br>') el.innerHTML = '';
    onChange(el.innerHTML);
  };
  el.addEventListener('input', update);
  el.addEventListener('paste', async (e) => {
    const items = [...(e.clipboardData?.items || [])];
    const img = items.find((i) => i.type.startsWith('image/'));
    e.preventDefault();
    if (img) {
      insertImageDataUrl(await resizeImage(img.getAsFile()));
    } else {
      // texte brut uniquement : évite d'importer des styles parasites
      document.execCommand('insertText', false, e.clipboardData.getData('text/plain'));
    }
    update();
  });
  if (onKeyDown) el.addEventListener('keydown', onKeyDown);
  return el;
}

function formatToolbar() {
  const imgInput = h('input', {
    type: 'file', accept: 'image/*', hidden: true,
    onchange: async (e) => {
      const f = e.target.files[0];
      e.target.value = '';
      if (f) { insertImageDataUrl(await resizeImage(f)); activeField?.dispatchEvent(new Event('input')); }
    },
  });
  const btn = (label, title, action) => h('button', {
    class: 'tool', title, type: 'button',
    onmousedown: (e) => e.preventDefault(), // garde la sélection
    onclick: () => { if (restoreSelection()) { action(); activeField.dispatchEvent(new Event('input')); } },
  }, label);
  const wrap = (tag) => () => {
    const sel = document.getSelection();
    const text = sel.toString() || '…';
    document.execCommand('insertHTML', false, `<${tag}>${text.replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' })[c])}</${tag}>`);
  };
  return h('div', { class: 'format-bar', role: 'toolbar', 'aria-label': 'Mise en forme' },
    btn(h('b', {}, 'G'), 'Gras (Ctrl+B)', () => document.execCommand('bold')),
    btn(h('i', {}, 'I'), 'Italique (Ctrl+I)', () => document.execCommand('italic')),
    btn(h('u', {}, 'S'), 'Souligné (Ctrl+U)', () => document.execCommand('underline')),
    btn(h('span', {}, 'x²'), 'Exposant', () => document.execCommand('superscript')),
    btn(h('span', {}, 'x₂'), 'Indice', () => document.execCommand('subscript')),
    btn(h('code', {}, '</>'), 'Code', wrap('code')),
    btn(h('span', {}, '•'), 'Liste', () => document.execCommand('insertUnorderedList')),
    h('button', {
      class: 'tool', title: 'Insérer une image', type: 'button',
      onmousedown: (e) => e.preventDefault(), onclick: () => imgInput.click(),
    }, icon('image')),
    btn(h('span', {}, '⌫'), 'Effacer la mise en forme', () => document.execCommand('removeFormat')),
    imgInput,
  );
}

// ------------------------------------------------------------------ éditeur

export function renderEditor(view, exam, { openAi = false } = {}) {
  const status = h('span', { class: 'save-status muted small' }, 'Enregistré');
  const persist = debounce(() => { store.saveExam(exam); status.textContent = 'Enregistré'; }, 400);
  // Nombre de pages du sujet (mode « sur le sujet »), calculé par le serveur sur la vraie mise en page
  const pageInfo = h('span', { class: 'page-info' });
  let layoutCtrl = null;
  const refreshPages = debounce(async () => {
    layoutCtrl?.abort();
    if (exam.answerMode === 'grid') { pageInfo.replaceChildren(); return; }
    layoutCtrl = new AbortController();
    try {
      const res = await api('/api/layout', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: layoutCtrl.signal,
        body: JSON.stringify({ exam: serverExam(exam), box_size: exam.boxSize === 'large' ? 'large' : 'standard' }),
      });
      const { pages } = await res.json();
      const cur = exam.density || 'normal';
      const n = pages[cur];
      const order = DENSITIES.map(([k]) => k);
      // le plus grand texte qui économise une page, sinon un texte plus grand qui tient autant
      const fewer = order.find((d) => pages[d] < n);
      const bigger = order.slice(0, order.indexOf(cur)).find((d) => pages[d] <= n);
      const hint = fewer || bigger;
      setChildren(pageInfo,
        h('span', { class: 'pages-count', title: `Texte ${densityName(cur).toLowerCase()}` }, icon('file'), `${n} page${n > 1 ? 's' : ''}`),
        hint ? h('button', {
          class: 'btn ghost small hint-btn', title: 'Appliquer cette taille de texte',
          onclick: () => { exam.density = hint; densitySel.value = hint; changed(); },
        }, fewer ? `→ ${pages[hint]} page${pages[hint] > 1 ? 's' : ''} en ${densityName(hint)}` : `Tient aussi en ${densityName(hint)}`) : null,
      );
    } catch (e) {
      if (e.name !== 'AbortError' && !layoutCtrl?.signal.aborted) pageInfo.replaceChildren();
    }
  }, 1200);
  const changed = () => { status.textContent = 'Modification…'; persist(); refreshPages(); };

  const densitySel = h('select', { onchange: (e) => { exam.density = e.target.value; changed(); } },
    DENSITIES.map(([k, label]) => h('option', { value: k, selected: (exam.density || 'normal') === k }, label)));

  const list = h('div', { class: 'questions' });

  const field = (label, input) => h('label', { class: 'field' }, h('span', {}, label), input);

  const settings = h('details', { class: 'card settings', open: exam.questions.length <= 1 },
    h('summary', {}, 'Paramètres du QCM'),
    h('div', { class: 'grid2' },
      field('Sous-titre (classe, date…)', h('input', {
        value: exam.subtitle, placeholder: '3e B — 12 octobre',
        oninput: (e) => { exam.subtitle = e.target.value; changed(); },
      })),
      field('Notation', h('select', {
        onchange: (e) => { exam.scoring = e.target.value; changed(); },
      },
        h('option', { value: 'strict', selected: exam.scoring === 'strict' }, 'Tout ou rien par question'),
        h('option', { value: 'partial', selected: exam.scoring === 'partial' }, 'Partielle (bonnes − mauvaises cases)'),
      )),
      field('Où les élèves répondent', h('select', {
        onchange: (e) => { exam.answerMode = e.target.value; changed(); },
      },
        h('option', { value: 'subject', selected: exam.answerMode !== 'grid' }, 'Directement sur le sujet'),
        h('option', { value: 'grid', selected: exam.answerMode === 'grid' }, 'Sur une grille de réponses séparée'),
      )),
      field('Taille des cases', h('select', {
        onchange: (e) => { exam.boxSize = e.target.value; changed(); },
      },
        h('option', { value: 'large', selected: exam.boxSize === 'large' }, 'Grandes — primaire (jusqu\'à ~60 questions)'),
        h('option', { value: 'standard', selected: exam.boxSize !== 'large' }, 'Standard (jusqu\'à ~130 questions)'),
      )),
      field('Taille du texte (sujet)', densitySel),
      field('Note ramenée sur', h('input', {
        type: 'number', min: 0, max: 255, value: exam.noteSur,
        oninput: (e) => { exam.noteSur = Number(e.target.value) || 0; changed(); },
      })),
    ),
    field('Consignes', richField(exam.instructions, (v) => { exam.instructions = v; changed(); }, {
      placeholder: 'Ex. : une ou plusieurs bonnes réponses par question. Calculatrice interdite.',
    })),
  );

  function renderQuestions() {
    list.replaceChildren(...exam.questions.map((q, i) => questionCard(q, i)));
    counter.textContent = `${exam.questions.length} question${exam.questions.length > 1 ? 's' : ''}`;
  }

  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= exam.questions.length) return;
    [exam.questions[i], exam.questions[j]] = [exam.questions[j], exam.questions[i]];
    renderQuestions(); changed();
    list.children[j]?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function questionCard(q, i) {
    const optsBox = h('ol', { class: 'options' });

    const renderOptions = (focusIdx) => {
      optsBox.replaceChildren(...q.options.map((o, k) => {
        const toggle = h('button', {
          type: 'button', class: `correct-toggle ${o.correct ? 'on' : ''}`,
          'aria-pressed': String(!!o.correct), title: o.correct ? 'Bonne réponse' : 'Marquer comme bonne réponse',
          onclick: () => { o.correct = !o.correct; renderOptions(); changed(); },
        }, icon('check'));
        const txt = richField(o.html, (v) => { o.html = v; changed(); }, {
          placeholder: `Réponse ${letter(k)}`, cls: 'option-text',
          onKeyDown: (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              if (q.options.length < MAX_OPTIONS) { q.options.splice(k + 1, 0, { html: '', correct: false }); renderOptions(k + 1); changed(); }
            } else if (e.key === 'Backspace' && !txt.textContent && !txt.querySelector('img') && q.options.length > 2) {
              e.preventDefault(); q.options.splice(k, 1); renderOptions(Math.max(0, k - 1)); changed();
            }
          },
        });
        return h('li', { class: `option ${o.correct ? 'is-correct' : ''}` },
          toggle, h('span', { class: 'letter' }, letter(k)), txt,
          h('button', {
            type: 'button', class: 'btn ghost icon-btn small', title: 'Supprimer la réponse', disabled: q.options.length <= 2,
            onclick: () => { q.options.splice(k, 1); renderOptions(); changed(); },
          }, icon('x')),
        );
      }));
      addOpt.disabled = q.options.length >= MAX_OPTIONS;
      if (focusIdx != null) optsBox.children[focusIdx]?.querySelector('.rich')?.focus();
      card.classList.toggle('warn', !q.options.some((o) => o.correct));
    };

    const addOpt = h('button', {
      type: 'button', class: 'btn ghost small',
      onclick: () => { q.options.push({ html: '', correct: false }); renderOptions(q.options.length - 1); changed(); },
    }, icon('plus'), 'Réponse');

    const card = h('article', { class: 'card question' },
      h('div', { class: 'q-head' },
        h('span', { class: 'q-num' }, i + 1),
        richField(q.html, (v) => { q.html = v; changed(); }, { placeholder: 'Intitulé de la question…', cls: 'q-text' }),
      ),
      optsBox,
      h('div', { class: 'q-foot' },
        addOpt,
        h('label', { class: 'points' }, 'Points',
          h('input', {
            type: 'number', min: 0, max: 127, step: 0.5, value: q.points,
            oninput: (e) => { q.points = Math.max(0, Number(e.target.value) || 0); changed(); },
          })),
        h('span', { class: 'spacer' }),
        h('button', { type: 'button', class: 'btn ghost icon-btn small', title: 'Monter', disabled: i === 0, onclick: () => move(i, -1) }, icon('up')),
        h('button', { type: 'button', class: 'btn ghost icon-btn small', title: 'Descendre', disabled: i === exam.questions.length - 1, onclick: () => move(i, 1) }, icon('down')),
        h('button', {
          type: 'button', class: 'btn ghost icon-btn small', title: 'Dupliquer',
          onclick: () => { exam.questions.splice(i + 1, 0, { ...structuredClone(q), id: store.uid() }); renderQuestions(); changed(); },
        }, icon('copy')),
        h('button', {
          type: 'button', class: 'btn ghost icon-btn small danger', title: 'Supprimer la question', disabled: exam.questions.length <= 1,
          onclick: async () => {
            if (await confirmDanger(`Supprimer la question ${i + 1} ?`, 'Son intitulé et ses réponses seront effacés.')) {
              exam.questions.splice(i, 1); renderQuestions(); changed();
            }
          },
        }, icon('trash')),
      ),
    );
    renderOptions();
    return card;
  }

  const counter = h('span', { class: 'muted' });
  const addQuestion = () => {
    exam.questions.push(store.newQuestion());
    renderQuestions(); changed();
    const last = list.lastElementChild;
    last.scrollIntoView({ behavior: 'smooth', block: 'center' });
    last.querySelector('.q-text').focus();
  };

  const isBlank = (q) => !q.html.trim() && q.options.every((o) => !o.html.trim());
  const generateWithAi = () => openAiDialog(exam, (questions, title) => {
    const first = exam.questions.length;
    // un QCM neuf ne contient qu'une question vide : on la remplace
    exam.questions = [...exam.questions.filter((q) => !isBlank(q)), ...questions];
    if (title && (!exam.title.trim() || exam.title === 'Nouveau QCM')) { exam.title = title; titleInput.value = title; }
    renderQuestions(); changed();
    list.children[Math.min(first, exam.questions.length - questions.length)]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  const titleInput = h('input', {
    class: 'title-input', value: exam.title, placeholder: 'Titre du QCM', 'aria-label': 'Titre du QCM',
    oninput: (e) => { exam.title = e.target.value; changed(); },
  });

  view.replaceChildren(
    h('section', { class: 'page editor' },
      h('div', { class: 'page-head' },
        h('a', { href: '#/', class: 'btn ghost small' }, '← Mes QCM'),
        status,
      ),
      titleInput,
      settings,
      formatToolbar(),
      list,
      h('div', { class: 'action-bar' },
        h('button', { class: 'btn', onclick: addQuestion }, icon('plus'), 'Question'),
        h('button', { class: 'btn', onclick: generateWithAi, title: 'Générer des questions avec l\'IA' }, '✨ IA'),
        counter,
        pageInfo,
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn primary', onclick: () => { persist.flush(); pdfDialog(exam, (d) => { densitySel.value = d; changed(); }); } }, icon('file'), 'Générer le PDF'),
      ),
    ),
  );
  renderQuestions();
  if (openAi) generateWithAi();
  refreshPages();

  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); addQuestion(); }
  };
  document.addEventListener('keydown', onKey);
  return () => { persist.flush(); layoutCtrl?.abort(); document.removeEventListener('keydown', onKey); };
}

// ------------------------------------------------------------------ PDF

function validate(exam) {
  const problems = [];
  exam.questions.forEach((q, i) => {
    if (!q.options.some((o) => o.correct)) problems.push(`Question ${i + 1} : aucune bonne réponse`);
    if (!q.html.trim()) problems.push(`Question ${i + 1} : intitulé vide`);
  });
  return problems;
}

async function buildPdf(exam, opts) {
  const { qr, examId } = await encryptExam(exam, store.currentKey());
  const body = {
    qr, ...opts,
    exam: serverExam(exam), // le corrigé n'est PAS envoyé : il n'existe que chiffré dans le QR
  };
  const res = await api('/api/pdf', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const blob = await res.blob();
  exam.qrIds = [...new Set([...(exam.qrIds || []), examId])];
  store.saveExam(exam);
  return blob;
}

/** Choix de la taille du texte, avec le nombre de pages de chaque taille. */
function densityPicker(exam, onChange) {
  const counts = {};
  const seg = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Taille du texte' });
  const render = () => setChildren(seg, DENSITIES.map(([k, label]) => {
    const on = (exam.density || 'normal') === k;
    return h('button', {
      type: 'button', role: 'radio', 'aria-checked': String(on), class: on ? 'on' : '',
      onclick: () => { exam.density = k; onChange(k); render(); },
    }, h('span', {}, label), h('span', { class: 'seg-sub' },
      counts[k] == null ? '…' : `${counts[k]} page${counts[k] > 1 ? 's' : ''}`));
  }));
  render();
  api('/api/layout', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ exam: serverExam(exam), box_size: exam.boxSize === 'large' ? 'large' : 'standard' }),
  }).then((r) => r.json()).then(({ pages }) => { Object.assign(counts, pages); render(); })
    .catch(() => { DENSITIES.forEach(([k]) => { counts[k] = counts[k] ?? '?'; }); render(); });
  return seg;
}

function pdfDialog(exam, onDensity = () => {}) {
  const problems = validate(exam);
  const onSubject = exam.answerMode !== 'grid';
  // mode sujet
  const copies = h('input', { type: 'number', min: 1, max: 200, value: exam.lastCopies || 1 });
  const numbered = h('input', { type: 'checkbox', checked: (exam.lastCopies || 1) > 1 });
  copies.addEventListener('input', () => { numbered.checked = Number(copies.value) > 1; });
  // mode grille
  const subject = h('input', { type: 'checkbox', checked: true });
  const sheets = h('input', { type: 'number', min: 0, max: 200, value: 1 });

  const out = h('div', { class: 'pdf-out' });
  const go = async (action) => {
    out.replaceChildren(h('p', { class: 'muted' }, 'Génération…'));
    try {
      const opts = onSubject
        ? { mode: 'subject', copies: Math.max(1, Number(copies.value) || 1), numbered: numbered.checked, density: exam.density || 'normal' }
        : { mode: 'grid', subject: subject.checked, sheets: Number(sheets.value) || 0 };
      if (onSubject) exam.lastCopies = opts.copies;
      const blob = await buildPdf(exam, opts);
      if (action === 'download') {
        download(blob, `${exam.title || 'qcm'}.pdf`);
        out.replaceChildren(h('p', { class: 'ok' }, 'PDF téléchargé.'));
      } else {
        const url = URL.createObjectURL(blob);
        out.replaceChildren(
          h('iframe', { src: url, class: 'pdf-frame', title: 'Aperçu du PDF' }),
          h('p', {}, h('a', { href: url, target: '_blank', rel: 'noopener' }, 'Ouvrir dans un nouvel onglet')),
        );
      }
    } catch (e) { out.replaceChildren(h('p', { class: 'error' }, e.message)); }
  };
  modal('Générer le PDF', h('div', {},
    problems.length ? h('div', { class: 'alert warn' }, h('strong', {}, 'À vérifier :'), h('ul', {}, problems.slice(0, 8).map((p) => h('li', {}, p)))) : null,
    onSubject
      ? h('div', {},
          h('div', { class: 'field' }, h('span', {}, 'Taille du texte'), densityPicker(exam, onDensity)),
          h('div', { class: 'row wrap' },
            h('label', { class: 'field inline' }, h('span', {}, 'Exemplaires'), copies),
            h('label', { class: 'check' }, numbered, 'Numéroter (un exemplaire par élève)'),
          ),
          h('p', { class: 'muted small' },
            'Numérotés : les pages de chaque élève sont regroupées automatiquement à la correction, dans n\'importe quel ordre. ',
            'Non numéroté (pour photocopier) : photographiez les pages de chaque copie dans l\'ordre, page 1 en premier.'))
      : h('div', { class: 'row wrap' },
          h('label', { class: 'check' }, subject, 'Inclure le sujet'),
          h('label', { class: 'field inline' }, h('span', {}, 'Grilles de réponses'), sheets),
        ),
    h('p', { class: 'muted small' },
      'Le corrigé est chiffré dans le QR code au moment de la génération : si vous modifiez les bonnes réponses ensuite, réimprimez.'),
    h('div', { class: 'row' },
      h('button', { class: 'btn', onclick: () => go('preview') }, icon('eye'), 'Aperçu'),
      h('button', { class: 'btn primary', onclick: () => go('download') }, icon('download'), 'Télécharger'),
    ),
    out,
  ), { wide: true });
}
