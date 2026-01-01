#!/usr/bin/env -S uv run --script --no-project --quiet
"""
repo_token_estimator.py

Back-of-the-envelope token estimate for a git repo (or any directory).

Default behavior:
- Uses `git ls-files` (tracked files only)
- Filters to "relevant" text-like extensions
- Skips likely-binary files by a quick null-byte sniff
- Reports totals + per-extension breakdown + largest files

Token estimate:
- low  = bytes / 4  (optimistic)
- high = bytes / 3  (pessimistic)
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


DEFAULT_EXTS = [
    # Python
    ".py", ".pyi",
    # JS/TS
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # JVM
    ".java", ".kt", ".kts", ".scala",
    # Go/Rust/C/C++
    ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
    # Shell
    ".sh", ".bash", ".zsh",
    # Data / configs (often token-expensive but commonly "relevant")
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    # Docs
    ".md", ".rst", ".txt", ".adoc",
    # SQL
    ".sql",
    # Misc
    ".proto", ".graphql",
]

SKIP_FILENAMES = {
    # JS/TS
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pnpm-lock.yml",
    "deno.lock",
    "bun.lockb",
    "bun.lock",
    # Python
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    # Rust
    "Cargo.lock",
    # Ruby
    "Gemfile.lock",
    # PHP
    "composer.lock",
    # Go
    "go.sum",
    "go.work.sum",
}


@dataclass(frozen=True)
class FileStat:
    path: Path
    size: int
    ext: str


def is_probably_binary(path: Path, sniff_bytes: int = 4096) -> bool:
    """
    Cheap binary sniff: if the file contains a NUL byte in the first N bytes,
    treat it as binary.

    This is intentionally simple and fast. It will misclassify some files,
    but it avoids pulling in external deps.
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(sniff_bytes)
        return b"\x00" in chunk
    except (OSError, IOError):
        return True  # unreadable -> treat as irrelevant


