# -*- mode: python ; coding: utf-8 -*-
"""漫剧工坊 - PyInstaller打包配置"""
import os
import sys
import pkgutil as _pkgutil
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# 隐藏导入（2026-09-29 修复 exe 启动崩溃：No module named 'providers'）
#
# 背景：frozen PYZ 里 app/*.py 以**裸名**登记（import serve / import export_manager …，
# 由 main.py 顶部 sys.path.insert(0, .../app) 在构建期被 modulegraph 模拟追踪而成）。
# 但 providers 是**包**：原 spec 的 collect_submodules('app') 执行 app/providers/__init__.py 时，
# 其内部 from env_loader import env（裸名）在 spec 求值环境解析失败 → PyInstaller 打 WARNING
# 后**静默跳过整个 providers 子包** → PYZ 缺 providers → frozen 启动在 app.py 第 86 行
# import providers 即崩（dist/logs/serve_stdout.log 有完整 traceback）。
#
# 修法：spec 求值阶段把 <root>/app 放进 sys.path（仅影响本进程；PyInstaller 构建期 modulegraph
# 有自己的路径模拟，不受污染），逐个 import 验证关键模块确实可解析，再显式写进 hidden_imports。
_ROOT = os.path.dirname(os.path.abspath(SPEC))
_APP_DIR = os.path.join(_ROOT, 'app')
sys.path.insert(0, _APP_DIR)
for _probe in ('serve', 'app', 'providers', 'env_loader', 'fs_atomic'):
    import importlib as _il
    _il.import_module(_probe)  # 任一失败 → spec 阶段直接崩，fail fast（不产生半残 exe）
hidden_imports = [
    # 显式：providers 子包（裸名）+ 运行时被裸 import 的关键模块
    'providers', 'providers.base', 'providers.cloud', 'providers.local',
    'env_loader', 'fs_atomic',
    # 全量：app/ 下所有顶层 .py / 包目录（裸名），与构建期 modulegraph 的模拟路径一致
]
for _m in _pkgutil.iter_modules([_APP_DIR]):
    hidden_imports.append(_m.name)

# 收集所有数据文件
datas = [
    ('app/templates', 'app/templates'),
    ('app/static', 'app/static'),
    ('locales', 'locales'),
    # ⚠️ 2026-09-28 补：工作流模板必须随 exe 分发。PROJECT_WORKFLOWS_DIR 在
    # frozen 模式下 = _MEIPASS/workflows（env_loader 注释：只读资源留 PROJECT_ROOT_DIR），
    # 漏掉这里会让单文件包用户「模板缺失」——克隆/Docker 有 workflows/ 但 exe 没有。
    ('workflows', 'workflows'),
    # ⚠️ 2026-09-29 移除 ('output', 'output')：output/ 是**纯写入目标**
    # （QC_DIR / tasks.db / PROJECTS_DIR 全在 PROJECT_OUTPUT_DIR 下），启动不读它，
    # 打包进 exe 只会让单文件包膨胀 3GB+ 且在 _MEIPASS 里变只读。运行期由
    # main.py 的 output_dir.mkdir(exist_ok=True) 自建（配合 MJSCXT_DATA_DIR 重定向）。
]

# 排除不必要的模块
excludes = [
    'matplotlib', 'scipy', 'pandas', 'jupyter', 
    'tkinter', 'IPython', 'notebook',
]

a = Analysis(
    ['main.py'],
    pathex=[_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='漫剧工坊',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 2026-09-30: upx 压缩 bootloader 后 frozen 启动报 "Could not create temporary directory"
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 调试模式显示控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='x86_64',
    codesign_identity=None,
    entitlements_file=None,
)
