# -*- coding: utf-8 -*-
"""四层质量状态机 + 哈希绑定人审（借鉴 ai-manga-factory 的 A/B/C/D 与哈希晋级）

为什么需要「四层」而不是「一个 passed」
------------------------------------
此前只有二值的质检结论，出问题时无法回答「卡在哪一层」。四层把责任分开：

======  ==================  ====================================================
层      名称                 谁负责 / 判据
======  ==================  ====================================================
A       technical_render    **确定性**：ffprobe 时长/分辨率/fps/codec/音轨是否存在
B       content_qa          **自动**：复用现有 qc_client 的逐镜与整片结论
C       editorial_review    **人工**：并排复核（合同目标 / 参考资产 / 抽帧 / QA 指标）
D       release_approval    **人工**：整集可发布（联系表、节奏、平台安全区…）
======  ==================  ====================================================

⚠️ A 层通过只代表「**技术生成完成**」，**绝不等于内容合格** ——
   这是竞品文档里反复强调的一条，也是本模块刻意用 label 而不是 passed 来措辞的原因。

为什么批准必须绑定哈希（本模块最值钱的部分）
-----------------------------------------
原实现只在**路径变化**时把 review 复位。但重做一集时产物路径是同一个
（ep01_full.mp4 会被原地覆盖）→ 路径没变 → **旧的「已验收」结论继续生效**。
用户看到的是「已验收」，实际批的是上一版内容 —— 静默事故，且无法察觉。

绑定三元组后，任一变化即判定批准失效：

* artifact_sha256：产物内容（快路径用 size+mtime 先比，避免每次哈希大文件）
* contract_hash  ：合同（该集应有的镜头数 / 时长 / 来源）
* qa_report_hash ：当次质检报告

一切失败 **fail-open**：绑定算不出/读不到时只判 unbound（不误伤），
状态机读写失败只降级为内存态，绝不阻断出片。
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fs_atomic import atomic_write_json, read_json_strict

logger = logging.getLogger(__name__)

__all__ = [
    "STAGES", "STAGE_NAMES", "STAGE_LABELS", "STATUSES",
    "technical_checks", "evaluate_technical", "evaluate_content",
    "blank_state", "set_stage", "release_ready",
    "state_path", "load_state", "save_state", "record_stage",
    "sha256_file", "hash_obj", "file_signature",
    "make_binding", "check_binding", "stage_binding", "check_stage_binding",
]

#: 四层，顺序即晋级顺序。
STAGES: Tuple[str, ...] = ("A", "B", "C", "D")
STAGE_NAMES = {"A": "technical_render", "B": "content_qa",
               "C": "editorial_review", "D": "release_approval"}
#: 给界面/日志用的中文措辞。A 刻意不写「通过」，避免被误读成质量结论。
STAGE_LABELS = {"A": "技术生成完成", "B": "内容质检", "C": "编辑复核", "D": "发布批准"}
STATUSES = ("pending", "passed", "failed", "blocked", "skipped")

_UNSET = object()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _norm_ep(episode: Any) -> str:
    """把集号规整成安全的目录名（支持 3 / "3" / "整集"）。"""
    s = str(episode if episode is not None else "all").strip() or "all"
    if s.isdigit():
        return "ep%02d" % int(s)
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in s)


# =====================================================================
# A 层：技术渲染（确定性，不掺任何主观判断）
# =====================================================================

def technical_checks(path: str, *, probe=None, min_duration: float = 2.0) -> List[Dict[str, Any]]:
    """A 层的确定性检查项。返回 [{check, ok, detail}]，全部可单独定位。"""
    checks: List[Dict[str, Any]] = []

    exists = bool(path) and os.path.isfile(path)
    checks.append({"check": "file_exists", "ok": exists,
                   "detail": os.path.basename(str(path or "")) or "(空路径)"})
    if not exists:
        return checks

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    checks.append({"check": "non_empty", "ok": size > 0, "detail": "%d bytes" % size})

    if probe is None:
        from video_probe import probe_video as probe
    try:
        info = probe(path) or {}
    except Exception as e:                                  # noqa: BLE001
        info = {"ok": False, "error": "探测异常: %s" % e}

    if not info.get("ok"):
        checks.append({"check": "probe", "ok": False,
                       "detail": str(info.get("error") or "探测失败")[:200]})
        return checks

    checks.append({"check": "probe", "ok": True,
                   "detail": "%sx%s @%sfps" % (info.get("width"), info.get("height"),
                                               info.get("fps"))})
    dur = float(info.get("duration") or 0)
    checks.append({"check": "duration", "ok": dur >= min_duration,
                   "detail": "%.2fs (下限 %.1fs)" % (dur, min_duration)})
    checks.append({"check": "video_codec", "ok": bool(info.get("video_codec")),
                   "detail": str(info.get("video_codec"))})
    checks.append({"check": "fps", "ok": float(info.get("fps") or 0) > 0,
                   "detail": str(info.get("fps"))})
    # 音轨只记录、不判失败：H3 视频自带原生音轨（成片即带配音）；此处只记录无音轨事实
    # 不判失败 —— 无音轨的硬拦截在 qc_client 视频客观层（B 层）。
    checks.append({"check": "has_audio", "ok": True,
                   "detail": "有音轨" if info.get("has_audio") else "无音轨（生成阶段静音或待配音）"})
    return checks


def evaluate_technical(path: str, *, probe=None,
                       min_duration: float = 2.0) -> Dict[str, Any]:
    """跑 A 层并给出该层状态。

    ⚠️ status=passed 的语义是「**技术生成完成**」，不是「内容合格」。
    """
    checks = technical_checks(path, probe=probe, min_duration=min_duration)
    failed = [c for c in checks if not c.get("ok")]
    return {
        "stage": "A", "name": STAGE_NAMES["A"], "label": STAGE_LABELS["A"],
        "status": "passed" if checks and not failed else "failed",
        "meaning": "技术生成完成（不代表内容合格）",
        "checks": checks,
        "failed_checks": [c["check"] for c in failed],
        "at": _now(),
    }


# =====================================================================
# B 层：内容质检（复用既有 qc_client 结论，不重复造判据）
# =====================================================================

def evaluate_content(qc_result: Optional[dict]) -> Dict[str, Any]:
    """把既有质检结论映射为 B 层状态。

    * 未开启/未送检 → skipped（**不是 passed**：没检过不能说合格）
    * 接口不可用     → blocked（已声明要检但检不了，必须阻断而不是放行）
    * passed=True   → passed
    * passed=False  → failed
    """
    if not qc_result:
        return {"stage": "B", "name": STAGE_NAMES["B"], "label": STAGE_LABELS["B"],
                "status": "skipped", "note": "未送检（质检未开启），不得视为合格",
                "at": _now()}
    ok = qc_result.get("ok", _UNSET)
    passed = qc_result.get("passed", _UNSET)
    if ok is False and passed is _UNSET:
        status, note = "blocked", "质检接口不可用"
    elif passed is True:
        status, note = "passed", ""
    elif passed is False:
        status, note = "failed", str(qc_result.get("reason") or "")[:300]
    else:
        status, note = "pending", "质检结论不完整"
    return {"stage": "B", "name": STAGE_NAMES["B"], "label": STAGE_LABELS["B"],
            "status": status, "note": note,
            "evidence": {"score": qc_result.get("score"),
                         "passed": qc_result.get("passed"),
                         "ok": qc_result.get("ok")},
            "at": _now()}


# =====================================================================
# 状态机
# =====================================================================

def blank_state(project: str, episode: Any) -> Dict[str, Any]:
    """全新状态：四层全部 pending。"""
    return {
        "v": 1, "project": str(project or ""), "episode": str(episode),
        "stages": {s: {"stage": s, "name": STAGE_NAMES[s], "label": STAGE_LABELS[s],
                       "status": "pending"} for s in STAGES},
        "updated_at": _now(),
    }


def set_stage(state: Dict[str, Any], stage: str, status: str, *,
              evidence: Optional[dict] = None, note: str = "",
              binding: Optional[dict] = None) -> Dict[str, Any]:
    """把某层置为某状态，返回**新** state（纯函数，不修改入参）。

    状态集合之外的取值会被拒绝（保持状态机封闭，避免写入拼错的字符串）。
    """
    if stage not in STAGES:
        raise ValueError("未知阶段: %r（合法值 %s）" % (stage, list(STAGES)))
    if status not in STATUSES:
        raise ValueError("未知状态: %r（合法值 %s）" % (status, list(STATUSES)))
    out = copy.deepcopy(state or {})
    stages = out.setdefault("stages", {})
    entry = dict(stages.get(stage) or {"stage": stage, "name": STAGE_NAMES[stage],
                                       "label": STAGE_LABELS[stage]})
    entry["status"] = status
    entry["at"] = _now()
    if note:
        entry["note"] = note
    if evidence is not None:
        entry["evidence"] = evidence
    if binding is not None:
        entry["binding"] = binding
    stages[stage] = entry
    out["updated_at"] = _now()
    return out


def stage_status(state: Dict[str, Any], stage: str) -> str:
    return str(((state or {}).get("stages") or {}).get(stage, {}).get("status") or "pending")


def release_ready(state: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """能否发布：**D 必须 passed**，且任一层不得处于 failed/blocked。

    B 层 skipped 允许放行（用户没开质检），但会把提示带出来 —— 不假装检过了。
    """
    reasons: List[str] = []
    for s in STAGES:
        st = stage_status(state, s)
        if st in ("failed", "blocked"):
            reasons.append("%s(%s) 状态为 %s" % (s, STAGE_NAMES[s], st))
    if stage_status(state, "D") != "passed":
        reasons.append("D(release_approval) 尚未批准")
    if stage_status(state, "B") == "skipped":
        reasons.append("提示：B 层未送检，本次发布未经内容质检")
    return (not [r for r in reasons if not r.startswith("提示：")], reasons)


def state_path(project: str, episode: Any, *, root: Optional[str] = None) -> str:
    """状态文件：<PROJECT_OUTPUT_DIR>/qc/<项目>/<集>/quality_state.json。"""
    if root is None:
        from config import PROJECT_OUTPUT_DIR as root
    return os.path.join(root, "qc", str(project or ""), _norm_ep(episode),
                        "quality_state.json")


def load_state(project: str, episode: Any, *, root: Optional[str] = None) -> Dict[str, Any]:
    """读状态；不存在/损坏一律返回 blank（fail-open，绝不阻断出片）。"""
    try:
        data = read_json_strict(state_path(project, episode, root=root), {}) or {}
        if isinstance(data, dict) and data.get("stages"):
            return data
    except Exception as e:                                   # noqa: BLE001
        logger.warning("质量状态读取失败（按全新状态处理）：%s", e)
    return blank_state(project, episode)


def save_state(project: str, episode: Any, state: Dict[str, Any], *,
               root: Optional[str] = None) -> bool:
    """原子落盘；失败只 warning 并返回 False（状态是观测面，不是生产依赖）。"""
    try:
        atomic_write_json(state_path(project, episode, root=root), state)
        return True
    except Exception as e:                                   # noqa: BLE001
        logger.warning("质量状态落盘失败（不影响出片）：%s", e)
        return False


def record_stage(project: str, episode: Any, stage: str, status: str, *,
                 evidence: Optional[dict] = None, note: str = "",
                 root: Optional[str] = None,
                 binding: Optional[dict] = None) -> Dict[str, Any]:
    """便捷入口：读 → 置层 → 写，返回新状态。"""
    st = set_stage(load_state(project, episode, root=root), stage, status,
                   evidence=evidence, note=note, binding=binding)
    save_state(project, episode, st, root=root)
    return st


def stage_binding(state: Dict[str, Any], stage: str) -> Optional[dict]:
    """取某层批准时钉下的产物绑定；无则 None。"""
    return ((state or {}).get("stages") or {}).get(stage, {}).get("binding")


def check_stage_binding(state: Dict[str, Any], stage: str, path: str, *,
                        contract: Any = None,
                        qa_report: Any = None) -> Tuple[str, str]:
    """校验某层批准绑定是否仍成立，返回 (status, reason)。

    无绑定（老数据 / 未绑定）→ unbound，**不误判为失效**；调用方据此可在界面上
    区分「尚未批准」与「批准已失效（产物被重做）」两种状态。
    """
    b = stage_binding(state, stage)
    return check_binding(b, path, contract=contract, qa_report=qa_report)


# =====================================================================
# 哈希绑定（批准必须钉在具体内容上）
# =====================================================================

def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """流式 sha256（不整文件读进内存）。失败返回空串（调用方按 unbound 处理）。"""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except (OSError, TypeError, ValueError) as e:
        logger.warning("产物哈希计算失败（按无法绑定处理）：%s", e)
        return ""


def hash_obj(obj: Any) -> str:
    """任意 JSON-able 对象的稳定哈希；不可序列化时返回空串。"""
    if obj is None:
        return ""
    try:
        blob = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError) as e:
        logger.warning("对象哈希计算失败：%s", e)
        return ""
    return hashlib.sha256(blob.encode("utf-8", "ignore")).hexdigest()[:32]


def file_signature(path: str, *, with_hash: bool = True) -> Dict[str, Any]:
    """产物签名：size+mtime 永远有（快路径），sha256 可选。

    size+mtime 是为了**避免每次校验都哈希大文件**：成片动辄几百 MB，
    在列表接口里逐个哈希会直接把界面拖死。只有 size/mtime 变了才需要真哈希。
    """
    sig: Dict[str, Any] = {"size": 0, "mtime": 0.0, "sha256": ""}
    try:
        stt = os.stat(path)
        sig["size"] = int(stt.st_size)
        sig["mtime"] = round(float(stt.st_mtime), 3)
    except (OSError, TypeError):
        return sig
    if with_hash:
        sig["sha256"] = sha256_file(path)
    return sig


def make_binding(path: str, *, contract: Any = None, qa_report: Any = None,
                 with_hash: bool = True) -> Dict[str, Any]:
    """构造批准绑定：产物签名 + 合同哈希 + 质检报告哈希。

    三者在**批准的那一刻**取快照。之后任一变一动，批准即失效。
    """
    sig = file_signature(path, with_hash=with_hash)
    return {
        "artifact_sha256": sig.get("sha256") or "",
        "artifact_size": sig.get("size"),
        "artifact_mtime": sig.get("mtime"),
        "contract_hash": hash_obj(contract),
        "qa_report_hash": hash_obj(qa_report),
        "bound_at": _now(),
    }


def check_binding(binding: Optional[dict], path: str, *,
                  contract: Any = None, qa_report: Any = None) -> Tuple[str, str]:
    """校验绑定，返回 (status, reason)。

    status ∈ {valid, invalid, unbound}
      * unbound —— 没有绑定信息（老数据/未绑定）：**不误判为失效**
      * valid   —— 三者都对得上
      * invalid —— 任一变了，附上具体是哪一项变了

    快路径：size 与 mtime 都没变 → 直接判 valid，不做 sha256（大文件友好）。
    """
    if not isinstance(binding, dict) or not binding:
        return "unbound", "无绑定信息（未绑定或旧数据）"

    try:
        stt = os.stat(path)
    except (OSError, TypeError):
        return "invalid", "产物已不存在或不可读"

    if binding.get("artifact_size") is not None and int(binding["artifact_size"]) != int(stt.st_size):
        return "invalid", ("产物内容已变（大小 %s → %s）"
                           % (binding.get("artifact_size"), stt.st_size))
    if binding.get("artifact_mtime") is not None and \
            abs(float(binding["artifact_mtime"]) - float(stt.st_mtime)) > 0.001:
        return "invalid", "产物已被重新写入（mtime 变化）"

    want = binding.get("artifact_sha256") or ""
    if want:
        got = sha256_file(path)
        if not got:
            return "unbound", "产物哈希本次算不出，无法确认（不误判失效）"
        if got != want:
            return "invalid", "产物内容指纹不匹配（内容已被替换）"

    bound_c = binding.get("contract_hash") or ""
    if bound_c and contract is not None:
        if hash_obj(contract) != bound_c:
            return "invalid", "合同已变（镜头数/时长等约定被修改）"
    bound_q = binding.get("qa_report_hash") or ""
    if bound_q and qa_report is not None:
        if hash_obj(qa_report) != bound_q:
            return "invalid", "质检报告已变（批准依据的结论已不是当次那份）"

    return "valid", "绑定一致"
