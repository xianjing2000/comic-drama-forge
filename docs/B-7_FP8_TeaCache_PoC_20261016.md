# B-7 · FP8 UNet / TeaCache 提速 PoC 方案（2026-10-16）

> 定位：`docs/优化方案_外部借鉴v2与落地_20261016.md` 的 B-7 落地说明。
> ⚠️ 这两项都改 **ComfyUI 侧**（启动参数 / 工作流节点），**不改 app 代码**；且都**改数值/结果**，
> 必须在**有 GPU 的机器**上按下方 A/B 流程实测，通过后再全量。

---

## 1. FP8 UNet（启动参数，已接成 opt-in）

**是什么**：ComfyUI 的 `--fp8_e4m3fn-unet` —— 把 UNet 以 FP8 加载，省显存/带宽（对 8GB 本机是友好项）。
与 `--use-sage-attention` 不同，它**不会**因缺少包而 `exit(-1)`；但它**改变数值 → 改变画面结果**。

**已落地**（守卫式 opt-in，默认关）：`.workbuddy/run_comfyui.bat`
```
set "FP8_FLAG="
if "%MJSCXT_H3_FP8%"=="1" set "FP8_FLAG=--fp8_e4m3fn-unet"
...  main.py ... --vram-headroom 2.0 %SAGE_FLAG% %FP8_FLAG% ...
```
即：**只有**设了环境变量 `MJSCXT_H3_FP8=1` 才追加该标志；否则与改动前逐字一致（零行为变更）。

> ⚠️ 本机已有 int8 变体模型（`minimax_h3_ref2va_pruned_int8_convrot`、`qwen_image_2.1_int8_convrot`）。
> FP8 与 int8 是**两套量化**，不要同时叠；A/B 时对比「现有 int8」vs「FP8」，看哪个更快/更省且质量不降。

**A/B 流程**：
1. 记下基准：同一 seed + 同一 prompt，跑一镜（分镜图或单段视频），记录 **耗时** 与 **质检分**。
2. 设 `MJSCXT_H3_FP8=1`，重启 ComfyUI（**生效判据：进程启动时间 > 镜像 mtime**）。
3. 同 seed 同 prompt 再跑，记录耗时/质检分。
4. 耗时显著下降且质检分不降 → 保留；否则删 `FP8_FLAG` 两行回退。
5. ⚠️ 见 §3 缓存键红线。

---

## 2. TeaCache（工作流节点，需先在 ComfyUI 侧确认）

**是什么**：训练无关的**时间步缓存复用**（约 1.5–2×），以 ComfyUI 节点形式接入。

**为什么只出方案**：TeaCache 需要 ComfyUI 侧**装有对应节点**（`ComfyUI-TeaCache` 之类），而本项目出图/出视频走的是
**用户亲自跑通的 H3 Director 工作流模板**（`h3_director_r2v_单采.json`）。加节点＝改模板拓扑，风险高于只加启动参数。

**前置确认（在有卡的机器上）**：
1. 看 `ComfyUI/custom_nodes/` 里有没有 TeaCache 节点（或可 `ComfyUI-Manager` 装）。
2. 若有：把它插到 UNet→采样链上，**阈值 0.05–0.15 按图/视频各调**（H3 段间连续性对缓存敏感，视频侧阈值要保守）。
3. 若无：本项**不做**（不引第三方节点进主链），只保留为观察项。

**A/B**：同 §1（同 seed 同 prompt，比耗时与质检分）。

---

## 3. 风险红线（务必遵守）

1. **缓存键会变**：改 FP8 量化 / 加 TeaCache 节点 → 内容寻址缓存键变化 → 已缓存的前置段/增强结果**失效**
   （一次性变慢，需重跑；不影响产物续跑语义）。**先小范围验证，再全量。**
2. **一次只改一个变量**：FP8 与 TeaCache **分开 A/B**，否则分不清是谁的收益/是谁把质量拉低了。
3. **保持必需启动参数**：任何改动都不得删 `--disable-pinned-memory` 与 `--vram-headroom`（见 `reference_comfyui_runtime`）。
4. **H3 段间连续性**：视频侧对缓存/量化比图像侧敏感，优先在**分镜图**链路验证收益，视频侧谨慎。

---

## 4. 现状与待办

- **FP8**：启动器已接成 opt-in（`MJSCXT_H3_FP8=1`）；**待你在有卡的机器上跑 A/B**。
- **TeaCache**：**待先确认 ComfyUI 侧是否已有该节点**；有则按 §2 接、无则不做。
- 两者都**不改 app 代码**，故不触发多副本同步；只改 `.workbuddy/run_comfyui.bat`（本机 dev 启动器）
  与 ComfyUI 侧（`D:\ComfyUI_portable_TE_v260619\...`，本仓之外）。
