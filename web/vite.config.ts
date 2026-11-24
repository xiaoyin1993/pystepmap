import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发时（npm run dev）把 /api 转给平台后端：先在仓库根目录运行 uv run pystepmap serve --no-open（默认端口 5179）。
// 后端换了端口或地址时，启动开发服务器前设环境变量 PYSTEPMAP_API，例如 PYSTEPMAP_API=http://127.0.0.1:5191 npm run dev。
// 打包后的页面（npm run build → web/dist）由 pystepmap serve 直接提供，和接口同源，不经过这里。
const API = process.env.PYSTEPMAP_API || 'http://127.0.0.1:5179'

export default defineConfig({
  plugins: [vue()],
  // 页面上"连不上后端"的提示里写出开发服务器把 /api 转到了哪里
  define: { __PYSTEPMAP_API__: JSON.stringify(API) },
  server: { port: 5180, proxy: { '/api': { target: API } } },
  // elkjs 的浏览器包（elk.bundled.js）本身就有 1.4 MB，拆不小
  build: { chunkSizeWarningLimit: 2500 },
})
