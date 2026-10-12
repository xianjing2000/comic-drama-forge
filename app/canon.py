# -*- coding: utf-8 -*-
"""设定一致性基线（canon）—— 硬事实的机器可读表 + 生成期注入 + 输出期校验。

## 为什么需要这个模块（用户 2026-10-12 成片质检报告）

实测缺陷两条：
    · 设定矛盾：同一集里既有「47层」又有「7层」（画面守则纸写「共四十七层」，
      配音却说「七层楼」）；
    · 工牌上印「天合科技」，配音 / 字幕却出现「天河科技」。

根因不是模型「不听话」，而是**链路里从来没有一份「本剧硬事实基准」**：
剧本 / 分镜 / H3 提示词只给「可用角色 / 物品 / 场景」清单，数字与公司名全靠模型
从上下文里猜 —— 猜错既不报错也不告警，直接在成片里显形成「前后不一致」。

## 本模块的三件事

    ① 载入 app/canon_data.json（机器可读设定表；每条 fact 都带小说原文出处
       —— 文件 + 行号 + 逐字引文）；
    ② 生成期注入：render_canon_block / inject_canon_into_prompt 把硬事实渲染成
       提示词片段，由 novel_to_script.build_shots_for_chunk 追加进分镜提示词
       （**先生成提示词、后调模型**），并写 INFO 日志说明注入了哪几条；
    ③ 输出期校验：detect_conflicts / validate_script 对含互斥表述的文本判定冲突并
       给出明细（行内上下文 + 规范值 + 原文出处），由 qc_helpers._qc_canon_conflicts
       暴露给质检侧，由 novel_to_script.convert_chapter_to_script 在落盘前扫一遍。

## 设计约束

    · **fail-open**：设定表缺失 / 损坏 / 结构非法时只记 WARNING 并返回空结果，
      绝不阻断生产（与全链路其它兜底口径一致）。
    · **只钉不随集次变化的硬事实**。随剧情推进而变的量（如主角修为等级、
      「当前所在楼层」）一律不入表 —— 把开局值注入晚期集次，本身就是新的设定矛盾。
    · **判定带前界**（见 _NUM_CLASS 与 fact.conflict_guard_before）：中文里
      「四十七层」包含子串「七层」、「27层」包含子串「7层」，不加前界会把正确
      表述判成错误表述 —— 假阳性一多，QC 告警就没人信了。
    · **原文自相矛盾要写进表里、而不是替它掩盖**：例如原文守则第 3 条「请勿进入
      2704」与第 5 条「本栋楼无 2704 号房间」互斥，那是叙事诡计，不是设定漂移；
      本表只钉「2704」这个数字本身，不把两条守则互判。取舍理由全部记在
      canon_data.json 的 books[].notes。

## 与「不删减原文」的关系

注入只约束**数值 / 名称的写法**，不改变原文覆盖率口径：把「四十七层」写成
「7层」既不是删内容也不是改写情节，而是写出一个原文没有的事实 —— 属于必须消除的缺陷。
"""
from __future__ import annotations

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

#: 设定表落盘路径（人类可读 / 可手改的唯一权威副本）
CANON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "canon_data.json")

#: fact.category 的四个必备类别（用户 2026-10-12 质检报告口径）
REQUIRED_CATEGORIES = ("楼层数", "公司名", "地址", "关键数字")

#: 数字 / 数词字符集 —— 所有表述匹配的「前界」字符类
_NUM_CLASS = "0-9零一二三四五六七八九十百千万两〇"

#: 每条 fact 注入提示词时的渲染上限（防提示词被设定表撑爆）。
#: 刻意用下划线前缀 + 不写进 config_doctor.REGISTRY：它是**渲染实现细节**，
#: 不是用户可调配置项 —— tests/test_config.py 的「未登记的可配置常量」检查就是钉这个的
#: （实测：命名成 MAX_INJECT_FACTS 会被该检查判为「该登记却没登记」）。
_MAX_INJECT_FACTS = 16

_PATTERN_CACHE = {}
_CANON_CACHE = {"path": None, "mtime": None, "data": None}


class CanonError(RuntimeError):
    """设定表不可用（文件缺失 / JSON 损坏 / 结构非法）。调用方一律 fail-open 捕获。"""

