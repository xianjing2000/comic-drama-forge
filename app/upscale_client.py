"""
视频超分（FlashVSR）真实实现 —— 成片视频 → ComfyUI 超分工作流 → 超分结果落盘

两套引擎（同一入口 ``VideoUpscaler.upscale(engine=...)`` 切换）：

1. **te-speed-flashvsr（默认，加速链路）**：以 ComfyUI 工作流模板
   「TE-Speed-flashVSR 视频超分放大加速工作流.json」为模板，在内存中构造等价 API prompt：
   ``VHS_LoadVideoPath → TEFlashVSRModelLoader → TEFlashVSRTuning（sparse_sage2 稀疏注意力 + 分块）
   → TEFlashVSRRestore → TESpeedVideoCombine``。模板文件**只读解析，不修改**。
2. **legacy-flashvsr（旧链路，保留可回退）**：``VHS_LoadVideoPath → FlashVSRInitPipe
   → FlashVSRNodeAdv → VHS_VideoCombine``（无加速节点）。

其他设计要点：
- 真跑通：节点名与输入名全部按本机 /object_info 实际声明书写，不用占位字段。
- 不静默失败：提交前做节点存在性/必填项自检；执行中解析 history 的 status.messages，
  任何 error 都抛出带节点与异常文本的 UpscaleError；模板不可用时明确报错并给出回退方式。
- 路径规范：输入保留原位（只读），超分结果统一落 output/upscale/<项目>/ 下，与原始视频隔离。
- 显存：8G 笔记本默认 tiny + tiled 分块 + 稀疏注意力，并提供可调档位。
"""
import os
import json
import time
import shutil
import logging
import subprocess
from typing import Dict, List, Optional

import requests
import cancellation  # S9：远端任务取消（中止信号贯穿超分轮询，与 pipeline/llm_client 同一套）

from config import (
    COMFYUI_URL, COMFYUI_OUTPUT_DIR, PROJECT_OUTPUT_DIR, UPSCALE_DIR,
    FLASHVSR_MODEL_DIR, FLASHVSR_REQUIRED_FILES, UPSCALE_DEFAULT_PARAMS,
    TE_UPSCALE_DEFAULT_PARAMS, UPSCALE_ENGINE, UPSCALE_TE_TEMPLATE_PATH,
    UPSCALE_CALIBRATION_PATH, KEEP_MODEL_LOADED,
)
from comfyui_client import ComfyUIClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """整数环境变量（非法/空 → default，绝不抛）。

    ⭐ t22：与 comfyui_client._env_int（t18）**同语义**；这里本地实现，避免跨模块依赖
    对方的私有名（两个模块都要能在没有对方的情况下独立导入）。
    """
    try:
        raw = str(os.environ.get(name) or "").strip()
        return int(float(raw)) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)

# ===== 引擎 A：TE-Speed-flashVSR 加速链路（默认）=====
# 节点与模板 JSON 一一对应（模板：TE-Speed-flashVSR 视频超分放大加速工作流.json）
TE_SPEED_NODES = (
    "VHS_LoadVideoPath",      # 读视频（本地绝对路径，输出 IMAGE + AUDIO）
    "TEFlashVSRModelLoader",  # 加载 FlashVSR-v1.1（mode/precision/device）
    "TEFlashVSRTuning",       # 加速调优：sparse_sage2 稀疏注意力 + tiled 分块
    "TEFlashVSRRestore",      # 实际超分（scale / color_fix / seed）
    "TESpeedVideoCombine",    # 合成 mp4（可选 audio 输入旁路）
)

# ===== 引擎 B：旧 FlashVSR Ultra-Fast 链路（回退用）=====
# 本机 FlashVSR Ultra-Fast 节点（model = "FlashVSR-v1.1" 即 models/FlashVSR-v1.1）
FLASHVSR_NODES = (
    "VHS_LoadVideoPath",     # 读视频（本地绝对路径）
    "FlashVSRInitPipe",      # 初始化 FlashVSR pipeline
    "FlashVSRNodeAdv",       # 实际超分（Ultra-Fast Advanced）
    "VHS_VideoCombine",      # 合成 mp4
)
# 备选链路（ComfyUI-FlashVSR 的 AILab 节点，需 models/FlashVSR/*.safetensors，本机未就位）
FLASHVSR_ALT_NODES = ("AILab_FlashVSR_Advanced",)

ENGINES = ("te-speed-flashvsr", "legacy-flashvsr")



class UpscaleError(RuntimeError):
    """超分失败（含明确原因）"""


# ============================ 工具函数 ============================

