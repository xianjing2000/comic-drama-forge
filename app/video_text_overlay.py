# -*- coding: utf-8 -*-
r"""文字类道具**后期叠加**合成（T6，2026-10-12）—— 修复「楼层安全守则小字 / 员工卡」文字乱码。

## 根因（已核实，不再重复验证）
H3 视频生成是**参考重绘**：它会把分镜图里的文字重新画一遍。分镜层的「文字道具特写提权」
（qc_helpers.py:212-240）只能修静帧、管不到视频层；资产图本身文字逐字正确（视觉复核：
守则 8 条全文可读、员工卡正反面清晰）。因此「成片后期叠加」是唯一**不依赖扩散模型写字能力**
的路径 —— 本模块就是这条路。

## 与 video_watermark.py 的关系（不改它，只复用）
video_watermark.py 是「水印」语义（前端 AIVaultPage 直接绑定），本体一字不动。本模块复用它的：
  · resolve_font()      中文字体解析（msyh/simhei/simsun …；返回空串表示无可用字体）
  · _esc_text()/_esc_path()  drawtext 文本转义 / 滤镜内路径转义
  · 命令模板与返回契约：libx264 -preset veryfast -crf 20 / {ok,out_path,cmd,elapsed,error,…}
并扩展它做不到的三件事：**多段时间轴**、**任意目标区域**、**图片贴片（RGBA 正确合成）**。

## 三条硬约束（对应 t6 验收）
① 时间轴 = pipeline.step_final 里**既有**的换算口径
   「成片 ffprobe 实测总时长 × 各镜剧本 duration 占比」（当前 pipeline.py L1485-1505 的
   subs/cur 循环）。唯一实现是本模块 shot_time_spans()。
② 文本来源 = **资产图 / 小说原文**（app/canon_data.json）—— **禁止**读 shot.surface_text。
   实测同一项目里存在三套互不相同的守则文本（取舍见 PROP_TEXT_POLICY 注释）。
③ Windows 路径安全：滤镜串里**不出现任何文件路径** ——
   · 图片只作为 ffmpeg 输入（[1:v]/[2:v]…），路径不进滤镜；
   · 字体走「cwd 切到字体目录 + 只传裸文件名」，与 video_postprocess.py:533-537 同一做法；
   · 输入/输出用绝对路径由 subprocess 列表参数直传（无 shell，不存在引号/反斜杠吞噬）。

## fail-open
任何一步失败（没 ffmpeg / 没字体 / 资产缺失 / 滤镜构造失败 / ffmpeg 非 0）都只返回
{ok: False, error: …} 并打 warning，由调用方（pipeline.step_final）按「失败不阻断成片」处理。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time

import video_watermark as _wm

logger = logging.getLogger(__name__)

#: 叠加策略表（dict 而非散落的模块常量：阈值/开关必须成套看，也不会被 config_doctor
#: 当成「未登记的可配置常量」—— 这些是算法标定值，不是用户可调项）
OVERLAY_POLICY = {
    "enabled": True,
    "max_items": 24,
    "min_span_sec": 0.4,
    "alpha_default": 1.0,
    "crf": 20,
    "preset": "veryfast",
}

#: 道具别名（剧本里同一个道具的多种写法 → 资产目录名）。
#: 只做**显式登记**，绝不改成自动模糊匹配：猜错＝给道具挂错图，比不挂更糟。
#: 依据：进境_整本小说 第1集剧本里「员工证」出现 26 次、「员工卡」38 次，而资产目录只有
#: 「天合科技员工卡」—— 用户反馈的「工牌前后不一致」正是这个混用（t3 实测）。
PROP_ALIASES = {
    "天合科技员工证": "天合科技员工卡",
    "员工证": "天合科技员工卡",
    "员工卡": "天合科技员工卡",
    "工牌": "天合科技员工卡",
    "安全守则": "楼层安全守则",
}

#: 默认叠加注册表：道具名 → 目标区域 / 素材裁切 / 透明度。
#: box 用**相对比例** (x, y, w, h)（相对成片画幅），与分辨率解耦；
#: crop_frac 用**相对比例**（相对资产图）裁出道具本体。
DEFAULT_PROP_OVERLAYS = {
    # 守则：**右上角小面板**（t18 优化前的旧值保留在 PROP_OVERLAY_HISTORY 供对照）。
    #
    # ⚠️ 为什么从「左侧 40% 宽竖版面板」改成「右上 26% 宽」——t6 的验收只钉住了
    #    「文字清晰可读」，但视觉复核发现那块白色纸面**直接盖在画面主体上**
    #    （shot#21 盖住手机屏幕、shot#25 盖住手持员工卡）。文字可读却挡住主体，
    #    等于用一个新问题换掉一个旧问题，故 t18 裁决必须优化。
    #
    # ⚠️ 本项目的守则镜里**画面本来就没有守则纸张**（实测：shot#21 手机屏是地图、
    #    shot#25 手里是员工卡、shot#52 手持文件），所以叠加本质是「凭空贴一块板」，
    #    不存在「贴回纸面原位」这种更优解 —— 只能选一块不压主体的空白区。
    #
    # 位置怎么定的（不是拍脑袋）：逐镜视觉定位跨镜空白区 → shot#21/25/28/52 的
    #   主体分别在中央手机屏 / 左侧人物+手持卡 / 左侧人物 / 下方对角纸张，
    #   **右上背景区在四镜里都是空的**（X≈650..950, Y≈0..300..450）。
    #
    # 尺寸怎么定的（实测标定，见 .local/t18_cand.txt + 视觉逐字判定）：
    #   40% 宽（旧）面积 28.2% —— 可读但压主体
    #   30% 宽 面积 14.8% —— 8 条全读通
    #   26% 宽 面积 12.0% —— 8 条全读通，判定「依然非常清晰锐利」  ← 取这档
    #   22% 宽 面积  9.3% —— 8 条全读通，但「字变小，需仔细辨认」
    #   19% 宽 面积  6.6% —— 「需凑近屏幕或眯眼」，已到可读下限，不取
    # 取 26%：留一档余量，不让清晰度贴着下限走。
    "楼层安全守则": {"box": (0.720, 0.040, 0.260, 0.580), "crop_frac": None,
                     "alpha": 1.0, "kind": "image"},
    # 员工卡：资产图是「正反面并排」的两张竖卡 → 只裁**正面卡的信息带**
    # （姓名/工号/请勿转借那两行），裁切比例经视觉定位：
    # 资产 1248x1248 里正面卡文字带 ≈ x 112..674、y 793..1043。
    # 整卡缩到角落时字太小（实测 212px 宽的卡，文字约 10px 字高、不可读），
    # 只贴信息带才能既小面积又清晰。
    "天合科技员工卡": {"box": (0.500, 0.660, 0.440, 0.280),
                       "crop_frac": (0.090, 0.635, 0.450, 0.200), "alpha": 1.0, "kind": "image"},
}

#: 历史值对照（t18）：只用于回归对照与日志，**不参与实际叠加**。
#: 留着它，是为了让「为什么现在是这个位置/大小」可被后人核对，而不是只剩一个孤零零的数字。
PROP_OVERLAY_HISTORY = {
    "楼层安全守则": [
        {"version": "t6", "box": (0.020, 0.060, 0.400, 0.880),
         "note": "左侧竖版面板，占宽 40%/面积 28.2%：文字可读但压住画面主体（已废弃）"},
        {"version": "t18", "box": (0.720, 0.040, 0.260, 0.580),
         "note": "右上空白区，占宽 26%/面积 12.0%：8 条仍逐字可读且不压主体（当前）"},
    ],
}

#: ⛔ 项B 判定（2026-10-12 实测）：**「印章」不纳入叠加**，维持 fail-open。
#:
#: 关键实测（比"缺资产图"更严重，是**H3 幻觉产物**）：
#:   · 成片 shot#52 纸上**确实有一个方形红色印记**（约 x750..900, y150..300），
#:     视觉复核判「有可辨认文字，但不太清晰，难以准确辨认」→ 这就是用户报的印章乱码；
#:   · 但**资产图里根本没有印章** —— 守则 base.png 右下角是空白的，纸上只有
#:     红色圈注（圈住第 4/5/6/8 条序号），**没有**任何方形/圆形红色印记、也没有手写批注字；
#:   · 小说原文 / 剧本 / canon_data.json 里「印章 / 红章 / 公章」均 **0 次**。
#: → 结论：那个印章是 H3 参考重绘时**凭空画出来的**，不是资产的一部分。
#:   这种情形**不能**用后期叠加"补一个正确的印章"来修 —— 那等于替幻觉背书、
#:   把一条原文里不存在的设定钉进成片（且没有任何权威文本可核对）。
#:   正确修法在**生成侧**（让 H3 别画 / 或资产图明确画上），不在叠加侧。
#:
#: 两个前置条件都不齐备：
#:   ① 资产图：无（目录只有 3 个道具：守则 / 员工卡 / 水瓶）
#:   ② 权威文本：无（原文/剧本/canon 均无「印章」字样，印章上的字无从核对）
SEAL_FEASIBILITY = {
    "verdict": "not_feasible_yet",
    "root_cause": "H3 参考重绘凭空生成的幻觉元素（资产图里没有，原文里也没有）",
    #: ⭐ 2026-10-12 追加：**原文其实已经排除了印章**，不是「没提到」而是「写死了没有」。
    #: 小说原文（进境 20261010_184008_8d5152.txt）:112-120 对这张纸的外观做完整枚举：
    #:   「守则末尾没有落款，没有日期，只有一行手写的、用红色圆珠笔写的字」
    #:   「他把纸翻过来，背面是空白的。」
    #: 即：正面的红色元素**只有那一行手写批注**，背面空白 —— 没有印章的位置。
    #: 这把「印章该不该存在」从一道二选一的设定题，变成了有原文答案的问题：
    #: 按原著，这张纸上**不该有印章**，H3 画上去的那个应当被抑制（生成侧）。
    "novel_evidence": ("小说原文 :112-120 已完整枚举纸面外观：末尾无落款无日期，"
                       "只有一行红色圆珠笔手写批注，背面空白 —— 原文中不存在印章"),
    "recommended_action": "生成侧抑制（让 H3 别画），而非叠加侧补一个",
    "asset_image": None,
    "authoritative_text": None,
    "missing": ["资产图（assets/items/<项目>/印章/base.png）",
                "权威文本（原文/剧本/canon 均无「印章」字样，印章上的字无从核对）"],
    "needed_from": ["生成侧（H3/分镜提示词）抑制印章（原文依据见 novel_evidence）",
                    "资产生成链路（若仍要保留，则出一张印章 base.png 并给出文字）"],
    "note": "叠加侧不可修：补一个正确印章等于替幻觉背书，应走生成侧",
}

#: 用户反馈的「印章」也可能指守则纸上的**红色手写批注**——它有明确原文出处，
#: 但那是手写批注不是印章，且**同样没有资产图**（资产图上只有红圈、没有批注字），
#: 故同样不登记。若后续资产图补上批注，这里的原文即可直接作为权威文本使用。
#: 出处：小说原文（进境 20261010_184008_8d5152.txt）第 112-114 行、第 259-261 行。
CANONICAL_RED_ANNOTATION = (
    "第五条和第三条矛盾。你不知道哪条是真的。祝你好运。|"
    "红色按钮不是用来求救的。"
)

#: ⛔ 明确不做的事：透视变换（perspective / 贴合纸面或卡面的倾斜角度）。
#: ffmpeg 原生 overlay 只支持**轴对齐矩形**，要贴合透视必须上 perspective/rotate 滤镜
#: 或 PIL 逐帧重投影 —— 后者要解码+逐帧变换+重编码，成本高、且容易在边界引入锯齿与
#: 时间抖动。t18 裁决：**本任务不碰**，留待后续单独立项。
#: 现状的取舍：贴片是轴对齐的正面视图，观感上像「浮在前景的告示牌」而非「贴在被摄
#: 物表面」。这是已知的观感上限，不是 bug；若要消除，走上面那条单独立项。
NO_PERSPECTIVE_TRANSFORM_NOTE = (
    "t18 裁决不做透视变换：ffmpeg overlay 仅支持轴对齐矩形，透视需 PIL 逐帧，"
    "成本高风险大，留待后续单独立项"
)

#: 文本来源策略（三套守则文本的取舍，写死在注释里，供评审核对）
#:
#: 实测三套互不相同的守则文本（2026-10-12 进境_整本小说 第1集）：
#:   ① 剧本 shot 21-28 / 44 / 46 / 52 的 surface_text：4 条（234 字）
#:   ② 剧本 shot 45 / 47 的 surface_text：另一套 7 条（136 字）
#:   ③ 资产图 assets/items/进境_整本小说/楼层安全守则/base.png：第三套 **8 条**（与小说原文对齐）
#: 取舍：**以资产图 / 小说原文为准**（即 ③）。理由：
#:   · 用户反馈的是**成片里看到的**乱码，成片画面来自分镜图 ← 资产图，贴资产图才是
#:     「把错的那份换成对的那份」；
#:   · ③ 的每一条都能在小说原文里找到出处（canon_data.json 的 source.quote 逐条对上，
#:     见 resolve_prop_text() 的 verified_lines）；
#:   · ① ② 只在剧本里出现且互相冲突（7 条 vs 4 条），读它等于把冲突搬进成片。
PROP_TEXT_POLICY = "asset_image_first_then_novel_novel_quote"

#: 权威文本台账：项目+道具 → 逐行文本，每行带出处。
#: source 取值：
#:   · "novel"       —— 与 app/canon_data.json 的原文引文逐字一致（可机器核对）
#:   · "asset_image" —— 来自资产图的视觉复核 OCR（canon 未覆盖的条目）
PROP_TEXT_REGISTRY = {
    ("进境_整本小说", "楼层安全守则"): {
        "lines": [
            "1. 本栋楼共四十七层，请勿前往四十八层及以上。",
            "2. 电梯仅停靠偶数层，若电梯停在奇数层，请立即离开。",
            "3. 请勿进入2704号房间。若发现该房间门已开启，请保持镇静，等待工作人员处理。",
            "4. 夜间（23:00-05:00）请勿乘坐楼梯。",
            "5. 本栋楼无2704号房间，若您看到该门牌号，请勿注视，直接移开视线。",
            "6. 不要相信穿着红色工牌的人。",
            "7. 员工证请佩戴于左胸，位置过低或过高均可能导致系统识别异常。",
            "8. 若您遇到任何规则冲突，请以本守则最新版本为准。",
        ],
        "title": "楼层安全守则",
        "note": "8 条 = 资产图逐字复核（视觉 OCR）；其中第 1/3/4 条与 canon_data.json 原文引文逐字一致",
    },
    ("进境_整本小说", "天合科技员工卡"): {
        "lines": ["天合科技 姓名 羡进", "员工证，请勿转借"],
        "title": "天合科技员工卡（正面）",
        "note": "来自资产图正面的视觉复核；资产图是正反面并排（背面文字：员工证请勿转借）",
    },
}


# ===================== ① 时间轴（复用 step_final 既有换算口径） =====================

def shot_time_spans(shots, total_sec) -> list:
    """把各镜剧本时长换算成**成片时间轴**上的 [start, end)。

    口径与 pipeline.step_final 的成片字幕循环**逐字一致**（当前 pipeline.py L1485-1505）：
        dur = float(shot.duration or 5)
        script_total = sum(dur for shots) or 1.0
        if 成片实测时长 > 0: dur = dur / script_total * 成片实测时长
        cur += dur
    为什么必须复用：单条整集源片无法逐段 ffprobe（没有 seg_map.json，也没有镜头级时间轴落盘），
    唯一权威基准就是「成片实测总时长 + 各镜剧本 duration 占比」。两处各写一套必然漂移
    （字幕与叠加错位，画面对不上文字）。

    total_sec <= 0（ffprobe 拿不到时长）→ 退回剧本 duration 绝对值（与 step_final 同语义）。
    返回 [{"shot_id", "index", "start", "end", "duration", "script_duration"}]
    """
    shots = [s for s in (shots or []) if isinstance(s, dict)]
    script_total = sum(float(s.get("duration") or 5) for s in shots) or 1.0
    try:
        total = float(total_sec or 0)
    except (TypeError, ValueError):
        total = 0.0
    spans, cur = [], 0.0
    for idx, s in enumerate(shots):
        sd = float(s.get("duration") or 5)
        dur = (sd / script_total * total) if total > 0 else sd
        spans.append({"shot_id": s.get("shot_id"), "index": idx,
                      "start": round(cur, 3), "end": round(cur + dur, 3),
                      "duration": round(dur, 3), "script_duration": sd})
        cur += dur
    return spans


# ===================== ② 几何：目标区域与「不变形」缩放 =====================

def norm_box(box, video_w, video_h) -> tuple:
    """目标区域 → 像素 (x, y, w, h)。

    接受两种写法：相对比例（全部 <=1，相对成片画幅）或绝对像素（>1）。非法输入返回 ()。
    """
    try:
        x, y, w, h = (float(v) for v in box)
    except (TypeError, ValueError):
        return ()
    if min(w, h) <= 0:
        return ()
    if max(x, y, w, h) <= 1.0:
        x, y, w, h = x * video_w, y * video_h, w * video_w, h * video_h
    W, H = int(video_w), int(video_h)
    x = max(0, min(int(round(x)), W - 1))
    y = max(0, min(int(round(y)), H - 1))
    w = max(2, min(int(round(w)), W - x))
    h = max(2, min(int(round(h)), H - y))
    return (x, y, w, h)


def crop_px(crop_frac, src_w, src_h) -> tuple:
    """素材裁切（相对比例）→ 像素 (w, h, x, y)；空/非法 → () 表示不裁。"""
    if not crop_frac:
        return ()
    try:
        fx, fy, fw, fh = (float(v) for v in crop_frac)
    except (TypeError, ValueError):
        return ()
    if min(fw, fh) <= 0:
        return ()
    W, H = int(src_w), int(src_h)
    if W <= 0 or H <= 0:
        return ()
    w = max(2, min(int(round(fw * W)), W))
    h = max(2, min(int(round(fh * H)), H))
    x = max(0, min(int(round(fx * W)), W - w))
    y = max(0, min(int(round(fy * H)), H - h))
    return (w, h, x, y)


def _even(v: int) -> int:
    """偶数化（x264 要求宽高为偶数，否则编码报错）。"""
    return max(2, int(v) - (int(v) % 2))


def fit_box(src_w, src_h, box) -> tuple:
    """把素材**等比**放进目标框并居中 → (w, h, x, y)，全部偶数化。

    「等比」是关键：资产图 1248x1248 与成片 960x544 宽高比不同，直接拉伸会把守则的字
    压扁/拉长（一眼可见）。这里取 min(缩放比) 做 contain 式内切，绝不改宽高比。
    """
    bx, by, bw, bh = box
    try:
        sw, sh = float(src_w), float(src_h)
    except (TypeError, ValueError):
        return ()
    if sw <= 0 or sh <= 0 or bw <= 0 or bh <= 0:
        return ()
    scale = min(float(bw) / sw, float(bh) / sh)
    w = _even(round(sw * scale))
    h = _even(round(sh * scale))
    x = int(bx) + max(0, (int(bw) - w) // 2)
    y = int(by) + max(0, (int(bh) - h) // 2)
    return (w, h, x, y)


# ===================== ③ 文本来源（资产图 / 小说原文） =====================

def _canon_quotes(project_name: str, canon_path: str = None) -> list:
    """读 app/canon_data.json，取该项目的**原文引文**（source.quote）列表（fail-open）。"""
    p = canon_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "canon_data.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.debug("canon 读取失败（跳过原文核对）：%s", e)
        return []
    want = str(project_name or "").strip()
    out = []
    for b in (data.get("books") or []):
        if not isinstance(b, dict):
            continue
        if want and str(b.get("key") or "").strip() != want:
            continue
        for f_ in (b.get("facts") or []):
            if not isinstance(f_, dict):
                continue
            quote = str((f_.get("source") or {}).get("quote") or "").strip()
            if quote:
                out.append(quote)
    return out


def resolve_item_alias(item_name: str) -> str:
    """道具名 → 资产目录名（显式别名表；未登记的原样返回）。"""
    name = str(item_name or "").strip()
    return PROP_ALIASES.get(name, name)


def resolve_prop_text(project_name: str, item_name: str, canon_path: str = None) -> dict:
    """解析道具的**权威文本**（资产图 / 小说原文），并逐行标注出处与原文核对结果。

    返回 {"item", "asset_name", "title", "lines", "verified_lines", "source", "note", "error"}
      · source="registry+canon"  → 台账逐行 + canon 原文引文核对（哪些行与原文逐字一致）
      · source="registry"        → canon 未覆盖该项目/道具（只有资产图视觉复核）
      · lines=[] / error         → 台账里没有这个道具：**不猜、不叠文字**（fail-open）
    绝不读 shot.surface_text —— 三套文本的差异与取舍见 PROP_TEXT_POLICY / PROP_TEXT_REGISTRY。
    """
    asset_name = resolve_item_alias(item_name)
    entry = PROP_TEXT_REGISTRY.get((str(project_name or "").strip(), asset_name)) or {}
    out = {"item": str(item_name or "").strip(), "asset_name": asset_name,
           "title": entry.get("title") or asset_name, "lines": list(entry.get("lines") or []),
           "verified_lines": [], "source": "", "note": entry.get("note") or "", "error": ""}
    if not out["lines"]:
        out["error"] = "无权威文本台账（不猜内容，跳过文字叠加）"
        out["source"] = "none"
        return out
    quotes = _canon_quotes(project_name, canon_path)
    if quotes:
        joined = " ".join(quotes)
        out["verified_lines"] = [ln for ln in out["lines"] if ln.strip() and ln.strip() in joined]
        out["source"] = "registry+canon"
    else:
        out["source"] = "registry"
    return out


# ===================== ④ 叠加计划 =====================

def default_assets_dir() -> str:
    """资产根目录（config.ASSETS_DIR；导入失败回落 PROJECT_OUTPUT_DIR/assets）。"""
    try:
        from config import ASSETS_DIR
        return ASSETS_DIR
    except Exception:  # noqa: BLE001
        try:
            from config import PROJECT_OUTPUT_DIR
            return os.path.join(PROJECT_OUTPUT_DIR, "assets")
        except Exception:  # noqa: BLE001
            return os.path.join("output", "assets")


def _shot_props(shot) -> list:
    """镜头声明的道具（items_in_shot 兼容 list[str] / list[dict]）。"""
    out = []
    for x in (shot.get("items_in_shot") or []):
        if isinstance(x, dict):
            x = x.get("name") or x.get("item") or ""
        x = str(x or "").strip()
        if x:
            out.append(x)
    return out


def plan_prop_overlays(project_name: str, shots, spans, *, assets_dir: str = "",
                       registry: dict = None, policy: dict = None) -> dict:
    """按「镜头声明的道具 + 素材资产图 + 该镜时间轴」生成叠加计划（纯计算，不碰 ffmpeg）。

    这是本模块的主入口：给定 shots / spans / 资产目录，产出可核验的 items 列表
    （每项含资产绝对路径、目标框、裁切、时间段、文本来源信息）。
    """
    assets_dir = assets_dir or default_assets_dir()
    pol = dict(OVERLAY_POLICY)
    pol.update(policy or {})
    reg = dict(DEFAULT_PROP_OVERLAYS)
    reg.update(registry or {})
    out = {"project": str(project_name or ""), "items": [], "skipped": [],
           "enabled": bool(pol.get("enabled", True)), "error": ""}
    if not out["enabled"]:
        return out
    span_by_idx = {s["index"]: s for s in (spans or [])}
    for idx, shot in enumerate([s for s in (shots or []) if isinstance(s, dict)]):
        span = span_by_idx.get(idx)
        if not span or float(span.get("duration") or 0) < float(pol.get("min_span_sec") or 0):
            continue
        for raw in _shot_props(shot):
            asset_name = resolve_item_alias(raw)
            conf = reg.get(asset_name)
            if not conf:
                continue
            img = os.path.join(assets_dir, "items", str(project_name), asset_name, "base.png")
            if not os.path.isfile(img):
                out["skipped"].append({"shot_id": shot.get("shot_id"), "item": asset_name,
                                       "reason": "资产图不存在", "path": img})
                continue
            txt = resolve_prop_text(project_name, asset_name)
            out["items"].append({
                "shot_id": shot.get("shot_id"), "item": asset_name, "requested": raw,
                "asset": os.path.abspath(img),
                "kind": conf.get("kind") or "image",
                "box": tuple(conf.get("box") or ()),
                "crop_frac": tuple(conf.get("crop_frac") or ()) or None,
                "alpha": float(conf.get("alpha", pol.get("alpha_default", 1.0)) or 1.0),
                "start": span["start"], "end": span["end"],
                "text_source": txt.get("source"), "text_lines": len(txt.get("lines") or []),
                "text_error": txt.get("error") or "",
            })
    if len(out["items"]) > int(pol.get("max_items") or 0):
        dropped = out["items"][int(pol["max_items"]):]
        out["items"] = out["items"][:int(pol["max_items"])]
        for d in dropped:
            out["skipped"].append({"shot_id": d["shot_id"], "item": d["item"],
                                   "reason": "超出单成片叠加上限"})
    return out


# ===================== ⑤ 滤镜构造（复用 video_watermark 的转义/字体） =====================

def build_overlay_chain(items, video_size, *, font_file: str = "") -> dict:
    """把叠加计划编译成 ffmpeg filter_complex。

    返回 {"filter_complex", "vmap", "extra_inputs", "font", "font_cwd", "notes", "error"}
      · 图片只作输入（[1:v]…），**路径不进滤镜串**（Windows 反斜杠/盘符不会出现在滤镜里）；
      · 字体用「cwd 切到字体目录 + 裸文件名」（video_postprocess.py:533-537 同做法）；
      · 时间段用 enable=between(t, a, b) 的转义逗号写法。
    """
    W, H = int(video_size[0]), int(video_size[1])
    chain, extra_inputs, notes = [], [], []
    last = "[0:v]"
    idx_in = 1
    for i, it in enumerate(items or []):
        path = str(it.get("asset") or "")
        if not os.path.isfile(path):
            return {"error": "叠加素材不存在：%s" % (path or "(空)")}
        try:
            from PIL import Image
            with Image.open(path) as im:
                sw, sh = im.width, im.height
        except Exception as e:  # noqa: BLE001
            sw, sh = 0, 0
            notes.append("素材尺寸读取失败（按目标框尺寸处理）：%s: %s" % (type(e).__name__, e))
        box = norm_box(it.get("box") or (), W, H)
        if not box:
            return {"error": "目标区域非法：%r" % (it.get("box"),)}
        crop = crop_px(it.get("crop_frac"), sw or box[2], sh or box[3])
        fitted = fit_box(crop[0] if crop else (sw or box[2]),
                         crop[1] if crop else (sh or box[3]), box)
        if not fitted:
            return {"error": "目标框无法容纳素材：%r" % (it.get("box"),)}
        fw, fh, fx, fy = fitted
        seg = "[%d:v]format=rgba" % idx_in
        if crop:
            seg += ",crop=%d:%d:%d:%d" % crop
        seg += ",scale=%d:%d" % (fw, fh)
        alpha = float(it.get("alpha") or 1.0)
        if alpha < 0.999:
            seg += ",colorchannelmixer=aa=%s" % round(alpha, 3)
        seg += "[ov%d]" % i
        chain.append(seg)
        try:
            st, en = float(it.get("start") or 0), float(it.get("end") or 0)
        except (TypeError, ValueError):
            return {"error": "时间段非法：%r-%r" % (it.get("start"), it.get("end"))}
        if en <= st:
            return {"error": "时间段为空：%s-%s" % (st, en)}
        nxt = "[v%d]" % i
        chain.append("%s[ov%d]overlay=x=%d:y=%d:format=auto:shortest=0:"
                     "enable=between(t\\,%s\\,%s)%s"
                     % (last, i, fx, fy, round(st, 3), round(en, 3), nxt))
        last = nxt
        extra_inputs.append(path)
        idx_in += 1
        notes.append("shot=%s %s → %dx%d@(%d,%d) %s-%ss" % (
            it.get("shot_id"), os.path.basename(path), fw, fh, fx, fy,
            round(st, 3), round(en, 3)))
    if not chain:
        return {"error": "没有可叠加的条目"}
    font = _wm.resolve_font(font_file)
    font_cwd = os.path.dirname(os.path.abspath(font)) if font else ""
    return {"filter_complex": ";".join(chain), "vmap": last, "extra_inputs": extra_inputs,
            "font": font, "font_cwd": font_cwd, "notes": notes, "error": ""}


def build_text_overlay_vf(lines, *, start, end, pos="top-left", font_size=28,
                          font_color="white", font_file: str = "", border_w: int = 0) -> str:
    """构造 drawtext 滤镜串（多行文本用 ffmpeg 约定的字面量反斜杠+n 连接）。

    文本先逐行过 video_watermark._esc_text（冒号/百分号/引号等），再用字面量反斜杠+n
    连接成 drawtext 的多行写法；字体同样只写**裸文件名**（调用方 cwd 已切到字体目录）。
    """
    font = _wm.resolve_font(font_file)
    if not font:
        return ""
    text = "\\n".join(_wm._esc_text(str(ln)) for ln in (lines or []) if str(ln).strip())
    if not text:
        return ""
    x, y = _wm._pos_expr(pos, 18)
    return ("drawtext=fontfile='%s':text='%s':fontsize=%d:fontcolor=%s:"
            "borderw=%d:bordercolor=black@0.6:x=%s:y=%s:enable=between(t\\,%s\\,%s)"
            % (os.path.basename(font), text, int(font_size), font_color, int(border_w),
               x, y, round(float(start), 3), round(float(end), 3)))


# ===================== ⑥ ffmpeg 执行（命令模板复用 video_watermark） =====================

def ffmpeg_available() -> bool:
    return _wm.ffmpeg_available()


def probe_audio_streams(path, timeout: int = 60) -> dict:
    """ffprobe 读音频流（验收要求：用 ffprobe 证明输出仍带 aac 音轨）。"""
    out = {"audio": [], "has_audio": False, "error": ""}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=codec_name,sample_rate,channels",
                            "-of", "json", os.path.abspath(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        if r.returncode != 0:
            out["error"] = (r.stderr or "").strip()[:300] or "ffprobe 返回码 %s" % r.returncode
            return out
        data = json.loads(r.stdout or "{}")
        out["audio"] = [{"codec": s.get("codec_name"), "sample_rate": s.get("sample_rate"),
                         "channels": s.get("channels")} for s in (data.get("streams") or [])]
        out["has_audio"] = bool(out["audio"])
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _probe_video_size(path) -> tuple:
    """ffprobe 取视频分辨率（拿不到 → ()）。"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "json",
                            os.path.abspath(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        if r.returncode != 0:
            return ()
        s = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return (int(s.get("width") or 0), int(s.get("height") or 0))
    except Exception:  # noqa: BLE001
        return ()


def apply_overlays(src: str, dst: str, plan: dict, *, video_size=None, font_file: str = "",
                   timeout: int = 1800, clip=None, policy: dict = None) -> dict:
    """把 plan 里的叠加烧进 src → dst（可调用接口：视频 + 道具资产图 + 目标区域 + 时间段）。

    返回 {ok, out_path, cmd, elapsed, error, items, filter_complex, audio}
      · 命令模板与 video_watermark.apply_watermark 同口径：libx264 -preset veryfast -crf 20、
        -map 0:v:0、-map 0:a?、-c:a copy、-movflags +faststart；
      · clip=(ss, t)：只看几秒样张用 —— 输入 -ss 裁剪，并把叠加时间段整体左移 ss。
    """
    pol = dict(OVERLAY_POLICY)
    pol.update(policy or {})
    result = {"ok": False, "out_path": "", "cmd": "", "elapsed": 0.0, "error": "",
              "items": len((plan or {}).get("items") or []), "filter_complex": "", "audio": {}}
    if not result["items"]:
        result["error"] = "叠加计划为空"
        return result
    if not src or not os.path.isfile(src):
        result["error"] = "源视频不存在：%s" % src
        return result
    if not ffmpeg_available():
        result["error"] = "未找到 ffmpeg（请安装并加入 PATH）"
        return result

    items = [dict(it) for it in plan["items"]]
    ss, limit = 0.0, 0.0
    if clip:
        try:
            ss = max(0.0, float(clip[0] or 0))
            limit = max(0.0, float(clip[1] or 0))
        except (TypeError, ValueError):
            ss, limit = 0.0, 0.0
        if ss > 0:
            for it in items:
                it["start"] = round(float(it.get("start") or 0) - ss, 3)
                it["end"] = round(float(it.get("end") or 0) - ss, 3)
            items = [it for it in items if float(it.get("end") or 0) > 0]
            for it in items:
                if float(it.get("start") or 0) < 0:
                    it["start"] = 0.0
        if not items:
            result["error"] = "裁剪后没有落在时间窗内的叠加条目"
            return result

    size = tuple(video_size or ())
    if len(size) != 2:
        size = _probe_video_size(src) or (960, 544)
    flt = build_overlay_chain(items, size, font_file=font_file)
    if flt.get("error"):
        result["error"] = flt["error"]
        return result
    result["filter_complex"] = flt["filter_complex"]
    result["items"] = len(items)

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    cmd = [_wm.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    if ss > 0:
        cmd += ["-ss", "%s" % round(ss, 3)]
    cmd += ["-i", os.path.abspath(src)]
    for extra in flt["extra_inputs"]:
        cmd += ["-i", os.path.abspath(extra)]
    if limit > 0:
        cmd += ["-t", "%s" % round(limit, 3)]
    cmd += ["-filter_complex", flt["filter_complex"], "-map", flt["vmap"],
            "-map", "0:a?", "-c:v", "libx264", "-preset", pol["preset"],
            "-crf", str(pol["crf"]), "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", os.path.abspath(dst)]
    result["cmd"] = " ".join(cmd)

    # Windows 路径安全：cwd 切到字体目录，字体在滤镜里只写裸文件名
    # （video_postprocess.py:533-537 同做法）；输入/输出都是绝对路径 + 列表参数直传，
    # 不经过 shell，因此不会出现「反斜杠被吞」。
    cwd = flt.get("font_cwd") or None
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=cwd)
        result["elapsed"] = round(time.time() - t0, 2)
        if p.returncode != 0 or not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            result["error"] = (p.stderr or "").strip()[:800] or "ffmpeg 返回码 %s" % p.returncode
            return result
    except Exception as e:  # noqa: BLE001
        result["elapsed"] = round(time.time() - t0, 2)
        result["error"] = "叠加烧写异常：%s: %s" % (type(e).__name__, e)
        return result

    result.update({"ok": True, "out_path": os.path.abspath(dst),
                   "audio": probe_audio_streams(dst)})
    return result


def apply_text_overlays(src: str, dst: str, items, *, font_file: str = "",
                        timeout: int = 1800, policy: dict = None) -> dict:
    """纯 drawtext 叠加路径（-vf + **-map 0:v:0**）。

    与 apply_overlays 的关系：
      · 图片贴片必须走 filter_complex（第二路输入 + overlay 滤镜），视频流由滤镜输出映射；
      · 纯文字叠加可以走 -vf，此时「视频=输入 0:v:0 经 drawtext」→ 命令里就是
        字面量 -map 0:v:0（与 video_watermark 的文字路径完全同口径）。
    两条路径的音频处理完全一致：-map 0:a? + -c:a copy。
    """
    pol = dict(OVERLAY_POLICY)
    pol.update(policy or {})
    result = {"ok": False, "out_path": "", "cmd": "", "elapsed": 0.0, "error": "",
              "items": len(items or []), "vf": "", "audio": {}}
    if not items:
        result["error"] = "没有可叠加的文字条目"
        return result
    if not src or not os.path.isfile(src):
        result["error"] = "源视频不存在：%s" % src
        return result
    if not ffmpeg_available():
        result["error"] = "未找到 ffmpeg（请安装并加入 PATH）"
        return result
    font = _wm.resolve_font(font_file)
    if not font:
        result["error"] = "未找到可用中文字体（drawtext 需要字体）"
        return result
    vfs = []
    for it in items:
        vf = build_text_overlay_vf(it.get("lines") or [], start=it.get("start") or 0,
                                   end=it.get("end") or 0, pos=it.get("pos") or "top-left",
                                   font_size=it.get("font_size") or 28,
                                   font_color=it.get("font_color") or "white",
                                   font_file=font_file, border_w=it.get("border_w") or 0)
        if not vf:
            result["error"] = "drawtext 构造失败（时间段或文本为空）"
            return result
        vfs.append(vf)
    result["vf"] = ",".join(vfs)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    cmd = [_wm.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", os.path.abspath(src), "-vf", result["vf"], "-map", "0:v:0",
           "-map", "0:a?", "-c:v", "libx264", "-preset", pol["preset"],
           "-crf", str(pol["crf"]), "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", os.path.abspath(dst)]
    result["cmd"] = " ".join(cmd)
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           cwd=os.path.dirname(os.path.abspath(font)))
        result["elapsed"] = round(time.time() - t0, 2)
        if p.returncode != 0 or not (os.path.isfile(dst) and os.path.getsize(dst) > 0):
            result["error"] = (p.stderr or "").strip()[:800] or "ffmpeg 返回码 %s" % p.returncode
            return result
    except Exception as e:  # noqa: BLE001
        result["elapsed"] = round(time.time() - t0, 2)
        result["error"] = "文字叠加烧写异常：%s: %s" % (type(e).__name__, e)
        return result
    result.update({"ok": True, "out_path": os.path.abspath(dst),
                   "audio": probe_audio_streams(dst)})
    return result


