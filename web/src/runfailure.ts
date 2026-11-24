// 失败的测试停在哪（裁决 S11e / S14）和运行条、节点详情上的说明文字。纯函数，测试见 runfailure.test.ts。
// 用的是 runview 算好的叠加层（buildRunOverlay：停点、走过的节点、记下的调用、入口函数）。

import { drillTargets, type ChangeStop } from './diffview'
import { implicitKind, type FunctionGraph, type ProjectGraph } from './graph'
import { findLine, lineFunctions, lineTasks, locateOnLine, type RunOverlay, type RunStop } from './runview'

const own = <T>(rec: Record<string, T>, key: string): T | undefined => (Object.hasOwn(rec, key) ? rec[key] : undefined)
const fnOf = (graph: ProjectGraph, key: string): FunctionGraph | undefined => own(graph.functions, key)
const fullTitle = (x: { title: string; title_full?: string }): string => x.title_full || x.title

/**
 * 失败的测试停在哪（裁决 S11e / S14）——按记录插件记下的事实：测试失败时的异常类型（exc、excs）、真实的调用（calls：
 * 直接调用，含动态调用、回调；icalls：经项目以外的帧——库的包装函数、contextmanager——的间接调用）、入口函数
 * （entry_functions：没有别的执行过的函数直接或间接调用过它的）。
 * 1. 这个测试没有记下异常类型（早于 S12 的运行，或者别的插件抢先生成了报告）→ unknown：不画红圈、不定位，列出这条线上
 *    抛出过异常的地方（点了能定位），说重新运行测试后能看到停点。
 * 2. 以失败的那种类型（excs 里的）离开的入口函数把异常带到了测试里。一个都没有 → handled：AssertionError 时"测试是在
 *    自己的断言里失败的"，别的类型时"测试以 X 失败，但流程里没有哪个入口抛出它（……）"；这条线的根任务正常结束时前面加
 *    "流程正常走完（结局：…），"；再补最多 3 个抛出过异常的函数（先这条线上的，再别的执行过的），顺着记下的调用者
 *    （calls ∪ icalls）往上看，以 T 离开的接着往上，没以 T 离开的就是接住它的：
 *    - 每一路都被接住 →"（「F」抛出过 T，被调用它的「G」接住了）"，隔了几层时"经「G」传到「H」，被「H」接住了"；
 *    - 每一路都以 T 离开到了入口（入口也以 T 离开，测试却不是因 T 失败的）→"（「F」抛出过 T，经「G」传了出去，但测试不是
 *      因它失败的（测试预期的，或被测试接住了））"；
 *    - 有的一路接住、有的一路传出去（或者看不出）→ 只说记下的："有的被「H」接住了，有的经「G」传了出去"，不加括号里那句；
 *    - 它抛出的正是测试失败的那种、又被流程调用过（执行过的函数里都找）→"（「F」也抛出过 T：……看不出是不是测试直接
 *      调用它时抛出的）"。
 *    从不画红圈。（流程里的 assert 抛出的 AssertionError 从入口出去了时照样是停点。）
 * 3. 每个（入口 E，类型）顺着记下的调用往下（同一种类型；对应到走过的调用处时按"在那里结束的执行"选），再按画布上这条线分：
 *    - 链上有这条线派发树上的任务 → stop：从那个任务往下，能钻入的钻入、派发的跳到子任务卡片、钻入不到的画在调用处上；
 *      某一环图上没有调用处（动态调用、回调）时画在那个函数里执行停下的节点上，说"异常是经图上连不出来的调用……"；
 *    - E 在这条线上、不是任何线的任务 → direct：只说记下的事（"「E」以 T 离开，没有记录到流程里的函数调用它（可能是测试
 *      直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）"）。红圈画在 E 的停点上，只在画布上有一条只经过这个测试里执行过的函数
 *      的钻入路时；否则不画红圈，只能"定位到函数"（经任一调用处，不说测试走过那里）；
 *    - 链上有别的线的任务 → otherLine（"异常是从「L」那条线抛出去的"，可以切过去）；
 *    - 否则 → elsewhere（同 direct 的说法，加上它在哪些线上 / 不在任何功能线上）。
 * 4. 几个时：stop > direct > otherLine > elsewhere；stop / direct 按这条线上的先后从深到浅，otherLine 按线的先后，再按类型名。
 *    第一个是答案，其余的写进"另有"。另有里的 direct / elsewhere：有执行过的函数调用它（记下的直接、间接调用，或图上的
 *    调用）、自己也以这种异常离开时不列（异常经那个函数走，已经算在它的那一项里）。
 * 5. 这个测试的入口函数（所有线上的）一共以答案（stop、direct）的那种异常离开不止一次（pytest.raises 预期了一次、之后
 *    同一个或另一个流程又真的失败了一次；参数化的几个用例）：红圈照旧，补"这种异常在这个测试里抛出过 N 次，红圈画的是其中
 *    一处（另一处可能是测试预期的那一次）"，并列出别的抛出处（点了能定位，在别的线上时换过去）。
 * 功能线找不到、没有选测试时 null。
 */
