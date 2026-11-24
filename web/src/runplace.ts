// 运行路径叠到图上（core 的 runpath 模块说明"叠到图上"：locate_function、match_function_run、locate_test_run 的照搬）
// 和测试的入口函数（core 的 entry_functions）。纯函数，测试见 runplace.test.ts；和 core 共用的用例在
// core/tests/fixtures/placement/*.json、core/tests/fixtures/entries.json（两边的测试读同一批文件）。
//
// - 函数：运行时、现在都不重名时 sid 相等才放上；重名（sid 带 #n）时这一组没变按相等放，变了按形状（节点 sid 序列的
//   sha1）找唯一的那个，运行时形状是"空"的（只有隐含节点）或者运行时这一组另有同形状的定义时放不上；
// - 节点：函数形状和运行时一样时 sid 相等就配上；不一样时只配基础 sid 不重名（运行时、现在都只有一个）且相等的；
// - 配不上的都记进 unmatched，绝不画到别的函数 / 节点上。

import type { FunctionRunJson, FunctionShapeJson, RunPathsJson, TestRunJson } from './api'
import type { FunctionGraph, ProjectGraph } from './graph'

// ---------------------------------------------------------------------------------------------------------------
// SHA-1（函数形状用；浏览器的 crypto.subtle 是异步的，这里要同步算）

/** 字符串按 UTF-8 编码后的 SHA-1，十六进制小写（同 Python 的 hashlib.sha1(s.encode()).hexdigest()） */
export function sha1Hex(text: string): string {
  const bytes = new TextEncoder().encode(text)
  const len = bytes.length
  const blocks = ((len + 8) >> 6) + 1
  const w = new Uint32Array(blocks * 16)
  for (let i = 0; i < len; i++) w[i >> 2] |= bytes[i] << (24 - (i & 3) * 8)
  w[len >> 2] |= 0x80 << (24 - (len & 3) * 8)
  const bits = len * 8
  w[blocks * 16 - 1] = bits >>> 0
  w[blocks * 16 - 2] = Math.floor(bits / 0x100000000)
  let h0 = 0x67452301
  let h1 = 0xefcdab89
  let h2 = 0x98badcfe
  let h3 = 0x10325476
  let h4 = 0xc3d2e1f0
  const m = new Uint32Array(80)
  for (let b = 0; b < blocks; b++) {
    for (let t = 0; t < 16; t++) m[t] = w[b * 16 + t]
    for (let t = 16; t < 80; t++) {
      const x = m[t - 3] ^ m[t - 8] ^ m[t - 14] ^ m[t - 16]
      m[t] = (x << 1) | (x >>> 31)
    }
    let a = h0
    let bb = h1
    let c = h2
    let d = h3
    let e = h4
    for (let t = 0; t < 80; t++) {
      let f: number
      let k: number
      if (t < 20) {
        f = (bb & c) | (~bb & d)
        k = 0x5a827999
      } else if (t < 40) {
        f = bb ^ c ^ d
        k = 0x6ed9eba1
      } else if (t < 60) {
        f = (bb & c) | (bb & d) | (c & d)
        k = 0x8f1bbcdc
      } else {
        f = bb ^ c ^ d
        k = 0xca62c1d6
      }
      const tmp = (((a << 5) | (a >>> 27)) + f + e + k + m[t]) >>> 0
      e = d
      d = c
      c = ((bb << 30) | (bb >>> 2)) >>> 0
      bb = a
      a = tmp
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + bb) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
  }
  return [h0, h1, h2, h3, h4].map((h) => h.toString(16).padStart(8, '0')).join('')
}

// ---------------------------------------------------------------------------------------------------------------
// 标识、形状

const NTH = /^([\s\S]*)#(\d+)$/

/** 去掉重名编号 #n 的基础 sid */
export function baseSid(sid: string): string {
  const m = NTH.exec(sid)
  return m ? m[1] : sid
}

/** 重名编号：sid 结尾的 #n 的 n，没有时为 1 */
export function nthSid(sid: string): number {
  const m = NTH.exec(sid)
  return m ? Number(m[2]) : 1
}

const own = <T>(rec: Record<string, T>, key: string): T | undefined => (Object.hasOwn(rec, key) ? rec[key] : undefined)
const sidOf = (f: FunctionGraph): string => f.sid || f.key

const shapeCache = new WeakMap<FunctionGraph, string>()

/** 函数的形状：节点 sid（没有 sid 的旧图用节点 id）按图里的顺序用换行连起来的 sha1（同 core 的 function_shape） */
export function functionShape(f: FunctionGraph): string {
  let s = shapeCache.get(f)
  if (s === undefined) {
    s = sha1Hex(f.nodes.map((n) => n.sid || n.id).join('\n'))
    shapeCache.set(f, s)
  }
  return s
}

/** 没有语句节点的函数的形状（只有开始、执行到函数末尾、未捕获异常退出）：都一样，认不出是重名组里的哪一个 */
export const TRIVIAL_SHAPES: ReadonlySet<string> = new Set(
  [['start'], ['start', 'end:implicit'], ['start', 'end:uncaught'], ['start', 'end:implicit', 'end:uncaught']].map((seq) => sha1Hex(seq.join('\n'))),
)

function baseCounts(f: FunctionGraph): Map<string, number> {
  const out = new Map<string, number>()
  for (const n of f.nodes) {
    const b = baseSid(n.sid || n.id)
    out.set(b, (out.get(b) ?? 0) + 1)
  }
  return out
}

// ---------------------------------------------------------------------------------------------------------------
// 叠到图上（core 的 locate_function、match_function_run、locate_test_run）