# ==================================================================
# 代码内嵌副本：打包成单文件 exe 时 app/*.json 不会进 _MEIPASS
# （漫剧工坊.spec 的 datas 只收 templates/static/locales/workflows），
# 而 hidden_imports 是按 pkgutil 全量收 app/ 下的 .py —— 所以只有 .py 一定在。
# 与 prompt_templates._DEFAULT_SCRIPT_GENERATE 同一约定：
# 内嵌副本只是兜底，权威副本仍是 app/canon_data.json，
# tests/test_canon.py 逐字断言两者解析后相等（防两份漂移）。
# ==================================================================
_DEFAULT_CANON_JSON = r'''{
  "schema_version": 1,
  "updated_at": "2026-10-12",
  "authority": "小说原文",
  "authority_note": "本表是小说原文的固化副本：每条 fact 都带 source（原文文件 + 行号 + 逐字引文）。bible.json / 已落盘剧本 / 资产图只作补充 —— 与原文冲突时以本表为准。",
  "categories": [
    "楼层数",
    "公司名",
    "地址",
    "关键数字",
    "专名",
    "物品名"
  ],
  "required_categories": [
    "楼层数",
    "公司名",
    "地址",
    "关键数字"
  ],
  "boundary_note": "所有表述一律加『数字/数词前界』(?<![0-9零一二三四五六七八九十百千万两〇])，因此「四十七层」「二十七层」不会被误判成「七层」、「观澜路四十七号」不会被误判成「观澜路十七号」。fact.conflict_guard_before 可追加前界字符（floor_total 追加「第」，因原文用「第N层」表示测试层级，见 novels/20261010_184008_8d5152.txt:1567/1667/1770）。",
  "books": [
    {
      "key": "进境_整本小说",
      "title": "《进境》",
      "match_keywords": [
        "进境"
      ],
      "source": {
        "novel_id": "20261010_184008_8d5152",
        "name": "进境_整本小说",
        "original_filename": "进境_整本小说.txt",
        "text_file": "novels/20261010_184008_8d5152.txt",
        "text_file_note": "运行期绝对路径 = <PROJECT_DATA_DIR>/novels/20261010_184008_8d5152.txt（本机 = %APPDATA%\\mjscxt-desktop\\mjscxt-data\\novels\\...）；下表 source.line 按该文件行号计。",
        "duplicate_novel_id": "20261010_083132_e14bdf（同内容，仅上传时间不同，行号一致）"
      },
      "notes": [
        "原文自相矛盾①：第 102 行守则第 1 条「本栋楼共四十七层」 vs 第 226 行叙述「楼不高，大概三十多层」。取舍：以四十七层为准 —— 该值有两处独立确认（守则第 1 条 + 第 3170 行面试官明确回答「四十七层。」），而「三十多层」是叙述者站在楼外的目测模糊量、且处在诡异氛围描写里，不构成硬事实。",
        "原文自相矛盾②：第 970 行守则第 2 条「B1层为设备层」 vs 第 1366 行林栖「B1层不是设备层……B1层是'睡眠区'」。取舍：两者都不是设定漂移，而是剧情内的『守则说谎 / 规则陷阱』——本表刻意**不**给 B1 层登记互斥表述，避免把叙事诡计误判成矛盾。",
        "刻意设定③：第 3172-3178 行「楼层编号不连续（27→43→67→顶层）」「顶层不存在于任何一层」是剧情设定，故 43层 / 67层 / 顶层 都不是四十七层的冲突项。",
        "用词非矛盾：原文同时使用「员工卡」（第 55 行）与「工牌」（第 232 行）指同一物，二者**不是**互相冲突的表述，不要据此告警。"
      ],
      "facts": [
        {
          "key": "floor_total",
          "category": "楼层数",
          "label": "楼栋总层数（天合大厦A座）",
          "value": "四十七层",
          "aliases": [
            "四十七层",
            "47层",
            "四十七楼",
            "47楼"
          ],
          "conflicts": [
            "七层",
            "7层",
            "七楼",
            "7楼"
          ],
          "conflict_guard_before": [
            "第"
          ],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 102,
            "quote": "1. 本栋楼共四十七层，请勿前往四十八层及以上。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 3170,
              "quote": "\"四十七层。\"她说。"
            },
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 409,
              "quote": "守则一：本栋楼共四十七层，请勿前往四十八层及以上。"
            }
          ],
          "note": "用户 2026-10-12 成片质检实测缺陷：画面守则纸写「共四十七层」（正确），配音却说「七层楼」。错形七层/7层/七楼/7楼一律判冲突（加数字前界，故「四十七层」「二十七层」不会误判；再加「第」前界，因原文用「第N层」表示测试层级）。"
        },
        {
          "key": "floor_forbidden_top",
          "category": "楼层数",
          "label": "禁入楼层（四十八层及以上）",
          "value": "四十八层及以上",
          "aliases": [
            "四十八层",
            "48层"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 102,
            "quote": "1. 本栋楼共四十七层，请勿前往四十八层及以上。"
          },
          "corroboration": [],
          "note": "与 floor_total 同源（守则第 1 条）。登记它的目的是防止模型把「四十八层」改写成其它数字。"
        },
        {
          "key": "company_name",
          "category": "公司名",
          "label": "公司名（员工卡 / 招聘广告 / 来电显示）",
          "value": "天合科技",
          "aliases": [
            "天合科技"
          ],
          "conflicts": [
            "天河科技",
            "天河科技有限公司",
            "天合科技有限公司"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 57,
            "quote": "卡的正面上面印着\"天合科技\"四个字，下面是他的照片。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 39,
              "quote": "门口挂着一块褪色的招牌，写着\"天合科技\""
            },
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 2736,
              "quote": "\"天合科技。\"那个声音说，\"你的办公室在天合大厦A座27楼。"
            }
          ],
          "note": "用户 2026-10-12 实测缺陷：工牌上是「天合科技」，配音/字幕却出现「天河科技」。全文检索原文：「天合」21 处、「天河」0 处 —— 故「天河科技」是模型臆造，判冲突。「天合科技有限公司」也判冲突：原文没有「有限公司」后缀，加了就是改设定。"
        },
        {
          "key": "building_name",
          "category": "公司名",
          "label": "大厦名",
          "value": "天合大厦A座",
          "aliases": [
            "天合大厦A座",
            "天合大厦 A座"
          ],
          "conflicts": [
            "天河大厦",
            "天合大厦B座",
            "天河大厦A座"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 35,
            "quote": "地址栏写的是：江城东区观澜路四十七号，天合大厦A座。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 228,
              "quote": "大楼门口挂着两块牌子，一块写着\"天合大厦\"，另一块写着\"A座\""
            },
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 100,
              "quote": "天合大厦A座 — 楼层安全守则"
            }
          ],
          "note": "A 座为原文唯一写法（第 228 行两块牌子「天合大厦」+「A座」）。B 座无原文依据。"
        },
        {
          "key": "care_home_name",
          "category": "公司名",
          "label": "机构名（疗养院）",
          "value": "天合疗养院",
          "aliases": [
            "天合疗养院"
          ],
          "conflicts": [
            "天河疗养院",
            "天合疗养所"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 952,
            "quote": "天合疗养院"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 1008,
              "quote": "\"欢迎光临天合疗养院。\"她说"
            }
          ],
          "note": "与公司名同源（同一「天合」品牌），一并钉住以免疫模型把「天合」写成「天河」。"
        },
        {
          "key": "address",
          "category": "地址",
          "label": "面试地址（终面邮件 / 地图搜索）",
          "value": "江城东区观澜路四十七号",
          "aliases": [
            "江城东区观澜路四十七号",
            "观澜路四十七号",
            "观澜路47号"
          ],
          "conflicts": [
            "观澜路七号",
            "观澜路7号",
            "观澜路十七号",
            "江城西区观澜路"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 35,
            "quote": "地址栏写的是：江城东区观澜路四十七号，天合大厦A座。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 39,
              "quote": "观澜路四十七号在他公司旁边那条街上"
            },
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 194,
              "quote": "他把员工卡塞回口袋，往观澜路四十七号的方向走去。"
            }
          ],
          "note": "门牌「四十七号」与楼层数「四十七层」同数，是原文刻意呼应；错形按 7号/七号/十七号 判冲突（字面匹配，故「观澜路四十七号」不会被「观澜路十七号」误伤）。"
        },
        {
          "key": "office_floor",
          "category": "关键数字",
          "label": "办公室楼层（27楼）",
          "value": "27楼",
          "aliases": [
            "27楼",
            "二十七楼"
          ],
          "conflicts": [
            "17楼",
            "72楼"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 2736,
            "quote": "\"天合科技。\"那个声音说，\"你的办公室在天合大厦A座27楼。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 2886,
              "quote": "工作地点：天合大厦A座27楼"
            },
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 3172,
              "quote": "\"但我记得——楼层编号不连续。27之后是43，43之后是67，67之后是——\""
            }
          ],
          "note": "原文三处一致（电话、招聘广告、面试官对话里的编号序列）。注意：守则第 2 条「电梯仅停靠偶数层」与 27 是奇数并不矛盾 —— 那是剧情内的规则陷阱，不是设定漂移。错形按数字漂移登记（17楼 / 72楼：位值写反或前后颠倒）。"
        },
        {
          "key": "no_entry_room",
          "category": "关键数字",
          "label": "禁入房间号（2704）",
          "value": "2704",
          "aliases": [
            "2704"
          ],
          "conflicts": [
            "2705",
            "2706",
            "2701"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 104,
            "quote": "3. 请勿进入2704号房间。若发现该房间门已开启，请保持镇静，等待工作人员处理。"
          },
          "corroboration": [
            {
              "file": "novels/20261010_184008_8d5152.txt",
              "line": 106,
              "quote": "5. 本栋楼无2704号房间，若您看到该门牌号，请勿注视，直接移开视线。"
            }
          ],
          "note": "守则第 3 条与第 5 条自相矛盾（禁入 2704 / 本栋无 2704）是**原文刻意设定**，不得据此判冲突：本表只钉「2704」这个数字本身，不把两条守则互判。"
        },
        {
          "key": "night_no_stairs",
          "category": "关键数字",
          "label": "夜间禁行时段（禁乘楼梯）",
          "value": "23:00-05:00",
          "aliases": [
            "23:00-05:00",
            "23：00-05：00",
            "23:00—05:00"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261010_184008_8d5152.txt",
            "line": 105,
            "quote": "4. 夜间（23:00-05:00）请勿乘坐楼梯。"
          },
          "corroboration": [],
          "note": "时间窗是成片字幕/台词里最容易写错的数字之一（冒号全半角、连字符），故把三种常见写法都收为同一规范值。"
        }
      ]
    },
    {
      "key": "逆天系统_全书",
      "title": "逆天系统",
      "match_keywords": [
        "逆天系统"
      ],
      "source": {
        "novel_id": "20261012_001311_21265d",
        "name": "逆天系统_全书",
        "original_filename": "逆天系统_全书.md",
        "text_file": "novels/20261012_001311_21265d.txt",
        "text_file_note": "运行期绝对路径 = <PROJECT_DATA_DIR>/novels/20261012_001311_21265d.txt；行号按该文件计。",
        "duplicate_novel_id": "20260922_144530_6a822a / 20260923_180240_61b637 / 20260922_213220_80c823（同内容不同上传批次）"
      },
      "notes": [
        "本书为修仙题材，原文**没有**「楼层数 / 公司名 / 地址」这类硬事实 —— 已实测核对全文：检索「大厦」「公司」「地址」「楼层」命中均为 0 处（intl 文本文件 7503 行）。故本书条目只覆盖「专名 / 物品名 / 关键数字」。",
        "主角修为（第 41 行「炼气九层」）**刻意不入表**：它随剧情推进而变，把开局值注入晚期集次反而会制造新的设定矛盾。本表只收录不随集次变化的硬事实。"
      ],
      "facts": [
        {
          "key": "sect_name",
          "category": "专名",
          "label": "宗门名",
          "value": "青云宗",
          "aliases": [
            "青云宗"
          ],
          "conflicts": [
            "青云门",
            "青云宗派"
          ],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261012_001311_21265d.txt",
            "line": 21,
            "quote": "青云宗，外门广场。"
          },
          "corroboration": [
            {
              "file": "novels/20261012_001311_21265d.txt",
              "line": 33,
              "quote": "也是青云宗东峰峰主赵广德的独子。"
            }
          ],
          "note": "暂未观测到实测错形；conflicts 里的「青云门」是常见近名，按防御性登记（命中即告警，不阻断）。"
        },
        {
          "key": "hero_name",
          "category": "专名",
          "label": "主角名",
          "value": "羡进",
          "aliases": [
            "羡进"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261012_001311_21265d.txt",
            "line": 39,
            "quote": "羡进。"
          },
          "corroboration": [
            {
              "file": "novels/20261012_001311_21265d.txt",
              "line": 41,
              "quote": "十六岁，炼气九层，在外门被称为\"万年废物\"。"
            }
          ],
          "note": "人物名由 bible/continuity 资产表另有登记；此处登记的是「原文唯一写法」这一事实，供注入与校验共用。"
        },
        {
          "key": "rival_name",
          "category": "专名",
          "label": "主要对手名",
          "value": "赵天霸",
          "aliases": [
            "赵天霸"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261012_001311_21265d.txt",
            "line": 31,
            "quote": "赵天霸。"
          },
          "corroboration": [
            {
              "file": "novels/20261012_001311_21265d.txt",
              "line": 33,
              "quote": "外门第一天才，筑基初期修为，也是青云宗东峰峰主赵广德的独子。"
            }
          ],
          "note": "同上：钉原文唯一写法，防止「赵天霸/赵霸天」这类字序漂移。"
        },
        {
          "key": "key_item_zhujidan",
          "category": "物品名",
          "label": "关键物品（引发冲突的赃物）",
          "value": "筑基丹",
          "aliases": [
            "筑基丹"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261012_001311_21265d.txt",
            "line": 43,
            "quote": "内门考核的筑基丹，是我特意为你准备的。"
          },
          "corroboration": [
            {
              "file": "novels/20261012_001311_21265d.txt",
              "line": 49,
              "quote": "那筑基丹可是我攒了半年的贡献点才换的"
            }
          ],
          "note": "第 01 章的剧情核心道具；名称错一个字整条因果链就断。"
        },
        {
          "key": "total_chapters",
          "category": "关键数字",
          "label": "原著章节数",
          "value": "42章",
          "aliases": [
            "全42章",
            "42章"
          ],
          "conflicts": [],
          "conflict_guard_before": [],
          "source": {
            "file": "novels/20261012_001311_21265d.txt",
            "line": 3,
            "quote": "> 主角：羡进 · 修仙逆袭爽文 · 全42章"
          },
          "corroboration": [],
          "note": "来自原文头部元信息行；仅用于「本片总集数口径」一类表述，不参与画面校验。"
        }
      ]
    }
  ]
}'''
_DEFAULT_CANON = json.loads(_DEFAULT_CANON_JSON)


