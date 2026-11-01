"""Browser-tab icon: an orange (Zenex) tile with a white requisition page.

Kept in code rather than as image files so it is always deployed with the app.
"""
import io
from functools import lru_cache

ORANGE = "#E97132"

SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="{ORANGE}"/>
<path d="M18 9h21l10 10v36H18z" fill="#fff"/>
<path d="M39 9v10h10z" fill="#F7C9AC"/>
<rect x="23" y="25" width="14" height="3.5" rx="1.5" fill="#F2A57A"/>
<rect x="23" y="33" width="21" height="3.5" rx="1.5" fill="#F2A57A"/>
<rect x="23" y="41" width="21" height="3.5" rx="1.5" fill="#F2A57A"/>
</svg>"""


@lru_cache(maxsize=4)
def png(size):
    """The same icon as a PNG (for /favicon.ico and the iPhone home-screen icon)."""
    from PIL import Image, ImageDraw

    k = 8  # draw large, then shrink, for smooth edges
    im = Image.new("RGBA", (64 * k, 64 * k), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    s = lambda *pts: [(x * k, y * k) for x, y in pts]
    d.rounded_rectangle((0, 0, 64 * k - 1, 64 * k - 1), radius=14 * k, fill=ORANGE)
    d.polygon(s((18, 9), (39, 9), (49, 19), (49, 55), (18, 55)), fill="#ffffff")
    d.polygon(s((39, 9), (39, 19), (49, 19)), fill="#F7C9AC")
    for y, w in ((25, 14), (33, 21), (41, 21)):  # the lines of text on the page
        d.rounded_rectangle((23 * k, y * k, (23 + w) * k, (y + 3.5) * k), radius=1.5 * k, fill="#F2A57A")
    out = io.BytesIO()
    im.resize((size, size), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()
