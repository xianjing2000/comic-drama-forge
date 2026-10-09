import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useApp } from '@/context/AppContext';
import { projectsApi, novelsApi } from '@/api/client';
import { Button, Input, Modal, Badge, ConfirmDialog, Select, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { AlertTriangle, Check, Clapperboard, FileText, FolderOpen, ImageIcon, Pencil, Plus, Trash2 } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import { dropAgentSession } from '@/agentSession';
import type { Project, Novel } from '@/types';

// 风格库缩略图（ComfyUI 真实渲染，与实际成片风格一致；经 Vite 打包进 /assets）。
// 放 src/assets 下 import 而非 public/：Flask 只挂了 /assets 路由，免加后端路由。
// 61 张 = 对标 pavo 风格库全量（2D 24 / 3D 11 / 真人 26），由
// .workbuddy/test/_out/gen_style_thumbs_61.py 批量生成，可复现。
import img_Style2dUrbanRomance from '@/assets/styles/style_2d_urban_romance.jpg';
import img_Style2dGuofengDongman from '@/assets/styles/style_2d_guofeng_dongman.jpg';
import img_StyleBw2dComic from '@/assets/styles/style_bw_2d_comic.jpg';
import img_Style2dKoreanWebtoon from '@/assets/styles/style_2d_korean_webtoon.jpg';
import img_Style2dRetroAmerican from '@/assets/styles/style_2d_retro_american.jpg';
import img_Style2dInkGuofeng from '@/assets/styles/style_2d_ink_guofeng.jpg';
import img_Style2dCelAnime from '@/assets/styles/style_2d_cel_anime.jpg';
import img_Style2dCyberpunkIllust from '@/assets/styles/style_2d_cyberpunk_illust.jpg';
import img_Style2d90sAnime from '@/assets/styles/style_2d_90s_anime.jpg';
import img_Style2dOtomo from '@/assets/styles/style_2d_otomo.jpg';
import img_Style2dPixel from '@/assets/styles/style_2d_pixel.jpg';
import img_Style2dTezuka from '@/assets/styles/style_2d_tezuka.jpg';
import img_Style2dAnime from '@/assets/styles/style_2d_anime.jpg';
import img_Style2dMiyazaki from '@/assets/styles/style_2d_miyazaki.jpg';
import img_Style2dAmericanComicAnim from '@/assets/styles/style_2d_american_comic_anim.jpg';
import img_Style2dShanghaiStudio from '@/assets/styles/style_2d_shanghai_studio.jpg';
import img_Style2dStickFigure from '@/assets/styles/style_2d_stick_figure.jpg';
import img_Style2dShojo from '@/assets/styles/style_2d_shojo.jpg';
import img_Style2dLooseSketch from '@/assets/styles/style_2d_loose_sketch.jpg';
import img_Style2dGuochao from '@/assets/styles/style_2d_guochao.jpg';
import img_Style2dBwInk from '@/assets/styles/style_2d_bw_ink.jpg';
import img_Style2dChineseMythology from '@/assets/styles/style_2d_chinese_mythology.jpg';
import img_Style2dCrayon from '@/assets/styles/style_2d_crayon.jpg';
import img_Style2dShadowPlay from '@/assets/styles/style_2d_shadow_play.jpg';
import img_Style3dXianxiaCg from '@/assets/styles/style_3d_xianxia_cg.jpg';
import img_Style3dGuofengCg from '@/assets/styles/style_3d_guofeng_cg.jpg';
import img_Style3dDarkFantasy from '@/assets/styles/style_3d_dark_fantasy.jpg';
import img_Style3dAaaConcept from '@/assets/styles/style_3d_aaa_concept.jpg';
import img_Style3dUe5Urban from '@/assets/styles/style_3d_ue5_urban.jpg';
import img_Style3dLuxuryFantasy from '@/assets/styles/style_3d_luxury_fantasy.jpg';
import img_Style3dDisney from '@/assets/styles/style_3d_disney.jpg';
import img_Style3dFruitPerson from '@/assets/styles/style_3d_fruit_person.jpg';
import img_Style3dGameRender from '@/assets/styles/style_3d_game_render.jpg';
import img_Style3dClaymation from '@/assets/styles/style_3d_claymation.jpg';
import img_Style3dClayStopmotion from '@/assets/styles/style_3d_clay_stopmotion.jpg';
import img_RpPostApocalyptic from '@/assets/styles/rp_post_apocalyptic.jpg';
import img_RpXianxiaReal from '@/assets/styles/rp_xianxia_real.jpg';
import img_Rp80sRural from '@/assets/styles/rp_80s_rural.jpg';
import img_RpAncientCostume from '@/assets/styles/rp_ancient_costume.jpg';
import img_RpHongkongFilm from '@/assets/styles/rp_hongkong_film.jpg';
import img_RpKoreanDrama from '@/assets/styles/rp_korean_drama.jpg';
import img_RpUrbanReal from '@/assets/styles/rp_urban_real.jpg';
import img_RpAmericanUpturn from '@/assets/styles/rp_american_upturn.jpg';
import img_RpAmericanVintageTv from '@/assets/styles/rp_american_vintage_tv.jpg';
import img_RpBwPhotography from '@/assets/styles/rp_bw_photography.jpg';
import img_Rp90sRealist from '@/assets/styles/rp_90s_realist.jpg';
import img_RpRetroHollywood from '@/assets/styles/rp_retro_hollywood.jpg';
import img_RpBlueOrange from '@/assets/styles/rp_blue_orange.jpg';
import img_RpHighKeyAbsurd from '@/assets/styles/rp_high_key_absurd.jpg';
import img_RpSuspense from '@/assets/styles/rp_suspense.jpg';
import img_RpQuentin from '@/assets/styles/rp_quentin.jpg';
import img_RpRetroFuturism from '@/assets/styles/rp_retro_futurism.jpg';
import img_RpRussianMelancholy from '@/assets/styles/rp_russian_melancholy.jpg';
import img_RpKoreeda from '@/assets/styles/rp_koreeda.jpg';
import img_RpKoreanCold from '@/assets/styles/rp_korean_cold.jpg';
import img_RpWarFilm from '@/assets/styles/rp_war_film.jpg';
import img_RpHorror from '@/assets/styles/rp_horror.jpg';
import img_RpCourtIntrigue from '@/assets/styles/rp_court_intrigue.jpg';
import img_RpWilderness from '@/assets/styles/rp_wilderness.jpg';
import img_Rp60sScifi from '@/assets/styles/rp_60s_scifi.jpg';
import img_RpAncientChineseReal from '@/assets/styles/rp_ancient_chinese_real.jpg';

type NovelSource = 'upload' | 'existing';

const DEFAULT_CONFIG = {
  // 默认创作风格：后端数据值（非 UI 文案），保持原字面值「3D动漫渲染」不变，
  // 仅以 \u 转义书写，避免源码里出现 CJK（i18n 扫描要求本文件零中文）。
  style: '3D\u52a8\u6f2b\u6e32\u67d3',
  episodes: 10,
  shots_per_episode: 12,
  resolution: '768p_vertical',
  aspect_ratio: '16:9 \u6a2a\u5c4f',
  fps: 24,
  duration_per_shot: 5,
  episode_duration_sec: 60,
  target_shots: 12,
  voice_map: {},
};

// 新建项目风格库（对标 pavo 风格库：缩略图卡片 + 分类筛选 + 自定义）。
// value 与后端 ai_chat.SETTING_FIELDS 里 art_style 的 options 大体对齐（config.style
// 是自由文本，超集无碍）；仍以 \u 转义书写（本文件零 CJK 约定）。value 即写入 config.style 的字面值。
type StyleCat = 'all' | '3d' | '2d' | 'real';
const STYLE_LIBRARY: { value: string; label: string; cat: Exclude<StyleCat, 'all'>; img: string }[] = [
  { value: "2D\u73b0\u4ee3\u90fd\u5e02\u98ce", label: "2D\u73b0\u4ee3\u90fd\u5e02\u98ce", cat: '2d', img: img_Style2dUrbanRomance },
  { value: "2D\u53e4\u98ce\u56fd\u6f2b\u98ce", label: "2D\u53e4\u98ce\u56fd\u6f2b\u98ce", cat: '2d', img: img_Style2dGuofengDongman },
  { value: "\u9ed1\u767d\u4e8c\u7ef4\u6f2b\u753b\u52a8\u753b\u98ce\u683c", label: "\u9ed1\u767d\u4e8c\u7ef4\u6f2b\u753b\u52a8\u753b\u98ce\u683c", cat: '2d', img: img_StyleBw2dComic },
  { value: "2D\u97e9\u6f2b\u98ce", label: "2D\u97e9\u6f2b\u98ce", cat: '2d', img: img_Style2dKoreanWebtoon },
  { value: "2D\u590d\u53e4\u7f8e\u6f2b\u98ce", label: "2D\u590d\u53e4\u7f8e\u6f2b\u98ce", cat: '2d', img: img_Style2dRetroAmerican },
  { value: "2D\u6c34\u58a8\u56fd\u98ce", label: "2D\u6c34\u58a8\u56fd\u98ce", cat: '2d', img: img_Style2dInkGuofeng },
  { value: "2D\u8d5b\u7490\u7490\u52a8\u753b\u98ce", label: "2D\u8d5b\u7490\u7490\u52a8\u753b\u98ce", cat: '2d', img: img_Style2dCelAnime },
  { value: "\u8d5b\u535a\u670b\u514b\u6570\u5b57\u63d2\u753b\u98ce\u683c", label: "\u8d5b\u535a\u670b\u514b\u6570\u5b57\u63d2\u753b\u98ce\u683c", cat: '2d', img: img_Style2dCyberpunkIllust },
  { value: "90\u5e74\u4ee3\u65e5\u5f0f\u52a8\u753b\u98ce\u683c", label: "90\u5e74\u4ee3\u65e5\u5f0f\u52a8\u753b\u98ce\u683c", cat: '2d', img: img_Style2d90sAnime },
  { value: "\u5927\u53cb\u514b\u6d0b\u98ce\u683c", label: "\u5927\u53cb\u514b\u6d0b\u98ce\u683c", cat: '2d', img: img_Style2dOtomo },
  { value: "\u50cf\u7d20\u98ce", label: "\u50cf\u7d20\u98ce", cat: '2d', img: img_Style2dPixel },
  { value: "\u624b\u51a2\u6cbb\u866b\u65f6\u4ee3\u5361\u901a\u63d2\u753b\u98ce\u683c", label: "\u624b\u51a2\u6cbb\u866b\u65f6\u4ee3\u5361\u901a\u63d2\u753b\u98ce\u683c", cat: '2d', img: img_Style2dTezuka },
  { value: "\u4e8c\u6b21\u5143\u52a8\u6f2b", label: "\u4e8c\u6b21\u5143\u52a8\u6f2b", cat: '2d', img: img_Style2dAnime },
  { value: "\u5bab\u5d0e\u9a8f\u753b\u98ce", label: "\u5bab\u5d0e\u9a8f\u753b\u98ce", cat: '2d', img: img_Style2dMiyazaki },
  { value: "\u7f8e\u56fd\u6f2b\u753b\u52a8\u753b\u63d2\u753b\u98ce\u683c", label: "\u7f8e\u56fd\u6f2b\u753b\u52a8\u753b\u63d2\u753b\u98ce\u683c", cat: '2d', img: img_Style2dAmericanComicAnim },
  { value: "\u4e0a\u7f8e\u5382\u8001\u52a8\u753b\u98ce\u683c", label: "\u4e0a\u7f8e\u5382\u8001\u52a8\u753b\u98ce\u683c", cat: '2d', img: img_Style2dShanghaiStudio },
  { value: "\u513f\u7ae5\u7b80\u7b14\u753b\u98ce\u683c", label: "\u513f\u7ae5\u7b80\u7b14\u753b\u98ce\u683c", cat: '2d', img: img_Style2dStickFigure },
  { value: "\u65e5\u5f0f\u5c11\u5973\u6f2b\u98ce\u683c", label: "\u65e5\u5f0f\u5c11\u5973\u6f2b\u98ce\u683c", cat: '2d', img: img_Style2dShojo },
  { value: "\u677e\u5f1b\u8f6e\u5ed3\u624b\u7ed8\u98ce", label: "\u677e\u5f1b\u8f6e\u5ed3\u624b\u7ed8\u98ce", cat: '2d', img: img_Style2dLooseSketch },
  { value: "\u56fd\u98ce\u4e8c\u6b21\u5143\u65b0\u56fd\u6f6e\u98ce\u683c", label: "\u56fd\u98ce\u4e8c\u6b21\u5143\u65b0\u56fd\u6f6e\u98ce\u683c", cat: '2d', img: img_Style2dGuochao },
  { value: "\u9ed1\u767d\u6c34\u58a8\u98ce\u683c", label: "\u9ed1\u767d\u6c34\u58a8\u98ce\u683c", cat: '2d', img: img_Style2dBwInk },
  { value: "2D\u4e2d\u56fd\u795e\u8bdd\u98ce", label: "2D\u4e2d\u56fd\u795e\u8bdd\u98ce", cat: '2d', img: img_Style2dChineseMythology },
  { value: "\u513f\u7ae5\u8721\u7b14\u624b\u7ed8\u63d2\u753b\u98ce\u683c", label: "\u513f\u7ae5\u8721\u7b14\u624b\u7ed8\u63d2\u753b\u98ce\u683c", cat: '2d', img: img_Style2dCrayon },
  { value: "\u76ae\u5f71\u620f\u63d2\u753b\u98ce\u683c", label: "\u76ae\u5f71\u620f\u63d2\u753b\u98ce\u683c", cat: '2d', img: img_Style2dShadowPlay },
  { value: "3D\u5199\u5b9eCG\u4ed9\u4fa0\u98ce", label: "3D\u5199\u5b9eCG\u4ed9\u4fa0\u98ce", cat: '3d', img: img_Style3dXianxiaCg },
  { value: "3D\u5199\u5b9eCG\u53e4\u88c5\u98ce", label: "3D\u5199\u5b9eCG\u53e4\u88c5\u98ce", cat: '3d', img: img_Style3dGuofengCg },
  { value: "\u9ed1\u6697\u5947\u5e7b\u98ce\u683c", label: "\u9ed1\u6697\u5947\u5e7b\u98ce\u683c", cat: '3d', img: img_Style3dDarkFantasy },
  { value: "\u7f8e\u56fd3A\u6e38\u620f\u6982\u5ff5\u827a\u672f\u98ce\u683c", label: "\u7f8e\u56fd3A\u6e38\u620f\u6982\u5ff5\u827a\u672f\u98ce\u683c", cat: '3d', img: img_Style3dAaaConcept },
  { value: "3D\u6b21\u4e16\u4ee3\u90fd\u5e02\u602a\u8c08\u98ce", label: "3D\u6b21\u4e16\u4ee3\u90fd\u5e02\u602a\u8c08\u98ce", cat: '3d', img: img_Style3dUe5Urban },
  { value: "3D\u5199\u5b9eCG\u73b0\u4ee3\u90fd\u5e02\u98ce", label: "3D\u5199\u5b9eCG\u73b0\u4ee3\u90fd\u5e02\u98ce", cat: '3d', img: img_Style3dLuxuryFantasy },
  { value: "3D\u8fea\u58eb\u5c3c\u52a8\u753b\u98ce\u683c", label: "3D\u8fea\u58eb\u5c3c\u52a8\u753b\u98ce\u683c", cat: '3d', img: img_Style3dDisney },
  { value: "\u6c34\u679c\u4eba\u98ce\u683c", label: "\u6c34\u679c\u4eba\u98ce\u683c", cat: '3d', img: img_Style3dFruitPerson },
  { value: "3D\u6e38\u620f\u6e32\u67d3\u98ce\u683c", label: "3D\u6e38\u620f\u6e32\u67d3\u98ce\u683c", cat: '3d', img: img_Style3dGameRender },
  { value: "\u7c98\u571f\u52a8\u753b\u98ce\u683c", label: "\u7c98\u571f\u52a8\u753b\u98ce\u683c", cat: '3d', img: img_Style3dClaymation },
  { value: "\u5b9a\u683c\u52a8\u753b\u9ecf\u571f\u98ce\u683c", label: "\u5b9a\u683c\u52a8\u753b\u9ecf\u571f\u98ce\u683c", cat: '3d', img: img_Style3dClayStopmotion },
  { value: "\u672b\u4e16\u771f\u4eba\u5199\u5b9e\u98ce", label: "\u672b\u4e16\u771f\u4eba\u5199\u5b9e\u98ce", cat: 'real', img: img_RpPostApocalyptic },
  { value: "\u53e4\u98ce\u4ed9\u4fa0\u5199\u5b9e\u98ce", label: "\u53e4\u98ce\u4ed9\u4fa0\u5199\u5b9e\u98ce", cat: 'real', img: img_RpXianxiaReal },
  { value: "\u5e74\u4ee3\u5267\u771f\u4eba\u5199\u5b9e\u98ce", label: "\u5e74\u4ee3\u5267\u771f\u4eba\u5199\u5b9e\u98ce", cat: 'real', img: img_Rp80sRural },
  { value: "\u53e4\u88c5\u771f\u4eba\u5199\u5b9e\u98ce", label: "\u53e4\u88c5\u771f\u4eba\u5199\u5b9e\u98ce", cat: 'real', img: img_RpAncientCostume },
  { value: "\u6e2f\u98ce\u7535\u5f71\u5199\u5b9e\u98ce", label: "\u6e2f\u98ce\u7535\u5f71\u5199\u5b9e\u98ce", cat: 'real', img: img_RpHongkongFilm },
  { value: "\u97e9\u5267\u90fd\u5e02\u5199\u5b9e\u98ce", label: "\u97e9\u5267\u90fd\u5e02\u5199\u5b9e\u98ce", cat: 'real', img: img_RpKoreanDrama },
  { value: "\u73b0\u4ee3\u90fd\u5e02\u5199\u5b9e\u98ce", label: "\u73b0\u4ee3\u90fd\u5e02\u5199\u5b9e\u98ce", cat: 'real', img: img_RpUrbanReal },
  { value: "\u7f8e\u5f0f\u7ecf\u6d4e\u4e0a\u884c\u98ce\u683c", label: "\u7f8e\u5f0f\u7ecf\u6d4e\u4e0a\u884c\u98ce\u683c", cat: 'real', img: img_RpAmericanUpturn },
  { value: "\u7f8e\u5f0f\u590d\u53e4\u5f71\u89c6\u98ce\u683c", label: "\u7f8e\u5f0f\u590d\u53e4\u5f71\u89c6\u98ce\u683c", cat: 'real', img: img_RpAmericanVintageTv },
  { value: "\u9ed1\u767d\u80f6\u7247\u6444\u5f71\u98ce\u683c", label: "\u9ed1\u767d\u80f6\u7247\u6444\u5f71\u98ce\u683c", cat: 'real', img: img_RpBwPhotography },
  { value: "90\u5e74\u4ee3\u5199\u5b9e\u7535\u5f71\u98ce\u683c", label: "90\u5e74\u4ee3\u5199\u5b9e\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_Rp90sRealist },
  { value: "\u7f8e\u5f0f\u590d\u53e4\u597d\u83b1\u575e", label: "\u7f8e\u5f0f\u590d\u53e4\u597d\u83b1\u575e", cat: 'real', img: img_RpRetroHollywood },
  { value: "\u84dd\u6a59\u8272\u8c03\u5f71\u89c6\u98ce\u683c", label: "\u84dd\u6a59\u8272\u8c03\u5f71\u89c6\u98ce\u683c", cat: 'real', img: img_RpBlueOrange },
  { value: "\u8352\u8bde\u9ad8\u8c03\u767d\u8272\u8272\u8c03\u7535\u5f71\u98ce\u683c", label: "\u8352\u8bde\u9ad8\u8c03\u767d\u8272\u8272\u8c03\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpHighKeyAbsurd },
  { value: "\u60ac\u7591\u7535\u5f71\u98ce\u683c", label: "\u60ac\u7591\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpSuspense },
  { value: "\u6606\u6c40\u80f6\u7247\u7535\u5f71", label: "\u6606\u6c40\u80f6\u7247\u7535\u5f71", cat: 'real', img: img_RpQuentin },
  { value: "\u7f8e\u5f0f\u590d\u53e4\u672a\u6765\u4e3b\u4e49", label: "\u7f8e\u5f0f\u590d\u53e4\u672a\u6765\u4e3b\u4e49", cat: 'real', img: img_RpRetroFuturism },
  { value: "\u4fc4\u7f57\u65af\u5fe7\u90c1\u7535\u5f71", label: "\u4fc4\u7f57\u65af\u5fe7\u90c1\u7535\u5f71", cat: 'real', img: img_RpRussianMelancholy },
  { value: "\u662f\u679d\u88d5\u548c\u65e5\u5f0f\u7eaa\u5b9e", label: "\u662f\u679d\u88d5\u548c\u65e5\u5f0f\u7eaa\u5b9e", cat: 'real', img: img_RpKoreeda },
  { value: "\u97e9\u56fd\u51b7\u6de1\u98ce\u7535\u5f71\u98ce\u683c", label: "\u97e9\u56fd\u51b7\u6de1\u98ce\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpKoreanCold },
  { value: "\u590d\u53e4\u6218\u4e89\u7535\u5f71\u98ce\u683c", label: "\u590d\u53e4\u6218\u4e89\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpWarFilm },
  { value: "\u6050\u6016\u7535\u5f71\u98ce\u683c", label: "\u6050\u6016\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpHorror },
  { value: "\u5bab\u6597\u6743\u8c0b\u51b7\u5cfb\u98ce\u683c", label: "\u5bab\u6597\u6743\u8c0b\u51b7\u5cfb\u98ce\u683c", cat: 'real', img: img_RpCourtIntrigue },
  { value: "\u8352\u91ce\u7535\u5f71\u98ce\u683c", label: "\u8352\u91ce\u7535\u5f71\u98ce\u683c", cat: 'real', img: img_RpWilderness },
  { value: "60\u5e74\u4ee3\u590d\u53e4\u79d1\u5e7b", label: "60\u5e74\u4ee3\u590d\u53e4\u79d1\u5e7b", cat: 'real', img: img_Rp60sScifi },
  { value: "\u771f\u4eba\u53e4\u98ce\u5199\u5b9e\u98ce\u683c", label: "\u771f\u4eba\u53e4\u98ce\u5199\u5b9e\u98ce\u683c", cat: 'real', img: img_RpAncientChineseReal },
];

// 风格库分类标签（labelKey 走 i18n）。顺序对标 pavo 风格库：全部 → 2D → 3D → 真人
const STYLE_CATS: { id: StyleCat; labelKey: string }[] = [
  { id: 'all', labelKey: 'project.styleCatAll' },
  { id: '2d', labelKey: 'project.styleCat2d' },
  { id: '3d', labelKey: 'project.styleCat3d' },
  { id: 'real', labelKey: 'project.styleCatReal' },
];

// 新建项目可选画面比例（与 ai_chat.SETTING_FIELDS 里 aspect_ratio 的 options 对齐）。
// value 即写入 config.aspect_ratio 的字面值，沿用 \u 转义（本文件零 CJK 约定）。
// 视频 / 分镜画幅由 style 串解析，这里落一个独立字段供生成链路与确认门识别。
// 8 种比例与 ComfyUI ResolutionSelector 节点的下拉一致（2026-09-23 用户截图）；
// ⚠️ value 后缀词不要用「横屏/竖屏」（style_kit 关键词已按显式比例优先，
//    且这些词仅供人读）——「超宽/竖幅/横幅」均可安全透过显式 "a:b" 解析。
const ASPECT_PRESETS: { value: string; labelKey: string }[] = [
  { value: '1:1 \u65b9\u5f62', labelKey: 'project.aspect1x1' },
  { value: '2:3 \u7ad6\u5e45', labelKey: 'project.aspect2x3' },
  { value: '3:2 \u6a2a\u5e45', labelKey: 'project.aspect3x2' },
  { value: '3:4 \u7ad6\u5e45', labelKey: 'project.aspect3x4' },
  { value: '4:3 \u6a2a\u5e45', labelKey: 'project.aspect4x3' },
  { value: '9:16 \u7ad6\u5c4f', labelKey: 'project.aspect9x16' },
  { value: '16:9 \u6a2a\u5c4f', labelKey: 'project.aspect16x9' },
  { value: '21:9 \u8d85\u5bbd', labelKey: 'project.aspect21x9' },
];

// 视频生成方式（写入 config.video_mode）。
// ⚠️ 2026-10-02 修复：后端 `config.norm_video_mode` 已**只保留「整集一次生成」**
//   （per_shot / keyframe 废弃，一律归一成 episode，见 config.py:384）。
//   这里原先仍列 3 项且注释自称「与后端 config.VIDEO_MODES 一致」——与事实不符，
//   用户选了会被后端静默归一。现与后端同源收敛为单值。
const VIDEO_MODE_OPTIONS: { value: string; labelKey: string; descKey: string }[] = [
  { value: 'episode', labelKey: 'project.videoModeEpisode', descKey: 'project.videoModeEpisodeDesc' },
];

const ACCEPT_EXTS = '.txt,.docx,.pdf,.epub,.md';

export function ProjectsPage() {
  const { t } = useApp();
  const toast = useToast();
  const [projects, setProjects] = useState<Project[]>([]);
  const [novels, setNovels] = useState<Novel[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  // --- 新建项目弹窗（上传小说 + 选已有小说 二合一）---
  const [showNewProject, setShowNewProject] = useState(false);
  const [source, setSource] = useState<NovelSource>('upload');
  const [projectName, setProjectName] = useState('');
  const [selectedNovel, setSelectedNovel] = useState('');
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  // 风格选择：风格库卡片（缩略图 + 分类筛选）+ 自定义输入。__custom__ 时用自定义值。
  const [stylePreset, setStylePreset] = useState(STYLE_LIBRARY[0].value);
  const [styleCat, setStyleCat] = useState<StyleCat>('all');
  const [customStyle, setCustomStyle] = useState('');
  // 画面比例（视频/分镜画幅）：与风格一起在新建入口统一设置
  // 默认 16:9 横屏（2026-09-28 由 9:16 翻转，与后端 style_kit.DEFAULT_RATIO 一致）。
  // ⚠️ 不要写 ASPECT_PRESETS[0]：预设按截图顺序排列后第一项是 1:1。
  const [aspectRatio, setAspectRatio] = useState('16:9 \u6a2a\u5c4f');
  // 视频生成方式（项目级，写入 config.video_mode）：默认整集一次生成
  const [videoMode, setVideoMode] = useState<string>('episode');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- 编辑项目弹窗 ---
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const [editName, setEditName] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState('');

  // --- 删除项目弹窗 ---
  const [deletingProject, setDeletingProject] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  // --- 项目封面（生成中按项目 id 记忆；coverUrls 存生成后的缓存击穿 URL） ---
  const [coverBusy, setCoverBusy] = useState('');
  const [coverUrls, setCoverUrls] = useState<Record<string, string>>({});

  const reload = async () => {
    const [p, n] = await Promise.all([
      projectsApi.list().then(d => d.projects || []).catch(() => [] as Project[]),
      novelsApi.list().then(d => d.novels || []).catch(() => [] as Novel[]),
    ]);
    setProjects(p);
    setNovels(n);
    return { p, n };
  };

  useEffect(() => {
    reload()
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const resetForm = () => {
    setSource('upload');
    setProjectName('');
    setSelectedNovel('');
    setPendingFile(null);
    setDragOver(false);
    setFormError('');
    setStylePreset(STYLE_LIBRARY[0].value);
    setStyleCat('all');
    setCustomStyle('');
    setAspectRatio('16:9 \u6a2a\u5c4f');
    setVideoMode('episode');
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const openModal = () => {
    resetForm();
    setShowNewProject(true);
  };

  const closeModal = () => {
    if (submitting) return;
    setShowNewProject(false);
    resetForm();
  };

  const pickFile = (file: File | undefined | null) => {
    if (!file) return;
    setPendingFile(file);
    setFormError('');
    // 用户没写项目名时，用文件名兜个默认值，减少一步手动输入
    if (!projectName.trim()) {
      setProjectName(file.name.replace(/\.[^.]+$/, ''));
    }
  };

  const handleCreate = async () => {
    setFormError('');
    const name = projectName.trim();
    if (!name) {
      setFormError(t('project.nameRequired'));
      return;
    }

    setSubmitting(true);
    try {
      // 风格：自定义模式必须填写，否则回落到预设
      let finalStyle = stylePreset === '__custom__' ? customStyle.trim() : stylePreset;
      if (!finalStyle) {
        setFormError(t('project.styleRequired'));
        setSubmitting(false);
        return;
      }

      let novelId = '';
      let parsedChapters: number | null = null;

      if (source === 'upload') {
        if (!pendingFile) {
          setFormError(t('project.needNovelFile'));
          return;
        }
        // autoProject=false：不让后端按小说标题自动建项目，
        // 而是由前端带着用户填的名称显式创建，避免名字对不上。
        const up = await novelsApi.upload(pendingFile, { autoProject: false });
        const first = (up.results || [])[0];
        if (!first?.success || !first.novel?.novel_id) {
          throw new Error(first?.error || t('upload.failed'));
        }
        novelId = first.novel.novel_id;
        parsedChapters = first.novel.chapter_count ?? null;
      } else {
        novelId = selectedNovel;
        if (!novelId) {
          setFormError(t('project.selectNovelPlaceholder'));
          return;
        }
      }

      const res = await projectsApi.create({
        name,
        novel_id: novelId,
        config: {
          ...DEFAULT_CONFIG,
          style: finalStyle,
          aspect_ratio: aspectRatio,
          // 视频生成方式：整集一次生成 / 逐镜生成 / 首尾帧驱动（后端会归一校验）
          video_mode: videoMode,
        },
      } as any);
      const key = res?.project?.dir_key || res?.project?.id || '';

      await reload();
      setShowNewProject(false);
      resetForm();

      if (key) {
        window.location.hash = `/?p=${encodeURIComponent(key)}`;
      }
      // 上传路径成功后反馈解析出的章节数（"选择已有小说"路径无此信息）。
      if (parsedChapters !== null) {
        toast.success(t('project.chapterParsed', { n: parsedChapters }));
      }
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const busyLabel = useMemo(() => {
    if (!submitting) return t('project.create');
    return source === 'upload' ? t('project.uploadingCreating') : t('project.creating');
  }, [submitting, source, t]);

  // --- 编辑项目 ---
  const openEditModal = (proj: Project) => {
    setEditingProject(proj);
    setEditName(proj.name);
    setEditError('');
  };

  const closeEditModal = () => {
    setEditingProject(null);
    setEditName('');
    setEditError('');
    setSavingEdit(false);
  };

  const handleSaveEdit = async () => {
    if (!editingProject) return;
    const newName = editName.trim();
    if (!newName) {
      setEditError(t('project.nameRequired'));
      return;
    }
    setSavingEdit(true);
    setEditError('');
    try {
      await projectsApi.rename(editingProject.dir_key || editingProject.id, newName);
      await reload();
      closeEditModal();
    } catch (e) {
      setEditError(e instanceof Error ? e.message : t('project.saveFailed'));
    } finally {
      setSavingEdit(false);
    }
  };

  // --- 删除项目 ---
  const openDeleteModal = (proj: Project) => {
    setDeletingProject(proj);
    setDeleteError('');
  };

  const closeDeleteModal = () => {
    setDeletingProject(null);
    setDeleteError('');
  };

  const handleDelete = async () => {
    if (!deletingProject) return;
    setDeleting(true);
    setDeleteError('');
    try {
      await projectsApi.deleteV2(deletingProject.dir_key || deletingProject.id, true);
      // 同名重建会复用同一 projectKey（dir_key）：不清总控会话缓存，重开面板会命中
      // 旧缓存，把已删项目的对话（含只有缓存里才有的 run 执行轨迹）原样带回新项目。
      dropAgentSession(deletingProject.dir_key || deletingProject.id);
      await reload();
      closeDeleteModal();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : t('project.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };

  // --- 项目封面 ---
  const handleGenCover = async (proj: Project) => {
    const key = proj.dir_key || proj.id;
    setCoverBusy(proj.id);
    try {
      const res = await projectsApi.generateCover(key);
      // 带时间戳击穿浏览器缓存，旧封面立即被替换
      setCoverUrls(prev => ({ ...prev, [proj.id]: res?.cover_url || `${projectsApi.coverUrl(key)}?t=${Date.now()}` }));
      await reload();
      toast.success(t('project.coverDone'));
    } catch (e) {
      toast.error(`${t('project.coverFailed')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCoverBusy('');
    }
  };

  // 加载态：沿用真实内容的外层与卡片网格列数，避免骨架 → 内容的布局跳变
  if (loading) {
    return (
      <div
        className="space-y-6 fade-in"
        role="status"
        aria-live="polite"
        aria-label={t('common.loading')}
      >
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-7 w-32" />
            <Skeleton className="h-4 w-56" />
          </div>
          <Skeleton className="h-9 w-28" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-xl" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('project.title')}</h2>
          <p className="text-sm text-ink-2 mt-1">{t('project.chooseOrUpload')}</p>
        </div>
        {/* whitespace-nowrap + shrink-0：375 视口下按钮文字会被挤成两行 */}
        <Button onClick={openModal} className="shrink-0 whitespace-nowrap">
          <Plus className="h-4 w-4" />
          {t('project.createNew')}
        </Button>
      </div>

      {/* 软失败：项目列表非空 → 只是这一次刷新失败，保留紧凑行内提示条，绝不吃掉已展示的列表 */}
      {loadError && projects.length > 0 && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {t('project.loadingFailed')}: {loadError}
        </div>
      )}

      {projects.length === 0 ? (
        loadError ? (
          /* 硬失败：项目列表为空且加载出错，没有任何数据可展示 → 整块错误态 + 重试 */
          <ErrorState
            title={t('project.loadingFailed')}
            description={loadError}
            onRetry={() => {
              setLoadError('');
              setLoading(true);
              reload()
                .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
                .finally(() => setLoading(false));
            }}
          />
        ) : (
          <EmptyState
            icon={<FolderOpen className="h-10 w-10" />}
            title={t('project.noProjects')}
            description={t('project.noProjectsHint')}
            action={<Button onClick={openModal}>{t('project.createNew')}</Button>}
          />
        )
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {projects.map((proj) => (
            <div
              key={proj.id}
              className="group bg-surface rounded-xl border border-line p-4 hover:shadow-lg transition-shadow"
            >
              <div
                role="button"
                tabIndex={0}
                className="cursor-pointer focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
                onClick={() => { window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`; }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    window.location.hash = `/?p=${encodeURIComponent(proj.dir_key || proj.id)}`;
                  }
                }}
              >
                <div className="relative aspect-video bg-surface-2 rounded-lg mb-4 flex items-center justify-center overflow-hidden group-hover:scale-105 transition-transform">
                  {(() => {
                    const key = proj.dir_key || proj.id;
                    const coverSrc = coverUrls[proj.id]
                      || (proj.has_cover ? projectsApi.coverUrl(key) : '');
                    const hasCover = Boolean(coverSrc);
                    return (
                      <>
                        {hasCover
                          ? <img src={coverSrc} alt={proj.name} className="h-full w-full object-cover" />
                          : <Clapperboard className="h-10 w-10 text-ink-3" />}
                        {/* 生成/换封面：stopPropagation 防止触发整卡跳工作台 */}
                        <button
                          type="button"
                          disabled={coverBusy === proj.id}
                          onClick={(e) => { e.stopPropagation(); handleGenCover(proj); }}
                          className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-black/45 px-2 py-1 text-xs font-medium text-white hover:bg-black/65 disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
                        >
                          <ImageIcon className="h-3.5 w-3.5" />
                          {coverBusy === proj.id
                            ? t('project.coverGenerating')
                            : hasCover ? t('project.coverRedo') : t('project.coverGen')}
                        </button>
                      </>
                    );
                  })()}
                </div>
                <h3 className="font-semibold text-ink-1 mb-1">{proj.name}</h3>
                <p className="text-sm text-ink-2 mb-3">
                  {t('project.style')}: {proj.config?.style || '—'}
                </p>
                <div className="flex items-center justify-between text-sm">
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                  <span className="text-ink-3">{new Date(proj.created_at).toLocaleDateString()}</span>
                </div>
              </div>

              {/* 操作按钮 */}
              <div className="flex gap-2 mt-3 pt-3 border-t border-line">
                {/* 编辑/删除按钮与可点击卡片是兄弟节点（不嵌套），无需再 stopPropagation */}
                <Button
                  variant="secondary"
                  size="sm"
                  className="flex-1"
                  onClick={() => {
                    setEditingProject(proj);
                    setEditName(proj.name);
                    setEditError('');
                  }}
                >
                  <Pencil className="h-4 w-4" /> {t('project.edit')}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => openDeleteModal(proj)}
                >
                  <Trash2 className="h-4 w-4" /> {t('project.delete')}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 新建项目（上传小说 / 选已有小说 二合一） */}
      <Modal isOpen={showNewProject} onClose={closeModal} title={t('project.createNew')}
        size="lg" closeOnBackdrop={false} closeOnEsc={false} preventClose={submitting}>
        <div className="space-y-4">
          {/* 项目名称 */}
          <div>
            <Input
              value={projectName}
              onChange={setProjectName}
              label={t('project.name')}
              placeholder={t('project.namePlaceholder')}
            />
          </div>

          {/* 风格库：分类标签 + 缩略图卡片 + 自定义（对标 pavo 风格库交互） */}
          <div>
            <label className="mb-1 block text-sm font-medium text-ink-2">
              {t('project.style')}
            </label>
            {/* 分类标签：全部 / 2D / 3D / 真人（对标 pavo 风格库分类），后缀显示该分类风格数 */}
            <div className="mb-2 flex flex-wrap gap-1.5">
              {STYLE_CATS.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => setStyleCat(c.id)}
                  aria-pressed={styleCat === c.id}
                  className={`rounded-full px-3 py-1 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                    styleCat === c.id
                      ? 'bg-ink-1 text-white'
                      : 'border border-line text-ink-2 hover:bg-surface-2'
                  }`}
                >
                  {t(c.labelKey)}
                  <span className="ml-1 opacity-60">
                    {c.id === 'all'
                      ? STYLE_LIBRARY.length
                      : STYLE_LIBRARY.filter(p => p.cat === c.id).length}
                  </span>
                </button>
              ))}
            </div>
            {/* 缩略图卡片网格：自定义卡在最前（对标 pavo 风格库「自定义风格」首位卡片）。
                61 张会很高 → 限高滚动，保持弹窗整体可操作（pavo 风格库弹层同理）。 */}
            <div className="max-h-72 grid grid-cols-3 gap-2 overflow-y-auto pr-1 sm:grid-cols-4 md:grid-cols-5">
              <button
                type="button"
                onClick={() => { setStylePreset('__custom__'); setFormError(''); }}
                aria-pressed={stylePreset === '__custom__'}
                title={t('project.styleCustom')}
                className={`flex aspect-[3/4] flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                  stylePreset === '__custom__'
                    ? 'border-brand bg-brand-subtle text-ink-1 ring-2 ring-brand/30'
                    : 'border-line-strong text-ink-3 hover:bg-surface-2'
                }`}
              >
                <Plus className="h-5 w-5" />
                <span className="px-0.5 text-center text-xs leading-tight">
                  {t('project.styleCustom')}
                </span>
              </button>
              {STYLE_LIBRARY
                .filter(p => styleCat === 'all' || p.cat === styleCat)
                .map((p) => (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => { setStylePreset(p.value); setFormError(''); }}
                    aria-pressed={stylePreset === p.value}
                    title={p.label}
                    className={`group relative aspect-[3/4] overflow-hidden rounded-lg border transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                      stylePreset === p.value
                        ? 'border-brand ring-2 ring-brand/40'
                        : 'border-line hover:border-line-strong'
                    }`}
                  >
                    <img
                      src={p.img}
                      alt={p.label}
                      className="absolute inset-0 h-full w-full object-cover"
                      loading="lazy"
                    />
                    {/* 底部文字遮罩：沿用 Modal 遮罩 slate-900 的例外约定（照片上保证可读） */}
                    <span className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-slate-900/85 via-slate-900/30 to-transparent px-1.5 pb-1 pt-5 text-left text-xs font-medium leading-tight text-white">
                      {p.label}
                    </span>
                    {stylePreset === p.value && (
                      <span className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-brand text-white">
                        <Check className="h-3 w-3" />
                      </span>
                    )}
                  </button>
                ))}
            </div>
            {stylePreset === '__custom__' && (
              <div className="mt-2">
                <Input
                  value={customStyle}
                  onChange={setCustomStyle}
                  placeholder={t('project.styleCustomPlaceholder')}
                />
              </div>
            )}
          </div>

          {/* 画面比例：与风格一起在新建入口统一设置，决定视频/分镜画幅 */}
          <div>
            <Select
              value={aspectRatio}
              onChange={(v) => { setAspectRatio(v); setFormError(''); }}
              label={t('project.aspectRatio')}
              options={ASPECT_PRESETS.map(p => ({ value: p.value, label: t(p.labelKey) }))}
            />
          </div>

          {/* 视频生成方式：新建时就定下来（整集一次生成 / 逐镜生成 / 首尾帧驱动），
              之后「生成视频」与托管生产都按它执行 —— 避免「功能有、入口没有」。 */}
          <div>
            <label className="block text-sm font-medium text-ink-1 mb-1">
              {t('project.videoMode')}
            </label>
            <div className="grid grid-cols-3 gap-2">
              {VIDEO_MODE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => { setVideoMode(opt.value); setFormError(''); }}
                  className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                    videoMode === opt.value
                      ? 'border-brand bg-brand/10'
                      : 'border-line-strong hover:bg-surface-2'
                  }`}
                >
                  <span className={`block text-sm font-medium ${videoMode === opt.value ? 'text-brand' : 'text-ink-2'}`}>
                    {t(opt.labelKey)}
                  </span>
                  <span className="mt-0.5 block text-xs leading-tight text-ink-3">
                    {t(opt.descKey)}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* 小说来源切换 */}
          <div>
            <label className="block text-sm font-medium text-ink-1 mb-1">
              {t('project.novelSource')}
            </label>
            <div className="inline-flex rounded-lg border border-line-strong overflow-hidden">
              {/* 保留原生：分段开关（选中态共用同一元素），
                  Button 的 rounded-md/h-8 会破坏「无间隙拼成一个圆角容器」的形状 */}
              {([
                { id: 'upload' as NovelSource, label: t('project.sourceUpload') },
                { id: 'existing' as NovelSource, label: t('project.sourceExisting') },
              ]).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setSource(opt.id); setFormError(''); }}
                  className={`px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                    source === opt.id
                      ? 'bg-brand text-white'
                      : 'bg-transparent text-ink-2 hover:bg-surface-2'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* 上传模式 */}
          {source === 'upload' && (
            <div>
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); pickFile(e.dataTransfer.files?.[0]); }}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-colors ${
                  dragOver
                    ? 'border-brand bg-info-subtle'
                    : 'border-line-strong hover:border-brand'
                }`}
              >
                <div className="mb-2 flex justify-center text-ink-3">
                  <FileText className="h-8 w-8" />
                </div>
                <p className="text-sm font-medium text-ink-1">
                  {pendingFile ? pendingFile.name : t('upload.uploadText')}
                </p>
                <p className="text-xs text-ink-2 mt-1">
                  {pendingFile
                    ? `${(pendingFile.size / 1024).toFixed(0)} KB`
                    : t('upload.fileHint')}
                </p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={ACCEPT_EXTS}
                  className="hidden"
                  onChange={(e) => pickFile(e.target.files?.[0])}
                />
              </div>
            </div>
          )}

          {/* 选择已有模式 */}
          {source === 'existing' && (
            <div>
              {novels.length === 0 ? (
                <p className="text-sm text-ink-2 py-3">
                  {t('project.noNovelsYet')}
                </p>
              ) : (
                <Select
                  value={selectedNovel}
                  onChange={(v) => { setSelectedNovel(v); setFormError(''); }}
                  options={[
                    { value: '', label: t('project.selectNovelPlaceholder') },
                    ...novels.map((n) => ({
                      value: n.novel_id,
                      label: t('project.novelOption', { name: n.name, n: n.chapter_count }),
                    })),
                  ]}
                />
              )}
            </div>
          )}

          {formError && (
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-words">
              {formError}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <Button variant="secondary" onClick={closeModal} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button onClick={handleCreate} disabled={submitting}>
              {busyLabel}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 编辑项目弹窗 */}
      {editingProject && (
        <Modal isOpen={!!editingProject} onClose={closeEditModal} title={t('project.editTitle')}
          closeOnBackdrop={false} closeOnEsc={false} preventClose={savingEdit}>
          <div className="space-y-4">
            <div>
              <Input value={editName} onChange={setEditName} label={t('project.name')} />
            </div>
            {editError && (
              <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {editError}
              </div>
            )}
            <div className="flex gap-3 pt-2">
              <Button variant="secondary" onClick={closeEditModal} disabled={savingEdit}>
                {t('common.cancel')}
              </Button>
              <Button onClick={handleSaveEdit} disabled={savingEdit}>
                {savingEdit ? t('common.saving') : t('common.save')}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除项目：统一确认弹窗（原生手写两步确认已被 ConfirmDialog 取代） */}
      <ConfirmDialog
        isOpen={!!deletingProject}
        onClose={closeDeleteModal}
        onConfirm={handleDelete}
        title={t('project.deleteTitle')}
        danger
        loading={deleting}
        confirmText={t('project.confirmDelete')}
        message={
          <>
            <p className="text-ink-1">
              {t('project.deleteConfirmPrefix')}<span className="font-semibold">{deletingProject?.name}</span>{t('project.deleteConfirmSuffix')}
            </p>
            <p className="mt-2 flex items-start gap-1.5 text-sm text-danger-strong">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              {t('project.deleteWarning')}
            </p>
            {deleteError && (
              <p className="mt-3 rounded-lg border border-danger/30 bg-danger-subtle p-3 text-sm text-danger-strong">
                {deleteError}
              </p>
            )}
          </>
        }
      />
    </div>
  );
}
