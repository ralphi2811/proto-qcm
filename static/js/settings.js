import * as store from './store.js';
import { formatKey, parseKey, newKeyHex } from './crypto.js';
import { h, icon, toast, download, api, ask, modal, confirmDanger } from './ui.js';

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

/** Création / modification d'une liste de classe. */
function editRoster(name, onDone) {
  const students = name ? store.getRoster(name) ?? [] : [];
  const nameInput = h('input', { value: name || '', placeholder: 'CM2 A' });
  const text = h('textarea', {
    rows: 12, class: 'mono',
    placeholder: 'Un élève par ligne, par exemple :\nDUPONT Léa\nMARTIN;Hugo\n(copier-coller depuis un tableur possible)',
  });
  text.value = students.map((s) => `${s.nom}\t${s.prenom}`).join('\n');
  const preview = h('p', { class: 'muted small' });
  const refresh = () => {
    const list = store.parseRoster(text.value);
    preview.textContent = list.length
      ? `${list.length} élève${list.length > 1 ? 's' : ''} : ${list.slice(0, 4).map(store.studentLabel).join(', ')}${list.length > 4 ? '…' : ''}`
      : 'Aucun élève.';
  };
  text.addEventListener('input', refresh);
  refresh();
  const dlg = modal(name ? 'Modifier la liste' : 'Nouvelle liste de classe', h('div', { class: 'stack' },
    h('label', { class: 'field' }, h('span', {}, 'Nom de la liste (sert aussi de « Classe »)'), nameInput),
    h('label', { class: 'field' }, h('span', {}, 'Élèves'), text),
    preview,
    h('div', { class: 'row end' },
      h('button', { class: 'btn ghost', onclick: () => dlg.close() }, 'Annuler'),
      h('button', {
        class: 'btn primary',
        onclick: () => {
          const n = nameInput.value.trim();
          const list = store.parseRoster(text.value);
          if (!n) { toast('Donnez un nom à la liste', 'warn'); return; }
          if (!list.length) { toast('Ajoutez au moins un élève', 'warn'); return; }
          if (n !== name && store.getRoster(n)) { toast('Une liste porte déjà ce nom', 'warn'); return; }
          store.saveRoster(n, list, name || n);
          dlg.close();
          onDone();
        },
      }, 'Enregistrer'),
    ),
  ));
  (name ? text : nameInput).focus();
}

/** Clé contenue dans un QR de transfert (lien #/import-key/…) ou une clé tapée telle quelle. */
function keyFromQrText(text) {
  const m = text.match(/import-key\/([0-9a-fA-F-]+)/);
  return parseKey(m ? m[1] : text);
}

/**
 * Scan du QR de transfert affiché par un autre appareil : caméra en direct (BarcodeDetector,
 * Chrome Android) ; sinon photo décodée sur l'appareil ou par le serveur.
 */
function scanKeyDialog() {
  const detector = 'BarcodeDetector' in window ? new window.BarcodeDetector({ formats: ['qr_code'] }) : null;
  const video = h('video', { class: 'qr-video', playsinline: true, muted: true, autoplay: true });
  const status = h('p', { class: 'muted small' });
  let stream = null;
  let done = false;
  const photo = h('input', {
    type: 'file', accept: 'image/*', capture: 'environment', hidden: true,
    onchange: async (e) => {
      const file = e.target.files[0];
      e.target.value = '';
      if (!file) return;
      status.textContent = 'Lecture de la photo…';
      try {
        let text = null;
        if (detector) {
          const codes = await detector.detect(await createImageBitmap(file));
          text = codes[0]?.rawValue ?? null;
        }
        if (!text) {
          const fd = new FormData();
          fd.append('image', file, 'qr.jpg');
          text = (await (await api('/api/qr/decode', { method: 'POST', body: fd })).json()).text;
        }
        found(text);
      } catch (err) { status.textContent = err.message; }
    },
  });
  const dlg = modal('Scanner la clé d\'un autre appareil', h('div', { class: 'stack' },
    h('p', { class: 'muted' }, 'Sur l\'autre appareil : Réglages → « Transférer vers un smartphone (QR) », puis visez le QR code.'),
    detector && navigator.mediaDevices?.getUserMedia ? video : null,
    status,
    h('div', { class: 'row wrap' },
      h('button', { class: 'btn', onclick: () => photo.click() }, icon('camera'), 'Prendre une photo du QR'),
    ),
    photo,
  ));
  const stop = () => { done = true; stream?.getTracks().forEach((t) => t.stop()); };
  dlg.addEventListener('close', stop);

  function found(text) {
    let k;
    try { k = keyFromQrText(text); }
    catch { status.textContent = 'Ce QR code ne contient pas de clé de correction.'; return; }
    stop();
    dlg.close();
    handleKeyImport(k);
  }

  if (detector && navigator.mediaDevices?.getUserMedia) {
    status.textContent = 'Ouverture de la caméra…';
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
      .then(async (s) => {
        if (done) { s.getTracks().forEach((t) => t.stop()); return; }
        stream = s;
        video.srcObject = s;
        await video.play().catch(() => {});
        status.textContent = 'Visez le QR code…';
        const tick = async () => {
          if (done) return;
          try {
            const codes = video.readyState >= 2 ? await detector.detect(video) : [];
            if (codes[0]?.rawValue) { found(codes[0].rawValue); if (done) return; }
          } catch {}
          setTimeout(tick, 250);
        };
        tick();
      })
      .catch(() => { video.remove(); status.textContent = 'Caméra indisponible : prenez une photo du QR code.'; });
  } else {
    status.textContent = 'Scan en direct non pris en charge par ce navigateur : prenez une photo du QR code.';
  }
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
          h('button', { class: 'btn', onclick: scanKeyDialog }, icon('camera'), 'Scanner la clé d\'un autre appareil'),
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
        h('h3', {}, 'Listes de classe'),
        h('p', { class: 'muted' },
          "À la correction, le nom écrit par l'élève est lu par l'IA puis rapproché de la liste choisie : ",
          "les fautes et les écritures difficiles sont rattrapées."),
        h('div', { class: 'roster-list' },
          Object.entries(store.rosters()).map(([name, list]) => h('div', { class: 'roster-row' },
            h('div', {}, h('strong', {}, name), h('span', { class: 'muted small' }, ` · ${list.length} élève${list.length > 1 ? 's' : ''}`)),
            h('div', { class: 'row' },
              h('button', { class: 'btn ghost icon-btn', title: 'Modifier', onclick: () => editRoster(name, () => renderSettings(view)) }, icon('edit')),
              h('button', {
                class: 'btn ghost icon-btn danger', title: 'Supprimer',
                onclick: async () => {
                  if (await confirmDanger(`Supprimer la liste « ${name} » ?`, 'Les résultats déjà enregistrés ne sont pas touchés.')) {
                    store.deleteRoster(name); renderSettings(view);
                  }
                },
              }, icon('trash')),
            )))),
        h('button', { class: 'btn', onclick: () => editRoster(null, () => renderSettings(view)) }, icon('plus'), 'Nouvelle liste'),
      ),
      h('div', { class: 'card' },
        h('h3', {}, 'Sauvegarde'),
        h('p', { class: 'muted' }, 'Tout est stocké dans ce navigateur (aucune base de données). Exportez régulièrement : QCM, résultats, listes de classe et clés.'),
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