def _embed_default():
    """兜底副本的深拷贝（调用方可能就地改 match_keywords，别污染模块级常量）。"""
    return json.loads(_DEFAULT_CANON_JSON)


def _read_json_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _normalize(data):
    """结构规整 + 基本校验；结构非法抛 CanonError。

    规整是必要的：aliases 缺省要回落到 value，match_keywords 缺省要回落到 key，
    否则「表里写了但匹配不上」会表现为「注入了 0 条」这种最难查的静默失效。
    """
    if not isinstance(data, dict) or not isinstance(data.get("books"), list):
        raise CanonError("canon 设定表结构非法：缺少 books 数组")
    books = []
    for raw in data["books"]:
        if not isinstance(raw, dict) or not str(raw.get("key") or "").strip():
            continue
        b = dict(raw)
        facts = []
        for rf in (b.get("facts") or []):
            if not isinstance(rf, dict) or not str(rf.get("key") or "").strip():
                continue
            f = dict(rf)
            f["key"] = str(f["key"]).strip()
            f["category"] = str(f.get("category") or "")
            f["value"] = str(f.get("value") or "")
            f["aliases"] = [str(x) for x in (f.get("aliases") or []) if str(x).strip()] \
                or ([f["value"]] if f["value"] else [])
            f["conflicts"] = [str(x) for x in (f.get("conflicts") or []) if str(x).strip()]
            f["conflict_guard_before"] = [str(x) for x in (f.get("conflict_guard_before") or []) if str(x)]
            facts.append(f)
        b["facts"] = facts
        kws = [str(x) for x in (b.get("match_keywords") or []) if str(x).strip()]
        b["key"] = str(b["key"]).strip()
        b["match_keywords"] = kws or [b["key"]]
        books.append(b)
    out = dict(data)
    out["books"] = books
    return out


