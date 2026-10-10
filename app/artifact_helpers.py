# -*- coding: utf-8 -*-
'''质检清理与产物回收助手（2026-10-11 从 app.py 下沉，助手域第二批）。'''

# 本批按**依赖闭包**整体搬迁（13 个对象）：
#   8 个函数：_purge_* (4) + _reject_artifact + _maybe_reclaim_comfyui_output
#             + _comfyui_official_dirs + _mark_history_file_purged
#   4 个常量：_PURGE_REJECTED_ENV、_COMFYUI_RECLAIM_LAST_TS/INTERVAL_SEC/LOCK
# 闭包实测自洽：app 只被用于 app.logger（已改为模块 logger），
#   4 个常量只被批内函数使用，因此搬迁后不会出现「两份绑定」。
# 函数体与下沉前逐字一致（唯一替换：app.logger -> logger）。
import datetime
import json
import logging
import os
import shutil
import threading
import time

logger = logging.getLogger(__name__)

_PURGE_REJECTED_ENV = "MJSCXT_PURGE_REJECTED"
_COMFYUI_RECLAIM_LAST_TS = 0.0          # 上次真正扫描的时间戳（模块级节流状态）
_COMFYUI_RECLAIM_INTERVAL_SEC = 600.0   # 同一进程 10 分钟内只真正扫描一次
_COMFYUI_RECLAIM_LOCK = threading.Lock()


def _purge_rejected_enabled() -> bool:
    """不合格产物清理开关。默认开启；设 `MJSCXT_PURGE_REJECTED=0` 时只记日志不移走。"""
    v = str(os.environ.get(_PURGE_REJECTED_ENV, "1")).strip().lower()
    return v not in ("0", "false", "no", "off", "")


