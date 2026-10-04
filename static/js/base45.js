// Base45 (RFC 9285) : alphabet = mode alphanumérique des QR codes -> encodage dense.
const ALPHA = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:';
const IDX = Object.fromEntries([...ALPHA].map((c, i) => [c, i]));

export function b45encode(bytes) {
  let out = '';
  for (let i = 0; i < bytes.length; i += 2) {
    if (i + 1 < bytes.length) {
      let n = (bytes[i] << 8) | bytes[i + 1];
      const c = Math.floor(n / 2025); n %= 2025;
      out += ALPHA[n % 45] + ALPHA[Math.floor(n / 45)] + ALPHA[c];
    } else {
      const n = bytes[i];
      out += ALPHA[n % 45] + ALPHA[Math.floor(n / 45)];
    }
  }
  return out;
}

export function b45decode(str) {
  const out = [];
  for (let i = 0; i < str.length; i += 3) {
    const ch = [...str.slice(i, i + 3)].map((c) => {
      if (!(c in IDX)) throw new Error('Base45 invalide');
      return IDX[c];
    });
    if (ch.length === 3) {
      const n = ch[0] + ch[1] * 45 + ch[2] * 2025;
      if (n > 0xffff) throw new Error('Base45 invalide');
      out.push(n >> 8, n & 0xff);
    } else if (ch.length === 2) {
      const n = ch[0] + ch[1] * 45;
      if (n > 0xff) throw new Error('Base45 invalide');
      out.push(n);
    } else throw new Error('Base45 invalide');
  }
  return new Uint8Array(out);
}