class Placer {
  private readonly dups: Set<string>
  private readonly groups = new Map<string, FunctionGraph[]>()
  private readonly located = new Map<string, FunctionGraph | null>()
  private readonly paths: RunPathsJson

  constructor(graph: ProjectGraph, paths: RunPathsJson) {
    this.paths = paths
    this.dups = new Set(paths.function_dups)
    for (const f of Object.values(graph.functions)) {
      const b = baseSid(sidOf(f))
      const g = this.groups.get(b)
      if (g) g.push(f)
      else this.groups.set(b, [f])
    }
    for (const g of this.groups.values()) g.sort((x, y) => nthSid(sidOf(x)) - nthSid(sidOf(y)))
  }

  stored(runSid: string): FunctionShapeJson | undefined {
    return own(this.paths.shapes, runSid)
  }

  locate(runSid: string): FunctionGraph | null {
    if (!this.located.has(runSid)) this.located.set(runSid, this.place(runSid))
    return this.located.get(runSid)!
  }

  private place(runSid: string): FunctionGraph | null {
    const base = baseSid(runSid)
    const group = this.groups.get(base) ?? []
    if (!this.dups.has(base) && group.length <= 1) return group.length === 1 && sidOf(group[0]) === runSid ? group[0] : null
    const stored = this.stored(runSid)
    if (!stored) return null
    const then = Object.entries(this.paths.shapes)
      .filter(([s]) => baseSid(s) === base)
      .sort((x, y) => nthSid(x[0]) - nthSid(y[0]))
      .map(([s, v]) => [s, v.shape] as const)
    const now = group.map((g) => [sidOf(g), functionShape(g)] as const)
    const same = then.length === now.length && then.every(([s, h], i) => s === now[i][0] && h === now[i][1])
    if (this.dups.has(base) && same) return group.find((g) => sidOf(g) === runSid) ?? null // 这一组没变：#n 还指同一个定义
    if (TRIVIAL_SHAPES.has(stored.shape)) return null // 空形状认不出是哪一个
    const hits = group.filter((g) => functionShape(g) === stored.shape)
    const twins = then.filter(([s, h]) => h === stored.shape && s !== runSid)
    return hits.length === 1 && twins.length === 0 ? hits[0] : null
  }
}

/** 一个函数的运行汇总叠到函数图上：运行里的节点 sid → 图里的节点 id，配不上的个数 */
function matchRun(f: FunctionGraph, run: FunctionRunJson, stored: FunctionShapeJson | undefined): { mapping: Map<string, string>; unmatched: number } {
  const bySid = new Map(f.nodes.map((n) => [n.sid || n.id, n.id]))
  const sids = new Set(Object.keys(run.nodes))
  for (const k of Object.keys(run.exits)) {
    const s = k.split('|')[0]
    if (s) sids.add(s)
  }
  const mapping = new Map<string, string>()
  if (stored && stored.shape === functionShape(f)) {
    for (const s of sids) if (bySid.has(s)) mapping.set(s, bySid.get(s)!)
  } else {
    const dups = new Set(stored?.dups ?? [])
    const counts = baseCounts(f)
    for (const s of sids) {
      const b = baseSid(s)
      if (bySid.has(s) && !dups.has(b) && counts.get(b) === 1) mapping.set(s, bySid.get(s)!)
    }
  }
  return { mapping, unmatched: sids.size - mapping.size }
}

export interface PlacedFunction {
  fn: FunctionGraph
  /** 运行时的函数 sid */
  runSid: string
  run: FunctionRunJson
  /** 运行里的节点 sid → 图里的节点 id */
  mapping: Map<string, string>
}

export interface LocatedTest {
  test: TestRunJson | null
  /** 图里的函数 key → 放上去的运行 */
  placed: Map<string, PlacedFunction>
  /** 放不上的函数个数 + 放上了的函数里配不上的节点 sid 个数 */
  unmatched: number
}

/** 运行路径里的一个测试叠到这张图上（core 的 locate_test_run）。测试不在 paths 里时 test 为 null。 */
export function locateTestRun(graph: ProjectGraph, paths: RunPathsJson, testId: string): LocatedTest {
  const test = paths.tests.find((t) => t.test === testId) ?? null
  const placed = new Map<string, PlacedFunction>()
  let unmatched = 0
  if (!test) return { test, placed, unmatched }
  const placer = new Placer(graph, paths)
  for (const [runSid, run] of Object.entries(test.functions)) {
    const f = placer.locate(runSid)
    if (!f) {
      unmatched += 1
      continue
    }
    const m = matchRun(f, run, placer.stored(runSid))
    placed.set(f.key, { fn: f, runSid, run, mapping: m.mapping })
    unmatched += m.unmatched
  }
  return { test, placed, unmatched }
}

/**
 * 测试的入口函数（同 core 的 entry_functions）：这个测试里执行过、却没有任何一个执行过的函数记录过（直接 calls 或经
 * 项目以外的帧间接 icalls）调用它的（自己调用自己不算）——测试直接调用的，或者记不到调用方的（事件循环调度的任务、
 * 别的线程、流程直接迭代的生成器）。运行时的函数 sid，排序。
 */
export function entryFunctions(test: TestRunJson): string[] {
  const called = new Set<string>()
  for (const [fsid, f] of Object.entries(test.functions)) {
    for (const callee of [...Object.keys(f.calls ?? {}), ...Object.keys(f.icalls ?? {})]) if (callee !== fsid) called.add(callee)
  }
  return Object.keys(test.functions)
    .filter((fsid) => !called.has(fsid))
    .sort()
}