def load_canon(path: str = "", *, force: bool = False) -> dict:
    """读取设定表（按 mtime 缓存）。

    优先级：path（默认 CANON_PATH）> 代码内嵌副本 _DEFAULT_CANON。
    文件不存在 / 读失败 / JSON 损坏 / 结构非法都退回内嵌副本并记 WARNING ——
    这条路径在单文件 exe 里是**常态**（app/*.json 不进 _MEIPASS），不是异常。
    两份都不可用才抛 CanonError。
    """
    p = os.path.abspath(path) if path else CANON_PATH
    if not force:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = None
        if _CANON_CACHE["data"] is not None and _CANON_CACHE["path"] == p \
                and _CANON_CACHE["mtime"] == mtime:
            return _CANON_CACHE["data"]
    data = None
    if os.path.isfile(p):
        try:
            data = _normalize(_read_json_file(p))
        except Exception as e:  # noqa: BLE001 - 表坏了也不能阻断生产
            logger.warning("canon 设定表解析失败（回落到代码内嵌副本）：%s: %s",
                           type(e).__name__, e)
            data = None
    else:
        logger.debug("canon 设定表不存在（%s），使用代码内嵌副本", p)
    if data is None:
        try:
            data = _normalize(_embed_default())
        except Exception as e:  # noqa: BLE001
            raise CanonError("canon 设定表与代码内嵌副本均不可用：%s: %s" % (type(e).__name__, e))
    try:
        _CANON_CACHE.update({"path": p, "mtime": os.path.getmtime(p), "data": data})
    except OSError:
        _CANON_CACHE.update({"path": p, "mtime": None, "data": data})
    return data


