# 分段导出 / 单镜返工：生产前审计与验证方案（只读审计，2026-10-12）

审计对象：提交 3ffc7a6 + ffdbe09（export_mode 默认 all→segments、段落盘 seg_%04d + concat + seg_map.json + _recompose_episode_video）
生产现场：项目 逆天系统_全书 第 1 集，分镜阶段（截至 13:02 日志：shot 45 渲染中，44 已入库）
数据根：C:\Users\liujianghua\AppData\Roaming\mjscxt-desktop\mjscxt-data
运行后端：PID 15264，D:\mjscxt-migrated\mjscxt_p139\resources\backend\serve.py，12:29 重启加载本轮代码
日志：<数据根>\logs\serve_stdout.log（实时）

---

## 0. 结论（先看这里）

**P0（阻断级）：export_mode=segments 会让「整场/整集成片」只剩一个分镜段，且 video_helpers 的段落盘 + concat + recompose 全部是死代码。**

三条独立证据（可复跑，见 5.6）：

1. 插件在 segments 模式下把整片拆成 output/minimax_seg_export/<时间戳>/seg_XXXX.mp4
   （director/segment_mp4_export.py:33-66），但**从不把这些文件登记进 ComfyUI history**
   （nodes/director.py:251-262 只返回 IMAGE/AUDIO 张量，无 ui/文件输出）。
2. 插件同时把「已落盘的段」从 images 输出里摘掉，只留最后一段（executor 里前序段被换成
   1 帧海报 director/executor_core.py:346-385, 1706-1718；finalize 里按海报剔除
   nodes/director_common.py:477-493, 569-591）→ Director.images 只喂给
   CreateVideo → SaveVideo **1 个片段**。AST 探针实测：3 段输入 → released_output_slots=[0,1]
   → 保留 [2]，即只有最后一段（见 5.6）。
3. 后端拿到的 files 永远只有 1 个元素：整集/逐场都走 generate_h3_sequence_sequential，
   其返回写死 "files": [best_file]（app/comfyui_client.py:4707-4723, 4785-4793）；
   逐场路径再取 _sf[0]（app/video_helpers.py:1680-1698）。

因此 app/video_helpers.py:1875 的 _seg_files 长度恒为 1 →
len(_seg_files) > 1（:1904）恒假 → 永远走 :1934 的 else 分支，把**一个分镜段**改名成
<集>_full.mp4 / scene_NN.mp4。后果：
- 每一场只剩该场最后一个分镜段（约 2-5 秒），整集 = 场次数 x 约 4 秒；
- seg_%04d.mp4 永不落盘 → _recompose_episode_video 恒走「目录里没有 seg_*.mp4，跳过」；
  默认逐场路径连 seg_map.json 都不写（:1884-1903 在非逐场分支内）→ 单镜返工闭环 100% 不生效；
- 视频 QC 有确定性时长闸门（app/qc_client.py:2913-2945，阈值 video_max_drift=0.6）→
  长场次必然命中 fatal → 触发「换种子整场重渲」x max_retries（历史同因烧过约 6 小时 GPU），
  而失败结果仍会被 _move_with_retry 写进 scene_NN.mp4 并把整集标成 completed
  （app/video_helpers.py:1696-1701, 1748-1761）。

**动作建议（不改代码即可止损）**：在第 1 集进入视频步骤**之前**，把默认退回 all
（MJSCXT_H3_EXPORT_MODE=all 环境变量），并在**步骤边界**（暂停托管后）重启后端使 env 生效。
export_mode 是**调用时**读 os.environ（app/comfyui_client.py:5432），但 env 只能由新进程继承
→ 仍必须重启；已经 import 的模块改文件同样无效。
仅当「分段链路修好（后端能拿到 seg_XXXX.mp4 或拿到全部段）」后才建议切回 segments。

---

## 1. 链路关键分支与兜底（路径:行号）

### 1.1 导出模式解析（唯一开关点）
- app/comfyui_client.py:5414-5436：export_mode = 参数 or env(MJSCXT_H3_EXPORT_MODE) or "segments"；
  非 all/segments → 回落 all（:5434-5436）。
- app/comfyui_client.py:5499：传给 builder；app/h3_director_builder.py:418-434 写入
  timeline.output.exportMode；插件侧解析在 director/plan.py:670-674
  （segments/segment/per_segment/by_segment 都归一到 segments）。

