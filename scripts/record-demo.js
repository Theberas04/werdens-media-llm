'use strict';
// Neemt een demo op van de Werdens Media LLM: de drie lagen in beeld, met een
// echte vraag die echt beantwoord wordt uit de documenten.
//   node scripts/record-demo.js [uitmap]      (standaard: docs/opname)
// Daarna: ffmpeg -f concat ... (zie onderaan dit bestand of de README).
//
// De wachttijd op een antwoord is op een CPU ongeveer anderhalve minuut. Die
// periode wordt traag vastgelegd (één beeld per 2,5 s) en in de tijdlijn kort
// gehouden, zodat de video niet anderhalve minuut stil staat.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const UIT = path.resolve(process.argv[2] || 'docs/opname');
const CHROOM = process.env.CHROOM || path.join(process.env.HOME, '.hermes/tools/chromium-1208/chrome-linux64/chrome');
const LIB = process.env.CHROOM_LIB || path.join(process.env.HOME, '.local/lib');
// 0 = laat Chromium zelf een vrije poort kiezen. Met een vaste poort kan een
// browser van een eerdere sessie die poort bezetten; je client verbindt dan met
// díe browser en neemt beelden van een heel andere app op. Dat is precies één
// keer gebeurd, vandaar deze route.
const POORT = Number(process.env.CDP_POORT || 0);
const W = Number(process.env.BREEDTE || 1440);
const H = Number(process.env.HOOGTE || 900);
const BASIS = process.env.BASIS || 'http://127.0.0.1';
const WERKRUIMTE = process.env.WERKRUIMTE || 'werdens-media-llm';
const VRAAG = process.env.VRAAG ||
  'Welke drie lagen heeft de Werdens Media LLM en op welke poorten draaien ze?';
const FRAME_MS = Number(process.env.FRAME_MS || 95);
const TRAAG_MS = Number(process.env.TRAAG_MS || 2500);
const TRAAG_DUUR = Number(process.env.TRAAG_DUUR || 0.25); // seconde per traag beeld

fs.rmSync(UIT, { recursive: true, force: true });
fs.mkdirSync(UIT, { recursive: true });
const profiel = fs.mkdtempSync(path.join(os.tmpdir(), 'wmlm-opname-'));

const chrome = spawn(CHROOM, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--disable-dev-shm-usage', `--remote-debugging-port=${POORT}`,
  `--user-data-dir=${profiel}`, `--window-size=${W},${H}`,
  '--force-device-scale-factor=1', 'about:blank',
], { env: { ...process.env, LD_LIBRARY_PATH: LIB }, stdio: ['ignore', 'ignore', 'pipe'] });

const wacht = (ms) => new Promise((r) => setTimeout(r, ms));

async function haalPoort() {
  if (POORT) return POORT;
  const bestand = path.join(profiel, 'DevToolsActivePort');
  for (let i = 0; i < 150; i++) {
    try {
      const p = Number(fs.readFileSync(bestand, 'utf8').split('\n')[0].trim());
      if (p) return p;
    } catch (_) {}
    await wacht(100);
  }
  throw new Error('Chromium gaf geen debugging-poort');
}

