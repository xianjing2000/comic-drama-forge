# -*- coding: utf-8 -*-
"""系统配置与引擎状态 API 蓝图（2026-10-11 按域搬迁，三步法）。

## 本批为什么选它们
app.py 剩 43 个路由，其中 38 个是 API。这 3 个接口**零内部依赖** ——
只用 config_doctor / config_center / comfyui_client / comfyui_locator 与 flask 原语，
不触碰 app.py 里的任何 worker 或私有助手，因此是最安全的搬迁起点（示范用）。

## 搬迁纪律（沿用既有三步法）
  · URL 规则与响应体**一字不改**，只把 @app.route 换成 @config_api_bp.route；
  · 共享助手一律从 routes/_shared.py 取（本批不需要）；
  · app.py 里保留 register_blueprint，既有 import 表面零改动。
"""
from flask import Blueprint, jsonify, request

config_api_bp = Blueprint('config_api', __name__)


@config_api_bp.route('/api/config/doctor', methods=['GET'])
def api_config_doctor():
    """配置体检（2026-10-10，用户要求「别老是改了一个地方另一个地方没改导致冲突」）。

    只读接口：扫描三类冲突并返回结构化报告 ——
      ① **参数登记 vs 实际值**：config_doctor.REGISTRY 里登记的可调参数
         （阈值/轮数/开关/上限）是否与代码实际值一致；
      ② **跨模块重复定义**：同一语义在两处定义（如 COVERAGE_MAX_ROUNDS 同时在
         continuity 与 novel_to_script）是否同值；
      ③ **提示词双副本**：app/prompts/*.txt（外部，优先）与 prompt_templates 的
         _DEFAULT_*（代码兜底）是否逐字一致 —— 这是历史上「改了外部忘了兜底」
         造成行为分叉的源头。

    本接口不修改任何配置，也不改变取值路径，纯粹用于暴露问题。
    """
    try:
        import config_doctor as _cd  # noqa: PLC0415
        rep = _cd.doctor()
        rep['success'] = True
        return jsonify(rep)
    except Exception as e:  # noqa: BLE001  体检接口永不 5xx
        return jsonify({'success': False, 'error': str(e)[:300]}), 200


@config_api_bp.route('/api/config/settings', methods=['GET', 'POST'])
def api_config_settings():
    """统一配置中心（2026-10-10，第二步：持久化到数据库）。

    GET  → 返回全部可调参数的生效值、来源（db/env/default）、默认值与说明，按分组组织。
    POST → 写入单个参数（JSON: {key, value, note?}），写库后**立即回写运行时**，
           全局生效，无需重启。

    设计：参数唯一登记表是 config_doctor.REGISTRY；只有登记过的键才允许修改，
    未登记键与非法值一律拒绝（防止脏数据把服务搞崩）。
    取值优先级：数据库 > 环境变量 > 代码默认值。
    """
    try:
        import config_center as _cc  # noqa: PLC0415
        if request.method == 'GET':
            return jsonify(_cc.snapshot())
        data = request.get_json(silent=True) or {}
        key = str(data.get('key') or '').strip()
        if not key:
            return jsonify({'ok': False, 'error': '缺少 key'}), 200
        r = _cc.set_value(key, data.get('value'), note=str(data.get('note') or ''))
        return jsonify(r), 200
    except Exception as e:  # noqa: BLE001  配置接口永不 5xx
        return jsonify({'ok': False, 'error': str(e)[:300]}), 200


@config_api_bp.route('/api/engine/state', methods=['GET'])
def api_engine_state():
    """生成引擎（ComfyUI）健康与自愈状态（2026-10-09）。

    配合 comfyui_client 的**主动心跳**：前端/运维可直接看到
    「引擎是否在线、连续失败几次、10 分钟内重启了几次、上次错误是什么」。

    只读：本接口不触发任何探测或重启，状态由后台心跳线程维护。
    """
    try:
        # 2026-10-09：app.py 里 comfyui_client 这个名字是**实例**
        # （comfyui_client = ComfyUIClient()），而 get_engine_state 是**模块级**函数 ——
        # 用实例取会 AttributeError（表现为接口全 None）。必须从模块取。
        import comfyui_client as _cc  # noqa: PLC0415
        st = _cc.get_engine_state()
    except Exception as e:  # noqa: BLE001  状态接口永不 5xx
        return jsonify({"success": False, "error": str(e)}), 200
    # 附带「ComfyUI 安装目录检测」结果（2026-10-10）：用户安装位置可能与开发机不同，
    # 这里让前端/运维直接看到「检测到哪个目录、依据是什么、启动脚本在哪」。
    # ?refresh=1 忽略缓存重新检测（排查「为什么没找到 / 找错了」用）。
    try:
        import comfyui_locator as _cl  # noqa: PLC0415
        if request.args.get("refresh"):
            _cl.find_comfyui_dir(refresh=True)
        st["comfyui_dir"] = _cl.describe()
    except Exception as e:  # noqa: BLE001
        st["comfyui_dir"] = {"found": False, "error": str(e)[:200]}
    st["success"] = True
    return jsonify(st)
