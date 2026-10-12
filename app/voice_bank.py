# -*- coding: utf-8 -*-
"""角色音色人设来源（t8 · 2026-10-13）—— 单一事实源。

背景（用户实测「每个角色的声音音色都一样」）
------------------------------------------
H3 主线的音色 100% 由 voice_bank 里每角色的 ref.wav 决定（H3 只有 ref_audio_0..2 音频槽，
没有文本音色入参）。ref.wav 由 tts_pre 用 QwenTTS 合成：有音色描述（instruct）就走
VoiceDesign（FB_Qwen3TTSVoiceDesign，真按人设造声线），没有就退 preset（9 个预置嗓子）。

原实现的断点：人设（voice_style）**只从第 1 集剧本读**（video_helpers 里硬编码第1集.json），
而「要生成哪些角色」来自角色资产全集。两者不重合时，多出来的角色拿不到 voice_style
→ mode=preset + speaker=SPEAKER_KEYS[0]（Ryan）+ seed=0 + instruct 空
→ **这些角色是同一个嗓子**（实测第 1 集只列 4 个角色，9 个里 5 个退化成 Ryan，
LTAS 长时平均谱余弦组内 0.9584 vs 有描述组 0.8152）。

本模块把「人设从哪来」收敛成唯一实现，供两条路径共用：
  · video_helpers._ensure_voice_bank_refs —— 生成/重做 ref.wav（H3 主线音色来源）；
  · routes.tts 的试听端点 —— 让试听走「人设」，而不是拿一段与人设无关的旧样本冒充。

取值优先序（captain 2026-10-13 定，实现逐条对应）
----------------------------------------------
  1. voice_map.json 里该角色**显式**的 instruct（用户在界面上手工定过，最权威）；
  2. 该角色**首次出场集**里的 voice_style（非空才采纳）；
  3. 该角色**台词最多集**里的 voice_style（非空才采纳；同台词数取集号小者）；
  4. 都没有 → 用 gender/age/identity/personality 拼一条中文音色描述**兜底**（仍走 design）；
  5. 连兜底都拼不出 → 回落 preset（并让 speaker 按名字确定性散开，不再人人 Ryan）。

⚠️ 同一角色的 voice_style 跨集常常不一致（例：「清冷少年音，先抑后扬」/「清朗少年音」/
「清冷少年音，平静有力」）。ref.wav 是**项目级单份**，故必须有一条确定性规则 ——
就是上面的优先序；否则音色取决于「哪一集先跑」，跨集漂移且不可复现。

本模块全部为纯函数 + 目录扫描缓存：**不联网、不调 LLM**；唯一写文件的是 mark_auto_ref，
且只在调用方明确要求时写 ref.json 元数据（不动音频本体）。
"""
import hashlib
import io
import json
import logging
import os
import re

from config import SCRIPT_DIR

logger = logging.getLogger(__name__)

#: ref.json 里标记「这段参考音频是本项目自动合成的」
REF_SOURCE_AUTO = "auto"
#: ref.json 里标记「用户自己上传的参考音频」——**永不覆盖**
REF_SOURCE_UPLOADED = "uploaded"

#: 自动生成的参考音频文件名形态：tts_client 走 tempfile.NamedTemporaryFile 产生的 tmpXXXX.wav
_AUTO_TMP_RE = re.compile(r"^tmp[0-9a-z_\-]+\.wav$", re.IGNORECASE)
#: 自动生成时用的参考文本模板（识别老数据：那时还不会写 source 字段）
_AUTO_TEXT_RE = re.compile(r"^我是[^，,。]{1,24}(，.*)?。?这是我的声音样本。$")

#: 人设索引缓存：{项目名: (目录签名, {角色名: 条目})}
_PERSONA_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# 剧本扫描：角色人设（全项目所有集）
# --------------------------------------------------------------------------- #

def _episode_no(path: str, data: dict) -> int:
    """该剧本文件属于第几集：优先 JSON 的 episode_no，其次文件名「第N集」。"""
    try:
        n = int(data.get("episode_no"))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    m = re.search(r"第\s*(\d+)\s*集", os.path.basename(path))
    if m:
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            pass
    return 0