### 1.2 整集 worker 的两条主路（默认走逐场）
- 逐场（默认开）：app/video_helpers.py:1546-1553（MJSCXT_VIDEO_PER_SCENE 默认 "1"）
  → :1627-1701 每场一次 generate_h3_sequence_sequential
  → :1655-1656 断点续跑（scene_NN.mp4 已存在则复用）
  → :1680-1681 取 _sf[0] 并判存在 → :1696-1698 move 成 scene_NN.mp4
  → :1703-1739 ffmpeg -c copy concat 成 <tag>_full.mp4（失败 → 状态 failed，不产出成片）。
- 整集一次提交（MJSCXT_VIDEO_PER_SCENE=0 才走）：:1783-1817
  → :1840-1861 无 files → failed
  → :1863-1937 分段导出分支（见 1.3）。

### 1.3 单段 / 多段分支与兜底（整集退路）
- :1875 _seg_files = sorted([...], key=_seg_sort_key)；
- :1884-1903 写 seg_map.json（episode_tag/total_segments/shot_to_seg/order），
  失败仅 warning（except 吞掉，不影响成片）；
- :1904-1933 **多段分支**（len>1，当前恒不成立）：逐段 move 成 seg_%04d.mp4
  （:1907-1913，单段 move 失败跳过并 warning）→ concat_videos（:1916-1926）→ 成功 info；
  concat 异常/失败 → :1920-1929 error，然后 :1930-1933 **回退首段当整集**；
- :1934-1937 **单段分支**（当前实际永远走这里）：直接 move 成 <tag>_full.mp4；
- :1938-2005 QC 汇总/状态（:2000 只有 ok and qc_passed 才 completed）。

### 1.4 单镜返工（_video_retry_shot_impl）
- :350-386 长镜切子段（子段名 shot_NN+字母）；
- :415-420 独立 cache_name=...retry_shotNN 与 filename_prefix=comic_drama_retry/...；
- :423-425 files 空/文件不存在 → 硬失败返回 500；
- :433-450 多子段时先按 _seg_sort_key 排序再 concat（:438-445），
  concat 失败 → :446-448 **回退首个子段**（该镜只剩半截）；
- :451-459 打「成片已过期」标记；
- :460-477 登记 rework/shot_NN.mp4 → 调 _recompose_episode_video，整体 try/except
  （失败只 warning，不影响重跑本身）；
- :478-484 返回体 recomposed / recomposed_file。

### 1.5 _recompose_episode_video 的兜底（全部 fail-open，返回空串）
- :113-116 无 seg_map.json → warning「无 seg_map.json，跳过（退回旧成片）」；
- :123-126 shot_to_seg 为空 → warning「seg_map 为空，跳过」；
- :127-135 目录无 seg_*.mp4（正则 seg_数字.mp4，:130）→ warning「目录里没有 seg_*.mp4，跳过」；
- :150-157 concat 失败 → error「concat 失败（保留旧成片）」；
- :158-163 成功路径：先把旧 <tag>_full.mp4 交给 _trash_move，再 _move_with_retry 新片；
- :164-169 成功 info / 任意异常 error（保留旧成片）；
- 集 tag 以 seg_map.json 内 episode_tag 为准（:118-122），避免覆盖错集。

### 1.6 插件侧真实行为（决定后端能看见什么）
- director/segment_mp4_export.py:33-57：segments 模式建 output/minimax_seg_export/<YYYYmmdd_HHMMSS>/；
- :64-66 段文件命名 seg_%04d.mp4（一采 _pre、精修 _pN、修脸前 _facepre 后缀）；
- :100-156 逐段写文件，任何异常只 warning 不中断（:149-156）；
- director/executor_core.py:512-513 把 run_dir 挂到 plan（:479 仅用于 report 文案）；
- director/executor_core.py:1706-1718 落盘后释放前序段像素（换 1 帧海报）；
- director/executor_core.py:1719-1724 仅 export_mode=all 才把段追加进 output_chunks；
- director/executor_core.py:1882-1904 segments 模式 combined = segment_outputs[-1]；
- nodes/director_common.py:477-493, 569-591 把海报段从 images 摘掉；
- nodes/director.py:251-262 images 是 list 输出（OUTPUT_IS_LIST），无文件类 ui 输出
  → get_output_files（app/comfyui_client.py:2071-2085，只遍历 history.outputs）
  只能看到 SaveVideo 的文件。
