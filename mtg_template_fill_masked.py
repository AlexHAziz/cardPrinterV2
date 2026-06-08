
#!/usr/bin/env python3

"""
mtg_template_fill_masked.py
Place images into template slots with uniform MTG-standard black borders.

Key features:
- Detects source card type: full-art (no border), standard MTG border, or bleeding-edge border.
- Normalizes ALL cards to a uniform border width on the printed page regardless of source type.
- Border width and color are configurable (default: 3.0 mm black, matching MTG standard).
- Reads a mask JSON with entries like: {"x","y","w","h","corner_radius_pt"} in PDF points.
- Supports either a folder of images (placed in row-major order) or a --single-image
  repeated across all masks.
- Optional --auto-rotate and --rotate (0/90/180/270).

Usage examples:

  # Use a SINGLE back image for all slots (recommended for card backs):
  python mtg_template_fill_masked.py \\
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \\
    --single-image "./back.png" \\
    --output "./Backs_Masked.pdf" \\
    --mask "./mask_2x3_0p1in_round.json" \\
    --auto-rotate

  # Use a folder of card fronts (will paginate as needed):
  python mtg_template_fill_masked.py \\
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \\
    --images "./card_art" \\
    --output "./Fronts_Masked.pdf" \\
    --mask "./mask_2x3_0p1in_round.json" \\
    --auto-rotate

  # Custom border color (red for testing):
  python mtg_template_fill_masked.py \\
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \\
    --images "./card_art" \\
    --output "./Fronts_Masked.pdf" \\
    --mask "./mask_2x3_0p1in_round.json" \\
    --border-color ff0000

Notes:
- Coordinates are in PDF points (1 in = 72 pt).
- Corner radius is applied in the rasterized mask we generate per-slot.
- Images are rasterized at 300 DPI relative to slot size for crisp edges.
"""

import os, sys, argparse, json
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image, ImageDraw

PT_PER_IN = 72.0
DEFAULT_DPI = 300                  # rasterization DPI for masked images
DEFAULT_BORDER_WIDTH_IN = 0.118    # ≈ 3.0 mm — standard MTG card border
DEFAULT_BORDER_COLOR = "000000"    # black


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def natural_key(s):
    import re
    _num_re = re.compile(r'(\d+)')
    return [int(t) if t.isdigit() else t.lower() for t in _num_re.split(os.path.basename(s))]


def list_images(images_dir):
    exts = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp")
    files = [os.path.join(images_dir, f) for f in os.listdir(images_dir)
             if f.lower().endswith(exts)]
    return sorted(files, key=natural_key)


def load_masks(path):
    with open(path, "r") as f:
        data = json.load(f)
    masks = data.get("masks", [])
    if not masks:
        raise SystemExit("No 'masks' in JSON.")
    border_inset_pt = data.get("border_inset_in", 0.0) * PT_PER_IN
    return masks, border_inset_pt


def parse_color(s):
    """Parse a hex color string like '000000', '#ff0000', or 'fff' → (R, G, B) tuple 0-255."""
    s = s.strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError(f"Invalid color: {s!r} — expected 6 hex chars")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def should_rotate_auto(iw, ih, sw_pt, sh_pt):
    """Return True if rotating 90° better matches the slot's aspect ratio."""
    slot_ratio = sw_pt / max(1e-6, sh_pt)
    img_ratio  = iw   / max(1e-6, ih)
    slot_portrait = sh_pt >= sw_pt
    img_portrait  = ih   >= iw
    if slot_portrait != img_portrait:
        return True
    err0  = abs(img_ratio - slot_ratio)
    err90 = abs((ih / max(1e-6, iw)) - slot_ratio)
    return err90 < err0


# ---------------------------------------------------------------------------
# Edge scanning (shared by bleed-trim and border detection)
# ---------------------------------------------------------------------------