def _reject_artifact(paths, project: str = "", reason: str = "", kind: str = "") -> dict:
    """质检不合格产物 → 移入回收站（可恢复），**绝不硬删**。

    返回 ``{"moved": [...], "skipped": [...], "failed": [...]}``，三态都带
    `{"src":..., "why":...}`（moved 项另带 `dst`）。

    安全闸（任一不满足即跳过并记日志，绝不抛异常）：
      · 只接受绝对路径（相对路径无法可靠判边界，直接拒绝）；
      · `os.path.normpath(os.path.abspath(p))` 归一 —— 本项目已知坑：混合分隔符
        （`/` 与 `\\`）会让外部 API 静默匹配失败，必须先归一；
      · 必须落在 PROJECT_OUTPUT_DIR 或 COMFYUI_OUTPUT_DIR 之内（越界拒绝）；
      · 显式排除 PROJECT_TRASH_DIR（含 `_backup_*` / `_watermark_backup` 同理越界/排除）；
      · `os.path.lexists` 判存在 —— episode 成片 `move` 后 src 已消失是常态，
        不存在即跳过，**不能当异常**；
      · `os.stat().st_nlink == 1` 校验（硬链接不释放空间，见 disk_reclaim.py 的教训）；
      · 文件与目录都支持（目录走 shutil.move）。

    全程 try/except：清理是优化而非功能，**任何失败都不阻断主流程**，只 logger.warning。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    paths = [p for p in (paths or []) if p]
    if not paths:
        return res

    # 归一化的边界根（含尾分隔符，防止 /output 误匹配 /output2）
    def _root_ok(p: str) -> bool:
        cands = []
        for root in (PROJECT_OUTPUT_DIR, COMFYUI_OUTPUT_DIR):
            if root:
                try:
                    cands.append(os.path.normpath(os.path.abspath(root)) + os.sep)
                except Exception:  # noqa: BLE001
                    pass
        return any(p == c.rstrip(os.sep) or p.startswith(c) for c in cands)

    trash_abs = ""
    try:
        if PROJECT_TRASH_DIR:
            trash_abs = os.path.normpath(os.path.abspath(PROJECT_TRASH_DIR))
    except Exception:  # noqa: BLE001
        trash_abs = ""

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = os.path.join(PROJECT_TRASH_DIR, "qc_reject",
                              f"{stamp}_{_safe_project(project or 'project')}")
    enabled = _purge_rejected_enabled()

    for raw in paths:
        try:
            if not os.path.isabs(raw):
                res["skipped"].append({"src": str(raw), "why": "非绝对路径"})
                logger.warning(
                    "[质检清理] 跳过：非绝对路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, raw)
                continue
            p = os.path.normpath(os.path.abspath(raw))
            if trash_abs and (p == trash_abs or p.startswith(trash_abs + os.sep)):
                res["skipped"].append({"src": p, "why": "回收站内，跳过"})
                continue
            if not _root_ok(p):
                res["skipped"].append({"src": p, "why": "越界（不在 output/ComfyUI 目录内）"})
                logger.warning(
                    "[质检清理] 拒绝越界路径（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            if not os.path.lexists(p):
                res["skipped"].append({"src": p, "why": "不存在"})
                continue
            try:
                if os.stat(p).st_nlink > 1:
                    res["skipped"].append({"src": p, "why": "硬链接（不释放空间）"})
                    logger.warning(
                        "[质检清理] 跳过硬链接（project=%s kind=%s reason=%s path=%s）",
                        project, kind, reason, p)
                    continue
            except OSError as se:
                res["failed"].append({"src": p, "why": f"stat 失败：{se}"})
                continue
            if not enabled:
                res["skipped"].append({"src": p, "why": f"{_PURGE_REJECTED_ENV}=0（仅记日志）"})
                logger.warning(
                    "[质检清理] 开关关闭，仅记日志不移走（project=%s kind=%s reason=%s path=%s）",
                    project, kind, reason, p)
                continue
            os.makedirs(trash_root, exist_ok=True)
            base = os.path.basename(p.rstrip(os.sep)) or "artifact"
            dst = os.path.join(trash_root, base)
            # 同名冲突（同项目多次重试同名）→ 加序号，绝不覆盖已在回收站里的证据
            _n = 1
            while os.path.lexists(dst):
                stem, ext = os.path.splitext(base)
                dst = os.path.join(trash_root, f"{stem}__{_n}{ext}")
                _n += 1
            _move_with_retry(p, dst)
            res["moved"].append({"src": p, "dst": dst})
            logger.warning(
                "[质检清理] 不合格产物已移入回收站（project=%s kind=%s reason=%s）：%s → %s",
                project, kind, reason, p, dst)
        except Exception as e:  # noqa: BLE001  清理绝不能中断生产
            res["failed"].append({"src": str(raw), "why": f"{type(e).__name__}: {e}"})
            logger.warning("[质检清理] 移入回收站失败（已忽略，不影响主流程）：%s: %s",
                               type(e).__name__, e)
    return res


def _purge_rejected_artifacts(paths, project: str = "", reason: str = "", kind: str = "",
                              history_file: str = "") -> dict:
    """``_reject_artifact`` 的语义化包装：移走后顺手把「质检历史 file 断链」补掉。

    ⚠️ 质检历史 json 是排查依据，**只把断链的 `file` 字段置 null + 打 `purged` 标记**，
    绝不删除历史记录本身（否则事后无法查「当时为什么不合格」）。
    """
    out = _reject_artifact(paths, project=project, reason=reason, kind=kind)
    moved_srcs = {os.path.normpath(m["src"]) for m in out.get("moved") or []}
    if moved_srcs and history_file:
        try:
            _mark_history_file_purged(history_file, moved_srcs)
        except Exception as e:  # noqa: BLE001
            logger.warning("[质检清理] 质检历史 file 断链标记失败（忽略）：%s", e)
    return out


def _mark_history_file_purged(history_file: str, moved_srcs: set) -> None:
    """把质检历史里指向**已移走路径**的 `file` / `frames_f` 记为 null 并打 `purged=true`。

    历史记录本身保留 —— 它是事后排查「当时为什么不合格」的唯一依据，只是不再指向
    已不存在的本地文件（否则前端/排障脚本按路径取会 404）。
    """
    if not history_file or not os.path.isfile(history_file):
        return
    with open(history_file, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    hit = False
    for rec in (data.get("records") or []):
        if not isinstance(rec, dict):
            continue
        fp = rec.get("file")
        if fp and os.path.normpath(os.path.abspath(str(fp))) in moved_srcs:
            rec["file"] = None
            rec["purged"] = True
            hit = True
        # 抽帧图（整片质检）：整目录被移走 → 逐条把已消失的帧路径剔除
        frames = rec.get("frames_f")
        if isinstance(frames, list) and frames:
            kept = [x for x in frames
                    if os.path.normpath(os.path.abspath(str(x))) not in moved_srcs]
            if len(kept) != len(frames):
                rec["frames_f"] = kept
                rec["frames_purged"] = True
                hit = True
    if hit:
        atomic_write_json(history_file, data)
        logger.warning("[质检清理] 已标记质检历史断链：%s", history_file)


def _purge_prompt_records(project: str, shot_key, reason: str = "") -> dict:
    """提示词预检不通过 → 移走该镜**唯一落盘物** `prompt_<shot>.json`（P12）。

    用户决策 1：``prompt_qc.preflight`` 是生成前的文本合规检查，不通过直接阻断不生成，
    因此没有图片/视频可删 —— 只有这份提示词历史 json 留在了本地。
    """
    try:
        key = qc_client.safe_token(shot_key, "0")
        path = os.path.join(QC_DIR, _safe_project(project or "project"), f"prompt_{key}.json")
    except Exception as e:  # noqa: BLE001
        logger.warning("[质检清理] 提示词落盘物路径解析失败（忽略）：%s", e)
        return {"moved": [], "skipped": [], "failed": []}
    return _reject_artifact([path], project=project, reason=reason or "提示词预检未通过",
                            kind="prompt")


def _purge_sb_refs(project: str) -> dict:
    """分镜参考图上传残留（ComfyUI output/sb_ref_*.png）按项目清理（P13）。

    ⚠️ 与「不合格产物」是**两码事**：`sb_ref_*` 是分镜**参考图输入**（上传给
    LoadImageOutput 的中间件），不是质检产物，**绝不能混进普通「不合格即删」逻辑**
    （那会在单镜重试中途删掉当前镜头正在用的参考图）。按用户决策 4：只在**本轮分镜
    批量生成循环全部结束后**调用一次，按项目前缀收口。
    """
    res = {"moved": [], "skipped": [], "failed": []}
    try:
        root = COMFYUI_OUTPUT_DIR
        if not root or not os.path.isdir(root):
            return res
        # ⚠️ 2026-09-29 前置安全检查（实测根因，用户 09-29 日志）：
        # 「收尾调用一次」这个前提在**异常收尾**时不成立 —— 某镜 wait_for_completion
        # 超时返回、或批次被中止信号打断时，任务其实**仍在 ComfyUI 队列里跑/等待**。
        # 此时照常清理 `sb_ref_*`，那些任务执行到 LoadImage 就报
        # FileNotFoundError（实测 shot_05~11 连续 7 镜全灭，每镜 0.01s 失败）。
        # 故队列非空、或队列状态查不到（ok=False，信息不明）时**跳过本轮清理**：
        # 宁可留残留（有 _maybe_reclaim_comfyui_output 滚动兜底），
        # 也不删正在被引用的参考图。
        _q = comfyui_client.queue_state()
        if not _q.get("ok") or _q.get("running") or _q.get("pending"):
            logger.warning(
                "[质检清理] 跳过 sb_ref 清理（project=%s）：ComfyUI 队列 运行=%s / 等待=%s"
                "（查询ok=%s）—— 队列里可能仍有引用这些参考图的任务，"
                "删掉会让它们 LoadImage 报 FileNotFoundError",
                project, _q.get("running"), _q.get("pending"), _q.get("ok"))
            return res
        # generate_storyboard 的命名：sb_ref_<filename_prefix 的 basename>_<idx>.png，
        # 分镜链路 filename_prefix 形如 `comic_drama_sb/<项目>_shot_NN[...]`，
        # 故前缀里含 `<项目>_shot_`。
        pref = f"sb_ref_{_safe_project(project or '')}_shot_"
        targets = []
        for fn in os.listdir(root):
            if fn.startswith(pref) and fn.lower().endswith(".png"):
                targets.append(os.path.join(root, fn))
        if not targets:
            return res
        res = _reject_artifact(targets, project=project,
                               reason="分镜参考图上传残留（本轮分镜生成结束）",
                               kind="sb_ref")
    except Exception as e:  # noqa: BLE001
        logger.warning("[质检清理] sb_ref 清理失败（忽略，不影响生产）：%s: %s",
                           type(e).__name__, e)
    return res


# B-14 P2-4：资产「取图判据」统一入口。就绪判据（_collect_asset_refs 的
# _first_nonempty_image）与取图判据（_build_asset_index 的 _first_existing）
# 此前各自维护一套「判有图」逻辑，口径漂移（一个只认 4 个扩展名、另一个只
# 认 front/base 固定名）。统一为：扩展名白名单 + 取第一张非空图片。
_ASSET_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

#: 资产目录内「主视角图」的取图优先级（2026-09-24 修复）。
#: ⚠️ 实测 bug：原来只按**文件名字典序**取第一张非空图，而角色资产目录里
#:    back.png < base.png < front.png < left.png < right.png —— 于是 `back.png`
#:    （**背面图**）被当成「主角锚点」喂给分镜/视频链路（_build_asset_index 在
#:    剧本 characters 没有 front/base 字段时就走这条兜底，autopilot 正是这种形态）。
#:    当时看不出来，是因为 4 张视角图与 base 内容完全一致（都是同一张三视图整图）；
#:    一旦视角图变成真单机位（2026-09-24 sheet_split 改造后就是如此），
#:    这个字典序兜底就会静默地把每个镜头的角色锚点换成「只有背面」。
#: 故改为显式优先级：正面 > 整图 > 左侧 > 右侧 > 背面 > 其它图片（字典序）。
_ASSET_IMG_PRIORITY = ("front.png", "base.png", "front.jpg", "base.jpg",
                       "left.png", "right.png", "back.png",
                       "left.jpg", "right.jpg", "back.jpg")


def _comfyui_official_dirs() -> list:
    """正式产物目录清单（全部由 config 常量推导，不硬编码盘符路径）。"""
    return [d for d in (CHARACTERS_DIR, ITEMS_DIR, SCENES_DIR, STORYBOARDS_DIR,
                        KEYFRAMES_DIR, VIDEOS_DIR, FINAL_DIR) if d]


def _maybe_reclaim_comfyui_output() -> None:
    """薄包装：带节流地回收 ``COMFYUI_OUTPUT_DIR`` 下的产物残留（D-11a）。

    只有**同时**满足下列条件的文件才会被删（判定细节与安全论证见
    ``app/disk_reclaim.py`` 模块 docstring）：

      ① 位于 ``COMFYUI_OUTPUT_DIR`` 下的 ``comic_drama*`` 产物目录内；
      ② 文件名是 ComfyUI 侧产物命名（带自动编号后缀 ``_00001_``，或含
         ``_retry``/``_try``）—— 交付件名 ``base.png`` 之类天然不匹配；
      ③ ``mtime`` 距今 > 24h；
      ④ 正式产物目录里已有 ``(size, sha256)`` **双匹配**的同内容副本；
      ⑤ ``st_nlink == 1``（硬链接删了不释放空间）；
      ⑥ 候选不在任何正式产物目录内（含大小写归一后的比较）。

    性能：内容指纹按 ``(路径, size, mtime_ns)`` 进程内缓存，稳态下只有**新增**的
    正式产物需要读盘；配合 10 分钟节流，同步调用不会给任务收尾带来可感知的延迟。
    """
    global _COMFYUI_RECLAIM_LAST_TS
    try:
        now = time.time()
        with _COMFYUI_RECLAIM_LOCK:
            if now - _COMFYUI_RECLAIM_LAST_TS < _COMFYUI_RECLAIM_INTERVAL_SEC:
                return
            _COMFYUI_RECLAIM_LAST_TS = now
        if not COMFYUI_OUTPUT_DIR or not os.path.isdir(COMFYUI_OUTPUT_DIR):
            return
        import disk_reclaim   # 延迟导入：与本文件其它叶子模块一致，避免加载期副作用
        stats = disk_reclaim.reclaim_comfyui_output(
            COMFYUI_OUTPUT_DIR, _comfyui_official_dirs(), logger=logger)
        if stats.get("delete"):
            logger.info("D-11a ComfyUI 输出回收：删 %d 个产物残留，释放 %.2f MB",
                            len(stats["delete"]),
                            (stats.get("removed_bytes") or 0) / 1048576.0)
    except Exception as e:  # noqa: BLE001  回收是优化，绝不能阻断生产
        logger.warning("D-11a ComfyUI 输出回收失败（不影响生产）：%s: %s",
                           type(e).__name__, e)
