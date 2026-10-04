// Contenu chiffré du QR code (voir app/payload.py pour le format).
//
// en-tête clair (AAD) : [version, nbQ, nbOptMax, tailleCases (0 std, 1 grande), examId x4]
// puis IV (12 o) puis AES-GCM-128 (tag 64 bits) de :
//   [mode, noteSur, flags] + cases correctes (nbOptMax bits / question)
//   + (flags&1) points*2 sur 1 o / question
//   + (flags&2) nb options par question (4 bits / question)
import { b45encode, b45decode } from './base45.js';

export const VERSION = 1;
export const BOX_SIZES = { standard: 0, large: 1 };
const TAG_BITS = 64;
const MODES = ['strict', 'partial'];

const hex = (u8) => [...u8].map((b) => b.toString(16).padStart(2, '0')).join('');
const unhex = (s) => new Uint8Array(s.match(/../g).map((h) => parseInt(h, 16)));

export function newKeyHex() {
  return hex(crypto.getRandomValues(new Uint8Array(16)));
}

export function formatKey(h) {
  return h.toUpperCase().match(/.{4}/g).join('-');
}

export function parseKey(text) {
  const h = text.replace(/[^0-9a-f]/gi, '').toLowerCase();
  if (h.length !== 32) throw new Error('Clé invalide (32 caractères hexadécimaux attendus)');
  return h;
}

function ensureSubtle() {
  if (!globalThis.crypto?.subtle) {
    throw new Error("WebCrypto indisponible : l'application doit être servie en HTTPS (ou sur localhost)");
  }
}

async function importKey(h) {
  ensureSubtle();
  return crypto.subtle.importKey('raw', unhex(h), 'AES-GCM', false, ['encrypt', 'decrypt']);
}

class BitWriter {
  constructor() { this.bytes = []; this.bit = 0; }
  write(value, n) {
    for (let i = n - 1; i >= 0; i--) {
      if (this.bit % 8 === 0) this.bytes.push(0);
      if ((value >> i) & 1) this.bytes[this.bytes.length - 1] |= 0x80 >> (this.bit % 8);
      this.bit++;
    }
  }
}

class BitReader {
  constructor(u8, offset = 0) { this.u8 = u8; this.bit = offset * 8; }
  read(n) {
    let v = 0;
    for (let i = 0; i < n; i++, this.bit++) {
      const byte = this.u8[this.bit >> 3];
      if (byte === undefined) throw new Error('Données tronquées');
      v = (v << 1) | ((byte >> (7 - (this.bit & 7))) & 1);
    }
    return v;
  }
  get bytePos() { return Math.ceil(this.bit / 8); }
}

/** Corrigé normalisé, utilisé pour la notation (depuis un QR ou depuis un QCM local). */
export function keyFromExam(exam) {
  return {
    mode: exam.scoring || 'strict',
    noteSur: Number(exam.noteSur) || 0,
    questions: exam.questions.map((q) => ({
      nOpt: q.options.length,
      correct: q.options.map((o) => !!o.correct),
      points: Number(q.points) || 1,
    })),
  };
}

export async function encryptExam(exam, keyHex) {
  const key = keyFromExam(exam);
  const qs = key.questions;
  const nQ = qs.length;
  const nOpt = Math.max(...qs.map((q) => q.nOpt));
  const examId = crypto.getRandomValues(new Uint8Array(4));
  const header = new Uint8Array([VERSION, nQ, nOpt, BOX_SIZES[exam.boxSize] ?? 0, ...examId]);

  const customPoints = qs.some((q) => q.points !== 1);
  const varOpts = qs.some((q) => q.nOpt !== nOpt);
  const w = new BitWriter();
  w.write(MODES.indexOf(key.mode), 8);
  w.write(Math.min(255, key.noteSur), 8);
  w.write((customPoints ? 1 : 0) | (varOpts ? 2 : 0), 8);
  for (const q of qs) for (let o = 0; o < nOpt; o++) w.write(q.correct[o] ? 1 : 0, 1);
  if (customPoints) for (const q of qs) w.write(Math.max(0, Math.min(255, Math.round(q.points * 2))), 8);
  if (varOpts) for (const q of qs) w.write(q.nOpt, 4);
  const plain = new Uint8Array(w.bytes);

  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ck = await importKey(keyHex);
  const ct = new Uint8Array(
    await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: header, tagLength: TAG_BITS }, ck, plain),
  );
  const blob = new Uint8Array(header.length + iv.length + ct.length);
  blob.set(header, 0); blob.set(iv, 8); blob.set(ct, 20);
  return { qr: b45encode(blob), examId: hex(examId) };
}

const PAGE_FORMAT = 2; // QR d'une page de sujet : partie « page » (positions des cases) puis blob

export function parseHeader(text) {
  let raw = b45decode(text.trim());
  if (raw[0] === PAGE_FORMAT) raw = raw.slice(raw[1]); // la partie page est lue par le serveur
  if (raw[0] !== VERSION) throw new Error(`Version de QR inconnue (${raw[0]})`);
  return { nQ: raw[1], nOpt: raw[2], style: raw[3], examId: hex(raw.slice(4, 8)), raw };
}

/** Essaie chaque clé du trousseau ; renvoie le corrigé ou lève une erreur. */
export async function decryptQr(text, keyHexes) {
  const h = parseHeader(text);
  const header = h.raw.slice(0, 8);
  const iv = h.raw.slice(8, 20);
  const ct = h.raw.slice(20);
  let plain = null;
  for (const k of keyHexes) {
    try {
      const ck = await importKey(k);
      plain = new Uint8Array(
        await crypto.subtle.decrypt({ name: 'AES-GCM', iv, additionalData: header, tagLength: TAG_BITS }, ck, ct),
      );
      break;
    } catch (e) {
      if (e.message?.includes('WebCrypto')) throw e;
    }
  }
  if (!plain) throw new Error("Ce QR a été chiffré avec une autre clé (importez la clé de l'enseignant)");

  const r = new BitReader(plain);
  const mode = MODES[r.read(8)] || 'strict';
  const noteSur = r.read(8);
  const flags = r.read(8);
  const correct = [];
  for (let q = 0; q < h.nQ; q++) {
    const row = [];
    for (let o = 0; o < h.nOpt; o++) row.push(!!r.read(1));
    correct.push(row);
  }
  let points = Array(h.nQ).fill(1);
  if (flags & 1) points = points.map(() => r.read(8) / 2);
  let nOpts = Array(h.nQ).fill(h.nOpt);
  if (flags & 2) nOpts = nOpts.map(() => r.read(4));
  return {
    examId: h.examId,
    mode,
    noteSur,
    questions: correct.map((c, i) => ({ nOpt: nOpts[i], correct: c.slice(0, nOpts[i]), points: points[i] })),
  };
}
