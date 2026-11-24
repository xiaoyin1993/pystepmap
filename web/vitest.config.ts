import { defineConfig } from 'vitest/config'

// 测试只覆盖不依赖界面的逻辑（src/graph、接口客户端、手工项、状态和视口逻辑；后端用假的 fetch 代替），不需要 Vue 插件。
// 真实项目的图：设环境变量 PYSTEPMAP_TEST_GRAPHS 指向放了图 JSON 的目录（见 src/graph/layout.test.ts），不设就跳过。
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
    testTimeout: 60_000,
  },
})
