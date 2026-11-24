// runfailure.ts：失败的测试停在哪（failureStop）和运行条、节点详情上的说明文字。
import { describe, expect, it } from 'vitest'
import type { FunctionRunJson, RunPathsJson, TestRunJson } from './api'
import { revealStop } from './diffview'
import { buildLineView, emptyState, type FunctionGraph, type ProjectGraph } from './graph'
import { call, fn, line, project, task, uncaught } from './graph/fixtures/builders'
import { K, LINE, all, big, check, fnRun, fsid, graph, post, run, send } from './graph/fixtures/runs'
import { failureBarText, failureStop, failureText, unknownIntro } from './runfailure'
import { functionShape } from './runplace'
import { buildRunOverlay, runMarks } from './runview'

describe('failureStop（裁决 S11e：按记下的异常类型和真实调用）', () => {
  /** 一个失败的测试：functions 里每项可以带 calls（被调函数的 sid → 次数） */
  const failed = (functions: Record<string, FunctionRunJson>, exc: string, excs: string[] = [exc]): TestRunJson => ({ test: 'f', outcome: 'failed', seconds: 0, cases: 1, functions, exc, excs })
  const c = (r: FunctionRunJson, calls: Record<string, number>): FunctionRunJson => ({ ...r, calls })
  const on = (g: ProjectGraph, fns: FunctionGraph[], t: TestRunJson, lineSid: string) => {
    const p: RunPathsJson = { version: 1, tests: [t], shapes: Object.fromEntries(fns.map((f) => [f.sid, { shape: functionShape(f), dups: [] }])), function_dups: [] }
    const f = failureStop(buildRunOverlay(g, p, t.test, lineSid), g, lineSid)!
    return { f, text: failureBarText(f, g) }
  }
  const main1 = (t: TestRunJson) => on(graph, all, t, LINE)

  describe('异常到了测试（有入口以那种异常离开）', () => {
    it('根任务以 ValueError 离开，记下的调用到 check（raise 语句）：停在 check 的那个结局，钻入 run 的"校验"', () => {
      const { f, text } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }), { [fsid(check)]: 1 }),
            [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:raise1|raise|ValueError': 1 }),
          },
          'ValueError',
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.check, nodeId: 'n3', raisedIn: null, dynamic: false })
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n2' }] })
      expect(text).toBe('停在「抛出超限」（ValueError）')
    })

    it('判断节点调用的判断函数里抛出（钻入不到）：红圈画在调用它的判断节点上', () => {
      const { f, text } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 1, 0], ['step:n2', 1, 5], ['cond:n3', 1, 5]], { 'start|step:n2': 1, 'step:n2|cond:n3': 1 }, { 'end:uncaught|raise|RuntimeError': 1 }), { [fsid(check)]: 1, [fsid(big)]: 1 }),
            [fsid(check)]: fnRun(check, [['start', 1, 0], ['end:n4', 1, 0]], {}, { 'end:n4|return|': 1 }),
            [fsid(big)]: fnRun(big, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:uncaught|raise|RuntimeError': 1 }),
          },
          'RuntimeError',
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.run, nodeId: 'n3', raisedIn: { functionKey: K.big, title: '太大了？' } })
      expect(text).toBe('停在「金额不对？」：异常是在它调用的「太大了？」里抛出的（RuntimeError）')
    })

    it('经图上连不出来的调用（动态调用 / 回调）到 post 里抛出：红圈画在执行停在那里的节点上，说明是动态调用', () => {
      // run 在"校验"这一步动态地调用了 post（图上"校验"只调用 check），post 抛出 KeyError
      const { f, text } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|KeyError': 1 }), { [fsid(post)]: 1 }),
            [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }),
          },
          'KeyError',
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.run, nodeId: 'n2', dynamic: true, raisedIn: { functionKey: K.post, title: 'W.post' } })
      expect(text).toBe('停在「校验」：异常是经图上连不出来的调用（动态调用 / 回调）到「W.post」里抛出的（KeyError）')
      expect(failureText(f)).toBe('异常是经图上连不出来的调用（动态调用 / 回调）到「W.post」里抛出的（KeyError）')
    })

    it('链上有一环连不出来（框架的包装函数），再往下是这条线的任务：跳到那个任务的卡片，停在它真正抛出的地方', () => {
      // run 经"校验"调用 check；check 记下调用了 send（图上 check 不调用 send：经包装函数等连不出来），send 是这条线的子任务
      const { f, text } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }), { [fsid(check)]: 1 }),
            [fsid(check)]: c(fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1 }), { [fsid(send)]: 1 }),
            [fsid(send)]: fnRun(send, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1 }),
          },
          'ValueError',
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.send, nodeId: 'n4', dynamic: false, raisedIn: null })
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ task: K.send, path: [] })
      expect(text).toBe('停在「未捕获异常退出」（ValueError）')
    })

    it('两个任务：根任务经派发调用子任务，子任务抛出同一种异常 → 停在子任务里（跳到子任务的卡片）', () => {
      const { f } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 1, 0], ['step:n7', 1, 5]], {}, { 'end:uncaught|raise|RuntimeError': 1 }), { [fsid(send)]: 1 }),
            [fsid(send)]: fnRun(send, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|RuntimeError': 1 }),
          },
          'RuntimeError',
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.send, nodeId: 'n4' })
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ task: K.send, path: [] })
    })

    it('参数化的几个用例各失败一种：红圈取线上最深的、再按类型名，其余的在另有里', () => {
      const { f, text } = main1(
        failed(
          {
            [fsid(run)]: c(fnRun(run, [['start', 2, 0], ['step:n2', 2, 5], ['cond:n3', 1, 5], ['loop:n5', 1, 5], ['step:n6', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1, 'end:uncaught|raise|KeyError': 1 }), { [fsid(check)]: 2, [fsid(post)]: 1 }),
            [fsid(check)]: fnRun(check, [['start', 2, 0]], {}, { 'end:raise1|raise|ValueError': 1, 'end:n4|return|': 1 }),
            [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }),
          },
          'KeyError',
          ['KeyError', 'ValueError'],
        ),
      )
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.post, exc: 'KeyError' })
      expect(text).toBe('停在「未捕获异常退出」（KeyError）；另有：ValueError（检查）')
    })

    it('流程里的 assert 抛出的 AssertionError 从入口出去了：也是停点（不是测试自己的断言）', () => {
      const { f } = main1(failed({ [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|AssertionError': 1 }) }, 'AssertionError'))
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.run, nodeId: 'n9' })
    })

    it('入口在这条线上、不是任务（没有记录到流程里的函数调用它）：只说记下的事；流程没跑过时不画红圈，只能定位到函数', () => {
      const { f, text } = main1(failed({ [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }) }, 'KeyError'))
      expect(f).toMatchObject({ kind: 'direct', functionKey: K.post, nodeId: 'n4', reveal: null, repeated: null })
      // 只是定位（经任一调用处，不说测试走过那里）：run 在这个测试里没跑过，不钻到它下面画红圈
      expect(f.kind === 'direct' && f.locate).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n6' }] })
      expect(text).toBe('「W.post」以 KeyError 离开，没有记录到流程里的函数调用它（可能是测试直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）')
      expect(failureText(f)).toBe(
        '这个测试没有经过这里的调用，只是定位到函数：「W.post」以 KeyError 离开，没有记录到流程里的函数调用它（可能是测试直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）',
      )
    })

    it('同上，但流程跑过（调用没记下：经线程、并发任务）：画布上只经执行过的函数到得了，红圈画在它的停点上', () => {
      const { f } = main1(
        failed(
          {
            [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:n8|return|': 1 }),
            [fsid(check)]: fnRun(check, [['start', 1, 0]], {}, { 'end:n4|return|': 1 }),
            [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|KeyError': 1 }),
          },
          'KeyError',
        ),
      )
      expect(f).toMatchObject({ kind: 'direct', functionKey: K.post, nodeId: 'n4', locate: null })
      expect(f.kind === 'direct' && f.reveal).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n6' }] })
    })

    it('经库的包装函数（icalls）调用的步骤抛出：不是入口，顺着间接调用钻进去停在它真正抛出的地方', () => {
      const t = failed(
        {
          [fsid(run)]: { ...fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }), icalls: { [fsid(check)]: 1 } },
          [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:raise1|raise|ValueError': 1 }),
        },
        'ValueError',
      )
      const { f, text } = main1(t)
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.check, nodeId: 'n3', others: [] })
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ task: K.run, path: [{ fn: K.run, node: 'n2' }] })
      expect(text).toBe('停在「抛出超限」（ValueError）')
    })

    it('另有里不列：没记下调用方的函数，图上有执行过的函数调用它、那个函数也以这种异常离开（异常经它走，已经算在它那一项里）', () => {
      // run 和 check 都以 ValueError 离开；check 的调用没记下（经并发任务），run 在图上调用 check
      const t = failed(
        {
          [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }),
          [fsid(check)]: fnRun(check, [['start', 1, 0], ['cond:n2', 1, 5]], {}, { 'end:raise1|raise|ValueError': 1 }),
          [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1 }),
        },
        'ValueError',
      )
      const { f, text } = main1(t)
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.run, nodeId: 'n9' })
      // check、post 都不列：run 在图上调用它们，run 也以 ValueError 离开
      expect(text).toBe('停在「未捕获异常退出」（ValueError）')
      // 调用它们的 run 以别的异常离开时照列（线上深的在前）
      const t2 = failed({ ...t.functions, [fsid(run)]: fnRun(run, [['start', 1, 0]], {}, { 'end:uncaught|raise|KeyError': 1 }) }, 'ValueError', ['KeyError', 'ValueError'])
      expect(main1(t2).text).toBe(
        '停在「未捕获异常退出」（KeyError）；另有：ValueError（W.post，没有记录到调用它的流程函数）、ValueError（检查，没有记录到调用它的流程函数）',
      )
    })

    it('入口以这种异常离开不止一次（pytest.raises 预期了一次，之后又真的失败）：红圈照旧画一处，说抛出过几次、列出别的抛出处', () => {
      const t = failed(
        {
          [fsid(run)]: c(
            fnRun(run, [['start', 2, 0], ['step:n2', 2, 5], ['cond:n3', 1, 5], ['loop:n5', 1, 5], ['step:n6', 1, 5]], { 'start|step:n2': 2, 'step:n2|cond:n3': 1, 'cond:n3|loop:n5': 1, 'loop:n5|step:n6': 1 }, { 'end:uncaught|raise|ValueError': 2 }),
            { [fsid(check)]: 2, [fsid(post)]: 1 },
          ),
          [fsid(check)]: fnRun(check, [['start', 2, 0], ['cond:n2', 2, 5]], {}, { 'end:raise1|raise|ValueError': 1, 'end:n4|return|': 1 }),
          [fsid(post)]: fnRun(post, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1 }),
        },
        'ValueError',
      )
      const { f, text } = main1(t)
      expect(f).toMatchObject({ kind: 'stop', functionKey: K.post, nodeId: 'n4', repeated: { n: 2, elsewhere: [{ functionKey: K.check, nodeId: 'n3', title: '「检查」的「抛出超限」' }] } })
      expect(text).toBe(
        '停在「未捕获异常退出」（ValueError）；这种异常在这个测试里抛出过 2 次，红圈画的是其中一处（另一处可能是测试预期的那一次）；别的抛出处：「检查」的「抛出超限」',
      )
      // 运行条把别的抛出处列成可以点的：句子里只写到"别的抛出处："
      expect(failureBarText(f, graph, undefined, true)).toBe('停在「未捕获异常退出」（ValueError）；这种异常在这个测试里抛出过 2 次，红圈画的是其中一处（另一处可能是测试预期的那一次）；别的抛出处：')
      // 只抛出过一次：不说
      expect(main1(failed({ [fsid(run)]: fnRun(run, [['start', 1, 0]], {}, { 'end:uncaught|raise|ValueError': 1 }) }, 'ValueError')).f).toMatchObject({ repeated: null })
    })

    it('直接调用的函数画布上钻入不到（判断函数）：direct，不画红圈', () => {
      const { f } = main1(failed({ [fsid(big)]: fnRun(big, [['start', 1, 0]], {}, { 'end:uncaught|raise|TypeError': 1 }) }, 'TypeError'))
      expect(f).toMatchObject({ kind: 'direct', functionKey: K.big, reveal: null })
    })
  })

  describe('几条线', () => {
    // 父流程 P（pa 调用子流程 ra，接住 RuntimeError）、子流程 R（ra 调用 hh）、计数 C（ca → safe → cd 递归）、线外的 outside
    const PA = 'm.py::pa@1'
    const RA = 'm.py::ra@20'
    const CA = 'm.py::ca@40'
    const SAFE = 'm.py::safe@60'
    const CD = 'm.py::cd@80'
    const HH = 'm.py::hh@100'
    const OUT = 'm.py::outside@120'
    const pa = fn(
      PA,
      [['n1', 'start', 'pa'], ['n2', 'step', '调用子流程', { calls: [call(RA)] }], ['n3', 'end', '父完成', { end: 'ok' }], ['n4', 'catch', '接住', { subtitle: 'except RuntimeError' }], ['n5', 'end', '子流程失败', { end: 'failed' }], uncaught('n6')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'next'], ['n2', 'n4', 'raise'], ['n4', 'n5', 'next']],
      { kind: 'task', title: '父流程', call_targets: [RA] },
    )
    const ra = fn(
      RA,
      [['n1', 'start', 'ra'], ['n2', 'step', '调用 hh', { calls: [call(HH)] }], ['n3', 'end', '抛出', { end: 'failed', sid: 'end:ra-raise' }], ['n4', 'end', '完成', { end: 'ok' }], uncaught('n5')],
      [['n1', 'n2', 'next'], ['n2', 'n4', 'next']],
      { kind: 'task', title: '子流程', call_targets: [HH] },
    )
    const ca = fn(
      CA,
      [['n1', 'start', 'ca'], ['n2', 'step', '安全计数', { calls: [call(SAFE)] }], ['n3', 'end', '计数完成', { end: 'ok' }], uncaught('n4')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'next']],
      { kind: 'task', title: '计数', call_targets: [SAFE] },
    )
    const safe = fn(
      SAFE,
      [['n1', 'start', 'safe'], ['n2', 'step', '倒数', { calls: [call(CD)] }], ['n3', 'end', '返回', { end: 'ok' }], ['n4', 'catch', '接住', { subtitle: 'except LookupError' }], ['n5', 'end', '返回 -1', { end: 'ok', sid: 'end:minus' }], uncaught('n6')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'next'], ['n2', 'n4', 'raise'], ['n4', 'n5', 'next']],
      { title: '安全计数', call_targets: [CD] },
    )
    const cd = fn(
      CD,
      [['n1', 'start', 'cd'], ['n2', 'cond', '到底了？'], ['n3', 'end', '抛出', { end: 'failed', sid: 'end:cd-raise' }], ['n4', 'end', '再数', { end: 'ok', sid: 'end:again', calls: [call(CD)] }], uncaught('n5')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'yes'], ['n2', 'n4', 'no']],
      { title: '倒数', call_targets: [CD] },
    )
    const hh = fn(HH, [['n1', 'start', 'hh'], ['n2', 'end', '返回', { end: 'ok' }], uncaught('n3')], [['n1', 'n2', 'next']], { title: 'hh' })
    const outside = fn(OUT, [['n1', 'start', 'outside'], ['n2', 'end', '抛出', { end: 'failed', sid: 'end:out-raise' }]], [], { title: '线外的函数' })
    const all3 = [pa, ra, ca, safe, cd, hh, outside]
    const lineP = line(task(PA), all3)
    const lineR = line(task(RA), all3)
    const lineC = line(task(CA), all3)
    const g3 = project(all3, [lineR, lineP, lineC])
    const at = (t: TestRunJson, ln: { sid: string }) => on(g3, all3, t, ln.sid)

    const parentCaught = failed(
      {
        [pa.sid]: c(fnRun(pa, [['start', 1, 0], ['step:n2', 1, 5], ['catch:n4', 1, 5], ['end:n5', 1, 0]], { 'start|step:n2': 1, 'step:n2|catch:n4': 1, 'catch:n4|end:n5': 1 }, { 'end:n5|return|': 1 }), { [ra.sid]: 1 }),
        [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:ra-raise|raise|RuntimeError': 1 }), { [hh.sid]: 1 }),
        [hh.sid]: fnRun(hh, [['start', 1, 0], ['end:n2', 1, 0]], {}, { 'end:n2|return|': 1 }),
      },
      'AssertionError',
    )

    it('子流程抛出、被父流程接住，测试自己的断言失败：每条线上都是 handled，说被谁接住了，从不画红圈', () => {
      expect(at(parentCaught, lineR).text).toBe('测试是在自己的断言里失败的（「子流程」抛出过 RuntimeError，被调用它的「父流程」接住了）')
      expect(at(parentCaught, lineP).text).toBe('流程正常走完（结局：子流程失败），测试是在自己的断言里失败的（「子流程」抛出过 RuntimeError，被调用它的「父流程」接住了）')
      expect(at(parentCaught, lineC).f).toEqual({ kind: 'handled', ends: [], caught: [], exc: 'AssertionError' })
    })

    it('测试以别的异常失败、流程里没有哪个入口抛出它（setup 出错、测试自己的代码）：说出来，不画红圈', () => {
      const setup = failed({ [ca.sid]: c(fnRun(ca, [['start', 1, 0], ['step:n2', 1, 5], ['end:n3', 1, 0]], {}, { 'end:n3|return|': 1 }), { [safe.sid]: 1 }), [safe.sid]: fnRun(safe, [['start', 1, 0], ['end:n3', 1, 0]], {}, { 'end:n3|return|': 1 }) }, 'RuntimeError')
      expect(at(setup, lineC).text).toBe('流程正常走完（结局：计数完成），测试以 RuntimeError 失败，但流程里没有哪个入口抛出它（异常出在测试自己的代码里，或流程之外）')
      expect(at(setup, lineR).text).toBe('测试以 RuntimeError 失败，但流程里没有哪个入口抛出它（异常出在测试自己的代码里，或流程之外）')
    })

    it('线上的函数抛出过测试失败的那种异常、又被流程调用过（看不出是不是测试直接调用它时抛出的）：不说被接住了，说看不出', () => {
      // ra 调用了 hh（记下的调用），测试又直接调用了 hh、它抛出 OSError、测试以 OSError 失败；hh 不是入口（流程调用过它）
      const both = failed(
        {
          [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5], ['end:n4', 1, 0]], {}, { 'end:n4|return|': 1 }), { [hh.sid]: 1 }),
          [hh.sid]: { ...fnRun(hh, [['start', 2, 0], ['end:n2', 1, 0]], {}, { 'end:n2|return|': 1, 'end:uncaught|raise|OSError': 1 }), executions: 2 },
        },
        'OSError',
      )
      expect(at(both, lineR).text).toBe(
        '流程正常走完（结局：完成），测试以 OSError 失败，但流程里没有哪个入口抛出它（异常出在测试自己的代码里，或流程之外）（「hh」也抛出过 OSError：它被流程里的函数调用过，看不出是不是测试直接调用它时抛出的）',
      )
    })

    it('递归（cd 调用自己）最底下抛出、被 safe 接住：cd 不是入口（自己调用自己不算），handled 并说被「安全计数」接住', () => {
      const rec = failed(
        {
          [ca.sid]: c(fnRun(ca, [['start', 1, 0], ['step:n2', 1, 5], ['end:n3', 1, 0]], {}, { 'end:n3|return|': 1 }), { [safe.sid]: 1 }),
          [safe.sid]: c(fnRun(safe, [['start', 1, 0], ['step:n2', 1, 5], ['catch:n4', 1, 5], ['end:minus', 1, 0]], {}, { 'end:minus|return|': 1 }), { [cd.sid]: 1 }),
          [cd.sid]: c(fnRun(cd, [['start', 4, 0], ['cond:n2', 4, 5], ['end:again', 3, 0]], {}, { 'end:cd-raise|raise|LookupError': 1, 'end:uncaught|raise|LookupError': 3 }), { [cd.sid]: 3 }),
        },
        'AssertionError',
      )
      expect(at(rec, lineC).text).toBe('流程正常走完（结局：计数完成），测试是在自己的断言里失败的（「倒数」抛出过 LookupError，被调用它的「安全计数」接住了）')
    })

    it('入口是别的线的任务：在这条线上说"异常是从那条线抛出去的"，可以切过去；入口在几条线上时说它是任务的那条线、其余的写在后面', () => {
      const only = failed({ [ra.sid]: fnRun(ra, [['start', 1, 0]], {}, { 'end:ra-raise|raise|RuntimeError': 1 }) }, 'RuntimeError')
      const { f, text } = at(only, lineC)
      expect(f).toMatchObject({ kind: 'otherLine', line: lineR.sid, alsoIn: [lineP.sid] })
      expect(text).toBe('异常是从「子流程」那条线抛出去的（RuntimeError；它也在「父流程」那条线上）')
      // 父流程那条线：子流程在它上面，但它是「子流程」那条线的任务 → 也说是从那条线抛出去的（不说测试直接调用）
      expect(at(only, lineP).f).toMatchObject({ kind: 'otherLine', line: lineR.sid, functionKey: RA })
    })

    it('入口不是任何线的任务、不在这条线上：只说记下的事和它在哪些线上 / 不在任何功能线上（不说"从那条线抛出去"）', () => {
      const helperOnly = failed({ [hh.sid]: fnRun(hh, [['start', 1, 0]], {}, { 'end:uncaught|raise|OSError': 1 }) }, 'OSError')
      const { f, text } = at(helperOnly, lineC)
      expect(f).toMatchObject({ kind: 'elsewhere', functionKey: HH })
      expect(text).toBe('「hh」以 OSError 离开，没有记录到流程里的函数调用它（可能是测试直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）；它在「子流程」等线上')
      const out = at(failed({ [outside.sid]: fnRun(outside, [['start', 1, 0]], {}, { 'end:out-raise|raise|OSError': 1 }) }, 'OSError'), lineC)
      expect(out.text).toBe('「线外的函数」以 OSError 离开，没有记录到流程里的函数调用它（可能是测试直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）；它不在任何功能线上')
    })

    it('抛出的函数的调用者都以这种异常离开（pytest.raises 预期了，测试再在自己的断言里失败）：不说被接住了，说经它传了出去', () => {
      const expected = failed(
        {
          [pa.sid]: c(fnRun(pa, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:uncaught|raise|RuntimeError': 1 }), { [ra.sid]: 1 }),
          [ra.sid]: fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5]], { 'start|step:n2': 1 }, { 'end:ra-raise|raise|RuntimeError': 1 }),
        },
        'AssertionError',
      )
      expect(at(expected, lineR).text).toBe('测试是在自己的断言里失败的（「子流程」抛出过 RuntimeError，经「父流程」传了出去，但测试不是因它失败的（测试预期的，或被测试接住了））')
    })

    it('往上隔了两层才被接住（hh 抛出 → 子流程传出去 → 父流程接住）：说被父流程接住了，不说"测试预期的"', () => {
      const t = failed(
        {
          [pa.sid]: c(fnRun(pa, [['start', 1, 0], ['step:n2', 1, 5], ['catch:n4', 1, 5], ['end:n5', 1, 0]], {}, { 'end:n5|return|': 1 }), { [ra.sid]: 1 }),
          [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|OSError': 1 }), { [hh.sid]: 1 }),
          [hh.sid]: fnRun(hh, [['start', 1, 0]], {}, { 'end:uncaught|raise|OSError': 1 }),
        },
        'AssertionError',
      )
      expect(at(t, lineR).text).toBe(
        '测试是在自己的断言里失败的（「子流程」抛出过 OSError，被调用它的「父流程」接住了；「hh」抛出过 OSError，经「子流程」传到「父流程」，被「父流程」接住了）',
      )
      // 经库的包装函数（icalls）往上也一样
      const viaWrapper = failed({ ...t.functions, [pa.sid]: { ...t.functions[pa.sid], calls: {}, icalls: { [ra.sid]: 1 } } }, 'AssertionError')
      expect(at(viaWrapper, lineR).text).toContain('「hh」抛出过 OSError，经「子流程」传到「父流程」，被「父流程」接住了')
    })

    it('每一路都以这种异常离开、一直传到了入口（测试预期了它）：才说"测试不是因它失败的（测试预期的，或被测试接住了）"', () => {
      const t = failed(
        {
          [pa.sid]: c(fnRun(pa, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|OSError': 1 }), { [ra.sid]: 1 }),
          [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|OSError': 1 }), { [hh.sid]: 1 }),
          [hh.sid]: fnRun(hh, [['start', 1, 0]], {}, { 'end:uncaught|raise|OSError': 1 }),
        },
        'AssertionError',
      )
      expect(at(t, lineR).text).toBe(
        '测试是在自己的断言里失败的（「子流程」抛出过 OSError，经「父流程」传了出去，但测试不是因它失败的（测试预期的，或被测试接住了）；「hh」抛出过 OSError，经「子流程」传了出去，但测试不是因它失败的（测试预期的，或被测试接住了））',
      )
    })

    it('几个调用者：有的一路接住、有的一路传到了入口：只说记下的，不加"测试预期的"', () => {
      // hh 被子流程（以 OSError 离开、是入口）和安全计数（没以 OSError 离开）调用过
      const t = failed(
        {
          [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|OSError': 1 }), { [hh.sid]: 1 }),
          [safe.sid]: { ...fnRun(safe, [['start', 1, 0], ['step:n2', 1, 5], ['end:n3', 1, 0]], {}, { 'end:n3|return|': 1 }), icalls: { [hh.sid]: 1 } },
          [hh.sid]: { ...fnRun(hh, [['start', 2, 0]], {}, { 'end:uncaught|raise|OSError': 2 }), executions: 2 },
        },
        'AssertionError',
      )
      // （子流程自己是入口、没有记下的调用者，不列）
      expect(at(t, lineR).text).toBe('测试是在自己的断言里失败的（「hh」抛出过 OSError，有的被「安全计数」接住了，有的经「子流程」传了出去）')
    })

    it('预期的异常和真的失败出自两个入口（两条线）：在预期的那条线上也说抛出过 2 次，列出另一处', () => {
      // with pytest.raises(RuntimeError): ra()（子流程那条线）；然后 ca()（计数那条线）真的以 RuntimeError 失败
      const two = failed(
        {
          [ra.sid]: fnRun(ra, [['start', 1, 0]], {}, { 'end:ra-raise|raise|RuntimeError': 1 }),
          [ca.sid]: fnRun(ca, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|RuntimeError': 1 }),
        },
        'RuntimeError',
      )
      const onR = at(two, lineR)
      expect(onR.f).toMatchObject({ kind: 'stop', functionKey: RA, repeated: { n: 2, elsewhere: [{ functionKey: CA, nodeId: 'n4', title: '「计数」的「未捕获异常退出」' }] } })
      expect(onR.text).toBe(
        '停在「抛出」（RuntimeError）；另有：RuntimeError（计数，从「计数」那条线抛出）；这种异常在这个测试里抛出过 2 次，红圈画的是其中一处（另一处可能是测试预期的那一次）；别的抛出处：「计数」的「未捕获异常退出」',
      )
      expect(at(two, lineC).f).toMatchObject({ kind: 'stop', functionKey: CA, repeated: { n: 2, elsewhere: [{ functionKey: RA, nodeId: 'n3' }] } })
    })

    it('抛出过测试失败的那种异常、又被流程调用过的函数：不在这条线上也说看不出（在所有执行过的函数里找）', () => {
      const both = failed(
        {
          [ra.sid]: c(fnRun(ra, [['start', 1, 0], ['step:n2', 1, 5], ['end:n4', 1, 0]], {}, { 'end:n4|return|': 1 }), { [hh.sid]: 1 }),
          [hh.sid]: { ...fnRun(hh, [['start', 2, 0], ['end:n2', 1, 0]], {}, { 'end:n2|return|': 1, 'end:uncaught|raise|OSError': 1 }), executions: 2 },
        },
        'OSError',
      )
      expect(at(both, lineC).text).toBe(
        '测试以 OSError 失败，但流程里没有哪个入口抛出它（异常出在测试自己的代码里，或流程之外）（「hh」也抛出过 OSError：它被流程里的函数调用过，看不出是不是测试直接调用它时抛出的）',
      )
    })

    it('两条线各有一个入口抛出（一个被 pytest.raises 预期、测试以另一个失败）：只算测试失败的那种', () => {
      const two = failed(
        {
          [ra.sid]: fnRun(ra, [['start', 1, 0]], {}, { 'end:ra-raise|raise|RuntimeError': 1 }),
          [ca.sid]: fnRun(ca, [['start', 1, 0]], {}, { 'end:uncaught|raise|ValueError': 1 }),
        },
        'ValueError',
      )
      expect(at(two, lineC).text).toBe('停在「未捕获异常退出」（ValueError）')
      expect(at(two, lineR).text).toBe('异常是从「计数」那条线抛出去的（ValueError）')
    })

    it('GeneratorExit 不算抛出', () => {
      const gen = failed({ [hh.sid]: fnRun(hh, [['start', 1, 0]], {}, { 'end:uncaught|raise|GeneratorExit': 1 }) }, 'AssertionError')
      expect(at(gen, lineR).f).toEqual({ kind: 'handled', ends: [], caught: [], exc: 'AssertionError' })
    })
  })

  describe('同一个函数的几处调用：按在那里结束的执行选（N1、N10）', () => {
    const M = 'two.py::main@1'
    const H = 'two.py::h@20'
    const main = fn(
      M,
      [['n1', 'start', 'main'], ['n2', 'loop', '每一项', { subtitle: 'for v in items' }], ['n3', 'step', '第一处', { calls: [call(H)] }], ['n4', 'step', '第二处', { calls: [call(H)] }], ['n5', 'end', '完成', { end: 'ok' }], uncaught('n6')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'loop_each'], ['n3', 'n4', 'next'], ['n4', 'n2', 'loop_back'], ['n2', 'n5', 'loop_exit']],
      { kind: 'task', title: '主流程', call_targets: [H] },
    )
    const h = fn(
      H,
      [['n1', 'start', 'h'], ['n2', 'cond', '坏了？'], ['n3', 'end', '抛出', { end: 'failed', sid: 'end:boom' }], ['n4', 'end', '返回', { end: 'ok' }], uncaught('n5')],
      [['n1', 'n2', 'next'], ['n2', 'n3', 'yes'], ['n2', 'n4', 'no']],
      { title: '帮手' },
    )
    const ln = line(task(M), [main, h])
    const g = project([main, h], [ln])
    const hRaised = { ...fnRun(h, [['start', 3, 0], ['cond:n2', 3, 5], ['end:n4', 2, 0]], {}, { 'end:n4|return|': 2, 'end:boom|raise|ValueError': 1 }), executions: 3 }

    it('第一圈两处都成功、第二圈第一处抛出：钻入第一处', () => {
      const t = failed(
        {
          [main.sid]: c(fnRun(main, [['start', 1, 0], ['loop:n2', 2, 5], ['step:n3', 2, 5], ['step:n4', 1, 5]], { 'start|loop:n2': 1, 'loop:n2|step:n3': 2, 'step:n3|step:n4': 1, 'step:n4|loop:n2': 1 }, { 'end:uncaught|raise|ValueError': 1 }), { [h.sid]: 1 }),
          [h.sid]: hRaised,
        },
        'ValueError',
      )
      const { f } = on(g, [main, h], t, ln.sid)
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ task: M, path: [{ fn: M, node: 'n3' }] })
    })

    it('第一处成功、第二处抛出（一圈）：钻入第二处，画布上红圈在第二处的钻入卡片里', () => {
      const t = failed(
        {
          [main.sid]: c(fnRun(main, [['start', 1, 0], ['loop:n2', 1, 5], ['step:n3', 1, 5], ['step:n4', 1, 5]], { 'start|loop:n2': 1, 'loop:n2|step:n3': 1, 'step:n3|step:n4': 1 }, { 'end:uncaught|raise|ValueError': 1 }), { [h.sid]: 1 }),
          [h.sid]: { ...fnRun(h, [['start', 2, 0], ['cond:n2', 2, 5], ['end:n4', 1, 0]], {}, { 'end:n4|return|': 1, 'end:boom|raise|ValueError': 1 }), executions: 2 },
        },
        'ValueError',
      )
      const p: RunPathsJson = { version: 1, tests: [t], shapes: Object.fromEntries([main, h].map((x) => [x.sid, { shape: functionShape(x), dups: [] }])), function_dups: [] }
      const o = buildRunOverlay(g, p, 'f', ln.sid)
      const f = failureStop(o, g, ln.sid)!
      expect(f.kind === 'stop' && f.reveal).toMatchObject({ path: [{ fn: M, node: 'n4' }] })
      const r = revealStop(g, ln.sid, emptyState(), (f as Extract<typeof f, { kind: 'stop' }>).reveal)!
      const lv = buildLineView(g, ln.sid, r.state)
      const m = runMarks(lv, g, o, f as Extract<typeof f, { kind: 'stop' }>, new Map(), new Map())
      expect([...m.stops]).toEqual([r.target])
      expect(r.target).toBe(`${lv.cards[0].id}/step:n4>${h.sid}/end:boom`)
    })
  })

  describe('没有记下异常类型的运行（旧版本、或者没收到失败的类型）', () => {
    const raisesOnLine = {
      [fsid(run)]: fnRun(run, [['start', 1, 0], ['step:n2', 1, 5]], {}, { 'end:uncaught|raise|ValueError': 1 }),
      [fsid(check)]: fnRun(check, [['start', 1, 0]], {}, { 'end:raise1|raise|ValueError': 1 }),
    }
    it('旧版本的运行（没有 exc，也没有记下的调用）：不画红圈，列出这条线上抛出过异常的地方，说重新运行能看到停点', () => {
      const { f, text } = main1({ test: 'f', outcome: 'failed', seconds: 0, cases: 1, functions: raisesOnLine })
      expect(f).toMatchObject({ kind: 'unknown', old: true, raises: [{ functionKey: K.run, nodeId: 'n9', exc: 'ValueError' }, { functionKey: K.check, nodeId: 'n3', exc: 'ValueError' }] })
      expect(text).toBe('这次运行是旧版本记录的，看不出异常有没有到达测试；下面是抛出过异常的函数：「转账」（ValueError）、「检查」（ValueError）。重新运行测试后能看到停点')
      expect(unknownIntro(f as Extract<typeof f, { kind: 'unknown' }>)).toBe('这次运行是旧版本记录的，看不出异常有没有到达测试（重新运行测试后能看到停点）。抛出过异常的：')
    })

    it('有记下的调用、但这个测试没有异常类型：同样不画红圈，换个说法', () => {
      const { f, text } = main1({ test: 'f', outcome: 'failed', seconds: 0, cases: 1, functions: { ...raisesOnLine, [fsid(run)]: c(raisesOnLine[fsid(run)], { [fsid(check)]: 1 }) } })
      expect(f).toMatchObject({ kind: 'unknown', old: false })
      expect(text).toBe('这次运行没有记下这个测试失败时的异常类型，看不出异常有没有到达测试；下面是抛出过异常的函数：「转账」（ValueError）、「检查」（ValueError）')
    })
  })
})
