'use strict';
// 漫剧工坊 桌面版 — Electron 主进程
// 职责：管理本地 Flask 后端（serve.py）的生命周期，并把 BrowserWindow 指向运行中的
//       Flask URL（http://127.0.0.1:<port>）。UI 的唯一事实源仍是 Flask，不复制 app/static。


// ---------------------------------------------------------------------------
// CLI 自检出口（t15，2026-10-12）
// ---------------------------------------------------------------------------
// 允许在没有 Electron 运行时的纯 node 下加载本文件，用于验证镜像指纹与播种决策：
//   node electron-app/main.js --mirror-fingerprint <root> [--mirror-side] [--json]
//   node electron-app/main.js --mirror-compare <installedRoot> <mirrorRoot> [--json]
//   node electron-app/main.js --mirror-decide <installedRoot> <mirrorRoot> [选项]
//   node electron-app/main.js --mirror-selftest [--json]
// 跑的就是本文件里运行时真正在用的那批函数（不复制第二份实现），不带这些参数时
// 行为与此前完全一致（正常启动 Electron）。
const MIRROR_CLI_ARGV = process.argv.slice(2);
const MIRROR_CLI_CMD = (function () {
  const a = MIRROR_CLI_ARGV[0];
  if (a === '--mirror-fingerprint') return 'fingerprint';
  if (a === '--mirror-compare') return 'compare';
  if (a === '--mirror-decide') return 'decide';
  if (a === '--mirror-seed') return 'seed';
  if (a === '--mirror-selftest') return 'selftest';
  return '';
})();

// ⭐ 2026-10-06：Electron/Chromium 静默崩溃诊断
// 打包版是 GUI 子系统程序，stdout/stderr 被系统丢弃，whenReady 之前的任何异常
// 表现就是「双击闪退、无日志」（用户实测 10/6 多次遇到）。这里在**任何 Electron
// 模块加载之前**（本文件顶部、require 之前）设好环境变量：
//   · ELECTRON_ENABLE_LOGGING=1        → Chromium 把 bootstrap 阶段日志写到 stderr
//   · ELECTRON_ENABLE_STACK_DUMPING=1  → 崩溃时生成 .dmp 到 userData/Crashpad
// 同时再写一个独立的「超早期」日志：连 Electron 启动器本身都崩掉时，desktop.log
// 不会有任何 bootLog 行，但下面这行会在进程一启动就立刻落盘，至少能区分
// 「进程根本没起」vs「起了但 main.js 没加载到」。
if (process.platform === 'win32' && !MIRROR_CLI_CMD) {
  try {
    process.env.ELECTRON_ENABLE_LOGGING = '1';
    process.env.ELECTRON_ENABLE_STACK_DUMPING = '1';
    const fsEarly = require('node:fs');
    const pathEarly = require('node:path');
    const osEarly = require('node:os');
    const dir = pathEarly.join(osEarly.homedir(), 'AppData', 'Roaming', 'mjscxt-desktop');
    fsEarly.mkdirSync(dir, { recursive: true });
    fsEarly.appendFileSync(
      pathEarly.join(dir, 'early_boot.log'),
      `[${new Date().toISOString()}] early-boot pid=${process.pid} ppid=${process.ppid} argv=${JSON.stringify(process.argv).slice(0, 400)}\n`,
    );
  } catch { /* 日志失败绝不影响启动 */ }
}

// ⭐ t15（2026-10-12）：CLI 自检模式下容忍「没有 Electron 运行时」——
//    纯 node 里 require('electron') 可能返回二进制路径字符串、也可能直接抛错，
//    两种情况都不该让 --mirror-* 的部署自检用不了。正常启动路径行为不变。
let electronModule = null;
try {
  electronModule = require('electron');
} catch (e) {
  if (!MIRROR_CLI_CMD) throw e;
}
const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  shell,
} = (electronModule && typeof electronModule === 'object') ? electronModule : {};

// ⭐ t15：全量指纹要逐文件算 sha1（见 computeMirrorFingerprint）
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const http = require('node:http');
const net = require('node:net');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const updater = require('./updater');
const updateConfig = require('./update-config');

// ---------------------------------------------------------------------------
// 启动日志
// ---------------------------------------------------------------------------
// ⚠️ Windows 的 GUI 子系统程序**没有控制台**，stdout/stderr 会被系统直接丢弃：
// 打包版一旦在 whenReady 之前抛异常，表现就是「双击没反应 / 一闪而过」，
// 看不到任何输出（实测：Start-Process 重定向出来的 log 是空的）。
// 所以这里把关键节点落盘，启动失败至少有据可查。
function bootLog(msg) {
  try {
    const dir = app.getPath('userData');
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, 'desktop.log'),
      `[${new Date().toISOString()}] ${msg}\n`);
  } catch { /* 日志失败绝不影响启动 */ }
}

process.on('uncaughtException', (e) => {
  bootLog(`uncaughtException: ${(e && e.stack) || e}`);
});
process.on('unhandledRejection', (e) => {
  bootLog(`unhandledRejection: ${(e && e.stack) || e}`);
});

// ---------------------------------------------------------------------------
// 常量
// ---------------------------------------------------------------------------

// 默认项目根 = 本 electron-app/ 的上级目录（即 漫剧生成系统/）
const DEFAULT_PROJECT_ROOT = path.resolve(__dirname, '..');
// 已知 venv python（用户机上的默认解释器）
const KNOWN_PYTHON =
  'C:\\Users\\liujianghua\\.workbuddy\\binaries\\python\\envs\\mjscxt\\Scripts\\python.exe';
// 桌面版专用端口：避免与浏览器版/PyInstaller 版共用 5210 时串数据根
const DEFAULT_PORT = 5211;

// 环形缓冲：子进程 stdout/stderr 最多保留 500 行
const LOG_RING_MAX = 500;
// 就绪探测：GET / 返回 200 才算就绪（不用 /api/status —— 它会去探 ComfyUI）
const READINESS_INTERVAL_MS = 500;
const READINESS_TIMEOUT_MS = 60000;
// 优雅停机：先 SIGINT，等 10s 还活着就 taskkill 兜底
const GRACEFUL_WAIT_MS = 10000;

// ---------------------------------------------------------------------------
// 配置（存于 Electron userData/backend.json）
// ---------------------------------------------------------------------------

function configPath() {
  return path.join(app.getPath('userData'), 'backend.json');
}

function defaultConfig() {
  return { projectRoot: DEFAULT_PROJECT_ROOT, pythonExe: KNOWN_PYTHON, port: DEFAULT_PORT };
}

function loadConfig() {
  const def = defaultConfig();
  try {
    if (fs.existsSync(configPath())) {
      const raw = JSON.parse(fs.readFileSync(configPath(), 'utf8'));
      return {
        projectRoot: typeof raw.projectRoot === 'string' ? raw.projectRoot : def.projectRoot,
        pythonExe: typeof raw.pythonExe === 'string' ? raw.pythonExe : def.pythonExe,
        port: Number.isFinite(raw.port) ? raw.port : def.port,
      };
    }
  } catch {
    /* 配置损坏 → 用默认 */
  }
  return def;
}

function saveConfig(cfg) {
  try {
    fs.mkdirSync(path.dirname(configPath()), { recursive: true });
    fs.writeFileSync(configPath(), JSON.stringify(cfg, null, 2), 'utf8');
  } catch (e) {
    console.error('保存 backend.json 失败：', e);
  }
}

let config = loadConfig();

// 校验项目根：app/serve.py 必须存在；.env 缺失只是警告（应用有内置默认值）
function validateProjectRoot(root) {
  const warnings = [];
  const servePy = path.join(root, 'app', 'serve.py');
  if (!fs.existsSync(servePy)) {
    throw new Error(`项目根目录无效：找不到 ${servePy}`);
  }
  if (!fs.existsSync(path.join(root, '.env'))) {
    warnings.push('未找到 .env（将使用应用内置默认配置）');
  }
  return warnings;
}

// python 解释器检测顺序：用户配置 → 已知 venv → `py -0` 枚举
async function detectPython() {
  if (config.pythonExe && fs.existsSync(config.pythonExe)) {
    return config.pythonExe;
  }
  if (fs.existsSync(KNOWN_PYTHON)) {
    return KNOWN_PYTHON;
  }
  try {
    const { execFile } = require('node:child_process');
    const out = await new Promise((resolve, reject) => {
      execFile('py', ['-0'], { windowsHide: true }, (err, so, se) =>
        err ? reject(err) : resolve(`${so}\n${se}`)
      );
    });
    // `py -0` 输出形如： -3.12-64 C:\...\python.exe
    const m = out.match(/-\d[\d.]*(?:-\d+)?\s+([^\s]+\s*python[\d._-]*\.exe)/i);
    if (m) return m[1].trim();
  } catch {
    /* py 不可用 */
  }
  throw new Error('未找到可用的 Python 解释器，请在设置中手动指定 python 路径');
}

// ---------------------------------------------------------------------------
// 后端布局：独立 exe vs 开发
// ---------------------------------------------------------------------------
// 打包态（app.isPackaged）= 前后端都自包含：electron-builder 的 extraResources
//   里带一份「嵌入式 Python + app 源码 + workflows + 前端静态」，spawn 它自带的
//   python，并把可写数据目录指向 Electron userData（Program Files 只读）。
// 开发态（npm start）= 前后端分离：用本机 venv 跑项目源码，数据落源根（可写）。
function resolveBackendLayout() {
  if (app.isPackaged) {
    const res = process.resourcesPath; // 安装区（只读）：python + app + workflows + locales
    const exeName = process.platform === 'win32' ? 'python.exe' : 'python';
    // 资源镜像（userData 下可写）：启动时由 seedResourceMirror() 从安装区播种，
    // 资源增量更新会覆盖它 → 后端实际读镜像里的 app/serve.py，增量重启后即刻生效。
    const mirror = resourceMirrorDir();
    const mirrorServe = path.join(mirror, 'app', 'serve.py');
    const useMirror = fs.existsSync(mirrorServe);
    return {
      pythonExe: path.join(res, 'python', exeName), // python 归整包管理，永远走安装区
      // 安装区：resources/app = Electron 应用目录（asar:false 的产物），
      // 后端源码在 resources/backend —— 刻意避开 'app' 这个名字：
      // 之前 asar=true 时 electron-builder 会把应用打成**名为 app.asar 的目录**，
      // 而 Electron 对含 .asar 的路径一律按存档解析，目录不是合法存档 →
      // package.json 读不到 → 双击无反应、静默退出。改普通目录 + 改名即可根治。
      servePy: useMirror ? mirrorServe : path.join(res, 'backend', 'serve.py'),
      dataDir: path.join(app.getPath('userData'), 'mjscxt-data'), // 可写数据区（output/novels/*.config.json/密钥）
      cwd: useMirror ? mirror : res,
    };
  }
  // 开发态：沿用 config（项目根 + 本机 venv），不设 MJSCXT_DATA_DIR（源根可写）
  return {
    pythonExe: null, // 交给 detectPython() 解析本机 venv
    servePy: path.join(config.projectRoot, 'app', 'serve.py'),
    dataDir: null,
    cwd: config.projectRoot,
  };
}

// ---------------------------------------------------------------------------
// 日志环形缓冲
// ---------------------------------------------------------------------------

