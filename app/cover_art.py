# -*- coding: utf-8 -*-
"""项目封面「爆款排版」加工：AI 出图 → 大字剧名 + 卖点短句叠加。

背景（2026-10-18 产品/爆款视角核查）：旧实现把 AI 出的图直接当封面 —— 没有标题
排版。平台上能打的短剧封面需要「人脸 + 冲突感 + 大字标题」（标题 3~10 字），在
缩略图尺寸下也要一眼读出剧名。构图留白（上方较暗干净区域）由提示词侧负责
（routes/projects._cover_prompt_from_outline），本模块只做**确定性排版**：

  choose_cover_texts()  选字：bible 剧名 > 剧本标题（第1集）> 项目名；
                        卖点短句取 drama_plan.creative_proposition.one_sentence_value
                        （4~18 字才用，太长/太短都不上封面）
  split_title()         分行（纯逻辑，可单测）：优先在「· ｜ ： 」等分隔点断开
  layout_title()        字号（纯逻辑，可单测）：最宽行收进 86% 画宽、总高 ≤ 38%
  burn_title()          PIL 渲染：白字黑描边 + 半透明投影，顶部居中；
                        **任何失败一律回退原图**（封面生成不因排版失败而失败）

中文字体经 video_watermark.resolve_font()（真机走微软雅黑等系统字体）。沙箱无
中文字体时会返回 False 并保留原图 —— 故纯逻辑层（分行/字号/选字）单独可测，
渲染路径用测试内自带的拉丁字体覆盖。
"""
import json
import logging
import os
import re
import shutil

logger = logging.getLogger(__name__)

#: 标题里可用的分隔符（原型：爆款剧名常见「A·B」结构，如「逆天系统·废物觉醒」）
_TITLE_SEPS = ("·", "｜", "|", "：", ":", "—", "～", "・")
#: 卖点短句最长字数（超过就不上封面，避免糊成一团）
_TAGLINE_MAX = 18
#: 标题最长字数（超出截断；剧名一般 3~10 字）
_TITLE_MAX = 14
#: 标题最多两行（三行在 16:9 缩略图里读不清）
_TITLE_MAX_LINES = 2


def _clean(s) -> str:
    s = str(s or "").strip()
    s = s.strip("《》「」\"'“”‘’ ")
    return re.sub(r"\s+", " ", s)


def choose_cover_texts(project_name, bible_title="", script_title="", plan=None):
    """选字（纯逻辑）：返回 ``(title, tagline)``。

    标题优先级：bible 剧名 > 剧本（第1集）标题 > 项目名。
    卖点：drama_plan.creative_proposition.one_sentence_value，仅 4~18 字时采用。
    """
    title = _clean(bible_title) or _clean(script_title) or _clean(project_name)
    title = title[:_TITLE_MAX]
    tagline = ""
    cp = ((plan or {}).get("creative_proposition") or {})
    if isinstance(cp, dict):
        v = _clean(cp.get("one_sentence_value") or "")
        if 4 <= len(v) <= _TAGLINE_MAX:
            tagline = v
    return title, tagline


def split_title(title, max_lines: int = _TITLE_MAX_LINES):
    """标题分行（纯逻辑）。

    规则：≤7 字单行；否则优先在靠近中线的分隔点断开（「逆天系统·废物觉醒」→
    「逆天系统」/「废物觉醒」）；没有合适分隔点就按中线均分。
    """
    t = _clean(title)
    if not t:
        return []
    if max_lines <= 1 or len(t) <= 7:
        return [t]
    for sep in _TITLE_SEPS:
        i = t.find(sep)
        if 0 < i < len(t) - 1 and abs(i + 0.5 - len(t) / 2.0) <= max(2.0, len(t) * 0.35):
            left, right = t[:i].strip(), t[i + 1:].strip()
            if left and right:
                return [left, right]
    mid = (len(t) + 1) // 2
    return [t[:mid], t[mid:]]


def layout_title(title, img_w: int, img_h: int, max_lines: int = _TITLE_MAX_LINES):
    """字号与版面（纯逻辑）：返回 ``{lines, font_size, line_gap, top, max_width}``。

    约束：最宽行 ≤ 86% 画宽（按中文字形≈1em 估算，含描边留 6% 余量）；
    标题块总高 ≤ 38% 画高；字号上限 15% 画高，下限 18px。
    """
    lines = split_title(title, max_lines=max_lines)
    if not lines:
        return None
    n = len(lines)
    longest = max(len(ln) for ln in lines)
    by_w = int(img_w * 0.86 / max(1, longest) / 1.06)
    by_h = int(img_h * 0.38 / (n * 1.35))
    fs = max(18, min(int(img_h * 0.15), by_w, by_h))
    gap = max(6, int(fs * 0.28))
    return {
        "lines": lines,
        "font_size": fs,
        "line_gap": gap,
        "top": max(6, int(img_h * 0.055)),
        "max_width": int(img_w * 0.92),
    }


