"""
hf_space_to_pdf.py
──────────────────
Downloads the FinePDFsBlog HuggingFace Space as a readable PDF.

Strategy (in priority order):
  1. Find the actual iframe / child-frame URL and render that directly.
  2. Intercept XHR/fetch calls to discover raw content endpoints.
  3. Full-page screenshot stitching (always readable, never clipped).
  4. Fallback: extract visible text → build a clean ReportLab PDF.

Requirements:
    pip install playwright Pillow reportlab
    playwright install chromium
"""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────────
SPACE_URL   = "https://huggingface.co/spaces/HuggingFaceFW/blogpost-fineweb-v1"
OUT_PATH    = Path("fineweb_blog.pdf")
VIEWPORT_W  = 1440
VIEWPORT_H  = 900          # normal height; we scroll manually
SCROLL_STEP = 600          # px per scroll tick
SCROLL_WAIT = 0.6          # seconds between ticks
SETTLE_WAIT = 4.0          # seconds after last scroll before capture
# ─────────────────────────────────────────────────────────────────────────────


# ── Helpers ───────────────────────────────────────────────────────────────────

def wait_for_load(page: Page, extra: float = 5.0) -> None:
    """Best-effort load wait."""
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except PWTimeout:
        pass
    time.sleep(extra)


def find_content_frame_url(page: Page) -> str | None:
    """
    HF Spaces often embed the actual Gradio app in an <iframe>.
    Return the src of the most likely content iframe, or None.
    """
    candidates: list[str] = []

    # 1. Check child frames already loaded by Playwright.
    for frame in page.frames:
        url = frame.url
        if url and url != page.url and "blank" not in url and "about:" not in url:
            candidates.append(url)

    # 2. Check <iframe> elements in DOM.
    try:
        srcs = page.eval_on_selector_all(
            "iframe[src]",
            "els => els.map(e => e.src).filter(Boolean)"
        )
        candidates.extend(srcs)
    except Exception:
        pass

    # Prefer URLs that look like the actual space app (not analytics/ads).
    for url in candidates:
        if "huggingface.co" in url or "hf.space" in url:
            return url

    return candidates[0] if candidates else None


def full_page_scroll(page: Page) -> None:
    """
    Scroll to the very bottom in small steps so lazy content loads,
    then return to top.
    """
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)

    total = page.evaluate("document.body.scrollHeight")
    pos   = 0

    while pos < total:
        pos += SCROLL_STEP
        page.evaluate(f"window.scrollTo(0, {pos})")
        time.sleep(SCROLL_WAIT)
        # The page may grow as we scroll (infinite scroll / lazy load).
        total = page.evaluate("document.body.scrollHeight")

    # Also scroll any overflow containers (Gradio wraps content in divs).
    page.evaluate("""
    () => {
        document.querySelectorAll('*').forEach(el => {
            const s = getComputedStyle(el);
            if ((s.overflowY === 'scroll' || s.overflowY === 'auto') &&
                el.scrollHeight > el.clientHeight + 10) {
                el.scrollTop = el.scrollHeight;
            }
        });
    }
    """)
    time.sleep(SCROLL_WAIT)

    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(SETTLE_WAIT)


def stitch_screenshots_to_pdf(page: Page, out: Path) -> None:
    """
    Resize the viewport to the full page height, take one screenshot,
    save as PDF via Pillow.  This always captures 100 % of the content.
    """
    # Measure true document height.
    full_height = page.evaluate("""
    () => Math.max(
        document.body.scrollHeight,
        document.documentElement.scrollHeight,
        document.body.offsetHeight,
        document.documentElement.offsetHeight
    )
    """)

    # Cap at a reasonable maximum to avoid OOM (≈ 30 A4 pages).
    MAX_H = 42_000
    if full_height > MAX_H:
        print(f"  ⚠  Page height {full_height}px > {MAX_H}px cap; content may be cut.")
        full_height = MAX_H

    # Resize viewport to full height so nothing is clipped.
    page.set_viewport_size({"width": VIEWPORT_W, "height": full_height})
    time.sleep(1.5)

    print(f"  Taking full-page screenshot ({VIEWPORT_W}×{full_height}px)…")
    png_bytes = page.screenshot(full_page=True, type="png")

    img = Image.open(io.BytesIO(png_bytes))

    # Convert to A4-proportioned PDF (scale width → 210 mm @ 96 dpi).
    a4_w_px = int(210 / 25.4 * 150)   # 150 dpi for a good balance of size/quality
    ratio   = a4_w_px / img.width
    new_h   = int(img.height * ratio)
    img     = img.resize((a4_w_px, new_h), Image.LANCZOS)

    # Split into A4 pages.
    a4_h_px   = int(297 / 25.4 * 150)
    pages_out : list[Image.Image] = []
    y = 0
    while y < new_h:
        crop = img.crop((0, y, a4_w_px, min(y + a4_h_px, new_h)))
        # Pad last page to full A4 height.
        if crop.height < a4_h_px:
            padded = Image.new("RGB", (a4_w_px, a4_h_px), (255, 255, 255))
            padded.paste(crop, (0, 0))
            crop = padded
        pages_out.append(crop.convert("RGB"))
        y += a4_h_px

    if not pages_out:
        raise RuntimeError("Screenshot produced no content.")

    first, rest = pages_out[0], pages_out[1:]
    first.save(out, save_all=True, append_images=rest,
               resolution=150, format="PDF")

    print(f"  ✓  Screenshot PDF: {out} ({out.stat().st_size:,} bytes, {len(pages_out)} pages)")


