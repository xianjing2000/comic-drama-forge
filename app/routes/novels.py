# -*- coding: utf-8 -*-
"""小说库 / 章节 / 前置预检 API 蓝图（Blueprint 拆分第四批）。

10 条路由：上传·列表·详情·试读（小说库），章节列表·拆集计划·章节结构分析，
章前置预检（生成/读取）·预检清单。
依赖：novel_parser（读文本/切章/预检）、project_store、chapter_preflight、autopilot、
novel_to_script 等叶子模块，以及 routes/_shared.py 里的助手
（_novels_stats / _safe_upload_name / _resolve_novel_project / _optional_llm_client ...）。

⚠️ URL 规则与响应体一字不改，只把 @app.route 换成 @novels_bp.route。
⚠️ 本批**不含**三生成类路由（screenplay/generate、screenplay/<集>、episodes/generate）——
   它们牵着 _screenplay_worker / _episodes_worker / generation_state / lock，
   留给下一批单独搬，避免把状态机一起搅动。
"""
import os
import time

from flask import Blueprint, current_app, jsonify, request

import autopilot
import chapter_preflight
import novel_to_script
import project_store
from config import CONTINUITY_DIR, NOVELS_DIR, NOVEL_PREVIEW_CHARS, SCRIPT_DIR
from llm_client import LLMError
from novel_parser import (NovelParseError, SUPPORTED_EXTS, chapter_body_chars,
                          ensure_chapter_structure, get_novel, ingest_novel,
                          preview_novel, read_novel_text)
from routes._shared import (EPISODE_BATCH_LIMIT, UPLOAD_TMP_DIR, _app_logger, _episode_units_for_chapters, _estimate_subchunks, _novels_stats, _optional_llm_client, _resolve_novel_project, _safe_upload_name)

novels_bp = Blueprint('novels', __name__)

from routes._shared import _ai_guide_response, _current_llm_client, _novel_key
@novels_bp.route('/api/novels/upload', methods=['POST'])
def api_upload_novels():
    """上传小说文件（支持多文件），解析为纯文本并入库"""
    files = request.files.getlist('file') or request.files.getlist('files')
    if not files:
        return jsonify({"success": False, "error": "未收到文件，请通过 file 字段上传"}), 400

    os.makedirs(UPLOAD_TMP_DIR, exist_ok=True)
    os.makedirs(NOVELS_DIR, exist_ok=True)
    # A：上传时可直接归属到某个项目（一部小说 = 一个独立项目）
    pref = (request.form.get('project_id') or request.args.get('project_id')
            or request.form.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else None
    # 无人值守生产要求「上传即建好项目」：未显式指定项目时，按小说自动建立独立项目，
    # 这样上传完成后即可直接进入「总控 AI 定风格 → 开启托管」，不需要用户手工建项目。
    auto_project = (request.form.get('auto_project') or request.args.get('auto_project')
                    or '1').strip() not in ('0', 'false', 'no')
    # 上传时用 LLM 归纳章节标题正则（每本书格式不同，纯正则易漏检）。
    # 取「文本分析模型」客户端是 best-effort：未配置/异常则传 None，退回纯正则切分，
    # 绝不阻断上传。
    novel_llm_client = _optional_llm_client()
    results, ok_count = [], 0
    for f in files:
        raw_name = _safe_upload_name(f.filename)
        if not raw_name:
            results.append({"filename": f.filename, "success": False, "error": "文件名为空"})
            continue
        ext = os.path.splitext(raw_name)[1].lower()
        tmp_path = os.path.join(
            UPLOAD_TMP_DIR,
            f"{time.strftime('%Y%m%d%H%M%S')}_{os.urandom(4).hex()}{ext or '.txt'}"
        )
        try:
            f.save(tmp_path)
            meta = ingest_novel(tmp_path, raw_name, NOVELS_DIR,
                                llm_client=novel_llm_client)
            ok_count += 1
            item_proj = proj
            if item_proj is None and auto_project:
                try:
                    item_proj = _resolve_novel_project({}, meta) or None
                except Exception as e:  # noqa: BLE001  建项目失败不该让上传整体失败
                    _app_logger().warning(f"自动建项目失败（小说已入库）：{e}")
            if item_proj:
                item_proj = project_store.update_project(
                    item_proj["id"], novel_id=meta.get("novel_id") or "",
                    novel_name=meta.get("name") or raw_name) or item_proj
                proj = proj or item_proj
            results.append({
                "filename": raw_name, "success": True,
                "novel": {k: v for k, v in meta.items() if k != "chapters"},
                "chapter_preview": (meta.get("chapters") or [])[:5],
                "project_id": (item_proj or {}).get("id", ""),
                "project_key": (item_proj or {}).get("dir_key", ""),
                "project": {k: (item_proj or {}).get(k) for k in
                            ("id", "name", "dir_key", "novel_id")} if item_proj else None,
            })
        except NovelParseError as e:
            results.append({"filename": raw_name, "success": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            _app_logger().error(f"小说解析失败 {raw_name}: {e}")
            results.append({"filename": raw_name, "success": False, "error": f"解析失败：{e}"})
        finally:
            # 清理临时文件属于「收尾」，绝不能因为它失败而让一个已经成功的上传变成
            # 无响应。除 OSError（Windows 杀软占用、共享冲突）外，某些运行环境注入的
            # 安全守卫会直接抛 SystemExit —— 它继承自 BaseException 而非 Exception，
            # Flask 不会把它转成 500，而是会掐断这次请求（客户端表现为挂起后空响应）。
            # 这里放宽到 BaseException，但保留 KeyboardInterrupt 的语义。
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except KeyboardInterrupt:
                raise
            except BaseException as _e:  # noqa: BLE001
                _app_logger().warning(f"上传临时文件清理失败（忽略，不影响本次上传）：{_e!r}")

    return jsonify({
        "success": ok_count > 0,
        "uploaded": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
        "supported_exts": sorted(SUPPORTED_EXTS),
        "stats": _novels_stats(),
    }), (200 if ok_count else 400)
@novels_bp.route('/api/novels', methods=['GET'])
def api_list_novels():
    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    include_unbound = (request.args.get('include_unbound', '1') not in ('0', 'false', 'no'))
    return jsonify({"success": True, **_novels_stats(pref or None, include_unbound),
                    "project_id": pref,
                    "supported_exts": sorted(SUPPORTED_EXTS)})
@novels_bp.route('/api/novels/<novel_id>', methods=['GET'])
def api_novel_detail(novel_id):
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True, "novel": meta,
                    "text_path": os.path.abspath(
                        os.path.join(NOVELS_DIR, meta.get("text_file") or f"{novel_id}.txt"))})
@novels_bp.route('/api/novels/<novel_id>/preview', methods=['GET'])
def api_novel_preview(novel_id):
    offset = request.args.get('offset', 0, type=int)
    limit = request.args.get('limit', NOVEL_PREVIEW_CHARS, type=int)
    try:
        data = preview_novel(NOVELS_DIR, novel_id, offset=offset, limit=limit)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True, **data})
