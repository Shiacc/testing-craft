"""链接检查（独立脚本，避免命令行引号转义问题）。"""

import re
import pathlib

PATTERN = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")


def main() -> None:
    bad = []
    for md in pathlib.Path(".").rglob("*.md"):
        if ".git" in md.parts:
            continue
        for match in PATTERN.finditer(md.read_text(encoding="utf-8")):
            target = match.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                bad.append((str(md), target))
    print("broken links:", len(bad))
    for item in bad[:10]:
        print(" ", item)


if __name__ == "__main__":
    main()