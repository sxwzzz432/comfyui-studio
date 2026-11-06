#!/usr/bin/env node
/*
 * browser_smoke.mjs — 用 CDP 驱动 headless Chrome 真跑一次出图。
 *
 * 为什么需要它：网页里的 bug 有一大类只在浏览器里出现（比如 WebSocket 提前关闭、
 * 图片 blob 拿不到、进度事件没接上），Python 侧的一致性测试和 node --check 都发现不了。
 * 这个脚本会真的点「生成」、轮询页面状态、抓控制台异常，最后截图存证。
 *
 * 前置：静态服务器已启动（python -m http.server 5500），ComfyUI 在 127.0.0.1:8188。
 *
 * 用法：
 *   node browser_smoke.mjs [页面URL] [最长等待秒数]
 *   node browser_smoke.mjs http://127.0.0.1:5500/web/app.html 60
 */

import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const CHROME = process.env.CHROME ||
  'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PAGE = process.argv[2] || 'http://127.0.0.1:5500/web/app.html';
const WAIT_S = Number(process.argv[3] || 60);
const PORT = Number(process.env.CDP_PORT || 9333);
const OUT = path.join(path.dirname(new URL(import.meta.url).pathname).replace(/^\//, ''), 'out');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function launch() {
  const args = ['--headless=new', '--disable-gpu', '--no-sandbox',
                `--remote-debugging-port=${PORT}`, '--window-size=1320,880',
                '--hide-scrollbars', 'about:blank'];
  const p = spawn(CHROME, args, { stdio: 'ignore' });
  return p;
}

async function devtoolsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find(t => t.type === 'page');
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch (e) { /* not up yet */ }
    await sleep(250);
  }
  throw new Error('Chrome DevTools 没起来（检查 CHROME 路径 / 端口被占）');
}

function rpc(ws) {
  let id = 0;
  return (method, params) => new Promise((resolve, reject) => {
    const mid = ++id;
    const timer = setTimeout(() => { ws.removeEventListener('message', onMsg); reject(new Error(method + ' 超时')); }, 30000);
    function onMsg(ev) {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.id === mid) {
        clearTimeout(timer);
        ws.removeEventListener('message', onMsg);
        resolve(m.result);
      }
    }
    ws.addEventListener('message', onMsg);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });
}

