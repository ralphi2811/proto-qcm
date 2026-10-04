import * as store from './store.js';
import { formatKey, parseKey, newKeyHex } from './crypto.js';
import { h, icon, toast, download, api, ask } from './ui.js';

export async function handleKeyImport(arg) {
  history.replaceState(null, '', '#/settings'); // retire la clé de l'URL
  window.dispatchEvent(new HashChangeEvent('hashchange')); // affiche les réglages derrière
  let k;
  try { k = parseKey(decodeURIComponent(arg || '')); }
  catch (e) { toast(e.message, 'error'); return; }
  if (store.allKeys()[0] === k) { toast('Cette clé est déjà active', 'info'); return; }
  const ok = await ask({
    title: 'Importer la clé de correction ?',
    message: h('div', {},
      h('p', {}, 'Cet appareil pourra lire les QR codes des copies chiffrées avec cette clé :'),
      h('p', {}, h('code', { class: 'mono key' }, formatKey(k)))),
    confirmLabel: 'Importer la clé',
  });
  if (!ok) return;
  store.setCurrentKey(k);
  toast('Clé importée', 'ok');
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}

export function renderSettings(view) {
  const key = store.currentKey();
  const qrBox = h('div', { class: 'qr-box' });
  const importInput = h('input', { placeholder: 'XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX', class: 'mono' });
  const backupInput = h('input', {
    type: 'file', accept: 'application/json', hidden: true,
    onchange: async (e) => {
      try { store.importAll(JSON.parse(await e.target.files[0].text())); toast('Sauvegarde restaurée', 'ok'); renderSettings(view); }
      catch (err) { toast(err.message, 'error'); }
    },
  });

  const showQr = async () => {
    const link = `${location.origin}/#/import-key/${key}`;
    try {
      const svg = await (await api('/api/qr.svg', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: link }),
      })).text();
      qrBox.innerHTML = svg;
      qrBox.append(h('p', { class: 'muted small' }, "Scannez ce code avec l'appareil photo du smartphone pour y importer la clé."));
    } catch (e) { toast(e.message, 'error'); }
  };

  view.replaceChildren(
    h('section', { class: 'page' },
      h('div', { class: 'page-head' }, h('h1', {}, 'Réglages')),
      h('div', { class: 'card' },
        h('h3', {}, 'Clé de correction'),
        h('p', { class: 'muted' },
          "Les bonnes réponses sont chiffrées dans le QR code de chaque grille avec cette clé. Sans elle, un élève qui scanne le QR ne voit qu'une suite de caractères illisible. ",
          h('strong', {}, "Copiez-la sur chaque appareil de correction et ne la perdez pas.")),
        h('div', { class: 'key-line' },
          h('code', { class: 'mono key' }, formatKey(key)),
          h('button', { class: 'btn ghost icon-btn', title: 'Copier', onclick: () => navigator.clipboard?.writeText(formatKey(key)).then(() => toast('Clé copiée', 'ok')) }, icon('copy')),
        ),
        h('div', { class: 'row wrap' },
          h('button', { class: 'btn', onclick: showQr }, 'Transférer vers un smartphone (QR)'),
          h('button', {
            class: 'btn ghost danger',
            onclick: () => {
              ask({
                title: 'Générer une nouvelle clé ?',
                message: "Les prochains PDF utiliseront la nouvelle clé. L'ancienne reste conservée pour lire les copies déjà imprimées. Pensez à transférer la nouvelle clé sur vos autres appareils.",
                confirmLabel: 'Nouvelle clé', kind: 'warn',
              }).then((ok) => { if (ok) { store.setCurrentKey(newKeyHex()); renderSettings(view); } });
            },
          }, 'Nouvelle clé'),
        ),
        qrBox,
        h('div', { class: 'row wrap' },
          importInput,
          h('button', {
            class: 'btn',
            onclick: () => {
              try { store.setCurrentKey(parseKey(importInput.value)); toast('Clé importée', 'ok'); renderSettings(view); }
              catch (e) { toast(e.message, 'error'); }
            },
          }, 'Importer une clé'),
        ),
        store.allKeys().length > 1 ? h('p', { class: 'muted small' }, `${store.allKeys().length - 1} ancienne(s) clé(s) conservée(s) pour la lecture.`) : null,
      ),
      h('div', { class: 'card' },
        h('h3', {}, 'Sauvegarde'),
        h('p', { class: 'muted' }, 'Tout est stocké dans ce navigateur (aucune base de données). Exportez régulièrement : QCM, résultats et clés.'),
        h('div', { class: 'row wrap' },
          h('button', {
            class: 'btn primary',
            onclick: () => download(new Blob([JSON.stringify(store.exportAll(), null, 2)], { type: 'application/json' }), `qcm-sauvegarde-${new Date().toISOString().slice(0, 10)}.json`),
          }, icon('download'), 'Exporter tout'),
          h('button', { class: 'btn', onclick: () => backupInput.click() }, 'Restaurer'),
          backupInput,
        ),
      ),
      !globalThis.crypto?.subtle ? h('div', { class: 'card alert error' },
        "Cette page n'est pas servie en HTTPS : le chiffrement (WebCrypto) est indisponible. Utilisez https:// ou localhost.") : null,
    ),
  );
}