def books(canon: dict = None) -> list:
    """全部设定表条目（小说级）。"""
    return list((canon or load_canon()).get("books") or [])


def facts_of(book: dict) -> list:
    """某本书的全部硬事实。"""
    return list((book or {}).get("facts") or [])


def fact_by_key(book: dict, key: str):
    """按 key 取一条 fact；不存在返回 None。"""
    for f in facts_of(book):
        if f.get("key") == key:
            return f
    return None


def fact_value(book: dict, key: str, default: str = "") -> str:
    """取一条 fact 的规范值（注入 / 展示用）。"""
    f = fact_by_key(book, key)
    return (f or {}).get("value") or default


def resolve_book(hint, canon: dict = None):
    """把小说名 / 剧名 / 项目名解析到设定表条目；匹配不上返回 None。

    匹配顺序：
      ① hint 与 book.key / book.title 完全相等；
      ② hint 含任一 match_keyword —— 覆盖「《进境》·第一章」「进境_整本小说#ep1」
         这种拼接串。

    匹配不上是**正常情况**（未登记的第三方小说）：注入与校验都跳过，不做任何猜测。
    """
    if not hint:
        return None
    s = str(hint)
    rows = books(canon)
    for b in rows:
        if s == str(b.get("key")) or s == str(b.get("title")):
            return b
    for b in rows:  # 关键词命中（表内顺序即优先级）
        kws = [k for k in (b.get("match_keywords") or []) if k and k in s]
        if kws:
            return b
    return None


def categories_of(book: dict) -> list:
    """按表内出现顺序返回类别（去重）—— 日志与提示词都按这个顺序排，稳定可 diff。"""
    out = []
    for f in facts_of(book):
        c = f.get("category") or "其它"
        if c not in out:
            out.append(c)
    return out


def _category_values(book: dict) -> list:
    """[(类别, [规范值, ...]), ...]，同类别多值按表内顺序。"""
    out = []
    for f in facts_of(book):
        c = f.get("category") or "其它"
        for row in out:
            if row[0] == c:
                row[1].append(f.get("value") or f.get("key"))
                break
        else:
            out.append((c, [f.get("value") or f.get("key")]))
    return out


def facts_summary(book: dict) -> list:
    """日志用的「注入了哪几条」：['楼层数=四十七层/四十八层及以上', '公司名=天合科技/…']。"""
    return ["%s=%s" % (c, "/".join([str(v) for v in vs if str(v).strip()]))
            for c, vs in _category_values(book)]


def _source_line(fact: dict) -> str:
    src = fact.get("source") or {}
    if not isinstance(src, dict):
        return ""
    bits = []
    if src.get("file"):
        bits.append(str(src["file"]))
    if src.get("line"):
        bits.append("第%s行" % src["line"])
    if src.get("quote"):
        bits.append("「%s」" % str(src["quote"])[:60])
    return " ".join(bits)