export interface FailureOther {
  exc: string
  functionKey: string
  title: string
  /** 它是哪一种：停点（不写）、没有记录到调用它的流程函数（这条线上 / 别处）、从别的线抛出 */
  how?: 'direct' | 'otherLine' | 'elsewhere'
  /** otherLine：那条线；elsewhere：它在的线（没有时为空） */
  lineTitles?: string[]
}

/** 一处抛出（点了能定位，不画红圈） */
export interface RaisePlace {
  functionKey: string
  nodeId: string
  /** 「函数」的「节点」 */
  title: string
}

/** 入口以这种异常离开不止一次时：一共几次、红圈之外的抛出处 */
export interface Repeated {
  n: number
  elsewhere: RaisePlace[]
}

export type FailureStop =
  | {
      kind: 'stop'
      /** 红圈画在哪个节点上（图里的函数 key、节点 id） */
      functionKey: string
      nodeId: string
      exc: string
      /** 给 revealStop 的位置（任务卡片、钻入的路） */
      reveal: ChangeStop
      /** 红圈画在调用处 / 动态调用前停下的地方时：异常是在哪个函数里抛出的 */
      raisedIn: { functionKey: string; title: string; exc: string } | null
      /** 链上有一环图上连不出来（动态调用、回调） */
      dynamic: boolean
      repeated: Repeated | null
      others: FailureOther[]
    }
  | {
      kind: 'direct'
      /** 以这种异常离开、没有记录到流程里的函数调用它的那个函数和它的停点 */
      functionKey: string
      nodeId: string
      exc: string
      title: string
      /** 画布上只经过这个测试里执行过的函数的钻入路（画红圈）；没有时 null，不画红圈 */
      reveal: ChangeStop | null
      /** reveal 为 null 时只定位到函数用的位置（经任一调用处，不画红圈）；画布上钻入不到时 null */
      locate: ChangeStop | null
      repeated: Repeated | null
      others: FailureOther[]
    }
  | {
      kind: 'otherLine'
      /** 异常从哪条线（功能线 sid）抛出去的：链上有它的任务的那条线 */
      line: string
      lineTitle: string
      functionKey: string
      title: string
      exc: string
      /** 这个任务也在的其余几条线（sid） */
      alsoIn: string[]
      alsoInTitles: string[]
      others: FailureOther[]
    }
  | {
      kind: 'elsewhere'
      /** 抛出异常的入口函数（不是任何线的任务、不在这条线上） */
      functionKey: string
      title: string
      exc: string
      /** 它在的线（sid、标题）；不在任何功能线上时为空 */
      lines: FlowLineRef[]
      others: FailureOther[]
    }
  | {
      kind: 'handled'
      ends: string[]
      /**
       * 抛出过异常的函数（最多 3 个），顺着记下的调用者往上看：caught 被 by 接住了（by 没以这种异常离开；via 是中间经过的
       * 直接调用者，by 就是直接调用者时没有）；passed 每一路都以这种异常离开到了入口（经 by 传了出去，测试却不是因它失败的）；
       * mixed 有的一路被 catcher 接住、有的一路经 by 传了出去（或者看不出），只说记下的；maybe 它抛出的正是测试失败的那种
       * 异常、又被流程调用过——看不出是不是测试直接调用它时抛出的（记下的调用只说"调用过"，不分哪一次）
       */
      caught: {
        functionKey: string
        title: string
        exc: string
        by: string
        how: 'caught' | 'passed' | 'mixed' | 'maybe'
        via?: string
        catcher?: string
      }[]
      /** 测试失败时的异常类型 */
      exc: string
    }
  | {
      kind: 'unknown'
      /** 早于 S12 的运行（连记下的调用都没有）；否则是这个测试没有记下异常类型 */
      old: boolean
      /** 这条线上抛出过异常的地方（点了能定位，不画红圈） */
      raises: { functionKey: string; nodeId: string; title: string; exc: string }[]
    }

interface FlowLineRef {
  sid: string
  title: string
}

/** 顺着异常往下的一环：这个函数、它的停点、往下一环的函数和图上的调用处（没有调用处时是动态调用） */
interface EscapeLink {
  key: string
  stop: RunStop
  next: string | null
  site: string | null
}

