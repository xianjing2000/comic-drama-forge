# -*- coding: utf-8 -*-
"""验证「思考档位（reasoning_effort）」全链路

背景：GLM-5.3-Flash 是 always-on reasoning —— 没有 enable_thinking=false 这个开关，
只能靠 reasoning_effort = low|high|max 调档（默认 max，最贵）。本项目原先只支持
「可关思考」的 Qwen/vLLM 系（disable_thinking），换成 GLM 后 max_tokens 太小 →
正文全空只剩 reasoning_content → 被误判成模型不可用 / 整集兜底。

本测试**不起 Flask、不联网、不写用户真实配置**：
- llm_client 的 HTTP 层用假 _post 驱动，直接检查真正发出去的请求体；
- ai_config 的读写全部指向 .workbuddy/test/_out/ 下的临时配置路径。
"""
import json
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
os.environ.setdefault("MJSCXT_AUTOPILOT", "0")

import llm_client  # noqa: E402
import ai_config   # noqa: E402
import ai_credentials_db  # noqa: E402

PASS, FAIL = [], []
OUT_DIR = os.path.join(ROOT, ".workbuddy", "test", "_out")
TMP_CFG = os.path.join(OUT_DIR, "reasoning_effort_probe_config.json")

# ⚠️ 隔离 AI 凭证库（tasks.db）：ai_config.save_module 会同步写 DB（P0-5 / A2），
#    不隔离会把探针端点（probe.invalid）写进**用户真实库**的 text 模块。
_CRED_ISO = os.path.join(OUT_DIR, "reasoning_effort_cred_iso")
os.makedirs(_CRED_ISO, exist_ok=True)
ai_credentials_db._PROJECT_ROOT = _CRED_ISO
ai_credentials_db._DB_PATH = os.path.join(_CRED_ISO, "output", "tasks.db")
ai_credentials_db._SCHEMA_READY = False


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def make_client(**kw):
    cfg = {"base_url": "http://127.0.0.1:1/v1", "api_key": "sk-test", "model": "glm-5.3-flash"}
    cfg.update(kw)
    return llm_client.LLMClient("", config=cfg, timeout=5)


# ---- 假的 HTTP 层：记录真实请求体，回一个合法 JSON ----
CAPTURED = []


