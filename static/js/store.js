// Stockage navigateur (localStorage). Suffisant pour le POC ; à migrer vers IndexedDB
// si les QCM contiennent beaucoup d'images (quota ~5 Mo).
import { newKeyHex } from './crypto.js';
import { notify } from './ui.js';

const K = { exams: 'qcm.exams', results: 'qcm.results', settings: 'qcm.settings', rosters: 'qcm.rosters' };

function load(k, def) {
  try { return JSON.parse(localStorage.getItem(k)) ?? def; } catch { return def; }
}
function save(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); }
  catch (e) {
    notify('Stockage du navigateur plein', 'Supprimez des images ou des QCM, ou exportez une sauvegarde puis faites du ménage.', 'warn');
    throw e;
  }
}

export const uid = () => crypto.randomUUID?.() ?? Math.random().toString(36).slice(2) + Date.now().toString(36);

// ---------------------------------------------------------------- QCM
export function listExams() {
  return Object.values(load(K.exams, {})).sort((a, b) => b.updatedAt - a.updatedAt);
}
export function getExam(id) { return load(K.exams, {})[id] ?? null; }
export function saveExam(exam) {
  const all = load(K.exams, {});
  exam.updatedAt = Date.now();
  all[exam.id] = exam;
  save(K.exams, all);
}
export function deleteExam(id) {
  const all = load(K.exams, {});
  delete all[id];
  save(K.exams, all);
}
export function findExamByQrId(qrId) {
  return listExams().find((e) => (e.qrIds || []).includes(qrId)) ?? null;
}

export function newQuestion() {
  return {
    id: uid(), html: '', points: 1,
    options: [{ html: '', correct: true }, { html: '', correct: false }, { html: '', correct: false }, { html: '', correct: false }],
  };
}
export function newExam() {
  return {
    id: uid(), title: 'Nouveau QCM', subtitle: '', instructions: '',
    scoring: 'strict', noteSur: 20, boxSize: 'large', answerMode: 'subject', questions: [newQuestion()], qrIds: [],
    createdAt: Date.now(), updatedAt: Date.now(),
  };
}

// ---------------------------------------------------------------- résultats
export function listResults() { return load(K.results, []); }
export function addResult(r) { const all = listResults(); all.push(r); save(K.results, all); }
export function deleteResult(id) { save(K.results, listResults().filter((r) => r.id !== id)); }

// ---------------------------------------------------------------- réglages / clés
export function settings() {
  const s = load(K.settings, {});
  if (!s.keys?.length) { s.keys = [newKeyHex()]; save(K.settings, s); }
  return s;
}
export function currentKey() { return settings().keys[0]; }
export function allKeys() { return settings().keys; }
export function setCurrentKey(h) {
  const s = settings();
  s.keys = [h, ...s.keys.filter((k) => k !== h)]; // les anciennes clés restent utilisables en lecture
  save(K.settings, s);
}

// ---------------------------------------------------------------- listes de classe
// { "CM2 A": [{ nom: "DUPONT", prenom: "Léa" }, …] }
export function rosters() { return load(K.rosters, {}); }
export function getRoster(name) { return rosters()[name] ?? null; }
export function saveRoster(name, students, oldName = name) {
  const all = rosters();
  if (oldName !== name) delete all[oldName];
  all[name] = students;
  save(K.rosters, all);
}
export function deleteRoster(name) { const all = rosters(); delete all[name]; save(K.rosters, all); }

/**
 * Une ligne par élève, collée depuis un tableur ou tapée :
 * "DUPONT;Léa", "DUPONT<tab>Léa", "DUPONT Léa" (nom en majuscules), "Léa DUPONT", ou "Dupont Léa"
 * (sans majuscules : premier mot = nom).
 */
export function parseRoster(text) {
  const out = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    let nom, prenom;
    const parts = line.split(/\s*[;\t,]\s*/).filter(Boolean);
    if (parts.length >= 2) [nom, prenom] = parts;
    else {
      const words = line.split(/\s+/);
      const upper = words.filter((w) => /\p{L}/u.test(w) && w === w.toUpperCase());
      if (upper.length && upper.length < words.length) {
        nom = upper.join(' ');
        prenom = words.filter((w) => !upper.includes(w)).join(' ');
      } else { [nom, ...prenom] = words; prenom = prenom.join(' '); }
    }
    out.push({ nom: nom.toUpperCase(), prenom: prenom || '' });
  }
  return out;
}
export const studentLabel = (s) => [s.nom, s.prenom].filter(Boolean).join(' ');

// ---------------------------------------------------------------- sauvegarde
export function exportAll() {
  return { app: 'proto-qcm', version: 1, exams: load(K.exams, {}), results: listResults(), keys: allKeys(), rosters: rosters() };
}
export function importAll(data) {
  if (data?.app !== 'proto-qcm') throw new Error('Fichier non reconnu');
  save(K.exams, { ...load(K.exams, {}), ...data.exams });
  const ids = new Set(listResults().map((r) => r.id));
  save(K.results, [...listResults(), ...(data.results || []).filter((r) => !ids.has(r.id))]);
  save(K.rosters, { ...rosters(), ...(data.rosters || {}) });
  const s = settings();
  s.keys = [...new Set([...s.keys, ...(data.keys || [])])];
  save(K.settings, s);
}
