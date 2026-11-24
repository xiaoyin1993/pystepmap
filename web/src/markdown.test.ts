import { describe, expect, it } from 'vitest'
import { renderMarkdown } from './markdown'

describe('renderMarkdown', () => {
  it('标题、列表、行内代码、加粗渲染成对应的标签', () => {
    const html = renderMarkdown('## Context\n\n- `billing_tags` 是项目\n\n**Goals:**\n')
    expect(html).toContain('<h2>Context</h2>')
    expect(html).toContain('<li><code>billing_tags</code> 是项目</li>')
    expect(html).toContain('<strong>Goals:</strong>')
  })

  it('flow 代码块保留缩进和换行', () => {
    const html = renderMarkdown('```flow\n判断：需要复核？\n  是：\n    步骤：提交\n```\n')
    expect(html).toContain('<pre><code class="language-flow">判断：需要复核？\n  是：\n    步骤：提交\n</code></pre>')
  })

  it('文件里写的 HTML 不执行，当文字显示', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;')
  })

  it('javascript: 链接不成链接，普通链接新开标签页', () => {
    expect(renderMarkdown('[x](javascript:alert(1))')).not.toContain('<a')
    expect(renderMarkdown('[x](https://example.com)')).toContain(
      '<a href="https://example.com" target="_blank" rel="noopener noreferrer">',
    )
  })

  it('表格外面套一层可以横向滚动的容器', () => {
    const html = renderMarkdown('| a | b |\n|---|---|\n| 1 | 2 |\n')
    expect(html).toContain('<div class="md-table"><table>')
    expect(html).toContain('</table></div>')
  })
})