def _scan_edge_thickness(rgb, edge, dark_threshold=30, samples=10, max_scan_frac=0.25):
    """
    Return the median dark-pixel run length from the given edge inward.

    Scans `samples` evenly-spaced positions along the edge.  At each position,
    counts consecutive pixels whose max channel value < dark_threshold.
    Returns the median count across all sample positions.
    """
    w, h = rgb.size

    def is_dark(x, y):
        px = rgb.getpixel((x, y))
        return max(px[0], px[1], px[2]) < dark_threshold

    thicknesses = []
    if edge in ('top', 'bottom'):
        xs = [int(w * (i + 1) / (samples + 1)) for i in range(samples)]
        max_d = int(h * max_scan_frac)
        for x in xs:
            count = 0
            for d in range(max_d):
                y = d if edge == 'top' else h - 1 - d
                if is_dark(x, y):
                    count += 1
                else:
                    break
            thicknesses.append(count)
    else:  # left / right
        ys = [int(h * (i + 1) / (samples + 1)) for i in range(samples)]
        max_d = int(w * max_scan_frac)
        for y in ys:
            count = 0
            for d in range(max_d):
                x = d if edge == 'left' else w - 1 - d
                if is_dark(x, y):
                    count += 1
                else:
                    break
            thicknesses.append(count)

    thicknesses.sort()
    return thicknesses[len(thicknesses) // 2]  # median


# ---------------------------------------------------------------------------
# Border detection
# ---------------------------------------------------------------------------

def detect_card_border(img, dark_threshold=30, min_border_frac=0.04, max_asymmetry=4.0):
    """
    Detect whether the image has a significant dark border on all 4 sides.

    Returns a dict:
      {
        'has_border': bool,
        'top': int, 'bottom': int, 'left': int, 'right': int,
        'min_thickness': int,   # minimum across all 4 sides
      }

    'has_border' is True only when:
      - All 4 sides have a dark run >= min_border_frac * short_dimension pixels
      - Opposite side pairs are within max_asymmetry of each other
        (highly asymmetric dark means art content, not a frame)
    """
    rgb = img.convert("RGB")
    w, h = rgb.size

    top    = _scan_edge_thickness(rgb, 'top',    dark_threshold)
    bottom = _scan_edge_thickness(rgb, 'bottom', dark_threshold)
    left   = _scan_edge_thickness(rgb, 'left',   dark_threshold)
    right  = _scan_edge_thickness(rgb, 'right',  dark_threshold)

    min_px = int(min(w, h) * min_border_frac)
    has_border = (
        min(top, bottom, left, right) >= min_px
        and max(top, bottom) <= max_asymmetry * max(1, min(top, bottom))
        and max(left, right) <= max_asymmetry * max(1, min(left, right))
    )

    return {
        'has_border': has_border,
        'top': top, 'bottom': bottom, 'left': left, 'right': right,
        'min_thickness': min(top, bottom, left, right),
    }


# ---------------------------------------------------------------------------
# Image normalisation — the core of uniform-border output
# ---------------------------------------------------------------------------

def normalize_card_image(img, target_w_px, target_h_px, border_px, border_color=(0, 0, 0)):
    """
    Produce a target_w_px × target_h_px RGBA image with a perfectly uniform border.

    Pipeline:
      1. Detect whether the source has a dark border on all 4 sides.
      2. If bordered: crop it off uniformly (minimum detected thickness on all sides).
      3. Scale the inner face to COVER the art area exactly
         (target minus 2*border_px on each axis).
      4. Center-crop to the art area so the border is identical on all 4 sides.
      5. Compose on a solid border_color canvas of the full target size.

    COVER (not FIT) is used so that the art area is filled exactly and no
    letterbox gap can accumulate on any axis — the border is always a uniform
    border_px on every side.  The typical MTG card vs slot aspect mismatch is
    ~1-2%, so the resulting center-crop is essentially invisible.
    """
    info = detect_card_border(img)

    if info['has_border']:
        t = info['min_thickness']
        w, h = img.size
        # Crop uniformly by the minimum detected border thickness on all sides
        crop_box = (t, t, w - t, h - t)
        inner = img.crop(crop_box)
        print(f"  [border] detected border={t}px per side; cropping inner face "
              f"({inner.width}×{inner.height})")
    else:
        inner = img
        print(f"  [border] no significant border detected; treating as full-art")

    # Art area = target minus the uniform border we're about to add
    art_w = max(target_w_px - 2 * border_px, 1)
    art_h = max(target_h_px - 2 * border_px, 1)

    # COVER scale: scale up so the inner face fills the art area on both axes,
    # then center-crop any overhang. This guarantees border_px on every side.
    iw, ih = inner.size
    scale = max(art_w / iw, art_h / ih)
    scaled_w = int(round(iw * scale))
    scaled_h = int(round(ih * scale))
    inner_scaled = inner.resize((scaled_w, scaled_h), Image.LANCZOS)

    # Center-crop to art area
    left = (scaled_w - art_w) // 2
    top  = (scaled_h - art_h) // 2
    inner_cropped = inner_scaled.crop((left, top, left + art_w, top + art_h)).convert("RGBA")

    # Compose: solid border-color background, art centered with exact border_px gap
    canvas = Image.new("RGBA", (target_w_px, target_h_px),
                       (border_color[0], border_color[1], border_color[2], 255))
    canvas.paste(inner_cropped, (border_px, border_px), inner_cropped)

    return canvas


# ---------------------------------------------------------------------------
# Rounded-corner masking
# ---------------------------------------------------------------------------

def rounded_rect_mask(size_px, radius_px):
    w, h = size_px
    r = max(0, min(radius_px, min(w, h) // 2))
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([(0, 0), (w - 1, h - 1)], radius=r, fill=255)
    return m


def apply_rounded_corners(img_rgba, radius_px):
    """Apply a rounded-rectangle alpha mask to img_rgba (in-place on a copy)."""
    result = img_rgba.convert("RGBA")
    mask = rounded_rect_mask(result.size, radius_px)
    result.putalpha(mask)
    return result


# ---------------------------------------------------------------------------
# Bleed-border trimming (optional pre-processing pass)
# ---------------------------------------------------------------------------

def trim_bleed_border(img, dark_threshold=20, min_bleed_frac=0.03,
                      keep_border_frac=0.04, max_asymmetry=3.0):
    """
    Detect and remove an oversized bleeding-edge border, keeping a thin remnant.

    Only trims when:
      - All 4 sides have a dark run >= min_bleed_frac of the short dimension.
      - Opposite sides are within max_asymmetry of each other.

    After trimming, keep_border_frac of the short dimension is left as border.
    """
    rgb = img.convert("RGB")
    w, h = rgb.size

    top    = _scan_edge_thickness(rgb, 'top',    dark_threshold)
    bottom = _scan_edge_thickness(rgb, 'bottom', dark_threshold)
    left   = _scan_edge_thickness(rgb, 'left',   dark_threshold)
    right  = _scan_edge_thickness(rgb, 'right',  dark_threshold)

    min_bleed_px = int(min(w, h) * min_bleed_frac)

    if min(top, bottom, left, right) < min_bleed_px:
        return img
    if max(top, bottom) > max_asymmetry * max(1, min(top, bottom)):
        return img
    if max(left, right) > max_asymmetry * max(1, min(left, right)):
        return img

    min_border  = min(top, bottom, left, right)
    keep_px     = int(min(w, h) * keep_border_frac)
    uniform_crop = max(0, min_border - keep_px)

    if uniform_crop == 0:
        return img

    trimmed = img.crop((uniform_crop, uniform_crop, w - uniform_crop, h - uniform_crop))
    print(f"  [trim-bleed] uniform crop={uniform_crop}px per side "
          f"(top={top} bottom={bottom} left={left} right={right})")
    return trimmed


# ---------------------------------------------------------------------------
# PDF page helpers
# ---------------------------------------------------------------------------

def fill_card_gutters(page, masks, border_color_01=(0, 0, 0)):
    """
    Paint solid color into every gap between adjacent mask slots.

    Card images have rounded corners which leave a transparent bite at each
    inner corner.  Without this fill the page-white background shows through
    at those corners after cutting.

    The gutter rect is extended by corner_radius into the surrounding slot
    border area to guarantee full coverage of the corner region.
    """
    if len(masks) < 2:
        return
    r = max(float(m.get('corner_radius_pt', 0)) for m in masks)
    n = len(masks)
    for i in range(n):
        mi = masks[i]
        xi, yi, wi, hi = float(mi['x']), float(mi['y']), float(mi['w']), float(mi['h'])
        for j in range(i + 1, n):
            mj = masks[j]
            xj, yj, wj, hj = float(mj['x']), float(mj['y']), float(mj['w']), float(mj['h'])
            # Column gap: j is to the right of i with overlapping y ranges
            y_overlap = (yi < yj + hj) and (yj < yi + hi)
            if y_overlap and xj > xi + wi:
                top = min(yi, yj) - r
                bot = max(yi + hi, yj + hj) + r
                page.draw_rect(fitz.Rect(xi + wi, top, xj, bot),
                               color=None, fill=border_color_01)
            # Row gap: j is below i with overlapping x ranges
            x_overlap = (xi < xj + wj) and (xj < xi + wi)
            if x_overlap and yj > yi + hi:
                left  = min(xi, xj) - r
                right = max(xi + wi, xj + wj) + r
                page.draw_rect(fitz.Rect(left, yi + hi, right, yj),
                               color=None, fill=border_color_01)


def paint_card_slot_backgrounds(page, masks, border_inset_pt, border_color_01=(0, 0, 0)):
    """
    Paint solid color rectangles covering the full cut area of each card slot.

    The PDF template's colored region is often slightly smaller than the
    Cricut's actual cut dimensions, leaving a thin strip at the outer card
    edges after cutting.  Drawing here — from the mask position back out by
    the border inset — ensures the color extends all the way to the cut edge.

    Called AFTER show_pdf_page and BEFORE the card images are placed.
    """
    for m in masks:
        x = float(m['x']) - border_inset_pt
        y = float(m['y']) - border_inset_pt
        w = float(m['w']) + 2 * border_inset_pt
        h = float(m['h']) + 2 * border_inset_pt
        page.draw_rect(fitz.Rect(x, y, x + w, y + h),
                       color=None, fill=border_color_01)


def prepare_stream(img_rgba):
    bio = BytesIO()
    img_rgba.save(bio, format="PNG")
    bio.seek(0)
    return bio.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template",        required=True,
                    help="Template PDF path (used as page background)")
    ap.add_argument("--images",
                    help="Folder of images to place (row-major order)")
    ap.add_argument("--single-image",
                    help="Use one image for every mask on every page")
    ap.add_argument("--output",          required=True,
                    help="Output PDF path")
    ap.add_argument("--mask",            required=True,
                    help="JSON with masks [{x,y,w,h,corner_radius_pt}]")
    ap.add_argument("--auto-rotate",     action="store_true",
                    help="Rotate 90° when it better matches slot orientation")
    ap.add_argument("--rotate",          type=int, choices=[0, 90, 180, 270], default=0,
                    help="Force clockwise rotation for all images")
    ap.add_argument("--trim-bleed",      action="store_true",
                    help="Pre-process: crop oversized bleed borders before normalisation")
    ap.add_argument("--dpi",             type=int, default=DEFAULT_DPI,
                    help=f"Rasterisation DPI for masked images (default {DEFAULT_DPI})")
    ap.add_argument("--border-width-in", type=float, default=DEFAULT_BORDER_WIDTH_IN,
                    help=f"Border width in inches added around each card face "
                         f"(default {DEFAULT_BORDER_WIDTH_IN} ≈ 3.0 mm MTG standard)")
    ap.add_argument("--border-color",    default=DEFAULT_BORDER_COLOR,
                    help="Border color as 6-digit hex (default '000000' = black)")
    args = ap.parse_args()

    if not (args.images or args.single_image):
        raise SystemExit("Provide either --images DIR or --single-image FILE.")

    # Parse border color
    border_color_rgb = parse_color(args.border_color)
    border_color_01  = tuple(c / 255.0 for c in border_color_rgb)
    print(f"Border: {args.border_width_in:.4f} in  "
          f"color #{args.border_color.lstrip('#').upper()}  "
          f"rgb={border_color_rgb}")

    tpl_doc = fitz.open(args.template)
    page0   = tpl_doc[0]
    pw, ph  = page0.rect.width, page0.rect.height

    masks, border_inset_pt = load_masks(args.mask)

    # Prepare image list
    if args.single_image:
        src_img = Image.open(args.single_image).convert("RGBA")
        images  = [src_img]
    else:
        paths = list_images(args.images)
        if not paths:
            raise SystemExit(f"No images found in {args.images}")
        for p in paths:
            print(f"Found image: {p}")
        images = [Image.open(p).convert("RGBA") for p in paths]

    out   = fitz.open()
    i_idx = 0  # index into images (only used in --images mode)

    while True:
        page = out.new_page(width=pw, height=ph)
        page.show_pdf_page(page.rect, tpl_doc, 0)

        # Paint border color at full card slot area (mask + inset) so the border
        # extends to the Cricut cut edge.  Must be after show_pdf_page and before
        # the card images.
        paint_card_slot_backgrounds(page, masks, border_inset_pt, border_color_01)
        fill_card_gutters(page, masks, border_color_01)

        filled_any = False
        for m in masks:
            x, y, w, h = float(m["x"]), float(m["y"]), float(m["w"]), float(m["h"])
            r_pt = float(m.get("corner_radius_pt", 0.0))

            if args.single_image:
                im = images[0].copy()
            else:
                if i_idx >= len(images):
                    break
                im = images[i_idx].copy()

            # Optional pre-trim of oversized bleed borders
            if args.trim_bleed:
                im = trim_bleed_border(im)

            # Rotation
            if args.rotate in (90, 180, 270):
                im = im.rotate(-args.rotate, expand=True)  # PIL CCW → negate for CW
            elif args.auto_rotate:
                if should_rotate_auto(im.width, im.height, w, h):
                    im = im.rotate(-90, expand=True)

            # Target slot size in pixels at chosen DPI
            tw_px     = int(round(w    * args.dpi / PT_PER_IN))
            th_px     = int(round(h    * args.dpi / PT_PER_IN))
            radius_px = int(round(r_pt * args.dpi / PT_PER_IN))
            border_px = int(round(args.border_width_in * args.dpi))

            print(f"  Slot {w:.1f}×{h:.1f}pt → {tw_px}×{th_px}px  "
                  f"border={border_px}px  radius={radius_px}px")

            # Normalise: detect border type → crop existing → FIT scale → add uniform border
            im_bordered = normalize_card_image(im, tw_px, th_px, border_px, border_color_rgb)

            # Apply rounded corners
            im_final = apply_rounded_corners(im_bordered, radius_px)

            img_bytes = prepare_stream(im_final)
            rect = fitz.Rect(x, y, x + w, y + h)
            page.insert_image(rect, stream=img_bytes, keep_proportion=False, overlay=True)

            filled_any = True
            if not args.single_image:
                i_idx += 1

        if not filled_any:
            break

        if args.single_image:
            break
        else:
            if i_idx >= len(images):
                break

    out.save(args.output, deflate=True)
    out.close()
    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
