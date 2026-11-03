// Mode scan : caméra en direct, repères détectés sur le téléphone, déclenchement automatique
// quand la feuille est entière, stable, nette et assez éclairée. Photo en pleine résolution.
// Rafale : la caméra reste ouverte ; après chaque photo (bip, vibration), elle attend que la
// feuille soit retirée avant d'en photographier une autre (pas deux fois la même page).
import { analyzeFrame, toGray } from './markers.js';
import { h, icon } from './ui.js';

const ANALYSIS_W = 480; // largeur de l'image analysée (px)
const HOLD_MS = 700; // durée pendant laquelle tout doit être bon avant la photo
const MIN_COVERAGE = 0.2; // part de l'image occupée par la feuille
const MIN_BRIGHTNESS = 85;
const MAX_GLARE = 0.25;
const STABLE_FRAMES = 4;
const STABLE_TOL = 0.015; // déplacement max des coins, en fraction de la diagonale
const GONE_FRAMES = 3; // images sans feuille (~0,3 s) : la page photographiée a été retirée

/** Bip court, généré (pas de fichier son). Le contexte audio est créé au clic d'ouverture. */
function beeper() {
  let ac = null;
  try { ac = new (window.AudioContext || window.webkitAudioContext)(); } catch {}
  return () => {
    if (!ac) return;
    ac.resume?.();
    const t = ac.currentTime;
    for (const [f, dt] of [[880, 0], [1320, 0.09]]) {
      const o = ac.createOscillator(), g = ac.createGain();
      o.type = 'sine';
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + dt);
      g.gain.exponentialRampToValueAtTime(0.25, t + dt + 0.01);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.12);
      o.connect(g).connect(ac.destination);
      o.start(t + dt);
      o.stop(t + dt + 0.13);
    }
  };
}

export const cameraSupported = () => !!(window.isSecureContext && navigator.mediaDevices?.getUserMedia);

/**
 * Ouvre le mode scan en plein écran. onCapture(blob) reçoit chaque photo (JPEG).
 * Renvoie une promesse résolue à la fermeture ; rejetée si la caméra est inaccessible.
 */
