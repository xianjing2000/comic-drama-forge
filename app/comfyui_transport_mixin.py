# -*- coding: utf-8 -*-
"""ComfyUIClient 的「_TransportMixin」方法（D-3 拆类，2026-10-16）。

从 app/comfyui_client.py **原样搬来**（零逻辑改动），以 mixin 形式被 ComfyUIClient 继承；
本模块 import config/comfyui_shared（叶子），**不 import comfyui_client**（避免成环）。
"""
import os
import re
import json
import time
import copy
import shutil
import random
import hashlib
import logging
import threading
import requests
import cancellation  # S9：远端任务取消（中止信号贯穿 ComfyUI 轮询，与 pipeline/llm_client 同一套）
import comfyui_job_store as job_store  # 崩溃免重渲检查点（2026-09-29：台账复用 + 重连，不重复提交）
import asset_library  # 跨项目角色资产库（2026-09-29：形象指纹命中即零渲染复用）
from typing import Dict, List, Optional, Any, Tuple, Sequence
# ⚠️ Sequence 曾被漏导入：类级注解 `_LIGHT_KEYWORDS: Sequence[...]` 在类创建时**不求值**，
# 所以模块照常导入、py_compile 也通过，但一旦有工具读取
# `ComfyUIClient.__annotations__` 或调用 `typing.get_type_hints()` 就会抛
# NameError: name 'Sequence' is not defined（实测）。别删这个导入。

from config import (
    COMFYUI_URL, COMFYUI_OUTPUT_DIR, COMFYUI_TEMP_DIR, MJSCXT_COMFYUI_DIR,
    resolve_workflow_path,
    PROJECT_OUTPUT_DIR, WORKFLOW_TEMPLATE, MULTIVIEW_CONFIG,
    SCENE_VIEW_ANGLE_ZH, SCENE_VIEW_KEYS, SCENE_VIEW_LABELS,
    H3_EMIT_AUDIO,
    CONFLICT_NEGATIVE_TOKENS,
    ENABLE_BLOCKING_ANNOTATION,
    H3_ENABLE_REFINE,
    H3_COMMON_NOTE_MODE,
    COMFYUI_INPUT_DIR,
    # 景别唯一权威表（分镜构图规范 / 解析顺序都必须由它派生，见 SHOT_CAMERA_SPECS 上方注释）
    SHOT_TYPES,
)
# 模板路径一律走 resolve_workflow_path()（项目内优先，回落 ComfyUI 目录）。
# COMFYUI_WORKFLOWS_DIR 在本模块内已不再直接使用，但**必须保留为模块属性**：
# .workbuddy/test 下的守卫（如 verify_style_injection.py）会通过
# `comfyui_client.COMFYUI_WORKFLOWS_DIR` 取用，删掉会让那些守卫 AttributeError。
from config import COMFYUI_WORKFLOWS_DIR  # noqa: F401
from dialogue_utils import (dialogue_text as _dlg_text, format_line as _dlg_line,
                            dialogue_speaker as _dlg_speaker)
from h3_episode_builder import H3EpisodeBuilder
import h3_director_builder
import comfyui_models
import style_kit
import h3_prompt_kit
# 提示词模板中心（2026-10-07）：九宫格「逐格写死」中文版主模板（storyboard_grid_main）从
# 这里加载（该模块只依赖标准库 + 延迟 import config，与本模块无循环依赖）。
import prompt_templates

from comfyui_shared import (
    ANNOTATED_DIR, INPUT_LOAD_CLASSES, OUTPUT_LOAD_CLASSES, SUFFIX_RE, _bump, _env_int,
)

logger = logging.getLogger(__name__)