export function failureStop(overlay: RunOverlay, graph: ProjectGraph, lineId: string): FailureStop | null {
  const line = findLine(graph, lineId)
  if (!line || overlay.test === null) return null
  const lineFns = lineFunctions(graph, line)
  const order = new Map(lineFns.map((k, i) => [k, i]))
  const tasks = lineTasks(graph, line)
  const anyTask = new Set(graph.lines.flatMap((l) => lineTasks(graph, l)))
  const raises = (key: string) => overlay.stops.filter((s) => s.functionKey === key && s.kind === 'raise' && s.exc !== 'GeneratorExit')
  const excsOf = (key: string) => [...new Set(raises(key).map((s) => s.exc))].sort()
  const leftWith = (key: string, exc: string) => raises(key).some((s) => s.exc === exc)
  const pos = (f: FunctionGraph, id: string) => f.nodes.findIndex((n) => n.id === id)
  const titleOf = (key: string) => fullTitle(fnOf(graph, key)!)
  /** 记下的被调函数：直接（calls）和经项目以外的帧（icalls）的 */
  const callees = (key: string) => new Set([...(overlay.calls.get(key)?.keys() ?? []), ...(overlay.icalls.get(key)?.keys() ?? [])])
  /** 记下调用过它的、执行过的函数（不算它自己） */
  const callersOf = (key: string) => [...overlay.functionsRun].filter((g) => g !== key && callees(g).has(key))
  /** 这个函数以这种异常离开的停点：raise 语句的结局优先（异常在这里抛出），再取图里靠后的 */
  const stopFor = (key: string, exc: string): { stop: RunStop; origin: boolean } | null => {
    const f = fnOf(graph, key)!
    const rs = raises(key).filter((s) => s.exc === exc)
    if (!rs.length) return null
    const uncaught = (s: RunStop) => implicitKind(f.nodes.find((n) => n.id === s.nodeId)!) === 'uncaught'
    const byPos = (a: RunStop, b: RunStop) => pos(f, b.nodeId) - pos(f, a.nodeId)
    const explicit = rs.filter((s) => !uncaught(s)).sort(byPos)
    return explicit.length ? { stop: explicit[0], origin: true } : { stop: [...rs].sort(byPos)[0], origin: false }
  }
  const byExc = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0)
  /** 这条线上深的在前，同样深的按类型名 */
  const deeper = (a: { key: string; exc: string }, b: { key: string; exc: string }) =>
    (order.get(b.key) ?? -1) - (order.get(a.key) ?? -1) || byExc(a.exc, b.exc)
  const otherLines = graph.lines.map((l, index) => ({ l, index })).filter(({ l }) => l.sid !== line.sid)
  const linesWith = (key: string) => otherLines.filter(({ l }) => lineFunctions(graph, l).includes(key)).map(({ l }) => ({ sid: l.sid, title: fullTitle(l) }))

  // 1. 没有记下异常类型
  const types = overlay.testExcs.length ? overlay.testExcs : overlay.testExc ? [overlay.testExc] : []
  if (!types.length) {
    const found: { functionKey: string; nodeId: string; title: string; exc: string }[] = []
    for (const key of lineFns) for (const exc of excsOf(key)) found.push({ functionKey: key, nodeId: stopFor(key, exc)!.stop.nodeId, title: titleOf(key), exc })
    return { kind: 'unknown', old: !overlay.hasCalls, raises: found }
  }
  // 2. 以失败的那种类型离开的入口函数
  const raising: { key: string; exc: string }[] = []
  for (const key of overlay.entries) for (const exc of excsOf(key)) if (types.includes(exc)) raising.push({ key, exc })
  if (!raising.length) return handled()

  // 3. 按这条线分
  const stops: { entry: string; exc: string; chain: EscapeLink[] }[] = []
  const direct: { key: string; exc: string }[] = []
  const elsewhereLine: { key: string; exc: string; line: FlowLineRef; index: number; alsoIn: FlowLineRef[] }[] = []
  const elsewhere: { key: string; exc: string; lines: FlowLineRef[] }[] = []
  for (const r of raising) {
    const chain = escapeChain(r.key, r.exc)
    const at = chain.findIndex((l) => tasks.includes(l.key))
    if (at >= 0) {
      stops.push({ entry: r.key, exc: r.exc, chain: chain.slice(at) })
      continue
    }
    if (order.has(r.key) && !anyTask.has(r.key)) {
      direct.push(r)
      continue
    }
    const home = chain.map((l) => ({ l, h: otherLines.find(({ l: ol }) => lineTasks(graph, ol).includes(l.key)) })).find((x) => x.h)
    if (home?.h) {
      const also = otherLines.filter(({ l }) => l.sid !== home.h!.l.sid && lineFunctions(graph, l).includes(home.l.key)).map(({ l }) => ({ sid: l.sid, title: fullTitle(l) }))
      elsewhereLine.push({ key: home.l.key, exc: r.exc, line: { sid: home.h.l.sid, title: fullTitle(home.h.l) }, index: home.h.index, alsoIn: also })
    } else {
      elsewhere.push({ key: r.key, exc: r.exc, lines: linesWith(r.key) })
    }
  }
  const finals = new Map<string, { entry: string; exc: string; chain: EscapeLink[] }>()
  for (const s of stops) {
    const last = s.chain[s.chain.length - 1]
    const k = `${last.key}\u0000${last.stop.exc}`
    if (!finals.has(k)) finals.set(k, s)
  }
  const lastOf = (s: { chain: EscapeLink[] }) => s.chain[s.chain.length - 1]
  const stopList = [...finals.values()].sort((a, b) => deeper({ key: lastOf(a).key, exc: a.exc }, { key: lastOf(b).key, exc: b.exc }))
  direct.sort(deeper)
  elsewhereLine.sort((a, b) => a.index - b.index || byExc(a.exc, b.exc))
  elsewhere.sort((a, b) => byExc(a.exc, b.exc) || byExc(titleOf(a.key), titleOf(b.key)))

  // 4. 第一个是答案，其余的写在另有里
  /** 有执行过的函数调用它（记下的直接、间接调用，或图上的调用）、自己也以这种异常离开：异常经那个函数走 */
  const passedThrough = (key: string, exc: string) =>
    [...overlay.functionsRun].some((g) => {
      if (g === key || !leftWith(g, exc)) return false
      if (callees(g).has(key)) return true
      const f = fnOf(graph, g)
      return !!f && (f.call_targets.includes(key) || f.nodes.some((n) => n.calls.some((c) => c.target === key)))
    })
  const others = (skip: 'stop' | 'direct' | 'otherLine' | 'elsewhere'): FailureOther[] => {
    const out: FailureOther[] = []
    const from = (k: string) => (k === skip ? 1 : 0)
    for (const s of stopList.slice(from('stop'))) out.push({ exc: s.exc, functionKey: lastOf(s).key, title: titleOf(lastOf(s).key) })
    for (const d of direct.slice(from('direct'))) if (!passedThrough(d.key, d.exc)) out.push({ exc: d.exc, functionKey: d.key, title: titleOf(d.key), how: 'direct' })
    for (const d of elsewhereLine.slice(from('otherLine'))) out.push({ exc: d.exc, functionKey: d.key, title: titleOf(d.key), how: 'otherLine', lineTitles: [d.line.title] })
    for (const d of elsewhere.slice(from('elsewhere'))) {
      if (!passedThrough(d.key, d.exc)) out.push({ exc: d.exc, functionKey: d.key, title: titleOf(d.key), how: 'elsewhere', lineTitles: d.lines.map((l) => l.title) })
    }
    return out
  }
  if (stopList.length) {
    const s = stopList[0]
    return ringFor(s.chain, s.entry, others('stop'))
  }
  if (direct.length) {
    const pick = direct[0]
    const { stop } = stopFor(pick.key, pick.exc)!
    const reveal = walkedPath(pick.key, stop.nodeId)
    return {
      kind: 'direct',
      functionKey: pick.key,
      nodeId: stop.nodeId,
      exc: pick.exc,
      title: titleOf(pick.key),
      reveal,
      locate: reveal ? null : locateOnLine(graph, line.sid, pick.key, stop.nodeId),
      repeated: repeatedOf(pick.key, pick.exc, pick.key, stop.nodeId),
      others: others('direct'),
    }
  }
  if (elsewhereLine.length) {
    const e = elsewhereLine[0]
    return {
      kind: 'otherLine',
      line: e.line.sid,
      lineTitle: e.line.title,
      functionKey: e.key,
      title: titleOf(e.key),
      exc: e.exc,
      alsoIn: e.alsoIn.map((l) => l.sid),
      alsoInTitles: e.alsoIn.map((l) => l.title),
      others: others('otherLine'),
    }
  }
  const e = elsewhere[0]
  return { kind: 'elsewhere', functionKey: e.key, title: titleOf(e.key), exc: e.exc, lines: e.lines, others: others('elsewhere') }

  /** 没有异常到测试里 */
  function handled(): FailureStop {
    const root = fnOf(graph, line!.root.function)
    const ends = root
      ? overlay.stops
          .filter((s) => s.functionKey === root.key && s.kind === 'return')
          .sort((a, b) => pos(root, a.nodeId) - pos(root, b.nodeId))
          .map((s) => fullTitle(root.nodes.find((n) => n.id === s.nodeId)!))
      : []
    // 抛出过异常、有记下的调用者的函数：先这条线上的；正是测试失败的那种异常的（看不出是不是测试直接调用的）在所有执行过
    // 的函数里找
    const caught: Extract<FailureStop, { kind: 'handled' }>['caught'] = []
    const consider = (key: string, failingOnly: boolean) => {
      const excs = excsOf(key)
      const callers = callersOf(key)
      if (!excs.length || !callers.length) return
      const failing = excs.find((e) => types.includes(e))
      if (failing !== undefined) {
        caught.push({ functionKey: key, title: titleOf(key), exc: failing, by: titleOf(callers[0]), how: 'maybe' })
        return
      }
      if (failingOnly) return
      const exc = excs[0]
      const item = { functionKey: key, title: titleOf(key), exc }
      const { catchers, escaped } = upward(key, exc)
      // 直接调用者里以这种异常离开的（往外传的那一路从它开始）
      const passer = callers.find((c) => leftWith(c, exc)) ?? callers[0]
      if (catchers.length && !escaped) {
        const c = catchers[0]
        caught.push({ ...item, how: 'caught', by: titleOf(c.by), ...(c.via !== c.by ? { via: titleOf(c.via) } : {}) })
      } else if (escaped && !catchers.length) caught.push({ ...item, how: 'passed', by: titleOf(passer) })
      else if (catchers.length) caught.push({ ...item, how: 'mixed', by: titleOf(passer), catcher: titleOf(catchers[0].by) })
      else caught.push({ ...item, how: 'mixed', by: titleOf(passer) })
    }
    /**
     * 从 key 顺着记下的调用者（calls ∪ icalls）往上：没以 exc 离开的调用者是接住它的（记下的事实）；以它离开的接着往上；
     * 一直到入口（没有记下的调用者）、入口也以它离开 = 传出去了（到了测试，测试却不是因它失败的）。via：那一路的直接调用者。
     */
    function upward(key: string, exc: string): { catchers: { by: string; via: string }[]; escaped: boolean } {
      const catchers: { by: string; via: string }[] = []
      let escaped = false
      const seen = new Set([key])
      const queue = callersOf(key).map((c) => ({ fn: c, via: c }))
      for (const q of queue) seen.add(q.fn)
      while (queue.length) {
        const { fn, via } = queue.shift()!
        if (!leftWith(fn, exc)) {
          catchers.push({ by: fn, via })
          continue
        }
        const up = callersOf(fn)
        if (!up.length) escaped = true
        for (const c of up) {
          if (seen.has(c)) continue
          seen.add(c)
          queue.push({ fn: c, via })
        }
      }
      return { catchers, escaped }
    }
    for (const key of lineFns) if (caught.length < 3) consider(key, false)
    for (const key of overlay.functionsRun) if (caught.length < 3 && !order.has(key)) consider(key, true)
    return { kind: 'handled', ends: [...new Set(ends)], caught, exc: overlay.testExc || types[0] }
  }

  /** 从 start（以 exc 离开）顺着记下的调用往下 */
  function escapeChain(start: string, exc: string): EscapeLink[] {
    const chain: EscapeLink[] = []
    const seen = new Set<string>()
    let key: string | null = start
    while (key !== null && !seen.has(key)) {
      const here: string = key
      seen.add(here)
      const found = stopFor(here, exc)
      if (!found) break
      const link: EscapeLink = { key: here, stop: found.stop, next: null, site: null }
      chain.push(link)
      if (found.origin) break // 异常就是在这个函数里抛出的
      // 往下：它记下调用过的、也以这种异常离开的函数；图上有走过的调用处时取有执行在那里结束的（ended > 0）、再取靠后的
      const f = fnOf(graph, here)!
      const walked = overlay.nodes.get(here)
      const ended = overlay.ended.get(here)
      const candidates: { next: string; site: string | null; rank: number }[] = []
      for (const callee of callees(here)) {
        if (callee === here || seen.has(callee) || !leftWith(callee, exc)) continue
        let site: string | null = null
        let rank = -1
        f.nodes.forEach((n, i) => {
          if (!walked?.has(n.id) || !n.calls.some((c) => c.target === callee)) return
          const r = ((ended?.get(n.id) ?? 0) > 0 ? 1_000_000 : 0) + i
          if (r > rank) {
            rank = r
            site = n.id
          }
        })
        candidates.push({ next: callee, site, rank })
      }
      candidates.sort((a, b) => b.rank - a.rank || byExc(a.next, b.next))
      const pick = candidates[0]
      if (!pick) break
      link.next = pick.next
      link.site = pick.site
      key = pick.next
    }
    return chain
  }

  /** 按这条链本身定位：能钻入的钻入，派发的跳到子任务的卡片，钻入不到的画在调用处上，连不出来的画在停下的节点上 */
  function ringFor(chain: EscapeLink[], entry: string, rest: FailureOther[]): FailureStop {
    const last = chain[chain.length - 1]
    const exc = last.stop.exc
    const repeated = repeatedOf(entry, exc, last.key, last.stop.nodeId)
    let task = chain[0].key
    let path: ChangeStop['path'] = []
    const at = (functionKey: string, nodeId: string): ChangeStop => ({ functionKey, nodeId, kind: 'changed', text: '', where: '', task, path })
    // 每一环要么返回，要么往下一环走（链的最后一环一定返回）
    for (let i = 0; ; i++) {
      const link = chain[i]
      if (i === chain.length - 1 || link.next === null) {
        return { kind: 'stop', functionKey: link.key, nodeId: link.stop.nodeId, exc, reveal: at(link.key, link.stop.nodeId), raisedIn: null, dynamic: false, repeated, others: rest }
      }
      const next = link.next
      if (link.site === null) {
        // 图上连不出来的调用：往下还有这条线的任务时跳到那个任务的卡片接着走
        const jump = chain.findIndex((l, j) => j > i && tasks.includes(l.key))
        if (jump > i) {
          task = chain[jump].key
          path = []
          i = jump - 1
          continue
        }
        // 否则画在这个函数里执行停下的节点上（在那里结束的执行 > 0 的、靠后的），没有时画在它的停点上
        const f = fnOf(graph, link.key)!
        const ended = overlay.ended.get(link.key)
        const stoppedAt = [...f.nodes].reverse().find((n) => (ended?.get(n.id) ?? 0) > 0 && n.type !== 'end')
        const nodeId = stoppedAt?.id ?? link.stop.nodeId
        return { kind: 'stop', functionKey: link.key, nodeId, exc, reveal: at(link.key, nodeId), raisedIn: { functionKey: last.key, title: titleOf(last.key), exc }, dynamic: true, repeated, others: rest }
      }
      const site = fnOf(graph, link.key)!.nodes.find((n) => n.id === link.site)!
      if (drillTargets(graph, site).includes(next)) path = [...path, { fn: link.key, node: site.id }]
      else if (tasks.includes(next) && site.calls.some((c) => c.target === next && c.is_dispatch)) {
        task = next
        path = []
      } else {
        return { kind: 'stop', functionKey: link.key, nodeId: site.id, exc, reveal: at(link.key, site.id), raisedIn: { functionKey: next, title: titleOf(next), exc }, dynamic: false, repeated, others: rest }
      }
    }
  }

  /**
   * 这个测试的入口函数（所有线上的，不只是 entry）一共以 exc 离开不止一次时：次数和红圈（ringKey、ringNode，链最后一环的
   * 停点）之外的抛出处——从这些入口（entry 在前）顺着记下的调用往下、以这种异常离开的函数里：raise 语句的结局都算；只有
   * "未捕获异常退出"、往下又没有以这种异常离开的被调函数的，那个停点也算（异常是它里面的语句抛出的）。一次时 null。
   * 经别的、也以这种异常离开的执行过的函数走的入口（passedThrough：记不到的调用，比如 gather）不另算次数——那是同一个异常。
   * （`with pytest.raises(X): flow_a()` 之后 `flow_b()` 真的以 X 失败：两个入口各一次，红圈不一定在真的那一处。）
   */
  function repeatedOf(entry: string, exc: string, ringKey: string, ringNode: string): Repeated | null {
    const counted = [...overlay.entries].filter((e) => leftWith(e, exc) && !passedThrough(e, exc)).sort()
    const n = counted.reduce((a, e) => a + raises(e).filter((s) => s.exc === exc).reduce((b, s) => b + s.n, 0), 0)
    if (n <= 1) return null
    const from = [entry, ...counted.filter((e) => e !== entry)]
    const places: RaisePlace[] = []
    const seen = new Set<string>(from)
    const queue = [...from]
    while (queue.length) {
      const key = queue.shift()!
      const f = fnOf(graph, key)!
      const down = [...callees(key)].filter((c) => !seen.has(c) && leftWith(c, exc))
      for (const s of raises(key).filter((x) => x.exc === exc)) {
        const node = f.nodes.find((x) => x.id === s.nodeId)!
        if (implicitKind(node) === 'uncaught' && down.length) continue
        if (!(key === ringKey && s.nodeId === ringNode) && !places.some((p) => p.functionKey === key && p.nodeId === s.nodeId)) {
          places.push({ functionKey: key, nodeId: s.nodeId, title: `「${titleOf(key)}」的「${fullTitle(node)}」` })
        }
      }
      for (const c of down) {
        seen.add(c)
        queue.push(c)
      }
    }
    return { n, elsewhere: places }
  }

  /**
   * 画布上到这个函数的钻入路，只经过这个测试里执行过的函数（从执行过的任务卡片出发、只钻入执行过的函数里的调用处）：
   * 不钻到没在这个测试里跑过的流程下面去（那会让人以为测试走过那里）。没有时 null。
   */
  function walkedPath(fnKey: string, nodeId: string): ChangeStop | null {
    const ran = overlay.functionsRun
    for (const t of tasks) {
      if (!ran.has(t)) continue
      if (t === fnKey) return { functionKey: fnKey, nodeId, kind: 'changed', text: '', where: '', task: t, path: [] }
      const seen = new Set([t])
      const queue: { key: string; path: ChangeStop['path'] }[] = [{ key: t, path: [] }]
      while (queue.length) {
        const { key, path } = queue.shift()!
        for (const n of fnOf(graph, key)?.nodes ?? []) {
          for (const callee of drillTargets(graph, n)) {
            if (seen.has(callee)) continue
            seen.add(callee)
            const next = [...path, { fn: key, node: n.id }]
            if (callee === fnKey) return { functionKey: fnKey, nodeId, kind: 'changed', text: '', where: '', task: t, path: next }
            if (ran.has(callee)) queue.push({ key: callee, path: next })
          }
        }
      }
    }
    return null
  }
}