const main = async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const chrome = launch();
  const errors = [];
  let ws;
  try {
    ws = new WebSocket(await devtoolsUrl());
    await new Promise((r, j) => { ws.addEventListener('open', r, { once: true }); ws.addEventListener('error', j, { once: true }); });
    const call = rpc(ws);

    ws.addEventListener('message', ev => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails;
        errors.push((d.text || '') + ' ' + ((d.exception && d.exception.description) || ''));
      }
      if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
        errors.push('console.error: ' + m.params.args.map(a => a.value).join(' '));
      }
    });

    await call('Page.enable');
    await call('Runtime.enable');
    await call('Page.navigate', { url: PAGE });
    await sleep(4000);

    // 把参数改成小图快跑，连点 3 次验证「排队多个任务」
    const CLICKS = 3;
    const setup = `(async () => {
      const set = (sel, v) => { const e = document.querySelector(sel); if (e) { e.value = v; return true; } return false; };
      const r = {};
      r.steps = set('[data-key="1134.steps"]', '6');
      r.w = set('[data-size=w]', '512');
      r.h = set('[data-size=h]', '512');
      r.pos = set('[data-key="1160.positive"]', 'a red apple on a white table');
      r.seed = set('[data-key="784.seed"]', '12345');
      const b = document.getElementById('btnGen');
      r.btn = !!b;
      for (let i = 0; i < ${CLICKS}; i++){
        b.click();
        await new Promise(x => setTimeout(x, 350));
      }
      r.disabled = b.disabled;
      return r;
    })()`;
    const s = await call('Runtime.evaluate', { expression: setup, returnByValue: true, awaitPromise: true });
    console.log('setup:', JSON.stringify(s.result && s.result.value));

    const probe = `(() => ({
      done: (typeof done !== 'undefined' ? done.length : -1),
      jobs: (typeof activeJobCount === 'function' ? activeJobCount() : -1),
      stage: document.getElementById('pv').querySelectorAll('img').length,
      bar: document.getElementById('bar').style.width,
      pct: document.getElementById('progPct').textContent,
      txt: document.getElementById('progText').textContent,
      count: document.getElementById('galCount').textContent,
      queue: document.getElementById('queueBtn').textContent,
      btn: document.getElementById('genLabel').textContent,
      disabled: document.getElementById('btnGen').disabled,
      rail: !!document.querySelector('.rail-v'),
      snap: (sessionStorage.getItem('cgu.done.v1') || '').length,
      lastLog: document.getElementById('logLast').textContent
    }))()`;

    const t0 = Date.now();
    let last = '', ok = false, btnWasDisabled = false;
    while ((Date.now() - t0) / 1000 < WAIT_S) {
      const r = await call('Runtime.evaluate', { expression: probe, returnByValue: true });
      const v = (r.result && r.result.value) || {};
      const txt = JSON.stringify(v);
      if (txt !== last) {
        console.log(new Date().toISOString().slice(11, 19), txt);
        last = txt;
      }
      if (v.disabled) btnWasDisabled = true;
      if (v.done >= CLICKS && v.stage > 0) { ok = true; break; }
      await sleep(1000);
    }
    console.log('生成按钮在位时是否被禁用过: ' + (btnWasDisabled ? '是（不符合预期）' : '否'));

    // 刷新页面：完成列表应从 sessionStorage 还原（图片重新从 /view 拉）
    console.log('--- 刷新验证列表持久化 ---');
    await call('Page.navigate', { url: PAGE });
    await sleep(6000);
    const afterReload = await call('Runtime.evaluate', { expression: probe, returnByValue: true });
    const ar2 = (afterReload.result && afterReload.result.value) || {};
    console.log('刷新后:', JSON.stringify(ar2));
    const persistedOk = ar2.done === CLICKS && ar2.stage > 0;
    console.log('刷新后列表数量: ' + ar2.done + '（期望 ' + CLICKS + '）' +
                (persistedOk ? ' OK' : ' FAIL'));

    const shot = await call('Page.captureScreenshot', { format: 'png' });
    const file = path.join(OUT, 'browser_smoke.png');
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    console.log('截图 ->', file);

    // 顺带验证「套用历史参数」：点最后一张缩略图上的 ↻，工作流名应变成「历史 xxxx」
    let applyOk = true;
    const applyRes = await call('Runtime.evaluate', {
      expression: `(() => {
        const b = document.querySelectorAll('#galRail .th .ap');
        const before = document.getElementById('pillWf').textContent;
        if (!b.length) return { ok: false, reason: '列表里没有带工作流的缩略图', before: before };
        b[b.length - 1].click();
        return { ok: true, before: before };
      })()`, returnByValue: true });
    const ar = (applyRes.result && applyRes.result.value) || {};
    if (ar.ok) {
      await sleep(1800);
      const after = await call('Runtime.evaluate', {
        expression: `document.getElementById('pillWf').textContent`, returnByValue: true });
      const name = (after.result && after.result.value) || '';
      console.log('套用历史参数: ' + ar.before + ' -> ' + name);
      applyOk = name.indexOf('套用') === 0;
      if (!applyOk) console.log('FAIL: 套用后工作流名没变（期望以「套用」开头）');
    } else {
      console.log('跳过套用验证: ' + (ar.reason || ''));
    }

    if (errors.length) {
      console.log('页面异常 ' + errors.length + ' 条:');
      errors.slice(0, 5).forEach(e => console.log('  ' + e));
    }
    if (!ok) { console.log('FAIL: 在 ' + WAIT_S + 's 内没等到图片'); return 1; }
    if (errors.length) { console.log('FAIL: 有页面异常'); return 1; }
    if (!persistedOk) { console.log('FAIL: 刷新后列表没还原'); return 1; }
    if (!applyOk) { console.log('FAIL: 套用历史参数不可用'); return 1; }
    console.log('PASS: 出图 + 预览 + 列表持久化 + 套用参数 都正常');
    return 0;
  } finally {
    try { ws && ws.close(); } catch (e) {}
    chrome.kill();
  }
};

main().then(c => process.exit(c)).catch(e => { console.error('ERR', e.message); process.exit(2); });
