'use strict';
// 漫剧工坊 桌面版 — Electron 主进程
// 职责：管理本地 Flask 后端（serve.py）的生命周期，并把 BrowserWindow 指向运行中的
//       Flask URL（http://127.0.0.1:<port>）。UI 的唯一事实源仍是 Flask，不复制 app/static。

// ⭐ 2026-10-06：Electron/Chromium 静默崩溃诊断
// 打包版是 GUI 子系统程序，stdout/stderr 被系统丢弃，whenReady 之前的任何异常
// 表现就是「双击闪退、无日志」（用户实测 10/6 多次遇到）。这里在**任何 Electron
// 模块加载之前**（本文件顶部、require 之前）设好环境变量：
//   · ELECTRON_ENABLE_LOGGING=1        → Chromium 把 bootstrap 阶段日志写到 stderr
//   · ELECTRON_ENABLE_STACK_DUMPING=1  → 崩溃时生成 .dmp 到 userData/Crashpad
// 同时再写一个独立的「超早期」日志：连 Electron 启动器本身都崩掉时，desktop.log
// 不会有任何 bootLog 行，但下面这行会在进程一启动就立刻落盘，至少能区分
// 「进程根本没起」vs「起了但 main.js 没加载到」。
if (process.platform === 'win32') {
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

const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  shell,
} = require('electron');
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
async function resolvePort() {
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

//: 参与指纹的关键文件（相对安装区 resourcesPath）。后端正文 + 前端产物入口。
//: 只哈希这几个文件（合计约 2MB），启动开销可忽略。
const MIRROR_FINGERPRINT_FILES = [
  'backend/app.py',
  'backend/comfyui_client.py',
  'backend/style_kit.py',
  'backend/prompt_qc.py',
  'backend/serve.py',
  'backend/config.py',
];

function computeMirrorFingerprint(res) {
  try {
    const crypto = require('crypto');
    const parts = [];
    for (const rel of MIRROR_FINGERPRINT_FILES) {
      try {
        const p = path.join(res, rel);
        const buf = fs.readFileSync(p);
        parts.push(rel + ':' + buf.length + ':' +
          crypto.createHash('sha1').update(buf).digest('hex').slice(0, 12));
      } catch {
        parts.push(rel + ':missing');
      }
    }
    // 前端产物入口（vite 产物名自带内容哈希，重建即变 → 前端改动同样能被发现）
    try {
      const dir = path.join(res, 'backend', 'static', 'assets');
      const names = fs.readdirSync(dir).filter((n) => /^index-.*\.(js|css)$/.test(n)).sort();
      parts.push('static:' + names.join(','));
    } catch {
      parts.push('static:missing');
    }
    // ⭐ 2026-10-07：工作流模板纳入指纹。此前只算 backend/*.py —— 只改模板
    //   （例如把「提示词增强节点」固化进 Qwen21 模板）而没碰 .py 时，镜像内容
    //   指纹不变 → 永不重播种 → 用户看到的工作流仍然是旧的（节点不出现）。
    //   这里把 workflows/ 下所有 .json 的名称+大小+内容哈希一起算进去。
    try {
      const wfDir = path.join(res, 'workflows');
      const names = fs.readdirSync(wfDir).filter((n) => n.toLowerCase().endsWith('.json')).sort();
      for (const n of names) {
        try {
          const buf = fs.readFileSync(path.join(wfDir, n));
          parts.push('wf/' + n + ':' + buf.length + ':' +
            crypto.createHash('sha1').update(buf).digest('hex').slice(0, 12));
        } catch {
          parts.push('wf/' + n + ':unreadable');
        }
      }
    } catch {
      parts.push('wf:missing');
    }
    return crypto.createHash('sha1').update(parts.join('|')).digest('hex').slice(0, 16);
  } catch (e) {
    return '';
  }
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
  const installed = updateConfig.currentVersion();
  const mirrorVer = readMirrorVersion(mirror);
  const mirrorExists = fs.existsSync(path.join(mirror, 'app'));
  // 需要（重新）播种的两种情况：镜像还没有；或安装区版本与镜像版本不一致
  // （整包更新替换了安装区 → 安装区为准，必须覆盖旧镜像）。
  const needSeedByVersion = !mirrorExists || mirrorVer !== installed;
  // 同版本内容漂移检测：版本号没变但安装区代码/前端产物变了（重建覆盖安装目录）。
  // ⚠️ 指纹**缺失**（''）同样判定为漂移并重播种：升级到带本机制的版本时，
  //    存量用户的镜像可能正是「同版本的老代码」（本次事故形态），
  //    若把「缺失」当成「已同步」跳过，本机制对存量用户永远不触发 = 修复不生效。
  //    重播种的来源就是安装区本身，代价是一次拷贝，无数据风险。
  // ⚠️ 标记值 'remote' = 「镜像来自远端资源增量，以镜像为准」，**绝不**据此重播种
  //    （否则增量更新会被本地安装区旧代码回滚，见 runResourceUpdate）。
  let needSeedByContent = false;
  if (!needSeedByVersion && mirrorExists) {
    const fpNow = computeMirrorFingerprint(res);
    const fpMirror = readMirrorContent(mirror);
    if (fpMirror !== MIRROR_CONTENT_REMOTE && fpNow && fpNow !== fpMirror) {
      needSeedByContent = true;
      console.log(`[更新] 同版本内容已变化/未记录（${fpMirror || '(无标记)'} -> ${fpNow}），重新播种资源镜像`);
    }
  }
  const needSeed = needSeedByVersion || needSeedByContent;
  // 安装区目录名 → 镜像目录名：后端源码在安装区叫 backend，镜像内仍叫 app
  // （后端代码里的相对引用不变，只有安装区这层名字避让 .asar / app 冲突）。
  // ⚠️ '.env' 是**文件**（安全子集配置，见 pack_backend.js）：播种时要区分文件/目录，
  //    否则 rmSync/cpSync 的 recursive 语义会按目录处理而抛错。
  const subs = [['backend', 'app'], ['workflows', 'workflows'], ['locales', 'locales'],
                ['.env', '.env']];
  if (needSeed) {
    console.log(`[更新] 资源镜像需(重新)播种：镜像版本=${mirrorVer || '(无)'} 安装区版本=${installed}`);
  }
  for (const [srcName, dstName] of subs) {
    const src = path.join(res, srcName);
    const dst = path.join(mirror, dstName);
    if (!fs.existsSync(src)) continue;
    if (!fs.existsSync(dst) || needSeed) {
      try {
        // 重新播种前清掉旧目标：cpSync 是覆盖式合并，若不先删，**已被上游删除的文件**
        // 会残留在镜像里继续被后端读到（旧代码复活）。
        if (needSeed && fs.existsSync(dst)) {
          if (fs.statSync(dst).isDirectory()) fs.rmSync(dst, { recursive: true, force: true });
          else fs.rmSync(dst, { force: true });
        }
        fs.mkdirSync(path.dirname(dst), { recursive: true });
        if (fs.statSync(src).isDirectory()) fs.cpSync(src, dst, { recursive: true });
        else fs.copyFileSync(src, dst);
        console.log(`[更新] 播种资源镜像 ${srcName} -> ${dst}`);
      } catch (e) {
        console.error(`[更新] 播种资源镜像失败 ${srcName}: `, e && e.message);
      }
    }
  }
  if (needSeed) writeMirrorVersion(mirror, installed);
  if (needSeed) writeMirrorContent(mirror, computeMirrorFingerprint(res));
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
    const { port } = await resolvePort();
    logLines.length = 0;
    backend.port = port;
    const childEnv = {
      ...process.env,
      APP_HOST: '127.0.0.1',
      APP_PORT: String(port),
      PYTHONUNBUFFERED: '1',
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
