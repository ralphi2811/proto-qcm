// Génération de questions par IA (via le serveur -> OpenRouter), avec relecture avant ajout.
import * as store from './store.js';
import { h, icon, toast, modal, api, escapeHtml } from './ui.js';

const PREFS = 'qcm.ai';
const loadPrefs = () => { try { return JSON.parse(localStorage.getItem(PREFS)) || {}; } catch { return {}; } };
const savePrefs = (p) => { try { localStorage.setItem(PREFS, JSON.stringify({ ...loadPrefs(), ...p })); } catch {} };

const ACCEPT = '.pdf,.txt,.md,image/*';
const fmtSize = (n) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} Mo` : `${Math.ceil(n / 1024)} Ko`);

/** Ouvre la fenêtre de génération. onAdd(questions, title) reçoit les questions retenues. */
export async function openAiDialog(exam, onAdd) {
  let status;
  try { status = await (await api('/api/ai/status')).json(); }
  catch (e) { toast(e.message, 'error'); return; }

  if (!status.enabled) {
    modal('Générer avec l\'IA', h('div', {},
      h('p', {}, 'La génération par IA n\'est pas configurée sur ce serveur.'),
      h('p', { class: 'muted' }, 'Copiez ', h('code', {}, '.env.example'), ' en ', h('code', {}, '.env'),
        ', renseignez ', h('code', {}, 'OPENROUTER_API_KEY'), ' puis redémarrez le serveur.'),
    ));
    return;
  }

  const prefs = loadPrefs();
  const files = [];
  const counts = exam.questions.map((q) => q.options.length);
  const defaultOpts = prefs.n_options || (counts.length ? Math.max(...counts) : 4);

  const prompt = h('textarea', {
    rows: 4, class: 'ai-prompt',
    placeholder: 'Ex. : 10 questions sur les fractions (addition, simplification), vocabulaire simple. Ou : « QCM sur le document joint ».',
  });
  const nq = h('input', { type: 'number', min: 1, max: 60, value: prefs.n_questions || 10 });
  const nopt = h('input', { type: 'number', min: 2, max: 8, value: defaultOpts });
  const level = h('input', { value: prefs.level ?? 'CM2', placeholder: 'CM2, 4e, BTS…' });
  const multiple = h('input', { type: 'checkbox', checked: !!prefs.multiple });
  const code = status.needs_code ? h('input', { type: 'password', value: prefs.code || '', placeholder: 'Code d\'accès IA', autocomplete: 'off' }) : null;

  const chips = h('div', { class: 'chips' });
  const renderChips = () => chips.replaceChildren(...files.map((f, i) =>
    h('span', { class: 'chip' }, icon(f.type.startsWith('image/') ? 'image' : 'file'), `${f.name} · ${fmtSize(f.size)}`,
      h('button', { type: 'button', class: 'chip-x', 'aria-label': `Retirer ${f.name}`, onclick: () => { files.splice(i, 1); renderChips(); } }, '×'))));
  const fileInput = h('input', {
    type: 'file', multiple: true, accept: ACCEPT, hidden: true,
    onchange: (e) => { files.push(...e.target.files); e.target.value = ''; renderChips(); },
  });
  const drop = h('div', {
    class: 'dropzone', tabindex: 0, role: 'button',
    onclick: () => fileInput.click(),
    onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } },
    ondragover: (e) => { e.preventDefault(); drop.classList.add('over'); },
    ondragleave: () => drop.classList.remove('over'),
    ondrop: (e) => { e.preventDefault(); drop.classList.remove('over'); files.push(...e.dataTransfer.files); renderChips(); },
  }, icon('file'), h('span', {}, 'Ajouter des documents ', h('span', { class: 'muted' }, '(PDF, texte, photos de cours — facultatif)')));

  const out = h('div', { class: 'ai-out' });
  const genBtn = h('button', { class: 'btn primary', onclick: () => run() }, '✨ Générer');
  let ctrl = null;

  const form = h('div', { class: 'ai-form' },
    prompt, drop, chips, fileInput,
    h('div', { class: 'grid3' },
      h('label', { class: 'field' }, h('span', {}, 'Questions'), nq),
      h('label', { class: 'field' }, h('span', {}, 'Propositions'), nopt),
      h('label', { class: 'field' }, h('span', {}, 'Niveau'), level),
    ),
    h('label', { class: 'check' }, multiple, 'Plusieurs bonnes réponses possibles'),
    code ? h('label', { class: 'field' }, h('span', {}, 'Code d\'accès'), code) : null,
    h('div', { class: 'row' }, genBtn, h('span', { class: 'muted small' }, `Modèle : ${status.model}`)),
  );
  const dlg = modal('Générer avec l\'IA', h('div', {}, form, out), { wide: true });
  dlg.addEventListener('close', () => ctrl?.abort());
  prompt.focus();

  async function run() {
    const opts = {
      prompt: prompt.value, n_questions: Number(nq.value) || 10, n_options: Number(nopt.value) || 4,
      multiple: multiple.checked, level: level.value.trim(),
    };
    if (!opts.prompt.trim() && !files.length) { toast('Écrivez une consigne ou ajoutez un document', 'warn'); return; }
    savePrefs({ n_questions: opts.n_questions, n_options: opts.n_options, multiple: opts.multiple, level: opts.level, ...(code ? { code: code.value } : {}) });

    const fd = new FormData();
    fd.append('options', JSON.stringify(opts));
    files.forEach((f) => fd.append('files', f, f.name));
    ctrl = new AbortController();
    genBtn.disabled = true;
    out.replaceChildren(h('div', { class: 'busy' }, h('span', { class: 'spinner' }),
      h('span', {}, 'Génération en cours… (jusqu\'à une minute avec des documents)'),
      h('button', { class: 'btn ghost small', onclick: () => ctrl.abort() }, 'Annuler')));
    try {
      const res = await api('/api/ai/generate', {
        method: 'POST', body: fd, signal: ctrl.signal, headers: code ? { 'X-AI-Code': code.value } : {},
      });
      review(await res.json());
    } catch (e) {
      out.replaceChildren(h('div', { class: 'alert error' }, ctrl.signal.aborted ? 'Génération annulée.' : e.message));
    } finally {
      genBtn.disabled = false;
      ctrl = null;
    }
  }

  function review(res) {
    const keep = res.questions.map(() => true);
    const addBtn = h('button', { class: 'btn primary' });
    const refresh = () => {
      const n = keep.filter(Boolean).length;
      addBtn.disabled = n === 0;
      addBtn.replaceChildren(icon('plus'), `Ajouter ${n} question${n > 1 ? 's' : ''}`);
    };
    addBtn.onclick = () => {
      const chosen = res.questions.filter((_, i) => keep[i]).map((q) => ({
        id: store.uid(), points: 1,
        html: escapeHtml(q.question),
        options: q.options.map((o) => ({ html: escapeHtml(o.text), correct: !!o.correct })),
      }));
      onAdd(chosen, res.title);
      toast(`${chosen.length} question${chosen.length > 1 ? 's' : ''} ajoutée${chosen.length > 1 ? 's' : ''} — relisez les bonnes réponses`, 'ok');
      dlg.close();
    };
    refresh();

    out.replaceChildren(...[
      res.warnings?.length ? h('div', { class: 'alert warn' }, h('ul', {}, res.warnings.map((w) => h('li', {}, w)))) : null,
      h('p', { class: 'muted small' },
        'Vérifiez chaque bonne réponse (✓) : l\'IA peut se tromper. Décochez les questions à écarter ; tout reste modifiable ensuite.'),
      h('div', { class: 'ai-review' }, res.questions.map((q, i) => {
        const box = h('input', { type: 'checkbox', checked: true, onchange: (e) => { keep[i] = e.target.checked; card.classList.toggle('off', !keep[i]); refresh(); } });
        const card = h('label', { class: 'ai-q' },
          box,
          h('div', {},
            h('div', { class: 'ai-q-title' }, `${i + 1}. ${q.question}`),
            h('ol', { class: 'ai-opts' }, q.options.map((o) => h('li', { class: o.correct ? 'ok' : '' }, o.correct ? '✓ ' : '', o.text))),
            q.explanation ? h('div', { class: 'muted small ai-expl' }, q.explanation) : null,
          ));
        return card;
      })),
      h('div', { class: 'row end' },
        res.usage?.cost != null ? h('span', { class: 'muted small spacer' }, `Coût : ${Number(res.usage.cost).toFixed(4)} $`) : null,
        h('button', { class: 'btn', onclick: () => run() }, 'Régénérer'),
        addBtn,
      ),
    ].filter(Boolean));
    out.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}
