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
                progress_cb=None, **overrides) -> Dict:
        """对 input_video 执行 FlashVSR 超分，返回结构化结果；失败抛 UpscaleError

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
                **{k: v for k, v in overrides.items() if k != "attach_audio"},
            )
        else:
            wf = build_upscale_workflow(
                input_video=os.path.abspath(input_video),
                filename_prefix=comfy_prefix,
                scale=scale,
                mode=mode,
                frame_rate=src["fps"] or 24.0,
                has_audio=src["has_audio"],
                **overrides,
            )
        report = self.client.validate_api_prompt(wf)
        if (report["unknown_types"] or report["missing_required"]
                or report["dangling_links"] or report.get("unexpected_inputs")):
            raise UpscaleError(f"超分工作流自检失败: {json.dumps(report, ensure_ascii=False)[:500]}")
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

    def _wait(self, prompt_id: str, timeout: int, step, t0: float) -> dict:
        deadline = t0 + timeout
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
        # S9：超时（非中止）也清理远端队列，避免本地判超时而远端白跑
        logger.warning("超分等待超时，清理远端队列: %s", prompt_id)
        self.client.interrupt(prompt_id)
        raise UpscaleError(
            f"超分等待超时（>{timeout}s，prompt_id={prompt_id}）。"
            f"可在 ComfyUI 队列查看该任务，或降低倍率/改用 tiny-long 模式后重试"
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
