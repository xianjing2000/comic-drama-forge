import React from 'react';

// ==========================================================================
// 线性图标集（Lucide 风格，零依赖手写）
// --------------------------------------------------------------------------
// 为什么不用 lucide-react：项目未装该依赖，且 Electron 离线打包对新增依赖敏感；
// 需要的图标只有十几个，手写 SVG 更安全也更轻。
// 统一约定：24x24 视窗、stroke=currentColor、粗细 1.75、圆头圆角 —— 保证全站
// 线条观感一致（此前是 emoji 与 SVG 混用，观感割裂）。
// ⚠️ 颜色一律继承 currentColor，不要在图标里写死色值。
// ==========================================================================

export type IconProps = {
  className?: string;
  /** 未传 className 时的默认尺寸 */
  size?: number;
  strokeWidth?: number;
};

function Svg({ children, className, size, strokeWidth = 1.75 }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

/** 概览 / 统计 */
export const BarChart3 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 3v18h18" />
    <path d="M7 16V10M12 16V6M17 16v-4" />
  </Svg>
);

/** 分镜 / 影片 */
export const Clapperboard = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20.2 6 3 11l-.9-2.4A1.5 1.5 0 0 1 3.5 6.6l15.3-4.2a1.5 1.5 0 0 1 1.8 1.1z" />
    <path d="M3 11h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
    <path d="m7.5 11 5.3 10M16.5 11l-5.3 10" />
  </Svg>
);

/** 质检 / 通过 */
export const CheckCircle2 = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="m8.5 12.5 2.5 2.5 4.5-5" />
  </Svg>
);

/** 超分 / 放大查看 */
export const ZoomIn = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5M11 8v6M8 11h6" />
  </Svg>
);

/** 关系图 */
export const Network = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="2" width="6" height="6" rx="1.5" />
    <rect x="2" y="16" width="6" height="6" rx="1.5" />
    <rect x="16" y="16" width="6" height="6" rx="1.5" />
    <path d="M12 8v4M9 12H5v4M15 12h4v4" />
  </Svg>
);

/** 音频 */
export const Music = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 18V5l11-2v13" />
    <circle cx="6" cy="18" r="3" />
    <circle cx="17" cy="16" r="3" />
  </Svg>
);

/** 输出 / 导出 */
export const Share2 = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="18" cy="5" r="3" />
    <circle cx="6" cy="12" r="3" />
    <circle cx="18" cy="19" r="3" />
    <path d="m8.6 10.6 6.8-4M8.6 13.4l6.8 4" />
  </Svg>
);

/** 警告 / 异常 */
export const AlertTriangle = (p: IconProps) => (
  <Svg {...p}>
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4M12 17h.01" />
  </Svg>
);

/** 空列表 / 任务 */
export const ClipboardList = (p: IconProps) => (
  <Svg {...p}>
    <rect x="8" y="2" width="8" height="4" rx="1" />
    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
    <path d="M8 11h8M8 15h5" />
  </Svg>
);

/** 项目 / 文件夹 */
export const FolderOpen = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 19V6a2 2 0 0 1 2-2h4l2 3h6a2 2 0 0 1 2 2v3" />
    <path d="M3 19l2.5-7.5A2 2 0 0 1 7.4 10h12.2a1 1 0 0 1 .95 1.32L19 19a2 2 0 0 1-2 1H5a2 2 0 0 1-2-1Z" />
  </Svg>
);

/** 素材库 */
export const BookOpen = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 7v14" />
    <path d="M3 18a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v1a2 2 0 0 0 2 2H7a4 4 0 0 1-4-4Z" />
    <path d="M21 18a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v1a2 2 0 0 1-2 2h6a4 4 0 0 0 4-4Z" />
    <path d="M6 11a7 7 0 0 1 6-7 7 7 0 0 1 6 7" />
  </Svg>
);