/** 只说记下的事：它以这种异常离开，没有记录到流程里的函数调用它 */
const unrecordedText = (title: string, exc: string): string =>
  `「${title}」以 ${exc} 离开，没有记录到流程里的函数调用它（可能是测试直接调用的，也可能是经并发任务、线程、生成器或其他没有记录到的方式调用的）`
const whereText = (lines: string[]): string => (lines.length ? `它在「${lines[0]}」${lines.length > 1 ? '等线' : '那条线'}上` : '它不在任何功能线上')

const otherText = (o: FailureOther): string => {
  const lines = o.lineTitles ?? []
  const how =
    o.how === 'direct'
      ? '，没有记录到调用它的流程函数'
      : o.how === 'otherLine'
        ? `，从「${lines[0]}」那条线抛出`
        : o.how === 'elsewhere'
          ? `，没有记录到调用它的流程函数，${lines.length ? `在「${lines[0]}」${lines.length > 1 ? '等线' : '那条线'}上` : '不在任何功能线上'}`
          : ''
  return `${o.exc}（${o.title}${how}）`
}
const othersText = (others: FailureOther[]): string => (others.length ? `；另有：${others.map(otherText).join('、')}` : '')
const caughtText = (caught: Extract<FailureStop, { kind: 'handled' }>['caught']): string =>
  caught.length
    ? `（${caught
        .map((c) =>
          c.how === 'maybe'
            ? `「${c.title}」也抛出过 ${c.exc}：它被流程里的函数调用过，看不出是不是测试直接调用它时抛出的`
            : c.how === 'passed'
              ? `「${c.title}」抛出过 ${c.exc}，经「${c.by}」传了出去，但测试不是因它失败的（测试预期的，或被测试接住了）`
              : c.how === 'mixed'
                ? c.catcher
                  ? `「${c.title}」抛出过 ${c.exc}，有的被「${c.catcher}」接住了，有的经「${c.by}」传了出去`
                  : `「${c.title}」抛出过 ${c.exc}，经「${c.by}」传了出去`
                : c.via
                  ? `「${c.title}」抛出过 ${c.exc}，经「${c.via}」传到「${c.by}」，被「${c.by}」接住了`
                  : `「${c.title}」抛出过 ${c.exc}，被调用它的「${c.by}」接住了`,
        )
        .join('；')}）`
    : ''