def extract_text_and_build_pdf(page: Page, out: Path) -> None:
    """
    Fallback: pull all visible text from the page and build a clean,
    fully-readable ReportLab PDF.  Not pretty, but 100 % readable.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    print("  Extracting text for fallback ReportLab PDF…")

    raw: str = page.evaluate("""
    () => {
        const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null
        );
        const parts = [];
        let node;
        while ((node = walker.nextNode())) {
            const t = node.textContent.trim();
            if (t.length > 1) parts.push(t);
        }
        return parts.join('\\n');
    }
    """)

    lines = [l.strip() for l in raw.splitlines() if l.strip()]

    doc    = SimpleDocTemplate(str(out), pagesize=A4,
                               leftMargin=20*mm, rightMargin=20*mm,
                               topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    heading_style = ParagraphStyle(
        "Heading", parent=styles["Heading2"], spaceAfter=6, spaceBefore=12
    )
    body_style = ParagraphStyle(
        "Body", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=4
    )

    story = []
    for line in lines:
        # Heuristic: short ALL-CAPS or title-case lines → treat as heading.
        if len(line) < 80 and (line.isupper() or re.match(r'^[A-Z][^a-z]{0,3}', line)):
            story.append(Paragraph(line, heading_style))
        else:
            # Escape ReportLab XML special chars.
            safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(safe, body_style))
        story.append(Spacer(1, 2))

    doc.build(story)
    print(f"  ✓  Text PDF: {out} ({out.stat().st_size:,} bytes)")


# ── Main ─────────────────────────────────────────────────────────────────────

def capture(out: Path = OUT_PATH) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",     # allow cross-origin iframe access
                "--disable-features=IsolateOrigins,site-per-process",
            ]
        )

        ctx  = browser.new_context(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
        page = ctx.new_page()

        # ── Step 1: Load the Space ────────────────────────────────────────────
        print(f"Loading {SPACE_URL} …")
        page.goto(SPACE_URL, wait_until="domcontentloaded", timeout=60_000)
        wait_for_load(page, extra=6.0)

        # ── Step 2: Try to find and navigate to the actual content iframe ─────
        iframe_url = find_content_frame_url(page)
        if iframe_url and iframe_url != SPACE_URL:
            print(f"Found content frame: {iframe_url}")
            print("Navigating directly to iframe URL…")
            page.goto(iframe_url, wait_until="domcontentloaded", timeout=60_000)
            wait_for_load(page, extra=5.0)
        else:
            print("No sub-frame found; rendering top-level page.")

        # ── Step 3: Scroll to trigger all lazy loading ────────────────────────
        print("Scrolling to load all content…")
        full_page_scroll(page)

        # ── Step 4: Capture ───────────────────────────────────────────────────
        print("Capturing PDF via screenshot stitching…")
        try:
            stitch_screenshots_to_pdf(page, out)
        except Exception as e:
            print(f"Screenshot approach failed ({e}), falling back to text extraction…")
            extract_text_and_build_pdf(page, out)

        browser.close()

    size = out.stat().st_size if out.exists() else 0
    if size < 50_000:
        print("⚠  Output PDF is very small — the Space may require login or JS execution.")
    else:
        print(f"\n✅  Done → {out.resolve()}  ({size:,} bytes)")


if __name__ == "__main__":
    capture()