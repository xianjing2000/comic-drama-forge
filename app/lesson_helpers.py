'''教训库写入助手（2026-10-11 从 app.py 下沉，助手域第三批）。'''

# 依赖闭包（实测 5 个对象 169 行）：
#   _record_qc_lesson 45 ｜ _record_preflight_lesson 24 ｜ _record_audio_qc_lesson 28
#   _qc_lesson_from_record 23（0 依赖）｜ _apply_audio_hints 49
# app 仅用于 logger（已改为模块 logger）。
import logging

# 2026-10-11 补齐搬迁时遗漏的模块级名字（自动扫描发现）
from config import PROJECT_OUTPUT_DIR
from dub_helpers import _dub_character_desc
from dub_helpers import _dub_line_speaker_from_script
from routes._shared import _project_style
from workers.audio import _audio_line_expect_sec
import prompt_memory
import tts_client
logger = logging.getLogger(__name__)


def _qc_lesson_from_record(rec: dict) -> dict:
    """从一条质检历史记录里取出「缺陷」，供教训库沉淀。

    ⚠️ `_qc_record_verdict` 返回的记录把 score/reason/issues 放在**顶层**，
    **没有** `verdict` 子对象。此前写入教训时误读 `rec["verdict"]`（恒为 None → {}），
    于是教训库里躺着的全是 score=0 / reason="" / issues=[] 的空记录，
    召回时自然什么建议都给不出来 —— 重试就变成了「换种子瞎撞」。
    这里对两种形态都做兼容，避免再被字段形态坑一次。
    """
    if not isinstance(rec, dict):
        return {}
    inner = rec.get("verdict") if isinstance(rec.get("verdict"), dict) else {}
    score = rec.get("score")
    if score is None:
        score = inner.get("score")
    issues = list(rec.get("issues") or inner.get("issues") or [])
    issues += list(rec.get("critical_issues") or inner.get("critical_issues") or [])
    issues += list(rec.get("style_issues") or inner.get("style_issues") or [])
    issues = [str(x).strip() for x in issues if str(x).strip()]
    reason = str(rec.get("reason") or inner.get("reason") or "").strip()
    if not issues and reason:
        issues = [reason]
    return {"score": score if score is not None else 0, "issues": issues[:20], "reason": reason}


def _record_qc_lesson(project_name: str, kind: str, prompt: str, rec: dict,
                      root_dir: str = "") -> dict:
    """把一次「质检不达标」沉淀成教训（供下次重试时改写提示词）。

    风格由 ``_project_style()`` 内部取（plan 的 style > AI 设定 > config.style），
    这样 6 个调用点不用各自找 style —— 它们本来就都在同一个项目上下文里。
    """
    lesson_src = _qc_lesson_from_record(rec)
    # 记录当时的视觉风格：召回时按「同风格加权 / 异风格降权」使用。
    # 没有它就无法回答「生成相同风格的提示词时有没有参考历史教训」——
    # 旧教训一律 context={}，跨画风的缺陷会串味（用 A 画风的标准要求 B 画风的图）。
    try:
        _qc_style = _project_style(project_name) or ""
    except Exception as _se:  # noqa: BLE001
        logger.warning("取项目风格失败（教训按无风格记录）：%s", _se)
        _qc_style = ""
    # 风格不达标：额外注入一条「明确的风格强化指令」，确保召回时能直接指导模型修正风格，
    # 而不是只给一条「风格不符」的缺陷描述。
    # ⚠️ 风格名必须写成占位符 {style}，**不能在记录时把项目风格写死**：
    #    教训库是跨项目复用的，写死会让 A 项目（中国古风玄幻）的教训被 B 项目
    #    （国漫偏写实）召回时强行要求 B 采用 A 的风格 —— 那是主动伤害。
    #    实际替换发生在 prompt_memory.suggestions(..., style=当前项目风格)。
    if (rec or {}).get("style_mismatch"):
        style_hint = ("画面风格与目标风格不符，必须严格采用「{style}」"
                      "的视觉风格、画风、渲染方式与配色，不得偏离")
        existing = lesson_src.get("issues") or []
        lesson_src["issues"] = [style_hint] + [i for i in existing if i != style_hint]
    if not lesson_src.get("issues") and not lesson_src.get("reason"):
        return {}
    try:
        got = prompt_memory.record(project=project_name, kind=kind, prompt=prompt,
                                   issues=lesson_src["issues"], reason=lesson_src["reason"],
                                   score=lesson_src.get("score"),
                                   # 2026-10-09：新增可选 root_dir 便于**隔离测试**（默认仍是项目输出目录，
                                   # 行为不变）。此前测试只能落真实教训库、再反手清理 —— 见报告第 116 节。
                                   root_dir=root_dir or PROJECT_OUTPUT_DIR, style=_qc_style)
        if got:
            logger.info("[教训库] %s 记录 %d 条缺陷（kind=%s score=%s style=%s）：%s",
                            project_name, len(lesson_src["issues"]), kind,
                            lesson_src.get("score"), _qc_style or "-",
                            lesson_src["issues"][:2])
        return got or {}
    except Exception as mem_err:  # noqa: BLE001
        logger.warning("记录质检教训失败：%s", mem_err)
        return {}


