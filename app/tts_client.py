# -*- coding: utf-8 -*-
"""
QwenTTS 配音客户端（真实链路）
==============================

基于本机 ComfyUI（127.0.0.1:8188）已注册的 ComfyUI-Qwen-TTS 节点（FB_Qwen3TTS*）实现真实配音：

    FB_Qwen3TTSCustomVoice / FB_Qwen3TTSVoiceDesign  ->  SaveAudio  ->  ComfyUI output  ->  项目 output/dub/

设计要点
--------
* 角色音色一致性：每个角色固定一套「音色定义 + seed」，全剧逐句复用，保证同一角色音色不变；
* 两种音色模式：preset（9 个预置 speaker）/ design（中文音色描述驱动 VoiceDesign，可自定义音色）；
* 批量合成：把多句台词编进同一个 ComfyUI prompt（模型只加载一次），逐句回填产物路径与时长；
* 落盘隔离：项目侧产物统一写 output/dub/<项目>/（lines/ 单句 + 整集合并音频），
  ComfyUI 侧原始文件保留在 ComfyUI/output/dub/<项目>/，互不覆盖；
* 不静默失败：节点 / 模型缺失、ComfyUI 执行报错、未取到音频文件都会抛出 TTSError 并给出明确原因。
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from config import COMFYUI_URL, MODELS_DIR, PROJECT_ROOT_DIR, TTS_DEFAULT_PARAMS, KEEP_MODEL_LOADED
from dialogue_utils import (
    normalize_lines as _norm_dlg_lines,
    format_line as _dlg_line, has_dialogue as _has_dlg,
)

logger = logging.getLogger(__name__)

# 语音合成必需节点（Add-new：缺任一即视为环境不可用）
REQUIRED_NODES = ("FB_Qwen3TTSCustomVoice", "FB_Qwen3TTSVoiceDesign", "SaveAudio")
OPTIONAL_NODES = ("FB_Qwen3TTSVoiceClone", "FB_Qwen3TTSDialogueInference", "FB_Qwen3TTSRoleBank")

# 模型权重约定目录（ComfyUI-Qwen-TTS 从 models/qwen-tts 或 HF 缓存读取）
TTS_MODEL_ROOT = os.path.join(MODELS_DIR, "qwen-tts")
TTS_MODEL_EXPECTED = [
    ("Qwen3-TTS-12Hz-1.7B-CustomVoice", "1.7B 预置音色（主用）"),
    ("Qwen3-TTS-12Hz-1.7B-VoiceDesign", "1.7B 音色设计"),
    ("Qwen3-TTS-12Hz-0.6B-CustomVoice", "0.6B 预置音色（低显存备选）"),
    ("Qwen3-TTS-Tokenizer-12Hz", "音频 Tokenizer / 编解码"),
]

# 9 个预置音色（来自节点 schema，实测可用）
VOICE_PRESETS = [
    {"speaker": "Ryan", "label": "Ryan · 少年清亮", "gender": "male"},
    {"speaker": "Aiden", "label": "Aiden · 青年清朗", "gender": "male"},
    {"speaker": "Dylan", "label": "Dylan · 青年阳光", "gender": "male"},
    {"speaker": "Eric", "label": "Eric · 沉稳磁性", "gender": "male"},
    {"speaker": "Uncle_fu", "label": "Uncle_fu · 中年浑厚", "gender": "male"},
    {"speaker": "Serena", "label": "Serena · 温柔知性", "gender": "female"},
    {"speaker": "Vivian", "label": "Vivian · 干练明亮", "gender": "female"},
    {"speaker": "Ono_anna", "label": "Ono_anna · 活泼灵动", "gender": "female"},
    {"speaker": "Sohee", "label": "Sohee · 甜美少女", "gender": "female"},
]
SPEAKER_KEYS = tuple(v["speaker"] for v in VOICE_PRESETS)
MALE_POOL = ["Ryan", "Aiden", "Dylan", "Eric", "Uncle_fu"]
FEMALE_POOL = ["Serena", "Vivian", "Ono_anna", "Sohee"]
# 说话人兜底名（非角色表中的真实角色）：当某句台词无法定位说话人时用它的默认音色。
# ⚠️ 2026-09-19 起它**不再是**「旁白补声」入口 —— 旁白通道已关闭（见 build_dub_plan 注释），
# 这里只作为「说话人识别不出来」时的音色兜底保留。
NARRATION_SPEAKER = "旁白"

LANGUAGES = ["Auto", "Chinese", "English", "Japanese", "Korean", "French",
             "German", "Spanish", "Portuguese", "Russian", "Italian"]

_FEMALE_HINTS = ("女", "少女", "姑娘", "女子", "母", "姐", "妹", "娘", "婆婆", "妃", "后",
                 "妈", "妮", "姬", "丫鬟", "圣女", "仙姑", "丫头")


class TTSError(Exception):
    """配音链路业务异常（环境不可用 / 执行失败 / 产物缺失）"""


# ===================== 基础工具 =====================

def _http_error_message(code: int, url: str, body: str, reason: str = "") -> str:
    """把 ComfyUI 的 HTTP 错误响应翻译成用户可读的中文提示（R5a）。

    背景：ComfyUI 在「工作流校验失败 / 节点参数错误」时返回 **HTTP 400**，响应体是
    形如 ``{"error": {...}, "node_errors": {"12": {"errors": [{"message": "..."}]}}}``
    的 JSON。此前 ``_http_json`` 不捕获 ``urllib.error.HTTPError`` → 用户试听时只看到
    urllib 的 ``HTTP Error 400: Bad Request``，完全不知道哪个节点、哪里错（报错不可读）。

    这里优先从响应体抽取「节点级 message」，其次 ``error`` / ``error.message``，
    最后回落 ``HTTP <code> <reason>（<url>）``。任何解析异常都吞掉，绝不让「报错翻译」
    本身再抛异常。

    Args:
        code: HTTP 状态码。
        url: 请求 URL（回落信息里带上，便于定位）。
        body: 已解码的响应体文本（可能为空）。
        reason: HTTPError 的 reason phrase（如 "Bad Request"），可空。

    Returns:
        面向用户的单行可读提示。
    """
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except Exception:  # noqa: BLE001  响应体不是 JSON → 走回落
            parsed = None

    detail = ""
    if isinstance(parsed, dict):
        # 1) node_errors：最有用 —— 指出「哪个节点、缺什么 / 参数错」
        node_errors = parsed.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            parts = []
            for nid, info in node_errors.items():
                msgs = []
                if isinstance(info, dict):
                    errs = info.get("errors")
                    if isinstance(errs, list):
                        for one in errs:
                            if isinstance(one, dict):
                                m = str(one.get("message") or one.get("details") or "").strip()
                                if m:
                                    msgs.append(m)
                            elif one:
                                msgs.append(str(one))
                    if not msgs:
                        cls = str(info.get("class_type") or "").strip()
                        if cls:
                            msgs.append(f"节点类型 {cls}")
                if msgs:
                    parts.append(f"节点 {nid}：" + "；".join(msgs))
            if parts:
                detail = "；".join(parts)
        # 2) error / error.message（部分接口把原因放在顶层 error）
        if not detail:
            err = parsed.get("error")
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("type") or "").strip()
            elif err:
                detail = str(err).strip()

    if detail:
        return f"ComfyUI 请求被拒绝（HTTP {code}）：{detail}"
    # 无可用响应体信息：回落「HTTP <code> <reason>（<url>）」
    head = f"HTTP {code}" + (f" {reason}" if reason else "")
    return f"ComfyUI 请求失败（{head}）（{url}）"


def _http_json(url: str, payload: Optional[dict] = None, timeout: int = 60):
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # R5a：ComfyUI 校验失败返回 400 + JSON（含 node_errors / error）。此前
        # urllib 直接抛 HTTPError（用户只看到 "HTTP Error 400: Bad Request"），
        # 且 `_submit` 里随后的 node_errors 解析永远跑不到（异常先抛了）。
        # 这里先读出响应体、翻译成可读 TTSError，让试听页能显示真实原因。
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001  读响应体失败也要给出可读信息
            body = ""
        raise TTSError(_http_error_message(e.code, url, body,
                                           getattr(e, "reason", "") or ""))


def _http_bytes(url: str, timeout: int = 300) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as resp:
        return resp.read()


def _http_multipart_upload(url: str, field: str, filename: str, content: bytes,
                           content_type: str = "application/octet-stream",
                           extra_fields: Optional[Dict[str, str]] = None,
                           timeout: int = 120) -> Dict:
    """用 urllib 手搓一个 multipart/form-data POST（不引入 requests 依赖）。

    用途：把参考音频上传到 ComfyUI 的 ``/upload/image``（通用 input 文件上传端点）。
    返回解析后的 JSON dict；HTTP 非 2xx 抛异常（调用方负责回落）。
    """
    boundary = "----mjscxt" + os.urandom(12).hex()
    parts: List[bytes] = []
    for k, v in (extra_fields or {}).items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
            .encode("utf-8"))
    parts.append(
        (f"--{boundary}\r\n"
         f"Content-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
         f"Content-Type: {content_type}\r\n\r\n").encode("utf-8"))
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Content-Length": str(len(body))},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw or "{}")


def probe_audio(path: str) -> Dict:
    """ffprobe 读取音频信息（时长/编码/采样率/声道/大小）"""
    info = {"path": os.path.abspath(path), "ok": False}
    if not path or not os.path.exists(path):
        info["error"] = "文件不存在"
        return info
    info["size_bytes"] = os.path.getsize(path)
    info["size_mb"] = round(info["size_bytes"] / 1048576, 3)
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_name,sample_rate,channels",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if r.returncode != 0:
            info["error"] = (r.stderr or "ffprobe 失败").strip()[:200]
            return info
        d = json.loads(r.stdout or "{}")
        fmt = d.get("format") or {}
        st = (d.get("streams") or [{}])[0]
        info.update({
            "ok": True,
            "duration": round(float(fmt.get("duration") or 0), 3),
            "codec": st.get("codec_name"),
            "sample_rate": st.get("sample_rate"),
            "channels": st.get("channels"),
        })
    except Exception as e:  # pragma: no cover - 环境相关
        info["error"] = f"{type(e).__name__}: {e}"
    return info


def concat_audio(paths: List[str], output_path: str, fmt: str = "wav") -> str:
    """把多个音频片段按顺序合并为一个音轨（ffmpeg concat filter，重编码保证兼容）"""
    paths = [p for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        raise TTSError("没有可合并的音频片段")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    cmd = ["ffmpeg", "-y"]
    for p in paths:
        cmd += ["-i", p]
    filt = "".join(f"[{i}:a]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]"]
    cmd += ["-c:a", "libmp3lame", "-q:a", "2", output_path] if fmt == "mp3" \
        else ["-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1", output_path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    if r.returncode != 0:
        raise TTSError(f"音频合并失败: {(r.stderr or '').strip()[:300]}")
    return os.path.abspath(output_path)


def safe_name(name: str, limit: int = 40) -> str:
    keep = [c for c in str(name or "") if c.isalnum() or c in "_-" or "\u4e00" <= c <= "\u9fff"]
    return "".join(keep)[:limit] or "line"


def stable_seed(key: str) -> int:
    """由角色名派生的稳定种子：同一角色在任何一次运行中都得到同一音色参数"""
    h = hashlib.md5(str(key).encode("utf-8")).hexdigest()
    return int(h[:8], 16) % (2 ** 31 - 1) or 12345


def guess_gender(text: str) -> str:
    s = str(text or "")
    if any(k in s for k in _FEMALE_HINTS):
        return "female"
    return "male"


# ===================== 环境自检 =====================

def check_environment(comfyui_url: str = COMFYUI_URL) -> Dict:
    """核查 ComfyUI 侧 Qwen-TTS 节点与模型权重，给出可用/缺失结论（不臆测）"""
    result = {
        "available": False, "reasons": [], "nodes": {}, "speakers": list(SPEAKER_KEYS),
        "comfyui_online": False, "model_root": TTS_MODEL_ROOT,
        "model_dirs": [], "expected_models": [], "default_params": dict(TTS_DEFAULT_PARAMS),
    }
    try:
        info = _http_json(f"{comfyui_url.rstrip('/')}/object_info", timeout=60)
        result["comfyui_online"] = True
    except Exception as e:
        result["reasons"].append(f"ComfyUI 不可访问（{comfyui_url}）：{type(e).__name__} {e}")
        return result

    for n in REQUIRED_NODES:
        result["nodes"][n] = n in info
    for n in OPTIONAL_NODES:
        result["nodes"][n] = n in info
    missing_nodes = [n for n in REQUIRED_NODES if not result["nodes"].get(n)]
    if missing_nodes:
        result["reasons"].append("缺少 Qwen-TTS 节点：" + "、".join(missing_nodes))

    node = info.get("FB_Qwen3TTSCustomVoice") or {}
    try:
        speakers = node["input"]["required"]["speaker"][0]
        if isinstance(speakers, list) and speakers:
            result["speakers"] = speakers
    except Exception as e:
        logger.debug("speakers 字段取值失败（忽略）：%s", e)

    # 模型权重：支持显式目录 + 多个常见 ComfyUI 布局，避免模块导入期路径被卡死。
    # 优先顺序：MJSCXT_TTS_MODEL_ROOT > 当前 MODELS_DIR > ComfyUI portable/standard 布局 > 项目内 models/qwen-tts。
    explicit_root = os.environ.get("MJSCXT_TTS_MODEL_ROOT", "").strip()
    model_root_candidates = [explicit_root, TTS_MODEL_ROOT]
    for base in (MODELS_DIR, os.path.join(MODELS_DIR, ".."), PROJECT_ROOT_DIR):
        if not base:
            continue
        model_root_candidates.append(os.path.normpath(os.path.join(base, "qwen-tts")))
        model_root_candidates.append(os.path.normpath(os.path.join(base, "ComfyUI", "ComfyUI", "models", "qwen-tts")))
        model_root_candidates.append(os.path.normpath(os.path.join(base, "ComfyUI", "models", "qwen-tts")))

    resolved_model_root = ""
    seen = set()
    for cand in model_root_candidates:
        if not cand or cand in seen:
            continue
        cand = os.path.normpath(cand)
        seen.add(cand)
        if not os.path.isdir(cand):
            continue
        children = []
        try:
            children = [x for x in os.listdir(cand) if os.path.isdir(os.path.join(cand, x))]
        except OSError:
            continue
        if children:
            resolved_model_root = cand
            result["model_dirs"] = sorted(children)
            break
    result["model_root"] = resolved_model_root or TTS_MODEL_ROOT

    if not resolved_model_root:
        result["reasons"].append(f"模型根目录不存在（已尝试：{', '.join(sorted(x for x in seen if x))}）")

    found_names = " ".join(result["model_dirs"]).lower()
    for expect, desc in TTS_MODEL_EXPECTED:
        hit = [d for d in result["model_dirs"] if expect.split("Qwen3-TTS-")[-1].lower() in d.lower()] \
            or ([d for d in result["model_dirs"] if expect.lower() in d.lower()] if expect.lower() in found_names else [])
        result["expected_models"].append({
            "name": expect, "desc": desc, "found": bool(hit), "matched": hit,
        })
    custom_ok = any(m["found"] for m in result["expected_models"] if "CustomVoice" in m["name"])
    if not custom_ok and resolved_model_root:
        result["reasons"].append(f"未找到 CustomVoice 预置音色权重（{resolved_model_root} 下）")

    result["available"] = not result["reasons"]
    return result


def list_voices() -> Dict:
    return {
        "speakers": VOICE_PRESETS,
        "languages": LANGUAGES,
        "model_choices": ["1.7B", "0.6B"],
        "modes": [
            {"key": "preset", "label": "预置音色（9 个内置音色，稳定）"},
            {"key": "design", "label": "音色设计（按中文描述生成音色，可自定义）"},
            {"key": "clone", "label": "参考音频克隆（上传一段该角色的音频，复刻其音色）"},
        ],
        "clone_supported": True,
        "clone_hint": "参考音频 3–15 秒、单人、无背景音乐最佳；"
                      "若填了「参考音频原文」，克隆相似度会明显更高。",
    }


def clone_available(comfyui_url: str = COMFYUI_URL) -> bool:
    """ComfyUI 侧是否具备参考音频克隆节点（FB_Qwen3TTSVoiceClone + LoadAudio）。"""
    try:
        info = _http_json(f"{comfyui_url.rstrip('/')}/object_info", timeout=20)
    except Exception:
        return False
    return "FB_Qwen3TTSVoiceClone" in info and "LoadAudio" in info


# ===================== 配音计划（角色 → 音色） =====================

# 中性情绪：这类词出现时没必要切 design 模式（切了反而可能让音色漂移，白花时间）
_NEUTRAL_EMOTIONS = ("平静", "平靜", "平常", "正常", "无", "無", "普通", "一般", "",
                     "中性", "陈述", "陳述", "陈述句")

# 情绪 → 语气描述（给 VoiceDesign 节点的中文指令）
_EMOTION_HINTS = {
    "愤怒": "愤怒地咬牙说出，语速偏快、声音发紧",
    "生气": "带怒气地说，语气冲",
    "低沉": "压低声音，语速缓慢、语气沉重",
    "悲伤": "带着哭腔，声音颤抖、语速慢",
    "难过": "声音低哑，带着失落",
    "惊喜": "语气上扬，带着意外的兴奋",
    "兴奋": "语速快、音调高，情绪饱满",
    "紧张": "语速急促、声音发紧，带着不安",
    "恐惧": "声音发抖、气息不稳，带着害怕",
    "害怕": "声音发抖、怯懦",
    "冷静": "语气平稳克制，不带起伏",
    "坚定": "语气斩钉截铁，字字有力",
    "温柔": "语气温柔舒缓，带着笑意",
    "疑惑": "语气上扬，带着疑问",
    "嘲讽": "带着讽刺与轻蔑，语调拖长",
    "疲惫": "气息虚弱，语速缓慢",
    "焦急": "语速很快，透着急切",
}


def _emotion_aware() -> bool:
    """是否启用情绪化配音（TTS_DEFAULT_PARAMS.emotion_aware，默认开）"""
    try:
        return bool(TTS_DEFAULT_PARAMS.get("emotion_aware", True))
    except Exception:  # noqa: BLE001
        return True


def _is_neutral_emotion(emotion: str) -> bool:
    e = str(emotion or "").strip()
    if not e:
        return True
    return any(k and k in e for k in _NEUTRAL_EMOTIONS)


def _emotion_instruct(emotion: str, char_desc: str = "") -> str:
    """把「角色音色底稿 + 该镜情绪」拼成 VoiceDesign 的 instruct。

    角色描述放前面用于稳住音色，情绪描述放后面控制语气 —— 顺序反了会
    让模型把情绪当成音色特征，导致同一角色每句听起来都不像同一个人。
    """
    e = str(emotion or "").strip()
    hint = ""
    for k, v in _EMOTION_HINTS.items():
        if k in e:
            hint = v
            break
    if not hint:
        hint = f"语气：{e}"
    desc = str(char_desc or "").strip()
    # 角色描述可能自带「，」结尾，这里统一裁剪避免重复标点
    return f"{desc}；{hint}".strip("；， ").strip("；") if desc else hint


def _pool_gender(ch: dict) -> str:
    """角色池分配时的性别判据：按 voice_style/appearance/personality/name 文本线索猜性别。

    与旧实现同源（guess_gender 命中女声线索词则 female，否则 male），抽成独立 helper
    供「预分配池位」与「建表」两处共用，保证口径一致。
    """
    hint = " ".join(str(ch.get(k) or "") for k in
                    ("voice_style", "appearance", "personality", "name", "description"))
    return "female" if guess_gender(hint) == "female" else "male"


def _assign_pool_speakers(needs_pool: Dict[str, str]) -> Dict[str, str]:
    """把「需要池分配」的角色映射到确定性的 speaker（**与输入顺序无关**）。

    P2-3 修复：旧实现按角色在输入列表里**出现的先后**递增取模（``pool[used_count % len(pool)]``），
    导致同一角色跨集/跨 bible 列表顺序变化时落到不同 speaker（音色漂移）。这里改为：
    每个性别组内**按角色名排序**后取第 k 位，第 k 个男性恒取 ``MALE_POOL[k % len(MALE_POOL)]``、
    第 k 个女性恒取 ``FEMALE_POOL[k % len(FEMALE_POOL)]`` —— 只取决于「本性别组里我排第几」，
    与「谁先谁后」无关，跨集恒稳。

    ``needs_pool``：``{角色名: "male"|"female"}``（仅**未**显式绑定 speaker 的角色；
    显式 tts_voice/speaker 由调用方在取用本结果前剔除）。返回 ``{角色名: speaker}``。
    """
    male_names = sorted(n for n, g in needs_pool.items() if g == "male")
    female_names = sorted(n for n, g in needs_pool.items() if g == "female")
    out: Dict[str, str] = {}
    for k, nm in enumerate(male_names):
        out[nm] = MALE_POOL[k % len(MALE_POOL)]
    for k, nm in enumerate(female_names):
        out[nm] = FEMALE_POOL[k % len(FEMALE_POOL)]
    return out


def default_voice_map(characters: List[dict], project: str = "", episode: int = 1) -> Dict:
    """按剧本角色生成默认音色映射：同角色固定 speaker + seed（全剧一致）

    优先采用角色自带的 tts_voice / speaker 字段；否则按性别线索从音色池**确定性**分配
    （P2-3：池位按「性别组内按名字排序的第几位」取模，与输入顺序无关，跨集恒稳）。
    """
    # 去重（按名字，首次出现为准）+ 收集显式绑定 / 需池分配名单
    ordered: Dict[str, dict] = {}
    for idx, ch in enumerate(characters or []):
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or f"角色{idx + 1}")
        if name not in ordered:
            ordered[name] = ch

    # 预分配：未显式绑定合法 speaker 的角色进入池分配（按性别组名字排序取模，顺序无关）
    needs_pool: Dict[str, str] = {}
    for name, ch in ordered.items():
        explicit = str(ch.get("tts_voice") or ch.get("speaker") or "").strip()
        if explicit not in SPEAKER_KEYS:
            needs_pool[name] = _pool_gender(ch)
    pool_speakers = _assign_pool_speakers(needs_pool)

    chars: Dict[str, Dict] = {}
    for name, ch in ordered.items():
        explicit = str(ch.get("tts_voice") or ch.get("speaker") or "").strip()
        if explicit in SPEAKER_KEYS:
            speaker = explicit
        else:
            speaker = pool_speakers.get(name, SPEAKER_KEYS[0])
        voice_style = str(ch.get("voice_style") or "").strip()
        chars[name] = {
            # mode="auto"：**未显式选择**（默认派生）。build_dub_plan 见到 auto 才会
            # 允许被 voice_bank 的参考音频改写为 clone；用户显式存过的模式不受影响。
            # normalize_voice 会把 auto 归一为 preset（下游拿到的永远是具体模式）。
            "mode": "auto",
            "speaker": speaker,
            "instruct": voice_style,          # 仅 design 模式生效；preset 下作为参考描述保存
            "seed": stable_seed(f"{project}|{name}"),
            "model_choice": TTS_DEFAULT_PARAMS["model_choice"],
            "language": TTS_DEFAULT_PARAMS["language"],
        }
    return {
        "project": project,
        "episode": episode,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "characters": chars,
        "lines": {},
    }


def normalize_voice(raw: Optional[dict], fallback: Optional[dict] = None) -> Dict:
    """校验并归一化单个音色配置（非法 speaker 直接报错，不静默回退）

    三种模式：
      · ``preset`` —— 9 个预置 speaker（稳定，默认）；
      · ``design`` —— 中文音色描述驱动 VoiceDesign；
      · ``clone``  —— 参考音频克隆（``ref_audio`` 为参考音频路径，``ref_text`` 可选
        且**强烈建议**填——它是参考音频里实际说出的那句话，填了克隆相似度显著更高）。
    """
    base = dict(fallback or {})
    raw = raw or {}
    _mode = raw.get("mode") or base.get("mode") or "preset"
    # "auto" 是「默认派生、未显式选择」的内部标记（见 default_voice_map）。
    # 归一为 preset —— 合成端拿到的永远是具体模式，不必认识 auto。
    if _mode == "auto":
        _mode = "preset"
    voice = {
        "mode": _mode,
        "speaker": raw.get("speaker") or base.get("speaker") or SPEAKER_KEYS[0],
        "instruct": raw.get("instruct", base.get("instruct", "")) or "",
        "seed": int(raw.get("seed") if raw.get("seed") is not None else (base.get("seed") or 0)),
        "model_choice": raw.get("model_choice") or base.get("model_choice") or TTS_DEFAULT_PARAMS["model_choice"],
        "language": raw.get("language") or base.get("language") or TTS_DEFAULT_PARAMS["language"],
        "temperature": float(raw.get("temperature") or base.get("temperature") or TTS_DEFAULT_PARAMS["temperature"]),
    }
    # clone 两个字段：即使当前不是 clone 模式也原样带着（切模式时不丢配置）
    _ref_audio = raw.get("ref_audio", base.get("ref_audio", "")) or ""
    _ref_text = raw.get("ref_text", base.get("ref_text", "")) or ""
    if _ref_audio:
        voice["ref_audio"] = str(_ref_audio)
    if _ref_text:
        voice["ref_text"] = str(_ref_text)
    if voice["mode"] not in ("preset", "design", "clone"):
        raise TTSError(f"不支持的音色模式：{voice['mode']}")
    if voice["mode"] == "preset" and voice["speaker"] not in SPEAKER_KEYS:
        raise TTSError(f"不支持的预置音色：{voice['speaker']}（可选：{'、'.join(SPEAKER_KEYS)}）")
    if voice["mode"] == "design" and not str(voice["instruct"]).strip():
        raise TTSError("音色设计模式需要提供音色描述（instruct）")
    if voice["mode"] == "clone":
        # ⚠️ 这里**不因 ref_audio 缺失而报错** —— 存量 voice_map 里可能存的是
        # 一个已被删除的参考音频，报错会让整张 voice_map 存不回去。由合成端
        # 回落 preset 并记账（见 synthesize_batch 的 clone_fallback）。
        if not str(voice.get("ref_audio") or "").strip():
            logger.warning("音色为 clone 模式但未提供 ref_audio，合成时将回落预置音色")
    if voice["model_choice"] not in ("1.7B", "0.6B"):
        raise TTSError(f"不支持的模型规格：{voice['model_choice']}")
    return voice


def clean_line_text(text, character="") -> str:
    """清洗台词：去除引号/说话人前缀/舞台提示括号，保留可朗读文本"""
    s = str(text or "").strip()
    for q in ("“", "”", "\"", "「", "」", "『", "』"):
        s = s.replace(q, "")
    if character:
        for pat in (f"{character}：", f"{character}:", f"{character} :"):
            if s.startswith(pat):
                s = s[len(pat):].strip()
    s = s.replace("（", "(").replace("）", ")")
    while "(" in s and ")" in s:
        a, b = s.find("("), s.find(")")
        if a == -1 or b < a:
            break
        s = (s[:a] + s[b + 1:]).strip()
    return s.strip()


def _first_present(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def build_dub_plan(script: Dict, voice_map: Optional[Dict] = None,
                   project: str = "", episode: int = 1,
                   shot_ids: Optional[List] = None,
                   only_missing: bool = False,
                   out_dir_wav: str = "") -> Dict:
    """由剧本 + 音色映射生成逐句配音计划

    默认取该镜头出场角色中的第一位作为说话人（shots[].characters_in_shot），
    可在 voice_map["lines"][<shot_id>] 中按句覆盖角色 / 音色 / seed。
    """
    characters = script.get("characters") or []
    # S-03：episode 也做 int 归一化兜底（调用方可能传字符串/None）
    try:
        episode = int(episode)
    except (TypeError, ValueError):
        episode = 1
    vmap = json.loads(json.dumps(voice_map or default_voice_map(characters, project, episode),
                                 ensure_ascii=False))
    vmap.setdefault("characters", {})
    vmap.setdefault("lines", {})

    # ---- 参考音频克隆（voice_bank）----
    # 某角色在 voice_bank 里登记过参考音频 → 其音色自动切 clone 模式（除非 voice_map
    # 里**显式**指定了其它模式，显式优先）。dub_dir 由 out_dir_wav 反推（parent），
    # 这样不用改 build_dub_plan 的签名（4 处调用方零改动）。
    _dub_dir = os.path.dirname(os.path.abspath(out_dir_wav)) if out_dir_wav else ""
    _bank_used: Dict[str, str] = {}
    if _dub_dir:
        for _name, _v in list(vmap["characters"].items()):
            if not isinstance(_v, dict):
                continue
            # mode=="auto" = 默认映射（无用户显式选择）→ 允许被 voice_bank 改写为 clone。
            # 用户显式存过 preset/design/clone 的一律尊重，不覆盖。
            if _v.get("mode") and _v.get("mode") != "auto":
                continue
            _ref, _rt = find_voice_bank_ref(_dub_dir, _name)
            if _ref:
                _v["mode"] = "clone"
                _v["ref_audio"] = _ref
                if _rt and not _v.get("ref_text"):
                    _v["ref_text"] = _rt
                _bank_used[_name] = _ref
            else:
                _v["mode"] = "preset"
    
    # 角色音色底稿：情绪化配音时固定这部分，只让「情绪」变，避免每句音色漂移
    _char_desc = {}
    for _ch in characters:
        _nm = str(_ch.get("name") or "").strip()
        if not _nm:
            continue
        _char_desc[_nm] = "，".join(
            str(_ch.get(k) or "").strip() for k in
            ("voice_style", "personality", "appearance", "description")
            if str(_ch.get(k) or "").strip())[:120]

    # 逐句角色覆盖：voice_map["lines"][str(shot_id)] = {"character": "...", ...}
    lines: List[Dict] = []
    legacy_narration: List[str] = []   # 旧剧本里「只剩旁白、没有台词」的镜头（旁白通道已关闭，会被记警告）
    for shot in script.get("shots") or []:
        shot_id = shot.get("shot_id")
        # S-03：shot_id 归一为 int（剧本 schema 里是 int，但旧版/手动编辑可能给字符串）。
        # 直接 int(shot_id) 遇 "abc" / None 抛 ValueError → 配音计划 500。统一兜底 0。
        try:
            _sid_int = int(shot_id) if shot_id is not None else 0
        except (TypeError, ValueError):
            _sid_int = 0
        if shot_ids and str(shot_id) not in [str(s) for s in shot_ids]:
            continue
        cast = shot.get("characters_in_shot") or shot.get("characters") or []
        # 台词来源：结构化 dialogue（[{speaker,text}]）优先，旧剧本回退 dialogue_text / 字符串推断
        dlg_rows = _norm_dlg_lines(shot.get("dialogue"), characters, cast)
        if not dlg_rows and shot.get("dialogue_text"):
            dlg_rows = _norm_dlg_lines(shot.get("dialogue_text"), characters, cast)
        # ---- 旁白通道已关闭（2026-09-19）----
        # 原实现会在这里把 narration（心理活动/背景补叙/环境描写）用默认「旁白」音色补声，
        # 目的是让「无台词但有旁白」的镜头不成片无声。但旁白被当成原文叙述的公共出口后，
        # 一句句画外音解说把成片彻底淹没（实测 ep04 旁白 2231 字 ≈ 496 秒铺在 100 秒画面上，
        # 尾部被 `-shortest` 静默截掉）。现在产品侧已决定「成片不产出旁白」：
        # 剧本阶段不再写 narration（novel_to_script.REWRITE_RULES 第 8 条），配音链路也不再念它。
        # 旧剧本里残留的 narration 会被显式记账（见下方 legacy_narration），不静默丢弃。
        if not dlg_rows and clean_line_text(shot.get("narration")):
            legacy_narration.append(str(shot_id))
        override = (vmap.get("lines") or {}).get(str(shot_id)) or {}
        multi = len(dlg_rows) > 1
        for li, row in enumerate(dlg_rows):
            text = clean_line_text(row.get("text"))
            if not text:
                continue
            dlg_speaker = row.get("speaker") or ""
            char_name = _first_present(
                override.get("character"), dlg_speaker,
                shot.get("dialogue_speaker"), shot.get("speaker"), shot.get("character"),
                cast[0] if cast else "", NARRATION_SPEAKER)
            base_voice = vmap["characters"].get(char_name) or default_voice_map(
                [{"name": char_name}], project, episode)["characters"].get(char_name)
            voice = normalize_voice(override, base_voice)
            # ---- 情绪化配音 ----
            # 剧本每镜都带 emotion 字段（如「愤怒 / 低沉 / 惊喜」），但此前**完全没被
            # 送进 TTS**：每句都用角色级固定 preset 音色，于是所有台词听起来一个调。
            # 这里把该镜情绪拼进 instruct，并在确实有情绪时切到 design 模式
            # （preset 模式下 CustomVoice 节点会把 instruct 当备注忽略，只有
            #  VoiceDesign 节点才真正按 instruct 控制语气）。
            emotion = str(shot.get("emotion") or "").strip()
            if emotion and _emotion_aware() and not _is_neutral_emotion(emotion):
                desc = _char_desc.get(char_name) or ""
                _instr = _emotion_instruct(emotion, desc)
                # ⚠️⭐ 2026-10-08 修复（用户实测「音色不对」）：**有角色参考音色时
                #     必须保持 clone**，情绪只写进 instruct —— 本节点
                #     （FB_Qwen3TTSVoiceClone）的入参表里**就有 instruct**。
                #     旧实现在这里无条件切 design，等于把上面刚按 voice_bank 定好的
                #     克隆音色整个丢掉，改由一段中文音色描述现「造」一个嗓音：
                #       · 同一角色逐句音色漂移（每句的 emotion 不同 → 描述不同）；
                #       · 与用户上传/约定的角色嗓音毫无关系。
                #     只有「没有参考音色」的角色才退回 design。
                if str(voice.get("mode") or "") == "clone":
                    voice = dict(voice, mode="clone", instruct=_instr)
                else:
                    voice = dict(voice, mode="design", instruct=_instr)
            suffix = f"_{li + 1}" if multi else ""
            out_name = (f"ep{int(episode):02d}_shot{_sid_int:02d}{suffix}"
                        f"_{safe_name(char_name, 12)}.wav")
            out_path = os.path.join(out_dir_wav, out_name) if out_dir_wav else out_name
            if only_missing and out_dir_wav and os.path.exists(out_path) \
                    and os.path.getsize(out_path) > 0:
                continue
            lines.append({
                "line_id": f"ep{int(episode):02d}_shot{_sid_int:02d}{suffix}",
                "shot_id": shot_id,
                "character": char_name,
                "text": text,
                # 配音行一律来自剧本 dialogue（旁白通道已于 2026-09-19 关闭）
                "source": "dialogue",
                "duration_hint": shot.get("duration"),
                "emotion": emotion,
                "voice": voice,
                "out_name": out_name,
                "out_path": os.path.abspath(out_path) if out_dir_wav else "",
            })

    chars_out = []
    for ch in characters:
        name = str(ch.get("name") or "")
        voice = normalize_voice(vmap["characters"].get(name), None) if name in vmap["characters"] else None
        used = sum(1 for l in lines if l["character"] == name)
        chars_out.append({
            "name": name,
            "voice_style": ch.get("voice_style") or "",
            "voice": voice,
            "line_count": used,
        })
    warns: List[str] = []
    if legacy_narration:
        warns.append(
            f"有 {len(legacy_narration)} 个镜头只有旁白、没有台词（镜头 {', '.join(legacy_narration[:8])}"
            f"{' 等' if len(legacy_narration) > 8 else ''}）：旁白通道已关闭，这些镜头不会产出配音，"
            f"成片会留白。这批剧本是旁白改造前生成的，建议重新生成剧本"
            f"（原文的心理活动应当改写成该角色的自语台词）。")
    return {
        "project": project, "episode": episode,
        "characters": chars_out,
        "lines": lines,
        "line_count": len(lines),
        "warnings": warns,
        "voice_map": vmap,
    }


def save_voice_map(voice_map: Dict, path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    data = dict(voice_map or {})
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.abspath(path)


def load_voice_map(path: str) -> Optional[Dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"音色映射读取失败 {path}: {e}")
        return None


# ===================== 参考音频音色库（voice_bank） =====================
# 每个角色可以挂一段参考音频（+ 该音频里说的话）作为克隆源。落盘结构：
#   <dub_dir>/voice_bank/<角色安全名>/ref.<ext>   ← 参考音频本体（用户上传的）
#   <dub_dir>/voice_bank/<角色安全名>/ref.json    ← {ref_text, original_filename, ...}
# 参考音频**复制**进 bank（不引用临时上传目录）—— 用户下次跑配音时临时文件早已清掉。

#: 参考音频允许的扩展名 / 时长建议区间（秒）
VOICE_BANK_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac")
VOICE_BANK_MIN_SEC = 1.0
VOICE_BANK_MAX_SEC = 60.0


def voice_bank_dir(dub_dir: str, character: str) -> str:
    """某角色的参考音频存放目录"""
    return os.path.join(dub_dir, "voice_bank", safe_name(character, 40) or "unknown")


def find_voice_bank_ref(dub_dir: str, character: str) -> Tuple[str, str]:
    """查找某角色已登记的参考音频，返回 ``(音频绝对路径, ref_text)``。

    找不到返回 ``("", "")``。只认第一个命中的音频文件（扩展名白名单内），
    配套 ref.json 读 ref_text（缺失或损坏都只当空串，不影响音频本身可用）。
    """
    d = voice_bank_dir(dub_dir, character)
    if not os.path.isdir(d):
        return "", ""
    ref_text = ""
    meta_path = os.path.join(d, "ref.json")
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                ref_text = str((json.load(f) or {}).get("ref_text") or "")
        except Exception as e:  # noqa: BLE001 元数据坏掉不影响音频可用
            logger.warning("参考音频元数据读取失败 %s：%s", meta_path, e)
    try:
        for fn in sorted(os.listdir(d)):
            if os.path.splitext(fn)[1].lower() in VOICE_BANK_EXTS:
                p = os.path.join(d, fn)
                if os.path.getsize(p) > 0:
                    return os.path.abspath(p), ref_text
    except OSError as e:
        logger.warning("参考音频目录不可读 %s：%s", d, e)
    return "", ""


def save_voice_bank_ref(dub_dir: str, character: str, src_path: str,
                        ref_text: str = "", original_filename: str = "") -> str:
    """把一段参考音频登记到该角色的 voice_bank（覆盖旧的），返回音频落盘路径。"""
    d = voice_bank_dir(dub_dir, character)
    os.makedirs(d, exist_ok=True)
    ext = os.path.splitext(src_path)[1].lower() or ".wav"
    if ext not in VOICE_BANK_EXTS:
        ext = ".wav"
    dst = os.path.join(d, f"ref{ext}")
    # 先清掉该角色其它格式的旧参考（换格式时不留下两份，find 会取到旧的那份）
    for fn in os.listdir(d):
        if os.path.splitext(fn)[1].lower() in VOICE_BANK_EXTS and os.path.join(d, fn) != dst:
            try:
                os.remove(os.path.join(d, fn))
            except OSError as e:
                logger.warning("清理旧参考音频失败 %s：%s", fn, e)
    shutil.copy2(src_path, dst)
    save_voice_map({
        "character": character,
        "ref_text": str(ref_text or "").strip(),
        "original_filename": original_filename or os.path.basename(src_path),
        "duration_sec": (probe_audio(dst) or {}).get("duration"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, os.path.join(d, "ref.json"))
    return os.path.abspath(dst)


def list_voice_bank(dub_dir: str) -> List[Dict]:
    """列出该项目已登记参考音频的所有角色（供前端展示绑定状态）。"""
    root = os.path.join(dub_dir, "voice_bank")
    out: List[Dict] = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        ref, ref_text = find_voice_bank_ref(dub_dir, name)
        if not ref:
            continue
        meta = {}
        try:
            with open(os.path.join(d, "ref.json"), "r", encoding="utf-8") as f:
                meta = json.load(f) or {}
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({
            "character": name,
            "file": ref,
            "exists": True,
            "ref_text": ref_text,
            "duration_sec": meta.get("duration_sec"),
            "original_filename": meta.get("original_filename") or os.path.basename(ref),
            "updated_at": meta.get("updated_at"),
        })
    return out


# ===================== 合成客户端 =====================

class QwenTTSClient:
    """通过 ComfyUI API 调用 Qwen-TTS 节点做真实语音合成"""

    def __init__(self, comfyui_url: str = COMFYUI_URL,
                 out_root: str = None, params: Dict = None):
        self.comfyui_url = comfyui_url.rstrip("/")
        self.params = dict(TTS_DEFAULT_PARAMS)
        if params:
            self.params.update(params)
        self.out_root = out_root

    # ---------- 低层 ----------

    def _keep_loaded(self) -> bool:
        """是否需要「模型常驻」：批次结束后不卸载、也不发 /free。

        读取顺序：显式传入的 params > 全局 config.KEEP_MODEL_LOADED
        （环境变量 MJSCXT_KEEP_MODEL_LOADED，默认 True）。
        """
        v = self.params.get("keep_model_loaded", KEEP_MODEL_LOADED)
        return bool(v)

    def _unload_model(self, self_task_id: str = "") -> None:
        """请求 ComfyUI 卸载已缓存的 TTS 模型（释放 GPU 显存）

        G-03：synthesize_batch 中途失败（_wait 超时/执行错误）时，
        前面已加载的模型未卸载（只有最后一句节点设了 unload_model_after_generate）。
        批量 TTS 是高频链路，长期 autopilot 下显存累积泄漏 → 后续任务 OOM。
        用 /free 接口（与 upscale_client 同口径）主动卸载。失败只告警不抛。

        B-01 P1-12：/free 互斥守卫——本进程有其它 running GPU 任务时**跳过** /free，
        避免卸掉分镜/视频/关键帧等其它任务正在使用的模型（反复换入换出）。

        ⚠️ 默认已不再调用本方法（config.KEEP_MODEL_LOADED=True，见 config 注释）：
        /free 的 unload_models 会走 unload_all_models() → cleanup_models_gc() →
        gc.collect()，把模型**连 RAM 里的权重一起释放**，下次生成要从磁盘重读
        19.5GB 模型。显存不够时 ComfyUI 会自行把暂不用的模型 offload 到 RAM。
        """
        # 模型常驻开关：默认保留模型，不再走 /free
        if self._keep_loaded():
            logger.info("QwenTTS 模型常驻（keep_model_loaded），跳过 /free 卸载")
            return
        # B-01 P1-12：/free 互斥守卫
        try:
            import gpu_task_gate
            if gpu_task_gate.has_other_running_gpu_tasks(self_task_id):
                logger.info("B-01 /free 守卫：本进程有其它 running GPU 任务，跳过 /free（避免卸他人模型）")
                return
        except Exception as e:  # noqa: BLE001  守卫失败不阻断卸载主流程
            # 级别用 warning 而非 debug：本守卫的作用正是「避免卸掉分镜/视频/关键帧等
            # 其它任务正在使用的模型」，检查失败 = 互斥保护失效、可能误卸他人模型，
            # 属安全类事件；且生产 root 级别为 WARNING，debug 会完全不可见。
            # （与紧邻的「/free 请求失败」保持同级，避免出现级别倒挂。）
            logger.warning("B-01 /free 互斥守卫检查失败（按「无其它任务」继续，"
                           "可能误卸其它任务模型）：%s", e)
        try:
            req = urllib.request.Request(
                f"{self.comfyui_url}/free",
                data=json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            logger.info("QwenTTS 模型已请求卸载（/free，释放显存）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"QwenTTS 模型卸载请求失败（不影响结果，仅显存未释放）：{e}")

    def _submit(self, prompt: Dict) -> str:
        # 提交前「模型名对齐」：与 comfyui_client.queue_prompt 同一收口（见 comfyui_models
        # 模块文末「运行时模型名对齐」）。TTS 工作流同样含模型文件型控件，写死的名字换机器
        # 就会失效 → 这里按本机 object_info 对齐（拉不到就跳过，绝不阻断配音）。
        try:
            import comfyui_models
            comfyui_models.align_prompt_models(prompt, base_url=self.comfyui_url, tag="TTS")
        except Exception as _ma_err:  # noqa: BLE001
            logger.warning("TTS 模型名对齐失败（按原样提交）：%s", _ma_err)
        r = _http_json(f"{self.comfyui_url}/prompt",
                       {"prompt": prompt, "client_id": f"dub-{int(time.time())}"})
        if not r.get("prompt_id"):
            raise TTSError(f"ComfyUI 未返回 prompt_id：{json.dumps(r, ensure_ascii=False)[:300]}")
        if r.get("node_errors"):
            raise TTSError(f"工作流校验失败：{json.dumps(r['node_errors'], ensure_ascii=False)[:400]}")
        return r["prompt_id"]

    def _wait(self, prompt_id: str, timeout: int,
              progress_cb: Optional[Callable[[str], None]] = None) -> Dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                h = _http_json(f"{self.comfyui_url}/history/{prompt_id}", timeout=60)
            except Exception:
                time.sleep(3)
                continue
            if prompt_id in h:
                entry = h[prompt_id]
                status = entry.get("status") or {}
                if status.get("status_str") == "error" or not status.get("completed", True):
                    msgs = status.get("messages") or []
                    detail = ""
                    for m in msgs:
                        if isinstance(m, list) and m and m[0] in ("execution_error", "execution_interrupted"):
                            detail = json.dumps(m[1] if len(m) > 1 else m, ensure_ascii=False)[:400]
                    raise TTSError(f"ComfyUI 执行失败：{detail or status.get('status_str')}")
                return entry
            if progress_cb:
                progress_cb(f"ComfyUI 合成中（{int(time.time() - t0)}s）")
            time.sleep(3)
        raise TTSError(f"配音超时（>{timeout}s），prompt_id={prompt_id}")

    def _download_and_convert(self, item: dict, raw_info: dict,
                              out_path: str) -> Dict:
        q = urllib.parse.urlencode({
            "filename": raw_info.get("filename"),
            "subfolder": raw_info.get("subfolder") or "",
            "type": raw_info.get("type") or "output",
        })
        blob = _http_bytes(f"{self.comfyui_url}/view?{q}", timeout=600)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        suffix = os.path.splitext(raw_info.get("filename") or ".flac")[1] or ".flac"
        fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="tts_raw_")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
            cmd = ["ffmpeg", "-y", "-i", tmp, "-ar", "24000", "-ac", "1",
                   "-c:a", "pcm_s16le", out_path]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            if r.returncode != 0:
                raise TTSError(f"音频转码失败: {(r.stderr or '').strip()[:300]}")
        finally:
            try:
                os.remove(tmp)
            except OSError as e:
                logger.debug("清理临时文件失败（忽略）：%s", e)
        info = probe_audio(out_path)
        if not info.get("ok") or (info.get("duration") or 0) <= 0:
            raise TTSError(f"产物音频无效：{out_path}（{info.get('error') or '时长为 0'}）")
        return info

    # ---------- 高层 ----------

    def _node_inputs(self, text: str, voice: dict, is_last: bool,
                     ref_node: Optional[str] = None) -> Dict:
        """构造一个 TTS 合成节点的 inputs。

        :param ref_node: clone 模式下 `LoadAudio` 节点的 id（其 AUDIO 输出接 ref_audio）。
            为 None 时 clone 模式不可用（调用方应已回落 preset）。
        """
        unload = (not self._keep_loaded()) and is_last
        common = {
            "model_choice": voice.get("model_choice") or self.params["model_choice"],
            "device": self.params.get("device", "auto"),
            "precision": self.params.get("precision", "bf16"),
            "language": voice.get("language") or self.params["language"],
            "seed": int(voice.get("seed") or 0),
            "max_new_tokens": int(self.params.get("max_new_tokens", 2048)),
            "top_p": float(self.params.get("top_p", 0.8)),
            "top_k": int(self.params.get("top_k", 20)),
            "temperature": float(voice.get("temperature") or self.params.get("temperature", 1.0)),
            "repetition_penalty": float(self.params.get("repetition_penalty", 1.05)),
            "attention": self.params.get("attention", "auto"),
            "unload_model_after_generate": bool(unload),
        }
        mode = voice.get("mode")
        if mode == "clone":
            # 参考音频克隆：FB_Qwen3TTSVoiceClone 用 ref_audio（+可选 ref_text）驱动音色。
            # ⚠️ ref_node 缺失时**绝不**静默退回 CustomVoice —— 那会得到一个与用户
            # 上传音频毫无关系的预置音色，且日志上看不出来。调用方负责先兜底。
            # ⚠️⭐ 2026-10-08 修复（用户实测「音色不对 / 克隆跑不通」）：本节点
            #    的必填入参名是 **target_text**，不是 text —— 传 text 会直接被
            #    ComfyUI 判为 required_input_missing: target_text，克隆整条挂掉
            #    （实测报错见 2026-10-08 日志）。这也是 voice_bank 里 8 段角色参考
            #    音色一直没被用上、只能退回预置 speaker 的根因。
            inputs = dict(common, target_text=text,
                          instruct=str(voice.get("instruct") or ""),
                          x_vector_only=False)
            if ref_node:
                inputs["ref_audio"] = [ref_node, 0]
            rt = str(voice.get("ref_text") or "").strip()
            if rt:
                inputs["ref_text"] = rt
            return "FB_Qwen3TTSVoiceClone", inputs
        if mode == "design":
            return "FB_Qwen3TTSVoiceDesign", dict(common, text=text,
                                                  instruct=str(voice.get("instruct") or ""))
        return "FB_Qwen3TTSCustomVoice", dict(common, text=text,
                                              speaker=voice.get("speaker") or SPEAKER_KEYS[0],
                                              instruct=str(voice.get("instruct") or ""))

    def _upload_ref_audio(self, local_path: str,
                          cache: Optional[Dict[str, Optional[str]]] = None) -> Optional[str]:
        """上传一段参考音频到 ComfyUI input 目录，返回可写进 LoadAudio 的相对名。

        走 ``/upload/image`` 端点（ComfyUI 的通用 input 文件上传，不校验 MIME，
        与 ``comfyui_client._upload_h3_director_audio`` 同一做法），文件名带路径
        哈希前缀防跨角色同名覆盖。失败返回 None（调用方据此回落 preset，绝不静默）。
        """
        if not local_path or not os.path.isfile(local_path):
            return None
        key = os.path.normcase(os.path.normpath(os.path.abspath(local_path)))
        if cache is not None and key in cache:
            return cache[key]
        base = os.path.basename(key) or "ref.wav"
        stem, ext = os.path.splitext(base)
        ext = ext or ".wav"
        tag = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:12]
        fname = f"{safe_name(stem, 24)}_{tag}{ext}"
        subdir = "mjscxt_tts_refs"
        val: Optional[str] = None
        try:
            with open(key, "rb") as fh:
                ctype = ("audio/wav" if ext.lower() == ".wav"
                         else "audio/mpeg" if ext.lower() == ".mp3" else "audio/flac")
                blob = fh.read()
            result = _http_multipart_upload(
                f"{self.comfyui_url}/upload/image",
                field="image", filename=fname, content=blob, content_type=ctype,
                extra_fields={"overwrite": "true", "type": "input", "subfolder": subdir})
            up = result.get("name") or fname
            sub = result.get("subfolder") or subdir
            val = f"{sub}/{up}" if sub else up
            logger.info("QwenTTS 参考音频已上传：%s → %s", base, val)
        except Exception as e:  # noqa: BLE001
            logger.warning("QwenTTS 参考音频上传失败 %s：%s", key, e)
            val = None
        if cache is not None:
            cache[key] = val
        return val

    def synthesize_batch(self, items: List[Dict],
                         progress_cb: Optional[Callable[[int, int, dict, str], None]] = None,
                         timeout: int = None) -> List[Dict]:
        """批量合成：items = [{text, voice, out_path, store_prefix}]，逐句回填产物信息

        clone 模式（voice["mode"]=="clone"）时，按 voice["ref_audio"] 上传参考音频并
        注入一个 LoadAudio 节点复用（同一批内同路径只上传一次）。上传失败或 ref_audio
        缺失 → **回落 preset 音色**并在结果里记 `clone_fallback`，绝不静默产出一个
        与参考音频无关的音色还装作克隆成功。
        """
        timeout = int(timeout or self.params.get("timeout", 1200))
        prompt: Dict = {}
        bindings: List[Dict] = []
        _ref_cache: Dict[str, Optional[str]] = {}
        _ref_nodes: Dict[str, str] = {}      # 上传后的相对名 → LoadAudio 节点 id
        for i, it in enumerate(items):
            voice = dict(it["voice"] or {})
            ref_node: Optional[str] = None
            fallback_note = ""
            if voice.get("mode") == "clone":
                _ref_local = str(voice.get("ref_audio") or "").strip()
                _up = self._upload_ref_audio(_ref_local, _ref_cache) if _ref_local else None
                if _up:
                    ref_node = _ref_nodes.get(_up)
                    if ref_node is None:
                        ref_node = f"ra{len(_ref_nodes)}"
                        _ref_nodes[_up] = ref_node
                        prompt[ref_node] = {"class_type": "LoadAudio",
                                            "inputs": {"audio": _up}}
                else:
                    # 克隆不可用 → 回落 preset（保留原 speaker），并记账
                    fallback_note = ("参考音频缺失或上传失败" if not _ref_local
                                     else "参考音频上传失败")
                    voice["mode"] = "preset"
                    voice.setdefault("speaker", SPEAKER_KEYS[0])
            ctype, inputs = self._node_inputs(it["text"], voice, i == len(items) - 1,
                                              ref_node=ref_node)
            cv, sv = f"cv{i}", f"sv{i}"
            prompt[cv] = {"class_type": ctype, "inputs": inputs}
            prompt[sv] = {"class_type": "SaveAudio",
                          "inputs": {"audio": [cv, 0], "filename_prefix": it["store_prefix"]}}
            _it = dict(it, voice=voice)
            bindings.append({"cv": cv, "sv": sv, "item": _it,
                             "clone_fallback": fallback_note})

        logger.info("QwenTTS 批量合成 %d 句 → prompt 节点 %d 个（clone 参考音频 %d 个）",
                    len(items), len(prompt), len(_ref_nodes))
        prompt_id = self._submit(prompt)
        # G-03：try/finally 确保 ComfyUI TTS 模型卸载。_wait 抛 TTSError（超时/执行失败）时，
        # 前面已加载的模型未卸载（只有最后一句节点设了 unload_model_after_generate）。
        # 批量 TTS 高频运行，显存累积泄漏 → 后续 OOM。成功时最后一句已卸载，/free 无副作用。
        # ⚠️ 模型常驻（keep_model_loaded）时整段跳过：不卸载 = 下一批直接复用已加载的
        # 权重，省掉每批一次的模型加载（Qwen3-TTS 加载一次几十秒）。
        try:
            history = self._wait(prompt_id, timeout, progress_cb=lambda m: None)
        finally:
            if not self._keep_loaded():
                self._unload_model()
        outputs = history.get("outputs") or {}

        results: List[Dict] = []
        for b in bindings:
            it, sv = b["item"], b["sv"]
            raw = ((outputs.get(sv) or {}).get("audio") or [None])[0]
            rec = {"line_id": it.get("line_id"), "shot_id": it.get("shot_id"),
                   "character": it.get("character"), "text": it["text"],
                   "speaker": it["voice"].get("speaker"), "mode": it["voice"].get("mode"),
                   "seed": it["voice"].get("seed"), "out_path": os.path.abspath(it["out_path"]),
                   "prompt_id": prompt_id}
            if b.get("clone_fallback"):
                rec["clone_fallback"] = b["clone_fallback"]
            if not raw:
                rec.update({"ok": False, "error": "ComfyUI 未返回音频文件"})
                results.append(rec)
                continue
            try:
                info = self._download_and_convert(it, raw, it["out_path"])
                rec.update({"ok": True, "comfy_raw": raw.get("filename"),
                            "comfy_subfolder": raw.get("subfolder"),
                            "duration": info.get("duration"), "size_bytes": info.get("size_bytes"),
                            "codec": info.get("codec"), "sample_rate": info.get("sample_rate"),
                            "channels": info.get("channels")})
            except TTSError as e:
                rec.update({"ok": False, "error": str(e)})
            results.append(rec)
        return results

    def synthesize_lines(self, lines: List[Dict], out_dir: str,
                         progress_cb: Optional[Callable[[int, int, dict, str], None]] = None,
                         timeout: int = None) -> List[Dict]:
        """按行合成（自动分批；批失败时降级为逐句重试，单句失败不影响其它句）"""
        batch_size = max(1, int(self.params.get("batch_lines", 4)))
        os.makedirs(out_dir, exist_ok=True)
        prepared: List[Dict] = []
        for i, ln in enumerate(lines):
            out_path = ln.get("out_path") or os.path.join(out_dir, ln.get("out_name") or f"line_{i:02d}.wav")
            prepared.append({
                "text": ln["text"], "voice": ln["voice"], "out_path": out_path,
                "line_id": ln.get("line_id"), "shot_id": ln.get("shot_id"),
                "character": ln.get("character"),
                "store_prefix": f"dub/{ln.get('project_tag') or 'project'}/{os.path.splitext(os.path.basename(out_path))[0]}",
            })

        all_results: List[Dict] = []
        done = 0
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start:start + batch_size]
            try:
                res = self.synthesize_batch(batch, timeout=timeout)
            except TTSError as e:
                logger.warning(f"批量合成失败（{len(batch)} 句），降级逐句重试：{e}")
                res = []
                for one in batch:
                    try:
                        res.extend(self.synthesize_batch([one], timeout=timeout))
                    except TTSError as e2:
                        res.append({"ok": False, "line_id": one.get("line_id"),
                                    "shot_id": one.get("shot_id"), "character": one.get("character"),
                                    "out_path": os.path.abspath(one["out_path"]),
                                    "error": str(e2), "text": one["text"]})
            all_results.extend(res)
            done += len(batch)
            if progress_cb:
                progress_cb(done, len(prepared), res[-1] if res else batch[-1],
                            "" if all(r.get("ok") for r in res) else "部分句失败")
        return all_results

    def synthesize_one(self, text: str, voice: dict, out_path: str,
                       store_prefix: str = None, timeout: int = None) -> Dict:
        """单句合成（试听用）"""
        tag = os.path.splitext(os.path.basename(out_path))[0]
        res = self.synthesize_batch([{
            "text": text, "voice": voice, "out_path": out_path,
            "store_prefix": store_prefix or f"dub/preview/{tag}",
            "line_id": tag, "shot_id": None, "character": str((voice or {}).get("character") or "") or None,
        }], timeout=timeout)
        return res[0] if res else {"ok": False, "error": "合成未返回结果"}
