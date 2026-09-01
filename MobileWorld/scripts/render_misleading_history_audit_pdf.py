"""Render the self-contained six-model audit report.

Every report image must be a repository-local, content-addressed PNG at the exact relative path
``report_assets/screenshots/<sha256>.png``. The renderer rejects absolute paths, URIs, traversal,
symlinks, and non-regular image files before invoking XeLaTeX. It never replaces an existing build
or output path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

IMAGE_RE = re.compile(r"^!\[(?P<alt>.*?)]\((?P<path>[^)]+)\)$")
REPORT_IMAGE_PATH_RE = re.compile(r"^report_assets/screenshots/(?P<sha256>[0-9a-f]{64})\.png$")
HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
INLINE_RE = re.compile(r"(\*\*.*?\*\*|`[^`]*`|\[[^]]+]\([^)]+\))")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_report_image(markdown_path: Path, raw_path: str) -> tuple[Path, str]:
    """Resolve and authenticate one strict repository-local report image reference."""

    match = REPORT_IMAGE_PATH_RE.fullmatch(raw_path)
    if match is None:
        raise ValueError(
            "report images must use "
            "report_assets/screenshots/<lowercase-sha256>.png; "
            f"rejected: {raw_path!r}"
        )

    report_dir = markdown_path.parent.resolve(strict=True)
    source = report_dir / raw_path
    cursor = report_dir
    for component in raw_path.split("/"):
        cursor /= component
        metadata = cursor.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"report image path must not contain a symlink: {source}")

    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"report image must be a regular file: {source}")
    try:
        source.resolve(strict=True).relative_to(report_dir)
    except ValueError as error:
        raise RuntimeError(f"report image escapes the report directory: {source}") from error

    expected_digest = match.group("sha256")
    observed_digest = sha256_file(source)
    if observed_digest != expected_digest:
        raise RuntimeError(
            "report image digest/filename mismatch: "
            f"expected {expected_digest}, observed {observed_digest}"
        )
    return source, observed_digest


def escape_tex(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def inline_tex(text: str) -> str:
    parts: list[str] = []
    cursor = 0
    for match in INLINE_RE.finditer(text):
        parts.append(escape_tex(text[cursor : match.start()]))
        token = match.group(0)
        if token.startswith("**"):
            parts.append(r"\textbf{" + escape_tex(token[2:-2]) + "}")
        elif token.startswith("`"):
            parts.append(r"\texttt{" + escape_tex(token[1:-1]) + "}")
        else:
            link = re.match(r"^\[([^]]+)]\(([^)]+)\)$", token)
            assert link is not None
            parts.append(
                r"\href{" + escape_tex(link.group(2)) + "}{" + escape_tex(link.group(1)) + "}"
            )
        cursor = match.end()
    parts.append(escape_tex(text[cursor:]))
    return "".join(parts)


def is_table_separator(line: str) -> bool:
    if not line.lstrip().startswith("|"):
        return False
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def image_tex(entries: list[tuple[str, Path, str]], figure_dir: Path) -> str:
    rendered: list[tuple[str, Path]] = []
    for index, (alt, source, digest) in enumerate(entries, start=1):
        target = figure_dir / f"figure-{digest[:12]}-{index}.png"
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(source, target)
        rendered.append((alt, target))

    chunks = [r"\begin{center}", r"\setlength{\fboxsep}{1pt}"]
    width = "0.47\\textwidth" if len(rendered) == 2 else "0.62\\textwidth"
    for position, (alt, target) in enumerate(rendered):
        if len(rendered) == 2:
            chunks.append(r"\begin{minipage}[t]{0.48\textwidth}\centering")
        chunks.append(
            r"\fbox{\includegraphics[width="
            + width
            + r",height=0.54\textheight,keepaspectratio]{"
            + escape_tex(str(target))
            + "}}"
        )
        chunks.append(r"\\[-1pt]{\scriptsize\color{gray}" + escape_tex(alt) + "}")
        if len(rendered) == 2:
            chunks.append(r"\end{minipage}")
            if position == 0:
                chunks.append(r"\hfill")
    chunks.append(r"\end{center}")
    return "\n".join(chunks)


def markdown_to_tex(markdown: str, figure_dir: Path, markdown_path: Path) -> str:
    lines = markdown.splitlines()
    body: list[str] = []
    index = 0
    title = "MobileWorld Misleading History Reuse Audit"

    def special(line: str) -> bool:
        return bool(
            HEADING_RE.match(line)
            or line.startswith("![")
            or line.startswith("- ")
            or (line.lstrip().startswith("|") and "|" in line.lstrip()[1:])
        )

    while index < len(lines):
        line = lines[index].rstrip()
        if not line:
            index += 1
            continue

        image = IMAGE_RE.fullmatch(line)
        if line.startswith("![") and image is None:
            raise ValueError(f"unsupported report image syntax: {line!r}")

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            if level == 1:
                title = text
            else:
                command = {2: "section", 3: "subsection", 4: "subsubsection"}[level]
                body.append(f"\\{command}{{{inline_tex(text)}}}")
            index += 1
            continue

        if image:
            entries: list[tuple[str, Path, str]] = []
            cursor = index
            while cursor < len(lines):
                candidate = lines[cursor].rstrip()
                if not candidate:
                    cursor += 1
                    continue
                image_candidate = IMAGE_RE.fullmatch(candidate)
                if not image_candidate or len(entries) == 2:
                    break
                source, digest = resolve_report_image(markdown_path, image_candidate.group("path"))
                entries.append((image_candidate.group("alt"), source, digest))
                cursor += 1
            body.append(image_tex(entries, figure_dir))
            index = cursor
            continue

        if line.startswith("- "):
            items: list[str] = []
            while index < len(lines) and lines[index].startswith("- "):
                item_parts = [lines[index][2:].strip()]
                index += 1
                while index < len(lines):
                    continuation = lines[index].rstrip()
                    if not continuation or continuation.startswith("- ") or special(continuation):
                        break
                    item_parts.append(continuation.strip())
                    index += 1
                items.append(" ".join(item_parts))
                while index < len(lines) and not lines[index].strip():
                    index += 1
            body.append(r"\begin{itemize}")
            body.extend(r"\item " + inline_tex(item) for item in items)
            body.append(r"\end{itemize}")
            continue

        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and is_table_separator(lines[index + 1])
        ):
            rows = [split_table_row(line)]
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                rows.append(split_table_row(lines[index]))
                index += 1
            columns = len(rows[0])
            if any(len(row) != columns for row in rows):
                raise RuntimeError("Inconsistent Markdown table width")
            specification = "|" + "|".join([r">{\raggedright\arraybackslash}X"] * columns) + "|"
            body.extend(
                [
                    r"\begin{center}",
                    r"\small",
                    r"\renewcommand{\arraystretch}{1.18}",
                    r"\begin{tabularx}{\textwidth}{" + specification + "}",
                    r"\hline",
                ]
            )
            for row_index, row in enumerate(rows):
                body.append(" & ".join(inline_tex(cell) for cell in row) + r" \\ \hline")
                if row_index == 0:
                    body.append(r"\noalign{\vskip 1pt}")
            body.extend([r"\end{tabularx}", r"\end{center}"])
            continue

        paragraph = [line.strip()]
        index += 1
        while index < len(lines):
            continuation = lines[index].rstrip()
            if not continuation or special(continuation):
                break
            paragraph.append(continuation.strip())
            index += 1
        text = " ".join(paragraph)
        if text.startswith("*") and text.endswith("*") and not text.startswith("**"):
            body.append(r"\begin{quote}\small\itshape " + inline_tex(text[1:-1]) + r"\end{quote}")
        else:
            body.append(inline_tex(text) + r"\par")

    preamble = r"""\documentclass[10.5pt,a4paper]{article}