def _dialogue_speakers(raw) -> list:
    """归一化 shots[].dialogue 的说话人列表（dict / str / list 三种形态都吃）。"""
    out = []
    if raw is None or isinstance(raw, str):
        return out
    if isinstance(raw, dict):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        for it in raw:
            if isinstance(it, dict):
                n = str(it.get("speaker") or it.get("character") or it.get("role")
                        or it.get("name") or "").strip()
                if n:
                    out.append(n)
    return out


def _script_files(project_name: str) -> list:
    """项目剧本文件（含 <项目>/第N集.json 与 <项目>/ep01/第N集.json 两种布局），跳过 .bak。"""
    root = os.path.join(SCRIPT_DIR, str(project_name or ""))
    if not project_name or not os.path.isdir(root):
        return []
    out = []
    for dp, _dn, fns in os.walk(root):
        if os.path.relpath(dp, root).count(os.sep) >= 2:
            continue          # 只下探一层子目录（ep01 这种集目录）
        for fn in fns:
            low = fn.lower()
            if not low.endswith(".json") or ".bak" in low:
                continue
            out.append(os.path.join(dp, fn))
    return sorted(out)


def _dir_signature(files: list) -> tuple:
    sig = []
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        sig.append((os.path.basename(p), int(st.st_mtime), int(st.st_size)))
    return tuple(sig)


def persona_index(project_name: str) -> dict:
    """扫**全项目所有集**的剧本，汇总每个角色的跨集人设。

    返回 {角色名: 条目}，条目形如::

        {"name": 角色名,
         "episodes": [{"episode": 1, "file": "...", "voice_style": "清冷少年音",
                        "lines": 21, "shots": 32, "gender": "男", "age": "十六岁",
                        "identity": "...", "personality": "...", "appearance": "..."}, ...],
         "gender"/"age"/"identity"/"personality"/"appearance": 首次出场集的取值}

    lines = 台词条数（shots[].dialogue 的 speaker 命中），用于「台词最多集」取值；
    episodes 按集号升序（同集号按文件名稳定排序）。
    结果按剧本目录签名缓存 —— 本函数在逐角色/逐次试听时被反复调用，不能每次都读盘解析。
    """
    files = _script_files(project_name)
    if not files:
        return {}
    key = str(project_name)
    sig = _dir_signature(files)
    cached = _PERSONA_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    per_char: dict = {}
    for path in files:
        try:
            with io.open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001 - 单集剧本坏了不该拖垮音色链路
            continue
        if not isinstance(data, dict):
            continue
        ep = _episode_no(path, data)
        ep_seen: dict = {}
        for c in (data.get("characters") or []):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            rec = per_char.setdefault(name, {"name": name, "episodes": []})
            rec["episodes"].append({
                "episode": ep,
                "file": path,
                "voice_style": str(c.get("voice_style") or "").strip(),
                "lines": 0,
                "shots": 0,
                "gender": str(c.get("gender") or "").strip(),
                "age": str(c.get("age") or "").strip(),
                "identity": str(c.get("identity") or "").strip(),
                "personality": str(c.get("personality") or "").strip(),
                "appearance": str(c.get("appearance") or c.get("description") or "").strip(),
            })
            ep_seen[name] = rec["episodes"][-1]
        for shot in (data.get("shots") or []):
            if not isinstance(shot, dict):
                continue
            cast = shot.get("characters_in_shot") or []
            if isinstance(cast, str):
                cast = [cast]
            for n in cast:
                n = str(n or "").strip()
                if n in ep_seen:
                    ep_seen[n]["shots"] += 1
            for n in _dialogue_speakers(shot.get("dialogue")):
                if n in ep_seen:
                    ep_seen[n]["lines"] += 1
    out = {}
    for name, rec in per_char.items():
        eps = sorted(rec["episodes"], key=lambda e: (e["episode"], os.path.basename(e["file"])))
        first = eps[0] if eps else {}
        out[name] = {
            "name": name,
            "episodes": eps,
            "gender": str(first.get("gender") or ""),
            "age": str(first.get("age") or ""),
            "identity": str(first.get("identity") or ""),
            "personality": str(first.get("personality") or ""),
            "appearance": str(first.get("appearance") or ""),
        }
    _PERSONA_CACHE[key] = (sig, out)
    return out