def burn_title(src: str, dst: str, title: str, tagline: str = "", *,
               font_file: str = "") -> bool:
    """在 ``src`` 上叠加大字标题+卖点，写出 ``dst``。

    返回 True=排版成功；False=未排版（无字体/无标题/任何异常），此时 ``dst``
    是 ``src`` 的原样拷贝 —— 封面生成链路因此永远不会因排版失败而失败。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
        import video_watermark as _wm

        title = _clean(title)
        font_path = _wm.resolve_font(font_file)
        if not font_path or not title:
            shutil.copyfile(src, dst)
            return False

        img = Image.open(src).convert("RGBA")
        W, H = img.size
        lay = layout_title(title, W, H)
        if not lay:
            shutil.copyfile(src, dst)
            return False
        fs = lay["font_size"]
        font = ImageFont.truetype(font_path, fs, index=0)
        sw = max(2, fs // 10)
        sh_off = max(3, int(fs * 0.045))

        # ⚠️ 顺序：投影层必须先 alpha_composite 到底图，再画**不透明**白字/金句。
        #    反过来（先画字后叠投影层）半透明黑影会盖住字身，标题发灰发脏（2026-10-18 预览实测）。
        y = lay["top"]
        step = fs + lay["line_gap"]

        shadow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow_layer)
        _yy = y
        for line in lay["lines"]:
            sd.text((W // 2, _yy + sh_off), line, font=font,
                    fill=(0, 0, 0, 170), anchor="mt",
                    stroke_width=sw, stroke_fill=(0, 0, 0, 170))
            _yy += step
        if tagline:
            tfs = max(14, int(fs * 0.45))
            tfont = ImageFont.truetype(font_path, tfs, index=0)
            tsw = max(2, tfs // 10)
            sd.text((W // 2, _yy + max(2, tsw)), _clean(tagline), font=tfont,
                    fill=(0, 0, 0, 170), anchor="mt",
                    stroke_width=tsw, stroke_fill=(0, 0, 0, 170))
        img = Image.alpha_composite(img, shadow_layer)

        draw = ImageDraw.Draw(img)
        for line in lay["lines"]:
            draw.text((W // 2, y), line, font=font,
                      fill=(255, 255, 255, 255), anchor="mt",
                      stroke_width=sw, stroke_fill=(0, 0, 0, 235))
            y += step

        if tagline:
            tfs = max(14, int(fs * 0.45))
            tfont = ImageFont.truetype(font_path, tfs, index=0)
            tsw = max(2, tfs // 10)
            draw.text((W // 2, y), _clean(tagline), font=tfont,
                      fill=(255, 211, 77, 255), anchor="mt",   # 金色，与黑底高对比
                      stroke_width=tsw, stroke_fill=(0, 0, 0, 220))

        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        img.convert("RGB").save(dst)
        logger.info("封面排版完成：%s（标题「%s」%s）",
                    dst, " / ".join(lay["lines"]), "，含卖点" if tagline else "")
        return True
    except Exception as e:  # noqa: BLE001  排版失败绝不阻断封面生成
        logger.warning("封面排版失败（保留原图）：%s", e)
        try:
            shutil.copyfile(src, dst)
        except Exception:  # noqa: BLE001
            logger.warning("封面原图回退拷贝也失败（%s → %s）", src, dst)
        return False


def load_cover_texts(dir_key: str, project_name: str, continuity_dir: str,
                     scripts_dir: str):
    """读盘选字：bible 剧名 / 第1集标题 / drama_plan 卖点；任何一项缺失都容忍。"""
    bible_title = ""
    plan = {}
    try:
        import continuity_store
        try:
            bible_title = str((continuity_store.load_bible(continuity_dir, dir_key) or {})
                              .get("title") or "")
        except Exception as e:  # noqa: BLE001
            logger.debug("读 bible 失败（封面标题回落）：%s", e)
        try:
            plan = continuity_store.load_drama_plan(continuity_dir, dir_key) or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("读 drama_plan 失败（封面卖点跳过）：%s", e)
    except Exception as e:  # noqa: BLE001
        logger.debug("continuity_store 不可用（封面按项目名）：%s", e)

    script_title = ""
    try:
        p = os.path.join(scripts_dir, dir_key, "第1集.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                script_title = str((json.load(f) or {}).get("title") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("读第1集标题失败（封面标题回落）：%s", e)

    return choose_cover_texts(project_name, bible_title, script_title, plan)
