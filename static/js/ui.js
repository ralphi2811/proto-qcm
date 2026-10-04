// Mini-helpers DOM (pas de framework, pas de build).

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'html') el.innerHTML = v;
    else if (k in el && typeof v !== 'string') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** replaceChildren qui ignore null/false (comme h()). */
export function setChildren(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}

export function icon(name) {
  const paths = {
    plus: 'M12 5v14M5 12h14',
    trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3',
    up: 'M12 19V5M5 12l7-7 7 7',
    down: 'M12 5v14M5 12l7 7 7-7',
    copy: 'M8 8h12v12H8zM4 16V4h12',
    check: 'M5 12l5 5 9-10',
    x: 'M6 6l12 12M18 6L6 18',
    camera: 'M4 8h3l2-3h6l2 3h3v11H4zM12 17a4 4 0 100-8 4 4 0 000 8z',
    image: 'M4 5h16v14H4zM4 15l5-5 5 5 2-2 4 4M15 9.5a1 1 0 100-.1',
    file: 'M6 3h8l4 4v14H6zM14 3v4h4',
    eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z',
    download: 'M12 4v11M7 10l5 5 5-5M5 20h14',
    edit: 'M4 20h4L19 9l-4-4L4 16zM14 6l4 4',
    alert: 'M12 4L2.5 20h19zM12 10v4.5M12 17.2v.3',
    info: 'M12 21a9 9 0 100-18 9 9 0 000 18zM12 11v5.5M12 7.8v.3',
  };
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  svg.innerHTML = `<path d="${paths[name] || ''}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  return svg;
}

export function toast(msg, kind = 'info', ms = 3500) {
  const t = h('div', { class: `toast ${kind}`, role: 'status' }, msg);
  document.getElementById('toasts').append(t);
  setTimeout(() => t.classList.add('out'), ms);
  setTimeout(() => t.remove(), ms + 400);
}

// Un clic sur le fond (hors de la boîte) ferme la modale.
function closeOnBackdrop(dlg, onClose) {
  dlg.addEventListener('mousedown', (e) => { dlg._downOnBackdrop = e.target === dlg; });
  dlg.addEventListener('click', (e) => { if (e.target === dlg && dlg._downOnBackdrop) onClose(); });
}

export function modal(title, body, { wide = false } = {}) {
  const dlg = h('dialog', { class: `modal ${wide ? 'wide' : ''}` },
    h('header', {}, h('h2', {}, title), h('button', { class: 'btn ghost icon-btn', 'aria-label': 'Fermer', onclick: () => dlg.close() }, icon('x'))),
    h('div', { class: 'modal-body' }, body),
  );
  closeOnBackdrop(dlg, () => dlg.close());
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

const DIALOG_ICONS = {
  danger: 'trash',
  warn: 'alert',
  info: 'info',
  ok: 'check',
};

/**
 * Boîte de dialogue applicative (remplace confirm/alert).
 * Renvoie une promesse résolue à true (confirmation) ou false (annulation, Échap, clic hors de la boîte).
 */
export function ask({
  title, message = '', confirmLabel = 'Confirmer', cancelLabel = 'Annuler', kind = 'info', alert = false,
} = {}) {
  return new Promise((resolve) => {
    let result = false;
    const confirmBtn = h('button', {
      class: `btn ${kind === 'danger' ? 'danger-solid' : 'primary'}`,
      onclick: () => { result = true; dlg.close(); },
    }, confirmLabel);
    const dlg = h('dialog', { class: `modal dialog ${kind}`, 'aria-labelledby': 'dlg-title' },
      h('div', { class: 'dialog-body' },
        h('div', { class: `dialog-icon ${kind}` }, icon(DIALOG_ICONS[kind] || 'info')),
        h('div', { class: 'dialog-text' },
          h('h2', { id: 'dlg-title' }, title),
          message ? (message instanceof Node ? message : h('p', {}, message)) : null,
        ),
      ),
      h('div', { class: 'dialog-actions' },
        alert ? null : h('button', { class: 'btn ghost', onclick: () => dlg.close() }, cancelLabel),
        confirmBtn,
      ),
    );
    closeOnBackdrop(dlg, () => dlg.close());
    dlg.addEventListener('close', () => { dlg.remove(); resolve(result); });
    document.body.append(dlg);
    dlg.showModal();
    confirmBtn.focus(); // Entrée confirme, Échap annule
  });
}

export const confirmDanger = (title, message, confirmLabel = 'Supprimer') =>
  ask({ title, message, confirmLabel, kind: 'danger' });

export const notify = (title, message, kind = 'info') =>
  ask({ title, message, confirmLabel: 'OK', kind, alert: true });

export function download(blob, name) {
  const a = h('a', { href: URL.createObjectURL(blob), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

export async function api(path, opts = {}) {
  let res;
  try { res = await fetch(path, opts); }
  catch (e) {
    if (e.name === 'AbortError') throw e; // annulation volontaire
    throw new Error('Serveur injoignable (hors-ligne ?)');
  }
  if (!res.ok) {
    let msg = `Erreur ${res.status}`;
    try { const j = await res.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return res;
}

export const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
