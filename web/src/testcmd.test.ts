import { describe, expect, it } from 'vitest'
import { composeCommand, DEFAULT_FORM, formProblem, parseCommand, type TestForm } from './testcmd'

const form = (over: Partial<TestForm>): TestForm => ({ ...DEFAULT_FORM, ...over })

describe('composeCommand', () => {
  it('默认的表单就是最常见的那条命令', () => {
    expect(composeCommand(DEFAULT_FORM)).toBe('.venv/bin/python -m pytest tests -q')
  })

  it('目录、环境变量、参数按顺序拼上', () => {
    expect(
      composeCommand(
        form({ cwd: 'backend', env: [{ name: 'PYTHONDONTWRITEBYTECODE', value: '1' }, { name: 'PYTHONPATH', value: 'src:$PYTHONPATH' }], args: '-q tests/test_a.py' }),
      ),
    ).toBe('cd backend && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:$PYTHONPATH .venv/bin/python -m pytest -q tests/test_a.py')
  })

  it('有空格、引号的值放进双引号，$ 不转义；空值写成 ""；空行不算；没有参数时不留空格', () => {
    expect(
      composeCommand(
        form({
          cwd: 'my dir',
          python: '/opt/py 3/bin/python',
          env: [{ name: 'A', value: 'x "y" `z` \\ $HOME' }, { name: 'B', value: '' }, { name: '', value: '' }],
          args: '  ',
        }),
      ),
    ).toBe('cd "my dir" && A="x \\"y\\" \\`z\\` \\\\ $HOME" B="" "/opt/py 3/bin/python" -m pytest')
  })
})

describe('parseCommand', () => {
  const cases: TestForm[] = [
    DEFAULT_FORM,
    form({ cwd: 'backend', env: [{ name: 'PYTHONDONTWRITEBYTECODE', value: '1' }], args: `-p no:cacheprovider -q $(grep -rlE "_workflow|step_[a-z_]+\\(" tests --include='test_*.py' | sort)` }),
    form({ cwd: 'my dir', python: '/opt/py 3/bin/python', env: [{ name: 'A', value: 'x "y" `z` \\ $HOME' }, { name: 'B', value: '' }], args: '' }),
    form({ python: 'python3.13', args: '-k "a and b" tests' }),
  ]
  it.each(cases)('拼出来的命令拆得回原来的表单 %#', (f) => {
    expect(parseCommand(composeCommand(f))).toEqual(f)
  })

  it.each([
    'pytest tests -q',
    'uv run pytest',
    '.venv/bin/python -m pytest  tests', // 两个空格：拼回去不一样
    "cd 'backend' && .venv/bin/python -m pytest",
    'cd backend; .venv/bin/python -m pytest',
    'A=1 B=2',
    'echo && x -m pytest y',
    '',
  ])('不是表单的样子时 null：%s', (c) => {
    expect(parseCommand(c)).toBeNull()
  })
})

describe('formProblem', () => {
  it('没问题时 null', () => {
    expect(formProblem(DEFAULT_FORM)).toBeNull()
    expect(formProblem(form({ env: [{ name: '', value: ' ' }] }))).toBeNull()
  })
  it.each([
    [form({ python: ' ' }), '解释器没有填'],
    [form({ python: '-c' }), '解释器的写法不对：-c'],
    [form({ python: 'A=1' }), '解释器的写法不对：A=1'],
    [form({ cwd: '-x' }), '目录的写法不对：-x'],
    [form({ args: 'a\nb' }), '每一项都只能写一行'],
    [form({ env: [{ name: '1A', value: 'x' }] }), '环境变量的名字只能用字母、数字和下划线，不能以数字开头：1A'],
    [form({ env: [{ name: '', value: 'x' }] }), '有一个环境变量没有写名字'],
    [form({ env: [{ name: 'A', value: '1' }, { name: 'A', value: '2' }] }), '环境变量 A 写了两次'],
  ])('%#', (f, text) => {
    expect(formProblem(f)).toBe(text)
  })
})
