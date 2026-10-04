import * as store from './store.js';
import { h, icon, download, modal, confirmDanger } from './ui.js';

const L = (i) => String.fromCharCode(65 + i);
const who = (r) => [r.nom, r.prenom].filter(Boolean).join(' ') || (r.copyNo ? `copie n° ${r.copyNo}` : '');
const fmt = (r) => (r.note != null ? `${r.note} / ${r.noteSur}` : `${r.total} / ${r.max}`);

function toCsv(rows) {
  const n = Math.max(0, ...rows.map((r) => r.details.length));
  const head = ['QCM', 'Nom', 'Prénom', 'Classe', 'Date', 'Points', 'Max', 'Note', 'Sur', ...Array.from({ length: n }, (_, i) => `Q${i + 1}`)];
  const esc = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const lines = rows.map((r) => [
    r.examTitle, r.nom, r.prenom, r.classe, new Date(r.date).toLocaleString('fr-FR'), r.total, r.max, r.note ?? '', r.noteSur ?? '',
    ...r.details.map((d) => d.marked.map((m, i) => (m ? L(i) : '')).join('')),
  ].map(esc).join(';'));
  return '﻿' + [head.map(esc).join(';'), ...lines].join('\r\n');
}

function detail(r) {
  modal(`${who(r) || 'Copie'} — ${fmt(r)}`, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Q'), h('th', {}, 'Cochées'), h('th', {}, 'Attendues'), h('th', {}, 'Points'))),
    h('tbody', {}, r.details.map((d, i) => {
      const got = d.marked.map((m, k) => (m ? L(k) : '')).join('') || '—';
      const exp = d.correct.map((c, k) => (c ? L(k) : '')).join('');
      return h('tr', { class: d.points >= d.max ? 'ok' : d.points > 0 ? 'mid' : 'ko' },
        h('td', {}, i + 1), h('td', {}, got), h('td', {}, exp), h('td', {}, `${d.points} / ${d.max}`));
    })),
  ));
}

export function renderResults(view) {
  const all = store.listResults().sort((a, b) => b.date - a.date);
  const groups = new Map();
  for (const r of all) {
    const k = r.examTitle || 'Sans titre';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }

  view.replaceChildren(
    h('section', { class: 'page' },
      h('div', { class: 'page-head' },
        h('h1', {}, 'Résultats'),
        all.length ? h('button', {
          class: 'btn', onclick: () => download(new Blob([toCsv(all)], { type: 'text/csv' }), 'resultats-qcm.csv'),
        }, icon('download'), 'Exporter CSV') : null,
      ),
      all.length === 0 ? h('div', { class: 'empty' }, h('p', {}, 'Aucune copie corrigée.')) : null,
      [...groups].map(([title, rows]) => {
        const notes = rows.map((r) => (r.note != null ? r.note : r.total));
        const avg = notes.reduce((a, b) => a + b, 0) / notes.length;
        return h('div', { class: 'card' },
          h('div', { class: 'row between' },
            h('h3', {}, title),
            h('span', { class: 'muted' }, `${rows.length} copie${rows.length > 1 ? 's' : ''} · moyenne ${avg.toFixed(2)}`),
          ),
          h('table', { class: 'table' },
            h('thead', {}, h('tr', {}, h('th', {}, 'Élève'), h('th', {}, 'Classe'), h('th', {}, 'Note'), h('th', {}, 'Date'), h('th', {}))),
            h('tbody', {}, rows.map((r) => h('tr', {},
              h('td', {}, who(r) || '—'),
              h('td', {}, r.classe || ''),
              h('td', {}, h('button', { class: 'link', onclick: () => detail(r) }, fmt(r))),
              h('td', { class: 'muted small' }, new Date(r.date).toLocaleString('fr-FR')),
              h('td', {}, h('button', {
                class: 'btn ghost icon-btn small danger', title: 'Supprimer',
                onclick: async () => {
                  if (await confirmDanger('Supprimer ce résultat ?', `${who(r) || 'Copie'} — ${fmt(r)}`)) {
                    store.deleteResult(r.id); renderResults(view);
                  }
                },
              }, icon('trash'))),
            ))),
          ),
        );
      }),
    ),
  );
}