def _record_preflight_lesson(project_name: str, prompt_original: str, pf: dict,
                             gate: dict) -> dict:
    """把一次「提示词预检不通过 / 有缺陷」沉淀成 ``kind="prompt"`` 教训。

    ``prompt_original`` 必须是**自愈前**（也**不含召回叠加块**）的原始提示词，作为稳定
    phash 键。收敛 keyframe / asset / storyboard 三处预检的沉淀逻辑，避免复制粘贴。
    """
    pf = pf if isinstance(pf, dict) else {}
    verdict = pf.get("verdict") if isinstance(pf.get("verdict"), dict) else {}
    gate = gate if isinstance(gate, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in (verdict.get("issues") or []) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(pf.get("reason") or gate.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "stage": "prompt_preflight",
        "label": str(pf.get("label") or gate.get("label") or ""),
    }
    if not rec["issues"] and rec["reason"]:
        rec["issues"] = [rec["reason"]]
    if not rec["issues"] and not rec["reason"]:
        return {}
    return _record_qc_lesson(project_name, "prompt", prompt_original or "", rec)


def _record_audio_qc_lesson(project_name: str, ln: dict, verdict: dict) -> dict:
    """把一句「配音成品质检不达标」沉淀成 ``kind="audio"`` 教训。

    提示词键用**自愈前**的台词原文（``audio_orig_text``，回退当前 ``ln["text"]``）：
    它正是 TTS 的实际输入，phash 稳定；预检已自愈过 text 时取自愈前的原文，避免指纹漂移。
    ⚠️ 沉淀的 issues **只进教训库，绝不改台词**（音频类召回是计划级纠偏，见 _apply_audio_hints）。
    """
    text_key = ln.get("audio_orig_text") or ln.get("text") or ""
    if not text_key:
        return {}
    verdict = verdict if isinstance(verdict, dict) else {}
    rec = {
        "issues": [str(x).strip() for x in
                   (list(verdict.get("issues") or []) +
                    list(verdict.get("critical_issues") or [])) if str(x).strip()],
        "critical_issues": [str(x).strip() for x in (verdict.get("critical_issues") or [])
                            if str(x).strip()],
        "reason": str(verdict.get("reason") or "").strip(),
        "score": verdict.get("score"),
        "audio": True,
    }
    if not rec["issues"] and not rec["reason"]:
        return {}
    try:
        return _record_qc_lesson(project_name, "audio", text_key, rec)
    except Exception as e:  # noqa: BLE001 - 沉淀失败绝不影响配音
        logger.warning(f"配音教训沉淀失败（忽略）：{e}")
        return {}


def _apply_audio_hints(ln: dict, hints: list, project_name: str = "") -> None:
    """音频类召回的**计划级纠偏**（设计 D4：音频建议绝不拼进 ``ln["text"]``，会被 TTS 念出来）。

    逐条扫描 hints（缺陷描述），按特征做确定性纠偏，只动 plan 的说话人/音色模式/期望时长：
      - 含「旁白」「speaker」「角色」：若本句说话人是旁白兜底（剧本 dialogue 没登记 speaker），
        且能拿到该镜在剧本里登记的 speaker，则回填 ``ln["character"]``，避免角色台词被旁白念；
      - 含「情绪」「语气」「instruct」：``voice.mode == "preset"`` 时切到 ``design``，
        并确保 ``instruct`` 携带该句情绪（preset 的 CustomVoice 会忽略 instruct，只有
        VoiceDesign 真正按 instruct 控制语气）；
      - 含「时长」「截断」：记录 ``ln["audio_expect_sec"]``（期望时长）供后续质检比对，不阻断；
      - 其它：仅留痕（hints 由调用方写入 ``ln["audio_hints"]`` 审计），不改 plan。

    纯就地修改、永不抛异常、不改 tts_client.py（build_dub_plan 保持纯计划构建）。
    """
    hints = [str(h).strip() for h in (hints or []) if str(h).strip()]
    if not hints:
        return
    try:
        joined = " ".join(hints)
        voice = ln.get("voice") or {}
        # —— 说话人回填：旁白兜底 + hint 提示该句其实是角色台词 → 按剧本登记的 speaker 纠偏 ——
        if (("旁白" in joined or "speaker" in joined or "角色" in joined)
                and str(ln.get("character") or "") == tts_client.NARRATION_SPEAKER):
            speaker = ""
            try:
                speaker = _dub_line_speaker_from_script(ln, project_name)
            except Exception:  # noqa: BLE001
                speaker = ""
            if speaker and speaker != tts_client.NARRATION_SPEAKER:
                ln["character"] = speaker
        # —— 情绪/语气：preset 忽略 instruct → 切 design 并携带情绪 ——
        if ("情绪" in joined or "语气" in joined or "instruct" in joined.lower()):
            emotion = str(ln.get("emotion") or "").strip()
            if emotion and not tts_client._is_neutral_emotion(emotion):
                desc = ""
                try:
                    desc = _dub_character_desc(ln.get("character"), project_name)
                except Exception:  # noqa: BLE001
                    desc = ""
                voice = dict(voice, mode="design",
                             instruct=tts_client._emotion_instruct(emotion, desc))
                ln["voice"] = voice
        # —— 时长/截断：记录期望时长供质检比对（不阻断）——
        if "时长" in joined or "截断" in joined:
            expect = _audio_line_expect_sec(ln)
            if expect > 0:
                ln["audio_expect_sec"] = round(float(expect), 2)
    except Exception as e:  # noqa: BLE001 - 纠偏失败绝不影响配音
        logger.warning(f"配音教训纠偏失败（忽略）：{e}")
