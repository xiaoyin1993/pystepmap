// 几种特别的形状：嵌套的短分支、互相调用的函数、循环 + 异常 + finally。
import { call, fn, line, project, task, uncaught } from './builders'

/** if 外层: (if 里层: 步骤 → 失败 / 否则 跳过) 否则 步骤 → 完成 */
export const NESTED = 'a.py::nested@1'
export const nestedFn = fn(
  NESTED,
  [
    ['n1', 'start'],
    ['n2', 'cond', '外层'],
    ['n3', 'cond', '里层'],
    ['n4', 'step'],
    ['n5', 'end', 'A', { end: 'failed' }],
    ['n6', 'end', 'B', { end: 'skipped' }],
    ['n7', 'step'],
    ['n8', 'end', '完成', { end: 'ok' }],
    uncaught('n9'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n3', 'n4', 'yes'],
    ['n4', 'n5', 'next'],
    ['n3', 'n6', 'no'],
    ['n2', 'n7', 'no'],
    ['n7', 'n8', 'next'],
  ],
)
export const nestedProject = project([nestedFn], [line(task(NESTED), [nestedFn])])

/** ping 调用 pong，pong 又调用 ping */
export const PING = 'a.py::ping@1'
export const PONG = 'a.py::pong@20'
const pingFn = fn(
  PING,
  [['n1', 'start'], ['n2', 'step', '叫 pong', { calls: [call(PONG)] }], ['n3', 'end', '完成', { end: 'ok' }]],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
)
const pongFn = fn(
  PONG,
  [['n1', 'start'], ['n2', 'step', '叫 ping', { calls: [call(PING)] }], ['n3', 'end', '完成', { end: 'ok' }]],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
  ],
)
export const pingPongProject = project([pingFn, pongFn])

/**
 * 排版难点都在一个函数里：for 循环（回边）、try 体以 if 开头而 except 只写日志（raise 与"否"并行落到同一节点）、
 * 循环里的 continue 穿过 finally、未捕获异常退出。
 */
export const TRICKY = 'a.py::tricky@1'
export const trickyFn = fn(
  TRICKY,
  [
    ['n1', 'start', '开始'],
    ['n2', 'step', '取一批数据', { subtitle: 'load_batch' }],
    ['n3', 'loop', 'for item in batch', { subtitle: 'for item in batch' }],
    ['n4', 'cond', '跳过这一条？', { subtitle: 'item.skip' }],
    ['n5', 'step', '处理一条', { subtitle: 'handle' }],
    ['n6', 'cond', '结果有问题？', { subtitle: 'bad(result)' }],
    ['n7', 'step', '记下问题', { subtitle: 'record' }],
    ['n8', 'step', 'finally 里释放锁', { subtitle: 'release' }],
    ['n9', 'step', '汇总', { subtitle: 'summarize' }],
    ['n10', 'end', '完成', { end: 'ok' }],
    uncaught('n11'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'loop_each'],
    ['n4', 'n8', 'finally', '是'],
    ['n4', 'n5', 'no'],
    ['n5', 'n6', 'next'],
    ['n6', 'n7', 'yes'],
    ['n6', 'n8', 'no'],
    ['n6', 'n8', 'raise'],
    ['n7', 'n8', 'next'],
    ['n8', 'n3', 'loop_back'],
    ['n3', 'n9', 'loop_exit'],
    ['n9', 'n10', 'next'],
  ],
)
export const trickyProject = project([trickyFn], [line(task(TRICKY), [trickyFn])])

/**
 * 循环里的 try / finally，分支里的 continue 穿过 finally（评审找到的形状）：
 * for x in xs: try: if bad(x): log(x); if fatal(x): raise E; continue / work(x)  finally: release()
 * "否"那条（continue）从判断节点经 finally 连线进入 finally 体，是接着往下走，不是结局。
 */
export const FIN = 'a.py::fin@1'
export const finFn = fn(
  FIN,
  [
    ['n1', 'start'],
    ['n2', 'loop', 'for x in xs', { subtitle: 'for x in xs' }],
    ['n3', 'cond', '这一条有问题？', { subtitle: 'bad(x)' }],
    ['n4', 'step', '记日志', { subtitle: 'log' }],
    ['n5', 'cond', '严重？', { subtitle: 'fatal(x)' }],
    ['n6', 'end', '抛出 E', { end: 'failed' }],
    ['n7', 'step', '处理', { subtitle: 'work' }],
    ['n8', 'step', '释放', { subtitle: 'release' }],
    ['n9', 'step', '收尾', { subtitle: 'done' }],
    ['n10', 'end', '完成', { end: 'ok' }],
    uncaught('n11'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'loop_each'],
    ['n3', 'n4', 'yes'],
    ['n4', 'n5', 'next'],
    ['n5', 'n6', 'yes'],
    ['n6', 'n8', 'finally'],
    ['n5', 'n8', 'finally', '否', 'no'],
    ['n3', 'n7', 'no'],
    ['n7', 'n8', 'next'],
    ['n8', 'n2', 'loop_back'],
    ['n2', 'n9', 'loop_exit'],
    ['n9', 'n10', 'next'],
  ],
)
export const finProject = project([finFn], [line(task(FIN), [finFn])])