def overlay_sample_frame(video: str, out_png: str, at_sec: float, timeout: int = 120) -> dict:
    """抽一帧存 PNG（样张核验用；不改变任何产物）。"""
    out = {"ok": False, "path": "", "error": ""}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
        r = subprocess.run([_wm.ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", "%s" % round(float(at_sec), 3), "-i", os.path.abspath(video),
                            "-frames:v", "1", os.path.abspath(out_png)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        if r.returncode != 0 or not os.path.isfile(out_png):
            out["error"] = (r.stderr or "").strip()[:400] or "ffmpeg 返回码 %s" % r.returncode
            return out
        out.update({"ok": True, "path": os.path.abspath(out_png)})
    except Exception as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def overlay_report(plan: dict) -> str:
    """把叠加计划渲染成人可读的报告（日志 / 排查 / 评审用）。"""
    lines = ["叠加条目 %d 条" % len((plan or {}).get("items") or [])]
    for it in (plan or {}).get("items") or []:
        lines.append("  shot#%-3s %-14s %s box=%s %s-%ss 文本来源=%s(%d 行)" % (
            it.get("shot_id"), it.get("item"), os.path.basename(str(it.get("asset"))),
            tuple(round(float(v), 3) for v in (it.get("box") or ())),
            it.get("start"), it.get("end"), it.get("text_source"), it.get("text_lines")))
    for sk in (plan or {}).get("skipped") or []:
        lines.append("  跳过 shot#%s %s：%s" % (sk.get("shot_id"), sk.get("item"), sk.get("reason")))
    return chr(10).join(lines)