def clear_cache() -> None:
    """清空人设缓存（测试与「剧本刚被改写」时用）。"""
    _PERSONA_CACHE.clear()


# --------------------------------------------------------------------------- #
# 取值优先序：voice_map 显式 > 首次出场集 > 台词最多集 > 兜底拼装
# --------------------------------------------------------------------------- #

def _compose_fallback(entry: dict) -> str:
    """由 gender/age/identity/personality 拼一条中文音色描述（都没有则空串）。"""
    entry = entry or {}
    parts = []
    for key, suffix in (("age", ""), ("gender", "性"), ("identity", ""), ("personality", "")):
        v = str(entry.get(key) or "").strip()
        if v:
            parts.append(v + suffix)
    desc = "，".join(parts)
    return ("音色：" + desc) if desc else ""


def _explicit_instruct(voice_map, name: str) -> str:
    """voice_map.json 里该角色**显式**的音色描述（没有则空串）。"""
    try:
        v = ((voice_map or {}).get("characters") or {}).get(name) or {}
    except AttributeError:
        return ""
    if not isinstance(v, dict):
        return ""
    return str(v.get("instruct") or "").strip()


def resolve_voice_style(entry: dict, voice_map=None) -> tuple:
    """按优先序取该角色的音色描述 → (voice_style, 来源标签, 集号)。

    来源标签是给日志/接口用的中文短语，取值：
      · "voice_map 显式配置"
      · "首次出场集(第N集)"
      · "台词最多集(第N集，M 句)"
      · "兜底拼装(gender/age/identity/personality)"
      · "无（回落预置音色）"
    """
    entry = entry or {}
    name = str(entry.get("name") or "").strip()
    eps = list(entry.get("episodes") or [])
    explicit = _explicit_instruct(voice_map, name)
    if explicit:
        return explicit, "voice_map 显式配置", None
    if eps:
        first = eps[0]
        vs = str(first.get("voice_style") or "").strip()
        if vs:
            ep0 = int(first.get("episode") or 0) or None
            return vs, "首次出场集(第%d集)" % int(first.get("episode") or 0), ep0
    best = None
    for e in eps:
        if not str(e.get("voice_style") or "").strip():
            continue
        if best is None or int(e.get("lines") or 0) > int(best.get("lines") or 0):
            best = e
    if best is not None:
        return (str(best.get("voice_style") or "").strip(),
                "台词最多集(第%d集，%d 句)" % (int(best.get("episode") or 0),
                                             int(best.get("lines") or 0)),
                int(best.get("episode") or 0) or None)
    composed = _compose_fallback(entry)
    if composed:
        return (composed, "兜底拼装(gender/age/identity/personality)",
                (int(eps[0].get("episode") or 0) or None) if eps else None)
    return "", "无（回落预置音色）", (int(eps[0].get("episode") or 0) or None) if eps else None