def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def probe_video(path: str, timeout: int = 60) -> Dict:
    """用 ffprobe 读取视频真实参数（分辨率 / 帧率 / 帧数 / 时长 / 体积 / 是否有音轨）

    失败时返回 {'ok': False, 'error': ...}，绝不猜测。
    """
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}
    ffprobe = _which("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        # 显式指定 utf-8：避免 Windows GBK 默认解码把 ffprobe 输出（含中文路径）解坏
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 ffprobe，可执行文件不在 PATH 中"}
    except Exception as e:
        return {"ok": False, "error": f"ffprobe 执行异常: {e}"}
    if r.returncode != 0:
        return {"ok": False, "error": f"ffprobe 返回 {r.returncode}: {(r.stderr or '').strip()[:300]}"}
    try:
        data = json.loads(r.stdout or "{}")
    except Exception as e:
        return {"ok": False, "error": f"ffprobe 输出解析失败: {e}"}

    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return {"ok": False, "error": "未找到视频流"}
    fps = 0.0
    try:
        num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except Exception:
        fps = 0.0
    size = 0
    try:
        size = int((data.get("format") or {}).get("size") or os.path.getsize(path))
    except Exception:
        size = os.path.getsize(path)
    return {
        "ok": True,
        "path": os.path.abspath(path),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "frames": int(v.get("nb_frames") or 0),
        "duration": round(float((data.get("format") or {}).get("duration") or 0), 3),
        "size_bytes": size,
        "size_mb": round(size / 1048576, 2),
        "video_codec": v.get("codec_name"),
        "has_audio": bool(a is not None),
        "audio_codec": (a or {}).get("codec_name"),
    }


def read_te_template(path: str = UPSCALE_TE_TEMPLATE_PATH) -> Dict:
    """只读解析 TE-Speed-flashVSR 模板工作流，返回 {found, path, class_types, nodes, error}

    仅用于核对加速链路与自检，绝不写回模板文件。
    """
    info: Dict = {"found": False, "path": path, "class_types": [], "node_count": 0, "error": ""}
    if not path or not os.path.exists(path):
        info["error"] = f"模板文件不存在：{path}"
        return info
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:  # noqa: BLE001
        info["error"] = f"模板解析失败：{e}"
        return info
    nodes = raw.get("nodes") if isinstance(raw, dict) else None
    if not isinstance(nodes, list):   # 也有可能是已经导出的 API 格式（{id: {class_type}}）
        if isinstance(raw, dict) and all(isinstance(v, dict) for v in raw.values()):
            info.update({"found": True, "class_types": [v.get("class_type") for v in raw.values()],
                         "node_count": len(raw)})
        else:
            info["error"] = "模板结构非预期（既无 nodes 数组，也不是 API 格式）"
        return info
    cts = [n.get("type") for n in nodes if isinstance(n, dict)]
    info.update({"found": True, "class_types": [c for c in cts if c], "node_count": len(nodes)})
    return info


def check_environment(comfy_url: str = COMFYUI_URL) -> Dict:
    """超分链路可用性检查：ComfyUI 在线 / 节点就位 / 模型文件就位 / 模板可读

    ``available`` 表示「至少一条引擎链路可用」；``te_ready`` / ``legacy_ready`` 分别标识两条链路。
    """
    reasons: List[str] = []
    result: Dict = {
        "comfy_url": comfy_url,
        "comfy_online": False,
        "comfy_version": "",
        "model_dir": FLASHVSR_MODEL_DIR,
        "model_files": [],
        "model_ready": False,
        "engine_default": UPSCALE_ENGINE,
        "engines": list(ENGINES),
        "template_path": UPSCALE_TE_TEMPLATE_PATH,
        "template_found": False,
        "template_class_types": [],
        # TE-Speed-flashVSR（加速，默认引擎）
        "te_nodes": {n: False for n in TE_SPEED_NODES},
        "te_ready": False,
        "te_params": dict(TE_UPSCALE_DEFAULT_PARAMS),
        # 旧 FlashVSR Ultra-Fast（回退引擎）
        "nodes": {n: False for n in FLASHVSR_NODES},
        "legacy_ready": False,
        "alt_nodes": {n: False for n in FLASHVSR_ALT_NODES},
        "available": False,
        "reasons": reasons,
    }

    # 1) 模型文件
    files = []
    for name in FLASHVSR_REQUIRED_FILES:
        p = os.path.join(FLASHVSR_MODEL_DIR, name)
        files.append({
            "name": name,
            "exists": os.path.exists(p),
            "size_mb": round(os.path.getsize(p) / 1048576, 1) if os.path.exists(p) else 0,
        })
    result["model_files"] = files
    result["model_ready"] = all(f["exists"] for f in files)
    if not result["model_ready"]:
        missing = [f["name"] for f in files if not f["exists"]]
        reasons.append(f"FlashVSR 模型缺失: {', '.join(missing)}（目录 {FLASHVSR_MODEL_DIR}）")

    # 2) 模板文件（只读核对加速链）
    tpl = read_te_template()
    result["template_found"] = bool(tpl["found"])
    result["template_class_types"] = tpl["class_types"]
    result["template_error"] = tpl["error"]

    # 3) ComfyUI 在线 & 节点
    try:
        info = requests.get(f"{comfy_url}/object_info", timeout=60).json()
        result["comfy_online"] = True
        for n in TE_SPEED_NODES:
            result["te_nodes"][n] = n in info
        for n in FLASHVSR_NODES:
            result["nodes"][n] = n in info
        for n in FLASHVSR_ALT_NODES:
            result["alt_nodes"][n] = n in info
        miss_te = [n for n in TE_SPEED_NODES if not result["te_nodes"][n]]
        miss_legacy = [n for n in FLASHVSR_NODES if not result["nodes"][n]]
        result["te_ready"] = not miss_te
        result["legacy_ready"] = not miss_legacy
        if tpl["found"]:
            need = [c for c in TE_SPEED_NODES if c not in (tpl["class_types"] or [])]
            if need:
                result["template_note"] = f"模板中未出现的节点（按本机节点链补齐）：{', '.join(need)}"
        if miss_te:
            reasons.append(f"ComfyUI 缺少 TE-Speed 加速节点: {', '.join(miss_te)}"
                           f"（需安装 TE-Speed-FlashVSR 节点包）")
        if miss_legacy:
            reasons.append(f"ComfyUI 缺少旧 FlashVSR 节点: {', '.join(miss_legacy)}"
                           f"（需安装 ComfyUI-FlashVSR_Ultra_Fast 与 VideoHelperSuite）")
        try:
            result["comfy_version"] = str(requests.get(f"{comfy_url}/system_stats", timeout=20)
                                          .json().get("system", {}).get("comfyui_version", ""))
        except Exception as e:
            logger.debug("ComfyUI 版本探测失败（忽略）：%s", e)
    except Exception as e:
        reasons.append(f"ComfyUI 不可达（{comfy_url}）: {e}")

    result["available"] = bool(result["comfy_online"] and result["model_ready"]
                               and (result["te_ready"] or result["legacy_ready"]))
    if result["te_ready"] and result["model_ready"] and result["comfy_online"]:
        reasons.append("超分链路就绪（TE-Speed-flashVSR 加速：" +
                       f"{TE_UPSCALE_DEFAULT_PARAMS['attention_backend']} + "
                       f"{TE_UPSCALE_DEFAULT_PARAMS['spatial_strategy']}，mode="
                       f"{TE_UPSCALE_DEFAULT_PARAMS['mode']}）")
    elif result["legacy_ready"] and result["model_ready"] and result["comfy_online"]:
        reasons.append("TE-Speed 加速节点缺失，仅旧 FlashVSR Ultra-Fast 链路可用（无加速）")
    return result


def resolve_engine(engine: Optional[str] = None, env: Optional[Dict] = None) -> str:
    """选择超分引擎：显式指定优先；否则默认引擎可用就用默认，不可用则自动回退另一条链路。

    两条链路都不可用时抛 UpscaleError（不静默降级为占位实现）。
    """
    want = str(engine or UPSCALE_ENGINE or "").strip().lower() or "te-speed-flashvsr"
    if want not in ENGINES:
        raise UpscaleError(f"未知超分引擎：{want}（可选 {' / '.join(ENGINES)}）")
    env = env or check_environment()
    if want == "te-speed-flashvsr":
        if env.get("te_ready"):
            return "te-speed-flashvsr"
        if env.get("legacy_ready"):
            logger.warning("TE-Speed 加速链路不可用（%s），自动回退旧 FlashVSR 链路",
                           "；".join(env.get("reasons") or []))
            return "legacy-flashvsr"
        raise UpscaleError("超分环境不可用：" + "；".join(env.get("reasons") or []))
    if env.get("legacy_ready"):
        return "legacy-flashvsr"
    if env.get("te_ready"):
        logger.warning("旧 FlashVSR 链路不可用，自动回退 TE-Speed 加速链路")
        return "te-speed-flashvsr"
    raise UpscaleError("超分环境不可用：" + "；".join(env.get("reasons") or []))


# ============================ 耗时标定（离线预估用） ============================

def load_calibration() -> Dict:
    try:
        with open(UPSCALE_CALIBRATION_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_calibration(entry: Dict) -> str:
    """记录一次真实超分实测（引擎 / 分辨率 / 帧数 / 耗时），用于离线预估同一素材的耗时"""
    data = load_calibration()
    data.setdefault("entries", [])
    data["entries"].append(entry)
    data["entries"] = data["entries"][-40:]
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(UPSCALE_CALIBRATION_PATH), exist_ok=True)
    with open(UPSCALE_CALIBRATION_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return UPSCALE_CALIBRATION_PATH




# ============================ 场内切片（分段超分） ============================

#: 字符码常量（本机写盘工具会吞掉源码里的反斜杠字面量，故一律用 chr() 表达）。
_BSCH = chr(92)      # 反斜杠: 转义单引号 / 清单换行
_NLCH = chr(10)      # 换行: ffmpeg concat 清单的分隔符
#: 单个切片的目标时长上限（秒）——片段时长 <= 该值时「不切片」，走原来的单次超分路径。
DEFAULT_CHUNK_SECONDS = 10
#: 切片时长下限：小于 1s 的切片没有意义（额外一次模型装载/提交，收益为负）。
MIN_CHUNK_SECONDS = 1
#: 切片时长上限：超过 60s 就回到了「整场一次超分」的老问题（显存/超时）。
MAX_CHUNK_SECONDS = 60


def normalize_chunk_seconds(value, default: int = DEFAULT_CHUNK_SECONDS) -> int:
    """把外部传入的切片时长收敛为 [1, 60] 的整数；非法/缺省 → default。

    <= 0 是**显式关闭切片**（回到「整场一次超分」的旧行为），不回落 default ——
    否则用户没法关掉切片。
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    if v <= 0:
        return 0
    return max(MIN_CHUNK_SECONDS, min(MAX_CHUNK_SECONDS, v))


def _segment_one(src: str, out_path: str, start: float, dur: float,
                 ffmpeg: str, timeout: int) -> str:
    """切出 [start, start+dur) 一段（-c copy，**不重编码**）；成功返回 out_path，失败返回空串。

    为什么 -ss 放在 -i **之前**：输入侧定位会跳到目标时间点前最近的关键帧再解码，
    既准确又快（输出侧 -ss 需要解码丢弃前面全部帧，多片时会线性变慢）。

    为什么不用 -f segment：实测那套会把音频 DTS 按绝对时间切（视频从 0 重新开始），
    结果片段被切成 10~17s 的乱长、最大超目标 65%。-ss/-t 的边界是确定的：
    每片从关键帧起、不短于 dur（-c copy 只能在关键帧切，故单片可能略长）。
    """
    cmd = [ffmpeg, "-hide_banner", "-y", "-v", "error",
           "-ss", "%.3f" % max(0.0, float(start)),
           "-i", src,
           "-t", "%.3f" % max(0.01, float(dur)),
           "-map", "0:v:0", "-map", "0:a?",
           "-c", "copy",
           "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart",
           out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:  # noqa: BLE001
        logger.warning("切片超分：切第 %.2fs 段异常：%s", start, e)
        return ""
    if r.returncode != 0:
        logger.warning("切片超分：切第 %.2fs 段失败（rc=%s）：%s",
                       start, r.returncode, (r.stderr or "").strip()[-300:])
        return ""
    if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
        return ""
    return out_path


def segment_video_nocopy(input_video: str, out_dir: str,
                         seconds: int = DEFAULT_CHUNK_SECONDS,
                         timeout: int = 300) -> List[str]:
    """把视频按 **不超过 seconds 秒** 切成片段（ffmpeg -ss/-t + -c copy，**不重编码**）。

    为什么必须不重编码：切片只是「换一种提交粒度」，画面不能被二次压缩（FlashVSR 之后
    再压一次会掉细节），且重编码一整场（164s）本身也要几十秒~几分钟。

    关键帧边界（重要）：-c copy 只能在关键帧处切，因此**单片可能略长于 seconds**
    （实测本机 H3 成片：目标 10s → 最长 16.5s，即 +65%）。这是可接受的取舍 —— 目标是
    「控制单次提交规模」，不是精确等分；这里会用 ffprobe 复核并把实际范围写进日志。

    返回按序号排列的切片绝对路径（>=1 个）；切不出（ffmpeg 缺失/失败/源不可读/源无时长）
    返回 []，调用方据此回落「整段一次超分」——绝不因为切片失败而让超分整体失败。
    """
    sec = normalize_chunk_seconds(seconds, DEFAULT_CHUNK_SECONDS)
    if sec <= 0:
        return []
    ffmpeg = _which("ffmpeg") or "ffmpeg"
    src = os.path.abspath(input_video or "")
    if not src or not os.path.exists(src):
        logger.warning("切片超分：源视频不存在，无法切片：%s", input_video)
        return []
    info = probe_video(src)
    total = float(info.get("duration") or 0) if info.get("ok") else 0.0
    if total <= 0:
        logger.warning("切片超分：读不到源时长（%s），回落整段超分", info.get("error"))
        return []
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.warning("切片超分：切片目录不可用（%s）：%s", out_dir, e)
        return []

    n = int((total + sec - 1e-6) // sec) + 1     # 片数上限（向上取整 + 1 防边界）
    chunks: List[str] = []
    want = 0.0              # 下一片的目标起点
    covered = 0.0           # 已被已切片段覆盖到的源时间（避免重叠：实测重叠会让成片变长）
    for i in range(n):
        if want >= total - 1e-3:
            break
        dur = min(float(sec), max(0.05, total - want))
        out = os.path.join(out_dir, "chunk_%03d.mp4" % i)
        got = _segment_one(src, out, want, dur, ffmpeg,
                           int(timeout) if timeout else 300)
        if not got:
            # 任一段切不出 → 整体回落整段超分（绝不交半套切片给下游）
            return []
        chunks.append(got)
        # -ss 落在**关键帧**上，实际起点可能早于 want → 下一片从「已被覆盖的位置」续切，
        # 避免与前一片重叠（实测 i*sec 的朴素步进会让每片重放一段前缀，成片被拉长 37%）。
        ci = probe_video(got)
        cd = float(ci.get("duration") or 0) if ci.get("ok") else 0.0
        covered = max(covered, want + (cd if cd > 0 else dur))
        want = covered - 1e-3
    if not chunks:
        logger.warning("切片超分：未产出任何切片，回落整段超分：%s", src)
        return []

    # 复核（fail-open）：探不到时长（或时长为 0）的切片剔除，避免把空壳提交给 ComfyUI
    kept: List[str] = []
    durs: List[float] = []
    for c in chunks:
        ci = probe_video(c)
        cd = float(ci.get("duration") or 0) if ci.get("ok") else 0.0
        if cd <= 0:
            logger.warning("切片超分：切片不可用（%s），剔除：%s", ci.get("error"), c)
            continue
        kept.append(c)
        durs.append(cd)
    if not kept:
        logger.warning("切片超分：全部切片不可用，回落整段超分：%s", src)
        return []
    _mx = max(durs)
    if _mx > sec * 3:
        logger.error("切片超分：实际最长片 %.2fs 超过目标 %ds 的 3 倍 —— 源片关键帧过稀，"
                     "单次提交规模未真正受控（该片仍会超分，但超时/显存风险回归）", _mx, sec)
    logger.info("切片超分：%s（%.2fs）→ %d 片（目标 <=%ds，实际 %.2f~%.2fs）",
                os.path.basename(src), total, len(kept), sec, min(durs), _mx)
    return kept


def concat_segments_nocopy(parts: List[str], out_path: str,
                           timeout: int = 1800) -> str:
    """把切片超分产物拼回**整段**（先试 -c copy 流拼接，不重编码）。

    为什么优先 -c copy：所有切片来自同一母版的同一次超分任务 → 编码参数天然一致，
    流拼接是逐字节搬运，既快又无损。

    失败/无音轨混合时回落 concat filter 重编码 —— 这里**必须**重编码，因为
    -c copy 对「部分片段无音轨」会静默丢轨（见 video_postprocess.concat_videos 的
    t11 P0 记录）。回落路径复用 video_postprocess 的成熟实现（它会逐段补静音轨）。

    返回 out_path 表示成功；失败返回 ""（契约与 video_postprocess.concat_videos 一致，
    不抛异常）。
    """
    parts = [os.path.abspath(p) for p in (parts or []) if p and os.path.exists(p)]
    if not parts:
        logger.error("切片超分：拼回整段失败 —— 无可用片段")
        return ""
    if len(parts) == 1:
        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            shutil.copy2(parts[0], out_path)
            return out_path
        except Exception as e:  # noqa: BLE001
            logger.error("切片超分：单片直接归档失败：%s", e)
            return ""

    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    except OSError as e:
        logger.error("切片超分：拼回目录不可用（%s）：%s", os.path.dirname(out_path), e)
        return ""

    ffmpeg = _which("ffmpeg") or "ffmpeg"
    list_file = out_path + ".list"
    try:
        with open(list_file, "w", encoding="utf-8") as f:
            for v in parts:
                # 单引号转义与 video_postprocess.concat_videos 同一约定（路径含 ' 时截断）
                f.write(_concat_file_line(v))
        cmd = [ffmpeg, "-hide_banner", "-y", "-v", "error",
               "-f", "concat", "-safe", "0", "-i", list_file,
               "-c", "copy", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        ok = (r.returncode == 0 and os.path.isfile(out_path)
              and os.path.getsize(out_path) > 0
              and float(probe_video(out_path).get("duration") or 0) > 0)
        if ok:
            logger.info("切片超分：拼回整段成功（-c copy，%d 片）：%s", len(parts), out_path)
            return out_path
        logger.warning("切片超分：-c copy 拼接失败（rc=%s，%s），回落重编码拼接",
                       r.returncode, (r.stderr or "").strip()[-300:])
    except Exception as e:  # noqa: BLE001
        logger.warning("切片超分：-c copy 拼接异常，回落重编码拼接：%s", e)
    finally:
        try:
            if os.path.exists(list_file):
                os.remove(list_file)
        except OSError as _ce:
            logger.debug("清理切片拼接清单失败（忽略）：%s", _ce)

    # 回落：复用 video_postprocess 的重编码拼接（统一参数 + 逐段补静音轨）
    try:
        from video_postprocess import VideoPostProcessor
        proc = VideoPostProcessor()
        got = proc.concat_videos(parts, out_path,
                                 caller="upscale_client.concat_segments_nocopy")
        if got and float(probe_video(got).get("duration") or 0) > 0:
            logger.info("切片超分：拼回整段成功（重编码，%d 片）：%s", len(parts), got)
            return got
    except Exception as e:  # noqa: BLE001
        logger.error("切片超分：重编码拼接也失败：%s", e, exc_info=True)
    return ""


# ============================ 切片拼接清单 ============================


def _concat_file_line(path: str) -> str:
    """ffmpeg concat demuxer 清单的一行（file + 单引号路径 + 行尾换行）。

    为什么单独抽成函数：清单语法用单引号包裹路径，路径里的单引号必须写成
    「关引号 + 反斜杠转义单引号 + 重开引号」；且**每行必须以换行结束**——
    实测漏掉换行时 ffmpeg 会把两行粘成 "chunk_000.mp4nfile ..." 直接报错。
    这两处都极易写错，抽出来一次覆盖即可守住。
    """
    p = os.path.abspath(path)
    esc = p.replace(chr(39), chr(39) + _BSCH + chr(39))
    return "file " + chr(39) + esc + chr(39) + _NLCH


# ============================ 工作流构造 ============================

def build_te_workflow(input_video: str, filename_prefix: str, scale: int = 2,
                      mode: str = "tiny", frame_rate: float = 24.0,
                      has_audio: bool = False, attach_audio: bool = False,
                      **overrides) -> Dict:
    """构造 TE-Speed-flashVSR 加速 API prompt（与模板工作流节点链一致）

    链路：VHS_LoadVideoPath → TEFlashVSRModelLoader → TEFlashVSRTuning
          → TEFlashVSRRestore → TESpeedVideoCombine
    加速点：attention_backend=sparse_sage2（稀疏 SageAttention2）+ 分块
            （max_tile_edge / blend_overlap）+ attention_budget / kv_retention 稀疏预算。
    低显存（8G）默认档：mode=tiny + max_tile_edge=256（与模板一致）；
            显存吃紧可传 spatial_strategy=adaptive_tiles / memory_policy=staged。
    所有字段名与取值域均按本机 /object_info 实测声明，越界参数在提交前直接拒绝。
    """
    p = dict(TE_UPSCALE_DEFAULT_PARAMS)
    p.update({k: v for k, v in (overrides or {}).items() if v is not None})
    scale = int(scale)
    if scale not in (2, 3, 4):
        raise UpscaleError(f"倍率仅支持 2 / 3 / 4，收到 {scale}")
    mode = str(mode or p["mode"]).strip().lower()
    if mode not in ("tiny", "tiny-long", "full"):
        raise UpscaleError(f"mode 仅支持 tiny / tiny-long / full，收到 {mode}")
    strategy = str(p["spatial_strategy"]).strip().lower()
    if strategy not in ("auto", "full_frame", "adaptive_tiles"):
        raise UpscaleError(f"spatial_strategy 仅支持 auto / full_frame / adaptive_tiles，收到 {strategy}")
    profile = str(p["quality_profile"]).strip().lower()
    if profile not in ("detail", "balanced", "throughput"):
        raise UpscaleError(f"quality_profile 仅支持 detail / balanced / throughput，收到 {profile}")

    def _num(key, lo, hi):
        v = float(p[key])
        if v < lo or v > hi:
            raise UpscaleError(f"{key} 需在 [{lo}, {hi}] 区间，收到 {v}")
        return v

    _wf = {
        "1": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": os.path.abspath(input_video),
                "force_rate": 0,          # 0 = 保持原始帧率
                "custom_width": 0,        # 0 = 保持原始尺寸（放大由 FlashVSR 负责）
                "custom_height": 0,
                "frame_load_cap": int(p.get("frame_load_cap") or 0),   # 0 = 全部帧
                "skip_first_frames": int(p.get("skip_first_frames") or 0),
                "select_every_nth": 1,
                "format": "None",         # 不做帧数/尺寸对齐（避免 Wan/H3 dim=8 截断帧）
            },
        },
        "2": {
            "class_type": "TEFlashVSRModelLoader",
            "inputs": {
                "model": "FlashVSR-v1.1",
                "mode": mode,             # 显存档位
                "precision": str(p["precision"]),   # bf16 / fp16
                "device": str(p["device"]),         # auto / cuda:0
            },
        },
        "3": {
            "class_type": "TEFlashVSRTuning",
            "inputs": {
                "quality_profile": profile,
                "intensity": _num("intensity", 0.0, 1.0),
                "spatial_strategy": strategy,        # auto / full_frame / adaptive_tiles
                "memory_policy": str(p["memory_policy"]),   # auto / resident / staged
                "attention_budget": _num("attention_budget", 1.0, 2.0),
                "kv_retention": _num("kv_retention", 1.0, 3.0),
                "local_radius": int(_num("local_radius", 9, 11)),
                "max_tile_edge": int(_num("max_tile_edge", 128, 2048)),
                "blend_overlap": int(_num("blend_overlap", 0, 512)),
                "preprocess_batch": int(_num("preprocess_batch", 1, 32)),
                "attention_backend": str(p["attention_backend"]),   # sparse_sage2 = 加速核心
            },
        },
        "4": {
            "class_type": "TEFlashVSRRestore",
            "inputs": {
                "model": ["2", 0],
                "frames": ["1", 0],
                "scale": scale,                      # 2 / 3 / 4
                "color_fix": bool(p["color_fix"]),
                "seed": int(p["seed"] if p.get("seed") is not None else 0),
                "settings": ["3", 0],                # 可选输入：加速/分块计划
            },
        },
        "5": {
            "class_type": "TESpeedVideoCombine",
            "inputs": {
                "images": ["4", 0],
                "frame_rate": float(frame_rate or 24.0),
                "filename_prefix": filename_prefix,
                "value": int(p.get("quality_value") or 3),   # 压缩质量档 1-8
                "save_output": True,
            },
        },
    }

    # 音轨旁路：H3 成片自带原生音轨（成片即带配音）：默认 attach_audio=True，
    # has_audio 时保留音轨；仅当显式传 attach_audio=false 或源片实测无音轨时才不接
    if attach_audio and has_audio:
        _wf["5"]["inputs"]["audio"] = ["1", 2]
    return _wf



def build_upscale_workflow(input_video: str, filename_prefix: str, scale: int = 2,
                           mode: str = "tiny", frame_rate: float = 24.0,
                           has_audio: bool = True, **overrides) -> Dict:
    """构造 FlashVSR 超分 API prompt（节点/字段名严格对齐本机 /object_info 声明）"""
    p = dict(UPSCALE_DEFAULT_PARAMS)
    p.update({k: v for k, v in (overrides or {}).items() if v is not None})
    scale = int(scale)
    if scale not in (2, 4):
        raise UpscaleError(f"倍率仅支持 2 或 4，收到 {scale}")

    wf: Dict[str, dict] = {
        "1": {
            "class_type": "VHS_LoadVideoPath",
            "inputs": {
                "video": input_video,
                "force_rate": 0,          # 0 = 保持原始帧率
                "custom_width": 0,        # 0 = 保持原始尺寸（超分由 FlashVSR 负责）
                "custom_height": 0,
                "frame_load_cap": int(p.get("frame_load_cap") or 0),   # 0 = 全部帧
                "skip_first_frames": int(p.get("skip_first_frames") or 0),
                "select_every_nth": 1,
                # format="None"：不做任何帧率/尺寸/帧数约束（AnimateDiff 的 dim=8 会强制对齐、
                # Wan/H3 等格式会截断帧数，超分必须原样读取）
                "format": "None",
            },
        },
        "2": {
            "class_type": "FlashVSRInitPipe",
            "inputs": {
                "model": "FlashVSR-v1.1",
                "mode": p["mode"] if mode in (None, "") else mode,
                "alt_vae": "none",
                "force_offload": bool(p["force_offload"]),
                "precision": p["precision"],
                "device": "auto",
                "attention_mode": p["attention_mode"],
            },
        },
        "3": {
            "class_type": "FlashVSRNodeAdv",
            "inputs": {
                "pipe": ["2", 0],
                "frames": ["1", 0],
                "scale": scale,
                "color_fix": bool(p["color_fix"]),
                "tiled_vae": bool(p["tiled_vae"]),
                "tiled_dit": bool(p["tiled_dit"]),
                "tile_size": int(p["tile_size"]),
                "tile_overlap": int(p["tile_overlap"]),
                "unload_dit": bool(p["unload_dit"]),
                "sparse_ratio": float(p["sparse_ratio"]),
                "kv_ratio": float(p["kv_ratio"]),
                "local_range": int(p["local_range"]),
                "seed": int(p["seed"]),
            },
        },
        "4": {
            "class_type": "VHS_VideoCombine",
            "inputs": {
                "images": ["3", 0],
                "frame_rate": float(frame_rate or 24.0),
                "loop_count": 0,
                "filename_prefix": filename_prefix,
                "format": "video/h264-mp4",
                "pingpong": False,
                "save_output": True,
            },
        },
    }
    if has_audio:
        # FlashVSR 只处理画面（IMAGE），音轨由 LoadVideoPath 直接旁路到 VideoCombine
        wf["4"]["inputs"]["audio"] = ["1", 2]
    return wf


def _history_error(entry: dict) -> str:
    """从 history 中提取可读的失败原因（含节点 id / 异常文本）"""
    status = (entry or {}).get("status", {}) or {}
    msgs = status.get("messages") or []
    parts = []
    for m in msgs:
        try:
            if isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] == "execution_error":
                d = m[1] or {}
                parts.append(
                    f"节点 {d.get('node_id')}({d.get('node_type')}) 执行异常: "
                    f"{d.get('exception_type')}: {str(d.get('exception_message'))[:400]}"
                )
            elif isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] in ("execution_interrupted",):
                parts.append("执行被中断")
        except Exception:
            continue
    if not parts:
        parts.append(json.dumps(status, ensure_ascii=False)[:500])
    return " | ".join(parts)


# ============================ 主流程 ============================

class VideoUpscaler:
    """成片/视频超分执行器（真实现，非占位）"""

    def __init__(self, base_url: str = COMFYUI_URL):
        self.base_url = base_url
        self.client = ComfyUIClient(base_url)

    # ---------- 入口 ----------

    def upscale(self, input_video: str, project_name: str = "project", scale: int = 2,
                mode: Optional[str] = None, timeout: Optional[int] = None,
                engine: Optional[str] = None,
                progress_cb=None,
                segment_seconds: Optional[int] = None,
                chunk_seconds: Optional[int] = None,
                **overrides) -> Dict:
        """对 input_video 执行 FlashVSR 超分，返回结构化结果；失败抛 UpscaleError

        engine: "te-speed-flashvsr"（默认，加速链路）/ "legacy-flashvsr"（旧链路回退）

        ⭐ 2026-10-13（t9）**场内切片超分**（本函数的默认行为）：
          长片段（如一场 164s）整段提交会同时击穿两条硬限制 ——
            ① 显存：VHS_LoadVideoPath(frame_load_cap=0) 把**全部帧**读进内存，
               164s@24fps/960x544 ≈ 3949 帧 ≈ 23GiB → 8G 卡必然 OOM；
            ② 超时：实测约 1.96 s/帧 → 一场 ≈ 2.15 小时 > 单次等待上限。
          做法：把片段按不超过 segment_seconds 秒（默认 10）切成若干片（ffmpeg -ss/-t +
          **-c copy，不重编码**）→ 逐片超分 → 拼回整段。下游「逐场超分→流拼接整集」不变。

          参数：
            segment_seconds / chunk_seconds：切片时长（秒），二者同义，segment_seconds 优先。
              缺省（None）= 用 DEFAULT_CHUNK_SECONDS(10)；**显式传 0/负数 = 关闭切片**
              （回到整段一次超分的旧行为，供兼容/回退）。
              只有「片段时长 > 切片时长」时才真的切片；否则仍走单次提交（短片段无收益）。
          失败语义（保持 fail-open）：**任一切片超分失败 → 自动整体回落整段超分**；
            切片/拼回失败同样回落；回落再失败才抛 UpscaleError。
        """
        # 切片时长解析：segment_seconds 优先，其次 chunk_seconds（两条入口名同义）
        _raw_seg = segment_seconds if segment_seconds is not None else chunk_seconds
        _seg = normalize_chunk_seconds(_raw_seg if _raw_seg is not None else DEFAULT_CHUNK_SECONDS,
                                       DEFAULT_CHUNK_SECONDS)
        return self._upscale_with_chunks(
            input_video, project_name=project_name, scale=scale, mode=mode,
            timeout=timeout, engine=engine, progress_cb=progress_cb,
            segment_seconds=_seg, **overrides)

    def _upscale_with_chunks(self, input_video: str, project_name: str = "project",
                             scale: int = 2, mode: Optional[str] = None,
                             timeout: Optional[int] = None, engine: Optional[str] = None,
                             progress_cb=None, segment_seconds: int = DEFAULT_CHUNK_SECONDS,
                             **overrides) -> Dict:
        """切片编排：切 → 逐片超分 → 拼回整段。任何一步不行都**整体回落整段超分**。"""
        t0 = time.time()

        def step(msg, pct=None):
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except Exception as e:
                    logger.debug("进度回调异常（忽略，不阻断超分）：%s", e)
            logger.info(f"[超分] {msg}")

        def _whole(why: str) -> Dict:
            """回落：整段一次超分（segment_seconds=0 防重入，绝不递归回切片）"""
            logger.warning("超分回落整段路径（%s）：%s", why, os.path.basename(input_video or ""))
            return self._upscale_once(
                input_video, project_name=project_name, scale=scale, mode=mode,
                timeout=timeout, engine=engine, progress_cb=progress_cb,
                segment_seconds=0, **overrides)

        if int(segment_seconds or 0) <= 0:
            return _whole("切片已关闭")
        if not input_video or not os.path.exists(input_video):
            # 交给 _upscale_once 报出原始错误（这里不抢先改写错误语义）
            return _whole("输入不存在/不可读")

        src = probe_video(input_video)
        if not src.get("ok"):
            return _whole("源视频不可解析")
        dur = float(src.get("duration") or 0)
        if dur <= 0 or dur <= float(segment_seconds):
            logger.info("切片超分：源片 %.2fs <= 切片时长 %ss，无需切片，直接整段超分",
                        dur, segment_seconds)
            return _whole("片段不长于切片时长")

        # 切片目录：与正式产物同盘的 _chunks/，**整场跑完无条件清理**（含异常路径）
        try:
            base = os.path.abspath(os.path.join(UPSCALE_DIR, project_name or "project"))
        except Exception:  # noqa: BLE001
            base = os.path.abspath(".")
        tag = time.strftime("%Y%m%d_%H%M%S")
        work_dir = os.path.join(base, "_chunks",
                                f"{os.path.splitext(os.path.basename(input_video))[0][:60]}_{tag}")

        tot_in = 0.0        # 各片超分耗时之和（标定用；含排队与模型装载）
        try:
            step(f"场内切片：{dur:.1f}s → 目标每片 <= {segment_seconds}s（不重编码）", 5)
            chunks = segment_video_nocopy(input_video, work_dir, segment_seconds)
            if not chunks:
                return _whole("切片失败")

            up_chunks: List[str] = []
            for i, c in enumerate(chunks):
                c_info = probe_video(c)
                c_dur = float(c_info.get("duration") or 0)
                pct = 5 + int(round(70.0 * (i / max(1, len(chunks)))))
                step(f"切片 {i + 1}/{len(chunks)}（{c_dur:.1f}s，FlashVSR {int(scale)}x）…", pct)

                def _chunk_prog(m, _p=None, _tag=f"{i + 1}/{len(chunks)}"):
                    _head = f"切片 {_tag}"
                    if _p is not None:
                        try:
                            _head += f"（{int(float(_p))}%）"
                        except (TypeError, ValueError):
                            pass
                    step(f"{_head}：{m}", pct)

                r = self._upscale_once(
                    c, project_name=project_name, scale=scale, mode=mode,
                    timeout=timeout, engine=engine, progress_cb=_chunk_prog,
                    segment_seconds=0, **overrides)
                up = (r or {}).get("output_path") or ""
                if not up or not os.path.isfile(up):
                    raise UpscaleError(f"第 {i + 1}/{len(chunks)} 片超分未产出文件")
                up_chunks.append(up)
                tot_in += float((r or {}).get("elapsed_sec") or 0)

            # 拼回「整段」：与整段超分同一落盘命名口径（<stem>_upscaled_<n>x_<ts>.mp4）
            stem = os.path.splitext(os.path.basename(input_video))[0].strip("_")
            out_dir = os.path.abspath(os.path.join(UPSCALE_DIR, project_name or "project"))
            os.makedirs(out_dir, exist_ok=True)
            joined = os.path.join(out_dir, f"{stem}_upscaled_{int(scale)}x_{tag}.mp4")
            step(f"切片超分完成，拼回整段（{len(up_chunks)} 片）…", 92)
            merged = concat_segments_nocopy(up_chunks, joined)
            if not merged or not os.path.isfile(merged):
                raise UpscaleError("切片产物拼回整段失败")

            dst = probe_video(merged)
            if not dst.get("ok") or float(dst.get("duration") or 0) <= 0:
                raise UpscaleError(f"拼回产物不可播放：{dst.get('error')}")
            elapsed = round(time.time() - t0, 1)
            step(f"完成（切片 {len(up_chunks)} 片）：{dst['width']}x{dst['height']}，"
                 f"耗时 {elapsed}s", 100)
            result = {
                "success": True,
                "engine": "te-speed-flashvsr",
                "engine_chain": f"segment(-c copy) x{len(up_chunks)} → FlashVSR → concat",
                "accelerated": True,
                "params": {},
                "prompt_id": "",
                "input_path": os.path.abspath(input_video),
                "output_path": merged,
                "output_filename": os.path.basename(merged),
                "comfyui_output": "",
                "scale": int(scale),
                "mode": str(mode or ""),
                "before": src,
                "after": dst,
                "size_delta_mb": round(dst["size_mb"] - src["size_mb"], 2),
                "elapsed_sec": elapsed,
                # ---- 切片专属审计字段（哪个路径、几片、单片/合计耗时）----
                "chunked": True,
                "chunk_seconds": int(segment_seconds),
                "chunk_count": len(up_chunks),
                "chunk_paths": list(up_chunks),
                "chunk_elapsed_sum_sec": round(tot_in, 1),
                "elapsed_sec_per_chunk": round(tot_in / max(1, len(up_chunks)), 1),
            }
            try:
                result["calibration_file"] = save_calibration({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "engine": "te-speed-flashvsr",
                    "input": os.path.abspath(input_video),
                    "before": f"{src.get('width')}x{src.get('height')}",
                    "after": f"{dst.get('width')}x{dst.get('height')}",
                    "frames": src.get("frames") or 0,
                    "mode": str(mode or ""),
                    "scale": int(scale),
                    "elapsed_sec": elapsed,
                    # 切片标定：离线预估按「每片耗时」外推，比按整段更准
                    "chunked": True,
                    "chunk_seconds": int(segment_seconds),
                    "chunk_count": len(up_chunks),
                    "chunk_elapsed_sum_sec": round(tot_in, 1),
                    "input_duration_sec": round(dur, 3),
                    "per_chunk_sec": round(tot_in / max(1, len(up_chunks)), 1),
                })
            except Exception as e:  # noqa: BLE001
                logger.warning(f"写耗时标定失败（不影响结果）: {e}")
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("切片超分未走通（%s: %s），回落整段超分", type(e).__name__, e)
            return _whole(f"切片路径异常：{type(e).__name__}: {e}")
        finally:
            # G8：切片件是**临时件**，成功/失败/异常都清掉（正式产物已归档到 out_dir）
            try:
                if os.path.isdir(work_dir):
                    shutil.rmtree(work_dir, ignore_errors=True)
                # 顺手收掉空的 _chunks/ 容器目录：空目录留着会让「临时目录是否清理干净」
                # 这条断言永远为假（本次实测踩到），也容易被误读成「还有残留」。
                _parent = os.path.dirname(work_dir)
                if os.path.isdir(_parent) and not os.listdir(_parent):
                    os.rmdir(_parent)
            except Exception as _ce:  # noqa: BLE001
                logger.debug("清理切片临时目录失败（忽略）：%s", _ce)

    def _upscale_once(self, input_video: str, project_name: str = "project", scale: int = 2,
                      mode: Optional[str] = None, timeout: Optional[int] = None,
                      engine: Optional[str] = None,
                      progress_cb=None, **overrides) -> Dict:
        """**单次**超分（不做切片编排）。切片路径逐片调用它；回落路径也调它。

        engine: "te-speed-flashvsr"（默认，加速链路）/ "legacy-flashvsr"（旧链路回退）
        """
        t0 = time.time()

        def step(msg, pct=None):
            if progress_cb:
                try:
                    progress_cb(msg, pct)
                except Exception as e:
                    logger.debug("进度回调异常（忽略，不阻断超分）：%s", e)
            logger.info(f"[超分] {msg}")

        # 0) 输入校验
        if not input_video or not os.path.exists(input_video):
            raise UpscaleError(f"输入视频不存在: {input_video}")
        if not input_video.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".webm")):
            raise UpscaleError(f"不支持的输入格式: {os.path.basename(input_video)}")

        # 1) 环境校验（模型 / 节点 / ComfyUI）
        env = check_environment(self.base_url)
        if not env["available"]:
            raise UpscaleError("超分环境不可用：" + "；".join(env["reasons"]))
        # 1.5) 引擎选择（默认 TE-Speed 加速链路；不可用时自动回退旧链路，不静默降级为占位）
        used_engine = resolve_engine(engine, env)
        step(f"超分引擎：{used_engine}" + ("（TE-Speed 加速链路）" if used_engine == "te-speed-flashvsr"
             else "（旧 FlashVSR 链路，无加速）"), 3)

        src = probe_video(input_video)
        if not src.get("ok"):
            raise UpscaleError(f"读取输入视频参数失败：{src.get('error')}")
        step(f"输入视频 {src['width']}x{src['height']} @{src['fps']}fps "
             f"{src['duration']}s {src['size_mb']}MB", 5)

        # 2) 结果落盘目录（与原始视频隔离）
        out_dir = os.path.abspath(os.path.join(UPSCALE_DIR, project_name))
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(input_video))[0].strip("_")
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_name = f"{stem}_upscaled_{int(scale)}x_{ts}.mp4"
        out_path = os.path.join(out_dir, out_name)
        # ComfyUI 侧先落到 output/upscale/<project>/ 子目录，便于统一回收
        comfy_prefix = f"upscale/{project_name}/{stem}_{int(scale)}x_{ts}"

        # 3) 构造并自检工作流（按引擎选择模板链路）
        if used_engine == "te-speed-flashvsr":
            wf = build_te_workflow(
                input_video=os.path.abspath(input_video),
                filename_prefix=comfy_prefix,
                scale=scale,
                mode=mode or TE_UPSCALE_DEFAULT_PARAMS["mode"],
                frame_rate=src["fps"] or 24.0,
                has_audio=src["has_audio"],
                attach_audio=bool(overrides.get("attach_audio", True)),
                # ⚠️ 编排参数绝不透传给工作流构建器（它们是**编排**参数，不是节点输入；
                #    误传会让 TEFlashVSRTuning 收到未知字段 → 提交前自检直接失败）。
                **{k: v for k, v in overrides.items()
                   if k not in ("attach_audio", "segment_seconds", "chunk_seconds")},
            )
        else:
            wf = build_upscale_workflow(
                input_video=os.path.abspath(input_video),
                filename_prefix=comfy_prefix,
                scale=scale,
                mode=mode,
                frame_rate=src["fps"] or 24.0,
                has_audio=src["has_audio"],
                **{k: v for k, v in overrides.items()
                   if k not in ("segment_seconds", "chunk_seconds")},
            )
        # G9：同一进程内重复提交**完全相同的源片 + 倍率**是 bug（会白烧一次超分），显式告警。
        #   （只告警、不阻断：正常业务里同一源片换 mode/参数重跑是合法的，判重只看
        #   源片路径 + 尺寸/帧率/时长 + 倍率这一组"本应命中幂等"的键。）
        _src_key = f"{os.path.abspath(input_video)}|{int(scale)}|{src['width']}x{src['height']}" \
                   f"@{src['fps']}:{src['duration']}"
        _seen = getattr(self, "_seen_src_keys", None)
        if _seen is None:
            _seen = set()
            self._seen_src_keys = _seen
        if _src_key in _seen:
            logger.error("超分重复提交：同一源片 + 同一倍率本次进程内已提交过（%s，scale=%s）"
                         "—— 白烧一次超分，请检查调用方是否绕过了幂等/断点续跑判断",
                         os.path.basename(input_video), scale)
        _seen.add(_src_key)
        report = self.client.validate_api_prompt(wf)
        if (report["unknown_types"] or report["missing_required"]
                or report["dangling_links"] or report.get("unexpected_inputs")):
            raise UpscaleError(f"超分工作流自检失败: {json.dumps(report, ensure_ascii=False)[:500]}")
        # 节点 id "2" 是 build_te_workflow / build_upscale_workflow 的固定拓扑 key：
        #   TE 链路 "2"=TEFlashVSRModelLoader（含 mode/precision/device），旧链路 "2"=FlashVSRInitPipe（含 mode）。
        #   两个构建器都硬编码节点 id（"1"~"5"），此处按同一契约读取 mode。
        used_mode = (wf["2"]["inputs"].get("mode")
                     or wf["2"]["inputs"].get("mode_choice") or mode or "")
        chain = "→".join(dict.fromkeys(v["class_type"] for v in wf.values()))
        step(f"工作流自检通过（{report['node_count']} 节点，链路 {chain}，"
             f"mode={used_mode}，scale={scale}）", 10)

        # 3.5) 释放 ComfyUI 已缓存模型显存（8G 笔记本显存紧张，避免超分 OOM）
        # 默认已改为**不发 /free**（config.KEEP_MODEL_LOADED=True）：/free 的
        # unload_models 会把模型连 RAM 权重一起释放，下次生成要从磁盘重读
        # 19.5GB 模型，是「每次都要重新加载」的根因。显存不够时 ComfyUI 会自行
        # 把暂不用的模型 offload 到 RAM，不需要我们提前卸。需要旧行为请显式传
        # free_vram=True 或设 MJSCXT_KEEP_MODEL_LOADED=0。
        _free_vram_default = (
            TE_UPSCALE_DEFAULT_PARAMS if used_engine == "te-speed-flashvsr"
            else UPSCALE_DEFAULT_PARAMS
        ).get("free_vram", not KEEP_MODEL_LOADED)
        if overrides.get("free_vram", _free_vram_default):
            # B-01 P1-12：/free 互斥守卫 —— 本进程无其它 running GPU 任务时才发，
            # 避免卸掉分镜/视频/关键帧等其它任务正在使用的模型（反复换入换出）。
            try:
                import gpu_task_gate
                _self_task_id = getattr(self, "current_task_id", "") or ""
                if gpu_task_gate.has_other_running_gpu_tasks(_self_task_id):
                    logger.info("B-01 /free 守卫：本进程有其它 running GPU 任务，跳过 /free（避免卸他人模型）")
                else:
                    r = requests.post(f"{self.base_url}/free",
                                      json={"unload_models": True, "free_memory": True}, timeout=30)
                    step(f"已请求 ComfyUI 释放显存（HTTP {r.status_code}）", 12)
            except Exception as e:
                logger.warning(f"释放显存请求失败（不影响继续）: {e}")

        # 4) 提交
        try:
            prompt_id = self.client.queue_prompt(wf)
        except Exception as e:
            raise UpscaleError(f"提交 ComfyUI 队列失败: {e}")
        if not prompt_id:
            raise UpscaleError("ComfyUI 未返回 prompt_id，提交失败")
        step(f"已提交任务 {prompt_id}", 15)

        # 5) 等待完成（带进度回调）
        entry = self._wait(prompt_id, int(timeout or UPSCALE_DEFAULT_PARAMS["timeout"]),
                           step, t0)
        status = (entry or {}).get("status", {}) or {}
        if status.get("status_str") != "success":
            raise UpscaleError(f"超分执行失败：{_history_error(entry)}")
        step("ComfyUI 执行完成，回收产物", 90)

        # 6) 回收产物到项目 output/upscale/<project>/
        files = [f for f in self.client.get_output_files(entry, ".mp4")
                 if os.path.exists(f)]
        if not files:
            raise UpscaleError("执行成功但未找到输出 mp4（视频合成节点未产出文件）")
        # VHS 在有音轨时会同时产出 <name>.mp4 与 <name>-audio.mp4（后者已混流音轨），优先取后者
        audio_files = [f for f in files if f.lower().endswith("-audio.mp4")]
        newest = max(audio_files or files, key=lambda p: os.path.getmtime(p))
        try:
            shutil.copy2(newest, out_path)
        except Exception as e:
            raise UpscaleError(f"超分产物拷贝失败（源 {newest} → 目标 {out_path}）: {e}")

        dst = probe_video(out_path)
        if not dst.get("ok"):
            raise UpscaleError(f"超分产物校验失败：{dst.get('error')}")
        elapsed = round(time.time() - t0, 1)
        step(f"完成：{dst['width']}x{dst['height']}，耗时 {elapsed}s", 100)

        result = {
            "success": True,
            "engine": used_engine,
            "engine_chain": chain,
            "template_path": UPSCALE_TE_TEMPLATE_PATH if used_engine == "te-speed-flashvsr" else "",
            "accelerated": used_engine == "te-speed-flashvsr",
            # 节点 id "3" 是 build_te_workflow 的固定拓扑 key：TE 链路 "3"=TEFlashVSRTuning，
            #   attention_backend/spatial_strategy/memory_policy/max_tile_edge/blend_overlap/
            #   attention_budget/kv_retention/local_radius/quality_profile/preprocess_batch 全在此节点。
            #   仅 TE 链路回读参数（旧链路节点结构不同，else 返回 {}）。
            "params": {k: wf["3"]["inputs"].get(k) for k in (
                "attention_backend", "spatial_strategy", "memory_policy", "max_tile_edge",
                "blend_overlap", "attention_budget", "kv_retention", "local_radius",
                "quality_profile", "preprocess_batch")} if used_engine == "te-speed-flashvsr" else {},
            "prompt_id": prompt_id,
            "input_path": os.path.abspath(input_video),
            "output_path": out_path,
            "output_filename": out_name,
            "comfyui_output": newest,
            "scale": int(scale),
            "mode": used_mode,
            "before": src,
            "after": dst,
            "size_delta_mb": round(dst["size_mb"] - src["size_mb"], 2),
            "elapsed_sec": elapsed,
        }
        # 记录耗时标定（离线预估用；不参与执行）
        try:
            result["calibration_file"] = save_calibration({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "engine": used_engine,
                "input": os.path.abspath(input_video),
                "before": f"{src['width']}x{src['height']}",
                "after": f"{dst['width']}x{dst['height']}",
                # 审计 P2-22（2026-09-29）：probe_video 返回的键名是 `frames`（没有
                # frame_count）—— 旧写法恒取 0，标定记录的帧数失真、离线预估不准
                "frames": src.get("frames") or 0,
                "mode": used_mode,
                "scale": int(scale),
                "elapsed_sec": elapsed,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"写耗时标定失败（不影响结果）: {e}")
        return result

    # ---------- 轮询 ----------

    def _recheck_after_timeout(self, prompt_id: str) -> tuple:
        """t22：到点复查远端 —— 返回 ("done"|"error"|"running"|"gone", entry)。

        优先复用 comfyui_client.ComfyUIClient._recheck_after_timeout（H3 那套：同一个
        ComfyUI 后端、同一套 status 语义 → 行为与日志口径天然一致）；注入的是自定义
        client（测试替身）时退回本模块自己的实现。
        """
        _rc = getattr(self.client, "_recheck_after_timeout", None)
        if callable(_rc):
            try:
                return _rc(prompt_id)
            except Exception as _e0:  # noqa: BLE001
                logger.debug("超分到点复查（复用 H3 实现）失败，改用本地实现：%s", _e0)
        try:
            hist = self.client.get_history(prompt_id) or {}
            entry = hist.get(prompt_id) if isinstance(hist, dict) else None
        except Exception as _e1:  # noqa: BLE001
            logger.debug("超分到点复查 history 失败（按仍在跑处理）：%s", _e1)
            return "running", {}
        if isinstance(entry, dict):
            st = entry.get("status") or {}
            if st.get("completed") or st.get("status_str") == "success":
                return "done", entry
            if st.get("status_str") == "error":
                return "error", entry
        try:
            q = requests.get(f"{self.base_url}/queue", timeout=15).json()
            qids = [x[1] for x in (q.get("queue_running") or [])
                    if isinstance(x, (list, tuple)) and len(x) > 1]
            qids += [x[1] for x in (q.get("queue_pending") or [])
                     if isinstance(x, (list, tuple)) and len(x) > 1]
        except Exception as _e2:  # noqa: BLE001
            logger.debug("超分到点复查 /queue 失败（按仍在跑处理）：%s", _e2)
            return "running", {}
        return ("running" if prompt_id in qids else "gone"), {}

    def _wait(self, prompt_id: str, timeout: int, step, t0: float) -> dict:
        # t77：对齐 comfyui_client.wait_for_completion 的 max(1, int(timeout)) 防御 ——
        # timeout=0 / 极小值时 deadline 至少往后推 1s，不会立即误判超时 + interrupt。
        deadline = t0 + max(1, int(timeout))
        # ⭐ t22：软超时续等（与 H3 那套**完全同名同语义**，见 comfyui_client.wait_for_completion）：
        #   到点先复查远端 —— 已完成 → 取回结果；仍在执行 → 续等；续等用尽才 interrupt + 报超时。
        #   env：MJSCXT_WAIT_EXTENSIONS（默认 3）、MJSCXT_WAIT_EXTEND_SECONDS（默认 1800）。
        _ext_left = _env_int("MJSCXT_WAIT_EXTENSIONS", 3)
        _ext_total = _ext_left
        _ext_sec = max(1, _env_int("MJSCXT_WAIT_EXTEND_SECONDS", 1800))
        last_note = 0.0
        while time.time() < deadline:
            # S9：中止信号检查（cancellation.should_stop 无注册时恒 False，不影响普通路径）
            if cancellation.should_stop():
                logger.warning("超分等待期间收到中止信号，打断远端任务 %s", prompt_id)
                self.client.interrupt(prompt_id)
                raise cancellation.Cancelled(f"超分远端等待期间收到中止信号：{prompt_id}")
            try:
                hist = self.client.get_history(prompt_id)
            except cancellation.Cancelled:
                raise
            except Exception as e:
                logger.debug(f"查询 history 失败: {e}")
                hist = {}
            if prompt_id in hist:
                entry = hist[prompt_id]
                st = (entry.get("status") or {}).get("status_str")
                if st == "success":
                    return entry
                if st == "error":
                    logger.error(f"超分执行出错: {_history_error(entry)}")
                    return entry
            now = time.time()
            if now - last_note >= 15:
                last_note = now
                elapsed = int(now - t0)
                try:
                    q = requests.get(f"{self.base_url}/queue", timeout=15).json()
                    running = q.get("queue_running") or []
                    pending = q.get("queue_pending") or []
                    mine = next((i for i, item in enumerate(pending)
                                 if len(item) > 1 and item[1] == prompt_id), None)
                    if mine is not None:
                        note = f"排队中（前方 {mine} 个任务），已等待 {elapsed}s"
                        pct = 20
                    elif any(len(item) > 1 and item[1] == prompt_id for item in running):
                        note = f"超分执行中…已耗时 {elapsed}s（FlashVSR 逐帧处理，请耐心等待）"
                        pct = 45
                    else:
                        note = f"等待超分结果…已耗时 {elapsed}s"
                        pct = 30
                except Exception:
                    note = f"超分执行中…已耗时 {elapsed}s"
                    pct = 45
                step(note, pct)
            # S9：可被打断的短休眠（3s 轮询间隔），暂停时最多 0.25s 即有反应
            cancellation.sleep(3)
            # ---- ⭐ t22：到点复查（软超时续等）----
            #   旧实现到点后**无条件** interrupt + 抛 UpscaleError：慢机器上 FlashVSR 逐帧
            #   处理常「刚好超过 3600s」，于是逐场超分整体失败、退化为整集超分（更慢更耗内存）。
            #   现在先复查：已完成 → 直接取回；仍在跑 → 续等（默认 3 × 1800s）。
            if _ext_left > 0 and time.time() >= deadline:
                _st22, _e22 = self._recheck_after_timeout(prompt_id)
                if _st22 == "done" and isinstance(_e22, dict):
                    logger.info("[超分续等] %s 在到点时刻已完成 → 取回结果（远端未取消）",
                                prompt_id)
                    return _e22
                if _st22 == "running":
                    _ext_left -= 1
                    deadline = time.time() + _ext_sec
                    logger.warning(
                        "[超分续等] %s 已等 %ds 仍未完成，但远端**仍在执行** → 续等 %ds"
                        "（剩余 %d 次）—— 绝不取消「刚好差一点」的任务（那是白烧一次超分）",
                        prompt_id, int(time.time() - t0), _ext_sec, _ext_left)
        # ③ 真超时（非中止）：仍清理远端，避免本地判超时而远端白跑
        logger.warning("超分等待超时，清理远端队列: %s", prompt_id)
        self.client.interrupt(prompt_id)
        raise UpscaleError(
            f"超分等待超时（>{timeout}s，已续等 {_ext_total - _ext_left} 次，prompt_id={prompt_id}）。"
            f"可调大 MJSCXT_UPSCALE_TIMEOUT / MJSCXT_WAIT_EXTENSIONS / MJSCXT_WAIT_EXTEND_SECONDS；"
            f"或在 ComfyUI 队列查看该任务；也可降低倍率/改用 tiny-long 模式后重试"
        )

    # ---------- 供旧接口复用 ----------

    def upscale_to(self, input_video: str, output_path: str, scale: int = 2,
                   project_name: Optional[str] = None, **kwargs) -> Dict:
        """兼容旧调用：把超分结果额外拷贝到指定 output_path"""
        project_name = project_name or os.path.basename(os.path.dirname(output_path)) or "project"
        res = self.upscale(input_video, project_name=project_name, scale=scale, **kwargs)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        shutil.copy2(res["output_path"], output_path)
        res["output_path"] = output_path
        return res
