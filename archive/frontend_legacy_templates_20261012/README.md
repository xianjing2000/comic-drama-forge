# 归档：前端遗留模板（死代码）2026-10-12

## 归档内容

| 文件 | 大小 | 原路径 |
|---|---|---|
| build_v2.py | 31085 B | app/templates/build_v2.py |
| index.html | 8017 B | app/templates/index.html |
| __pycache__/ | — | app/templates/__pycache__/（build_v2 的 pyc，313/314 两份） |

## 为什么归档（问题）

t14 前端审计（2026-10-12）结论：工作区曾并存**三套前端来源**，其中这一套从**未被使用**，
但它会让排查者得出「前端全崩」的错误结论：

1. **不可达**：全仓没有任何 effective 的 `render_template(...)` 调用
   （`app/app.py:16` / `app/asset_refs.py:50` / `app/storyboard_helpers.py:95` /
   `app/video_helpers.py:212` / `app/routes/autonomous.py:11` 均只有 import，无调用点）。
   Flask 因此永远不会渲染这套模板。
2. **无构建来源**：全仓 `grep build_v2` = 0 命中，没有任何代码 import 或执行它。
3. **引用破窗**：`index.html` 末尾两行引用 `/static/js/i18n.js` 与 `/static/js/app.js`，
   而 `app/static/js` 目录**不存在**（`Test-Path app/static/js` → False）。
4. **同名不同物**：本文件 `index.html`（8017 B / 244 行、14 处 `data-i18n`、
   标题「漫剧工坊 · 全自动AI生产平台」、外链 cdn.jsdelivr.net 的 bootstrap-icons）
   与 Flask 真正 serve 的 `app/static/index.html`
   （Vite 产物、约 2.3 KB / 50 行、标题「漫剧生成系统」）**是两个不同文件**，
   极易混淆，是本次审计最耗时的误判点。

## 唯一权威构建路径（收敛结论）

```
frontend/（React 18 + TS + Vite，唯一真源码，src/ 118 文件）
      │  npm run build   （= tsc && vite build）
      ▼
app/static/           （唯一交付产物；Flask 默认 static 目录，URL 前缀 /static）
      │  electron-app/pack_backend.js:93-108（打包时再触发一次 npm run build）
      ▼
electron-app/_backend/app/static → 安装包 / resource-mirror
```

- 构建配置：`frontend/vite.config.ts:46-52`，`build.outDir = '../app/static'`、`emptyOutDir: true`。
- 入口：`frontend/index.html`（Vite 注入 `/assets/index-<hash>.js|css`）；
  静态附件来自 `frontend/public/`（`favicon.svg`、`te_3d_render/render.html`）。

## 验证（归档前，只读）

- `build_v2` 引用数 = 0；`render_template(` 有效调用 = 0；`app/templates` 路径引用 = 0。
- 归档动作后立即复验生产：`GET /api/autopilot/status` → HTTP 200（running=true，第1集 step=video 50%）、
  `GET /` → HTTP 200、`GET /api/i18n/zh-CN` → HTTP 200（success=true，71 个顶层 key）、
  服务日志无 `TemplateNotFound` / `jinja2` 报错。**生产未受影响**。

## 如何恢复

若将来确实需要这套页面（不推荐，它与 React 版是两套独立实现）：

```powershell
Move-Item archive\frontend_legacy_templates_20261012\app_templates\* app\templates\
```

但恢复前必须先解决它的破窗（补 `/static/js/i18n.js` 与 `/static/js/app.js`，
或在后端为它新增一条真实的 `render_template` 路由），否则它依然是不可达的死代码。

## 为什么是「归档」而不是「直接删除」

- `build_v2.py` 与 `index.html` 此前**被 git 跟踪**，删除后仍可从历史找回；
  但项目既有惯例（`archive/legacy_scripts_20261008/`、`archive/stale_guards_20261009/`）
  是「移入 archive 并保留文件」，本归档沿用该惯例。
- 本目录不被 `.gitignore` 忽略（仅 `archive/reports_*/`、`archive/code_backups_*/`、
  `archive/local_docs/` 被忽略），故归档内容随仓库版本化，可回溯。