- 注：若某次运行 released_output_slots 判空（例如单段场次），images 会是全部段，ComfyUI 列表映射
  （execution.py:251-258, 319-327）会让 CreateVideo/SaveVideo 跑 N 次 → history 出现 N 个 mp4
  （execution.py:415-422 合并 ui）；但后端仍只取 files[0]（app/comfyui_client.py:4720, 4786）
  → 结论不变：**落盘的整集仍是「一个分镜段」**。

---
## 2. 生产验证所需的可观测日志行

| 期望字样（唯一匹配） | 出处 | 判读 |
|---|---|---|
| MiniMax H3 Director segment mp4 run dir:  | 插件 segment_mp4_export.py:53 | 出现 = segments 模式下确实在逐段导出（ComfyUI 控制台 / D:\ComfyUI_portable_TE_v260619\comfyui_stdout.log） |
| MiniMax H3 Director segment #N mp4 saved:  | 插件 segment_mp4_export.py:142-147 | 每段一条；条数应 = 该场段数 |
| Export mode: 分段导出 | 插件 director/plan.py:1177（report 文本） | 证明 exportMode 已生效 |
| [H3-episode] 整集生成：K 段一次提交，qc_fn=True, max_retries=2 | app/comfyui_client.py:4611 | 每场开场一条；K = 该场段数 |
| [H3-episode] 完成：产物 ...，通过=?，attempts=K | app/comfyui_client.py:4783 | 产物名形如 逆天系统_全书_ep01_s01_0000X_.mp4；attempts>1 = QC 正在重试（烧 GPU） |
| [episode] 按场次生成：N 场 / 0 段（build_only=False） | app/video_helpers.py:1582 | 逐场模式开场（流式下段数为 0 正常） |
| [episode][per-scene] 整集拼接完成：...<tag>_full.mp4（N 场） | app/video_helpers.py:1763 | 整集成片落盘；没有这条 = 视频步骤没完成 |
| [整集质检] 按段抽帧：N 段 → 请求 M 帧（每段中点占比） | app/video_helpers.py:1293 | 质检范围 = 当前场（_qc_scope，:1639） |
| 视频实测时长 ... 与期望 ... 偏差 ...%超阈值 | app/qc_client.py:2943-2945 | 出现 = P0 的时长崩坏被 QC 抓到 |
| [分段导出] shot->seg 映射已落盘：X 镜 / Y 段 | app/video_helpers.py:1900 | **只在 PER_SCENE=0 路径出现**；默认逐场不出现 = 闭环断点直接证据 |
| [分段导出] Y 段已落盘 + concat 成 Z | app/video_helpers.py:1925 | 出现才代表段盘 + 自拼生效（当前不可达） |
| [分段导出] concat 失败，回退首段作为成片：Z | app/video_helpers.py:1928 | 兜底告警 |
| [分段导出] 第 i 段落盘失败（跳过该段） | app/video_helpers.py:1913 | 兜底告警（段位会跳号） |
| [分段导出] concat 异常： | app/video_helpers.py:1922 | 兜底告警 |
| [单镜重跑] shot=S 由 N 个子段 concat 而成 | app/video_helpers.py:445 | 单镜返工子段拼接成功 |
| [单镜重跑] 子段 concat 失败，回退首个子段 | app/video_helpers.py:447 | 该镜只剩半截 |
| [单镜重跑] 自动重新合成失败（保留旧成片）： | app/video_helpers.py:477 | 返工成功但成片没更新 |
| [重新合成] 无 seg_map.json，跳过（退回旧成片） | app/video_helpers.py:115 | **预期会出现**（默认路径不写 seg_map） |
| [重新合成] 目录里没有 seg 段文件，跳过 | app/video_helpers.py:134 | 出现 = 没有可返工素材 |
| [重新合成] 完成：N 段（返工 K 镜：[...]）-> 成片名 | app/video_helpers.py:164 | 只有闭环真正接通才会出现 |

---

## 3. 会导致成片损坏的风险点（按严重度）

- **P0-1 成片被截成单段**：见第 0 节。证据链：插件摘段（director_common.py:477-493,569-591）+
  后端只留 files[0]（comfyui_client.py:4720/4786、video_helpers.py:1680/1875）。
- **P0-2 分段/返工闭环不可达**：:1904 多段分支恒不成立；seg_map.json 在默认逐场路径不写
  （:1884-1903 位于非逐场分支）；_recompose_episode_video 恒返回空串（段文件不落盘）。
- **P1-1 返工后该镜重复出现**：_recompose_episode_video:136-147 只用最小段位的段位换成
  返工段，**没有删掉该镜其余原段**，与 docstring「原其余段位置空」不符
  → 新片之后紧接着播旧残留子段（画面重复）。