/** 记忆 / 知识 */
export const Brain = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5a3 3 0 0 1 5.2-1.6A3.5 3.5 0 0 1 20.5 8a3 3 0 0 1 .5 4.5V20a2 2 0 0 1-2 2h-3l-2-3h-3l-2 3H6a2 2 0 0 1-2-2v-7.5A3 3 0 0 1 4.5 8a3.5 3.5 0 0 1 3.3-4.6A3 3 0 0 1 12 5Z" />
    <path d="M12 5v17" />
  </Svg>
);

/** 密钥 / 保险库 */
export const KeyRound = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="7.5" cy="15.5" r="4.5" />
    <path d="m10.7 12.3 8.3-8.3M19 4l2 2M17 7l2 2" />
  </Svg>
);

/** 可见（显示密码/密钥） */
export const Eye = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
    <circle cx="12" cy="12" r="3" />
  </Svg>
);

/** 不可见（隐藏密码/密钥） */
export const EyeOff = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
    <path d="M10.7 5.12A9.8 9.8 0 0 1 12 5c6.5 0 10 7 10 7a13.2 13.2 0 0 1-1.67 2.68" />
    <path d="M6.61 6.61A13.5 13.5 0 0 0 2 12s3.5 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
    <path d="m2 2 20 20" />
  </Svg>
);

/** 搜索 */
export const Search = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </Svg>
);

/** 播放 */
export const Play = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 4.5v15l13-7.5z" />
  </Svg>
);

/** 暂停 */
export const Pause = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 5v14" />
    <path d="M16 5v14" />
  </Svg>
);

/** 下载 */
export const Download = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3v12M7 11l5 5 5-5" />
    <path d="M4 20h16" />
  </Svg>
);

/** 上传 */
export const Upload = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 21V9M7 13l5-5 5 5" />
    <path d="M4 4h16" />
  </Svg>
);

/** 重试 */
export const RefreshCw = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 12a9 9 0 1 1-3.2-6.9" />
    <path d="M21 4v5h-5" />
  </Svg>
);

/** 新增 */
export const Plus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5v14M5 12h14" />
  </Svg>
);

/** 展开/收起箭头 */
export const ChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="m9 6 6 6-6 6" />
  </Svg>
);

export const ChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="m6 9 6 6 6-6" />
  </Svg>
);

/** 时钟 / 耗时 */
export const Clock = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3.5 2" />
  </Svg>
);

/** 关闭 */
export const X = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Svg>
);

// ==========================================================================
// 第二批：emoji → 线性 SVG 全站替换（此前功能图标用 emoji，字号受系统字体
// 影响、跨平台观感不一，且与线性图标集割裂）。命名与风格同上。
// ==========================================================================

/** 设置 / 配置 */
export const Settings = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12.2 2h-.4a2 2 0 0 0-2 2v.2a2 2 0 0 1-1 1.7l-.4.3a2 2 0 0 1-2 0l-.2-.1a2 2 0 0 0-2.7.7l-.2.4a2 2 0 0 0 .7 2.7l.2.1a2 2 0 0 1 1 1.7v.5a2 2 0 0 1-1 1.7l-.2.1a2 2 0 0 0-.7 2.7l.2.4a2 2 0 0 0 2.7.7l.2-.1a2 2 0 0 1 2 0l.4.3a2 2 0 0 1 1 1.7V20a2 2 0 0 0 2 2h.4a2 2 0 0 0 2-2v-.2a2 2 0 0 1 1-1.7l.4-.3a2 2 0 0 1 2 0l.2.1a2 2 0 0 0 2.7-.7l.2-.4a2 2 0 0 0-.7-2.7l-.2-.1a2 2 0 0 1-1-1.7v-.5a2 2 0 0 1 1-1.7l.2-.1a2 2 0 0 0 .7-2.7l-.2-.4a2 2 0 0 0-2.7-.7l-.2.1a2 2 0 0 1-2 0l-.4-.3a2 2 0 0 1-1-1.7V4a2 2 0 0 0-2-2Z" />
    <circle cx="12" cy="12" r="3" />
  </Svg>
);