@novels_bp.route('/api/novels/<novel_id>/chapters', methods=['GET'])
def api_novel_chapters(novel_id):
    """章节列表（章名 / 字数 / 规模提示 / 已生成剧集状态）"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    pref = (request.args.get('project_id') or request.args.get('project_name') or '').strip()
    proj = project_store.get_project(pref) if pref else project_store.find_by_novel(novel_id)
    pkey = proj["dir_key"] if proj else None
    key = _novel_key(meta, pkey)
    raw = meta.get("chapters") or []
    # ⭐ 集号口径统一（2026-09-25）：本路由原先硬写「集号 = 章序号」，
    #    而托管与 /episodes/generate 走的是 `autopilot.episode_units`（超长章会拆，
    #    集号 ≠ 章号）。同一部小说在「章节列表」与「成片/进度」两处显示不同集号，
    #    用户按章节列表的集号去点生产，实际打到了另一集。
    #    统一到 `_episode_units_for_chapters`（其编号基于**全量章节**展开，
    #    与托管口径逐字一致，见该函数注释）。
    # 注意：这里按**全量章节**展开后回填，因此列表里的 episode_no 是「该章第一个单元的集号」，
    #       多单元时并给出 episode_nos 完整列表；`episode`（已生成产物的状态）也改按单元找。
    _units_by_chapter: dict = {}
    try:
        _all_units = _episode_units_for_chapters(meta, raw)
    except Exception as e:  # noqa: BLE001
        _app_logger().warning("章节列表分集展开失败（按一章一集回显）：%s", e)
        _all_units = []
    for u in (_all_units or []):
        _ci = int(u.get("chapter_index") or 0)
        _units_by_chapter.setdefault(_ci, []).append(int(u.get("episode_no") or 0))
    # 单元 → 产物：list_episodes 的 chapter_index 在多单元时同一章会有多行，
    # 这里按集号再索引一份，供每个单元精确命中自己那一集的产物。
    _gen_by_ep = {int(e.get("episode_no") or 0): e for e in
                  novel_to_script.list_episodes(SCRIPT_DIR, key)}

    def _units_of(idx: int) -> list:
        """该章对应的集号列表；拿不到单元表时退回「一章一集」（历史行为）"""
        return _units_by_chapter.get(idx) or [idx]

    chapters = []
    for c in raw:
        idx = int(c.get("index") or (len(chapters) + 1))
        cnt = int(c.get("char_count") or 0)
        sub = _estimate_subchunks(cnt)
        _eps = _units_of(idx)
        _gen = [_gen_by_ep[e] for e in _eps if e in _gen_by_ep]
        chapters.append({
            "index": idx,
            # 集号 = 该章**第一个单元**的集号（未拆章时即等于章号，与历史一致）
            "episode_no": _eps[0],
            # 该章拆出的全部集号（未拆章时只有 1 个）—— 前端据此提示「本章 N 集」
            "episode_nos": _eps,
            "episode_count": len(_eps),
            "title": c.get("title") or f"第{idx}章",
            "char_count": cnt,
            "start": c.get("start"), "end": c.get("end"),
            "est_subchunks": sub,
            "est_shots": novel_to_script.estimate_episode_shots(cnt),
            "too_short": cnt < novel_to_script.CHAPTER_MIN_CHARS,
            "advice": novel_to_script.chapter_advice(cnt, sub)["message"],
            "generated": bool(_gen),
            # 兼容旧前端：episode 仍是「单个已生成剧集」；多集时取第一个
            "episode": (_gen[0] if _gen else None),
            "episodes": _gen,
            "generated_count": len(_gen),
        })

    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "project_id": (proj or {}).get("id", ""),
        "project_key": key,
        "title": meta.get("title") or meta.get("name"),
        "name": meta.get("name"),
        "char_count": meta.get("char_count"),
        "chapter_count": len(chapters),
        "fallback": not chapters,
        "fallback_hint": ("未识别到章节标记，无法按章分集；可先用「AI 转成剧本」整本处理，"
                          "或上传带「第N章」标题的小说") if not chapters else "",
        "min_chars": novel_to_script.CHAPTER_MIN_CHARS,
        "chunk_chars": novel_to_script.CHAPTER_CHUNK_CHARS,
        "max_subchunks": novel_to_script.CHAPTER_MAX_SUBCHUNKS,
        "batch_limit": EPISODE_BATCH_LIMIT,
        # 已生成剧集数：按**产物文件**计数（口径与 /api/episodes 一致）。
        # 旧代码用 `len(ep_map)`（以 chapter_index 为键去重）—— 一章拆多集时会少算。
        "generated_count": len([e for e in _gen_by_ep.values() if e.get("shots")]),
        "unit_total": sum(len(_units_of(int(c.get("index") or 0))) for c in raw),
        "split_mode": "content",   # 分集策略：纯按内容体量自动判断（见 novel_to_script）
        # 章节目录体检结果（LLM 读目录判断真章节；未体检时为 None）——前端据此提示
        # 「已折叠 N 个卷/分部标题」，并说明第 1 集对应的是哪一节正文。
        "chapter_structure": meta.get("chapter_structure") or None,
        "chapter_structure_audited_at": meta.get("chapter_structure_audited_at"),
        "chapter_collapse_note": (meta.get("chapter_collapse_note")
                                  or meta.get("chapter_structure_note") or ""),
        "chapters": chapters,
    })
@novels_bp.route('/api/novels/<novel_id>/split-plan', methods=['GET'])
def api_novel_split_plan(novel_id):
    """P2-2 渐进分集 + 人会确认：返回「分集断点提议」payload，供用户在触发整集渲染前
    逐条核对「这章拆 N 集 / 每个切点落在原文哪句 / 每集约几镜几秒 / 是否超单集红线」。

    与生产口径**逐字同源**：底层都是 ``novel_to_script.split_chapter_for_episodes``
    （生产走 ``autopilot.episode_units`` 逐章调用它；本路由直接复用其结果再算预估/红线/切点
    预览），因此「提议」与「实际生成」的集数/切点完全一致，不会提议说 1 集、真生成拆 3 集。
    纯只读（不触发任何 LLM/生成），可反复查询。

    query params（均可选）:
      - max_sec：单集时长上限（秒），默认 ``EPISODE_MAX_SEC``（180）
      - fixed_parts：每章最低拆几集（默认 1＝不强制）
      - max_shots：单集镜数上限（**2026-10-09 起已废弃**：改为按场次生产后单集不再设上限，
        本参数保留仅为兼容旧调用，传入不再产生截断/收紧效果）

    返回：{"success", "novel_id", "chapter_count", "total_episodes",
           "needs_confirm_chapters": [章号…], "episodes_per_chapter": [章号…],
           "chapters": [{index,title,char_count,units:[{part,start,end,char_count,
           preview,est_shots,est_sec,over_redline}],needs_confirm,total_parts,message}…]}
    """
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    all_chapters, text = autopilot.chapters_and_text(meta)
    if not all_chapters:
        return jsonify({"success": False,
                       "error": "该小说未识别到章节标记，无法按章分集"}), 400
    # ⭐ 与生产路径**逐字对齐**（P2-2 正确性核心）：生产 ``autopilot.episode_units`` 调
    #    ``split_chapter_for_episodes(ch, text, fixed_parts=...)`` 时**不传 max_sec / max_shots**
    #    （两者取模块默认），只透传 fixed_parts。故默认 query（不带参数）时三者全 None，
    #    「提议」≡「实际生成」，绝不会提议说 1 集、真生成拆 3 集。仅当用户显式 query 指定
    #    某项时才用自定义值（预览「把单集上限调到 N 秒会拆几集」）。
    def _opt_int(key):
        raw = request.args.get(key)
        if raw in (None, '', '0'):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    max_sec = _opt_int('max_sec')
    max_shots = _opt_int('max_shots')
    fixed_parts = _opt_int('fixed_parts')
    chapters_out = []
    needs_confirm_idxs = []
    episodes_per_chapter = []
    total_episodes = 0
    for ch in all_chapters:
        # 与生产 episode_units 同源：默认 max_sec/fixed_parts/max_shots 口径一致
        proposal = novel_to_script.propose_chapter_split(
            ch, text, max_sec=max_sec, fixed_parts=fixed_parts, max_shots=max_shots)
        # 一章一集口径（2026-10-04 用户决策）：分集拆分已停用 —— 「超单集红线 /
        # 建议手动拆 / 需人工确认」警告全部失效不再输出（预估镜数/秒数仅作参考）。
        if str(os.environ.get("MJSCXT_EPISODE_SPLIT") or "").strip().lower() \
                not in ("1", "true", "yes", "on"):
            proposal["needs_confirm"] = False
            proposal["total_parts"] = 1
            proposal["message"] = "一章一集（分集拆分已停用，单集时长仅作参考）"
            for _u in (proposal.get("units") or []):
                if isinstance(_u, dict):
                    _u["over_redline"] = False
        proposal["index"] = ch.get("index")
        proposal["title"] = ch.get("title")
        proposal["char_count"] = ch.get("char_count")
        total_episodes += int(proposal.get("total_parts") or 1)
        episodes_per_chapter.append({
            "chapter_index": ch.get("index"),
            "title": ch.get("title"),
            "total_parts": int(proposal.get("total_parts") or 1),
        })
        if proposal.get("needs_confirm"):
            needs_confirm_idxs.append(ch.get("index"))
        chapters_out.append(proposal)
    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "chapter_count": len(all_chapters),
        "total_episodes": total_episodes,
        "needs_confirm_chapters": needs_confirm_idxs,
        "episodes_per_chapter": episodes_per_chapter,
        "params": {"max_sec": max_sec, "max_shots": max_shots,
                   "fixed_parts": fixed_parts},
        "chapters": chapters_out,
    })
@novels_bp.route('/api/novels/<novel_id>/chapters/analyze', methods=['POST'])
def api_novel_chapters_analyze(novel_id):
    """LLM 章节目录体检：像人一样读目录，判断「哪一条才是真正的正文章节」。

    在生成剧本之前调用（前端可给一个「AI 分析章节结构」按钮）。判据是
    「标题是不是分卷/分部名」+「标题行外有没有成段正文」，结果缓存进 meta，
    同一本书只跑一次 LLM；force=true 强制重跑。

    背景：CHAPTER_PATTERNS 用同一条正则匹配「第[数字][章回节卷篇集部]」，
    《蛊真人》的「第一卷：魔性不改」（正文 0 字）因此被当成第 1 章，
    整集剧本全靠模型编（8 个镜头全是自造的四字口号），覆盖率还报 100%。
    """
    try:
        get_novel(NOVELS_DIR, novel_id)   # 存在性校验
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    client = _optional_llm_client()
    if client is None:
        return jsonify({
            "success": False,
            "error": "尚未配置「文本分析模型」，无法做章节目录体检；"
                     "系统已用规则兜底折叠（标题行外无正文即视为卷/分部标题）。"}), 400
    force = bool((request.json or {}).get("force"))
    try:
        meta = ensure_chapter_structure(NOVELS_DIR, novel_id, client, force=force)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({
        "success": True,
        "novel_id": novel_id,
        "chapter_count": meta.get("chapter_count"),
        "chapter_structure": meta.get("chapter_structure") or None,
        "audited_at": meta.get("chapter_structure_audited_at"),
        "note": (meta.get("chapter_collapse_note")
                 or meta.get("chapter_structure_note") or ""),
        "chapters": [{"index": c.get("index"), "title": c.get("title"),
                      "char_count": c.get("char_count"), "volume": c.get("volume")}
                     for c in (meta.get("chapters") or [])[:30]],
    })
@novels_bp.route('/api/novels/<novel_id>/chapters/<int:chapter_index>/preflight', methods=['POST'])
def api_novel_chapter_preflight(novel_id, chapter_index):
    """单章前置解析：输出人物档案 + 故事梗概 + 关键事件 + 人物情绪。

    结果落盘到 output/continuity/<项目键>/preflight/第N章.json，
    并将人物档案并入 bible.json（锁定角色，防止全片 OOC）。

    body: { force?: bool }  force=true 覆盖已有结果
    返回: { success, chapter_index, result, path, bible_added, bible_updated }
    """
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404

    client = _current_llm_client()
    if not client.configured:
        return _ai_guide_response("尚未配置自定义 AI 接口，无法执行前置解析")

    all_chapters = meta.get("chapters") or []
    target = next((c for c in all_chapters if int(c.get("index") or 0) == int(chapter_index)), None)
    if not target:
        return jsonify({"success": False,
                        "error": f"章节 {chapter_index} 不存在（共 {len(all_chapters)} 章）"}), 404

    data = request.json or {}
    proj = _resolve_novel_project(data, meta)
    key = _novel_key(meta, proj["dir_key"])
    _full_text = read_novel_text(NOVELS_DIR, meta["novel_id"])
    ch_text = _full_text[int(target.get("start") or 0):int(target.get("end") or 0)]

    try:
        result = chapter_preflight.preflight_analyze(
            client,
            meta.get("title") or meta.get("name") or "",
            int(chapter_index),
            target.get("title") or f"第{chapter_index}章",
            ch_text,
        )
    except LLMError as e:
        return jsonify({"success": False, "error": f"LLM 解析失败：{e}"}), 500
    except Exception as e:  # noqa: BLE001
        _app_logger().exception("前置解析异常（章节 %s）", chapter_index)
        return jsonify({"success": False, "error": f"解析异常：{e}"}), 500

    path = chapter_preflight.save_preflight(CONTINUITY_DIR, key, result)
    bible_merge = chapter_preflight.merge_to_bible(CONTINUITY_DIR, key, result)

    return jsonify({
        "success": True,
        "chapter_index": int(chapter_index),
        "chapter_title": result.get("chapter_title"),
        "result": result,
        "path": path,
        "project_key": key,
        "bible_added": bible_merge.get("added") or [],
        "bible_updated": bible_merge.get("updated") or [],
        "characters": len(result.get("characters") or []),
        "key_events": len(result.get("key_events") or []),
    })
@novels_bp.route('/api/novels/<novel_id>/chapters/<int:chapter_index>/preflight', methods=['GET'])
def api_novel_chapter_preflight_get(novel_id, chapter_index):
    """读取某章已有前置解析结果；不存在返回 {exists: false}"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    proj = _resolve_novel_project({}, meta)
    key = _novel_key(meta, proj["dir_key"])
    result = chapter_preflight.load_preflight(CONTINUITY_DIR, key, int(chapter_index))
    if not result:
        return jsonify({"exists": False, "chapter_index": int(chapter_index)})
    return jsonify({"exists": True, "chapter_index": int(chapter_index), "result": result})
@novels_bp.route('/api/novels/<novel_id>/preflight/list', methods=['GET'])
def api_novel_preflight_list(novel_id):
    """列出该小说（项目）所有已完成前置解析的章节号"""
    try:
        meta = get_novel(NOVELS_DIR, novel_id)
    except NovelParseError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    proj = _resolve_novel_project({}, meta)
    key = _novel_key(meta, proj["dir_key"])
    indices = chapter_preflight.list_preflight(CONTINUITY_DIR, key)
    return jsonify({"success": True, "chapter_indices": indices, "count": len(indices)})