async function haalDoel(poort) {
  for (let i = 0; i < 150; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${poort}/json/list`);
      const p = (await r.json()).find((t) => t.type === 'page');
      if (p) return p.webSocketDebuggerUrl;
    } catch (_) {}
    await wacht(100);
  }
  throw new Error('Chromium kwam niet opzetten');
}

(async () => {
  const ws = new WebSocket(await haalDoel(await haalPoort()));
  let id = 0;
  const wachtOp = new Map();
  const stuur = (method, params = {}) => new Promise((resolve, reject) => {
    const n = ++id;
    wachtOp.set(n, { resolve, reject });
    ws.send(JSON.stringify({ id: n, method, params }));
  });
  ws.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    const w = msg.id && wachtOp.get(msg.id);
    if (!w) return;
    wachtOp.delete(msg.id);
    msg.error ? w.reject(new Error(msg.error.message)) : w.resolve(msg.result);
  });
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  await stuur('Page.enable');
  await stuur('Runtime.enable');
  await stuur('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  const js = async (expr) => {
    const r = await stuur('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  const frames = [];
  let laatste = Date.now();
  let stoppen = false;
  let klaar = false;
  let traag = false;

  // Pas starten ná de eerste navigatie: anders leg je het lege startscherm vast
  // en begint de video met een wit beeld.
  const startLoop = () => { (async () => {
    while (!stoppen) {
      try {
        const r = await stuur('Page.captureScreenshot', { format: 'jpeg', quality: 82, fromSurface: true });
        const nu = Date.now();
        const bestand = path.join(UIT, `f${String(frames.length).padStart(5, '0')}.jpg`);
        fs.writeFileSync(bestand, Buffer.from(r.data, 'base64'));
        frames.push({ bestand, van: laatste, tot: nu, traag });
        laatste = nu;
      } catch (_) {}
      await wacht(traag ? TRAAG_MS : FRAME_MS);
    }
    klaar = true;
  })(); };

  const ga = async (url, ms = 2200) => { await stuur('Page.navigate', { url }); await wacht(ms); };
  const klik = async (sel) => {
    const ok = await js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return false; e.click(); return true; })()`);
    if (!ok) throw new Error('niet gevonden: ' + sel);
  };
  const typ = async (sel, tekst) => {
    for (let i = 1; i <= tekst.length; i++) {
      await js(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (e) { e.value = ${JSON.stringify(tekst.slice(0, i))}; e.dispatchEvent(new Event('input', { bubbles: true })); } return true; })()`);
      await wacht(38);
    }
  };

  // 1. Werkruimte openen
  console.log('werkruimte openen');
  await ga(`${BASIS}:8090/`, 3000);
  await wacht(1500);
  startLoop();
  await wacht(600);

  // 2. De vraag intypen
  console.log('vraag intypen');
  await typ('#vraag', VRAAG);
  await wacht(900);

  // 3. Vragen en wachten op het antwoord (traag vastleggen)
  console.log('vraag stellen');
  await klik('#vraag + button, button[onclick="stelVraag()"]');
  traag = true;
  let antwoord = '';
  for (let i = 0; i < 120; i++) {
    await wacht(2000);
    antwoord = await js(`(() => { const g = document.querySelector('#gesprek'); return g ? g.innerText : ''; })()`);
    if (antwoord.includes('Bron') || antwoord.length > 900) break;
  }
  traag = false;
  console.log(`antwoord binnen: ${antwoord.length} tekens`);
  await wacht(3500);

  // 4. Naar de bronnen scrollen
  await js(`(() => { const g = document.querySelector('#gesprek'); if (g) g.scrollIntoView({ block: 'start' }); return true; })()`);
  await wacht(2600);

  // 5. Medialaag: tekst laten voorlezen
  console.log('medialaag');
  await ga(`${BASIS}:8091/`, 2500);
  // Let op: #tekst is het uitvoerveld (een div); het invoerveld is #voorleestekst.
  await js(`(() => {
    const t = document.querySelector('#voorleestekst');
    if (t) { t.value = 'De Werdens Media LLM draait volledig lokaal, zonder abonnement en zonder tokenkosten.'; t.dispatchEvent(new Event('input', { bubbles: true })); }
    return true;
  })()`);
  await wacht(1400);
  await klik('#spreek').catch(() => {});
  await wacht(4200);

  // 6. Engine
  console.log('engine');
  await ga(`${BASIS}:11434/`, 2600);

  stoppen = true;
  while (!klaar) await wacht(50);
  try { ws.close(); } catch (_) {}
  chrome.kill('SIGKILL');
  try { fs.rmSync(profiel, { recursive: true, force: true }); } catch (_) {}

  if (!frames.length) throw new Error('geen beelden opgenomen');
  frames[frames.length - 1].tot = Date.now();
  const regels = frames.map((f) => {
    const echt = (f.tot - f.van) / 1000;
    const d = f.traag ? Math.min(TRAAG_DUUR, Math.max(0.05, echt)) : Math.max(0.03, Math.min(echt, 1.2));
    return `file '${f.bestand.replace(/'/g, "'\\''")}'\nduration ${d.toFixed(3)}`;
  });
  const lijst = path.join(UIT, 'frames.txt');
  fs.writeFileSync(lijst, regels.join('\n') + `\nfile '${frames[frames.length - 1].bestand}'\n`);
  const trage = frames.filter((f) => f.traag).length;
  const duur = frames.reduce((s, f, i) => {
    const echt = (f.tot - f.van) / 1000;
    return s + (f.traag ? Math.min(TRAAG_DUUR, Math.max(0.05, echt)) : Math.max(0.03, Math.min(echt, 1.2)));
  }, 0);
  fs.writeFileSync(path.join(UIT, 'meta.json'), JSON.stringify({
    beelden: frames.length, trage_beelden: trage, videoseconden: +duur.toFixed(1),
    opnamesconden: +((Date.now() - frames[0].van) / 1000).toFixed(1), lijst,
  }, null, 2));
  console.log(`klaar: ${frames.length} beelden (${trage} traag), video ${duur.toFixed(1)}s → ${lijst}`);
  process.exit(0);
})().catch((e) => {
  console.error('fout:', e.message);
  try { chrome.kill('SIGKILL'); } catch (_) {}
  process.exit(1);
});