class _TransportMixin:
    def comfy_online(self, timeout: int = 3) -> bool:
        """快速探测 ComfyUI 是否在线（/system/status，短超时）。"""
        try:
            self.get_status(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            return False


    def restart_comfyui(self, wait_sec: int = 120, poll_sec: float = 2.0) -> bool:
        """重启 ComfyUI（杀现有 python 进程 + 重新拉起 run_nvidia_gpu_fixed.bat + 等 /system/status 就绪）。

        全程 fail-open：任何环节失败都返回 False，不抛异常。
        wait_sec：等 ComfyUI 就绪的总超时（默认 120s，便携版冷启动约 60-90s）。

        ⚠️ 重启会清掉 ComfyUI 侧未完成的队列任务——调用方应在「确认当前任务已超时/失败」后才调用，
        不要在正常出图中途调（会杀掉正在跑的图）。
        """
        import subprocess
        comfy_dir = MJSCXT_COMFYUI_DIR
        bat = os.path.join(comfy_dir, "run_nvidia_gpu_fixed.bat")
        if not os.path.isfile(bat):
            logger.warning("ComfyUI 重启失败：找不到启动脚本 %s（请设 MJSCXT_COMFYUI_DIR）", bat)
            return False
        # ① 杀掉现有 ComfyUI python 进程
        # ⚠️⚠️ 2026-10-09 严重缺陷修复：原实现是
        #       subprocess.run(["taskkill", "/F", "/IM", "python.exe"], …)
        #     —— 它会杀掉**机器上全部 python.exe**，包括**本服务自己**
        #    （后端的 serve.py 也是 python.exe）。实跑复现：心跳探测到 ComfyUI 掉线
        #    → 调本函数 → 后端被自己杀死 → 5211 与 8188 同时消失、生产中断。
        #    现在改为**只杀命令行里含 ComfyUI main.py 的进程**，并在日志里记录杀掉了谁；
        #    匹配不到时**不杀任何进程**（宁可重启失败，也不能误杀后端）。
        killed = []
        try:
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*main.py*' -and "
                "$_.CommandLine -like '*ComfyUI*' } | "
                "ForEach-Object { Write-Output $_.ProcessId; "
                "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                                  capture_output=True, timeout=20, check=False,
                                  text=True, encoding="utf-8", errors="replace")
            killed = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip().isdigit()]
            if killed:
                logger.info("ComfyUI 重启：已终止 ComfyUI 进程 %s", killed)
            else:
                logger.info("ComfyUI 重启：未发现运行中的 ComfyUI 进程（不杀任何进程）")
        except Exception as e:  # noqa: BLE001
            logger.debug("终止 ComfyUI 进程时忽略：%s", e)
        # ①.5 等旧进程**真正退出**再拉新进程
        #   ⚠️ 2026-10-10 实测缺陷：此前 ① 杀掉后**立即** Popen —— 而 Stop-Process 是
        #   异步的，旧进程可能还要几百毫秒~数秒才释放 8188，新进程于是报
        #   "Port 8188 is already in use on address 127.0.0.1"（stderr_crash.log:311）
        #   然后**自行退出** → 表面「重启成功」实际一个 ComfyUI 都没有；
        #   更糟的是若旧进程只是被判定「忙/慢」而未真死，就会出现两个实例同时抢 8G 显存。
        if killed:
            _wait_t0 = time.time()
            _deadline = _wait_t0 + 25
            while time.time() < _deadline:
                try:
                    _chk = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                         "Where-Object { $_.CommandLine -like '*main.py*' -and "
                         "$_.CommandLine -like '*ComfyUI*' } | Measure-Object | "
                         "-ExpandProperty Count"],
                        capture_output=True, timeout=15, check=False,
                        text=True, encoding="utf-8", errors="replace")
                    _left = int((_chk.stdout or "0").strip() or 0)
                except Exception:  # noqa: BLE001
                    _left = 0
                if _left == 0:
                    logger.info("ComfyUI 重启：旧进程已全部退出（等待 %.1fs）",
                                time.time() - _wait_t0)
                    break
                time.sleep(1.0)
            else:
                logger.warning("ComfyUI 重启：等待 25s 后仍检测到未退出的 ComfyUI 进程，"
                               "仍尝试拉起（可能出现端口冲突，请关注日志）")
        # ② 重新拉起（后台，不阻塞）
        try:
            subprocess.Popen(["cmd.exe", "/c", bat], cwd=comfy_dir,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            logger.info("ComfyUI 重启：已拉起 %s", bat)
        except Exception as e:  # noqa: BLE001
            logger.warning("ComfyUI 重启：拉起失败：%s", e)
            return False
        # ③ 等 /system/status 就绪
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            time.sleep(poll_sec)
            if self.comfy_online(timeout=3):
                logger.info("ComfyUI 重启：已就绪（耗时 %.0fs）", time.time() - t0)
                return True
        logger.warning("ComfyUI 重启：%ds 内未就绪（仍离线）", wait_sec)
        return False


    def wait_comfy_online(self, wait_sec: int = 120, poll_sec: float = 2.0) -> bool:
        """只等 ComfyUI 就绪（不重启）。ComfyUI 自愈/冷启动后等待可用。fail-open。"""
        t0 = time.time()
        while time.time() - t0 < wait_sec:
            time.sleep(poll_sec)
            if self.comfy_online(timeout=3):
                return True
        return False


    def _generate_client_id(self) -> str:
        import uuid
        return str(uuid.uuid4())


    def _get(self, path: str, timeout: int = 30) -> Any:
        resp = requests.get(f"{self.base_url}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()


    @staticmethod
    def _http_error_detail(resp) -> str:
        """把 ComfyUI 的错误响应体压成一行可读文本。

        ⚠️ 2026-10-01 全流程实测补：`/prompt` 的 400 **不是网络抖动，而是工作流校验失败** ——
        响应体里带着 `error.type / error.message` 和 `node_errors`（哪个节点、哪个输入不合法）。
        原实现直接 `raise_for_status()`，异常里只剩
        "400 Client Error: Bad Request for url: …"，**真正的原因被丢掉**：
        整集视频挂掉时日志里完全看不出为什么，上层还在无意义地「换种子重试 6 次」
        （校验错误换种子永远不可能成功）。所以这里把响应体解析出来带上。
        """
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            try:
                return (resp.text or "").strip()[:600]
            except Exception:  # noqa: BLE001
                return ""
        if not isinstance(body, dict):
            return str(body)[:600]
        bits = []
        err = body.get("error")
        if isinstance(err, dict):
            bits.append(f"{err.get('type') or 'error'}: {err.get('message') or ''}".strip(": "))
        elif err:
            bits.append(str(err))
        node_errors = body.get("node_errors")
        if isinstance(node_errors, dict):
            for nid, info in list(node_errors.items())[:6]:
                if not isinstance(info, dict):
                    bits.append(f"节点 {nid}: {info}")
                    continue
                msgs = []
                for e in (info.get("errors") or []):
                    if not isinstance(e, dict):
                        continue
                    extra = e.get("extra_info") if isinstance(e.get("extra_info"), dict) else {}
                    where = extra.get("input_name") or extra.get("node_id") or ""
                    msgs.append(f"{e.get('message') or e.get('type') or '不合法'}"
                                + (f"[输入 {where}]" if where else ""))
                bits.append(f"节点 {nid}({info.get('class_type') or '?'}): "
                            + ("; ".join(msgs) or str(info)[:120]))
        return " | ".join(x for x in bits if x)[:900]


    def _post(self, path: str, data: dict = None) -> Any:
        resp = requests.post(f"{self.base_url}{path}", json=data or {}, timeout=300)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"ComfyUI POST {path} 失败：HTTP {resp.status_code}"
                + (f" —— {self._http_error_detail(resp)}" if self._http_error_detail(resp) else ""))
        return resp.json()


    def get_status(self, timeout: int = 3) -> dict:
        """探测 ComfyUI 在线状态。

        注意：这是高频调用的状态接口（前端每次刷新都会打），
        因此**必须用短超时**——默认 3 秒。用 requests 的 (connect, read) 元组形式，
        保证「主机不可达时快速失败」而不是干等 30 秒（Windows 上防火墙丢包会更久）。
        """
        try:
            resp = requests.get(f"{self.base_url}/system_stats",
                                timeout=(min(2, timeout), timeout))
            resp.raise_for_status()
            return {"status": "online", "stats": resp.json()}
        except Exception as e:
            return {"status": "offline", "error": str(e)}


    def get_object_info(self, force: bool = False, ttl: int = 300) -> dict:
        """获取 /object_info（带缓存，离线时返回空 dict，不影响主流程）"""
        now = time.time()
        if not force and self._object_info is not None and (now - self._object_info_ts) < ttl:
            return self._object_info
        try:
            self._object_info = self._get("/object_info", timeout=60)
            self._object_info_ts = now
        except Exception as e:
            logger.warning(f"获取 object_info 失败（将跳过严格校验）: {e}")
            if self._object_info is None:
                self._object_info = {}
        return self._object_info


    def declared_inputs(self, class_type: str) -> List[str]:
        """按 object_info 声明顺序返回节点可声明的输入名（required 在前，optional 在后）"""
        oi = self.get_object_info().get(class_type) or {}
        spec = oi.get("input") or {}
        names = list((spec.get("required") or {}).keys()) + list((spec.get("optional") or {}).keys())
        return names


    def upload_image(self, image_path: str, name: str = None,
                     subfolder: str = "", image_type: str = "input") -> str:
        """上传图片到 ComfyUI（input / output / temp 目录）

        ⚠️ ComfyUI 的 /upload/image 只接受**裸文件名**：
        `name` 里带 "/" 而 subfolder 为空时，服务端会拿这个相对路径去 join 一个
        不存在的目录 → 直接 **500 "Server got itself in trouble"**（实测复现）。
        而调用方习惯把「项目/类型/资产」目录一起塞进 name（见 generate_multiview
        的 filename_prefix），于是「资产基础图上传」100% 500，多视角链路整条断掉。
        这里统一做归一化：name 里的目录部分挪到 subfolder 参数，name 只留文件名。
        返回格式不变（"子目录/文件名"），下游 LoadImageOutput 的 [output] 标注照旧。
        """
        url = f"{self.base_url}/upload/image"
        if name is None:
            name = os.path.basename(image_path)
        # 归一化：反斜杠统一；按 "/" 切段（丢弃空段，兼容首/尾/重复斜杠）；
        # 末段是文件名，前面所有段是目录 → 目录进 subfolder，name 只留裸文件名。
        # 末尾带 "/" 视为「只给了目录」→ 文件名回落本地图片的 basename
        # （否则 "d/" 会被当成名为 "d" 的无扩展名文件静默传上去）。
        raw = str(name).replace("\\", "/")
        parts = [p for p in raw.split("/") if p]
        if parts and raw.endswith("/"):
            head, tail = "/".join(parts), os.path.basename(image_path)
        elif len(parts) > 1:
            head, tail = "/".join(parts[:-1]), parts[-1]
        elif parts:
            head, tail = "", parts[0]
        else:
            head, tail = "", os.path.basename(image_path)
        name = tail
        if head:
            subfolder = "/".join(p for p in (str(subfolder).strip("/"), head) if p)
        with open(image_path, "rb") as f:
            files = {"image": (name, f, "image/png")}
            data = {"overwrite": "true", "type": image_type}
            if subfolder:
                data["subfolder"] = subfolder
            resp = requests.post(url, files=files, data=data, timeout=120)
            resp.raise_for_status()
            result = resp.json()
            uploaded = result.get("name", name)
            sub = result.get("subfolder") or ""
            return f"{sub}/{uploaded}" if sub else uploaded


    def queue_prompt(self, api_prompt: dict) -> str:
        # TE_MAN 提示词增强节点运行时注入（2026-10-06）：所有图片提交路径在此收口，
        # 预检/风格清理等文本工序都已完成，注入的是最终文本；条件不满足时原样返回
        # （判定链与 fail-open 口径见 _inject_prompt_enhancer）。
        api_prompt = self._inject_prompt_enhancer(api_prompt)
        # 提交前「模型名对齐」（2026-10-08）：模板 / 代码默认值 / 用户选定里写死的模型名
        # 会因目录改名而失效（实测 VAELoader 86 的 vae_name、增强节点的 mmproj 各炸一次：
        # /prompt 400 → 整集视频被丢弃）。这里用 object_info（ComfyUI 自己校验用的权威
        # 列表）把模型名改写成当前布局下的真实地址；匹配不上只告警、不阻断（fail-open）。
        try:
            import comfyui_models  # 函数内导入：与本模块无环依赖，也规避导入顺序问题
            comfyui_models.align_prompt_models(
                api_prompt, object_info=self.get_object_info(), tag="提交前")
        except Exception as _ma_err:  # noqa: BLE001
            logger.warning("模型名对齐调用失败（按原样提交）：%s", _ma_err)
        payload = {"prompt": api_prompt, "client_id": self.client_id}
        try:
            result = self._post("/prompt", payload)
        except Exception:
            _bump("prompt_failed")
            raise
        if "error" in result:
            _bump("prompt_failed")
            raise RuntimeError(f"ComfyUI 队列错误: {result['error']}")
        _bump("prompt_submitted")
        return result.get("prompt_id", "")


    def get_history(self, prompt_id: str) -> dict:
        return self._get(f"/history/{prompt_id}")


    def clear_history(self) -> bool:
        """清空 ComfyUI **任务历史列表**（``POST /history {"clear":true}``）。

        为什么要清：ComfyUI 界面的「任务历史」面板**只增不减**，而质检每失败一次
        重跑就多一条记录 —— 跑几轮下来面板里几百条，容易被误读成「生成了大量废图」。
        实测某项目面板 162 条时，磁盘上真正残留的废弃分镜图 **0 张**。

        ⚠️ 与磁盘产物完全无关：只清内存/磁盘上的 `history` 记录，不删任何 output 文件，
        也不影响**正在执行或排队中**的任务（它们结束后会各自追加新记录）。

        永不抛异常：失败只 warning（清历史是「可观测性优化」，不能拖垮生产）。
        """
        try:
            self._post("/history", {"clear": True})
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI 任务历史清理失败（不影响生产）: {e}")
            return False


    def interrupt(self, prompt_id: str = None) -> None:
        """S9：向 ComfyUI 发 /interrupt，打断当前正在出队的任务。

        - `prompt_id` 为 None → 打断队列中**正在执行**的那个（ComfyUI 官方语义）；
        - 为具体 prompt_id → 仅当它仍在队列/执行中时才有效（配合 `delete_queued` 精准清理）。
        失败静默（网络抖了也不应让取消路径本身抛错拖垮上层）。
        """
        try:
            if prompt_id is None:
                self._post("/interrupt")
            else:
                self._post("/interrupt")
                self.delete_queued(prompt_id)
            logger.info(f"已请求 ComfyUI 打断远端任务: {prompt_id or '(当前出队)'}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI /interrupt 失败（不影响取消流程）: {e}")


    def delete_queued(self, prompt_id: str) -> None:
        """S9：把指定 prompt 从队列中删除（ComfyUI `POST /queue {"delete":[id]}`）。"""
        try:
            self._post("/queue", {"delete": [prompt_id]})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"ComfyUI 删除队列项失败（不影响取消流程）: {prompt_id}: {e}")


    def queue_state(self) -> dict:
        """查询 ComfyUI 队列状态 → ``{"running": n, "pending": n, "ok": bool}``。

        用于**删除类操作前的安全检查**。`sb_ref_*` 参考图是本轮分镜上传到
        ComfyUI output 目录的**临时引用文件**，收尾清理时若队列里还压着任务，
        删文件会让那些任务执行到 ``LoadImage`` 时直接报 ``FileNotFoundError``。

        实测根因（用户 2026-09-29 日志）：某镜 ``wait_for_completion`` **超时返回**
        或批次被中止后，任务其实仍留在 ComfyUI 队列里；收尾照常清理 `sb_ref` →
        那些任务开始执行时文件已被移入回收站 → **连续 7 个镜头 0.01s 失败**，
        分镜图整批全灭。故清理前先问一句「还有人在用吗」。

        ``ok=False`` 表示查询本身失败（ComfyUI 不可达）——调用方应当**保守处理**：
        宁可跳过清理（残留有滚动回收兜底），也不要在信息不明时删掉可能正被引用的文件。
        """
        try:
            d = self._get("/queue") or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("查询 ComfyUI 队列失败: %s", e)
            return {"running": 0, "pending": 0, "ok": False}
        return {"running": len(d.get("queue_running") or []),
                "pending": len(d.get("queue_pending") or []),
                "ok": True}


    def _recheck_after_timeout(self, prompt_id: str) -> Tuple[str, dict]:
        """t18：到点复查远端，返回 ("done"|"error"|"running"|"gone", entry)。

        为什么需要：旧实现到点后**无条件** interrupt 并判失败 —— 而实测
        「单段 ≈ 17 分钟 × 10 段」的总耗时与旧公式给出的总超时**恰好重合**，
        于是每次都在最后一刻把「马上就好」的任务取消掉、重投：最后一段白跑、
        还要重新排队（段缓存虽被复用，但用户看到的现象就是「永远出不来 mp4」）。
        现在先复查：已完成 → 取回结果；仍在跑 → 交给调用点续等；都不在 → 真超时。
        """
        try:
            _h = self.get_history(prompt_id) or {}
            _e = _h.get(prompt_id) if isinstance(_h, dict) else None
        except Exception as _e1:  # noqa: BLE001
            logger.debug("到点复查 history 失败（按仍在跑处理）：%s", _e1)
            return "running", {}
        if isinstance(_e, dict):
            _st = _e.get("status") or {}
            if _st.get("completed") or _st.get("status_str") == "success":
                return "done", _e
            if _st.get("status_str") == "error":
                return "error", _e
        try:
            _q = self._get("/queue") or {}
            _qids = [x[1] for x in (_q.get("queue_running") or [])
                     if isinstance(x, (list, tuple)) and len(x) > 1]
            _qids += [x[1] for x in (_q.get("queue_pending") or [])
                      if isinstance(x, (list, tuple)) and len(x) > 1]
        except Exception as _e2:  # noqa: BLE001
            logger.debug("到点复查 /queue 失败（按仍在跑处理）：%s", _e2)
            return "running", {}
        return ("running" if prompt_id in _qids else "gone"), {}


    def wait_for_completion(self, prompt_id: str, timeout: int = 3600) -> dict:
        """轮询远端任务直到完成。

        S9 增强（不改变返回契约——超时仍返回 `{}`，避免 ripple 到 6 处调用方）：
          ① 轮询内检查 `cancellation.should_stop()`（contextvar，无注册时恒 False，
             不影响普通 API 调用路径）；一旦收到「暂停/停止」→ 立即 `interrupt` +
             抛 `cancellation.Cancelled`（穿透到 pipeline 归一为 cancelled），不再白烧 GPU；
          ② 轮询用 `cancellation.sleep`（可被打断的短休眠），点了暂停最多 0.25s 就有反应，
             而不是等满 3s；
          ③ 超时（非中止）后也 `interrupt` 一次，避免「本地判超时、远端继续跑」的双重浪费。
        ⚠️ 注意（审计 S9 备注）：cancellation 检查点**刻意不放进 ComfyUI 渲染循环**
        （会留半成品）——这里加的是「超时/取消后的远端清理」，两者不冲突。
        B-21 P1-13：三态分离（completed / error / timeout）+ interrupt 定向到指定 prompt_id。
        超时和 error 的 interrupt 都传 prompt_id（不再打断队列中正在执行的其他任务）。
        """
        start = time.time()
        # ⭐ t18：软超时 —— deadline 可在循环体内被「续等」推后（见循环体末尾的到点复查）。
        deadline = start + max(1, int(timeout))
        _ext_left = _env_int("MJSCXT_WAIT_EXTENSIONS", 3)
        _ext_sec = max(1, _env_int("MJSCXT_WAIT_EXTEND_SECONDS", 1800))
        # ⭐ 2026-10-08：存活检测计数器。旧实现只轮询 history —— 一旦 ComfyUI 重启、
        #    历史被清、或提交被丢，这个 prompt_id 就**永远不会出现在 history 里**，
        #    于是死等满 timeout（默认 3600s = 1 小时！实测整集卡死 6~25 分钟无日志、
        #    队列为空、GPU 空闲）。连续 _MISS_LIMIT 次（≈15s）既不在 history 也不在
        #    队列 → 判定远端已丢失，立即返回 {}（调用方按「没拿到结果」重试）。
        _miss = 0
        _MISS_LIMIT = 5
        while time.time() < deadline:
            # ⓪ 轮询顺手确保 temp 目录存在（exist_ok 幂等、零开销）——ComfyUI 侧节点
            # 仍可能在 temp 下建工作目录，此为通用防御。
            try:
                os.makedirs(COMFYUI_TEMP_DIR, exist_ok=True)
            except OSError:
                pass
            # ① 中止信号：点「暂停」后立刻打断远端并抛出，让上层转 cancelled 而非干等
            if cancellation.should_stop():
                logger.warning(f"等待期间收到中止信号，打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise cancellation.Cancelled(f"ComfyUI 远端等待期间收到中止信号：{prompt_id}")
            try:
                history = self.get_history(prompt_id)
                if prompt_id in history:
                    _miss = 0
                    entry = history[prompt_id]
                    status = entry.get("status", {}) or {}
                    if status.get("completed") or status.get("status_str") == "success":
                        _bump("completed")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        return entry
                    if status.get("status_str") == "error":
                        logger.error(f"生成出错: {status}")
                        _bump("waited_seconds", round(time.time() - start, 2))
                        # B-21 P1-13：error 态也定向 interrupt（清理本 prompt 的残留队列项）
                        self.interrupt(prompt_id)
                        return entry
                else:
                    # ⭐ 2026-10-08 存活检测：history 里没有 → 再看队列里在不在。
                    try:
                        _q = self._get("/queue") or {}
                        _qids = [x[1] for x in (_q.get("queue_running") or [])
                                 if isinstance(x, (list, tuple)) and len(x) > 1]
                        _qids += [x[1] for x in (_q.get("queue_pending") or [])
                                  if isinstance(x, (list, tuple)) and len(x) > 1]
                    except Exception:  # noqa: BLE001
                        _qids = [prompt_id]   # 查询失败 → 保守当作还在跑，绝不误杀
                    if prompt_id in _qids:
                        _miss = 0
                    else:
                        _miss += 1
                        if _miss >= _MISS_LIMIT:
                            logger.warning(
                                "等待中的任务在 ComfyUI 已不存在（history/queue 均无 %s，"
                                "连续 %d 次）→ 判定远端丢失，放弃等待交调用方重试",
                                prompt_id, _miss)
                            _bump("lost", 1)
                            _bump("waited_seconds", round(time.time() - start, 2))
                            return {}
            except cancellation.Cancelled:
                raise  # 中止信号必须穿透，不能被轮询的通用 except 吞掉
            except Exception as e:
                logger.debug(f"轮询历史失败: {e}")
            # ② 可被打断的短休眠（3s 轮询间隔），暂停时最多 0.25s 即有反应。
            # ⚠️ 关键：`cancellation.sleep` 在收到中止信号时**直接抛 Cancelled**，
            # 会绕过循环顶部的 `should_stop()` 分支 —— 也就是说「点暂停」时
            # **不会执行 self.interrupt()，ComfyUI 上的任务会继续白跑**
            # （用户反馈「暂停要同步停止 comfyui 的任务」的直接原因）。
            # 因此这里自己捕获并补上远端中断，再原样抛出。
            try:
                cancellation.sleep(3)
            except cancellation.Cancelled:
                logger.warning(f"退避期间收到中止信号，主动打断远端任务 {prompt_id}")
                self.interrupt(prompt_id)
                raise
            # ---- ⭐ t18：到点复查（软超时续等）----
            #  旧实现到点后**无条件** interrupt + 判失败：而「17min/段 × 10 段」的实测
            #  总耗时与旧公式给出的超时**恰好重合** → 每次都在最后一刻取消、重投，
            #  最后一段白跑并重新排队。这里先复查：已完成 → 直接取回；仍在跑 → 续等。
            if _ext_left > 0 and time.time() >= deadline:
                _st18, _e18 = self._recheck_after_timeout(prompt_id)
                if _st18 == "done" and isinstance(_e18, dict):
                    _bump("completed", 1)
                    _bump("waited_seconds", round(time.time() - start, 2))
                    logger.info("[等待续等] %s 在到点时刻已完成 → 取回结果（远端未取消）",
                                prompt_id)
                    return _e18
                if _st18 == "running":
                    _ext_left -= 1
                    deadline = time.time() + _ext_sec
                    logger.warning(
                        "[等待续等] %s 已等 %ds 仍未完成，但远端**仍在执行** → 续等 %ds"
                        "（剩余 %d 次）—— 绝不取消「刚好差一点」的任务（那是白烧最后一段）",
                        prompt_id, int(time.time() - start), _ext_sec, _ext_left)
        # ③ 超时（非中止）：仍清理远端，避免本地判超时而远端白跑
        # B-21 P1-13：超时 interrupt 定向到指定 prompt_id（不再误伤队列中其他任务）
        logger.warning(f"等待超时: {prompt_id}（清理远端队列）")
        self.interrupt(prompt_id)
        _bump("waited_seconds", round(time.time() - start, 2))
        return {}


    def _stage_from_library(self, lib_files: List[str],
                            filename_prefix: str = None) -> List[str]:
        """把库里的产物**复制**到 ComfyUI output 目录，返回调用方期望的绝对路径。

        为什么要复制、而不是直接返回库路径：调用方拿到产物后会 shutil.move 走
        （见 app.py 资产链路：move 到 scratch 暂存区再质检）。若直接返回库路径，
        **第一次复用就会把库条目移走**、库当场失效 —— 复制一份出去，库保持只读语义。
        """
        subdir, stem = "comic_drama", "asset"
        if filename_prefix:
            norm = str(filename_prefix).replace(os.sep, "/").replace("\\", "/").strip("/")
            parts = [p for p in norm.split("/") if p]
            if len(parts) > 1:
                subdir, stem = "/".join(parts[:-1]), parts[-1]
            elif parts:
                stem = parts[0]
        out_dir = os.path.join(COMFYUI_OUTPUT_DIR, *subdir.split("/"))
        os.makedirs(out_dir, exist_ok=True)
        staged: List[str] = []
        for i, src in enumerate(lib_files):
            ext = os.path.splitext(src)[1] or ".png"
            dst = os.path.join(out_dir, "%s_reuse_%02d%s" % (stem, i + 1, ext))
            shutil.copy2(src, dst)
            staged.append(dst)
        return staged


    def _resume_history(self, outputs: List[str]) -> dict:
        """把台账里存的**产物绝对路径**还原成 get_output_files 认得的 history 结构。

        为什么要还原而不是另开一条返回通道：get_output_files 是按
        COMFYUI_OUTPUT_DIR + subfolder + filename 解析的，只要把绝对路径反推回
        «子目录 + 文件名»，复用路径就能**零改动**地穿过现有 6 处调用方 ——
        这也是本功能刻意选择「返回 history」而不是「返回文件表」的原因。

        不在 ComfyUI output 目录下的产物（人为挪过位置）无法用 history 语义表达，
        直接跳过 → 该文件不计入复用集合，调用方按「没拿到文件」正常处理。
        """
        outs: Dict[str, Any] = {}
        for i, p in enumerate(outputs or []):
            try:
                rel = os.path.relpath(str(p), COMFYUI_OUTPUT_DIR)
            except (ValueError, TypeError):
                continue
            if rel.startswith(".."):
                logger.warning("免重渲：产物不在 ComfyUI output 目录下，跳过复用：%s", p)
                continue
            sub = os.path.dirname(rel).replace("\\", "/")
            outs["_resume%d" % i] = {"files": [{"filename": os.path.basename(rel),
                                                "subfolder": sub}]}
        return outs


    def submit_resumable(self, api_prompt: dict, *, job_key: str = "",
                         timeout: int = None, file_ext: str = "",
                         label: str = "") -> Tuple[dict, str, bool]:
        """提交并等待，**带崩溃免重渲检查点**。返回 (history, prompt_id, resumed)。

        三种分流（见 comfyui_job_store 模块文档）：

        * **已完成**：台账里的产物仍存在 → 构造 history 直接返回，resumed=True，零渲染；
        * **在跑中**：远端仍有该 prompt → wait_for_completion **重连**，不重复提交；
        * **不可复用**：无台账 / 哈希不符 / 产物已删 / 远端报错 → 正常提交（现状行为）。

        job_key 为空 → 完全等价于旧的 queue_prompt + wait_for_completion，
        这样未接入任务键的路径行为零变化。

        fail-open 口径：台账或远端 history 查询任何异常都只降级为「本次不复用」，
        绝不阻断生产 —— 缓存是加速器，不是依赖。
        """
        timeout = timeout or 3600
        if not job_key:
            pid = self.queue_prompt(api_prompt)
            return self.wait_for_completion(pid, timeout=timeout), pid, False

        wf_hash = job_store.workflow_hash(api_prompt)
        rec = job_store.find(job_key)
        if rec and wf_hash and rec.get("workflow_hash") == wf_hash and rec.get("prompt_id"):
            old_pid = rec["prompt_id"]
            # ① 台账产物仍在磁盘上 → 零渲染复用
            outs = [p for p in (rec.get("outputs") or []) if p]
            if outs and all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in outs):
                logger.info("[免重渲] %s 命中台账，复用 %d 个已完成产物（prompt=%s）",
                            label or job_key, len(outs), old_pid)
                return ({"outputs": self._resume_history(outs),
                         "status": {"completed": True, "status_str": "success"}},
                        old_pid, True)
            # ② 台账产物被清理 → 问远端 history：完成则复用，仍在跑则重连
            entry = None
            try:
                hist = self.get_history(old_pid) or {}
                entry = hist.get(old_pid) if isinstance(hist, dict) else None
            except Exception as e:                       # noqa: BLE001
                logger.debug("免重渲：远端 history 查询失败，按重渲处理：%s", e)
            if isinstance(entry, dict):
                st = entry.get("status") or {}
                if st.get("completed") or st.get("status_str") == "success":
                    got = self.get_output_files(entry, file_ext)
                    if got and all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in got):
                        job_store.mark_done(job_key, got)
                        logger.info("[免重渲] %s 远端已完成且产物有效，复用 %d 个（prompt=%s）",
                                    label or job_key, len(got), old_pid)
                        return entry, old_pid, True
                elif st.get("status_str") == "error":
                    job_store.mark_failed(job_key, "remote_error")
                    logger.info("[免重渲] %s 远端该任务已失败，重新提交", label or job_key)
                else:
                    logger.info("[免重渲] %s 远端仍在队列/执行中，**重连等待**（不重复提交，prompt=%s）",
                                label or job_key, old_pid)
                    hist2 = self.wait_for_completion(old_pid, timeout=timeout)
                    got2 = self.get_output_files(hist2, file_ext)
                    if got2:
                        job_store.mark_done(job_key, got2)
                    return hist2, old_pid, False

        pid = self.queue_prompt(api_prompt)
        try:
            job_store.remember(job_key, wf_hash, pid, label=label or job_key)
        except Exception as e:                           # noqa: BLE001
            logger.debug("免重渲：台账登记失败（不影响生成）：%s", e)
        history = self.wait_for_completion(pid, timeout=timeout)
        try:
            got = self.get_output_files(history, file_ext)
            if got:
                job_store.mark_done(job_key, got)
            else:
                job_store.mark_failed(job_key, "no_output")
        except Exception as e:                           # noqa: BLE001
            logger.debug("免重渲：台账收尾记录失败（不影响生成）：%s", e)
        return history, pid, False


    def get_output_files(self, history: dict, file_ext: str = "",
                         filename_prefix: str = "") -> List[str]:
        """从 ComfyUI history 收集产物文件路径。

        :param filename_prefix: 非空时**只**返回相对 output 目录的路径以该前缀开头的产物。
            为什么需要（对齐 KT-AI-Studio run_scene_merge 的前缀校验口径）：
            history 的 outputs 是**按节点 id 组织的字典**，一次提交里可能有多个保存节点；
            调用方通常取 files[0] 当结果。历史实现不过滤，一旦工作流里混入第二个 SaveImage
            （模板改造 / 增强节点回传 / 上游残留），就会把**别的节点或别的任务**的图当成结果
            返回 —— 静默串档，且事后无法从日志看出。显式传入前缀后，不符合的产物被丢弃并
            **告警**（宁可告警，不可静默）。
        """
        prefix = str(filename_prefix or "").replace("\\", "/").strip("/")
        files = []
        skipped = []
        for _node_id, node_output in (history.get("outputs") or {}).items():
            for _key, value in (node_output or {}).items():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and "filename" in item:
                        filename = item["filename"]
                        subfolder = item.get("subfolder", "")
                        base = os.path.join(COMFYUI_OUTPUT_DIR, subfolder) if subfolder else COMFYUI_OUTPUT_DIR
                        full = os.path.join(base, filename)
                        if file_ext and not filename.endswith(file_ext):
                            continue
                        if prefix:
                            rel = ("%s/%s" % (str(subfolder).replace("\\", "/").strip("/"), filename)).lstrip("/")
                            if not rel.startswith(prefix):
                                skipped.append(rel)
                                continue
                        files.append(full)
        if skipped:
            logger.warning("[产物回收] 前缀「%s」过滤掉 %d 个非本任务产物（例：%s）",
                           prefix, len(skipped), skipped[:3])
        return files


    @staticmethod
    def resolve_local_path(path_or_url: str) -> Optional[str]:
        """把前端传来的 HTTP 资源路径（/api/assets/...、/api/videos/...）解析为本地绝对路径"""
        if not path_or_url or not isinstance(path_or_url, str):
            return None
        p = path_or_url.strip()
        if p.startswith("http://") or p.startswith("https://"):
            # 去掉协议与主机，仅保留路径部分
            rest = p.split("//", 1)[-1]
            p = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        p = p.replace("\\", "/")
        if p.startswith("/api/assets/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "assets", p[len("/api/assets/"):]))
        if p.startswith("/api/videos/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "videos", p[len("/api/videos/"):]))
        if p.startswith("/api/final/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "final", p[len("/api/final/"):]))
        if p.startswith("/api/storyboards/file/"):
            return os.path.normpath(os.path.join(PROJECT_OUTPUT_DIR, "storyboards",
                                                 p[len("/api/storyboards/file/"):]))
        if os.path.exists(p):
            return os.path.normpath(p)
        return None


    @staticmethod
    def annotate_file_ref(filename: str, class_type: str, default_dir: str = ANNOTATED_DIR) -> str:
        """按「读取目录」语义为文件名型资源输入补/去目录标注。

        - Load*Output 系列（LoadImageOutput / LoadAudioOutput / ...）读 ComfyUI output 目录
          → 返回 "名字 [output]"
        - LoadImage / LoadAudio / LoadVideo 等读 input 目录 → 返回 "名字"（不带标注）
        - 其它类型 → 原样返回，不做任何加工（避免引入新错误）
        """
        if not filename or not isinstance(filename, str):
            return filename
        name = SUFFIX_RE.sub("", filename).strip()   # 先剥离可能已存在的标注，保证幂等
        if class_type in OUTPUT_LOAD_CLASSES or \
                (class_type.startswith("Load") and class_type.endswith("Output")):
            return f"{name} [{default_dir}]"
        if class_type in INPUT_LOAD_CLASSES or class_type.startswith("Load"):
            return name
        return filename


