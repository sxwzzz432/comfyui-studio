#!/usr/bin/env node
/*
 * browser_snapshot.mjs — 验证参数快照：改了参数 → 刷新 → 参数还在 → 恢复内置 → 回默认值。
 *
 * 和 browser_smoke 一样用 CDP 驱动 headless Chrome，只是不真的出图（几秒就跑完）。
 *
 * 用法：node browser_snapshot.mjs [页面URL]
 */

import { spawn } from 'node:child_process';

const CHROME = process.env.CHROME ||
  'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PAGE = process.argv[2] || 'http://127.0.0.1:5500/web/app.html';
const PORT = Number(process.env.CDP_PORT || 9334);

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function devtoolsUrl() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find(t => t.type === 'page');
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch (e) { /* not ready */ }
    await sleep(250);
  }
  throw new Error('Chrome DevTools 没起来');
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
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
    `--remote-debugging-port=${PORT}`, '--window-size=1320,880', '--hide-scrollbars',
    'about:blank'], { stdio: 'ignore' });

  const errors = [];
  let ws;
  const fails = [];
  const check = (cond, name, detail) => {
    console.log(`  [${cond ? 'pass' : 'FAIL'}] ${name}` + (!cond && detail ? '   ' + detail : ''));
    if (!cond) fails.push(name);
  };

  try {
    ws = new WebSocket(await devtoolsUrl());
    await new Promise((r, j) => {
      ws.addEventListener('open', r, { once: true });
      ws.addEventListener('error', j, { once: true });
    });
    const call = rpc(ws);
    ws.addEventListener('message', ev => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails;
        errors.push((d.text || '') + ' ' + ((d.exception && d.exception.description) || ''));
      }
    });

    await call('Page.enable');
    await call('Runtime.enable');
    const evalJs = async expr => {
      const r = await call('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
      return r.result && r.result.value;
    };
    const go = async () => { await call('Page.navigate', { url: PAGE }); await sleep(4000); };

    const STEP_SEL = '[data-key="1134.steps"]';
    const PROMPT_SEL = '[data-key="1160.positive"]';
    const readState = `(() => ({
      steps: (document.querySelector('${STEP_SEL}')||{}).value,
      prompt: (document.querySelector('${PROMPT_SEL}')||{}).value,
      loras: (document.querySelector('[data-role=loras]')||{})._items
             ? document.querySelector('[data-role=loras]')._items.length : -1,
      log: document.getElementById('logLast').textContent,
      logAll: document.getElementById('log').textContent
    }))()`;

    console.log('1) 打开页面并改参数');
    await go();
    const before = await evalJs(readState);
    console.log('   初始 steps=' + before.steps + ' loras=' + before.loras);

    await evalJs(`(() => {
      const s = document.querySelector('${STEP_SEL}'); s.value = '9';
      s.dispatchEvent(new Event('input', {bubbles:true}));
      const p = document.querySelector('${PROMPT_SEL}'); p.value = 'snapshot test';
      p.dispatchEvent(new Event('input', {bubbles:true}));
      // 删掉一个 LoRA，验证数组也能恢复
      const del = document.querySelector('#loraBox .lora-row .del'); if (del) del.click();
      return true;
    })()`);
    await sleep(1200);              // 等防抖 500ms 落盘

    console.log('2) 刷新页面');
    await go();
    const after = await evalJs(readState);
    console.log('   刷新后 steps=' + after.steps + ' loras=' + after.loras +
                ' log=' + JSON.stringify(after.log));
    check(after.steps === '9', '刷新后 steps 恢复为 9', String(after.steps));
    check(after.prompt === 'snapshot test', '刷新后提示词恢复', JSON.stringify(after.prompt));
    check(after.loras === before.loras - 1, '刷新后 LoRA 数量恢复（少了一个）',
          before.loras + ' -> ' + (after.loras));
    check(String(after.logAll).indexOf('已恢复上次的参数') >= 0, '日志提示已恢复',
          String(after.log));

    console.log('3) 点「恢复内置」');
    await evalJs(`document.getElementById('btnBuiltin').click()`);
    await sleep(2500);
    const reset = await evalJs(readState);
    console.log('   恢复后 steps=' + reset.steps + ' loras=' + reset.loras +
                ' log=' + JSON.stringify(reset.log));
    check(reset.steps === '50', '恢复内置后 steps 回到工作流默认 50', String(reset.steps));
    check(reset.loras === before.loras, '恢复内置后 LoRA 数量回到 13', String(reset.loras));
    check(String(reset.log).indexOf('已恢复内置工作流') >= 0, '日志提示已恢复内置',
          String(reset.log));

    console.log('4) 再刷一次，确认没有被旧快照污染');
    await go();
    const final = await evalJs(readState);
    check(final.steps === '50', '再刷新仍是默认 50', String(final.steps));

    if (errors.length) {
      console.log('页面异常 ' + errors.length + ' 条:');
      errors.slice(0, 5).forEach(e => console.log('  ' + e));
      fails.push('页面异常');
    }
    console.log('---');
    if (fails.length) { console.log('FAIL: ' + fails.join(', ')); return 1; }
    console.log('PASS: 参数快照 / 刷新恢复 / 恢复内置 都正常');
    return 0;
  } finally {
    try { ws && ws.close(); } catch (e) {}
    chrome.kill();
  }
};

main().then(c => process.exit(c)).catch(e => { console.error('ERR', e.message); process.exit(2); });