def fake_post(self, payload, timeout=None, chat_url=None):
    CAPTURED.append(json.loads(json.dumps(payload)))
    return {
        "choices": [{"message": {"content": '{"hello": "world"}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


llm_client.LLMClient._post = fake_post


# ===================== 0. 常量 =====================
print("== 0. 档位常量 ==")
check("REASONING_EFFORT_LEVELS == ('low','high','max')",
      tuple(llm_client.REASONING_EFFORT_LEVELS) == ("low", "high", "max"),
      str(getattr(llm_client, "REASONING_EFFORT_LEVELS", None)))
check("MIN_TOKENS_WHEN_REASONING_EFFORT == 2048",
      llm_client.MIN_TOKENS_WHEN_REASONING_EFFORT == 2048,
      str(getattr(llm_client, "MIN_TOKENS_WHEN_REASONING_EFFORT", None)))
check("MIN_TOKENS_WHEN_REASONING_EFFORT >= MIN_TOKENS_WHEN_THINKING",
      llm_client.MIN_TOKENS_WHEN_REASONING_EFFORT >= llm_client.MIN_TOKENS_WHEN_THINKING,
      f"{llm_client.MIN_TOKENS_WHEN_REASONING_EFFORT} vs {llm_client.MIN_TOKENS_WHEN_THINKING}")

# ===================== 1. 请求体注入 =====================
print("== 1. _build_payload 注入档位 ==")
CAPTURED.clear()
cl = make_client(reasoning_effort="low")
p = cl._build_payload([{"role": "user", "content": "hi"}], 0.4, 512)
check("档位 low 被注入 chat_template_kwargs",
      (p.get("chat_template_kwargs") or {}).get("reasoning_effort") == "low",
      json.dumps(p, ensure_ascii=False))
check("clear_thinking 显式传 True（避免上一轮思考串场）",
      (p.get("chat_template_kwargs") or {}).get("clear_thinking") is True,
      json.dumps(p.get("chat_template_kwargs"), ensure_ascii=False))
check("档位模式不再注入 enable_thinking（两套机制互斥）",
      "enable_thinking" not in (p.get("chat_template_kwargs") or {}),
      json.dumps(p.get("chat_template_kwargs"), ensure_ascii=False))
check("max_tokens 512 → 抬到 2048",
      p["max_tokens"] == 2048, str(p.get("max_tokens")))

CAPTURED.clear()
p2 = make_client(reasoning_effort="max")._build_payload([], 0.4, 8192)
check("max_tokens 已够大时不回退（8192 保持 8192）",
      p2["max_tokens"] == 8192, str(p2.get("max_tokens")))

print("== 1b. 与 disable_thinking 的互斥关系 ==")
p3 = make_client(reasoning_effort="high", disable_thinking=True)._build_payload([], 0.4, 4096)
check("disable_thinking=True + 档位 → 档位优先，不发 enable_thinking",
      (p3.get("chat_template_kwargs") or {}).get("reasoning_effort") == "high"
      and "enable_thinking" not in (p3.get("chat_template_kwargs") or {}),
      json.dumps(p3.get("chat_template_kwargs"), ensure_ascii=False))

p4 = make_client(disable_thinking=True)._build_payload([], 0.4, 4096)
check("无档位 + disable_thinking=True → 走原来的 enable_thinking=false",
      (p4.get("chat_template_kwargs") or {}).get("enable_thinking") is False,
      json.dumps(p4.get("chat_template_kwargs"), ensure_ascii=False))

print("== 1c. 非法档位必须被忽略（部分模型会把非法值当最高档） ==")
bad = make_client(reasoning_effort="MAX")   # 大写 → 归一化后合法
check("'MAX' 归一化为 'max'（大小写不敏感）", bad.reasoning_effort == "max", bad.reasoning_effort)
bad2 = make_client(reasoning_effort="turbo")
check("非法值 'turbo' → 忽略为空", bad2.reasoning_effort == "", bad2.reasoning_effort)
pb = bad2._build_payload([], 0.4, 4096)
check("非法值不发 reasoning_effort 字段",
      "reasoning_effort" not in (pb.get("chat_template_kwargs") or {}),
      json.dumps(pb.get("chat_template_kwargs"), ensure_ascii=False))

# ===================== 2. 真实调用路径 =====================
print("== 2. chat_ex / chat_json_robust 实际发出的请求体 ==")
CAPTURED.clear()
cl2 = make_client(reasoning_effort="low")
r = cl2.chat_ex([{"role": "user", "content": "hi"}], max_tokens=100)
check("chat_ex 请求体带档位",
      (CAPTURED[-1].get("chat_template_kwargs") or {}).get("reasoning_effort") == "low",
      json.dumps(CAPTURED[-1], ensure_ascii=False))
check("chat_ex 拿到正文", r["content"] == '{"hello": "world"}', str(r)[:200])

CAPTURED.clear()
data = cl2.chat_json_robust("给我一个 JSON", max_tokens=256, max_attempts=1)
check("chat_json_robust 能解析出 JSON", data.get("hello") == "world", str(data))
check("chat_json_robust 请求体带档位 + 额度下限 2048",
      (CAPTURED[-1].get("chat_template_kwargs") or {}).get("reasoning_effort") == "low"
      and CAPTURED[-1]["max_tokens"] >= 2048,
      json.dumps(CAPTURED[-1], ensure_ascii=False))
check("last_json_meta 记录了 attempts",
      (cl2.last_json_meta or {}).get("attempts") == 1, str(cl2.last_json_meta))

print("== 2b. test_connection 也走同一条 payload 构建（曾自己拼 body 绕过下限） ==")
CAPTURED.clear()
tr = make_client(reasoning_effort="low").test_connection()
check("连通测试带档位", (CAPTURED[-1].get("chat_template_kwargs") or {}).get("reasoning_effort") == "low",
      json.dumps(CAPTURED[-1], ensure_ascii=False))
check("连通测试 max_tokens ≥ 2048（不会只剩思考内容）",
      CAPTURED[-1]["max_tokens"] >= 2048, str(CAPTURED[-1].get("max_tokens")))
check("连通测试 verdict=ok", tr.get("verdict") == "ok", str(tr))

# ===================== 3. 配置落盘 =====================
print("== 3. ai_config：归一化 / 落盘 / 回显 ==")
os.makedirs(OUT_DIR, exist_ok=True)
check("normalize_reasoning_effort('low') == 'low'",
      ai_config.normalize_reasoning_effort("low") == "low")
check("normalize_reasoning_effort('LOW') == 'low'（大小写归一）",
      ai_config.normalize_reasoning_effort("LOW") == "low")
check("normalize_reasoning_effort('turbo') == ''（非法值清空）",
      ai_config.normalize_reasoning_effort("turbo") == "")
check("normalize_reasoning_effort(None) == ''",
      ai_config.normalize_reasoning_effort(None) == "")
check("normalize_reasoning_effort('  high ') == 'high'（去空格）",
      ai_config.normalize_reasoning_effort("  high ") == "high")

if os.path.exists(TMP_CFG):
    os.remove(TMP_CFG)
ai_config.save_module(TMP_CFG, "text", base_url="http://probe.invalid/v1", model="glm-5.3-flash",
                      reasoning_effort="low")
raw = json.loads(open(TMP_CFG, encoding="utf-8").read())
check("reasoning_effort 写进了 json",
      raw["modules"]["text"].get("reasoning_effort") == "low",
      json.dumps(raw["modules"]["text"], ensure_ascii=False))
check("json 里不落明文密钥",
      raw["modules"]["text"].get("api_key") == "",
      json.dumps(raw["modules"]["text"], ensure_ascii=False))

ai_config.save_module(TMP_CFG, "text", base_url="http://probe.invalid/v1", model="glm-5.3-flash",
                      reasoning_effort="turbo")
cfg = ai_config.load_config(TMP_CFG, None)
check("非法值经 save_module 后被清空为 ''",
      cfg["modules"]["text"].get("reasoning_effort") == "",
      json.dumps(cfg["modules"]["text"], ensure_ascii=False))
check("save_module 不传 reasoning_effort 时不改动原值（None = 保持）",
      cfg["modules"]["text"].get("reasoning_effort") == "")

ai_config.save_module(TMP_CFG, "text", base_url="http://probe.invalid/v1", model="glm-5.3-flash",
                      reasoning_effort="high")
cfg = ai_config.load_config(TMP_CFG, None)
check("改为 high 生效", cfg["modules"]["text"].get("reasoning_effort") == "high")
ai_config.save_module(TMP_CFG, "text", base_url="http://probe.invalid/v1", model="glm-5.3-flash")
cfg = ai_config.load_config(TMP_CFG, None)
check("再次保存其他字段时 high 不被清掉（不会被默认值覆盖）",
      cfg["modules"]["text"].get("reasoning_effort") == "high",
      json.dumps(cfg["modules"]["text"], ensure_ascii=False))

view = ai_config.module_public_view(ai_config.get_module(cfg, "text"))
check("module_public_view 回显 reasoning_effort",
      view.get("reasoning_effort") == "high", json.dumps(view, ensure_ascii=False))
check("module_public_view 仍不含明文密钥", "api_key" not in view, json.dumps(view, ensure_ascii=False))

pv = ai_config.public_view(cfg)
check("public_view 下发 reasoning_effort_options",
      pv.get("reasoning_effort_options") == ["", "off", "low", "high", "max"],
      str(pv.get("reasoning_effort_options")))

# 旧配置（没有该字段）必须能被读，且归一化成 ''
legacy_shaped = {"version": 1, "modules": {"text": {"base_url": "http://x/v1", "model": "m"},
                                           "qc": {}, "chat": {}}}
open(TMP_CFG, "w", encoding="utf-8").write(json.dumps(legacy_shaped))
cfg2 = ai_config.load_config(TMP_CFG, None)
check("旧配置缺该字段时归一化为 ''（不炸）",
      cfg2["modules"]["text"].get("reasoning_effort") == "",
      json.dumps(cfg2["modules"]["text"], ensure_ascii=False))

# ===================== 4. 端到端：配置 → 客户端 =====================
print("== 4. app._ai_client_for_module 必须透传档位（漏传 = 测试与保存行为不一致） ==")
import app as appmod  # noqa: E402

c = appmod._ai_client_for_module("text", base_url="http://probe.invalid/v1",
                                 api_key="sk-test", model="glm-5.3-flash",
                                 reasoning_effort="low")
check("显式传档位 → 客户端拿到 low", c.reasoning_effort == "low", repr(c.reasoning_effort))

c2 = appmod._ai_client_for_module("text", base_url="http://probe.invalid/v1",
                                  api_key="sk-test", model="glm-5.3-flash")
saved_re = appmod.ai_config.get_module(
    appmod.ai_config.load_config(appmod.AI_CONFIG_PATH, appmod.LLM_CONFIG_PATH), "text"
).get("reasoning_effort") or ""
check("不传档位 → 沿用已保存值（当前项目真实配置）",
      c2.reasoning_effort == saved_re, f"{c2.reasoning_effort!r} vs {saved_re!r}")

# /api/ai/test 路由里 ep 的构造（此处只验证字段进得去，不真的发请求）
src = open(os.path.join(ROOT, "app", "app.py"), encoding="utf-8").read()
# 2026-10-09：路由已拆到 app/routes/*.py，源码断言改读双域，
# 并把蓝图装饰器口径统一回 @app.route，保持原断言语义。
_rdir = os.path.join(ROOT, 'app', 'routes')
for _f in sorted(os.listdir(_rdir)):
    if _f.endswith('.py'):
        src += '\n' + open(os.path.join(_rdir, _f), encoding='utf-8').read()
for _bp in ['agent_bp', 'ai_bp', 'analytics_bp', 'assets_bp', 'autopilot_bp', 'caption_verify_bp', 'characters_bp', 'consistency_bp', 'continuity_bp', 'episodes_bp', 'export_bp', 'final_bp', 'generation_status_bp', 'keyframes_bp', 'llm_bp', 'memory_bp', 'novels_bp', 'plugin_bp', 'projects_bp', 'prompts_bp', 'qc_bp', 'quality_bp', 'relations_bp', 'scenes_bp', 'script_quality_bp', 'scripts_bp', 'storyboards_bp', 'system_bp', 'tasks_bp', 'tts_bp', 'upscale_bp', 'videos_bp', 'watermark_bp']:
    src = src.replace('@' + _bp + '.route(', '@app.route(')
check("app.py 的 /api/ai/test 构造 ep 时带 reasoning_effort",
      '"reasoning_effort": str(_re).strip()' in src)
check("app.py 的保存路由接收 reasoning_effort",
      'data.get("reasoning_effort")' in src)

# ===================== 5. 前端契约 =====================
print("== 5. 前端契约（只读源码，不做构建） ==")
fe_client = open(os.path.join(ROOT, "frontend", "src", "api", "client.ts"), encoding="utf-8").read()
fe_types = open(os.path.join(ROOT, "frontend", "src", "types", "index.ts"), encoding="utf-8").read()
fe_page = open(os.path.join(ROOT, "frontend", "src", "pages", "AIVaultPage.tsx"), encoding="utf-8").read()
check("client.ts 的 aiConfigApi.save 带 reasoning_effort", "reasoning_effort" in fe_client)
check("client.ts 的 aiConfigApi.test 带 reasoning_effort",
      "reasoning_effort" in fe_client.split("test:")[-1])
check("types 里 AIConfigModule 声明 reasoning_effort",
      "reasoning_effort?: string" in fe_types)
check("AIVaultPage 渲染档位下拉",
      ("REASONING_EFFORT_LABEL_KEYS" in fe_page or "FALLBACK_REASONING_OPTIONS" in fe_page)
      and "reasoning_effort" in fe_page)

# ===================== 收尾 =====================
if os.path.exists(TMP_CFG):
    os.remove(TMP_CFG)

print()
print(f"总计 {len(PASS) + len(FAIL)} 项：PASS {len(PASS)} / FAIL {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过 ✅")