- **P1-2 拼接期成片短暂/永久消失**：:158-163 先 _trash_move(_out) 再 _move_with_retry；
  若 move 抛错（被 :167-169 的 except 吞掉并返回空串），磁盘上**没有** <tag>_full.mp4
  → 下游 pipeline.probe_video（app/pipeline.py:570-582，只看 _playable）判 not-ready，
  交付链直接缺片（旧成片已进回收站）。
- **P1-3 段位映射与实际文件错位**：seg_map.json 的 shot_to_seg 用 segs 顺序（:1884-1891），
  实际段号是排序后 enumerate 的 seg_%04d（:1906-1907），且 move 失败会跳号（:1912-1913）
  → 一旦两者不同源，返工会替换错误段位（画面错乱，无任何校验）。
- **P1-4 兜底把单段当整集交付**：:1930-1933（concat 失败回退首段）与 :447-448
  （子段 concat 失败回退首个子段）只打 error，无段数/时长校验；QC 关闭或不可判定时会静默进交付。
- **P1-5 QC 时长闸门触发整场重渲**：qc_client.py:2939-2945 + qc_config.json
  （video_max_drift=0.6、video_frame_count=3、pass_score=70）；
  comfyui_client.py:4753-4764 质量类失败会换种子（连带作废段缓存）→ 每场成倍白烧 GPU。
- **P2-1 磁盘无限增长**：output/minimax_seg_export/<stamp>/ 每次运行留一份整场 mp4，
  app 侧清理只针对 qc_results 里的 file（video_helpers.py:1944-1963），不清该目录；
  且这些段文件后端读不到（设计断点）。
- **P2-2 IO 放大**：逐场模式每场先写 ComfyUI 产物 → move 成 scene_NN.mp4 → 再 concat 整集；
  启用返工后还要 rework/shot_NN.mp4 副本。
- **P2-3 排序键边界**：_seg_sort_key（:172-196）已修好（ffdbe09，见 5.6），
  但「取文件名最后一组数字」依赖 SaveVideo 计数器（h3_director_builder.py:951-961 同 prefix 递增），
  跨 source/多前缀混排不可依赖；无数字的文件统一排到末尾（:191-192）。
- **P2-4 发布面不一致**：electron-app 下 _backend\app 的副本（resource-mirror）仍是 3ffc7a6 之前的版本
  （见 5.5 哈希），从 mirror 打包的产物行为与线上相反（all vs segments）。

---
## 4. 第 1 集进入视频阶段的逐步验证清单（命令级，只读）

统一变量（PowerShell，先在一个窗口里设好）：
```powershell
$DATA   = "$env:APPDATAmjscxt-desktopmjscxt-data"
$LOG    = "$DATAlogsserve_stdout.log"
$VDIR   = "$DATAoutputideos逆天系统_全书ep01"
$SEGOUT = "D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/output/minimax_seg_export"
$APP    = "C:/Users/liujianghua/WorkBuddy/2026-09-09-16-55-22/漫剧生成系统/app"
```

### S0（现在就做，必须在视频步骤开始前）
1. 确认线上跑的就是 segments 默认：
```powershell
(Get-FileHash 'D:/mjscxt-migrated/mjscxt_p139/resources/backend/comfyui_client.py' -Algorithm SHA256).Hash
(Get-FileHash "$APP/comfyui_client.py" -Algorithm SHA256).Hash
Select-String -Path 'D:/mjscxt-migrated/mjscxt_p139/resources/backend/comfyui_client.py' -Pattern 'segments' | Select-Object -First 3 | ForEach-Object { $_.Line }
```
   判据：两份哈希一致（见 5.5）= 线上已生效。
2. 确认没有 env 兜底：
```powershell
[Environment]::GetEnvironmentVariable('MJSCXT_H3_EXPORT_MODE','User')
[Environment]::GetEnvironmentVariable('MJSCXT_H3_EXPORT_MODE','Machine')
```
   两个都为空 = 线上用的是硬编码默认 segments（危险）。
