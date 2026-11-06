#!/usr/bin/env node
/*
 * web_logic_probe.js — 把 web/app.html 的纯逻辑抠出来在 Node 里跑一遍。
 *
 * 为什么需要：工作流扫描 + 参数映射在浏览器里是 JS、在命令行里是 Python，
 * 两份实现一旦漂移，就会出现「CLI 跑得通、网页跑出来不一样」这种最难查的 bug。
 * 这里原样提取 HTML 里的那段代码执行，让 Python 侧拿输出来对比（见 test_web_logic.py）。
 *
 * 用法：
 *   node web_logic_probe.js scan  [workflow.json]   # 只扫描，输出推导出的参数表
 *   node web_logic_probe.js read  [workflow.json]   # 扫描 + 还原当前参数值
 *   node web_logic_probe.js '{...}' [workflow.json] # 扫描 + 应用参数，输出工作流
 */

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'web', 'app.html');
const DEFAULT_WF = path.join(ROOT, 'workflow', 'moss.json');
const START_MARK = '/* ==== 纯逻辑开始';
const END_MARK = '/* ==== 纯逻辑结束';

function main() {
  const html = fs.readFileSync(HTML, 'utf8');
  const a = html.indexOf(START_MARK);
  const b = html.indexOf(END_MARK);
  if (a < 0 || b < 0 || b <= a) {
    console.error('无法在 app.html 里定位纯逻辑段（检查标记注释）');
    return 2;
  }
  const code = html.slice(a, b);

  const ctx = {console, JSON, Math, String, Number, Object, Array, Error,
               isNaN, parseInt, parseFloat, undefined};
  vm.createContext(ctx);
  try {
    vm.runInContext(code +
      '\nthis.__api = {scanWorkflow, readParams, applyParams, flattenFields,' +
      ' asLoraList, ADAPTERS, randomSeed};', ctx);
  } catch (e) {
    console.error('提取的代码执行失败：' + e.message);
    return 2;
  }
  const api = ctx.__api;
  if (!api || !api.scanWorkflow) {
    console.error('没抓到 scanWorkflow');
    return 2;
  }

  const mode = process.argv[2] || 'scan';
  const wfPath = process.argv[3] || DEFAULT_WF;
  const workflow = JSON.parse(fs.readFileSync(wfPath, 'utf8'));
  const spec = api.scanWorkflow(workflow);

  let out;
  if (mode === 'scan') {
    out = {spec: spec};
  } else if (mode === 'read') {
    out = {params: api.readParams(workflow, spec)};
  } else {
    const r = api.applyParams(workflow, spec, JSON.parse(mode));
    out = {workflow: r.wf, changes: r.changes, resolved: r.resolved};
  }
  process.stdout.write(JSON.stringify(out));
  return 0;
}

process.exit(main());
