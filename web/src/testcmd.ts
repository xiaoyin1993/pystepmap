// 测试命令的表单：把"在哪个目录、哪个解释器、哪些环境变量、pytest 的参数"分开填，拼成后端存的那一条命令
// （后端、确认、运行都还是只认这一条命令）。存着的命令是按这个样子写的才拆回表单：拆出来再拼回去和原文一字不差
// 才算——否则对话框让人直接改整条命令。
//
// 拼出来的样子：[cd <目录> && ][名字=<值> ]…<解释器> -m pytest[ <参数>]
// - 目录、解释器、变量的值：只有字母、数字和 _@%+=:,./$-{} 时原样写（$NAME 照常展开），否则放进双引号（\ " ` 前加 \，
//   $ 不转义，照常展开）；
// - 参数：原样接在后面（它是 shell 的写法，引号、$(…) 都由填的人自己写）。

export interface TestForm {
  /** 在项目目录下的哪个子目录里执行（空 = 项目目录） */
  cwd: string
  /** 解释器（相对上面的目录，或绝对路径） */
  python: string
  env: { name: string; value: string }[]
  /** pytest 的参数（测试目录、文件、选项） */
  args: string
}

export const DEFAULT_FORM: TestForm = { cwd: '', python: '.venv/bin/python', env: [], args: 'tests -q' }

const BARE = /^[A-Za-z0-9_@%+=:,./${}-]+$/
const NAME = /^[A-Za-z_][A-Za-z0-9_]*$/

const quote = (s: string): string => (BARE.test(s) ? s : `"${s.replace(/[\\"`]/g, '\\$&')}"`)

/** 表单哪里填得不对（没问题时 null） */
export function formProblem(form: TestForm): string | null {
  const python = form.python.trim()
  if (!python) return '解释器没有填'
  for (const s of [form.cwd, form.python, form.args, ...form.env.flatMap((e) => [e.name, e.value])]) {
    if (/[\n\r\0]/.test(s)) return '每一项都只能写一行'
  }
  // 以 - 开头的解释器、目录会被当成选项；名字=值 样子的解释器会被当成环境变量
  if (python.startsWith('-') || /^[A-Za-z_][A-Za-z0-9_]*=/.test(python)) return `解释器的写法不对：${python}`
  if (form.cwd.trim().startsWith('-')) return `目录的写法不对：${form.cwd.trim()}`
  const seen = new Set<string>()
  for (const e of form.env) {
    const name = e.name.trim()
    if (!name && !e.value.trim()) continue
    if (!NAME.test(name)) return name ? `环境变量的名字只能用字母、数字和下划线，不能以数字开头：${name}` : '有一个环境变量没有写名字'
    if (seen.has(name)) return `环境变量 ${name} 写了两次`
    seen.add(name)
  }
  return null
}

/** 表单 → 命令（先用 formProblem 查过；空的环境变量行不算） */
export function composeCommand(form: TestForm): string {
  const cwd = form.cwd.trim()
  const args = form.args.trim()
  const env = form.env.filter((e) => e.name.trim()).map((e) => `${e.name.trim()}=${e.value === '' ? '""' : quote(e.value)} `)
  return `${cwd ? `cd ${quote(cwd)} && ` : ''}${env.join('')}${quote(form.python.trim())} -m pytest${args ? ` ${args}` : ''}`
}

const TOKEN = String.raw`(?:[A-Za-z0-9_@%+=:,./\${}-]+|"(?:[^"\\\`]|\\["\\\`])*")`
const CD = new RegExp(`^cd (${TOKEN}) && `)
const ENV = new RegExp(`^([A-Za-z_][A-Za-z0-9_]*)=(${TOKEN}) `)
const PY = new RegExp(`^(${TOKEN}) -m pytest(?: (.*))?$`)

const unquote = (t: string): string => (t.startsWith('"') ? t.slice(1, -1).replace(/\\(["\\`])/g, '$1') : t)

/** 命令 → 表单；不是按表单的样子写的（拼回去和原文不一样）时 null */
export function parseCommand(command: string): TestForm | null {
  let rest = command
  const form: TestForm = { cwd: '', python: '', env: [], args: '' }
  const cd = CD.exec(rest)
  if (cd) {
    form.cwd = unquote(cd[1])
    rest = rest.slice(cd[0].length)
  }
  for (let m = ENV.exec(rest); m; m = ENV.exec(rest)) {
    form.env.push({ name: m[1], value: unquote(m[2]) })
    rest = rest.slice(m[0].length)
  }
  const py = PY.exec(rest)
  if (!py) return null
  form.python = unquote(py[1])
  form.args = py[2] ?? ''
  if (formProblem(form) !== null || composeCommand(form) !== command) return null
  return form
}