const logLines = [];

function pushLog(stream, text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (line.length > 0) {
      logLines.push(`[${stream}] ${line}`);
      if (logLines.length > LOG_RING_MAX) logLines.shift();
    }
  }
}

function tailLog(n = 100) {
  return logLines.slice(-n);
}

// ---------------------------------------------------------------------------
// HTTP / 端口探测
// ---------------------------------------------------------------------------

// 单次 GET，返回响应状态码；连接失败/超时/非 200 一律返回 null
function httpGetStatus(url, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => {
      if (!settled) {
        settled = true;
        resolve(v);
      }
    };
    try {
      const req = http.get(url, (res) => {
        done(res.statusCode === 200 ? 200 : null);
        res.resume();
      });
      req.on('error', () => done(null));
      req.setTimeout(timeoutMs, () => {
        req.destroy();
        done(null);
      });
    } catch {
      done(null);
    }
  });
}

// 端口上是否有监听者（不关心是谁）
function isPortListening(port) {
  return new Promise((resolve) => {
    const sock = net
      .connect({ host: '127.0.0.1', port })
      .on('connect', () => {
        sock.destroy();
        resolve(true);
      })
      .on('error', () => resolve(false));
  });
}

// 找下一个空闲端口（从 DEFAULT_PORT+1 起，最多扫 999 个）
async function findFreePort() {
  for (let p = DEFAULT_PORT + 1; p < DEFAULT_PORT + 1000; p += 1) {
    if (!(await isPortListening(p))) return p;
  }
  throw new Error('5212–6210 端口范围内未找到空闲端口（共扫描 999 个），无法启动后端');
}

// 端口决策：桌面版**永不复用外部后端**。
// 旧逻辑会因 5210/5000 上已有健康 Flask 就直接复用，导致项目数据写到别的进程的数据根，
// 表现为「桌面版每次打开都是空项目」。现在：
// - 只有“本进程自己 spawn 且 childAlive”才复用（见 startBackend）；
// - 若默认端口被占用，直接找下一个空闲端口，始终保证桌面版有自己的后端与数据根。
// 探测某端口上的后端「身份」（数据根指纹）。返回 null = 那里不是本项目后端。
function probeBackendIdentity(port) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    let req;
    try {
      req = require('node:http').get(
        { host: '127.0.0.1', port, path: '/api/system/identity', timeout: 2500 },
        (res) => {
          let data = '';
          res.on('data', (c) => { data += c; });
          res.on('end', () => {
            try {
              const j = JSON.parse(data);
              finish(j && j.app === 'mjscxt' ? j : null);
            } catch (e) { finish(null); }
          });
        }
      );
    } catch (e) { return finish(null); }
    req.on('error', () => finish(null));
    req.on('timeout', () => { try { req.destroy(); } catch (e) {} finish(null); });
  });
}

function sameDataDir(a, b) {
  if (!a || !b) return false;
  try {
    return path.resolve(String(a)).toLowerCase() === path.resolve(String(b)).toLowerCase();
  } catch (e) { return false; }
}

// ⭐⭐ 2026-10-11 用户要求：「两个服务进程并存这个问题要解决，就和打开你现在运行一样，
//    检测到已打开的程序直接显示窗口就行了」。
//
// 之前的做法是**永不复用外部后端**（见上方注释：旧版会因端口上有健康 Flask 就复用，
// 导致数据写到别的数据根、桌面版打开是空项目），于是默认端口被占就另找空闲端口 ——
// 代价是「手工起的服务 + 桌面版服务」同时存在，互抢租约（日志里的
// 「正在被另一个执行体生产」即由此而来）。
//
// 现在补上缺失的判据：**复用前先校验数据根一致**。
//   · 指纹一致 → 直接复用（单实例，符合用户预期：「检测到已打开就直接显示」）；
//   · 不一致   → 仍另找空闲端口（保住原设计要防的数据根混淆）。
async function resolvePort(wantDataDir) {
  const ident = await probeBackendIdentity(DEFAULT_PORT);
  if (ident && sameDataDir(ident.data_dir, wantDataDir)) {
    bootLog('singleInstance: reuse backend on ' + DEFAULT_PORT + ' (same data root)');
    return { port: DEFAULT_PORT, reuse: true };
  }
  if (await isPortListening(DEFAULT_PORT)) {
    const port = await findFreePort();
    return { port, reuse: false };
  }
  return { port: DEFAULT_PORT, reuse: false };
}

// ---------------------------------------------------------------------------
// 更新系统（GitHub 公开仓库 · 匿名下载）
// ---------------------------------------------------------------------------
// 安装模型：
//   · 安装区 resourcesPath（extraResources）= 只读：python + app + workflows + locales
//   · 可写数据区 userData = output/novels/*.config.json/.secret_key/secrets.enc
//   · 资源镜像 userData/resource-mirror = 可写的 workflows/app/locales 副本（后端实际读这里）
// 两种更新：
//   资源增量：下载 resources-x.y.z.zip（不含 python）→ 校验 → 覆盖镜像 → 重启后端生效。体积小、不打断数据。
//   整包：下载 Portable exe → 校验 → spawn bootstrapper 替换安装区并自重启。
// 触发：启动检查一次 + 每 24h 后台轮询 + 菜单「检查更新」手动。
const UPDATE_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000; // 24h

function resourceMirrorDir() {
  return path.join(app.getPath('userData'), 'resource-mirror');
}

// 首次运行：把安装区 resourcesPath/{app,workflows,locales} 播种到可写镜像。
// 已存在且非空则跳过（增量更新会另行覆盖）。不播种 python（python 归整包管理，运行时用安装区）。
// 镜像版本标记文件：记录「当前镜像里的资源是哪一版」。
// ⭐ 2026-10-05 修复关键缺陷：旧实现只在镜像**不存在**时播种。于是整包更新（Portable
//   替换安装区）后，安装区已是新代码，但 userData 里的旧镜像仍然存在 → useMirror 命中
//   旧镜像 → 后端继续跑**更新前的老代码**。用户表现为「明明更新了却什么都没变」，
//   而且没有任何报错，极难定位。
// 现在：把版本写进标记文件，启动时比对「安装区版本 vs 镜像版本」，不一致就重新播种
// （安装区更新了说明换过整包，以安装区为准；资源增量则会把标记同步成资源版本）。
const MIRROR_VERSION_FILE = '.mirror-version';

// ⭐ 2026-10-07 修复关键缺陷（用户实测：「重新打包了，但桌面版看不到我新加的前端功能」）。
//   旧实现**只在版本号变化时**重播种。而开发/内测阶段经常「版本号不动、代码变了」
//   （本次 1.3.9 反复重建即是），于是安装区已是新代码，userData 里的旧镜像仍被
//   useMirror 命中 → 后端与前端**继续跑老代码**，且无任何报错，极难定位
//   （排查耗时：靠比对镜像与包内 static 的文件名哈希才确认）。
//   修法：额外记录「安装区关键文件内容指纹」，在**版本相同的分支**里比对，
//   不一致则重播种。
//   ⚠️ 刻意只做增量：完全不改动上面的版本比对逻辑 —— 资源增量更新会把
//      .mirror-version 写成**远端版本**（≠ 本地安装版本），走原有版本分支，
//      不会被本逻辑干扰；且该路径会清空内容标记（见 runResourceUpdate），
//      避免「增量更新后一启动就被本地安装区回滚」。
const MIRROR_CONTENT_FILE = '.mirror-content';

//: 内容标记的哨兵值：镜像内容来自**远端资源增量**，以镜像为准 ——
//: 同版本内容检测见到它就跳过重播种（见 runResourceUpdate）。
const MIRROR_CONTENT_REMOTE = 'remote';

// ===========================================================================
// 资源镜像：全量清单指纹 + 播种方向决策（t15，2026-10-12）
// ===========================================================================
// 修的两个高风险部署缺陷（t7 基础设施审计）：
//   ① 旧指纹只哈希 6 个 backend/*.py（外加 static 产物名与 workflows/*.json），
//      其余 120+ 文件（routes/ workers/ 各子目录…）改了**不触发重播种** ——
//      重新打包后桌面版启动仍跑镜像里的老代码，用户看不到改动且无任何报错。
//   ② 「同版本内容不一致 → 用安装区整体覆盖镜像」这条路径是**静默**的：开发者/会话
//      把工作区修复同步进可写镜像后，下次启动会被安装区旧代码回滚（生产代码倒退），
//      而且没有任何提示。
//
// 唯一事实源：下面的 MIRROR_TREES 既是**指纹范围**也是**播种范围**（同一份清单，
//   不会出现「指纹算了但没播」或「播了但指纹没算」的两套口径）。
// 口径对齐：快照文件与编译产物不参与 —— 与 tools/check_app_sync.py 的
//   SNAPSHOT_MARKERS / SKIP_DIRS / SKIP_SUFFIX 保持一致（同一套「需同步文件」定义）。

//: 参与指纹与播种的子树/文件：安装区相对路径 -> 镜像相对路径
//: ⚠️ 这里是**整棵子树**（不是文件白名单）—— 任何新增目录/文件自动纳入指纹。
const MIRROR_TREES = [
  ['backend', 'app'],
  ['workflows', 'workflows'],
  ['locales', 'locales'],
  ['.env', '.env'],
];

//: 遍历时跳过的目录（缓存 / 版本控制，不属于要分发的源码）
const MIRROR_SKIP_DIRS = new Set([
  '__pycache__', '.git', '.ipynb_checkpoints', '.mypy_cache', '.pytest_cache',
  '.venv', 'venv', 'node_modules',
]);
//: 跳过的文件后缀（编译产物）
const MIRROR_SKIP_SUFFIX = ['.pyc', '.pyo', '.pyd'];
//: 跳过的文件名（系统元数据 + 镜像自身的标记/日志/基准文件）
const MIRROR_SKIP_NAMES = new Set([
  '.DS_Store', 'Thumbs.db', '.mirror-content', '.mirror-version',
  '.mirror-seed.json', '.mirror-seed.log',
]);
//: 历史快照/备份标记 —— 与 tools/check_app_sync.py 的 SNAPSHOT_MARKERS **同源**。
//: 这类文件是历史备份、不参与分发；若纳入指纹，「安装区留了几个 .bak」就会让镜像
//: 永远判为不一致（每次启动都重播种）。
const MIRROR_SNAPSHOT_MARKERS = ['.bak_', '.bak.', '.bak-', '.rollback_'];

//: 单文件哈希前缀长度（sha1 前 12 位，与改造前一致，便于对照历史值）
const MIRROR_HASH_PREFIX = 12;
//: 目录指纹长度（sha1 前 16 位）—— .mirror-content 的格式不变
const MIRROR_FP_LENGTH = 16;

function mirrorSkipFile(name) {
  if (MIRROR_SKIP_NAMES.has(name)) return true;
  const low = String(name).toLowerCase();
  if (MIRROR_SKIP_SUFFIX.some(function (s) { return low.endsWith(s); })) return true;
  return MIRROR_SNAPSHOT_MARKERS.some(function (m) { return String(name).includes(m); });
}