/**
 * 提前结束的分支以一个和别处共用的结局收尾：判断 A 是 → 两步 → 拒绝（失败）；否 → 一步 → 判断 B，
 * B 是 → 同一个"拒绝"；否 → 一步 → 完成。
 */
export const SHARED = 'a.py::shared@1'
export const sharedFn = fn(
  SHARED,
  [
    ['n1', 'start'],
    ['n2', 'cond', '来源可疑？'],
    ['n3', 'step', '记下来源'],
    ['n4', 'step', '告警'],
    ['n5', 'step', '校验'],
    ['n6', 'cond', '校验失败？'],
    ['n7', 'step', '入库'],
    ['n8', 'end', '完成', { end: 'ok' }],
    ['n9', 'end', '拒绝', { end: 'failed' }],
    uncaught('n10'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'yes'],
    ['n3', 'n4', 'next'],
    ['n4', 'n9', 'next'],
    ['n2', 'n5', 'no'],
    ['n5', 'n6', 'next'],
    ['n6', 'n9', 'yes'],
    ['n6', 'n7', 'no'],
    ['n7', 'n8', 'next'],
  ],
)
export const sharedProject = project([sharedFn], [line(task(SHARED), [sharedFn])])

/**
 * 循环节点的出边挤在一起（按一个真实项目里标签被压住的循环照搬形状，文字是编的）。
 * whileRaise：while True 的"条件成立"、"循环结束"和 raise 三条出边，raise 在中间、正好穿过"条件成立"的标签。
 */
export const WHILE_RAISE = 'a.py::serve@1'
export const whileRaiseFn = fn(
  WHILE_RAISE,
  [
    ['n1', 'start', 'serve'],
    ['n2', 'step', '先发全量快照，再把队列消息逐条推出去。', { subtitle: 'sender' }],
    ['n3', 'loop', 'while True', { subtitle: 'while True' }],
    ['n4', 'step', 'ws.receive_text', { subtitle: 'ws.receive_text' }],
    ['n5', 'step', 'hub.unsubscribe', { subtitle: 'hub.unsubscribe' }],
    ['n6', 'end', '执行到函数末尾', { end: 'ok', sid: 'end:implicit', line: 0 }],
    uncaught('n7'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'next'],
    ['n3', 'n4', 'loop_each'],
    ['n4', 'n3', 'loop_back'],
    ['n3', 'n5', 'loop_exit'],
    ['n3', 'n5', 'raise'],
    ['n5', 'n6', 'next'],
  ],
)
export const whileRaiseProject = project([whileRaiseFn], [line(task(WHILE_RAISE), [whileRaiseFn])])

/**
 * retry：for 循环的"每一项"、"循环结束"和落到异常处理的 raise 三条出边，ELK 把它们按节点宽度均匀排开、间距不到
 * 40 像素，中间那条的标签哪一侧都放不下（不加宽循环节点时"循环结束"被"每一项"穿过、"每一项"被 raise 穿过）。
 */
export const RETRY = 'a.py::retry@1'
export const retryFn = fn(
  RETRY,
  [
    ['n1', 'start', 'retry'],
    ['n2', 'loop', 'for _ in range(5)', { subtitle: 'for _ in range(5)' }],
    ['n3', 'step', '等训练集和基座都准备好，最多等五轮', { subtitle: 'step_wait_ready' }],
    ['n4', 'cond', 'wait["action"] != "start"', { subtitle: 'wait["action"] != "start"' }],
    ['n5', 'cond', 'wait["action"] == "start"', { subtitle: 'wait["action"] == "start"' }],
    ['n6', 'step', '开始跑：置 running、画像', { subtitle: 'step_profile' }],
    ['n7', 'end', '返回 "ok"', { end: 'ok' }],
    ['n8', 'end', '返回 "failed"', { end: 'failed' }],
    ['n9', 'catch', '捕获 Exception', { subtitle: 'Exception' }],
    ['n10', 'step', '兜底：运行未终态则置 failed', { subtitle: 'step_mark_failed' }],
    ['n11', 'end', '返回 "failed"', { end: 'failed' }],
    uncaught('n12'),
  ],
  [
    ['n1', 'n2', 'next'],
    ['n2', 'n3', 'loop_each'],
    ['n3', 'n4', 'next'],
    ['n4', 'n2', 'loop_back', '否', 'no'],
    ['n4', 'n5', 'break', '是', 'yes'],
    ['n2', 'n5', 'loop_exit'],
    ['n5', 'n6', 'yes'],
    ['n5', 'n8', 'no'],
    ['n6', 'n7', 'next'],
    ['n2', 'n9', 'raise'],
    ['n9', 'n10', 'next'],
    ['n10', 'n11', 'next'],
  ],
)
export const retryProject = project([retryFn], [line(task(RETRY), [retryFn])])
