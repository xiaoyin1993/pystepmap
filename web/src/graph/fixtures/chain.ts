// 钻入链的测试用小项目：一个步骤（一条语句）调用了几个能钻入的函数，可以同时钻入，几张钻入卡片排成一串
// （viewmodel、layout、diffview、runview、replay 的测试共用）。
//   main（根任务）：开始 → 生成输出（out = [a(r, b()) for r in rows]：调用 a、b）→ 算 x（x = a(b(c()))：调用 a、b、c）
//                   → 有问题？─是→ 记下问题（调用 a、b）→ 失败（提前结束的短分支，默认折叠）
//                              └否→ 完成
//   a：开始 → 叫 c 和 d（调用 c、d）→ 返回        b：开始 → 要跳过？─是→ 跳过 / 否→ 好
//   c：开始 → 派发 t → 返回                       d：开始 → 做事 → 返回
//   t（main 派发的子任务，经 a → c 或直接经 c）：开始 → 完成
import { call, dispatch, fn, line, project, task, uncaught } from './builders'

export const CK = {
  main: 'm.py::main@1',
  a: 'm.py::a@20',
  b: 'm.py::b@40',
  c: 'm.py::c@60',
  d: 'm.py::d@80',
  t: 'tasks.py::t@1',
}

const viaT = { dispatches: [CK.t] }

export const mainFn = fn(
  CK.main,
  [
    ['n1', 'start', '主流程'],
    ['n2', 'step', '生成输出', { subtitle: 'out = [a(r, b()) for r in rows]', calls: [call(CK.a, viaT), call(CK.b)] }],
    ['n3', 'step', '算 x', { subtitle: 'x = a(b(c()))', calls: [call(CK.a, viaT), call(CK.b), call(CK.c, viaT)] }],
    ['n4', 'cond', '有问题？', { subtitle: 'bad(x)' }],
    ['n5', 'step', '记下问题', { subtitle: 'a(b())', calls: [call(CK.a, viaT), call(CK.b)] }],
    ['n6', 'end', '失败', { end: 'failed' }],
    ['n7', 'end', '完成', { end: 'ok' }],
    uncaught('n8'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'next'],
    ['n4', 'n5', 'yes'],
    ['n5', 'n6', 'next'],
    ['n4', 'n7', 'no'],
  ],
  { kind: 'route', title: '主流程', route: 'POST /main', call_targets: [CK.a, CK.b, CK.c] },
)
export const aFn = fn(
  CK.a,
  [['n1', 'start', 'a'], ['n2', 'step', '叫 c 和 d', { subtitle: 'c(d())', calls: [call(CK.c, viaT), call(CK.d)] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '函数 a', call_targets: [CK.c, CK.d] },
)
export const bFn = fn(
  CK.b,
  [
    ['n1', 'start', 'b'],
    ['n2', 'cond', '要跳过？'],
    ['n3', 'end', '跳过', { end: 'skipped' }],
    ['n4', 'end', '好', { end: 'ok' }],
    uncaught('n5'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n2', 'n4', 'no'],
  ],
  { title: '函数 b' },
)
export const cFn = fn(
  CK.c,
  [['n1', 'start', 'c'], ['n2', 'step', '派发 t', { calls: [dispatch(CK.t)] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '函数 c' },
)
export const dFn = fn(
  CK.d,
  [['n1', 'start', 'd'], ['n2', 'step', '做事'], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '函数 d' },
)
export const tFn = fn(
  CK.t,
  [['n1', 'start', '任务 t'], ['n2', 'end', '完成', { end: 'ok' }], uncaught('n3')],
  [['n1', 'n2', 'next']],
  { kind: 'task', title: '任务 t' },
)
export const chainFns = [mainFn, aFn, bFn, cFn, dFn, tFn]
export const chainLine = line(task(CK.main, [task(CK.t)]), chainFns)
export const chainProject = project(chainFns, [chainLine])
export const CL = chainLine.sid
/** 根卡片 id、根卡片里节点的 id、钻入 id（步骤节点 id + > + 被调函数 sid） */
export const ROOT = `${CL}|${mainFn.sid}`
export const at = (sid: string, host = ROOT) => `${host}/${sid}`
export const into = (step: string, fnKey: string) => `${step}>${chainFns.find((f) => f.key === fnKey)!.sid}`
