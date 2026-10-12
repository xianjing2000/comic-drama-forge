# -*- coding: utf-8 -*-
"""专业导出系统 - 支持 FCPXML、EDL、JSON 等格式

对标 openframe 的导出能力，支持接入 Premiere/达芬奇等专业后期软件

.. deprecated:: 2026-10-12 (t44)

    本模块已被 ``nle_export`` 取代（剪映草稿 / FCPXML / SRT / 帧导出等更完整），
    当前活跃代码（app/ 目录）零引用。仅因历史打包副本
    （packaging/_dist/…/app.py）与测试备份（.workbuddy/test/_bl/app.py）仍
    ``import export_manager`` 而保留；新代码请改用 nle_export，勿再 import 本模块。
"""
import json
import xml.etree.ElementTree as ET
from xml.dom import minidom
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any


class ExportManager:
    """导出管理器"""

    def __init__(self, project_key: str, base_dir: str):
        self.project_key = project_key
        self.base_dir = Path(base_dir) / "exports" / project_key
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def export_fcpml(self, timeline: Dict[str, Any]) -> str:
        """导出 FCPXML 格式 (Final Cut Pro XML)

        timeline 结构:
        {
            "project_name": "项目名称",
            "duration": 120.0,  # 总时长
            "resolution": {"width": 1920, "height": 1080},
            "fps": 30,
            "sequences": [
                {
                    "name": "主序列",
                    "clips": [
                        {
                            "name": "镜头1",
                            "start": 0.0,
                            "end": 5.0,
                            "asset_path": "/path/to/video.mp4",
                            "video_track": 1,
                            "audio_track": 1
                        }
                    ]
                }
            ]
        }
        """
        root = ET.Element("fcpxml")
        root.set("version", "1.9")

        # 审计 P2-25（2026-09-29）：整个方法重写为**合规 FCPXML**。旧实现自造元素
        # （spacetime / videospacetimerange / asset-ref / originalsequence，且无
        # DOCTYPE / resources / library / spine 结构），Premiere / Final Cut 根本
        # 导不进去 —— 用户却以为导出成功。现对齐 nle_export.export_fcpxml 的结构：
        # resources(format+asset) + library/event/project/sequence/spine，
        # 素材用 asset-clip（带 offset/duration），无素材的镜头用 gap 占位保持时间轴。
        proj_name = str(timeline.get("project_name") or self.project_key)
        fps = int(timeline.get("fps", 30) or 30)
        clips_info = (timeline.get("sequences") or [{}])[0].get("clips", [])
        width = int(timeline.get("resolution", {}).get("width", 1920) or 1920)
        height = int(timeline.get("resolution", {}).get("height", 1080) or 1080)

        from xml.sax.saxutils import escape as _xml_escape

        def _e(s) -> str:
            return _xml_escape(str(s or ""))

        assets: List[str] = []
        spine_clips: List[str] = []
        for i, clip_info in enumerate(clips_info):
            start = float(clip_info.get("start", 0) or 0)
            end = float(clip_info.get("end", 0) or 0)
            dur = max(0.0, end - start)
            dur_s = f"{int(round(dur * fps))}/{fps}s"
            off_s = f"{int(round(start * fps))}/{fps}s"
            clip_name = str(clip_info.get("name", f"clip{i + 1}"))
            asset_path = str(clip_info.get("asset_path") or "")
            if asset_path and Path(asset_path).is_file():
                aid = f"r{i + 2}"
                assets.append(
                    f'    <asset id="{aid}" name="{_e(clip_name)}" '
                    f'start="0s" duration="{dur_s}" hasVideo="1" format="r1">\n'
                    f'      <media-rep kind="original-media" '
                    f'src="file:///{_e(Path(asset_path).resolve().as_posix())}"/>\n'
                    f'    </asset>')
                spine_clips.append(
                    f'        <asset-clip ref="{aid}" offset="{off_s}" '
                    f'name="{_e(clip_name)}" duration="{dur_s}"/>')
            else:
                spine_clips.append(
                    f'        <gap name="{_e(clip_name)}" offset="{off_s}" '
                    f'duration="{dur_s}"/>')

        total_s = f"{int(round(float(timeline.get('duration', 0) or 0) * fps))}/{fps}s"
        xml_str = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE fcpxml>\n'
            '<fcpxml version="1.9">\n'
            '  <resources>\n'
            f'    <format id="r1" name="FFVideoFormat{width}x{height}" '
            f'frameDuration="1/{fps}s" width="{width}" height="{height}" '
            f'colorSpace="1-1-1 (Rec. 709)"/>\n'
            + ("\n".join(assets) + "\n" if assets else "")
            + '  </resources>\n'
            '  <library>\n'
            f'    <event name="{_e(proj_name)}">\n'
            f'      <project name="{_e(proj_name)}">\n'
            f'        <sequence format="r1" duration="{total_s}" tcStart="0s" tcFormat="NDF">\n'
            '          <spine>\n'
            + ("\n".join(spine_clips) + "\n" if spine_clips else "")
            + '          </spine>\n'
            '        </sequence>\n'
            '      </project>\n'
            '    </event>\n'
            '  </library>\n'
            '</fcpxml>\n'
        )

        # Save to file
        output_path = self.base_dir / f"{self.project_key}_fcpml.xml"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(xml_str)

        return str(output_path)

    def export_edl(self, timeline: Dict[str, Any]) -> str:
        """导出 EDL (Edit Decision List) 格式

        EDL 格式:
        001  CUT    00:00:00 00:00:05 00:00:00 00:00:05 * CLIP NAME
        """
        lines = []
        lines.append("FILENAME: {}\n".format(self.project_key))
        lines.append("FROM CLIP NAME:\n")

        clip_num = 1
        for seq in timeline.get("sequences", []):
            for clip in seq.get("clips", []):
                start = clip.get("start", 0)
                end = clip.get("end", 0)
                duration = end - start

                # 转换为时间码
                start_tc = self._seconds_to_timecode(start, timeline.get("fps", 30))
                end_tc = self._seconds_to_timecode(end, timeline.get("fps", 30))
                dur_tc = self._seconds_to_timecode(duration, timeline.get("fps", 30))

                line = "{:03d}  CUT    {} {} {} {} * {}\n".format(
                    clip_num, start_tc, end_tc, start_tc, dur_tc,
                    clip.get("name", "CLIP").upper()
                )
                lines.append(line)
                clip_num += 1

        output_path = self.base_dir / f"{self.project_key}_edl.edl"
        with open(output_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)

        return str(output_path)

    def export_json(self, timeline: Dict[str, Any]) -> str:
        """导出 JSON 格式 (完整元数据)"""
        output_path = self.base_dir / f"{self.project_key}_timeline.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(timeline, f, ensure_ascii=False, indent=2)
        return str(output_path)

    def export_all(self, timeline: Dict[str, Any]) -> Dict[str, str]:
        """导出所有格式"""
        exports = {}
        exports["fcpml"] = self.export_fcpml(timeline)
        exports["edl"] = self.export_edl(timeline)
        exports["json"] = self.export_json(timeline)
        return exports

    def _seconds_to_timecode(self, seconds: float, fps: int = 30) -> str:
        """秒数转时间码 HH:MM:SS:FF"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        frames = int((seconds % 1) * fps)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}:{frames:02d}"