3. 决定并执行止损（需 captain/用户批准，勿擅自动生产）：
   - 方案 A（推荐）：在第 1 集视频步骤启动前，暂停托管 → 重启后端并带上 MJSCXT_H3_EXPORT_MODE=all
     → 恢复托管；重启后按 S1 观察是否还出现 segment mp4 run dir。
   - 方案 B：保持 segments 跑完第 1 场，用 S2 判定 P0 是否命中，再决定回退（代价：一场视频 + 若干次换种子重渲）。
   - 警告：不要在分镜渲染中途 kill PID 15264，会打断当前镜头（日志会出现中止/重试）。
   - 注意：本步骤没有自然缓冲窗口 —— autopilot 计划里 enable_video=true、stop_on_failure=true（plan.json），
     分镜 46/46 完成后会自动进入视频。

### S1（视频阶段开始，实时观测）
```powershell
Select-String -Path $LOG -Pattern '按场次生成','H3-episode','per-scene','整集质检','分段导出','重新合成','单镜重跑' -SimpleMatch |
  Select-Object -Last 40 | ForEach-Object { $_.Line }
Select-String -Path 'D:/ComfyUI_portable_TE_v260619/comfyui_stdout.log','D:/ComfyUI_portable_TE_v260619/comfyui.log' -Pattern 'segment mp4 run dir','segment #','Export mode' -SimpleMatch -ErrorAction SilentlyContinue |
  Select-Object -Last 30 | ForEach-Object { $_.Line }
```

### S2（第 1 场完成后的 10 秒判定：P0 是否命中）
```powershell
Get-ChildItem $VDIR | Select-Object LastWriteTime,Length,Name
ffprobe -v error -show_entries format=duration -of csv=p=0 "$VDIRscene_01.mp4"
Get-ChildItem $SEGOUT -Recurse -Filter *.mp4 -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 20 FullName,Length
```
判据：
- scene_01.mp4 时长约 2-5 秒，而该场镜头时长合计远大于此 → **P0 命中**；
- $SEGOUT 下已有 seg_XXXX.mp4，但 $VDIR 下没有 seg_0000.mp4、没有 seg_map.json
  → 后端没接管段文件（P0-2 命中）；
- 日志出现「视频实测时长 ... 与期望 ... 偏差 ...%超阈值（H3 可能截断/补白）」→ 同一问题；
- 该场期望时长可从日志取「[整集质检] 逐段一致性门禁：N 段时间区间 ...」，
  或用分镜 meta 汇总（$DATAoutputstoryboards逆天系统_全书shot_*.meta.json）。

### S3（整集完成后）
```powershell
Get-ChildItem $VDIR | Select-Object LastWriteTime,Length,Name
ffprobe -v error -show_entries format=duration -of csv=p=0 "$VDIRep01_full.mp4"
Test-Path "$VDIRseg_map.json"; Test-Path "$VDIRework"
Select-String -Path $LOG -Pattern '[分段导出]','[重新合成]' -SimpleMatch | Select-Object -Last 20 | ForEach-Object { $_.Line }
```
判据：ep01_full.mp4 时长应约等于各场期望时长之和；seg_map.json **预期不存在**（P0-2 证据）；
只有 PER_SCENE=0 退路才会有 [分段导出] 行。

### S4（单镜返工演练：**仅在 P0 修复后做**，否则必然失败）
```powershell
$body = @{ project='逆天系统_全书'; episode=1; shot_id='<shot_id>'; mode='episode' } | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:<后端端口>/api/videos/retry_shot' -Method Post -Body $body -ContentType 'application/json'
Select-String -Path $LOG -Pattern '[单镜重跑]','[重新合成]' -SimpleMatch | Select-Object -Last 10 | ForEach-Object { $_.Line }
Get-ChildItem "$VDIRework"; (Get-Item "$VDIRep01_full.mp4").LastWriteTime
```
判据：返回体 recomposed=true；日志出现「[重新合成] 完成：N 段（返工 1 镜：[...]）-> ep01_full.mp4」；
ep01_full.mp4 mtime 更新且总时长基本不变。
未修复时预期结果：返回 recomposed=false + 「[重新合成] 无 seg_map.json，跳过（退回旧成片）」
或「[重新合成] 目录里没有 seg 段文件，跳过」——这就是闭环未接通的复现步骤。

### S5（止损/回退）
```powershell
Get-Content "$DATAoutputautopilotuntime.json"    # 必须先确认 paused=true 再重启后端
# 退回默认：给后端进程环境设 MJSCXT_H3_EXPORT_MODE=all（启动脚本/系统环境变量）后重启后端
# 人工确认后清理受污染产物：
#   $VDIRscene_*.mp4、$VDIRep01_full.mp4、$DATAoutputinal逆天系统_全书ep01_final.mp4
# 之后在 UI 点「重做本集视频」，再按 S2 复核 scene_01.mp4 时长约等于该场期望时长
```

