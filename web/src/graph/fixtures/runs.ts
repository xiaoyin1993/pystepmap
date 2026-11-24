// 运行路径页签的测试用小项目（runview.test.ts、runstate.test.ts 共用）和手写运行路径的简便写法。
// 函数形状用 runplace 的 functionShape（runplace.test.ts 另外对过它和 node 的 sha1 一样）。
import type { FunctionRunJson, RunPathsJson, TestRunJson } from '../../api'
import { functionShape } from '../../runplace'
import type { FunctionGraph, ProjectGraph } from '../types'
import { call, dispatch, fn, line, project, task, uncaught } from './builders'

// 一个小项目：
//   转账 run（根任务）：开始 → 校验（调用 check）→ 金额不对？（判断节点调用判断函数 big，画布上钻入不到）
//                       ─是→ 记下拒绝原因 → 拒绝（失败；提前结束的分支，默认折叠）
//                                                    └否→ 逐笔（循环）→ 记账（调用 post）→ 回到逐笔；循环结束 → 派发通知（派发 send）→ 完成
//   check：开始 → 超限？─是→ 抛出（raise 语句的结局）/ 否 → 通过
//   post：开始 → 写库 → 返回
//   send（子任务）：开始 → 发消息 → 完成
//   big（判断函数，@condition）：开始 → 太大了？→ 是 / 否
export const K = {
  run: 'wf.py::W.run@1',
  check: 'wf.py::W.check@20',
  post: 'wf.py::W.post@30',
  send: 'tasks.py::send@1',
  big: 'wf.py::W.too_big@40',
}
export const run = fn(
  K.run,
  [
    ['n1', 'start', '转账'],
    ['n2', 'step', '校验', { calls: [call(K.check)] }],
    ['n3', 'cond', '金额不对？', { calls: [call(K.big)] }],
    ['n10', 'step', '记下拒绝原因', { line: 4 }],
    ['n4', 'end', '拒绝', { end: 'failed' }],
    ['n5', 'loop', '逐笔', { subtitle: 'for e in entries' }],
    ['n6', 'step', '记账', { calls: [call(K.post)] }],
    ['n7', 'step', '派发通知', { calls: [dispatch(K.send)] }],
    ['n8', 'end', '完成', { end: 'ok' }],
    uncaught('n9'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n10', 'yes'],
    ['n10', 'n4', 'next'],
    ['n3', 'n5', 'no'],
    ['n5', 'n6', 'loop_each'],
    ['n6', 'n5', 'loop_back'],
    ['n5', 'n7', 'loop_exit'],
    ['n7', 'n8', 'next'],
  ],
  { kind: 'task', title: '转账', call_targets: [K.check, K.big, K.post] },
)
export const check = fn(
  K.check,
  [
    ['n1', 'start', '检查'],
    ['n2', 'cond', '超限？'],
    ['n3', 'end', '抛出超限', { end: 'failed', sid: 'end:raise1' }],
    ['n4', 'end', '通过', { end: 'ok' }],
    uncaught('n5'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n2', 'n4', 'no'],
  ],
  { kind: 'condition', title: '检查' },
)
export const post = fn(
  K.post,
  [['n1', 'start', '记一笔'], ['n2', 'step', '写库'], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
)
export const send = fn(
  K.send,
  [['n1', 'start', '通知'], ['n2', 'step', '发消息'], ['n3', 'end', '完成', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { kind: 'task', title: '通知' },
)
export const big = fn(
  K.big,
  [['n1', 'start', '太大了？'], ['n2', 'cond', '超过一万？'], ['n3', 'end', '是', { end: 'ok', sid: 'end:yes' }], ['n4', 'end', '否', { end: 'ok', sid: 'end:no' }], uncaught('n5')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n2', 'n4', 'no'],
  ],
  { kind: 'condition', title: '太大了？' },
)
export const all = [run, check, post, send, big]
export const mainLine = line(task(K.run, [task(K.send)]), all)
export const graph: ProjectGraph = project(all, [mainLine])
export const LINE = mainLine.sid

export const fsid = (f: FunctionGraph) => f.sid

/** 一个函数的运行汇总：nodes 写 [节点 sid, 次数, 耗时] */
export function fnRun(f: FunctionGraph, nodes: [string, number, number][], edges: Record<string, number>, exits: Record<string, number>, hasTime = true): FunctionRunJson {
  return {
    function: f.sid,
    executions: 1,
    nodes: Object.fromEntries(nodes.map(([s, visits, ns]) => [s, { visits, ns }])),
    edges,
    exits,
    first_path: nodes.map(([s]) => s),
    off_graph: 0,
    has_time: hasTime,
  }
}

export function paths(tests: TestRunJson[], fns: FunctionGraph[] = all, extra: Partial<RunPathsJson> = {}): RunPathsJson {
  return {
    version: 1,
    tests,
    shapes: Object.fromEntries(fns.map((f) => [f.sid, { shape: functionShape(f), dups: [] }])),
    function_dups: [],
    ...extra,
  }
}

export const T_OK = 'tests/test_wf.py::test_ok'
/** 通过的测试：校验通过，循环两圈，派发通知 */
export const okTest: TestRunJson = {
  test: T_OK,
  outcome: 'passed',
  seconds: 0.01,
  cases: 1,
  functions: {
    [fsid(run)]: fnRun(
      run,
      [
        ['start', 1, 0],
        ['step:n2', 1, 4_000_000],
        ['cond:n3', 1, 1_000],
        ['loop:n5', 3, 3_000],
        ['step:n6', 2, 2_000_000],
        ['step:n7', 1, 990_000],
        ['end:n8', 1, 6_000],
      ],
      { 'start|step:n2': 1, 'step:n2|cond:n3': 1, 'cond:n3|loop:n5': 1, 'loop:n5|step:n6': 2, 'step:n6|loop:n5': 2, 'loop:n5|step:n7': 1, 'step:n7|end:n8': 1, 'start|end:n8': 1 },
      { 'end:n8|return|': 1 },
    ),
    [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 1_000_000], ['end:n4', 1, 0]], { 'start|cond:n2': 1, 'cond:n2|end:n4': 1 }, { 'end:n4|return|': 1 }),
    [fsid(post)]: fnRun(post, [['start', 2, 0], ['step:n2', 2, 1_800_000], ['end:n3', 2, 0]], { 'start|step:n2': 2, 'step:n2|end:n3': 2 }, { 'end:n3|return|': 2 }),
    [fsid(send)]: fnRun(send, [['start', 1, 0], ['step:n2', 1, 500_000], ['end:n3', 1, 0]], { 'start|step:n2': 1, 'step:n2|end:n3': 1 }, { 'end:n3|return|': 1 }),
  },
}