def git_tracked_files(repo_root: Path) -> list[Path]:
    """
    Return tracked files in repo_root (like `git ls-files`).
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError("Not a git repo (or git not found). Use --all to scan directory.")

    parts = out.split(b"\0")
    paths: list[Path] = []
    for p in parts:
        if not p:
            continue
        s = p.decode("utf-8", "ignore")
        paths.append(repo_root / s)
    return paths


def walk_all_files(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        # skip common junk dirs
        d = Path(dirpath)
        # mutate dirnames in-place to prune walk
        prune = {
            ".git", ".hg", ".svn",
            "node_modules", ".venv", "venv",
            "__pycache__", ".pytest_cache", ".mypy_cache",
            "dist", "build", "target", ".gradle",
            ".idea", ".vscode",
        }
        dirnames[:] = [dn for dn in dirnames if dn not in prune]
        for fn in filenames:
            yield d / fn


def iter_filestats(
    paths: Iterable[Path],
    exts: set[str],
    root: Path,
    include_hidden: bool,
    binary_policy: str,
    exclude_globs: list[str],
) -> Iterator[FileStat]:
    """
    binary_policy:
      - "skip"   : skip probable binaries
      - "include": include all files regardless
    """
    for p in paths:
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p

        if not include_hidden:
            # skip hidden path segments ('.foo')
            if any(part.startswith(".") for part in rel.parts):
                continue

        if p.name in SKIP_FILENAMES:
            continue

        if exclude_globs:
            rel_posix = rel.as_posix()
            name = p.name
            skip = False
            for pat in exclude_globs:
                if any(ch in pat for ch in "*?[]"):
                    if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(name, pat):
                        skip = True
                        break
                else:
                    # Treat plain strings as substring match on filename or path
                    if pat in name or pat in rel_posix:
                        skip = True
                        break
            if skip:
                continue

        ext = p.suffix.lower()
        if exts and ext not in exts:
            continue

        if binary_policy == "skip" and is_probably_binary(p):
            continue

        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        if not st or st.st_size <= 0:
            continue

        yield FileStat(path=p, size=int(st.st_size), ext=ext or "<none>")


def fmt_int(n: float | int) -> str:
    return f"{int(n):,}"


def estimate_tokens(size_bytes: int) -> tuple[int, int]:
    low = int(size_bytes / 4)
    high = int(size_bytes / 3)
    return low, high


def print_table(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for idx, r in enumerate(rows):
        line = "  ".join(r[i].rjust(widths[i]) if i else r[i].ljust(widths[i]) for i in range(len(r)))
        print(line)
        if idx == 0:
            print("  ".join("-" * w for w in widths))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Estimate tokens for a repo/directory.")
    ap.add_argument("path", nargs="?", default=".", help="Repo/directory root (default: .)")
    ap.add_argument("--all", action="store_true",
                    help="Scan all files under PATH (not just git-tracked).")
    ap.add_argument("--ext", action="append", default=None,
                    help="Include only these extensions (repeatable), e.g. --ext .py --ext .md")
    ap.add_argument("--no-ext-filter", action="store_true",
                    help="Do not filter by extension (danger: includes lots of stuff).")
    ap.add_argument("--include-hidden", action="store_true",
                    help="Include hidden files/dirs (dotfiles).")
    ap.add_argument("--exclude", "-x", action="append", default=[],
                    help="Glob pattern(s) to exclude, repeatable (e.g. -x '*.lock' -x 'dist/**').")
    ap.add_argument("--binary", choices=["skip", "include"], default="skip",
                    help="Binary handling: skip (default) or include.")
    ap.add_argument("--top", type=int, default=10, help="Show top N largest files (default: 10)")
    args = ap.parse_args(argv)

    root = Path(args.path).resolve()

    if args.no_ext_filter:
        exts: set[str] = set()
    elif args.ext:
        exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in args.ext}
    else:
        exts = set(DEFAULT_EXTS)

    # Collect candidate paths
    if args.all:
        paths = list(walk_all_files(root))
    else:
        try:
            paths = git_tracked_files(root)
        except RuntimeError as e:
            print(f"warning: {e}", file=sys.stderr)
            print("falling back to --all scan.", file=sys.stderr)
            paths = list(walk_all_files(root))

    stats = list(iter_filestats(
        paths=paths,
        exts=exts,
        root=root,
        include_hidden=args.include_hidden,
        binary_policy=args.binary,
        exclude_globs=args.exclude,
    ))

    total_bytes = sum(s.size for s in stats)
    total_files = len(stats)
    t_low, t_high = estimate_tokens(total_bytes)

    print(f"Root: {root}")
    print(f"Files counted: {total_files}")
    print(f"Bytes counted: {fmt_int(total_bytes)}")
    print()

    # Per-extension breakdown
    by_ext: dict[str, list[FileStat]] = defaultdict(list)
    for s in stats:
        by_ext[s.ext].append(s)

    rows: list[list[str]] = [["ext", "files", "bytes", "tok_low", "tok_high"]]
    for ext in sorted(by_ext.keys(), key=lambda k: sum(x.size for x in by_ext[k]), reverse=True):
        b = sum(x.size for x in by_ext[ext])
        lo, hi = estimate_tokens(b)
        rows.append([ext, fmt_int(len(by_ext[ext])), fmt_int(b), fmt_int(lo), fmt_int(hi)])

    print("By extension:")
    print_table(rows)
    print()

    # Largest files
    topn = sorted(stats, key=lambda s: s.size, reverse=True)[: max(args.top, 0)]
    if topn:
        print(f"Top {len(topn)} largest files:")
        rows2 = [["bytes", "tok_low", "tok_high", "path"]]
        for s in topn:
            lo, hi = estimate_tokens(s.size)
            rel = s.path.relative_to(root) if s.path.is_relative_to(root) else s.path
            rows2.append([fmt_int(s.size), fmt_int(lo), fmt_int(hi), str(rel)])
        print_table(rows2)

    avg = int((t_low + t_high) / 2)
    spread = 0.0 if avg == 0 else (t_high - avg) / avg * 100
    print()
    print(f"Tokens: {fmt_int(avg)} (+/-{int(round(spread))}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