def _fallback_speaker(name: str, gender: str) -> str:
    """无人设时的预置嗓子：按**名字稳定哈希**确定性散开（不再人人 Ryan）。

    旧行为：单人成组 → 池位恒取第 0 位 → 所有男性角色 = Ryan、所有女性 = Serena，
    这本身就是一种「音色都一样」。这里按名字哈希分散到池内不同预设，且与
    「哪一集先跑」「字典序」无关（同名恒同 speaker）。
    """
    import tts_client
    g = str(gender or "").strip()
    if "女" in g:
        pool = list(tts_client.FEMALE_POOL)
    elif "男" in g:
        pool = list(tts_client.MALE_POOL)
    else:
        pool = list(tts_client.SPEAKER_KEYS)
    if not pool:
        return tts_client.SPEAKER_KEYS[0]
    h = int(hashlib.sha1(("speaker|" + str(name)).encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def persona_ref_text(name: str, entry: dict) -> str:
    """参考音色的样句：把身份/年龄/性格写进句子（TTS 有语境，音色更贴人设）。"""
    entry = entry or {}
    bits = []
    for k in ("identity", "age", "personality"):
        v = str(entry.get(k) or "").strip()
        if v:
            bits.append(v)
    desc = "，".join(bits)
    if desc:
        return "我是%s，%s。这是我的声音样本。" % (name, desc)
    return "我是%s，这是我的声音样本。" % name


def build_voice_spec(project_name: str, name: str, *, voice_map=None, entry=None,
                     persona=None) -> dict:
    """该角色该用什么 TTS 音色（ref.wav 生成**与**试听共用）。

    返回::

        {"character", "mode": "design"|"preset", "speaker", "seed", "instruct",
         "ref_text", "voice_style_source", "source_episode", "reason"}

    mode=design 时 instruct 就是人设音色描述（VoiceDesign 参数）；
    mode=preset 只在「连兜底描述都拼不出」时出现。
    seed 用 tts_client.stable_seed(项目|角色) —— 与既有口径一致，跨集恒稳。
    """
    import tts_client
    if entry is None:
        if persona is None:
            persona = persona_index(project_name)
        entry = persona.get(name) or {"name": name, "episodes": []}
    instruct, source, ep_no = resolve_voice_style(entry, voice_map=voice_map)
    gender = str(entry.get("gender") or "").strip()
    speaker = _fallback_speaker(name, gender)
    try:
        seed = int(tts_client.stable_seed("%s|%s" % (project_name, name)))
    except Exception:  # noqa: BLE001
        seed = 0
    ref_text = persona_ref_text(name, entry)
    mode = "design" if instruct else "preset"
    if mode == "design":
        if ep_no:
            reason = "角色 %s 取第 %s 集说明（理由：%s）" % (name, ep_no, source)
        else:
            reason = "角色 %s 取说明（理由：%s）" % (name, source)
    else:
        reason = ("角色 %s 全项目剧本都没有可用的 voice_style/人设 → 回落预置嗓子 %s（%s）"
                  % (name, speaker, source))
    return {
        "character": name,
        "mode": mode,
        "speaker": speaker,
        "seed": seed,
        "instruct": instruct,
        "ref_text": ref_text,
        "voice_style_source": source,
        "source_episode": ep_no,
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# 参考音频来源判定：用户上传 vs 本项目自动合成（前者永不覆盖）
# --------------------------------------------------------------------------- #

def read_ref_meta(dub_dir: str, character: str) -> dict:
    """读 voice_bank/<角色>/ref.json（缺失/损坏返回 {}）。"""
    try:
        import tts_client
        p = os.path.join(tts_client.voice_bank_dir(dub_dir, character), "ref.json")
    except Exception:  # noqa: BLE001
        return {}
    if not os.path.isfile(p):
        return {}
    try:
        with io.open(p, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    return meta if isinstance(meta, dict) else {}


def ref_origin(dub_dir: str, character: str) -> str:
    """该角色参考音频的来源：uploaded / auto / none。

    判定顺序：
      1. ref.json 里的 source 标记（新数据由 mark_auto_ref 或上传接口写）；
      2. 老数据回落启发式：文件名形如 tmpXXXX.wav（tempfile 产物）**且** ref_text 是
         自动样句模板 → auto；其余一律当 uploaded（**宁可不动**）。
    """
    import tts_client
    try:
        ref, ref_text = tts_client.find_voice_bank_ref(dub_dir, character)
    except Exception as e:  # noqa: BLE001 - 读不到就按「未绑定」处理，但必须留痕
        logger.warning("[音色人设] %s 参考音色读取失败（按未绑定处理）：%s", character, e)
        return "none"
    if not ref:
        return "none"
    meta = read_ref_meta(dub_dir, character)
    src = str(meta.get("source") or "").strip().lower()
    if src in (REF_SOURCE_AUTO, REF_SOURCE_UPLOADED):
        return src
    fname = os.path.basename(str(meta.get("original_filename") or ref))
    text = str(meta.get("ref_text") or ref_text or "").strip()
    if _AUTO_TMP_RE.match(fname) and (not text or _AUTO_TEXT_RE.match(text)):
        return REF_SOURCE_AUTO
    return REF_SOURCE_UPLOADED


def mark_auto_ref(dub_dir: str, character: str, *, mode: str = "", instruct: str = "",
                  seed=None, source_episode=None, voice_style_source: str = "",
                  ref_text: str = "", extra: dict = None) -> dict:
    """给**自动合成**的参考音频打标（ref.json 合并写回，不动音频本体）。

    为什么要打标：老的 ref.json 只有 character/ref_text/original_filename/duration_sec，
    既无法区分「用户上传」与「本项目自动生成」（本次排查就被卡住），也无法追溯
    「当初用哪条人设、哪个 seed 生成」。打完标后 ref_origin 一眼可判，
    用户上传的音频因此**永不被自动流程覆盖**。
    """
    import tts_client
    meta = dict(read_ref_meta(dub_dir, character) or {})
    meta["character"] = character
    meta["source"] = REF_SOURCE_AUTO
    if mode:
        meta["mode"] = str(mode)
    if instruct:
        meta["instruct"] = str(instruct)
    if seed is not None:
        try:
            meta["seed"] = int(seed)
        except (TypeError, ValueError):
            pass
    if source_episode is not None:
        try:
            meta["source_episode"] = int(source_episode)
        except (TypeError, ValueError):
            pass
    if voice_style_source:
        meta["voice_style_source"] = str(voice_style_source)
    if ref_text:
        meta["ref_text"] = str(ref_text)
    for k, v in (extra or {}).items():
        meta[str(k)] = v
    p = os.path.join(tts_client.voice_bank_dir(dub_dir, character), "ref.json")
    try:
        tts_client.save_voice_map(meta, p)
    except Exception as e:  # noqa: BLE001 - 打标失败不影响音色本体
        logger.warning("[音色人设] %s ref.json 打标失败（忽略）：%s", character, e)
    return meta


# --------------------------------------------------------------------------- #
# 试听：走人设（VoiceDesign）还是走用户上传的克隆源
# --------------------------------------------------------------------------- #

def preview_spec(dub_dir: str, project_name: str, character: str, *,
                 voice_map=None) -> dict:
    """试听该角色时用的音色方案。

    · 用户**上传**过参考音频 → clone 它（那就是用户要听的声线，不能被人设顶掉）；
    · 其余（自动合成 / 还没有参考音频）→ **design + 人设 instruct**
      （= H3 将来会听到的声线，因为 ref.wav 本身就是用同一条 instruct 合成的）；
    · 连人设都取不到 → preset（speaker 按名字稳定散开）。

    返回 {"mode", "speaker", "seed", "instruct", "ref_audio", "ref_text",
          "voice_style_source", "source_episode", "ref_origin", "reason"}
    """
    import tts_client
    origin = ref_origin(dub_dir, character)
    spec = build_voice_spec(project_name, character, voice_map=voice_map)
    out = {
        "character": character,
        "mode": spec["mode"],
        "speaker": spec["speaker"],
        "seed": spec["seed"],
        "instruct": spec["instruct"],
        "ref_audio": "",
        "ref_text": spec["ref_text"],
        "voice_style_source": spec["voice_style_source"],
        "source_episode": spec["source_episode"],
        "ref_origin": origin,
        "reason": spec["reason"],
    }
    if origin == REF_SOURCE_UPLOADED:
        ref, ref_text = tts_client.find_voice_bank_ref(dub_dir, character)
        if ref:
            out["mode"] = "clone"
            out["ref_audio"] = ref
            out["ref_text"] = ref_text or out["ref_text"]
            out["reason"] = ("角色 %s 试听：克隆**用户上传**的参考音频（%s）；人设来源=%s"
                             % (character, os.path.basename(ref), spec["voice_style_source"]))
    else:
        out["reason"] = ("角色 %s 试听：按人设走 VoiceDesign（%s）；%s"
                         % (character, spec["voice_style_source"], spec["reason"]))
    return out