//: 递归列出 dir 下参与同步的文件：Map<相对路径(POSIX), "size:sha1(12)">。
//: 逐个文件读内容算哈希 —— 这是「任何文件改动都能被发现」的根本。
function walkMirrorEntries(dir) {
  const out = new Map();
  const walk = function (cur, rel) {
    let items;
    try {
      items = fs.readdirSync(cur, { withFileTypes: true });
    } catch (e) {
      return;
    }
    items.sort(function (a, b) { return a.name < b.name ? -1 : (a.name > b.name ? 1 : 0); });
    for (const it of items) {
      const relPath = rel ? rel + '/' + it.name : it.name;
      const full = path.join(cur, it.name);
      if (it.isDirectory()) {
        if (MIRROR_SKIP_DIRS.has(it.name)) continue;
        walk(full, relPath);
        continue;
      }
      if (!it.isFile()) continue;
      if (mirrorSkipFile(it.name)) continue;
      try {
        const buf = fs.readFileSync(full);
        out.set(relPath, buf.length + ':' +
          crypto.createHash('sha1').update(buf).digest('hex').slice(0, MIRROR_HASH_PREFIX));
      } catch (e) {
        out.set(relPath, 'unreadable');
      }
    }
  };
  walk(dir, '');
  return out;
}

//: 把一棵树映射成「条目名 -> size:hash」的表。
//: ⚠️ 条目名一律用**安装区**的相对路径（如 backend/routes/agent.py），两侧才能直接比较。
//: kind='installed' 取安装区名字；kind='mirror' 取镜像名字（backend -> app 等映射）。
function mirrorEntryMap(root, kind) {
  const map = new Map();
  const trees = [];
  let bytes = 0;
  for (const pair of MIRROR_TREES) {
    const srcName = pair[0];
    const dstName = pair[1];
    const rel = kind === 'mirror' ? dstName : srcName;
    const full = path.join(root, rel);
    let st = null;
    try {
      st = fs.statSync(full);
    } catch (e) {
      st = null;
    }
    trees.push({ name: srcName, exists: !!st });
    if (!st) continue;
    if (st.isDirectory()) {
      for (const kv of walkMirrorEntries(full)) {
        map.set(srcName + '/' + kv[0], kv[1]);
        const sz = parseInt(String(kv[1]).split(':')[0], 10);
        if (Number.isFinite(sz)) bytes += sz;
      }
    } else {
      try {
        const buf = fs.readFileSync(full);
        map.set(srcName, buf.length + ':' +
          crypto.createHash('sha1').update(buf).digest('hex').slice(0, MIRROR_HASH_PREFIX));
        bytes += buf.length;
      } catch (e) {
        map.set(srcName, 'unreadable');
      }
    }
  }
  return { map: map, bytes: bytes, trees: trees };
}

//: 全量清单指纹：对清单内**每一个文件**取「条目名:大小:sha1(12)」，排序拼接后再
//: sha1 取前 16 位。新增/删除/改内容都会改变它（这就是「任何改动都触发重播种」）。
//: 缺失的子树记 tree:<name>:missing ——「整棵树没了」与「树为空」必须可区分。
function computeMirrorFingerprint(root, kind) {
  try {
    const em = mirrorEntryMap(root, kind || 'installed');
    const parts = [];
    for (const t of em.trees) {
      parts.push('tree:' + t.name + ':' + (t.exists ? 'present' : 'missing'));
    }
    const keys = Array.from(em.map.keys()).sort();
    for (const k of keys) parts.push(k + ':' + em.map.get(k));
    return crypto.createHash('sha1').update(parts.join('|')).digest('hex').slice(0, MIRROR_FP_LENGTH);
  } catch (e) {
    return '';
  }
}

//: 两侧逐条目差异 —— 覆盖前留痕用：把「将被覆盖/丢弃的内容」写进日志，回退可审计。
function diffMirrorTrees(installedRoot, mirrorRoot, limit) {
  const lim = Number.isFinite(limit) ? limit : 40;
  const a = mirrorEntryMap(installedRoot, 'installed');
  const b = mirrorEntryMap(mirrorRoot, 'mirror');
  const onlyInstalled = [];
  const onlyMirror = [];
  const differing = [];
  for (const k of Array.from(a.map.keys()).sort()) {
    if (!b.map.has(k)) onlyInstalled.push(k);
    else if (b.map.get(k) !== a.map.get(k)) differing.push(k);
  }
  for (const k of Array.from(b.map.keys()).sort()) {
    if (!a.map.has(k)) onlyMirror.push(k);
  }
  const cap = function (arr) {
    return arr.length > lim ? arr.slice(0, lim).concat(['...(共 ' + arr.length + ' 项)']) : arr;
  };
  return {
    installedCount: a.map.size, mirrorCount: b.map.size,
    installedBytes: a.bytes, mirrorBytes: b.bytes,
    onlyInstalled: cap(onlyInstalled), onlyMirror: cap(onlyMirror), differing: cap(differing),
    onlyInstalledCount: onlyInstalled.length, onlyMirrorCount: onlyMirror.length,
    differingCount: differing.length,
  };
}

//: 清单范围内文件的最新 mtime（毫秒）。空树 -> 0。
function newestMtimeMs(root, kind) {
  let newest = 0;
  for (const pair of MIRROR_TREES) {
    const rel = kind === 'mirror' ? pair[1] : pair[0];
    const full = path.join(root, rel);
    let st = null;
    try {
      st = fs.statSync(full);
    } catch (e) {
      st = null;
    }
    if (!st) continue;
    if (!st.isDirectory()) {
      newest = Math.max(newest, st.mtimeMs || 0);
      continue;
    }
    const walk = function (cur) {
      let items;
      try {
        items = fs.readdirSync(cur, { withFileTypes: true });
      } catch (e) {
        return;
      }
      for (const it of items) {
        const fp = path.join(cur, it.name);
        if (it.isDirectory()) {
          if (MIRROR_SKIP_DIRS.has(it.name)) continue;
          walk(fp);
          continue;
        }
        if (!it.isFile() || mirrorSkipFile(it.name)) continue;
        try {
          newest = Math.max(newest, fs.statSync(fp).mtimeMs || 0);
        } catch (e) { /* 单个文件 stat 失败不影响整体 */ }
      }
    };
    walk(full);
  }
  return newest;
}

//: 上次播种的时间基准（.mirror-seed.json）。
//: ⚠️ 为什么必须有它：播种本身会把镜像文件 mtime 刷成「现在」，直接拿
//:   「镜像 mtime vs 安装区 mtime」比会让镜像永远显得更新、永远不重播种
//:   （新包永不生效）。改用「播种完成时刻」作基准，才能区分
//:   「播种带来的 mtime」与「播种之后被外部改动」。
const MIRROR_SEED_STATE_FILE = '.mirror-seed.json';
//: 判定「播种之后又被改过」的时间容差：播种完成时刻写基准，被复制文件的 mtime
//: 必然 <= 完成时刻，留 60s 容忍时钟/IO 抖动。
const MIRROR_MTIME_TOLERANCE_MS = 60 * 1000;

function readMirrorSeedState(mirror) {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(mirror, MIRROR_SEED_STATE_FILE), 'utf8'));
    return (raw && typeof raw === 'object') ? raw : {};
  } catch (e) {
    return {};
  }
}

function writeMirrorSeedState(mirror, obj) {
  try {
    fs.mkdirSync(mirror, { recursive: true });
    fs.writeFileSync(path.join(mirror, MIRROR_SEED_STATE_FILE),
      JSON.stringify(obj, null, 2), 'utf8');
  } catch (e) {
    console.warn('[更新] 写入镜像播种基准失败（忽略）：', e && e.message);
  }
}

//: 覆盖方向策略：auto（默认，按证据判断）/ mirror（永远保留镜像）/ installed（永远以安装区为准）。
//: 环境变量 MJSCXT_MIRROR_POLICY 可在不重新打包的前提下改向 —— 排障出口。
function mirrorPolicy() {
  const v = String(process.env.MJSCXT_MIRROR_POLICY || '').trim().toLowerCase();
  return (v === 'mirror' || v === 'installed') ? v : 'auto';
}

//: 播种决策（**纯函数**）：运行时与 CLI 自检共用，保证「测的就是跑的」。
//: 返回 {seed, action, reason, note}，action: 'seed' | 'keep' | 'none'。
function decideMirrorSeed(o) {
  const same = !!(o.installedFp && o.mirrorFp && o.installedFp === o.mirrorFp);
  if (!o.mirrorExists) {
    return { seed: true, action: 'seed', reason: 'mirror-missing',
      note: '镜像不存在 → 首次播种（安装区为准）' };
  }
  if (o.policy === 'installed') {
    return { seed: true, action: 'seed', reason: 'policy-installed',
      note: 'MJSCXT_MIRROR_POLICY=installed → 安装区为准' };
  }
  if (o.policy === 'mirror') {
    return { seed: false, action: 'keep', reason: 'policy-mirror',
      note: 'MJSCXT_MIRROR_POLICY=mirror → 镜像为准，不覆盖' };
  }
  if (o.contentMarker === MIRROR_CONTENT_REMOTE) {
    return { seed: false, action: 'keep', reason: 'remote-content',
      note: '镜像来自远端资源增量 → 以镜像为准，不用安装区覆盖' };
  }
  if (same) {
    return { seed: false, action: 'none', reason: 'in-sync',
      note: '安装区与镜像内容一致，无需动作' };
  }
  // ⭐ 核心防线：镜像在上次播种**之后**被外部改过（典型：工作区/会话把新代码同步进
  //   镜像）→ 绝不用安装区覆盖它。这正是 t7 实测的「静默回退生产代码」。
  const seededAtMs = Number(o.seededAtMs || 0);
  const mirrorNewestMs = Number(o.mirrorNewestMs || 0);
  const installedNewestMs = Number(o.installedNewestMs || 0);
  // 基准优先「上次播种完成时刻」（.mirror-seed.json，最可靠）；存量用户首次跑本版本
  // 时没有基准，退化为「安装区内容最新 mtime」—— 镜像比安装区还新同样视为外部更新。
  const baseMs = seededAtMs > 0 ? seededAtMs : installedNewestMs;
  if (baseMs > 0 && mirrorNewestMs > baseMs + MIRROR_MTIME_TOLERANCE_MS) {
    return { seed: false, action: 'keep', reason: 'mirror-touched-after-seed',
      note: '镜像比上次播种基准更新（镜像最新 mtime ' + new Date(mirrorNewestMs).toISOString() +
        ' > 基准 ' + new Date(baseMs).toISOString() +
        (seededAtMs > 0 ? '（播种完成时刻）' : '（存量镜像无播种基准，改用安装区最新 mtime）') +
        '）→ 保留镜像，不用安装区覆盖；如确需以安装区为准请设 MJSCXT_MIRROR_POLICY=installed' };
  }
  if (String(o.installedVersion || '') !== String(o.mirrorVersion || '')) {
    return { seed: true, action: 'seed', reason: 'version-changed',
      note: '安装区版本 ' + (o.mirrorVersion || '(无)') + ' -> ' + (o.installedVersion || '(无)') +
        '（整包更新）→ 安装区为准' };
  }
  return { seed: true, action: 'seed', reason: 'content-changed',
    note: '同版本但内容指纹不一致（安装区 ' + (o.installedFp || '(空)') +
      ' / 镜像 ' + (o.mirrorFp || '(空)') + '）→ 安装区为准' };
}