const raisesText = (raises: { title: string; exc: string }[]): string =>
  raises.length ? `下面是抛出过异常的函数：${raises.map((r) => `「${r.title}」（${r.exc}）`).join('、')}` : '这条线上没有函数抛出过异常'

/**
 * 入口以这种异常离开不止一次时补的一句和别的抛出处；links 为真时只写到"别的抛出处："为止（运行条在后面列成可以点的）。
 */
export function repeatedText(r: Repeated | null, links = false): string {
  if (!r) return ''
  const where = r.elsewhere.length ? `；别的抛出处：${links ? '' : r.elsewhere.map((p) => p.title).join('、')}` : ''
  return `；这种异常在这个测试里抛出过 ${r.n} 次，红圈画的是其中一处（另一处可能是测试预期的那一次）${where}`
}

/** 运行条上 unknown 的开头（后面跟着可以点的抛出处） */
export function unknownIntro(f: Extract<FailureStop, { kind: 'unknown' }>): string {
  const head = f.old
    ? '这次运行是旧版本记录的，看不出异常有没有到达测试（重新运行测试后能看到停点）'
    : '这次运行没有记下这个测试失败时的异常类型，看不出异常有没有到达测试'
  return f.raises.length ? `${head}。抛出过异常的：` : `${head}。这条线上没有函数抛出过异常`
}

