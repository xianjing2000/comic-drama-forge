# -*- coding: utf-8 -*-
"""t20 / D5：清理被测试写进**开发数据根**的污染记录（先备份，再删）。

背景：tests/test_config.py 的 CC.set_value(..., note="test-restore") 曾写进
仓库根 output/tasks.db 的 app_settings 表（t23 发现于 13:31；t20 复核复现于 16:37/16:39）。
本脚本可复现、可回退：
  1) 把整个 tasks.db 逐字节复制到 output/backup/（带时间戳）；
  2) 把被污染的 app_settings 行导出成 json（内容 + 删除依据）；
  3) 只删除**判定为测试产物**的行（note 以 test 开头），并写清理报告。

跑法：python tests/_t20_cleanup_pollution.py [--dry-run]
"""
import argparse
import datetime
import io
import json
import os
import shutil
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_ROOT, "output", "tasks.db")
_BACKUP_DIR = os.path.join(_ROOT, "output", "backup")

#: 判定为「测试产物」的依据：note 以 test 开头（test / test-restore / test-*）。
#: 只删这些；真实配置行（note 为空或非测试说明）一律不碰。
_TEST_NOTE_PREFIX = "test"


def _dump_rows(db):
    c = sqlite3.connect("file:" + db.replace(chr(92), "/") + "?mode=ro", uri=True)
    rows = [{"key": r[0], "value": r[1], "value_type": r[2],
             "updated_at": r[3], "note": r[4]}
            for r in c.execute("select key, value, value_type, updated_at, note"
                               " from app_settings")]
    c.close()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.isfile(_DB):
        print("没有开发数据根：%s" % _DB)
        return 0
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(_BACKUP_DIR, "tasks.db.t20-before-cleanup-%s" % stamp)
    rows_before = _dump_rows(_DB)
    polluted = [r for r in rows_before
                if str(r.get("note") or "").strip().lower().startswith(_TEST_NOTE_PREFIX)]
    print("备份 ->", bak)
    if not args.dry_run:
        shutil.copy2(_DB, bak)
    jreport = os.path.join(_BACKUP_DIR, "t20_pollution_rows_%s.json" % stamp)
    payload = {"db": _DB, "backup": bak, "polluted": polluted, "all_before": rows_before}
    if not args.dry_run:
        io.open(jreport, "w", encoding="utf-8").write(
            json.dumps(payload, ensure_ascii=False, indent=2))
    print("污染行 %d / 总行 %d" % (len(polluted), len(rows_before)))
    for r in polluted:
        print("   -", r["key"], "=", r["value"], "note=", r["note"], "at", r["updated_at"])
    if args.dry_run:
        print("[dry-run] 未改动任何文件")
        return 0
    c = sqlite3.connect(_DB, timeout=10)
    try:
        with c:
            for r in polluted:
                c.execute("delete from app_settings where key = ? and note = ?",
                          (r["key"], r["note"]))
    finally:
        c.close()
    rows_after = _dump_rows(_DB)
    print("清理后剩余 %d 行：" % len(rows_after))
    for r in rows_after:
        print("   *", r["key"], "=", r["value"], "note=", r["note"])
    report_path = os.path.join(_BACKUP_DIR, "t20_cleanup_report_%s.json" % stamp)
    io.open(report_path, "w", encoding="utf-8").write(json.dumps({
        "backup": bak, "rows_json": jreport,
        "deleted": polluted, "remaining": rows_after,
        "at": datetime.datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=2))
    print("报告 ->", report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
