import * as store from './store.js';
import { h, icon, toast, download, confirmDanger } from './ui.js';
import { renderEditor } from './editor.js';
import { renderScanner } from './scanner.js';
import { renderResults } from './results.js';
import { renderSettings, handleKeyImport } from './settings.js';

const view = document.getElementById('view');
let cleanup = null;

function renderExamList() {
  const exams = store.listExams();
  const fileInput = h('input', {
    type: 'file', accept: 'application/json', hidden: true,
    onchange: async (e) => {
      const f = e.target.files[0];
      if (!f) return;
      try {
        const data = JSON.parse(await f.text());
        if (data.app === 'proto-qcm') store.importAll(data);
        else if (data.questions) store.saveExam({ ...data, id: store.uid() });
        else throw new Error('Format inconnu');
        toast('Import réussi', 'ok');
        route();
      } catch (err) { toast(err.message, 'error'); }
    },
  });

  view.replaceChildren(
    h('section', { class: 'page' },
      h('div', { class: 'page-head' },
        h('h1', {}, 'Mes QCM'),
        h('div', { class: 'row' },
          h('button', { class: 'btn ghost', onclick: () => fileInput.click() }, icon('file'), 'Importer'),
          h('button', {
            class: 'btn',
            onclick: () => { const e = store.newExam(); store.saveExam(e); location.hash = `#/edit/${e.id}/ai`; },
          }, '✨ Créer avec l\'IA'),
          h('button', {
            class: 'btn primary',
            onclick: () => { const e = store.newExam(); store.saveExam(e); location.hash = `#/edit/${e.id}`; },
          }, icon('plus'), 'Nouveau QCM'),
        ),
        fileInput,
      ),
      exams.length === 0
        ? h('div', { class: 'empty' },
            h('p', {}, 'Aucun QCM pour le moment.'),
            h('p', { class: 'muted' }, 'Vos QCM sont stockés uniquement dans ce navigateur. Pensez à exporter une sauvegarde (Réglages).'))
        : h('div', { class: 'cards' }, exams.map((e) =>
            h('article', { class: 'card exam-card' },
              h('a', { href: `#/edit/${e.id}`, class: 'exam-link' },
                h('h3', {}, e.title || 'Sans titre'),
                h('p', { class: 'muted' }, `${e.questions.length} question${e.questions.length > 1 ? 's' : ''}`,
                  e.subtitle ? ` · ${e.subtitle}` : ''),
                h('p', { class: 'muted small' }, `Modifié le ${new Date(e.updatedAt).toLocaleString('fr-FR')}`)),
              h('div', { class: 'row end' },
                h('button', {
                  class: 'btn ghost icon-btn', title: 'Dupliquer',
                  onclick: () => { store.saveExam({ ...structuredClone(e), id: store.uid(), title: `${e.title} (copie)`, qrIds: [] }); route(); },
                }, icon('copy')),
                h('button', {
                  class: 'btn ghost icon-btn', title: 'Exporter (JSON)',
                  onclick: () => download(new Blob([JSON.stringify(e, null, 2)], { type: 'application/json' }), `${e.title || 'qcm'}.json`),
                }, icon('download')),
                h('button', {
                  class: 'btn ghost icon-btn danger', title: 'Supprimer',
                  onclick: async () => {
                    if (await confirmDanger(`Supprimer « ${e.title} » ?`, 'Le QCM sera effacé de ce navigateur. Les résultats déjà enregistrés sont conservés.')) {
                      store.deleteExam(e.id); route();
                    }
                  },
                }, icon('trash')),
              ),
            ))),
    ),
  );
}

function route() {
  if (cleanup) { cleanup(); cleanup = null; }
  const [, page = '', arg, extra] = location.hash.split('/');
  const tab = { '': 'exams', edit: 'exams', scan: 'scan', results: 'results', settings: 'settings' }[page] ?? 'exams';
  document.querySelectorAll('#tabs a').forEach((a) => a.classList.toggle('active', a.dataset.tab === tab));
  document.body.dataset.page = page || 'home';

  if (page === 'import-key') { handleKeyImport(arg); return; }
  if (page === 'edit') {
    const exam = store.getExam(arg);
    if (!exam) { location.hash = '#/'; return; }
    cleanup = renderEditor(view, exam, { openAi: extra === 'ai' });
    if (extra === 'ai') history.replaceState(null, '', `#/edit/${exam.id}`);
  } else if (page === 'scan') cleanup = renderScanner(view);
  else if (page === 'results') renderResults(view);
  else if (page === 'settings') renderSettings(view);
  else renderExamList();
  window.scrollTo(0, 0);
}

window.addEventListener('hashchange', route);
store.settings(); // crée la clé au premier lancement
route();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