export async function openScanCamera({ onCapture }) {
  const beep = beeper(); // avant tout await : encore dans le geste de l'utilisateur
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
  });
  const track = stream.getVideoTracks()[0];
  try { await track.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }); } catch {}

  const video = h('video', { class: 'cam-video', playsinline: true, muted: true, autoplay: true });
  video.srcObject = stream;
  const overlay = h('canvas', { class: 'cam-overlay' });
  const msg = h('div', { class: 'cam-msg' }, 'Ouverture de la caméra…');
  const ring = h('div', { class: 'cam-ring' });
  const shutter = h('button', { class: 'cam-shutter', 'aria-label': 'Prendre la photo' }, ring);
  const caps = track.getCapabilities?.() || {};
  let torchOn = false;
  const torchBtn = caps.torch ? h('button', { class: 'cam-btn', 'aria-label': 'Lampe', onclick: () => setTorch(!torchOn) }, '🔦') : null;
  const closeBtn = h('button', { class: 'cam-btn', 'aria-label': 'Fermer' }, icon('x'));
  const flash = h('div', { class: 'cam-flash' });
  const doneBtn = h('button', { class: 'cam-done', hidden: true }, 'Terminer');
  const badge = h('div', { class: 'cam-badge' });

  const dlg = h('dialog', { class: 'cam' },
    h('div', { class: 'cam-stage' }, video, overlay, flash, badge),
    h('div', { class: 'cam-top' }, closeBtn, h('span', { class: 'cam-title' }, 'Mode scan'), torchBtn ?? h('span')),
    h('div', { class: 'cam-bottom' }, msg,
      h('div', { class: 'cam-controls' }, h('span'), shutter, doneBtn),
      h('p', { class: 'cam-hint' }, 'Feuille à plat, les 4 carrés noirs dans le cadre : la photo se prend toute seule. Enchaînez les pages, puis « Terminer ».')),
  );
  document.body.append(dlg);
  dlg.showModal();

  async function setTorch(on) {
    try { await track.applyConstraints({ advanced: [{ torch: on }] }); torchOn = on; torchBtn?.classList.toggle('on', on); } catch {}
  }

  const work = document.createElement('canvas');
  const wctx = work.getContext('2d', { willReadFrequently: true });
  const history = [];
  let sharpMax = 0;
  let okSince = 0;
  let busy = false;
  let closed = false;
  let timer = 0;
  let shots = 0;
  let waitRemoval = false; // page photographiée encore sous la caméra
  let gone = 0;
  let resolveClosed;
  const closedP = new Promise((r) => { resolveClosed = r; });

  const setMsg = (text, kind = '') => { msg.textContent = text; msg.className = `cam-msg ${kind}`; };

  /** Rectangle affiché de la vidéo (object-fit: contain) dans l'élément. */
  function videoRect() {
    const W = video.clientWidth, H = video.clientHeight;
    const s = Math.min(W / video.videoWidth, H / video.videoHeight);
    const w = video.videoWidth * s, hh = video.videoHeight * s;
    return { x: (W - w) / 2, y: (H - hh) / 2, s };
  }

  function draw(res, scale, good) {
    const dpr = window.devicePixelRatio || 1;
    overlay.width = overlay.clientWidth * dpr;
    overlay.height = overlay.clientHeight * dpr;
    const g = overlay.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, overlay.width, overlay.height);
    if (!res.corners) return;
    const r = videoRect();
    const pts = res.corners.map((p) => [r.x + (p.x / scale) * r.s, r.y + (p.y / scale) * r.s]);
    g.lineWidth = 3;
    g.strokeStyle = good ? '#22c55e' : '#facc15';
    g.fillStyle = good ? 'rgba(34,197,94,.15)' : 'rgba(250,204,21,.10)';
    g.beginPath();
    pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
    g.closePath();
    g.fill();
    g.stroke();
    g.fillStyle = g.strokeStyle;
    for (const [x, y] of pts) { g.beginPath(); g.arc(x, y, 7, 0, Math.PI * 2); g.fill(); }
  }

  function stable(corners, diag) {
    history.push(corners);
    if (history.length > STABLE_FRAMES) history.shift();
    if (history.length < STABLE_FRAMES) return false;
    for (let i = 0; i < 4; i++) {
      for (const c of history) if (Math.hypot(c[i].x - corners[i].x, c[i].y - corners[i].y) > STABLE_TOL * diag) return false;
    }
    return true;
  }

  async function tick() {
    if (closed) return;
    if (!busy && video.readyState >= 2 && video.videoWidth) {
      const scale = ANALYSIS_W / Math.max(video.videoWidth, video.videoHeight);
      const w = Math.round(video.videoWidth * scale), hh = Math.round(video.videoHeight * scale);
      if (work.width !== w) { work.width = w; work.height = hh; }
      wctx.drawImage(video, 0, 0, w, hh);
      const res = analyzeFrame(toGray(wctx.getImageData(0, 0, w, hh).data, w, hh), w, hh);
      const diag = Math.hypot(w, hh);
      let good = false;
      if (waitRemoval) {
        gone = res.corners ? 0 : gone + 1;
        if (gone >= GONE_FRAMES) { waitRemoval = false; history.length = 0; }
      }
      if (waitRemoval) {
        setMsg(`✓ Page ${shots} prise — posez la page suivante`, 'ok');
      } else if (!res.corners) {
        history.length = 0;
        sharpMax *= 0.97;
        setMsg(res.brightness < 50 ? 'Trop sombre : rapprochez-vous de la lumière' + (torchBtn && !torchOn ? ' ou allumez la lampe' : '')
          : shots ? `Page ${shots + 1} : cadrez la feuille entière` : 'Cadrez la feuille entière : les 4 carrés noirs doivent être visibles');
      } else {
        sharpMax = Math.max(sharpMax * 0.99, res.sharpness);
        const isStable = stable(res.corners, diag);
        if (res.coverage < MIN_COVERAGE) setMsg('Rapprochez-vous de la feuille');
        else if (res.brightness < MIN_BRIGHTNESS) setMsg('Trop sombre' + (torchBtn && !torchOn ? ' : allumez la lampe' : ' : cherchez plus de lumière'), 'warn');
        else if (res.glare > MAX_GLARE) setMsg('Reflet : inclinez un peu le téléphone', 'warn');
        else if (!isStable) setMsg('Ne bougez plus…');
        else if (res.sharpness < Math.max(30, sharpMax * 0.6)) setMsg('Image floue : ne bougez plus…');
        else good = true;
      }
      draw(waitRemoval ? { corners: null } : res, scale, good);
      if (good) {
        okSince ||= performance.now();
        const p = Math.min(1, (performance.now() - okSince) / HOLD_MS);
        ring.style.setProperty('--p', p);
        setMsg('Parfait, ne bougez plus', 'ok');
        if (p >= 1) { await capture(); }
      } else {
        okSince = 0;
        ring.style.setProperty('--p', 0);
      }
    }
    timer = setTimeout(tick, 90);
  }

  /** Photo en pleine résolution (ImageCapture) ; à défaut, image de la vidéo. */
  async function grab() {
    if ('ImageCapture' in window) {
      try {
        const ic = new window.ImageCapture(track);
        const blob = await Promise.race([
          ic.takePhoto(),
          new Promise((_, rej) => setTimeout(() => rej(new Error('délai')), 4000)),
        ]);
        if (blob?.size) return blob;
      } catch {}
    }
    const c = document.createElement('canvas');
    c.width = video.videoWidth;
    c.height = video.videoHeight;
    c.getContext('2d').drawImage(video, 0, 0);
    return new Promise((res) => c.toBlob(res, 'image/jpeg', 0.92));
  }

  async function capture() {
    if (busy) return;
    busy = true;
    setMsg('Photo…', 'ok');
    flash.classList.remove('go');
    void flash.offsetWidth;
    flash.classList.add('go');
    navigator.vibrate?.(40);
    try {
      const blob = await grab();
      if (torchOn) await setTorch(true); // certaines caméras éteignent la lampe après la photo
      onCapture(blob);
      shots++;
      beep();
      navigator.vibrate?.([40, 60, 40]);
      badge.textContent = `✓ ${shots}`;
      badge.classList.remove('go');
      void badge.offsetWidth;
      badge.classList.add('go');
      doneBtn.hidden = false;
      doneBtn.textContent = `Terminer · ${shots} page${shots > 1 ? 's' : ''}`;
      waitRemoval = true;
      gone = 0;
      okSince = 0;
      ring.style.setProperty('--p', 0);
    } catch (e) {
      setMsg(`Échec de la photo : ${e.message}`, 'warn');
    } finally {
      busy = false;
    }
  }

  function close() {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    stream.getTracks().forEach((t) => t.stop());
    if (dlg.open) dlg.close();
    dlg.remove();
    resolveClosed();
  }

  shutter.addEventListener('click', () => capture()); // à la main : même si la page n'a pas été retirée
  doneBtn.addEventListener('click', close);
  closeBtn.addEventListener('click', close);
  dlg.addEventListener('cancel', (e) => { e.preventDefault(); close(); });

  await video.play().catch(() => {});
  setMsg('Cadrez la feuille entière : les 4 carrés noirs doivent être visibles');
  tick();
  return closedP;
}
