#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""密钥泄漏扫描器（只读）。

扫描五个面，一律只输出「打码后」的片段，绝不打印完整密钥：

  A 工作区文本文件      —— 源码/配置/脚本里的明文密钥
  B git 历史            —— 曾提交过的密钥（即使后来删除，仍留在对象库里）
  C git 本地元数据      —— .git/config、reflog、packed-refs 里内嵌的 URL 凭据
  D DSH 会话记录        —— %DSH_HOME%/sessions/**/session.v4.jsonl.zstd（多帧 zstd）
  E Windows 凭据管理器  —— 只列 target/user 条目名，不读取任何密码

本工具不联网、不写文件（--json 除外，且由调用方显式指定）。

用法::

    python tools/scan_secrets.py
    python tools/scan_secrets.py --root . --json reports/secrets.json
    python tools/scan_secrets.py --no-sessions          # 跳过会话库扫描

退出码：0 = 只读面干净或仅有 low 级发现；1 = 存在 medium/high 级发现。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------- 检测规则

# 说明：这里用「前缀 + 字符类」而不是完整密钥字面量，
# 因此本文件自身不会被这些规则命中（前缀后面跟的是 '['，不属于字符类）。
PATTERNS = [
    ("github_fine_grained_pat", "high", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_classic_pat", "high", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("github_oauth_token", "high", re.compile(r"gho_[A-Za-z0-9]{30,}")),
    ("github_app_token", "high", re.compile(r"gh[usr]_[A-Za-z0-9]{30,}")),
    ("openai_style_key", "high", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws_access_key_id", "high", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private_key_block", "high", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", "low", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
]

# git 历史扫描：-S（pickaxe）只做粗筛，命中提交再用 git grep <正则> 精确确认。
# 粗筛前缀与精确正则分开写，扫描器自身的正则字面量因此不会被误判为命中。
HISTORY_PROBES = [
    ("github_fine_grained_pat", "github_pat_", r"github_pat_[A-Za-z0-9_]{20,}"),
    ("github_classic_pat", "ghp_", r"ghp_[A-Za-z0-9]{30,}"),
    ("github_oauth_token", "gho_", r"gho_[A-Za-z0-9]{30,}"),
    ("aws_access_key_id", "AKIA", r"AKIA[0-9A-Z]{16}"),
    ("private_key_block", "-----BEGIN", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]

KEYWORD_RE = re.compile(
    r"""(?ix) \b (api[_-]?key | apikey | secret[_-]?key | client[_-]?secret
        | access[_-]?token | auth[_-]?token | password | passwd | pwd)
        \b \s* [:=] \s* ["']? ([A-Za-z0-9_\-+/=.]{24,}) ["']? """,
)

# 明显是占位符/示例的，不报
PLACEHOLDER_RE = re.compile(
    r"(?i)(your[_-]?|my[_-]?|example|sample|placeholder|changeme|dummy|fake|test[_-]?key|xxxx|\.\.\.|<\w+>)"
)

TEXT_EXTS = {
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".jsonl",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".md", ".txt", ".log", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".bash",
    ".sql", ".html", ".htm", ".css", ".xml", ".kv", ".dockerfile", ".gitignore",
}

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".idea",
    ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
    "dist", "build", ".next", ".nuxt", "coverage", ".tox",
}

MAX_FILE_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------- 工具函数

def mask(secret: str) -> str:
    """把密钥打码成 前6…后4 (len=N)，保证日志里不出现完整值。"""
    s = secret.strip()
    if len(s) <= 12:
        return "%s (len=%d)" % ("*" * len(s), len(s))
    return "%s…%s (len=%d)" % (s[:6], s[-4:], len(s))


def _looks_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192]


def _iter_text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in TEXT_EXTS and name not in (
                ".env", ".gitignore", "Dockerfile", "Makefile",
            ):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield p


def scan_text(text: str):
    """在文本中查找密钥规则，返回 [(kind, severity, masked, line_no)]。"""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, sev, rx in PATTERNS:
            for m in rx.finditer(line):
                out.append((kind, sev, mask(m.group(0)), lineno))
        for m in KEYWORD_RE.finditer(line):
            value = m.group(2)
            if PLACEHOLDER_RE.search(line):
                continue
            if len(set(value)) < 8:          # aaaa... 之类的噪声
                continue
            out.append(("keyword_assignment", "medium", mask(value), lineno))
    return out


def scan_tree(root: Path):
    findings = []
    for p in _iter_text_files(root):
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        if _looks_binary(blob):
            continue
        try:
            text = blob.decode("utf-8", errors="replace")
        except Exception:
            continue
        for kind, sev, masked, lineno in scan_text(text):
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                rel = str(p)
            findings.append({
                "surface": "workspace", "path": rel, "line": lineno,
                "kind": kind, "severity": sev, "masked": masked,
            })
    return findings


# ---------------------------------------------------------------- git 面

def _git(root: Path, *args: str, timeout: int = 300):
    exe = shutil.which("git") or "git"
    try:
        cp = subprocess.run(
            [exe, *args], cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return cp.returncode, cp.stdout, cp.stderr
    except Exception as exc:                                   # pragma: no cover
        return 127, "", str(exc)


def scan_git_metadata(root: Path):
    """C 面：.git/config、reflog、packed-refs 中的内嵌凭据。"""
    findings = []
    targets = [root / ".git" / "config", root / ".git" / "packed-refs"]
    logs = root / ".git" / "logs"
    if logs.is_dir():
        targets.extend(sorted(logs.rglob("*")))
    for p in targets:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, sev, rx in PATTERNS:
                for m in rx.finditer(line):
                    findings.append({
                        "surface": "git_metadata", "path": rel,
                        "line": lineno, "kind": kind, "severity": sev,
                        "masked": mask(m.group(0)),
                    })
            # https://user:password@host  形式的内嵌凭据
            m = re.search(r"https://([^/\s:@]+):([^/\s@]+)@", line)
            if m:
                findings.append({
                    "surface": "git_metadata", "path": rel,
                    "line": lineno, "kind": "url_embedded_credential",
                    "severity": "high",
                    "masked": "user=%s pass=%s" % (m.group(1), mask(m.group(2))),
                })
    return findings


def scan_git_history(root: Path, max_commits_per_probe: int = 20):
    """B 面：git 历史里是否真的出现过密钥。

    先用 -S（pickaxe）粗筛提交；pickaxe 只看「字符串出现次数是否变化」，
    对 github_pat_ 这类前缀会连扫描器自己的源码一起命中，所以每个候选
    提交还要用 git grep <正则> 在其完整文件树里确认，只有正则真命中才报告。
    """
    findings = []
    for kind, prefix, regex in HISTORY_PROBES:
        rc, out, err = _git(root, "log", "--all", "--format=%H", "-S" + prefix)
        if rc != 0:
            findings.append({
                "surface": "git_history", "path": prefix, "line": 0,
                "kind": "scan_error", "severity": "low", "masked": (err or "")[:120],
            })
            continue
        commits = [c.strip() for c in out.split() if c.strip()]
        if not commits:
            continue
        confirmed = 0
        for commit in commits[:max_commits_per_probe]:
            rc2, out2, _ = _git(root, "grep", "--no-color", "-n", "-I", "-E", regex, commit)
            if rc2 != 0 or not out2.strip():
                continue
            confirmed += 1
            for line in out2.splitlines()[:3]:
                m = re.search(regex, line)
                if not m:
                    continue
                findings.append({
                    "surface": "git_history", "path": commit[:10] + " " + kind,
                    "line": 0, "kind": kind + "_in_history", "severity": "high",
                    "masked": mask(m.group(0)),
                })
        if confirmed == 0:
            findings.append({
                "surface": "git_history", "path": prefix, "line": 0,
                "kind": "prefix_seen_but_no_real_match", "severity": "info",
                "masked": "%d 个提交中出现过该字符串，均非真实密钥形态" % len(commits),
            })
    return findings


# ---------------------------------------------------------------- DSH 会话面

_NODE_SCAN = r"""
const fs = require('fs'), path = require('path'), zlib = require('zlib');
const root = process.argv[2] || process.argv[1];
const rules = [
  ['github_pat_', /github_pat_[A-Za-z0-9_]{20,}/g],
  ['ghp_', /ghp_[A-Za-z0-9]{30,}/g],
  ['gho_', /gho_[A-Za-z0-9]{30,}/g],
  ['AKIA', /AKIA[0-9A-Z]{16}/g],
  ['sk_key', /sk-(?:proj-)?[A-Za-z0-9]{32,}/g],
];
const MAGIC = Buffer.from([0x28, 0xB5, 0x2F, 0xFD]);
function walk(d, out) { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
  const p = path.join(d, e.name); if (e.isDirectory()) walk(p, out); else out.push(p); } return out; }
var files = [];
try { files = walk(root, []).filter(function (f) { return f.endsWith('.zstd'); }); }
catch (e) { console.log('[]'); process.exit(0); }
const res = [];
for (const f of files) {
  const buf = fs.readFileSync(f);
  const offs = []; let i = 0;
  while (true) { const k = buf.indexOf(MAGIC, i); if (k < 0) break; offs.push(k); i = k + 4; }
  let text = '';
  for (let k = 0; k < offs.length; k++) {
    const end = k + 1 < offs.length ? offs[k + 1] : buf.length;
    try { text += zlib.zstdDecompressSync(buf.subarray(offs[k], end)).toString('utf8'); } catch (e) {}
  }
  const hits = [];
  for (const rule of rules) {
    const pre = rule[0], rx = rule[1];
    rx.lastIndex = 0;
    let m, n = 0;
    while ((m = rx.exec(text)) !== null && n < 5) {
      n++;
      const s = Math.max(0, m.index - 60);
      hits.push({ prefix: pre, at: m.index, ctx: text.slice(s, m.index).replace(/\s+/g, ' ') });
    }
  }
  if (hits.length) res.push({ file: f, frames: offs.length, chars: text.length, hits: hits });
}
console.log(JSON.stringify(res));
"""


def find_node():
    return shutil.which("node") or shutil.which("node.exe")


def scan_sessions(session_root: Path, node_exe=None):
    """D 面：DSH 会话记录（多帧 zstd）中的密钥。需要 node。"""
    if not session_root.is_dir():
        return [], "会话目录不存在：%s" % session_root
    node_exe = node_exe or find_node()
    if not node_exe:
        return [], "未找到 node，跳过会话库扫描（会话文件为多帧 zstd，Python 标准库不支持）"
    try:
        cp = subprocess.run(
            [node_exe, "-e", _NODE_SCAN, str(session_root)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
    except Exception as exc:
        return [], "会话库扫描失败：%s" % exc
    if cp.returncode != 0:
        return [], "会话库扫描失败：%s" % (cp.stderr or "")[:200]
    try:
        raw = json.loads(cp.stdout.strip() or "[]")
    except Exception as exc:
        return [], "会话库扫描输出无法解析：%s" % exc
    findings = []
    for item in raw:
        for hit in item["hits"]:
            ctx = hit["ctx"][-60:]
            findings.append({
                "surface": "dsh_session",
                "path": Path(item["file"]).parent.name,
                "line": 0,
                "kind": "session_contains_" + hit["prefix"].strip("-").lower(),
                "severity": "high",
                "masked": "偏移 %d，命中点前文：…%s ← [%s]（本体不打印）"
                          % (hit["at"], ctx, hit["prefix"][:6] + "…"),
            })
    return findings, None


# ---------------------------------------------------------------- 凭据管理器

def scan_credential_manager():
    """E 面：只列条目名，绝不调用 CredRead 读取密码。"""
    if os.name != "nt":
        return [], "非 Windows，跳过"
    try:
        cp = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    except Exception as exc:
        return [], "cmdkey 执行失败：%s" % exc
    entries = []
    cur = {}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("target:"):
            if cur:
                entries.append(cur)
            cur = {"target": line.split(":", 1)[1].strip()}
        elif line.lower().startswith("user:") and cur:
            cur["user"] = line.split(":", 1)[1].strip()
    if cur:
        entries.append(cur)
    findings = [{
        "surface": "credential_manager", "path": e.get("target", ""), "line": 0,
        "kind": "stored_credential", "severity": "info",
        "masked": "user=%s（值未读取）" % e.get("user", "-"),
    } for e in entries if e.get("target")]
    return findings, None


# ---------------------------------------------------------------- 主流程

def run(root: Path, sessions, json_out=None):
    # Windows 控制台默认 GBK，遇到会话记录里的 ✓ / emoji 会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    root = Path(root)
    all_findings = []
    notes = []

    all_findings += scan_tree(root)
    if (root / ".git").is_dir():
        all_findings += scan_git_metadata(root)
        all_findings += scan_git_history(root)
    else:
        notes.append("未发现 .git，跳过历史扫描")

    if sessions is not None:
        f, note = scan_sessions(Path(sessions))
        all_findings += f
        if note:
            notes.append(note)

    f, note = scan_credential_manager()
    all_findings += f
    if note:
        notes.append(note)

    sev_rank = {"high": 3, "medium": 2, "low": 1, "info": 0}
    worst = max([sev_rank.get(x["severity"], 0) for x in all_findings] or [0])

    print("=" * 72)
    print("密钥泄漏扫描报告")
    print("  工作区 : %s" % root)
    print("  会话库 : %s" % (sessions or "(已跳过)"))
    print("=" * 72)

    order = ["workspace", "git_history", "git_metadata", "dsh_session",
             "credential_manager"]
    for surface in order:
        rows = [x for x in all_findings if x["surface"] == surface]
        if surface == "credential_manager":
            print("\n[E] Windows 凭据管理器：%d 条存储凭据（只列名字）" % len(rows))
            for r in rows:
                print("    - %s  %s" % (r["path"], r["masked"]))
            continue
        if not rows:
            print("\n[%s] 干净" % surface)
            continue
        print("\n[%s] %d 处发现" % (surface, len(rows)))
        for r in rows[:40]:
            loc = "%s:%s" % (r["path"], r["line"]) if r["line"] else r["path"]
            print("    - [%s] %-28s %s" % (r["severity"], r["kind"], loc))
            print("        %s" % r["masked"])
        if len(rows) > 40:
            print("    … 另有 %d 处" % (len(rows) - 40))

    if notes:
        print("\n备注：")
        for n in notes:
            print("  * %s" % n)

    verdict = ["干净", "低风险", "中风险", "高风险"][worst]
    print("\n结论：%s（%d 处发现）" % (verdict, len(all_findings)))

    if json_out:
        json_out = Path(json_out)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(
            {"root": str(root), "sessions": str(sessions) if sessions else None,
             "findings": all_findings, "notes": notes, "verdict": verdict},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print("JSON 报告：%s" % json_out)

    return 1 if worst >= 2 else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="密钥泄漏扫描（只读）")
    ap.add_argument("--root", default=".", help="工作区根目录")
    ap.add_argument("--sessions", default=None, help="DSH 会话库目录（默认取 $DSH_HOME/sessions）")
    ap.add_argument("--no-sessions", action="store_true", help="跳过会话库扫描")
    ap.add_argument("--json", default=None, help="把完整报告写成 JSON")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if args.no_sessions:
        sessions = None
    elif args.sessions:
        sessions = Path(args.sessions).resolve()
    else:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
        sessions = Path(home) / "sessions"

    json_out = Path(args.json).resolve() if args.json else None
    return run(root, sessions, json_out)


if __name__ == "__main__":
    sys.exit(main())
