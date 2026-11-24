// 一个小项目：接口 entry 派发告警任务、任务 A（A 再派发 B、C，C 又派发 B）、经 helper2 派发 B；
// 公共任务 notify 不进功能线；empty 是没有节点的任务。
//
// 功能线任务树（照引擎 lines.py 的展开顺序）：
//   entry ─┬─ task_alert
//          ├─ task_a ─┬─ task_b
//          │          └─ task_c ── task_b（goto）
//          └─ task_b（goto）
import { call, dispatch, fn, line, project, task, uncaught } from './builders'

export const K = {
  entry: 'api.py::entry@1',
  alert: 'tasks.py::task_alert@5',
  a: 'tasks.py::task_a@10',
  b: 'tasks.py::task_b@20',
  c: 'tasks.py::task_c@30',
  notify: 'tasks.py::notify@50',
  empty: 'tasks.py::empty@60',
  helper: 'helpers.py::helper@1',
  helper2: 'helpers.py::helper2@5',
}

export const entry = fn(
  K.entry,
  [
    ['n1', 'start', '接收请求'],
    ['n2', 'step', '校验参数', { subtitle: 'helper', calls: [call(K.helper)] }],
    ['n3', 'cond', '参数不对？', { subtitle: 'bad' }],
    ['n4', 'step', '发告警', { subtitle: '派发 task_alert', calls: [dispatch(K.alert)] }],
    ['n5', 'end', '拒绝', { end: 'failed' }],
    ['n6', 'step', '派发任务 A', { subtitle: '派发 task_a', calls: [dispatch(K.a)] }],
    ['n7', 'step', '经 helper2 派发 B', { subtitle: 'helper2', calls: [call(K.helper2, { dispatches: [K.b] })] }],
    ['n8', 'end', '已受理', { end: 'ok' }],
    uncaught('n9'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'yes'],
    ['n4', 'n5', 'next'],
    ['n3', 'n6', 'no'],
    ['n6', 'n7', 'next'],
    ['n7', 'n8', 'next'],
  ],
  { kind: 'route', route: 'POST /x', title: '接收请求' },
)

export const taskAlert = fn(
  K.alert,
  [['n1', 'start', '告警'], ['n2', 'step', '发消息', { calls: [call(K.helper)] }], ['n3', 'end', '结束', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { kind: 'task', title: '告警任务' },
)

export const taskA = fn(
  K.a,
  [
    ['n1', 'start', '任务 A'],
    ['n2', 'step', '派发 B', { calls: [dispatch(K.b)] }],
    ['n3', 'step', '派发 C 并通知', { calls: [dispatch(K.c), dispatch(K.notify)] }],
    ['n4', 'end', '完成', { end: 'ok' }],
    uncaught('n5', '未捕获异常退出（任务会重试）'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'next'],
  ],
  { kind: 'task', title: '任务 A', retries: true },
)

export const taskB = fn(
  K.b,
  [['n1', 'start', '任务 B'], ['n2', 'step', '干活', { calls: [call(null, { name: 'x.y' })] }], ['n3', 'end', '完成', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { kind: 'task', title: '任务 B' },
)

export const taskC = fn(
  K.c,
  [['n1', 'start', '任务 C'], ['n2', 'step', '再派发 B', { calls: [dispatch(K.b, { resolution: 'guessed' })] }], ['n3', 'end', '完成', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { kind: 'task', title: '任务 C' },
)

export const notify = fn(K.notify, [['n1', 'start', '通知'], ['n2', 'end', '结束', { end: 'ok' }], uncaught('n3')], [['n1', 'n2', 'next']], {
  kind: 'task',
  title: '通知',
})

export const empty = fn(K.empty, [], [], { kind: 'task', title: '空任务' })

export const helper = fn(
  K.helper,
  [['n1', 'start', 'helper'], ['n2', 'step', '查库', { calls: [call(null, { name: 'db.get', resolution: 'external' })] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '校验参数（helper）' },
)

export const helper2 = fn(
  K.helper2,
  [['n1', 'start', 'helper2'], ['n2', 'step', '派发 B', { calls: [dispatch(K.b)] }], ['n3', 'end', '返回', { end: 'ok' }], uncaught('n4')],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
  { title: '帮忙派发 B' },
)

const all = [entry, taskAlert, taskA, taskB, taskC, notify, empty, helper, helper2]

export const miniLine = line(
  task(K.entry, [task(K.alert), task(K.a, [task(K.b), task(K.c, [task(K.b, [], true)])]), task(K.b, [], true)]),
  all,
)

/** 根任务不在图里的线、根任务没有节点的线 */
export const missingLine = line(task('gone.py::gone@1', [task(K.b)]), all)
export const emptyLine = line(task(K.empty), all)

export const mini = project(all, [miniLine, missingLine, emptyLine], [K.notify])