/** 失败停点的说明（嵌在运行面板里的节点详情：红圈就在这个节点上时；handled 等也用它） */
export function failureText(f: FailureStop): string {
  if (f.kind === 'handled') {
    const lead = f.ends.length ? `流程正常走完（结局：${f.ends.join('、')}），` : ''
    const base =
      f.exc === 'AssertionError'
        ? '测试是在自己的断言里失败的'
        : `测试以 ${f.exc} 失败，但流程里没有哪个入口抛出它（异常出在测试自己的代码里，或流程之外）`
    return lead + base + caughtText(f.caught)
  }
  if (f.kind === 'unknown') {
    return f.old
      ? `这次运行是旧版本记录的，看不出异常有没有到达测试；${raisesText(f.raises)}。重新运行测试后能看到停点`
      : `这次运行没有记下这个测试失败时的异常类型，看不出异常有没有到达测试；${raisesText(f.raises)}`
  }
  if (f.kind === 'direct') return `这个测试没有经过这里的调用，只是定位到函数：${unrecordedText(f.title, f.exc)}${repeatedText(f.repeated)}`
  if (f.kind === 'otherLine') return `异常是从「${f.lineTitle}」那条线抛出去的（${f.exc}）`
  if (f.kind === 'elsewhere') return `${unrecordedText(f.title, f.exc)}；${whereText(f.lines.map((l) => l.title))}`
  const rep = repeatedText(f.repeated)
  if (f.dynamic && f.raisedIn) return `异常是经图上连不出来的调用（动态调用 / 回调）到「${f.raisedIn.title}」里抛出的（${f.raisedIn.exc}）${rep}`
  if (f.raisedIn) return `异常是在它调用的「${f.raisedIn.title}」里抛出的（${f.raisedIn.exc}）${rep}`
  return `失败的测试在这里离开（${f.exc}）${rep}`
}

