"""Render the offline demo (and the test run) into PNG "run screenshots".

Usage::

    python demo/render_screenshots.py

Writes ``docs/screenshots/*.png``. Rendering uses Pillow and the Consolas font
that ships with Windows, so no browser or screenshot tooling is required.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_stdio_smoke import run_smoke
from offline_demo import Screen, build_screens
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "docs" / "screenshots"

FONT_REGULAR = Path(r"C:\Windows\Fonts\consola.ttf")
FONT_BOLD = Path(r"C:\Windows\Fonts\consolab.ttf")

FONT_SIZE = 15
LINE_HEIGHT = 21
PADDING = 22
TITLE_BAR = 34
MIN_WIDTH = 900
MAX_WIDTH = 2200

BG = (12, 12, 12)
TITLE_BG = (30, 33, 39)
BORDER = (58, 63, 70)
TEXT = (212, 212, 212)
ACCENT = (86, 182, 255)
SUCCESS = (106, 202, 122)
FAILURE = (247, 118, 142)
HEADING = (255, 208, 120)
DIM = (140, 146, 155)


@dataclass
class Palette:
    """Colour chosen for one line of output."""

    fill: tuple[int, int, int]
    bold: bool = False


def classify(line: str) -> Palette:
    """Pick a colour for a transcript line.

    Args:
        line: The rendered line of text.

    Returns:
        Palette: Colours to draw the line with.
    """
    stripped = line.strip()
    if stripped.startswith("###"):
        return Palette(HEADING, bold=True)
    if stripped.startswith("# "):
        return Palette(ACCENT)
    if "[OK" in line:
        return Palette(SUCCESS)
    if "[FAIL" in line:
        return Palette(FAILURE)
    if stripped.startswith(("status", "service", "environment", "circuit", "rate limiter")):
        return Palette(ACCENT)
    if stripped.startswith("pg_mcp_"):
        return Palette(DIM)
    return Palette(TEXT)


def load_fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """Load the regular and bold monospace fonts.

    Returns:
        tuple: ``(regular, bold)`` fonts.

    Raises:
        FileNotFoundError: If no suitable font is available.
    """
    if not FONT_REGULAR.exists():
        raise FileNotFoundError(f"Monospace font not found: {FONT_REGULAR}")
    regular = ImageFont.truetype(str(FONT_REGULAR), FONT_SIZE)
    bold = ImageFont.truetype(str(FONT_BOLD), FONT_SIZE) if FONT_BOLD.exists() else regular
    return regular, bold


def slugify(text: str) -> str:
    """Turn a screen title into a filesystem-friendly slug."""
    cleaned = "".join(ch if ch.isalnum() or ch in " -._" else "" for ch in text).strip()
    return "-".join(cleaned.lower().replace("_", "-").split())[:60]


def render_screen(screen: Screen, index: int, total: int) -> Path:
    """Render one screen to a PNG.

    Args:
        screen: The transcript chunk.
        index: 1-based index used in the file name.
        total: Total number of screens.

    Returns:
        Path: The written PNG path.
    """
    regular, bold = load_fonts()
    lines = screen.text().splitlines()

    measured = max(regular.getlength(line) for line in lines) if lines else MIN_WIDTH
    content_width = int(measured) + PADDING * 2
    width = max(MIN_WIDTH, min(MAX_WIDTH, content_width))
    height = TITLE_BAR + PADDING * 2 + LINE_HEIGHT * len(lines)

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    # Title bar
    draw.rectangle([0, 0, width, TITLE_BAR], fill=TITLE_BG)
    for offset, colour in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = 16 + offset * 20
        draw.ellipse([cx, TITLE_BAR // 2 - 6, cx + 12, TITLE_BAR // 2 + 6], fill=colour)
    draw.text(
        (84, TITLE_BAR // 2 - 8),
        f"pg-mcp  |  {screen.title}  [{index}/{total}]",
        font=bold,
        fill=TEXT,
    )

    y = TITLE_BAR + PADDING
    for line in lines:
        palette = classify(line)
        draw.text(
            (PADDING, y),
            line.replace("\t", "    "),
            font=bold if palette.bold else regular,
            fill=palette.fill,
        )
        y += LINE_HEIGHT

    draw.rectangle([0, 0, width - 1, height - 1], outline=BORDER)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{index:02d}-{slugify(screen.title)}.png"
    image.save(path)
    return path


def test_run_screen() -> Screen:
    """Run the offline test suite and capture its summary as a screen.

    Returns:
        Screen: Transcript chunk containing the pytest summary.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "--tb=no",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    stdout = completed.stdout.strip().splitlines()
    stderr_tail = [line for line in completed.stderr.strip().splitlines() if line.strip()]

    lines = [
        "Command: .venv\\Scripts\\python.exe -m pytest -q --no-header",
        "Working directory: pg-mcp/",
        "",
        *stdout[-16:],
        "",
        f"exit code: {completed.returncode}",
    ]
    if stderr_tail:
        lines.extend(["", "stderr:", *stderr_tail[-4:]])

    return Screen(
        title="Test suite - deterministic and fully offline",
        subtitle="Live PostgreSQL/OpenAI suites are skipped unless PG_MCP_LIVE_TESTS=1",
        lines=lines,
    )


def main() -> None:
    """Render every demo screen plus the test-run screen to PNG."""
    screens = asyncio.run(build_screens())
    screens.append(asyncio.run(run_smoke()))
    screens.append(test_run_screen())

    for index, screen in enumerate(screens, start=1):
        path = render_screen(screen, index, len(screens))
        print(f"wrote {path.relative_to(ROOT)}")

    print(f"\n{len(screens)} screenshots written to {OUTPUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