\usepackage[margin=18mm,headheight=14pt]{geometry}
\usepackage{fontspec}
\usepackage{xeCJK}
\setmainfont{TeX Gyre Heros}
\setsansfont{TeX Gyre Heros}
\setmonofont{DejaVu Sans Mono}[Scale=0.86]
\setCJKmainfont{Noto Sans CJK SC}
\setCJKsansfont{Noto Sans CJK SC}
\usepackage{graphicx}
\usepackage{xcolor}
\usepackage{array}
\usepackage{tabularx}
\usepackage{booktabs}
\usepackage{enumitem}
\usepackage{fancyhdr}
\usepackage{hyperref}
\hypersetup{colorlinks=true,linkcolor=blue!45!black,urlcolor=blue!50!black}
\definecolor{heading}{HTML}{183153}
\definecolor{rulegray}{HTML}{D9E0E8}
\usepackage{titlesec}
\titleformat{\section}{\Large\bfseries\color{heading}}{}{0pt}{}
\titleformat{\subsection}{\large\bfseries\color{heading}}{}{0pt}{}
\titleformat{\subsubsection}{\normalsize\bfseries\color{heading}}{}{0pt}{}
\titlespacing*{\section}{0pt}{1.8em}{0.6em}
\titlespacing*{\subsection}{0pt}{1.3em}{0.4em}
\titlespacing*{\subsubsection}{0pt}{1.0em}{0.25em}
\setlist[itemize]{leftmargin=1.5em,itemsep=0.2em,topsep=0.35em}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.55em}
\setlength{\emergencystretch}{3em}
\sloppy
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\small MobileWorld History Audit}
\fancyhead[R]{\small Six audited MobileWorld agents}
\fancyfoot[C]{\thepage}
\begin{document}
"""
    title_page = (
        r"\begin{titlepage}\centering\vspace*{0.18\textheight}"
        + r"{\Huge\bfseries\color{heading} "
        + inline_tex(title)
        + r"\par}\vspace{1.2cm}"
        + r"{\Large Evidence Report\par}\vspace{0.8cm}"
        + r"{\large MAI-UI-8B, Qwen3-VL-8B, GELab-Zero-4B,\par}"
        + r"{\large UI-Venus-1.5-8B, GUI-Owl-1.5-8B-Instruct, and MemGUI-8B-SFT\par}\vfill"
        + r"{\small Rendered 2026-08-25 UTC\par}\end{titlepage}"
        + "\\tableofcontents\\clearpage\n"
    )
    return preamble + title_page + "\n".join(body) + "\n\\end{document}\n"


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: render_misleading_history_audit_pdf.py REPORT.md BUILD_DIR OUTPUT.pdf"
        )
    markdown_path = Path(sys.argv[1]).resolve()
    build_dir = Path(sys.argv[2]).resolve()
    output_path = Path(sys.argv[3]).resolve()
    if not markdown_path.is_file():
        raise FileNotFoundError(markdown_path)
    if build_dir.exists() or build_dir.is_symlink():
        raise FileExistsError(f"build directory must be absent: {build_dir}")
    if output_path.exists() or output_path.is_symlink():
        raise FileExistsError(f"output path must be absent: {output_path}")
    if not output_path.parent.is_dir():
        raise FileNotFoundError(output_path.parent)
    build_dir.mkdir(parents=True)
    figure_dir = build_dir / "figures"
    figure_dir.mkdir()
    tex_path = build_dir / "report.tex"
    tex_path.write_text(
        markdown_to_tex(markdown_path.read_text(encoding="utf-8"), figure_dir, markdown_path),
        encoding="utf-8",
    )
    command = [
        "xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-output-directory",
        str(build_dir),
        str(tex_path),
    ]
    build_environment = dict(os.environ)
    build_environment.setdefault("SOURCE_DATE_EPOCH", "1756080000")
    build_environment.setdefault("TZ", "UTC")
    for _ in range(2):
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=180,
            env=build_environment,
        )
        (build_dir / "xelatex.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (build_dir / "xelatex.stderr.log").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            tail = "\n".join(completed.stdout.splitlines()[-50:])
            raise RuntimeError(f"xelatex failed ({completed.returncode}):\n{tail}")
    produced = build_dir / "report.pdf"
    if not produced.is_file() or produced.stat().st_size == 0:
        raise RuntimeError("xelatex produced no PDF")
    with produced.open("rb") as source, output_path.open("xb") as destination:
        shutil.copyfileobj(source, destination)
    print(f"PDF={output_path}")
    print(f"BYTES={output_path.stat().st_size}")


if __name__ == "__main__":
    main()
