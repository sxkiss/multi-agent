<!-- AUTO-DOC: Update me when files in this folder change -->

# Frontend Source

Vue3 + Vite 前端源码。

## Files

| File | Role | Function |
|------|------|----------|
| `main.js` | Entry | Vue3 应用入口，挂载 App |
| `App.vue` | Core | 全局状态机：SSE 处理器、消息队列、任务状态栏 |
| `styles.css` | Style | 全局样式 |
| `vite.config.js` | Config | Vite 构建配置 |
| `package.json` | Config | 前端依赖声明 |
| `build.sh` | Deploy | 构建脚本 |

## Subdirectories

- [components/](./components/INDEX.md) — UI 组件（ChatMain / ChatSidebar / ChatInput / MessageItem / SettingsDrawer）
