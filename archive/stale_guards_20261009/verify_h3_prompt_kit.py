"""回归：H3 提示词构建（h3_prompt_kit）+ 提示词输出链路修复

覆盖「第一遍出的提示词基本都用不了」的两大根因：
  A. 剧本阶段让 LLM 写 `prompt_h3`（一句无参考图标签的裸英文），生成期
     `shot.get('prompt_h3') or _build_h3_prompt(...)` 又让它把结构化构建器顶掉
     → 全项目 200+ 镜头结构化提示词数量为 0
  B. `_build_h3_prompt` 自身缺段（只 2 段）、时间码写成中文、资产参考提示词
     风格重复两遍、英文提示词出现 'xuanxuan' 这类硬音译

覆盖点：
 1. fmt_ts 时间码格式（MM:SS.mmm）
 2. lang_tag 语言判定
 3. dialogue_lines / speaker_slots 归一与 S1/S2 分配
 4. validate 六段/三段/不合规判定
 5. build_ref2va 六段齐全 + 顺序 + 时间码 + <Picture N>/<Subject N> + 台词标注
 6. build_base 三段
 7. 时间轴节拍与总时长一致
 8. merge_detail 把旧薄描述并入 detailed_description
 9. resolve 择优：合规沿用 / 不合规重建
10. comfyui_client.resolve_h3_prompt / _h3_picture_defs（不连 ComfyUI）
11. style_kit.with_style 幂等（修风格重复两遍）
12. style_kit.sanitize_prompt_en / with_style_en / apply_asset_style_all
13. novel_to_script._keep_valid_h3 丢弃薄英文、保留结构化
14. 源码级断言：app.py / comfyui_client / novel_to_script / script_prompt_analyzer

跑法：MJSCXT_AUTOPILOT=0 C:/Python314/python.exe .workbuddy/test/verify_h3_prompt_kit.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
APP_DIR = os.path.join(ROOT, "app")
sys.path.insert(0, APP_DIR)

PASS = FAIL = 0
FAILED = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL {name}  {extra}")


def section(title):
    print(f"\n== {title} ==")


def src(name):
    with open(os.path.join(APP_DIR, name), encoding="utf-8") as f:
        return f.read()


import h3_prompt_kit as kit
import style_kit

SHOT = {
    "shot_id": 3,
    "duration": 8.5,
    "camera": "全景升降",
    "location": "家主阁前广场",
    "description": "朝阳升起，霞光铺满广场，方源立于人群之前，银白长发被晨风吹动",
    "narration": "山雾不是很浓，被利剑般的阳光轻易洞穿。",
    "emotion": "庄重肃穆",
    "audio_cues": "风声，人群低语",
    "dialogue": [{"speaker": "学堂家老", "text": "今天是开窍大典。"}],
    "characters_in_shot": ["方源", "学堂家老"],
    "style": "中国古风玄幻漫剧",
}
PICS = [
    ("<Picture 1>", "该镜头的分镜图，定义构图、景别、机位与人物姿态"),
    ("<Picture 2>", "方源的外观参考，定义五官、发型、服装与画风"),
]
SUBS = [{"name": "方源", "appearance": "清瘦少年，黑瞳锐利，灰粗布常服束发"}]
LEGACY_EN = ("Aerial shot rising through thin mist onto a five-story pagoda courtyard, "
             "golden sunrise rays, symmetrical composition")

# ============ 1. 时间码 ============
section("1. fmt_ts 时间码（MM:SS.mmm）")
check("0 → 00:00.000", kit.fmt_ts(0) == "00:00.000", kit.fmt_ts(0))
check("5 → 00:05.000", kit.fmt_ts(5) == "00:05.000", kit.fmt_ts(5))
check("65.4 → 01:05.400", kit.fmt_ts(65.4) == "01:05.400", kit.fmt_ts(65.4))
check("125.999 → 02:05.999", kit.fmt_ts(125.999) == "02:05.999", kit.fmt_ts(125.999))
check("负数归零", kit.fmt_ts(-3) == "00:00.000", kit.fmt_ts(-3))
check("None/非法值归零", kit.fmt_ts(None) == "00:00.000" and kit.fmt_ts("x") == "00:00.000")
check("旧式中文时间码已消失", "镜头3" not in kit.build_ref2va(SHOT, PICS, SUBS, style=SHOT["style"]))

# ============ 2. 语言判定 ============
section("2. lang_tag 语言判定")
check("中文 → Chinese", kit.lang_tag("今天是开窍大典") == "Chinese")
check("英文 → English", kit.lang_tag("Today is the ceremony") == "English")
check("空 → 空串", kit.lang_tag("") == "")
check("中英混排偏中文", kit.lang_tag("方源 looks at 天空 carefully 001") == "Chinese")

# ============ 3. 台词归一 ============
section("3. dialogue_lines / speaker_slots")
check("str 台词", kit.dialogue_lines("你好") == [{"speaker": "", "text": "你好"}])
check("dict 台词", kit.dialogue_lines({"speaker": "方源", "text": "走"})
      == [{"speaker": "方源", "text": "走"}])
check("list 台词", len(kit.dialogue_lines([{"speaker": "A", "text": "1"},
                                           {"speaker": "B", "text": "2"}])) == 2)
check("None → 空列表", kit.dialogue_lines(None) == [])
check("嵌套列表展平", len(kit.dialogue_lines([[{"speaker": "A", "text": "1"}],
                                              [{"speaker": "B", "text": "2"}]])) == 2)
slots = kit.speaker_slots([{"speaker": "方源"}, {"speaker": "古月赤练"}, {"speaker": "方源"}])
check("S 槽位按首次出场分配且同名复用", slots == {"方源": "S1", "古月赤练": "S2"}, slots)

# ============ 4. validate ============
section("4. validate 结构校验")
check("空串不合规", kit.validate("")["valid"] is False)
check("裸英文句不合规", kit.validate(LEGACY_EN)["valid"] is False)
check("裸英文按 base 报缺 3 段",
      kit.validate(LEGACY_EN)["missing"] == list(kit.BASE_SECTIONS),
      kit.validate(LEGACY_EN)["missing"])
ref_text = kit.build_ref2va(SHOT, PICS, SUBS, style=SHOT["style"])
verdict = kit.validate(ref_text)
check("六段式合规", verdict["valid"] is True and verdict["mode"] == "ref", verdict)
check("六段全部 found", set(verdict["found"]) == set(kit.REF_SECTIONS))
base_text = kit.build_base(SHOT, "T2VA", style=SHOT["style"])
check("三段式合规（base）",
      kit.validate(base_text)["valid"] is True and kit.validate(base_text)["mode"] == "base")
partial = ref_text.replace("non_diegetic_music:", "音乐：")
check("缺一段即不合规", kit.validate(partial)["valid"] is False)
check("能定位缺失段名", "non_diegetic_music" in kit.validate(partial)["missing"])

# ============ 5. Ref2VA 内容 ============
section("5. build_ref2va 内容与格式")
for sec in kit.REF_SECTIONS:
    check(f"含段落 {sec}", f"{sec}:" in ref_text)
order = [ref_text.index(f"{s}:") for s in kit.REF_SECTIONS]
check("六段顺序与规范一致", order == sorted(order), order)
check("含 <Picture 1>/<Picture 2> 标签", "<Picture 1>" in ref_text and "<Picture 2>" in ref_text)
check("含 <Subject 1> 主体定义", "<Subject 1>" in ref_text)
check("首镜以 [Shot 1] 开头",
      any(ln.startswith("[Shot 1]") for ln in
          ref_text.split("detailed_description:")[1].split("\n")))
# 2026-09-24 二次对齐：本地模板的 detailed_description **先写一句全片风格**
#（「The target video uses a … style …」），再进入 [Shot 1]。故这里断言「风格句在
# 首镜之前」，而不是「段首就是 [Shot 1]」。
check("detailed_description 以风格句起头（模板同格式）",
      ref_text.split("detailed_description:")[1].lstrip("\n").startswith("The target video uses"),
      ref_text.split("detailed_description:")[1][:160])
# 2026-09-24：对齐本地手跑模板（H3信号10段测试001.json）后，时间码不再是
# 「[Shot N] 00:03.500」形式的首行前缀，而是**内嵌在后续镜的句子**里
#（「At 00:03.500, the camera cuts to ...」）。断言改为校验 At 时间码存在且格式严格。
check("时间码严格 MM:SS.mmm（At 形式）",
      bool(re.search(r"At \d{2}:\d{2}\.\d{3}, the camera cuts to", ref_text)),
      ref_text.split("detailed_description:")[1][:300])
check("时间码用 [Shot N] 而非中文「镜头N」", "镜头3," not in ref_text and "[Shot 1]" in ref_text)
check("台词带 (S1) 说话人槽位", "(S1)" in ref_text, ref_text[:200])
# 2026-09-26 P0-1「口型与画面分离」：I2V 提示词**不再写台词原文**（配音走独立
# QwenTTS，H3 自带人声还会被 HDEMUCS 剔除）。断言改为：不出现台词原文与 <d> 标签，
# 但保留「开口说话 + 口型自然开合」的动作句（这才是驱动嘴型的信号）。
check("P0-1 台词原文不再写入提示词", "今天是开窍大典" not in ref_text, ref_text[:200])
check("P0-1 不再出现 <d> 台词标签", "<d>" not in ref_text, ref_text[:200])
check("P0-1 保留开口说话动作句", "speaks" in ref_text and "lips moving naturally" in ref_text,
      ref_text.split("detailed_description:")[1][:400])
check("retention_analysis 锁定风格", "中国古风玄幻漫剧" in ref_text)
# 2026-09-24：本地模板不含「严禁出现任何文字」这类否定指令（该措辞会把
# 「字幕/文字」两个词引入提示词，反而诱发字幕）。改成断言「不再出现禁字句」
# + 「无台词镜显式标 no one speaks / no voice-over」（2026-09-27 由旧「No dialogue.」升级）。
check("不再出现「严禁出现任何文字」禁字句", "严禁出现任何文字" not in ref_text)
check("无台词节拍显式标注 no one speaks / no voice-over",
      "no voice-over" in ref_text.lower() or ref_text.count("[Shot 1]") == 1)
# 2026-09-26 P0-1：<d>台词</d> 已从提示词移除（见上方 P0-1 断言），此处不再断言 <d>。
check("P0-1 有台词镜也以 no-voice-over 兜底标注（无台词原文可写）",
      "no voice-over" not in ref_text.split("detailed_description:")[1].lower()
      or "speaks" in ref_text)
check("soundscape 独立成段且非空",
      kit.build_soundscape(SHOT).strip() != ""
      and ("ambient" in kit.build_soundscape(SHOT) or "环境" in kit.build_soundscape(SHOT)))
# 2026-09-24 二次对齐：默认跟随模板写 N/A（模板 10 段里 9 段是 N/A），
# 故「非空」是唯一要求 —— 段名必须存在（validate 靠段名判合规）。
check("music 独立成段且非空", kit.build_music(SHOT).strip() != "")
check("无台词镜头不给 (S1)", "(S1)" not in kit.build_ref2va(
    dict(SHOT, dialogue=[]), PICS, SUBS, style="国漫"))

# ============ 6. 时间轴 ============
section("6. 时间轴节拍与总时长一致")
# 2026-09-24：对齐本地模板后，首镜是 [Shot 1]，后续镜是「At 时间码, ...」，
# 两者都带字面量「[Shot 」，故节拍计数口径不变（首镜恒有 [Shot 1]）。
#
# ⚠️ 二次对齐后**不能再用 ``text.count("[Shot ")``**：retention_analysis 段新增了
# ``(appears in [Shot 1], [Shot 2])`` 归属声明，会被一并计入，导致节拍数虚高、
# 断言假红。统一改成「只数节拍行」（行首 ``[Shot N]`` / ``At ``）。
def _beat_count(text):
    return len([ln for ln in text.split("\n")
                if ln.lstrip().startswith("[Shot ") or ln.lstrip().startswith("At ")])


short = kit.build_ref2va(dict(SHOT, duration=4), PICS, SUBS, style="国漫")
check("4 秒 → 单节拍", _beat_count(short) == 1, _beat_count(short))
mid = kit.build_ref2va(dict(SHOT, duration=8.5), PICS, SUBS, style="国漫")
check("8.5 秒 → 2 节拍", _beat_count(mid) == 2, _beat_count(mid))
long = kit.build_ref2va(dict(SHOT, duration=20), PICS, SUBS, style="国漫")
check("20 秒 → 3 节拍", _beat_count(long) == 3, _beat_count(long))
check("末节拍时间码 < 总时长", "00:01" in long or "00:06" in long)

# ============ 7. merge_detail ============
section("7. merge_detail 把旧薄描述并入详细描述")
merged = kit.merge_detail(ref_text, LEGACY_EN)
check("结构仍是六段", kit.validate(merged)["valid"] is True)
check("旧描述被保留", "Aerial shot rising" in merged)
check("旧描述落在 detailed_description 段内",
      merged.index("detailed_description") < merged.index("Aerial shot rising")
      < merged.index("overall_soundscape"))

# ============ 8. resolve 择优 ============
section("8. resolve 择优（核心根因修复）")
no_exist = kit.resolve(SHOT, PICS, SUBS, style=SHOT["style"])
check("无既有提示词 → 规范六段", kit.validate(no_exist)["valid"] is True)
check("无参考图 → base 三段",
      kit.validate(kit.resolve(SHOT, [], [], style="国漫"))["mode"] == "base")
good = kit.build_ref2va(SHOT, PICS, SUBS, style=SHOT["style"])
check("既有合规提示词被原样沿用",
      kit.resolve(dict(SHOT, prompt_h3=good), PICS, SUBS, style=SHOT["style"]) == good)
legacy_res = kit.resolve(dict(SHOT, prompt_h3=LEGACY_EN), PICS, SUBS, style=SHOT["style"])
check("既有裸英文 → 重建为六段", kit.validate(legacy_res)["valid"] is True)
check("既有裸英文不被丢弃（并入细节）", "Aerial shot rising" in legacy_res)
check("裸英文不再整体充当提示词", not legacy_res.strip().startswith("Aerial"))

# ============ 9. comfyui_client ============
section("9. comfyui_client 参考图映射与择优入口（不连 ComfyUI）")
import comfyui_client as cc

client = cc.ComfyUIClient.__new__(cc.ComfyUIClient)
pics, subs, _sb0 = cc.ComfyUIClient._h3_picture_defs(
    [{"name": "方源", "appearance": "清瘦少年"}], [{"name": "广场"}], None)
check("无分镜图：角色 → <Picture 1>", pics[0][0] == "<Picture 1>")
check("无分镜图：场景排在其后", pics[-1][1].startswith("广场"), pics)
check("主体定义带外观", subs and subs[0]["appearance"] == "清瘦少年", subs)
pics_sb, subs_sb, _sb1 = cc.ComfyUIClient._h3_picture_defs(
    [{"name": "方源", "appearance": "清瘦少年"}], [], {"name": "shot_03"})
check("有分镜图：<Picture 1> 为分镜图", "分镜图" in pics_sb[0][1])
# 2026-09-27 参考图策略改为「分镜图 + 本镜角色三视图独立锚点」：分镜图只承载
# 构图/机位/姿态，角色外观由各自的三视图锚点（<Picture 2> 起）独立锁定。
# 断言：分镜图 1 张 + 每个角色 1 张三视图；主角不再归属 <Picture 1> 而是自己的三视图。
check("有分镜图：分镜图 + 角色三视图独立锚点（2 张：分镜 + 方源三视图）",
      len(pics_sb) == 2, pics_sb)
check("有分镜图：主角作为 Subject 归属自己的三视图 <Picture 2>",
      any(s.get("name") == "方源" and s.get("picture") == "<Picture 2>" for s in subs_sb),
      subs_sb)

resolved = client.resolve_h3_prompt(
    dict(SHOT, prompt_h3=LEGACY_EN),
    [{"name": "方源", "appearance": "清瘦少年"}], [{"name": "广场"}], None)
check("resolve_h3_prompt 产出合规六段", kit.validate(resolved)["valid"] is True)
check("resolve_h3_prompt 保留旧描述细节", "Aerial shot rising" in resolved)
rebuilt = client._build_h3_prompt(
    dict(SHOT, prompt_h3=LEGACY_EN),
    [{"name": "方源", "appearance": "清瘦少年"}], [{"name": "广场"}], None)
check("_build_h3_prompt 是无条件干净重建（不含旧描述）", "Aerial shot rising" not in rebuilt)
check("_build_h3_prompt 仍产出六段", kit.validate(rebuilt)["valid"] is True)

# ============ 10. 风格幂等 ============
section("10. style_kit.with_style 幂等（修风格重复两遍）")
STYLE = "中国古风玄幻漫剧"
llm_wrote = "清瘦少年三视图，黑瞳锐利，灰粗布常服束发，中国古风玄幻漫剧风格。"
once = style_kit.with_style(llm_wrote, STYLE)
check("模型自写「XX风格。」后不再追加", once == llm_wrote, once)
check("风格词只出现一次", once.count("中国古风玄幻漫剧") == 1, once.count("中国古风玄幻漫剧"))
twice = style_kit.with_style(once, STYLE)
check("二次调用仍幂等", twice == once)
colon_form = "少年三视图。风格：中国古风玄幻漫剧，画面精致，光影细腻，构图稳定，无畸形。"
check("带冒号写法同样幂等", style_kit.with_style(colon_form, STYLE) == colon_form)
clean = "清瘦少年三视图，黑瞳锐利"
appended = style_kit.with_style(clean, STYLE)
check("干净提示词正常追加一次", appended.count("中国古风玄幻漫剧") == 1, appended)
check("追加后带质量收尾", "无畸形" in appended)
check("空提示词 → 只有风格后缀",
      style_kit.with_style("", STYLE).startswith("风格："))

# ============ 11. 英文提示词纠错 ============
section("11. style_kit 英文提示词纠错")
bad_en = "Thin boy three-view, sharp black eyes, Chinese xuanxuan comic style"
fixed = style_kit.sanitize_prompt_en(bad_en)
check("xuanxuan 被纠错", "xuanxuan" not in fixed.lower(), fixed)
# 纠错映射本身（用 re.sub 单测词表，避免被「风格声明一律剥离」的后续步骤掩盖）
_map = bad_en
for _pat, _repl in style_kit._BAD_ROMANIZATION:
    _map = __import__("re").sub(_pat, _repl, _map, flags=__import__("re").IGNORECASE)
check("xuanxuan 映射为 Chinese animated style", "Chinese animated style" in _map, _map)
check("纠错后保留真实画面内容（未被误删）",
      "Thin boy three-view" in fixed and "sharp black eyes" in fixed, fixed)
check("质量词被剥离",
      "masterpiece" not in style_kit.sanitize_prompt_en("a boy, masterpiece, best quality").lower())
check("不再产生 Chinese Chinese 双重污染",
      "chinese chinese" not in fixed.lower(), fixed)
check("模型自写的风格声明被剥离（由程序统一收尾）",
      "style" not in style_kit.sanitize_prompt_en("boy, Chinese animated style, blue robe").lower(),
      style_kit.sanitize_prompt_en("boy, Chinese animated style, blue robe"))
en_styled = style_kit.with_style_en("thin boy three view, black hair", STYLE)
check("英文风格后缀已追加", "Style:" in en_styled, en_styled)
check("英文风格后缀含质量收尾", "highly detailed" in en_styled, en_styled)
check("英文风格幂等", style_kit.with_style_en(en_styled, STYLE) == en_styled)
check("复合风格串被翻译成英文（非中文直落）",
      "古风" not in style_kit.style_suffix_en(STYLE)
      and "ancient Chinese style" in style_kit.style_suffix_en(STYLE),
      style_kit.style_suffix_en(STYLE))

# ============ 11b. 历史脏数据自愈 ============
section("11b. 历史风格双写自愈（_collapse_style）")
DIRTY = (f"清瘦少年三视图，黑瞳锐利，灰粗布常服束发，{STYLE}风格。"
         f"风格：{STYLE}，画面精致，光影细腻，构图稳定，无畸形。")
healed = style_kit.with_style(DIRTY, STYLE)
check("双写被收敛为一份", healed.count(STYLE) == 1, healed.count(STYLE))
check("自愈后仍保留画面前提信息", "清瘦少年三视图" in healed and "灰粗布常服束发" in healed, healed)
check("自愈后带规范后缀", "无畸形" in healed)
check("自愈结果再次调用幂等", style_kit.with_style(healed, STYLE) == healed)

# 换风格场景（用户与总控反复调风格 → 资产提示词里残留的是旧风格）
OLD_STYLE = "国漫3D渲染"
migrated = style_kit.with_style(
    f"清瘦少年三视图，黑瞳锐利，{OLD_STYLE}风格。风格：{OLD_STYLE}，画面精致，光影细腻，构图稳定，无畸形。",
    STYLE)
check("换风格后旧风格被清掉", OLD_STYLE not in migrated, migrated)
check("换风格后只保留新风格一份", migrated.count(STYLE) == 1, migrated)
check("换风格后仍保留画面主体信息", "清瘦少年三视图" in migrated, migrated)
check("换风格结果幂等", style_kit.with_style(migrated, STYLE) == migrated)
# 未重复时不得误伤
once_styled = style_kit.with_style("少年三视图", STYLE)
check("单份风格不被 _collapse_style 误删", style_kit.with_style(once_styled, STYLE) == once_styled)

# ============ 12. apply_asset_style_all ============
section("12. apply_asset_style_all 中英双语补风格")
assets = [{"name": "方源", "reference_prompt_zh": "清瘦少年三视图", "reference_prompt_en": "thin boy three-view"}]
n = style_kit.apply_asset_style_all(assets, STYLE)
check("返回改写条数 > 0", n > 0, n)
check("中文已补风格", STYLE in assets[0]["reference_prompt_zh"], assets[0]["reference_prompt_zh"])
check("英文已补风格", "Style:" in assets[0]["reference_prompt_en"], assets[0]["reference_prompt_en"])
check("中文风格只出现一次", assets[0]["reference_prompt_zh"].count(STYLE) == 1)
check("二次调用幂等", style_kit.apply_asset_style_all(assets, STYLE) == 0)
empty = [{"name": "x", "reference_prompt_zh": "三视图", "reference_prompt_en": ""}]
style_kit.apply_asset_style_all(empty, STYLE)
check("英文为空时不写入垃圾串", "Style:" not in empty[0]["reference_prompt_en"]
      or empty[0]["reference_prompt_en"].startswith("Style:"), empty[0]["reference_prompt_en"])

# ============ 13. novel_to_script._keep_valid_h3 ============
section("13. novel_to_script._keep_valid_h3")
import novel_to_script as nts

check("裸英文被丢弃", nts._keep_valid_h3(LEGACY_EN) == "")
check("空值安全", nts._keep_valid_h3(None) == "" and nts._keep_valid_h3("") == "")
check("结构化提示词被保留", nts._keep_valid_h3(good) == good)
check("base 三段同样被保留",
      nts._keep_valid_h3(base_text) == base_text)

# ============ 14. 源码级断言 ============
section("14. 源码级修复断言")
app_src = src("app.py")
cc_src = src("comfyui_client.py")
nts_src = src("novel_to_script.py")
spa_src = src("script_prompt_analyzer.py")

check("app.py 不再有「prompt_h3 or _build_h3_prompt」顶掉结构化构建器",
      not re.search(r"get\(['\"]prompt_h3['\"]\)\s*or\s*comfyui_client\._build_h3_prompt",
                    app_src))
check("app.py 改用 resolve_h3_prompt（两处）",
      app_src.count("comfyui_client.resolve_h3_prompt") == 2,
      app_src.count("comfyui_client.resolve_h3_prompt"))
check("app.py 已 import h3_prompt_kit", "import h3_prompt_kit" in app_src)
check("comfyui_client 有 resolve_h3_prompt", "def resolve_h3_prompt" in cc_src)
check("comfyui_client 有 _h3_picture_defs", "def _h3_picture_defs" in cc_src)
check("comfyui_client 已 import h3_prompt_kit", "import h3_prompt_kit" in cc_src)
check("分镜提示词不再硬编码「国漫3D渲染风格」兜底",
      'style_clause = "国漫3D渲染风格；"' not in cc_src)
check("novel_to_script 不再让模型写 prompt_h3 英文描述",
      '"prompt_h3": "英文画面描述' not in nts_src)
check("continuity 重写也不再让模型写 prompt_h3",
      '"prompt_h3": "英文画面描述' not in src("continuity.py"))
check("continuity 不回写 prompt_h3",
      '("camera", "location", "description", "emotion", "audio_cues", "prompt_h3")'
      not in src("continuity.py"))
check("novel_to_script 明确禁止输出 prompt_h3 字段",
      "【禁止输出 prompt_h3 字段】" in nts_src)
check("novel_to_script 资产红线改为「不得自写风格词」",
      "不得自行写风格词、画风词或质量词" in nts_src)
check("novel_to_script 要求英文准确翻译（禁止自造罗马字）",
      "invented pinyin" in nts_src and "xuanxuan" in nts_src)
check("novel_to_script 描述上限放宽到 120 字", "120 字以内" in nts_src)
check("novel_to_script 用 apply_asset_style_all（中英双语）",
      nts_src.count("apply_asset_style_all") == 7 and "apply_asset_style(" not in nts_src,
      nts_src.count("apply_asset_style_all"))
check("提示词分析器 REQUIRED_MARKERS 覆盖六段",
      "REQUIRED_MARKERS = h3_prompt_kit.REF_SECTIONS" in spa_src)
check("提示词分析器规格含全部六段",
      all(f"{s}:" in spa_src for s in kit.REF_SECTIONS))
# 2026-09-24 二次对齐：规格不再教「[Shot 1] 00:00.000 中景：」旧行首，改为模板格式
# （首镜 `[Shot 1] <英文景别>:`，后续镜 `At MM:SS.mmm, the camera cuts to …`）。
check("提示词分析器要求模板时间码格式（At MM:SS.mmm, the camera cuts to）",
      "camera cuts to" in spa_src and "[Shot 1]" in spa_src)
check("提示词分析器要求英文景别（不再教中文景别行首）",
      "英文景别短语" in spa_src)
check("提示词分析器不再教有害的「严禁出现任何文字/字幕」",
      "画面里严禁出现任何文字" not in spa_src)
check("提示词分析器要求 [reference generation] 模式声明",
      "[reference generation]" in spa_src)
check("提示词分析器要求 fully_preserved 句式",
      "fully_preserved" in spa_src)
check("提示词分析器要求台词带「开口说话」动作（speaks — … voice）",
      "speaks —" in spa_src and "lips close" in spa_src)
check("提示词分析器不再要求台词语言标记（P0-1 口型分离，不写台词原文）",
      "[Chinese]" not in spa_src and "<d>" not in spa_src)
check("提示词分析器明确不写台词原文（P0-1）", "不要写台词原文" in spa_src)
check("提示词分析器资产端要求不写风格词", "不得包含风格词、画风词或质量词" in spa_src)
check("提示词分析器资产端确定性补风格", "style_kit.apply_asset_style_all" in spa_src)
check("提示词分析器报告具体缺段", "_missing_sections" in spa_src)
check("storyboard_prompt_zh 真正被消费（不再只写不读）",
      'shot.get("storyboard_prompt_zh")' in cc_src)
check("分镜图提示词优先用分析器产出、回退 description",
      'or (shot.get("description") or "").strip()' in cc_src)
check("storyboard 详情接口区分分镜图提示词与视频提示词",
      '"video_prompt": shot_meta.get("prompt_h3")' in app_src)

print("\n" + "=" * 46)
print(f"总计 {PASS + FAIL} 项：PASS {PASS} / FAIL {FAIL}")
if FAIL:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过 ✅")
