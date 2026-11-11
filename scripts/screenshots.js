'use strict';
// Maakt de schermafbeeldingen voor de README: de drie lagen in beeld.
// Start eerst de lagen (engine 11434, werkruimte 8090, media 8091) en dan:
//   node scripts/screenshots.js [uitmap]
//
// Chromium van Hermes mist soms libnspr4/libnss3; zet die in ~/.local/lib
// (zie de skill headless-chromium-screenshots). Dit script zet LD_LIBRARY_PATH
// zelf goed, want de browser-daemon van browser_exec erft die variabele niet.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const UIT = path.resolve(process.argv[2] || 'docs/img');
const CHROOM = process.env.CHROOM || path.join(process.env.HOME, '.hermes/tools/chromium-1208/chrome-linux64/chrome');
const LIB = process.env.CHROOM_LIB || path.join(process.env.HOME, '.local/lib');
// 0 = Chromium kiest zelf een vrije poort (zie record-demo.js voor waarom).
const POORT = Number(process.env.CDP_POORT || 0);
const W = Number(process.env.BREEDTE || 1440);
const H = Number(process.env.HOOGTE || 900);
const BASIS = process.env.BASIS || 'http://127.0.0.1';
const WERKRUIMTE = process.env.WERKRUIMTE || 'werdens-media-llm';
const VRAAG = process.env.VRAAG ||
  'Welke drie lagen heeft de Werdens Media LLM en op welke poorten draaien ze?';

fs.mkdirSync(UIT, { recursive: true });
const profiel = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'wmlm-profiel-'));

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

  const js = async (expr) => {
    const r = await stuur('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
    return r.result.value;
  };

  async function ga(url, ms = 1500) {
    await stuur('Page.navigate', { url });
    await wacht(ms);
  }

  // Schermafbeelding van het zichtbare venster; geen clip, want een clip zet
  // plakkende koppen op hun vastgeplakte plek midden in het beeld.
  async function schiet(naam, { volledig = false } = {}) {
    const params = { format: 'png', fromSurface: true, captureBeyondViewport: volledig };
    if (volledig) {
      const m = await stuur('Page.getLayoutMetrics');
      const h = Math.min(Math.ceil(m.cssContentSize.height), 6000);
      params.clip = { x: 0, y: 0, width: W, height: h, scale: 1 };
    }
    const r = await stuur('Page.captureScreenshot', params);
    const bestand = path.join(UIT, naam + '.png');
    fs.writeFileSync(bestand, Buffer.from(r.data, 'base64'));
    const kb = (fs.statSync(bestand).size / 1024).toFixed(0);
    console.log(`  ${naam}.png  ${kb} kB`);
    return bestand;
  }

  // 1. De engine: statuspagina
  console.log('engine (11434)');
  await ga(`${BASIS}:11434/`, 1200);
  await schiet('laag-1-engine');

  // 2. De werkruimtelaag: werkruimte kiezen, documenten tonen
  console.log('werkruimte (8090)');
  await ga(`${BASIS}:8090/`, 2500);
  await js(`(() => { if (typeof kies === 'function') kies(${JSON.stringify(WERKRUIMTE)}); return true; })()`);
  await wacht(2000);
  // Het documentenpaneel rendert per document een div.bron; een andere selector
  // telt nul en meldt onterecht dat er niets in de werkruimte staat.
  const docs = await js(`document.querySelectorAll('#docs .bron').length`);
  console.log(`  documenten in beeld: ${docs}`);
  await schiet('laag-2-werkruimte');

  // 3. Een echte vraag stellen en wachten op het antwoord met bronnen
  console.log(`vraag stellen: ${VRAAG.slice(0, 60)}…`);
  // Let op: de vraaginvoer heet #vraag; #v hoort bij de embed-widget.
  await js(`(() => {
    const v = document.querySelector('#vraag');
    if (v) { v.value = ${JSON.stringify(VRAAG)}; v.dispatchEvent(new Event('input', { bubbles: true })); }
    if (typeof stelVraag === 'function') stelVraag();
    return true;
  })()`);
  // Op deze CPU duurt een antwoord ongeveer anderhalve minuut; wacht op de bronnen.
  let antwoord = '';
  for (let i = 0; i < 120; i++) {
    await wacht(2500);
    antwoord = await js(`(() => { const g = document.querySelector('#gesprek'); return g ? g.innerText : ''; })()`);
    if (antwoord.includes('Bron') || antwoord.length > 900) break;
  }
  console.log(`  antwoord binnen: ${antwoord.length} tekens`);
  await js(`(() => { const g = document.querySelector('#gesprek'); if (g) g.scrollIntoView({ block: 'start' }); return true; })()`);
  await wacht(1200);
  await schiet('laag-2-antwoord');

  // 4. De medialaag
  console.log('media (8091)');
  await ga(`${BASIS}:8091/`, 2000);
  // Let op: #tekst is het uitvoerveld (een div); het invoerveld is #voorleestekst.
  await js(`(() => {
    const t = document.querySelector('#voorleestekst');
    if (t) { t.value = 'De Werdens Media LLM draait volledig lokaal, zonder abonnement en zonder tokenkosten.'; t.dispatchEvent(new Event('input', { bubbles: true })); }
    return true;
  })()`);
  await wacht(500);
  await schiet('laag-3-media');

  ws.close();
  chrome.kill('SIGKILL');
  // Het profiel opruimen mag nooit de hele run laten mislukken.
  try { fs.rmSync(profiel, { recursive: true, force: true }); } catch (_) {}
  console.log(`klaar → ${UIT}`);
  process.exit(0);
})().catch((e) => {
  console.error('fout:', e.message);
  try { chrome.kill('SIGKILL'); } catch (_) {}
  process.exit(1);
});
