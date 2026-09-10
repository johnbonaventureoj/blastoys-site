"""Render exploded-view example videos for the Blastoys B2B page.

One MP4 + WebM + poster JPG per livery in liveries.json.

Why frames-from-SVG and not an AI video model: an exploded view only reads as
real if every part stays IDENTICAL as it moves. Generative video redraws the
subject each frame, so the cab changes shape halfway through the take. The SVG
is the same geometry the live page already animates, so the video and the page
can never drift apart.

    py tools/render_exploded.py            # all liveries
    py tools/render_exploded.py --only corvus
    py tools/render_exploded.py --fps 24 --check

Outputs to media/. Safe to re-run; overwrites its own outputs only.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
MEDIA = ROOT / "media"
TEMPLATE = TOOLS / "exploded_template.html"
SPEC = TOOLS / "liveries.json"

W, H = 1280, 720

# Timeline in seconds. Start and end both sit at t=0 (fully assembled) so the
# clip loops seamlessly on the page - no visible jump on repeat.
HOLD_IN = 0.4
EXPLODE = 2.2
HOLD_OUT = 1.3
IMPLODE = 1.6
HOLD_END = 0.4
TOTAL = HOLD_IN + EXPLODE + HOLD_OUT + IMPLODE + HOLD_END


def progress_at(sec: float) -> float:
    """Position on the explode timeline (0 assembled, 1 fully apart)."""
    if sec < HOLD_IN:
        return 0.0
    sec -= HOLD_IN
    if sec < EXPLODE:
        return sec / EXPLODE
    sec -= EXPLODE
    if sec < HOLD_OUT:
        return 1.0
    sec -= HOLD_OUT
    if sec < IMPLODE:
        return 1.0 - (sec / IMPLODE)
    return 0.0


def build_html(livery: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    text = livery["livery_text"]
    # Long trading names overflow the container panel; shrink to fit rather
    # than letting the text run off the side of the model.
    size = 21 if len(text) <= 9 else (17 if len(text) <= 13 else 14)
    subs = {
        "__FIRM__": livery["firm"],
        "__STRAP__": livery["strap"],
        "__CAB_TOP__": livery["cab_top"],
        "__CAB_BOT__": livery["cab_bot"],
        "__BOX_TOP__": livery["box_top"],
        "__BOX_BOT__": livery["box_bot"],
        "__BOX_INNER__": livery["box_inner"],
        "__LIVERY_TEXT__": text,
        "__LIVERY_FILL__": livery["livery_fill"],
        "__LIVERY_SIZE__": str(size),
        "__BAND__": livery["band"],
        "__PLATE__": livery["plate"],
    }
    for k, v in subs.items():
        html = html.replace(k, v)
    if "__" in html:
        leftover = [ln for ln in html.splitlines() if "__" in ln][:3]
        raise SystemExit(f"unsubstituted placeholder still in template:\n" + "\n".join(leftover))
    return html


def render_frames(page, outdir: pathlib.Path, fps: int) -> int:
    n = int(round(TOTAL * fps))
    stage = page.locator("#stage")
    for i in range(n):
        t = progress_at(i / fps)
        page.evaluate("t => window.setProgress(t)", t)
        stage.screenshot(path=str(outdir / f"f{i:04d}.png"))
    return n


def encode(frames: pathlib.Path, slug: str, fps: int) -> None:
    MEDIA.mkdir(exist_ok=True)
    src = str(frames / "f%04d.png")
    mp4 = MEDIA / f"exploded-{slug}.mp4"
    webm = MEDIA / f"exploded-{slug}.webm"
    poster = MEDIA / f"exploded-{slug}.jpg"

    # yuv420p + even dimensions, or Safari and older Android refuse to decode.
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", src,
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-crf", "23", "-movflags", "+faststart", str(mp4),
    ], check=True)

    # WebM as well: an MP4-only page stalls at readyState 0 in a Chromium
    # without proprietary codecs - silently, with no media error to catch.
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", src,
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "36", "-row-mt", "1",
        str(webm),
    ], check=True)

    # Poster = the fully-exploded hold, the most informative single frame.
    poster_idx = int(round((HOLD_IN + EXPLODE + HOLD_OUT / 2) * fps))
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(frames / f"f{poster_idx:04d}.png"),
        "-q:v", "4", str(poster),
    ], check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--only", help="render one livery by slug")
    ap.add_argument("--check", action="store_true",
                    help="verify inputs and tools, render nothing")
    args = ap.parse_args()

    if not TEMPLATE.exists():
        print(f"missing template: {TEMPLATE}", file=sys.stderr)
        return 2
    if not shutil.which("ffmpeg"):
        print("ffmpeg not on PATH", file=sys.stderr)
        return 2

    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    liveries = spec["liveries"]
    if args.only:
        liveries = [l for l in liveries if l["slug"] == args.only]
        if not liveries:
            print(f"no livery with slug {args.only!r}", file=sys.stderr)
            return 2

    n_frames = int(round(TOTAL * args.fps))
    print(f"{len(liveries)} livery(s), {TOTAL:.1f}s @ {args.fps}fps = {n_frames} frames each")

    if args.check:
        for l in liveries:
            build_html(l)          # raises if a placeholder is unsubstituted
            print(f"  [OK] {l['slug']:12} {l['firm']}")
        print("check only - nothing rendered")
        return 0

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H},
                                device_scale_factor=1)
        for l in liveries:
            slug = l["slug"]
            print(f"  {slug}: ", end="", flush=True)
            with tempfile.TemporaryDirectory() as td:
                tmp = pathlib.Path(td)
                html = tmp / "frame.html"
                html.write_text(build_html(l), encoding="utf-8")
                page.goto(html.as_uri())
                page.wait_for_function("() => typeof window.setProgress === 'function'")
                frames = tmp / "frames"
                frames.mkdir()
                got = render_frames(page, frames, args.fps)
                print(f"{got} frames -> ", end="", flush=True)
                encode(frames, slug, args.fps)
            mp4 = MEDIA / f"exploded-{slug}.mp4"
            # Refuse to report success on a file that is missing or empty - a
            # clean-looking zero is the failure mode that costs the most here.
            if not mp4.exists() or mp4.stat().st_size < 10_000:
                print("FAILED (no usable mp4)")
                return 1
            print(f"{mp4.stat().st_size // 1024} kB mp4")
        browser.close()

    print(f"\nwrote {len(liveries) * 3} files to {MEDIA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
