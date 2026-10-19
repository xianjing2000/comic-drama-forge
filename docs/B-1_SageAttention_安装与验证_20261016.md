# B-1 落地说明：ComfyUI 启用 SageAttention（守卫式）

> 日期：2026-10-16　对应方案：`docs/优化方案_外部借鉴v2与落地_20261016.md` §3 Phase 1.1 / §4 B-1
> 一句话：**启动器已改成「装了 sageattention 就自动启用、没装就照旧」**；剩下只需你在 Windows 上把包装好。

---

## 1. 已核验的环境（2026-10-16，只读）

| 项 | 值 | 来源 |
|---|---|---|
| Python | **3.13.6**（MSC v.1944） | `comfyui_startup.log` |
| PyTorch | **2.14.0+cu132** | `python_embeded\Lib\site-packages\torch\version.py` |
| CUDA（torch） | 13.2 | 同上 |
| Triton | **3.7.0（已安装）** | `site-packages\triton\__init__.py` |
| ComfyUI | **0.39.0** | `ComfyUI\comfyui_version.py` |
| GPU | RTX 5060 Laptop（**Blackwell / sm_120**），驱动 616.92 | `comfyui_stdout.log` |
| `sageattention` 包 | **未安装** | `SeedVR2 optimizations check: SageAttention ✗ \| Triton ✓`（日志）+ 目录实测 |
| ComfyUI 支持 | `--use-sage-attention`（`comfy/cli_args.py:150`）；另有独立 `sage3` 路径（`attention.py:40-45,958`） | — |

**关键事实（决定顺序）**：`comfy/ldm/modules/attention.py:28-38` —— 一旦传了 `--use-sage-attention` 而 `sageattention` 不可导入，ComfyUI **直接 `exit(-1)`**（启动即死）。**所以必须先装包，再加/生效标志**——这正是下面把它做成「守卫式自动启用」的原因。

---

## 2. 已落地的改动（本次已改，纯增量、可回退）

给**两个候选启动器**都加了同一段守卫逻辑（两处都改，是因为哪个是当前生效的启动器无法从磁盘单点确证；改动在未装包时**行为与原样完全一致**，所以对“非生效”那份无害）：

1. `D:\ComfyUI_portable_TE_v260619\ComfyUI\run_nvidia_gpu_fixed.bat`（README 指向、参数与 10-12 性能画像吻合）
   备份：`run_nvidia_gpu_fixed.bat.bak_sageattention_20261016`
2. `D:\漫剧生成系统\.workbuddy\run_comfyui.bat`（自称由计划任务 `MJSCXT_ComfyUI` 启动）
   备份：`run_comfyui.bat.bak_sageattention_20261016`

加入的守卫段（ASCII，符合两文件「保持纯 ASCII」的要求）：

```bat
set "SAGE_FLAG="
python_embeded\python.exe -c "import importlib.util as u; raise SystemExit(0 if u.find_spec('sageattention') else 1)" >nul 2>&1 && set "SAGE_FLAG=--use-sage-attention"

python_embeded\python.exe -s ComfyUI\main.py ... %SAGE_FLAG%
```

含义：启动时快速探测 `sageattention` 是否存在（`find_spec` 不执行模块、毫秒级）；**装了才追加 `--use-sage-attention`，没装就一个字符都不加**——因此**不会**触发 `exit(-1)`。装好包后**只需重启 ComfyUI 即自动生效**，无需再改文件。

> ⚠️ 请确认哪一份才是计划任务实际调用的启动器（可看任务动作 `schtasks /query /tn MJSCXT_ComfyUI /v /fo LIST` 的 “Task To Run”）。若不是这两份之一，把同样两行加进真正那份即可。

---

## 3. 安装 sageattention（**需你在 Windows 执行**——我无法代跑 pip/Windows 命令）

在 ComfyUI **停止**的状态下（先停后台渲染），用**它自带的**嵌入式解释器装：

```bat
cd /d "D:\ComfyUI_portable_TE_v260619\ComfyUI"
python_embeded\python.exe -s -m pip install sageattention
```

**风险预告（重要，别抱侥幸）**：`py3.13 + torch2.14 + CUDA13.2 + sm_120` 是**极新的组合**，PyPI 上的预编译轮子**很可能没有**对应版本 → 大概率**回落到源码编译**（需要 MSVC + CUDA Toolkit 13.x，且要能编出 sm_120 内核）。若上面命令报 “没有匹配的 wheel / 构建失败”，按下面备选处理：