//: 覆盖前把「将被安装区覆盖/删除」的镜像文件**备份**到 <mirror>/.mirror-overwritten-<ts>/。
//: 目的：即便判定为「安装区为准」，被覆盖的内容也能一键找回 —— 彻底消除
//: 「静默且不可逆的回退」。备份有上限（超出只留清单与日志）。
function mirrorToInstalledName(name) {
  for (const pair of MIRROR_TREES) {
    if (name === pair[0]) return pair[1];
    if (name.indexOf(pair[0] + '/') === 0) return pair[1] + name.slice(pair[0].length);
  }
  return name;
}

function backupMirrorDiff(installedRoot, mirrorRoot, limitFiles, limitBytes) {
  const lf = Number.isFinite(limitFiles) ? limitFiles : 2000;
  const lb = Number.isFinite(limitBytes) ? limitBytes : 64 * 1024 * 1024;
  const a = mirrorEntryMap(installedRoot, 'installed');
  const b = mirrorEntryMap(mirrorRoot, 'mirror');
  const victims = [];
  let bytes = 0;
  let skipped = 0;
  for (const kv of b.map.entries()) {
    const name = kv[0];
    if (a.map.has(name) && a.map.get(name) === kv[1]) continue; // 与安装区一致 → 不涉及
    const relMirror = mirrorToInstalledName(name);
    const full = path.join(mirrorRoot, relMirror);
    let sz = 0;
    try {
      sz = fs.statSync(full).size;
    } catch (e) {
      sz = 0;
    }
    if (victims.length >= lf || bytes + sz > lb) {
      skipped += 1;
      continue;
    }
    victims.push(relMirror);
    bytes += sz;
  }
  if (!victims.length) return { dir: '', files: 0, bytes: 0, skipped: skipped };
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const dir = path.join(mirrorRoot, '.mirror-overwritten-' + stamp);
  let copied = 0;
  for (const rel of victims) {
    try {
      const dst = path.join(dir, rel);
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(path.join(mirrorRoot, rel), dst);
      copied += 1;
    } catch (e) { /* 单文件失败不影响其余 */ }
  }
  return { dir: dir, files: copied, bytes: bytes, skipped: skipped };
}

//: 决策留痕（追加 JSONL，>512KB 时保留最后 500 行）——「绝不静默」的可审计证据。
function appendMirrorSeedLog(mirror, obj) {
  const line = JSON.stringify(Object.assign({ ts: new Date().toISOString() }, obj));
  try {
    fs.mkdirSync(mirror, { recursive: true });
    const p = path.join(mirror, '.mirror-seed.log');
    fs.appendFileSync(p, line + '\n');
    try {
      if (fs.statSync(p).size > 512 * 1024) {
        const tail = fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).slice(-500);
        fs.writeFileSync(p, tail.join('\n') + '\n', 'utf8');
      }
    } catch (e) { /* 轮转失败不影响启动 */ }
  } catch (e) { /* 日志失败绝不影响启动 */ }
  return line;
}

//: 执行「安装区 -> 镜像」的重播种（**运行时与 CLI 共用**，保证测的就是跑的）：
//: 备份镜像差异文件 -> 按 MIRROR_TREES 复制 -> 写版本/内容标记与播种基准 -> 复核指纹。
//: 返回 {decision, installedFp, mirrorFp, backup, copied, afterFp, verifyOk}。
//: ⚠️ 决策为 keep/none 时**不做任何写入**（绝不覆盖镜像）。
function applyMirrorSeed(installedRoot, mirrorRoot, opts) {
  const o = opts || {};
  const policy = o.policy || mirrorPolicy();
  const mirrorExists = fs.existsSync(path.join(mirrorRoot, 'app'));
  const installedFp = computeMirrorFingerprint(installedRoot, 'installed');
  const mirrorFp = mirrorExists ? computeMirrorFingerprint(mirrorRoot, 'mirror') : '';
  const contentMarker = mirrorExists ? readMirrorContent(mirrorRoot) : '';
  const seedState = mirrorExists ? readMirrorSeedState(mirrorRoot) : {};
  const mirrorVer = mirrorExists ? readMirrorVersion(mirrorRoot) : '';
  const mirrorNewestMs = mirrorExists ? newestMtimeMs(mirrorRoot, 'mirror') : 0;
  const installedNewestMs = newestMtimeMs(installedRoot, 'installed');
  const decision = decideMirrorSeed({
    policy: policy,
    mirrorExists: mirrorExists,
    installedFp: installedFp,
    mirrorFp: mirrorFp,
    contentMarker: contentMarker,
    installedVersion: o.installedVersion || '',
    mirrorVersion: mirrorVer,
    seededAtMs: seedState.seededAtMs || 0,
    mirrorNewestMs: mirrorNewestMs,
    installedNewestMs: installedNewestMs,
  });
  const out = {
    decision: decision, installedFp: installedFp, mirrorFp: mirrorFp,
    contentMarker: contentMarker, mirrorVersion: mirrorVer, policy: policy,
    seededAtMs: seedState.seededAtMs || 0, mirrorNewestMs: mirrorNewestMs,
    installedNewestMs: installedNewestMs, backup: null, copied: [],
    afterFp: '', verifyOk: true,
  };
  if (decision.action !== 'seed') {
    // ⭐ keep = 「保护镜像、拒绝被安装区覆盖」的关键动作，必须留痕（绝不静默）；
    //    none = 两侧一致，不必每次启动都写日志（否则日志天天刷）。
    if (decision.action === 'keep') {
      appendMirrorSeedLog(mirrorRoot, {
        action: 'keep', reason: decision.reason, note: decision.note,
        installedFp: installedFp, mirrorFp: mirrorFp, contentMarker: contentMarker,
        installedVersion: o.installedVersion || '', mirrorVersion: mirrorVer, policy: policy,
        seededAtMs: seedState.seededAtMs || 0,
        mirrorNewestMs: mirrorNewestMs, installedNewestMs: installedNewestMs,
      });
    }
    return out;
  }

  appendMirrorSeedLog(mirrorRoot, {
    action: 'seed', reason: decision.reason, note: decision.note,
    installedFp: installedFp, mirrorFp: mirrorFp, contentMarker: contentMarker,
    installedVersion: o.installedVersion || '', mirrorVersion: mirrorVer, policy: policy,
    seededAtMs: seedState.seededAtMs || 0,
    diff: mirrorExists ? diffMirrorTrees(installedRoot, mirrorRoot) : null,
  });

  // ⭐ 覆盖前先备份「会被覆盖/删除的镜像文件」：即使判断失误也能一键找回，
  //   不存在「静默且不可逆的回退」；备份目录写进日志。
  if (mirrorExists) {
    try {
      out.backup = backupMirrorDiff(installedRoot, mirrorRoot);
    } catch (e) {
      out.backup = null;
      console.warn('[更新] 覆盖前备份镜像差异失败（继续播种）：', e && e.message);
    }
    if (out.backup && out.backup.files) {
      appendMirrorSeedLog(mirrorRoot, {
        action: 'backup-before-seed', reason: decision.reason,
        backupDir: out.backup.dir, backupFiles: out.backup.files,
        backupBytes: out.backup.bytes, backupSkipped: out.backup.skipped,
      });
    }
  }

  // 先删后拷：cpSync 是覆盖式合并，不先删的话「上游已删除的文件」会在镜像里复活
  fs.mkdirSync(mirrorRoot, { recursive: true });
  for (const pair of MIRROR_TREES) {
    const srcName = pair[0];
    const dstName = pair[1];
    const src = path.join(installedRoot, srcName);
    const dst = path.join(mirrorRoot, dstName);
    if (!fs.existsSync(src)) continue;
    try {
      if (fs.existsSync(dst)) {
        if (fs.statSync(dst).isDirectory()) fs.rmSync(dst, { recursive: true, force: true });
        else fs.rmSync(dst, { force: true });
      }
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      if (fs.statSync(src).isDirectory()) fs.cpSync(src, dst, { recursive: true });
      else fs.copyFileSync(src, dst);
      out.copied.push(srcName);
    } catch (e) {
      console.error('[更新] 播种资源镜像失败 ' + srcName + ': ', e && e.message);
    }
  }

  out.afterFp = computeMirrorFingerprint(mirrorRoot, 'mirror');
  if (o.installedVersion) writeMirrorVersion(mirrorRoot, o.installedVersion);
  writeMirrorContent(mirrorRoot, installedFp);
  // 播种基准 = **完成时刻**：此后镜像若被改动（mtime 更新），下次启动判为「外部更新」
  // 并保留镜像，绝不再用安装区覆盖。
  writeMirrorSeedState(mirrorRoot, {
    seededAtMs: Date.now(), installedFp: installedFp,
    installedVersion: o.installedVersion || '', installedRoot: installedRoot, policy: policy,
    note: '播种基准：镜像在此时间之后被改动则不再被安装区覆盖（MJSCXT_MIRROR_POLICY 可强制改向）',
  });
  out.verifyOk = out.afterFp === installedFp;
  if (!out.verifyOk) {
    console.error('[更新] 播种后镜像指纹仍与安装区不一致（' + out.afterFp + ' != ' +
      installedFp + '）—— 请检查 MIRROR_TREES 清单与镜像目录权限');
    appendMirrorSeedLog(mirrorRoot, {
      action: 'verify-failed', reason: 'post-seed-fingerprint-mismatch',
      installedFp: installedFp, mirrorFp: out.afterFp,
    });
  }
  return out;
}

function readMirrorContent(mirror) {
  try {
    return String(fs.readFileSync(path.join(mirror, MIRROR_CONTENT_FILE), 'utf8') || '').trim();
  } catch {
    return '';
  }
}

function writeMirrorContent(mirror, fp) {
  try {
    fs.writeFileSync(path.join(mirror, MIRROR_CONTENT_FILE), String(fp || ''), 'utf8');
  } catch (e) {
    console.warn('[更新] 写入镜像内容标记失败（忽略）：', e && e.message);
  }
}


function readMirrorVersion(mirror) {
  try {
    return String(fs.readFileSync(path.join(mirror, MIRROR_VERSION_FILE), 'utf8') || '').trim();
  } catch {
    return '';
  }
}

function writeMirrorVersion(mirror, ver) {
  try {
    fs.writeFileSync(path.join(mirror, MIRROR_VERSION_FILE), String(ver || ''), 'utf8');
  } catch (e) {
    console.warn('[更新] 写入镜像版本标记失败（忽略）：', e && e.message);
  }
}