/**
 * 运行条上的一句：停在哪 / 是在它调用的哪个函数里抛出的 / 经连不出来的调用 / 以这种异常离开却没有记录到调用它的流程函数 /
 * 从哪条线抛出去的 / 测试自己的断言 / 看不出来；抛出过不止一次时补一句；另有的。lineTitle 给出功能线显示用的标题（手工改过
 * 的），不给时用图里的。links 为真时别的抛出处不写进句子（运行条把它们列成可以点的，见 repeatedText）。
 */
export function failureBarText(f: FailureStop, graph: ProjectGraph, lineTitle?: (sid: string) => string | undefined, links = false): string {
  const t = (sid: string, fallback: string) => lineTitle?.(sid) || fallback
  if (f.kind === 'handled' || f.kind === 'unknown') return failureText(f)
  if (f.kind === 'otherLine') {
    const also = f.alsoIn.length ? `；它也在${f.alsoIn.map((sid, i) => `「${t(sid, f.alsoInTitles[i])}」`).join('')}那条线上` : ''
    return `异常是从「${t(f.line, f.lineTitle)}」那条线抛出去的（${f.exc}${also}）${othersText(f.others)}`
  }
  if (f.kind === 'elsewhere') return `${unrecordedText(f.title, f.exc)}；${whereText(f.lines.map((l) => t(l.sid, l.title)))}${othersText(f.others)}`
  if (f.kind === 'direct') return `${unrecordedText(f.title, f.exc)}${othersText(f.others)}${repeatedText(f.repeated, links)}`
  const n = fnOf(graph, f.functionKey)?.nodes.find((x) => x.id === f.nodeId)
  const where = n ? `停在「${fullTitle(n)}」` : '停在这里'
  const what = f.raisedIn
    ? f.dynamic
      ? `：异常是经图上连不出来的调用（动态调用 / 回调）到「${f.raisedIn.title}」里抛出的（${f.raisedIn.exc}）`
      : `：异常是在它调用的「${f.raisedIn.title}」里抛出的（${f.raisedIn.exc}）`
    : `（${f.exc}）`
  return where + what + othersText(f.others) + repeatedText(f.repeated, links)
}
