# 提交规则（COMMIT RULES）

> 2026-10-11 建立。起因：仓库审计发现 107 个不该入库的文件（构建产物 66 个 + 第三方二进制 41 个），
> `.git` 因此涨到 253 MB。本文档把「什么该进版本控制」固化下来，避免重犯。

## 一、绝不入库（已经在 .gitignore）

| 类别 | 具体路径 / 模式 | 为什么 |
|---|---|---|
| **密钥与私有配置** | `ai_config.json`、`llm_config.json`、`qc_config.json`、`.env*`、`*.enc`、`.secret_key` | 含 API Key 明文 |
| **运行产物** | `output/`、`/tasks.db`、`logs/`、`*.log` | 体积大、因机而异、可从源码重建 |
| **构建产物** | `app/static/`、`frontend/dist/`、`electron-app/dist/`、`packaging/_dist/` | `npm run build` 可重建（带内容哈希是典型特征） |
| **第三方二进制** | `resources/whisper/`、`node_modules/`、`*.exe`、`*.dll` | 应从上游发布页获取，体积大且与平台绑定 |
| **本机快照** | `*.bak_*`、`*.rollback_*`、`backup_code_*/`、`.workbuddy/`、`.local/`、`.mimosa/` | 个人历史，无协作价值 |
| **模型权重** | `*.safetensors`、`*.ckpt`、`*.pth`、`*.onnx` | 单文件常达 GB 级 |
| **小说原文** | `novels/` | 含版权内容 |

### 体积红线
- 单文件 **> 5 MB**：提交前三思，先问「这是生成件还是源？」
- 单文件 **> 50 MB**：禁止入库（GitHub 硬限 100 MB，超限推送直接失败）
- 确需的大二进制：用 **Git LFS**，或写脚本从上游下载

## 二、必须入库

- **源码**：`app/**/*.py`、`frontend/src/**`、`electron-app/src/**`、`tests/**`
- **构建脚本与配置**：`package.json`、`*.yml`、`serve.py`、`pack_backend.js`、`make_package.py`
- **提示词模板**：`app/prompts/*.txt` —— **这是业务配置，不是密钥**，必须版本化
  （历史事故：改了外部模板忘了代码内兜底 → 行为分叉，正因模板是受版本控制的资产才好定位）
- **文档**：`*.md`、`docs/`
- **品牌资源**：`electron-app/build/**`（图标；CI 打包需要，故在 .gitignore 里专门放行）

## 三、提交前自检（3 条）

1. **逐文件自问**：`git status` 里每个待提交文件，能否用一句话说明「为什么它必须进版本控制」？
   答不上来的，就是生成件，应进 .gitignore。
2. **看 diff 的形态**：`git diff --stat` —— 纯代码重构不该出现二进制、不该有 MB 级数字。
3. **新增目录先看内容**：`git add` 一个目录前，先 `git status --short <dir>` 确认里面没有产物/缓存。

## 四、提交信息规范

```
<type>(<scope>): <一句话说清做了什么>（<量化结果>）

<为什么这么做：问题现象、根因、影响>（一段）

<怎么做：关键取舍、踩过的坑、修了哪些自己的错误>

<验证：跑了什么、看到什么数字>
```

- **type**：`feat` / `fix` / `refactor` / `test` / `docs` / `chore` / `perf`
- **量化结果**：如「app.py -684 行」「50 个测试通过」
- **失败与自我纠错也要写进去**：本项目多次出现「我的自动脚本误删代码」，
  如实记录比粉饰更有价值 —— 下次才不会重犯。

## 五、⚠️ 已入库的大文件不会因 ignore 而消失

`git rm --cached` 只**停止跟踪**，**历史里的对象仍在** `.git` 中（本仓库仍有 250+ MB 历史包袱）。

要真正瘦身必须重写历史（`git filter-repo` 或 BFG），这会**改写所有 commit hash**，
影响已 clone 的副本，属于需要明确授权的操作 —— **不要擅自执行**。

## 六、本仓库的常见陷阱（实测）

- `app/static/`：容易误以为是源码。实际是 Vite 输出，源在 `frontend/src/`；
  `app/static/assets/*.jpg` 与 `frontend/src/assets/styles/*.jpg` **一一对应**（已核对 61/61）。
  停止跟踪后，本机开发/部署前必须先 `npm run build`。
- `resources/whisper/`：whisper.cpp 的 Release 预编译件，41 个 exe/dll。
  停止跟踪后，需从上游获取或保留本机副本。
- `*.bak_*` / `*.rollback_*`：改代码时留的回溯快照，曾混进桌面安装包（实测 3 个约 710 KB）。
- `%SystemDrive%/`：某次脚本把环境变量当字面量用，在仓库根建出该目录（已忽略）。