function seedResourceMirror() {
  if (!app.isPackaged) return null; // 开发态直接用项目根，无镜像
  const res = process.resourcesPath;
  const mirror = resourceMirrorDir();
  const r = applyMirrorSeed(res, mirror, {
    installedVersion: updateConfig.currentVersion(),
    policy: mirrorPolicy(),
  });
  const d = r.decision;
  if (d.action === 'none') return mirror; // 两侧一致：不写日志、不做任何 IO
  const head = '[更新] 镜像播种决策：' + d.action + ' / ' + d.reason + ' —— ' + d.note;
  if (d.action === 'keep') {
    console.warn(head);
    console.warn('[更新] 已保留镜像内容（未用安装区覆盖）。镜像目录：' + mirror);
    bootLog('mirror-keep ' + d.reason + ' ' + d.note);
    return mirror;
  }
  console.log(head);
  if (r.backup && r.backup.files) {
    console.warn('[更新] 已备份将被覆盖的镜像文件 ' + r.backup.files + ' 个（' +
      Math.round(r.backup.bytes / 1024) + ' KB）-> ' + r.backup.dir +
      (r.backup.skipped ? ('；' + r.backup.skipped + ' 个超出备份上限，仅记录在日志') : ''));
  }
  if (!r.verifyOk) {
    console.error('[更新] 播种后镜像指纹与安装区仍不一致（' + r.afterFp + ' != ' +
      r.installedFp + '），请检查 MIRROR_TREES 清单与镜像目录权限');
  }
  bootLog('mirror-seed ' + d.reason + ' ' + d.note);
  return mirror;
}


// 更新编排结果（交给 UI 展示 / IPC 回传）
async function checkForUpdatesNow(manual) {
  const current = updateConfig.currentVersion();
  let info;
  try {
    info = await updater.checkForUpdates(current);
  } catch (e) {
    const msg = `检查更新失败：${e.message}`;
    console.warn(msg);
    if (manual) await dialog.showMessageBox(undefined, {
      type: 'warning', title: '检查更新', message: msg, buttons: ['好'],
    });
    return { checked: true, update: false, error: msg };
  }
  if (!info.update) {
    if (manual) await dialog.showMessageBox(undefined, {
      type: 'info', title: '检查更新',
      message: `当前已是最新版本（${info.current}）`,
      detail: `GitHub 最新版本：${info.latest}`, buttons: ['好'],
    });
    return { checked: true, ...info };
  }
  // 有更新：询问用户走哪种
  const opts = {
    type: 'info', title: '发现新版本',
    message: `新版本 ${info.latest}（当前 ${info.current}）`,
    detail: [
      '· 资源增量更新：仅更新工作流/前端/后端代码（不含 Python 运行时），体积小，保留你的密钥与数据。',
      '· 整包更新：下载完整 Portable 应用（含 Python 运行时），体积约 200MB+。',
      '',
      '两种都会自动 SHA256 校验。资源增量需重启后端生效。',
    ].join('\n'),
    buttons: ['资源增量', '整包', '暂不更新'], defaultId: 0,
  };
  const { response } = await dialog.showMessageBox(undefined, opts);
  if (response === 0) return runResourceUpdate(info, manual);
  if (response === 1) return runFullUpdate(info, manual);
  return { checked: true, ...info, update: false, skipped: true };
}

// 资源增量：下载 zip + SHA256SUMS 校验 → 覆盖镜像 → 重启后端
async function runResourceUpdate(info, manual) {
  const mirror = resourceMirrorDir();
  const destDir = path.join(app.getPath('userData'), 'update-cache');
  let shaSums = '';
  try {
    const rel = await updater.fetchLatestRelease();
    const sumAsset = (rel.assets || []).find((a) => a.name === updateConfig.ASSET.sha256sums);
    if (sumAsset) shaSums = await (await fetch(sumAsset.url, { headers: { 'User-Agent': 'mjscxt' } })).text();
    // 审计 P2-24：清单拉取失败/缺失时 shaSums 为空 → downloadAndVerify 会
    // **拒绝更新**（fail-closed），不再静默跳过校验继续解包。
  } catch { /* 错误由 downloadAndVerify 的 fail-closed 统一处理 */ }
  try {
    await updater.downloadAndUnpackResources(info.latest, info.assets, shaSums, destDir, mirror,
      (f) => { if (manual) console.log(`资源增量下载 ${(f * 100).toFixed(0)}%`); });
    // ⭐ 2026-10-05：把镜像版本标记成**资源版本**。否则下次启动时
    //   seedResourceMirror 会发现「安装区版本(旧) != 镜像版本」并重新播种安装区旧代码，
    //   把刚更新好的资源增量**回滚掉**（用户表现为「更新成功但一重启就回到老样子」）。
    writeMirrorVersion(mirror, info.latest);
    // ⭐ 2026-10-07：写入 'remote' 哨兵 —— 资源增量来自远端 zip，与本地安装区内容
    //   必然不同；标记成「镜像为准」后，同版本内容检测会跳过，**不会**用安装区
    //   旧代码把刚装好的增量回滚掉（该类回滚事故见本函数开头的注释）。
    writeMirrorContent(mirror, MIRROR_CONTENT_REMOTE);
    console.log('[更新] 资源增量已覆盖镜像（版本标记 ' + info.latest + '），重启后端生效');
    const status = await stopBackend().then(startBackend).then(() => backendStatus());
    await dialog.showMessageBox(undefined, {
      type: 'info', title: '更新完成',
      message: `资源已更新到 ${info.latest}，后端已重启。`,
      detail: status && status.reused ? '（端口复用了已有实例）' : '',
      buttons: ['好'],
    });
    return { checked: true, ...info, applied: 'resource' };
  } catch (e) {
    await dialog.showMessageBox(undefined, {
      type: 'error', title: '资源增量更新失败',
      message: e.message, buttons: ['好'],
    });
    return { checked: true, ...info, applied: 'resource', error: e.message };
  }
}

// 整包：下载 Portable exe + 校验 → 用户确认后才 spawn 接管替换 → 本进程退出
async function runFullUpdate(info, manual) {
  const destDir = path.join(app.getPath('userData'), 'update-cache');
  let shaSums = '';
  try {
    const rel = await updater.fetchLatestRelease();
    const sumAsset = (rel.assets || []).find((a) => a.name === updateConfig.ASSET.sha256sums);
    if (sumAsset) shaSums = await (await fetch(sumAsset.url, { headers: { 'User-Agent': 'mjscxt' } })).text();
    // 审计 P2-24：同资源增量 —— 清单为空时 applyFullPortable 会拒绝下载（fail-closed）
  } catch { /* 错误由 downloadAndVerify 的 fail-closed 统一处理 */ }
  try {
    // 只下载 + 校验，先不 spawn；把接管时机留给用户确认后
    const { localPath } = await updater.applyFullPortable(info.latest, info.assets, shaSums, destDir,
      (f) => { if (manual) console.log(`整包下载 ${(f * 100).toFixed(0)}%`); },
      false);
    const { response } = await dialog.showMessageBox(undefined, {
      type: 'info', title: '整包已下载',
      message: `新版本 ${info.latest} 已下载并通过校验。`,
      detail: `临时文件：${localPath}\n选择「立即更新」将启动它完成替换并重启应用；选「稍后」则保持现状。`,
      buttons: ['立即更新', '稍后'],
    });
    if (response !== 0) {
      // 稍后：不 spawn、不退出；bootstrapper 已下载好，下次可再次触发
      return { checked: true, ...info, applied: 'full', deferred: true };
    }
    await stopBackend();
    updater.spawnPortable(localPath); // bootstrapper detached 接管
    app.quit();
    return { checked: true, ...info, applied: 'full' };
  } catch (e) {
    await dialog.showMessageBox(undefined, {
      type: 'error', title: '整包更新失败', message: e.message, buttons: ['好'],
    });
    return { checked: true, ...info, applied: 'full', error: e.message };
  }
}

// 自动检查：启动一次 + 每 24h；静默（manual=false 不弹「无更新」框）
let autoCheckTimer = null;
function scheduleAutoChecks() {
  checkForUpdatesNow(false);
  if (autoCheckTimer) clearInterval(autoCheckTimer);
  autoCheckTimer = setInterval(() => { checkForUpdatesNow(false); }, UPDATE_CHECK_INTERVAL_MS);
  autoCheckTimer.unref();
}

// ---------------------------------------------------------------------------
// 后端生命周期
// ---------------------------------------------------------------------------

const backend = {
  child: null,      // 我们 spawn 的 python 子进程；复用实例时为 null
  reused: false,    // 是否复用了 5000 上已有的 Flask（例如浏览器版已在跑）
  port: null,
  starting: false,
  shuttingDown: false,
};

function childAlive() {
  return backend.child != null && !backend.child.killed;
}

// 就绪探测：轮询 GET / 直到 200 或超时
function waitForReady(port) {
  const deadline = Date.now() + READINESS_TIMEOUT_MS;
  return new Promise((resolve) => {
    const tick = async () => {
      if (backend.shuttingDown || !childAlive()) {
        resolve(false);
        return;
      }
      const status = await httpGetStatus(`http://127.0.0.1:${port}/`);
      if (status === 200) {
        resolve(true);
        return;
      }
      if (Date.now() >= deadline) {
        resolve(false);
        return;
      }
      setTimeout(tick, READINESS_INTERVAL_MS);
    };
    tick();
  });
}

// spawn 绝对路径的 serve.py（项目目录含中文，相对路径有 CWD 编码歧义风险）
async function startBackend() {
  if (backend.starting) return backend;
  if (childAlive()) return backend;
  backend.starting = true;
  backend.reused = false;
  try {
    const layout = resolveBackendLayout();
    const warnings = app.isPackaged
      ? []
      : validateProjectRoot(config.projectRoot);
    for (const w of warnings) console.warn(w);

    // 打包态用自带的嵌入式 Python；开发态探测本机 venv
    const python = layout.pythonExe || await detectPython();
    if (!fs.existsSync(layout.servePy)) {
      throw new Error(`找不到后端入口 ${layout.servePy}（请确认已运行 build:win 打包或项目路径正确）`);
    }
    const { port, reuse } = await resolvePort(layout.dataDir);
    // ⭐ 单实例：复用已在运行的、同一数据根的后端 —— 不起新进程，直接连它
    if (reuse) {
      logLines.length = 0;
      backend.port = port;
      backend.reused = true;
      backend.child = null;
      backend.starting = false;
      pushLog('out', '[single-instance] reused existing backend on port ' + port + ' (same data root)');
      return backend;
    }
    logLines.length = 0;
    backend.port = port;
    const childEnv = {
      ...process.env,
      APP_HOST: '127.0.0.1',
      APP_PORT: String(port),
      PYTHONUNBUFFERED: '1',
      // ⭐⭐⭐ 2026-10-12 修复「桌面版无法在中文目录运行」：
      //   Windows 上 Python 的 stdout/stderr 默认跟随**系统 locale 编码**
      //   （简体中文 Windows = cp936/GBK）。安装目录、数据目录或日志内容一旦含中文，
      //   任何一次 print/logging 就会抛 UnicodeEncodeError，后端进程随即死掉 ——
      //   表现就是「装在中文路径下就打不开/闪退」。
      //   下面两项分别关闭这层不确定性：
      //     PYTHONUTF8=1        → 开启 Python UTF-8 模式（PEP 540），
      //                           文件系统编码、stdio 编码全部按 UTF-8 处理；
      //     PYTHONIOENCODING    → 显式钉死 stdio 编码与错误策略，防止被系统 locale 覆盖。
      //   两者叠加后，路径含中文（甚至路径里有生僻字/emoji）都不会再影响启动。
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8:replace',
      // 不手动转发 COMFYUI_* / MJSCXT_* —— 应用自带 env_loader 会读 .env
    };
    // 可写数据目录：打包态指向 userData（Program Files 只读），开发态不设（源根可写）
    if (layout.dataDir) {
      fs.mkdirSync(layout.dataDir, { recursive: true });
      childEnv.MJSCXT_DATA_DIR = layout.dataDir;
    }
    backend.child = spawn(python, [layout.servePy], {
      cwd: layout.cwd,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: childEnv,
      windowsHide: true,
    });
    backend.child.stdout.on('data', (d) => pushLog('out', d));
    backend.child.stderr.on('data', (d) => pushLog('err', d));
    backend.child.on('error', (e) => pushLog('err', String(e)));
    backend.child.on('exit', (code, signal) => {
      pushLog('sys', `python 进程退出（code=${code}, signal=${signal}）`);
      backend.child = null;
      if (!backend.shuttingDown) {
        backend.starting = false;
        // 只有「serve.py 真正退出」才提示用户；Flask 内部自动重启发生在
        // serve.py 进程内部，子进程本身不会退出，因此不会误报。
        notifyBackendExited(code, signal);
      }
    });
    backend.starting = false;
    const ready = await waitForReady(port);
    if (!ready) {
      // 超时：杀掉这次 spawn 的子进程，把日志尾部交给用户（重试/查看日志）
      if (childAlive()) {
        backend.shuttingDown = true;
        backend.child.kill('SIGINT');
        await new Promise((r) => setTimeout(r, 1500));
        backend.shuttingDown = false;
        forceKillIfAlive();
      }
      await notifyReadinessTimeout(port);
      return backend;
    }
    return backend;
  } catch (e) {
    backend.starting = false;
    pushLog('sys', String(e && e.message ? e.message : e));
    await notifyBackendExited('detect', e.message);
    return backend;
  }
}

