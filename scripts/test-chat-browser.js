'use strict';
// Proef: echt chatten met de Werdens Media LLM via de chatpagina in de browser.
// Start de engine op 11434 en draai: node scripts/test-chat-browser.js
// Bewijst: de pagina laadt, de modellenlijst vult zich, een vraag wordt verstuurd,
// het antwoord komt binnen (streamend) en de pagina zet het in een bubbel.
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const CHROOM = process.env.CHROOM || path.join(process.env.HOME, '.hermes/tools/chromium-1208/chrome-linux64/chrome');
const LIB = process.env.CHROOM_LIB || path.join(process.env.HOME, '.local/lib');
const POORT = Number(process.env.CDP_POORT || 0);
const ADRES = process.env.ADRES || 'http://127.0.0.1:11434/chat';
const VRAAG = process.env.VRAAG || 'Noem in één zin wat je voor mij kunt doen.';
const UIT = process.env.UIT || '/tmp';

const profiel = fs.mkdtempSync(path.join(os.tmpdir(), 'wmlm-chat-'));
const chrome = spawn(CHROOM, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
  '--disable-dev-shm-usage', `--remote-debugging-port=${POORT}`,
  `--user-data-dir=${profiel}`, '--window-size=1440,900',
  '--force-device-scale-factor=1', 'about:blank',
], { env: { ...process.env, LD_LIBRARY_PATH: LIB }, stdio: ['ignore', 'ignore', 'pipe'] });

const wacht = (ms) => new Promise((r) => setTimeout(r, ms));

async function haalPoort() {
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
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data);
    if (d.id && wachtOp.has(d.id)) {
      const { resolve, reject } = wachtOp.get(d.id);
      wachtOp.delete(d.id);
      if (d.error) reject(new Error(d.error.message)); else resolve(d.result);
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  await stuur('Page.enable');
  await stuur('Runtime.enable');

  const js = async (uitdrukking) => {
    const r = await stuur('Runtime.evaluate', { expression: uitdrukking, awaitPromise: true, returnByValue: true });
    return r.result ? r.result.value : undefined;
  };

  console.log('  pagina openen:', ADRES);
  await stuur('Page.navigate', { url: ADRES });
  await wacht(1500);

  console.log('  titel:', await js('document.title'));
  // wachten tot de modellenlijst gevuld is
  let aantal = 0;
  for (let i = 0; i < 60; i++) {
    aantal = await js('(document.getElementById("model") || {}).options ? document.getElementById("model").options.length : 0');
    if (aantal > 0) break;
    await wacht(500);
  }
  console.log('  modellen in de keuzelijst:', aantal);
  console.log('  gekozen model:', await js('document.getElementById("model").value'));
  console.log('  logo zichtbaar:', await js('(function(){var i=document.querySelector(".merk .logo img"); return !!i && i.naturalWidth > 0;})()'));

  console.log('  vraag typen:', VRAAG);
  await js(`(function(){
    var v = document.getElementById("invoer");
    v.value = ${JSON.stringify(VRAAG)};
    v.dispatchEvent(new Event("input"));
    return v.value.length;
  })()`);
  const t0 = Date.now();
  await js('document.getElementById("stuur").click()');
  console.log('  verstuurd, wachten op het antwoord…');

  let antwoord = '';
  for (let i = 0; i < 240; i++) {
    await wacht(1000);
    antwoord = await js('(function(){var b=document.querySelectorAll(".bubbel.van-ai .tekst"); return b.length ? b[b.length-1].innerText : "";})()');
    const klaar = await js('document.getElementById("stuur").disabled === false && document.getElementById("stop").hidden === true');
    if (klaar && antwoord) break;
  }
  const sec = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('  antwoord binnen in', sec, 's');
  console.log('  bubbels op de pagina:', await js('document.querySelectorAll(".bubbel").length'));
  console.log('  voettekst:', await js('document.getElementById("voet").textContent'));
  console.log('  --- antwoord ---');
  console.log(antwoord.split('\n').map((r) => '  ' + r).join('\n'));

  const shot = async (naam) => {
    const r = await stuur('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(UIT, naam), Buffer.from(r.data, 'base64'));
    console.log('  schermafbeelding:', path.join(UIT, naam));
  };
  await shot('chat-bureau.png');

  await stuur('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await wacht(900);
  await shot('chat-mobiel.png');
  await stuur('Emulation.clearDeviceMetricsOverride');

  chrome.kill('SIGKILL');
  process.exit(0);
})().catch((e) => {
  console.error('  FOUT:', e.message);
  chrome.kill('SIGKILL');
  process.exit(1);
});