def render_canon_block(book: dict, *, max_facts: int = _MAX_INJECT_FACTS) -> str:
    """把设定表渲染成一段提示词（自带标题与硬约束句，可安全追加到任意提示词尾部）。

    返回空串的条件：book 为空 / 没有 fact —— 调用方据此保持「未登记小说行为零变化」。
    """
    facts = facts_of(book)[:max(1, int(max_facts))]
    if not facts:
        return ""
    lines = ["【本剧设定基准（硬事实·来自小说原文，必须遵守）】"]
    for c, vs in _category_values({"facts": facts}):
        vals = "｜".join([str(v) for v in vs if str(v).strip()])
        if vals:
            lines.append("  · %s：%s" % (c, vals))
    banned = []
    for f in facts:
        for form in (f.get("conflicts") or []):
            if form not in banned:
                banned.append("%s（%s，应为「%s」）" % (form, f.get("category") or f.get("key"),
                                                      f.get("value") or ""))
    if banned:
        lines.append("  ⛔ 禁写（出现即判定为设定冲突）：" + "；".join(banned))
    lines.append("  ⛔ 硬约束：以上数值与名称是本剧**唯一**正确写法 —— 画面文字（守则 / 工牌 /"
                 "告示 / 招牌）、台词、字幕、音效提示里一律使用它们；不得出现「禁写」里的任何"
                 "表述，也不得自行发明新的数字 / 公司名 / 楼层数。引用原文条款时逐字照抄，"
                 "不得改写其中的数值。")
    lines.append("  · 原文出处（本剧硬事实的判定依据，改动前请先核对这几行）：")
    for f in facts:
        src_line = _source_line(f)
        if src_line:
            lines.append("      %s ← %s" % (f.get("key"), src_line))
    return "\n".join(lines)


def inject_canon_into_prompt(prompt, book, *, label: str = "", events: list = None) -> str:
    """生成期注入：把设定表追加进提示词，并记 INFO 日志「注入了哪几条」。

    调用点在 novel_to_script.build_shots_for_chunk（**渲染完提示词、调用模型之前**），
    这样「注入的内容」与「缓存键用的提示词」是同一份 —— 改了设定表，缓存自然失效，
    不会出现「表改了却还在用旧提示词缓存出来的结果」。

    book 为空 / 无 fact → 原样返回 prompt（未登记小说零变化）。
    任何异常都原样返回 prompt（fail-open），绝不因为设定表问题让整集生成失败。
    """
    try:
        if not isinstance(book, dict) or not prompt:
            return prompt
        block = render_canon_block(book)
        if not block:
            return prompt
        summary = facts_summary(book)
        logger.info("[canon注入] %s：注入 %d 条硬事实（设定表：《%s》）—— %s",
                    label or "prompt", len(facts_of(book)), book.get("key"),
                    "；".join(summary))
        if events is not None:
            try:
                events.append({"label": label, "event": "canon_inject",
                               "book": book.get("key"),
                               "facts": [f.get("key") for f in facts_of(book)],
                               "summary": summary, "block_chars": len(block)})
            except Exception:  # noqa: BLE001
                pass
        return str(prompt) + "\n" + block
    except Exception as e:  # noqa: BLE001
        logger.warning("canon 注入失败（忽略，提示词原样下发）：%s", e)
        return prompt


# ------------------------------------------------------------------
# 输出期校验
# ------------------------------------------------------------------
def _pattern(form, guard_before=()):
    """把字面表述编译成「带前界」的正则（带缓存）。

    前界 = 数字/数词字符集 + fact.conflict_guard_before。
    有了它，「四十七层」里的「七层」、「27层」里的「7层」都不会被当成独立表述。
    """
    cls = _NUM_CLASS + "".join(str(c) for c in (guard_before or ()))
    key = (str(form), cls)
    pat = _PATTERN_CACHE.get(key)
    if pat is None:
        try:
            pat = re.compile(r"(?<![" + cls + r"])" + re.escape(str(form)))
        except re.error:  # 极端输入：正则坏了也不能让质检挂掉
            pat = re.compile(re.escape(str(form)))
        _PATTERN_CACHE[key] = pat
    return pat


def _hits(text: str, pattern):
    return [m.start() for m in pattern.finditer(text)]


def _context(text: str, pos: int, width: int = 16) -> str:
    a = max(0, pos - width)
    b = min(len(text), pos + width)
    s = text[a:b].replace("\n", " ").replace("\r", " ")
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


def detect_conflicts(text, book=None, *, novel=None, canon: dict = None,
                     max_contexts: int = 3) -> list:
    """校验一段文本是否含与设定表互斥的表述，返回冲突明细（每条形如）::

        {"key": "floor_total", "category": "楼层数", "value": "四十七层",
         "form": "七层", "kind": "contradiction",      # 或 "forbidden_form"
         "count": 2, "contexts": ["…配音：这是七层楼…"],
         "source": {...}, "message": "…"}

    kind 的两种取值：
      · contradiction  —— 同一段文本里**同时**出现规范值与禁写值（用户报告的那类）；
      · forbidden_form —— 只出现禁写值（没有规范值对照，但表述本身就是错的）。

    book 可以是设定表条目 dict，也可以是小说名 / 剧名（经 resolve_book 解析）；
    解析不到书籍 → 返回 []（不猜、不误报）。
    """
    if isinstance(book, str):
        book = resolve_book(book, canon)
    if book is None and novel:
        book = resolve_book(novel, canon)
    if not isinstance(book, dict):
        return []
    text = "" if text is None else str(text)
    if not text:
        return []
    out = []
    for f in facts_of(book):
        guard = f.get("conflict_guard_before") or ()
        can_forms = []
        for form in (f.get("aliases") or []):
            n = len(_hits(text, _pattern(form)))
            if n:
                can_forms.append((form, n))
        for form in (f.get("conflicts") or []):
            try:
                positions = _hits(text, _pattern(form, guard))
            except Exception:  # noqa: BLE001
                positions = []
            if not positions:
                continue
            kind = "contradiction" if can_forms else "forbidden_form"
            if kind == "contradiction":
                msg = ("文本同时出现规范值「%s」与互斥表述「%s」，二者互斥 —— 成片里会表现为"
                       "前后不一致（规范值应为「%s」）"
                       % ("/".join([c for c, _ in can_forms]), form, f.get("value")))
            else:
                msg = ("文本出现与设定表互斥的表述「%s」，规范值应为「%s」"
                       % (form, f.get("value")))
            out.append({
                "key": f.get("key"), "category": f.get("category"),
                "value": f.get("value"), "form": form, "kind": kind,
                "count": len(positions),
                "positions": positions[:max_contexts],
                "contexts": [_context(text, p) for p in positions[:max_contexts]],
                "canonical_hits": [{"form": c, "count": n} for c, n in can_forms],
                "source": f.get("source") or {},
                "note": f.get("note") or "",
                "message": msg,
            })
    return out