async function stopBackend() {
  backend.shuttingDown = true;
  if (backend.reused) {
    // 复用实例不是我们 spawn 的，不杀别人的进程
    backend.reused = false;
    backend.port = null;
    backend.shuttingDown = false;
    return;
  }
  if (childAlive()) {
    const pid = backend.child.pid;
    backend.child.kill('SIGINT'); // 先给 serve.py 优雅停机钩子机会
    const exited = await new Promise((resolve) => {
      const c = backend.child;
      if (c == null) return resolve(true);
      const t = setTimeout(() => resolve(false), GRACEFUL_WAIT_MS);
      c.on('exit', () => {
        clearTimeout(t);
        resolve(true);
      });
    });
    if (!exited) {
      // Windows 非交互场景 SIGINT 可能送不达 → taskkill 兜底（进程树）
      forceKillIfAlive(pid);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
  backend.child = null;
  backend.reused = false;
  backend.port = null;
  backend.shuttingDown = false;
}

function restartBackend() {
  // stop 后重新 spawn（复用模式则重新走端口探测）
  return stopBackend().then(startBackend);
}

// 兜底：taskkill /PID <pid> /T /F
function forceKillIfAlive(pid) {
  const c = backend.child;
  if (c == null) return;
  const p = pid != null ? pid : c.pid;
  if (p == null) return;
  try {
    spawn('taskkill', ['/PID', String(p), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
    });
    pushLog('sys', `SIGINT 未能在 ${GRACEFUL_WAIT_MS / 1000}s 内结束，使用 taskkill /PID ${p} /T /F 兜底`);
  } catch (e) {
    pushLog('err', `taskkill 失败：${e}`);
  }
}

function backendStatus() {
  return {
    running: childAlive() || backend.reused,
    reused: backend.reused,
    port: backend.port,
    pid: childAlive() ? backend.child.pid : null,
    starting: backend.starting,
  };
}

// ---------------------------------------------------------------------------
// 用户提示（日志尾部 + 重试/查看日志）
// ---------------------------------------------------------------------------

function notifyBackendExited(code, signal) {
  const win = BrowserWindow.getAllWindows()[0];
  const detail = tailLog(30).join('\n');
  dialog
    .showMessageBox(win || undefined, {
      type: 'error',
      noLink: true,
      title: '后端已退出',
      message: `后端进程意外退出（code=${code}, signal=${signal}）`,
      detail,
      buttons: ['重试', '查看日志', '关闭'],
    })
    .then(({ response }) => {
      if (response === 0) startBackend();
      else if (response === 1) dialog.showMessageBox(win || undefined, {
        type: 'none',
        title: '后端日志（尾部）',
        message: tailLog(100).join('\n'),
        buttons: ['好'],
      });
    });
}

function notifyReadinessTimeout(port) {
  const win = BrowserWindow.getAllWindows()[0];
  const detail = tailLog(30).join('\n');
  dialog
    .showMessageBox(win || undefined, {
      type: 'error',
      noLink: true,
      title: '后端启动超时',
      message: `等待 http://127.0.0.1:${port}/ 就绪超时（60s），后端日志尾部如下`,
      detail,
      buttons: ['重试', '查看日志', '关闭'],
    })
    .then(({ response }) => {
      if (response === 0) startBackend();
      else if (response === 1) dialog.showMessageBox(win || undefined, {
        type: 'none',
        title: '后端日志（尾部）',
        message: tailLog(100).join('\n'),
        buttons: ['好'],
      });
    });
}

// ---------------------------------------------------------------------------
// IPC（preload 暴露的最小面）
// ---------------------------------------------------------------------------

function registerIpc() {
  ipcMain.handle('backend:start', () => startBackend().then(backendStatus));
  ipcMain.handle('backend:stop', () => stopBackend().then(backendStatus));
  ipcMain.handle('backend:restart', () =>
    restartBackend().then(() => backendStatus())
  );
  ipcMain.handle('backend:log', (_e, n = 100) => tailLog(Math.max(1, Number(n) || 100)));
  ipcMain.handle('backend:status', () => backendStatus());

  // 更新系统：纯数据返回（不弹窗），由 UI 决定如何展示；菜单项仍走 checkForUpdatesNow(true)
  ipcMain.handle('updater:check', async () => {
    const current = updateConfig.currentVersion();
    try {
      return { ok: true, ...(await updater.checkForUpdates(current)) };
    } catch (e) {
      return { ok: false, error: String(e.message || e) };
    }
  });
  // ⭐ 桌面版手动更新（2026-10-04）：manual=true —— 主进程内的进度日志打到 ring buffer，
  // 资源增量完成后重启后端，前端再整页重载拉新资源（UpdateChecker 已处理）。
  ipcMain.handle('updater:applyResource', async () => {
    const current = updateConfig.currentVersion();
    const info = await updater.checkForUpdates(current);
    if (!info.update) return { ok: false, error: '已是最新版本' };
    const r = await runResourceUpdate(info, true);
    return { ok: true, ...r };
  });
  ipcMain.handle('updater:applyFull', async () => {
    const current = updateConfig.currentVersion();
    const info = await updater.checkForUpdates(current);
    if (!info.update) return { ok: false, error: '已是最新版本' };
    const r = await runFullUpdate(info, true);
    // 整包「稍后」分支：runFullUpdate 返回 deferred=true，前端据此恢复选择态
    return { ok: true, ...r };
  });
  ipcMain.handle('config:get', () => ({ ...config, configPath: configPath() }));
  const applyProjectRoot = async (root) => {
    root = String(root || '').trim();
    if (!root) throw new Error('项目根目录不能为空');
    const warnings = validateProjectRoot(root); // 无效则抛错，preload 侧会显示
    config.projectRoot = root;
    saveConfig(config);
    return { ok: true, warnings, config: { ...config } };
  };
  ipcMain.handle('config:setProjectRoot', (_e, root) => applyProjectRoot(root));
  ipcMain.handle('config:chooseProjectRoot', async () => {
    const win = BrowserWindow.getAllWindows()[0];
    const r = await dialog.showOpenDialog(win, {
      title: '选择项目根目录（应包含 app/serve.py）',
      defaultPath: config.projectRoot,
      properties: ['openDirectory'],
    });
    if (r.canceled || r.filePaths.length === 0) return { ok: false };
    try {
      // 审计 P2（2026-09-29）：旧实现误用 ipcMain.handle 当「调用」——handle 是
      // 注册接口，listener 传 null 直接 TypeError，导致「选择目录」流程恒失败。
      // 现在直接调用与 setProjectRoot 同一个应用函数。
      const res = await applyProjectRoot(r.filePaths[0]);
      return { ok: true, ...res };
    } catch (e) {
      dialog.showErrorBox('项目根目录无效', String(e.message || e));
      return { ok: false, error: String(e.message || e) };
    }
  });
  // 原生窗口按钮（最小化/最大化/关闭）的配色跟随前端主题。
  // titleBarOverlay 由主进程绘制，前端切浅色主题时不同步就会出现
  // 「深色按钮压在浅色页面上」的割裂感 —— 桌面软件不该有这种破绽。
  ipcMain.on('window:titlebar-theme', (e, payload) => {
    const win = BrowserWindow.fromWebContents(e.sender);
    if (!win || typeof win.setTitleBarOverlay !== 'function') return;
    const isLight = payload && payload.theme === 'light';
    try {
      win.setTitleBarOverlay({
        color: isLight ? '#f5f7fb' : TITLEBAR_COLOR,
        symbolColor: isLight ? '#334155' : TITLEBAR_SYMBOL,
        height: TITLEBAR_HEIGHT,
      });
    } catch { /* 该平台不支持覆盖层时静默忽略 */ }
  });

  ipcMain.handle('shell:openExternal', (_e, url) => {
    if (String(url).startsWith('http')) shell.openExternal(url);
  });
}

// ---------------------------------------------------------------------------
// 窗口
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// 窗口外观：像「桌面软件」而不是「一个网页窗口」
// ---------------------------------------------------------------------------
// 依据（用户反复反馈「桌面版看起来就是浏览器」）：
//   1. 系统标题栏 + 常显菜单栏 = 最像浏览器的两处；
//   2. 启动瞬间的白底闪烁 = 典型的网页观感；
//   3. 没有应用图标，任务栏里认不出来；
//   4. 每次打开都是默认大小位置，不像装好的软件。
// 这里逐条处理：隐藏系统标题栏（保留原生最小化/最大化/关闭的覆盖层）+
// 菜单栏改为 Alt 唤出 + 深色底先铺 + 应用图标 + 窗口尺寸位置记忆。
const APP_ICON = path.join(__dirname, 'build', 'icon.ico');
const TITLEBAR_HEIGHT = 44;          // 与前端 .electron-titlebar 的高度保持一致
const TITLEBAR_COLOR = '#0b0f19';    // 深色底：和前端 bg-canvas 同色系，避免接缝
const TITLEBAR_SYMBOL = '#c7d2e0';   // 最小化/最大化/关闭 的图标色（浅色，深底可见）

function windowStateFile() {
  return path.join(app.getPath('userData'), 'window-state.json');
}

function loadWindowState() {
  try {
    const s = JSON.parse(fs.readFileSync(windowStateFile(), 'utf8'));
    if (s && Number.isFinite(s.width) && Number.isFinite(s.height)) return s;
  } catch { /* 首次运行或文件损坏 → 用默认值 */ }
  return null;
}

function saveWindowState(win) {
  try {
    // getNormalBounds：最大化状态下取「还原后」的尺寸，否则记下来的是全屏大小
    const b = typeof win.getNormalBounds === 'function' ? win.getNormalBounds() : win.getBounds();
    fs.writeFileSync(windowStateFile(),
      JSON.stringify({ ...b, maximized: win.isMaximized() }), 'utf8');
  } catch { /* 落盘失败不影响使用 */ }
}

function createWindow() {
  const saved = loadWindowState();
  const win = new BrowserWindow({
    width: saved ? saved.width : 1440,
    height: saved ? saved.height : 900,
    x: saved ? saved.x : undefined,
    y: saved ? saved.y : undefined,
    minWidth: 1120,
    minHeight: 700,
    title: '漫剧工坊',
    icon: fs.existsSync(APP_ICON) ? APP_ICON : undefined,
    backgroundColor: TITLEBAR_COLOR,   // 先铺深色，消除启动白闪
    show: false,                        // 等 ready-to-show 再显示，避免先白后黑
    autoHideMenuBar: true,              // 菜单栏收起，按 Alt 唤出（内容更满、更像应用）
    titleBarStyle: 'hidden',            // 去掉系统标题栏
    titleBarOverlay: {                  // 但保留原生窗口按钮的覆盖层（自绘按钮易做错）
      color: TITLEBAR_COLOR,
      symbolColor: TITLEBAR_SYMBOL,
      height: TITLEBAR_HEIGHT,
    },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  if (saved && saved.maximized) win.maximize();
  win.once('ready-to-show', () => {
    bootLog('window ready-to-show -> show');
    win.show();
  });
  // 兜底：若 15s 内 ready-to-show 仍未触发（例如后端 5000 迟迟未就绪导致页面
  // 渲染不出来），强制显示窗口，避免"主进程活着但窗口永远藏在后台"的静默卡死。
  // 无论卡在哪一步，用户都能看到窗口（哪怕里面是"后端未就绪"），且日志可查。
  setTimeout(() => {
    if (!win.isDestroyed() && !win.isVisible()) {
      bootLog('window show fallback: ready-to-show 未按时到来，强制 show');
      win.show();
    }
  }, 15000);
  win.on('resize', () => saveWindowState(win));
  win.on('move', () => saveWindowState(win));
  win.on('close', () => saveWindowState(win));
  const port = backend.port || DEFAULT_PORT;
  win.webContents.on('did-fail-load', (e, code, desc, url) => {
    bootLog(`window did-fail-load code=${code} desc=${desc} url=${url}`);
  });
  win.loadURL(`http://127.0.0.1:${port}/`);
  return win;
}

function buildMenu() {
  const template = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    {
      label: '后端',
      submenu: [
        { label: '重启后端', click: () => restartBackend() },
        { label: '查看日志', click: async () => {
          const lines = tailLog(100).join('\n');
          const win = BrowserWindow.getAllWindows()[0];
          dialog.showMessageBox(win, {
            type: 'none',
            title: '后端日志（尾部 100 行）',
            message: lines || '（暂无日志）',
            buttons: ['好'],
          });
        } },
        { type: 'separator' },
        { label: '停止后端', click: () => stopBackend() },
      ],
    },
    { role: 'fileMenu' },
    { role: 'editMenu' },
    { role: 'viewMenu' },
    { role: 'windowMenu' },
    {
      label: '帮助',
      submenu: [
        { label: '检查更新', click: () => checkForUpdatesNow(true) },
        { type: 'separator' },
        { label: '关于漫剧工坊', click: () => {
          const win = BrowserWindow.getAllWindows()[0];
          dialog.showMessageBox(win || undefined, {
            type: 'info', title: '关于',
            message: '漫剧工坊',
            detail: `版本 ${updateConfig.currentVersion()}（xianjing2000/comic-drama-forge）`,
            buttons: ['好'],
          });
        } },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}


// ===========================================================================
// CLI 自检实现（t15）—— node electron-app/main.js --mirror-* 系列
// ===========================================================================
// 部署 / CI / 排障：不启动 Electron 直接验证镜像指纹与播种决策。
// 退出码：0 = 成功 / 两侧一致 / 自检通过；1 = 不一致 / 自检失败；2 = 参数错误。
function mirrorCliUsage() {
  return [
    '用法：',
    '  node electron-app/main.js --mirror-fingerprint <root> [--mirror-side] [--json]',
    '  node electron-app/main.js --mirror-compare <installedRoot> <mirrorRoot> [--json]',
    '  node electron-app/main.js --mirror-decide <installedRoot> <mirrorRoot> [选项]',
    '      --policy auto|mirror|installed   覆盖方向（默认 auto）',
    '      --seeded-at <ms>                 上次播种基准（默认读镜像的 .mirror-seed.json）',
    '      --installed-version <v> --mirror-version <v>',
    '      --marker <v>                     .mirror-content 的值（remote = 远端增量）',
    '      --fail-on-seed                   决策为 seed 时退出码 1（CI 拦「即将覆盖」）',
    '      --json',
    '  node electron-app/main.js --mirror-seed <installedRoot> <mirrorRoot> [选项]',
    '      （真正执行重播种：备份差异 -> 按清单复制 -> 写标记与基准 -> 复核指纹）',
    '  node electron-app/main.js --mirror-selftest [--json]',
    '退出码：0 = 成功/一致/通过；1 = 不一致/自检失败；2 = 参数错误',
  ].join('\n');
}

function mirrorCliFail(msg) {
  console.error('[mirror-cli] ' + msg + '\n' + mirrorCliUsage());
  return 2;
}

function mirrorCliMain(cmd, argv) {
  const flags = new Set(argv.filter(function (a) { return a.indexOf('--') === 0; }));
  const args = argv.filter(function (a) { return a.indexOf('--') !== 0; });
  const asJson = flags.has('--json');
  const val = function (name, dflt) {
    const i = argv.indexOf(name);
    return (i >= 0 && i + 1 < argv.length) ? argv[i + 1] : dflt;
  };

  if (cmd === 'fingerprint') {
    const root = args[0];
    if (!root) return mirrorCliFail('缺少目录参数');
    const kind = flags.has('--mirror-side') ? 'mirror' : 'installed';
    const em = mirrorEntryMap(root, kind);
    const fp = computeMirrorFingerprint(root, kind);
    if (asJson) {
      console.log(JSON.stringify({ root: root, kind: kind, fingerprint: fp,
        files: em.map.size, bytes: em.bytes, trees: em.trees }, null, 2));
    } else {
      console.log('目录   : ' + root);
      console.log('视角   : ' + kind + (kind === 'mirror' ? '（镜像侧：app -> backend 映射）' : '（安装区侧）'));
      console.log('指纹   : ' + fp);
      console.log('文件数 : ' + em.map.size);
      console.log('总字节 : ' + em.bytes);
    }
    return 0;
  }

  if (cmd === 'compare') {
    const a = args[0];
    const b = args[1];
    if (!a || !b) return mirrorCliFail('需要「安装区目录」与「镜像目录」两个参数');
    const fpA = computeMirrorFingerprint(a, 'installed');
    const fpB = computeMirrorFingerprint(b, 'mirror');
    const diff = diffMirrorTrees(a, b);
    const same = !!fpA && fpA === fpB;
    if (asJson) {
      console.log(JSON.stringify({ installed: { root: a, fingerprint: fpA },
        mirror: { root: b, fingerprint: fpB }, same: same, diff: diff }, null, 2));
    } else {
      console.log('安装区 : ' + a + '  ' + fpA);
      console.log('镜像   : ' + b + '  ' + fpB);
      console.log('一致   : ' + (same ? '是' : '否'));
      if (!same) {
        console.log('仅在安装区 (' + diff.onlyInstalledCount + ') : ' + diff.onlyInstalled.join(', '));
        console.log('仅在镜像   (' + diff.onlyMirrorCount + ') : ' + diff.onlyMirror.join(', '));
        console.log('内容不同   (' + diff.differingCount + ') : ' + diff.differing.join(', '));
      }
    }
    return same ? 0 : 1;
  }

  if (cmd === 'decide') {
    const a = args[0];
    const b = args[1];
    if (!a || !b) return mirrorCliFail('需要「安装区目录」与「镜像目录」两个参数');
    const mirrorExists = fs.existsSync(path.join(b, 'app'));
    const seedState = mirrorExists ? readMirrorSeedState(b) : {};
    const seededAtMs = Number(val('--seeded-at', seedState.seededAtMs || 0)) || 0;
    const installedFp = computeMirrorFingerprint(a, 'installed');
    const mirrorFp = mirrorExists ? computeMirrorFingerprint(b, 'mirror') : '';
    const marker = String(val('--marker', mirrorExists ? readMirrorContent(b) : '') || '');
    const decision = decideMirrorSeed({
      policy: String(val('--policy', mirrorPolicy()) || 'auto').toLowerCase(),
      mirrorExists: mirrorExists,
      installedFp: installedFp,
      mirrorFp: mirrorFp,
      contentMarker: marker,
      installedVersion: String(val('--installed-version', '') || ''),
      mirrorVersion: String(val('--mirror-version', mirrorExists ? readMirrorVersion(b) : '') || ''),
      seededAtMs: seededAtMs,
      mirrorNewestMs: mirrorExists ? newestMtimeMs(b, 'mirror') : 0,
      installedNewestMs: newestMtimeMs(a, 'installed'),
    });
    if (asJson) {
      console.log(JSON.stringify({ decision: decision, installedFp: installedFp,
        mirrorFp: mirrorFp, mirrorExists: mirrorExists, seededAtMs: seededAtMs,
        mirrorNewestMs: mirrorExists ? newestMtimeMs(b, 'mirror') : 0,
        contentMarker: marker }, null, 2));
    } else {
      console.log('安装区指纹 : ' + installedFp);
      console.log('镜像指纹   : ' + (mirrorFp || '(无镜像)'));
      console.log('播种基准   : ' + (seededAtMs ? new Date(seededAtMs).toISOString() : '(无)'));
      console.log('决策       : ' + decision.action + '（' + decision.reason + '）');
      console.log('说明       : ' + decision.note);
    }
    if (flags.has('--fail-on-seed') && decision.action === 'seed') return 1;
    return 0;
  }

  if (cmd === 'seed') {
    const a = args[0];
    const b = args[1];
    if (!a || !b) return mirrorCliFail('需要「安装区目录」与「镜像目录」两个参数');
    const r = applyMirrorSeed(a, b, {
      installedVersion: String(val('--installed-version', '') || ''),
      policy: String(val('--policy', mirrorPolicy()) || 'auto').toLowerCase(),
    });
    if (asJson) {
      console.log(JSON.stringify(r, null, 2));
    } else {
      console.log('安装区指纹 : ' + r.installedFp);
      console.log('镜像指纹   : ' + (r.mirrorFp || '(无镜像)'));
      console.log('决策       : ' + r.decision.action + '（' + r.decision.reason + '）');
      console.log('说明       : ' + r.decision.note);
      if (r.decision.action === 'seed') {
        console.log('已复制     : ' + r.copied.join(', '));
        console.log('覆盖前备份 : ' + (r.backup && r.backup.files
          ? (r.backup.files + ' 个文件 -> ' + r.backup.dir) : '无（无差异或首次播种）'));
        console.log('播种后指纹 : ' + r.afterFp + '  与安装区一致：' + (r.verifyOk ? '是' : '否'));
      } else {
        console.log('（未写入任何内容：镜像被保留）');
      }
    }
    if (r.decision.action === 'seed' && !r.verifyOk) return 1;
    return 0;
  }
  if (cmd === 'selftest') return mirrorCliSelftest(asJson);
  return mirrorCliFail('未知命令：' + cmd);
}

//: 自检：在临时目录里复现 t7 的两个缺陷场景，验证新策略（**不碰真实环境**）。
function mirrorCliSelftest(asJson) {
  const osMod = require('node:os');
  const results = [];
  const ok = function (name, pass, extra) {
    results.push({ name: name, ok: !!pass, extra: extra || '' });
  };
  const tmp = fs.mkdtempSync(path.join(osMod.tmpdir(), 'mjscxt-mirror-selftest-'));
  const installedRoot = path.join(tmp, 'resources');
  const mirrorRoot = path.join(tmp, 'resource-mirror');
  const writeInstalled = function (rel, body) {
    const p = path.join(installedRoot, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, body, 'utf8');
  };
  const reseedMirror = function () {
    fs.rmSync(mirrorRoot, { recursive: true, force: true });
    fs.mkdirSync(mirrorRoot, { recursive: true });
    fs.cpSync(path.join(installedRoot, 'backend'), path.join(mirrorRoot, 'app'), { recursive: true });
    if (fs.existsSync(path.join(installedRoot, 'workflows'))) {
      fs.cpSync(path.join(installedRoot, 'workflows'), path.join(mirrorRoot, 'workflows'), { recursive: true });
    }
    if (fs.existsSync(path.join(installedRoot, 'locales'))) {
      fs.cpSync(path.join(installedRoot, 'locales'), path.join(mirrorRoot, 'locales'), { recursive: true });
    }
    if (fs.existsSync(path.join(installedRoot, '.env'))) {
      fs.copyFileSync(path.join(installedRoot, '.env'), path.join(mirrorRoot, '.env'));
    }
  };
  const decideHere = function (extra) {
    const o = Object.assign({
      policy: 'auto',
      mirrorExists: fs.existsSync(path.join(mirrorRoot, 'app')),
      installedFp: computeMirrorFingerprint(installedRoot, 'installed'),
      mirrorFp: computeMirrorFingerprint(mirrorRoot, 'mirror'),
      contentMarker: '',
      installedVersion: '1.0.0',
      mirrorVersion: '1.0.0',
      seededAtMs: 0,
      mirrorNewestMs: newestMtimeMs(mirrorRoot, 'mirror'),
      installedNewestMs: newestMtimeMs(installedRoot, 'installed'),
    }, extra || {});
    return decideMirrorSeed(o);
  };

  try {
    writeInstalled(path.join('backend', 'app.py'), 'print("a")\n');
    writeInstalled(path.join('backend', 'serve.py'), 'print("s")\n');
    writeInstalled(path.join('backend', 'routes', 'agent.py'), 'AGENT = 1\n');
    writeInstalled(path.join('backend', 'workers', 'episodes.py'), 'W = 2\n');
    writeInstalled(path.join('backend', 'static', 'assets', 'index-abc.js'), 'console.log(1)\n');
    writeInstalled(path.join('workflows', 'qwen.json'), '{"nodes": []}\n');
    writeInstalled(path.join('locales', 'zh.json'), '{"a": "x"}\n');
    writeInstalled('.env', 'MJSCXT=1\n');
    writeInstalled(path.join('backend', 'app.py.bak_20260101'), 'bak\n');
    writeInstalled(path.join('backend', '__pycache__', 'app.cpython-311.pyc'), 'pyc\n');

    const keys = Array.from(mirrorEntryMap(installedRoot, 'installed').map.keys());

    // ① 清单覆盖面：旧实现只哈希 6 个 backend/*.py；多级子目录与其它树现在必须都在
    ok('指纹覆盖 backend 多级子目录（routes/ workers/ static/）',
      keys.indexOf('backend/routes/agent.py') >= 0 &&
      keys.indexOf('backend/workers/episodes.py') >= 0 &&
      keys.indexOf('backend/static/assets/index-abc.js') >= 0, keys.length + ' 个条目');
    ok('指纹覆盖 workflows / locales / .env',
      keys.indexOf('workflows/qwen.json') >= 0 &&
      keys.indexOf('locales/zh.json') >= 0 && keys.indexOf('.env') >= 0);

    // ② 敏感性（t7 缺陷①）：改一个**旧指纹完全没覆盖**的文件 → 指纹必须变
    const fpBefore = computeMirrorFingerprint(installedRoot, 'installed');
    writeInstalled(path.join('backend', 'routes', 'agent.py'), 'AGENT = 999\n');
    const fpAfter = computeMirrorFingerprint(installedRoot, 'installed');
    ok('改 backend/routes/agent.py（旧指纹未覆盖）→ 指纹变化', fpBefore !== fpAfter,
      fpBefore + ' -> ' + fpAfter);

    // ③ 快照/编译产物不参与（与 check_app_sync 口径一致）
    ok('快照与 __pycache__ 不参与指纹',
      !keys.some(function (k) { return k.indexOf('.bak_') >= 0 || k.indexOf('__pycache__') >= 0; }));

    // ④ 一致 → 不动作
    reseedMirror();
    // 模拟「上次播种发生在 10 分钟前」：这样随后对镜像的修改才落在基准之后
    const seededAt1 = Date.now() - 10 * 60 * 1000;
    const d0 = decideHere({ seededAtMs: seededAt1 });
    ok('安装区与镜像一致 → none', d0.action === 'none', d0.reason);

    // ⑤ 核心场景（t7 缺陷②）：镜像被外部同步更新、安装区仍是旧的 → 必须 keep
    fs.writeFileSync(path.join(mirrorRoot, 'app', 'routes', 'agent.py'),
      'AGENT = "NEW-FROM-WORKSPACE"\n', 'utf8');
    const d1 = decideHere({ seededAtMs: seededAt1 });
    ok('镜像在上次播种后被更新 → keep（绝不被安装区覆盖）',
      d1.action === 'keep' && d1.reason === 'mirror-touched-after-seed', d1.reason);

    // ⑥ 只有安装区变化（重新打包）→ seed
    reseedMirror();
    const seededAt2 = Date.now();
    writeInstalled(path.join('backend', 'app.py'), 'print("rebuilt")\n');
    const d2 = decideHere({ seededAtMs: seededAt2 });
    ok('只有安装区变化（重新打包）→ seed', d2.action === 'seed', d2.action + '/' + d2.reason);

    // ⑦ 远端资源增量 → 永不覆盖
    const d3 = decideHere({ seededAtMs: seededAt2, contentMarker: 'remote' });
    ok('contentMarker=remote → keep', d3.action === 'keep' && d3.reason === 'remote-content', d3.reason);

    // ⑧ 显式策略出口
    ok('MJSCXT_MIRROR_POLICY=installed → seed',
      decideHere({ policy: 'installed' }).action === 'seed');
    ok('MJSCXT_MIRROR_POLICY=mirror → keep',
      decideHere({ policy: 'mirror' }).action === 'keep');

    // ⑨ 存量用户（没有播种基准）→ 仍按版本/内容判断（不能因为缺基准就永不播种）
    const d4 = decideHere({ seededAtMs: 0, installedFp: 'aaa', mirrorFp: 'bbb' });
    ok('无播种基准（存量用户）→ seed', d4.action === 'seed', d4.reason);

    // ⑨b 存量用户（无播种基准）且镜像比安装区新 → 同样必须 keep
    const d4b = decideHere({
      seededAtMs: 0, installedFp: 'aaa', mirrorFp: 'bbb',
      mirrorNewestMs: Date.now(), installedNewestMs: Date.now() - 3600 * 1000,
    });
    ok('无播种基准但镜像明显比安装区新 → keep',
      d4b.action === 'keep' && d4b.reason === 'mirror-touched-after-seed', d4b.reason);

    // ⑩ 整包换版（版本变化，镜像未被外部改）→ seed
    const d5 = decideHere({ seededAtMs: seededAt2, installedVersion: '2.0.0', mirrorVersion: '1.0.0' });
    ok('整包换版 → seed（安装区为准）', d5.action === 'seed' && d5.reason === 'version-changed', d5.reason);
  } finally {
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch (e) { /* 清理失败不影响结论 */ }
  }

  const failed = results.filter(function (r) { return !r.ok; });
  if (asJson) {
    console.log(JSON.stringify({ ok: failed.length === 0, checks: results }, null, 2));
  } else {
    for (const r of results) {
      console.log((r.ok ? '  [PASS] ' : '  [FAIL] ') + r.name + (r.extra ? '   （' + r.extra + '）' : ''));
    }
    console.log('镜像策略自检：' + (results.length - failed.length) + '/' + results.length +
      ' 通过' + (failed.length ? '（失败 ' + failed.length + ' 项）' : ''));
  }
  return failed.length === 0 ? 0 : 1;
}

// CLI 自检出口：命中 --mirror-* 时只跑上面的逻辑并退出，**不会启动 Electron**
// （下面的 App 生命周期代码不会被执行）。
if (MIRROR_CLI_CMD) {
  let cliCode = 0;
  try {
    cliCode = mirrorCliMain(MIRROR_CLI_CMD, MIRROR_CLI_ARGV.slice(1));
  } catch (e) {
    console.error('[mirror-cli] 执行失败：' + ((e && e.stack) || e));
    cliCode = 2;
  }
  process.exit(typeof cliCode === 'number' ? cliCode : 0);
}

// ---------------------------------------------------------------------------
// App 生命周期
// ---------------------------------------------------------------------------

bootLog(`boot pid=${process.pid} isPackaged=${app.isPackaged} resourcesPath=${process.resourcesPath} execPath=${process.execPath}`);
const gotLock = app.requestSingleInstanceLock();
bootLog(`singleInstanceLock=${gotLock}`);
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const wins = BrowserWindow.getAllWindows();
    if (wins.length > 0) {
      if (wins[0].isMinimized()) wins[0].restore();
      wins[0].focus();
    }
  });

  app.whenReady().then(async () => {
    // 先播种资源镜像（打包态），后端布局才会指到可写镜像而非只读安装区
    seedResourceMirror();
    registerIpc();
    buildMenu();
    // 后端与窗口并行：先起窗口（指向端口），后端就绪后 reload，避免首屏白屏死等
    const win = createWindow();
    startBackend().then((b) => {
      if (b.reused || b.port) {
        win.loadURL(`http://127.0.0.1:${b.port}/`);
      }
    });
    // 启动检查一次 + 每 24h 后台轮询（静默，不弹「无更新」框）
    scheduleAutoChecks();
  });

  // 退出前：SIGINT → 等 10s → taskkill 兜底（仅我们 spawn 的子进程）
  app.on('before-quit', (event) => {
    if (!childAlive()) return; // 复用模式或已退出：无需处理
    event.preventDefault();
    backend.shuttingDown = true;
    const child = backend.child;
    const pid = child ? child.pid : null;
    if (child) child.kill('SIGINT');
    setTimeout(() => {
      forceKillIfAlive(pid);
      backend.child = null;
      app.quit(); // 二次 before-quit 时 childAlive() 为 false，直接放行
    }, GRACEFUL_WAIT_MS);
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}
