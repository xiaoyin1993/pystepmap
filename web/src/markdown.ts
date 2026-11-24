// 把需求文件（Markdown）渲染成 HTML，给"查看文件内容"用。文件是被分析的项目里的，内容不可信：
// 不认文件里写的 HTML（原样当文字显示），链接的协议由 markdown-it 把关（javascript: 之类不成链接）。
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({ html: false, linkify: false, breaks: false })

// 链接一律新开标签页，不带来源页
const renderLink =
  md.renderer.rules.link_open ?? ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return renderLink(tokens, idx, options, env, self)
}

// 表格外面套一层，宽了只滚表格自己
md.renderer.rules.table_open = () => '<div class="md-table"><table>\n'
md.renderer.rules.table_close = () => '</table></div>\n'

export function renderMarkdown(text: string): string {
  return md.render(text.replace(/\r\n?/g, '\n'))
}