- **备选 A — 社区 Windows 预编译轮子**：woct0rdho / 相关维护者的 SageAttention Windows 构建（**待核实是否有 torch2.14/cu132/py3.13/sm120 版本**）。装对应轮子：`python_embeded\python.exe -s -m pip install <wheel路径>.whl`。
- **备选 B — 源码编译**：`pip install --no-build-isolation sageattention`（需已装 VS Build Tools + CUDA Toolkit 13.x）。门槛最高。
- **备选 C — 放弃全局标志，评估替代**：见 §6。

装完先**验证导入**（这一步决定能不能启用）：

```bat
python_embeded\python.exe -c "import sageattention, sageattention.sageattn as s; print('sageattention OK', getattr(sageattention,'__version__','?'))"
```

`print` 出 OK 才算成功（能导入 = 守卫会自动启用）。

---

## 4. 验证与验收（go / no-go）

1. **启动**：运行启动器，控制台/日志出现 `Using sage attention`（`attention.py:923`），**且没有报错退出**。
   - 若日志出现 `CUDA API FAILED` / `triton` abort / `Fatal Python error` → 视为 **no-go**（本机有 Blackwell triton 崩溃史，见 §5）。
2. **单镜 A/B**：用**同一 seed、同一 prompt**跑一张分镜图：
   - 记录「分镜 GPU 段」耗时（对照 `[分镜计时]` 日志，或性能画像口径的 ≈211s/镜）。
   - 人工核对画面无崩坏（糊、错位、色偏）。
3. **小批量**：跑 3–5 镜，确认稳定、QC 通过率不下降。
4. **过则全量**：把该启动器设为计划任务实际使用的那份，进入常态生产。

**期望收益**：注意力 2–5×（出图/出视频普遍受益）——注意这是**单镜 GPU 段**的提速，前置段（预检/3D）不受影响。

---

## 5. 回退（任选其一，秒级）

- 删掉启动器里那两行 `set "SAGE_FLAG="` / 探测行 → 立即回到原行为；或
- 用备份覆盖：`copy run_nvidia_gpu_fixed.bat.bak_sageattention_20261016 run_nvidia_gpu_fixed.bat`；或
- `python_embeded\python.exe -s -m pip uninstall sageattention`（卸载后守卫自动不再加标志）。

---

## 6. 风险与「若装不上」的替代

- **兼容性**：`spas_sage_attn`（SpargeAttn，已装）只带 `_qattn_sm80/sm89/sm90.pyd`，**没有 sm_120** → 对本机 Blackwell 不直接可用，佐证「这条路在新卡上不好走」。
- **稳定性**：本机历史上有 Blackwell 的 triton abort（启动器注释：`SolAttn triton abort`）。SageAttention 同为 triton 内核，**必须实测**，不能纸面认定。
- **替代方案（若 B-1 走不通）**：
  1. **`--use-ck-attention`**（ComfyUI 本机的 `comfy_kitchen` int8 注意力，`attention.py:942-945`）——本机已装 `comfy_kitchen`，是**同为注意力加速**的低成本候选；但注意它同样在不可用时 `exit(-1)`（`:946-948`），且 SolAttn 在本机有过崩溃史，需实测。
  2. **`sage3`（SageAttention3，Blackwell 专用）**：ComfyUI 有 `sageattn3_blackwell` 路径（`attention.py:40-45,767,958`），但它是**节点级注册**（`register_attention_function("sage3", …)`），**不受 `--use-sage-attention` 全局开关控制**；若走这条路，需要在工作流里显式选用 `sage3` 注意力函数，而非改启动器。
  3. 退回**不依赖注意力内核**的提速项：`B-4`（提示词增强降频）、`B-5`（内存护栏）、以及少步蒸馏 LoRA（`B-2`，若 H3 有对应 LoRA）。

---

## 7. 结论

- **本次已交付**：守卫式启动器改造（零风险、装了即生效、可秒回退）+ 本落地说明。
- **待你执行**：在 Windows 上装 `sageattention` 并跑 §4 的 A/B 验收。
- **诚实的预判**：鉴于栈极新 + 卡是 Blackwell + 本机有 triton 崩溃史，**B-1 有可能装不上或不稳**；若如此，转 §6 替代（尤其 `--use-ck-attention` / `sage3`），别在源码编译上无限投入。