def fact_hits(text, fact: dict) -> dict:
    """文本对**单条 fact** 的命中情况：{"canonical": [...], "conflicts": [...]}。

    ⭐ 2026-10-12（T10）新增：H3 视频层需要**逐条**判断「本镜涉不涉及这条设定」
    （涉及才注入、不涉及不注入），而 detect_conflicts 是「整段文本判有没有冲突」，
    粒度对不上。两者共用同一套 _pattern（数字前界 + fact.conflict_guard_before），
    所以「四十七层」「27层」不会被判成「七层」——若各写一套正则，迟早分叉。

    纯增量函数：不改动任何既有函数的签名与行为（T1 已验收）。
    非 dict 的 fact / 空文本 / 空表述 → 两个列表都为空，永不抛。
    """
    out = {"canonical": [], "conflicts": []}
    try:
        if not isinstance(fact, dict) or not text:
            return out
        s = str(text)
        for form in (fact.get("aliases") or []):
            form = str(form or "")
            if form and _hits(s, _pattern(form)):
                out["canonical"].append(form)
        guard = fact.get("conflict_guard_before") or ()
        for form in (fact.get("conflicts") or []):
            form = str(form or "")
            if form and _hits(s, _pattern(form, guard)):
                out["conflicts"].append(form)
    except Exception as e:  # noqa: BLE001 - 判定失败按「未命中」处理，绝不阻断出片
        logger.warning("canon 单条事实命中判定失败（按未命中处理）：%s", e)
    return out


def format_conflicts(conflicts, limit: int = 8) -> list:
    """把冲突明细压成日志行（每条一行，带原文出处位置）。"""
    lines = []
    for c in list(conflicts or [])[:max(1, int(limit))]:
        src = c.get("source") or {}
        where = "%s:第%s行" % (src.get("file") or "-", src.get("line") or "-")
        ctx = (c.get("contexts") or [""])[0]
        lines.append("[canon冲突] %s（%s）%s｜出现 %d 次，例：%s｜原文出处 %s"
                     % (c.get("key"), c.get("category"), c.get("message"),
                        int(c.get("count") or 0), ctx, where))
    return lines


def iter_script_texts(script):
    """遍历剧本里所有「会进成片」的文本，产出 (位置, 文本)。

    只取**会显示或会被念出来**的字段（画面描述 / 台词 / 字幕 / 画面文字 / 音效提示），
    刻意不扫 reference_prompt_zh 这类内部提示词 —— 它们不进成片，扫了只会制造噪声。
    """
    if script is None:
        return
    if isinstance(script, str):
        yield ("text", script)
        return
    if isinstance(script, list):
        for i, row in enumerate(script):
            for pos, t in iter_script_texts(row):
                yield ("[%d].%s" % (i, pos), t)
        return
    if not isinstance(script, dict):
        return
    for key in ("title", "theme"):
        if script.get(key):
            yield (key, str(script[key]))
    for i, it in enumerate(script.get("items") or []):
        if isinstance(it, dict) and it.get("surface_text"):
            yield ("items[%d].surface_text" % i, str(it["surface_text"]))
    for i, sc in enumerate(script.get("scenes") or []):
        if isinstance(sc, dict) and sc.get("surface_text"):
            yield ("scenes[%d].surface_text" % i, str(sc["surface_text"]))
    for i, ch in enumerate(script.get("characters") or []):
        if isinstance(ch, dict) and ch.get("identity"):
            yield ("characters[%d].identity" % i, str(ch["identity"]))
    for i, sh in enumerate(script.get("shots") or []):
        if not isinstance(sh, dict):
            continue
        base = "shots[%d]" % i
        for fld in ("description", "visual_detail", "surface_text", "audio_cues",
                    "caption_text", "action", "emotion"):
            if sh.get(fld):
                yield ("%s.%s" % (base, fld), str(sh[fld]))
        cap = sh.get("caption")
        if isinstance(cap, dict) and cap.get("text"):
            yield ("%s.caption.text" % base, str(cap["text"]))
        for j, ln in enumerate(sh.get("dialogue") or []):
            if isinstance(ln, dict) and ln.get("text"):
                yield ("%s.dialogue[%d].text" % (base, j), str(ln["text"]))
            elif isinstance(ln, str) and ln.strip():
                yield ("%s.dialogue[%d]" % (base, j), ln)