/** 物品 / 包裹 */
export const Box = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
    <path d="m3.3 7 8.7 5 8.7-5" />
    <path d="M12 22V12" />
  </Svg>
);

/** 风格 / 配色 */
export const Palette = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3a9 9 0 0 0 0 18c.9 0 1.5-.7 1.5-1.5 0-.4-.15-.75-.4-1.03-.25-.28-.35-.6-.35-.97 0-.83.67-1.5 1.5-1.5H16a5 5 0 0 0 5-5c0-4.42-4.03-8-9-8Z" />
    <path d="M7.5 12h.01M10 7.8h.01M14.5 7.8h.01M17.5 11.5h.01" />
  </Svg>
);

/** 关键帧 / 画面（避开 DOM 的 Image 命名空间） */
export const ImageIcon = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <circle cx="9" cy="9" r="2" />
    <path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
  </Svg>
);

/** 构图草案 / 靶心 */
export const Target = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="5" />
    <circle cx="12" cy="12" r="1.2" />
  </Svg>
);

/** 录音 / 配音 */
export const Mic = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="2" width="6" height="13" rx="3" />
    <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
    <path d="M12 19v3" />
  </Svg>
);

/** 音量 / 混音 */
export const Volume2 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M11 5 6 9H2v6h4l5 4z" />
    <path d="M15.5 8.5a5 5 0 0 1 0 7" />
    <path d="M19 4.9a10 10 0 0 1 0 14.2" />
  </Svg>
);

/** 文档 / 剧本 / 清单 */
export const FileText = (p: IconProps) => (
  <Svg {...p}>
    <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z" />
    <path d="M14 2v5h5" />
    <path d="M16 13H8M16 17H8M10 9H8" />
  </Svg>
);

/** 影片 / 工程文件 */
export const Film = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2" y="2" width="20" height="20" rx="2.2" />
    <path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5" />
  </Svg>
);

/** 对话 */
export const MessageSquare = (p: IconProps) => (
  <Svg {...p}>
    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
  </Svg>
);

/** 编辑 */
export const Pencil = (p: IconProps) => (
  <Svg {...p}>
    <path d="M17 3a2.83 2.83 0 0 1 4 4L7.5 20.5 2 22l1.5-5.5z" />
    <path d="m15 5 4 4" />
  </Svg>
);

/** 删除 */
export const Trash2 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 6h18" />
    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <path d="M10 11v6M14 11v6" />
  </Svg>
);

/** 提示 / 说明 */
export const Lightbulb = (p: IconProps) => (
  <Svg {...p}>
    <path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5a6 6 0 0 0-12 0c0 1 .2 2.3 1.5 3.5.8.8 1.3 1.5 1.5 2.5" />
    <path d="M9 18h6M10 22h4" />
  </Svg>
);

/** 对勾（状态标记） */
export const Check = (p: IconProps) => (
  <Svg {...p}>
    <path d="M20 6 9 17l-5-5" />
  </Svg>
);

/** 复制（提示词模板的变量 chip 点击复制） */
export const Copy = (p: IconProps) => (
  <Svg {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </Svg>
);

/** 关系 / 链接 */
export const Link2 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 17H7A5 5 0 0 1 7 7h2" />
    <path d="M15 7h2a5 5 0 0 1 0 10h-2" />
    <path d="M8 12h8" />
  </Svg>
);

/** 场景 / 山景 */
export const Mountain = (p: IconProps) => (
  <Svg {...p}>
    <path d="m8 3 4 8 5-5 5 15H2L8 3z" />
  </Svg>
);

/** 角色 / 用户 */
export const User = (p: IconProps) => (
  <Svg {...p}>
    <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </Svg>
);

/** 验收 */
export const ClipboardCheck = (p: IconProps) => (
  <Svg {...p}>
    <rect x="8" y="2" width="8" height="4" rx="1" />
    <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" />
    <path d="m9 14 2 2 4-4" />
  </Svg>
);