---

## 5. 证据与复跑

### 5.1 提交内容
```
git show --stat 3ffc7a6   # comfyui_client.py +17, video_helpers.py +220
git show --stat ffdbe09   # video_helpers.py +7/-1（_seg_sort_key 去扩展名）
```

### 5.2 关键代码点
- app/comfyui_client.py:5432-5436（默认 segments）、:4707-4723、:4785-4793（files 只有一个）、
  :5610-5644（history -> files -> 返回体 export_mode）
- app/video_helpers.py:1875、1904-1937、1884-1903、433-450、467-477、95-196、
  1546-1553、1627-1701、1703-1739、1783-1817
- app/qc_client.py:2913-2945（时长闸门）、app/pipeline.py:570-582（probe_video 只看可播）
- 插件：director/segment_mp4_export.py:33-66,100-156,236-251；
  director/executor_core.py:346-385,512-513,1706-1724,1882-1904；
  nodes/director_common.py:462-493,569-591；nodes/director.py:251-262；
  director/plan.py:670-674,1177

### 5.3 现场事实（2026-10-12 13:00 前后）
- D:/ComfyUI_portable_TE_v260619/ComfyUI/ComfyUI/output/minimax_seg_export **不存在**
  → segments 模式从未在这台机器上真跑过（与提交说明「未做端到端实测」一致）。
- 生产日志最后一段视频活动是 10 月 11 日的 进境_整本小说（旧 all 行为）：
  「[H3-episode] 完成：产物 ...进境_整本小说_ep01_s01_00002_.mp4，通过=False，attempts=3」。
- 当前 逆天系统_全书：分镜 44 已入库、45 渲染中、46 已出九宫格规划
  （$DATAoutputstoryboards逆天系统_全书 共 88 个文件 = 44 镜 x 2）。
- 托管计划 $DATAoutputautopilot逆天系统_全书plan.json：enable_video=true、stop_on_failure=true、
  max_episode_attempts=2、step_max_retries=2；runtime.json paused=false。

### 5.4 生产运行实例
- D:/mjscxt-migrated/mjscxt_p139/resources/backend/serve.py（PID 15264，12:29 重启；
  __pycache__ideo_helpers.cpython-313.pyc 时间戳 12:29:28 = 该进程加载了本轮代码）。
- 日志可见重启缘由：autopilot「托管已暂停：整体重启（加载分段导出新代码 + 释放内存）」。

### 5.5 三份拷贝一致性（SHA256 前 16 位）
```
comfyui_client.py  app=074D41667E9E0CAE  prod=074D41667E9E0CAE  mirror=FE6404A600FCCE9F
video_helpers.py   app=E135C59B6B93BC00  prod=E135C59B6B93BC00  mirror=F1863733A3975015
```
→ 线上后端与 app/ 字节一致（segments 已生效）；electron-app 下的 _backendapp（resource-mirror）
仍是旧版，**违反「app/*.py 必须同步两处」的团队约束**。

### 5.6 可复跑探针（AST 取真实源文件里的函数，不复制实现）
本轮已执行，输出：
```
seg_sort_key : a_00010_ -> (0,10)   a_00002_ -> (0,2)   a_00001_ -> (0,1)   a_00003_ -> (0,3)
               seg_0000.mp4 -> (0,0)   seg_0012.mp4 -> (0,12)
               逆天系统_ep01_00004_.mp4 -> (0,4)   夜航灯_两章_ep02_s01_00002_.mp4 -> (0,2)
               => 升序正确（ffdbe09 修复有效）
poster frames: 1    full frames: 96
released_output_slots: [0, 1]
kept in images output (feeds CreateVideo -> SaveVideo): [2] => count: 1   <-- P0 机制的直接证据
minimax_seg_export exists: False
```
复跑方式：用 ComfyUI 自带解释器
（D:/ComfyUI_portable_TE_v260619/ComfyUI/python_embeded/python.exe）执行 AST 抽取脚本，
被测的 _seg_sort_key、is_released_poster、released_output_slots 三个函数**直接取自**
app/video_helpers.py 与 custom_nodes/ComfyUI_MiniMaxH3_Director/director/segment_mp4_export.py 真实源文件。

### 5.7 本轮未做的事（约束遵守）
- 未修改/未删除任何生产文件；未重启/未中断任何进程；未提交 GPU 任务。
- 唯一新增文件：本审计报告 docs/segments-export-video-audit-20261012.md。