def validate_text(text, novel=None, *, canon: dict = None) -> dict:
    """校验一段文本。返回 {"novel","ok","conflicts","error"}（永不抛）。"""
    res = {"novel": "", "ok": True, "conflicts": [], "error": ""}
    try:
        book = resolve_book(novel, canon) if novel else None
        if book is None:
            return res
        res["novel"] = book.get("key")
        conflicts = detect_conflicts(text, book, canon=canon)
        res["conflicts"] = conflicts
        res["ok"] = not conflicts
    except Exception as e:  # noqa: BLE001
        res["error"] = "%s: %s" % (type(e).__name__, e)
        logger.warning("canon 文本校验失败（忽略）：%s", e)
    return res


def validate_script(script, novel=None, *, canon: dict = None, max_where: int = 5) -> dict:
    """校验剧本 / 镜头列表里所有会进成片的文本。

    返回结构同 validate_text，另外每条冲突多一个 where（命中的字段位置列表）。
    """
    res = {"novel": "", "ok": True, "conflicts": [], "error": ""}
    try:
        book = resolve_book(novel, canon) if novel else None
        if book is None:
            return res
        res["novel"] = book.get("key")
        merged = {}
        for where, text in iter_script_texts(script):
            for c in detect_conflicts(text, book, canon=canon):
                sig = (c.get("key"), c.get("form"))
                row = merged.get(sig)
                if row is None:
                    row = dict(c)
                    row["where"] = []
                    row["count"] = 0
                    row["contexts"] = []
                    merged[sig] = row
                row["count"] += int(c.get("count") or 0)
                if where not in row["where"] and len(row["where"]) < max_where:
                    row["where"].append(where)
                if len(row["contexts"]) < 3:
                    row["contexts"].extend(c.get("contexts") or [])
        conflicts = list(merged.values())
        res["conflicts"] = conflicts
        res["ok"] = not conflicts
    except Exception as e:  # noqa: BLE001
        res["error"] = "%s: %s" % (type(e).__name__, e)
        logger.warning("canon 剧本校验失败（忽略）：%s", e)
    return res


# ------------------------------------------------------------------
# 巡检验证：把表里的引文逐条对回小说原文（tests/test_canon.py 与手工巡检都用它）
# ------------------------------------------------------------------
def _candidate_novel_dirs(extra=None):
    dirs = []
    for d in (extra or []):
        if d:
            dirs.append(str(d))
    env_dir = os.environ.get("MJSCXT_CANON_NOVEL_DIR")
    if env_dir:
        dirs.append(env_dir)
    try:  # 延迟导入：canon 本身保持零项目依赖，测试与巡检才需要数据目录
        import env_loader
        dirs.append(os.path.join(env_loader.PROJECT_DATA_DIR, "novels"))
    except Exception:  # noqa: BLE001
        pass
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(os.path.join(appdata, "mjscxt-desktop", "mjscxt-data", "novels"))
        dirs.append(os.path.join(appdata, "mjscxt-desktop", "resource-mirror", "novels"))
    dirs.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "novels"))
    out, seen = [], set()
    for d in dirs:
        d = os.path.abspath(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _resolve_novel_file(rel, dirs):
    for d in dirs:
        cand = os.path.join(d, os.path.basename(str(rel or "")))
        if os.path.isfile(cand):
            return cand
    return ""


def verify_sources(canon: dict = None, novel_dirs=None) -> dict:
    """逐条核对 fact 的 source / corroboration 引文是否真在原文对应行上。

    返回 {"ok": n, "bad": [...], "skipped": n, "missing_files": [...]}。
    这是「设定表 = 小说原文的固化副本」这句话的**可复现证据**：
    引文对不上（行号漂了或引文抄错）就说明表已经不是原文的忠实副本了。
    原文文件不在本机（换机器 / 精简部署）时只计入 skipped，不算失败。
    """
    data = canon or load_canon()
    dirs = _candidate_novel_dirs(novel_dirs)
    ok = 0
    skipped = 0
    missing, bad = [], []
    for b in books(data):
        for f in facts_of(b):
            for ref in [f.get("source")] + list(f.get("corroboration") or []):
                if not isinstance(ref, dict) or not ref.get("quote"):
                    skipped += 1
                    continue
                path = _resolve_novel_file(ref.get("file"), dirs)
                if not path:
                    if ref.get("file") not in missing:
                        missing.append(ref.get("file"))
                    skipped += 1
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        lines = fh.read().splitlines()
                except Exception:  # noqa: BLE001
                    skipped += 1
                    continue
                n = int(ref.get("line") or 0)
                line = lines[n - 1] if 0 < n <= len(lines) else ""
                if str(ref["quote"]) in line:
                    ok += 1
                else:
                    bad.append({"book": b.get("key"), "key": f.get("key"),
                                "file": ref.get("file"), "line": n,
                                "quote": ref["quote"], "actual": line})
    return {"ok": ok, "bad": bad, "skipped": skipped, "missing_files": missing}
