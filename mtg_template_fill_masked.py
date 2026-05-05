
#!/usr/bin/env python3

"""
mtg_template_fill_masked.py
Place images into template slots using rounded-corner masks with a fixed border.

Key features:
- Reads a mask JSON with entries like: {"x","y","w","h","corner_radius_pt"} in PDF points.
- Places images in **cover** mode inside each mask: slot fully filled, no visible background
  (except the intentional outer border defined by the mask JSON).
- Supports either a folder of images (placed in row-major order) or a --single-image
  repeated across all masks.
- Optional --auto-rotate and --rotate (0/90/180/270).
- Keeps transparency (PNG) in the inserted image stream.

Usage examples:

  # Use a SINGLE back image for all slots (recommended for card backs):
  python mtg_template_fill_masked.py \
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \
    --single-image "./back.png" \
    --output "./Backs_Masked.pdf" \
    --mask "./mask_2x3_0p1in_round.json" \
    --auto-rotate

  # Use a folder of card fronts (will paginate as needed):
  python mtg_template_fill_masked.py \
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \
    --images "./card_art" \
    --output "./Fronts_Masked.pdf" \
    --mask "./mask_2x3_0p1in_round.json" \
    --auto-rotate

Notes:
- Coordinates are in PDF points (1 in = 72 pt).
- Corner radius is applied in the rasterized mask we generate per-slot to ensure rounded edges.
- For best edge quality, we rasterize each masked image at 300 DPI relative to its slot size.
"""

import os, sys, argparse, json, random
from io import BytesIO

import fitz  # PyMuPDF
from PIL import Image, ImageDraw

PT_PER_IN = 72.0
DEFAULT_DPI = 300  # for rasterizing masked images crisply

def natural_key(s):
    import re, os
    _num_re = re.compile(r'(\d+)')
    return [int(t) if t.isdigit() else t.lower() for t in _num_re.split(os.path.basename(s))]

def list_images(images_dir):
    exts = (".png",".jpg",".jpeg",".webp",".tif",".tiff",".bmp")
    files = [os.path.join(images_dir,f) for f in os.listdir(images_dir) if f.lower().endswith(exts)]
    return sorted(files, key=natural_key)

def load_masks(path):
    with open(path,"r") as f:
        data = json.load(f)
    masks = data.get("masks", [])
    if not masks:
        raise SystemExit("No 'masks' in JSON.")
    border_inset_pt = data.get("border_inset_in", 0.0) * PT_PER_IN
    return masks, border_inset_pt

def should_rotate_auto(iw, ih, sw_pt, sh_pt):
    # Decide if rotating 90 helps match slot orientation
    slot_ratio = sw_pt / max(1e-6, sh_pt)
    img_ratio = iw / max(1e-6, ih)
    err0 = abs(img_ratio - slot_ratio)
    err90 = abs((ih/ max(1e-6, iw)) - slot_ratio)
    slot_portrait = sh_pt >= sw_pt
    img_portrait = ih >= iw
    if slot_portrait != img_portrait:
        return True
    return err90 < err0

def cover_size(iw, ih, tw, th):
    # Return (rw, rh) resized to cover target tw x th while preserving aspect
    src_ratio = iw/ih
    dst_ratio = tw/th
    if src_ratio > dst_ratio:
        rh = th
        rw = int(round(src_ratio * rh))
    else:
        rw = tw
        rh = int(round(rw / src_ratio))
    return rw, rh


def trim_bleed_border(img, dark_threshold=20, min_bleed_frac=0.03, keep_border_frac=0.04, max_asymmetry=3.0):
    """
    Detect and remove a bleeding edge border from a card image.

    A real bleed border is nearly pure black on all four sides and reasonably
    symmetric. This function only trims when:
      - all 4 sides have a detectable dark border >= min_bleed_frac of dimension
      - opposite sides are within max_asymmetry of each other in thickness

    dark_threshold: max per-channel brightness to count as black (default 20)
    min_bleed_frac: minimum border fraction on each side to trigger trimming (default 3%)
    keep_border_frac: fraction of dimension to keep as border after trimming (default 3% ≈ 2mm on a standard card)
    max_asymmetry: max ratio between opposite sides before treating dark as art, not bleed (default 3x)
    """
    rgb = img.convert("RGB")
    w, h = rgb.size

    def is_black(x, y):
        px = rgb.getpixel((x, y))
        return max(px[0], px[1], px[2]) < dark_threshold

    def scan_edge(edge, samples=10, max_scan_frac=0.12):
        thicknesses = []
        if edge in ('top', 'bottom'):
            xs = [int(w * (i + 1) / (samples + 1)) for i in range(samples)]
            max_d = int(h * max_scan_frac)
            for x in xs:
                count = 0
                for d in range(max_d):
                    y = d if edge == 'top' else h - 1 - d
                    if is_black(x, y):
                        count += 1
                    else:
                        break
                thicknesses.append(count)
        else:
            ys = [int(h * (i + 1) / (samples + 1)) for i in range(samples)]
            max_d = int(w * max_scan_frac)
            for y in ys:
                count = 0
                for d in range(max_d):
                    x = d if edge == 'left' else w - 1 - d
                    if is_black(x, y):
                        count += 1
                    else:
                        break
                thicknesses.append(count)
        thicknesses.sort()
        return thicknesses[len(thicknesses) // 2]  # median

    top    = scan_edge('top')
    bottom = scan_edge('bottom')
    left   = scan_edge('left')
    right  = scan_edge('right')

    min_bleed_px = int(min(w, h) * min_bleed_frac)

    # All 4 sides must have a dark border — a real bleed forms a complete frame
    if min(top, bottom, left, right) < min_bleed_px:
        return img

    # Opposite sides must be roughly symmetric — asymmetric dark means it's art content
    if max(top, bottom) > max_asymmetry * min(top, bottom):
        return img
    if max(left, right) > max_asymmetry * min(left, right):
        return img

    # Use the minimum detected border across all 4 sides as the uniform crop basis —
    # this ensures the same amount is removed from every edge, giving an even border all around.
    min_border = min(top, bottom, left, right)
    keep_px = int(min(w, h) * keep_border_frac)
    uniform_crop = max(0, min_border - keep_px)

    if uniform_crop == 0:
        return img

    trimmed = img.crop((uniform_crop, uniform_crop, w - uniform_crop, h - uniform_crop))
    print(f"  [trim-bleed] uniform crop={uniform_crop}px per side (detected borders: top={top} bottom={bottom} left={left} right={right})")
    return trimmed

def fill_card_gutters(page, masks):
    """
    Paint solid black into every gap between adjacent mask slots.

    Card images have rounded corners, which leaves a transparent bite at each
    inner corner.  Without this fill the page-white background shows through at
    those corners — visible as a white sliver on the top of right-column cards
    and the bottom of left-column cards once cut and rotated to portrait.

    The gutter rect is extended by corner_radius into the surrounding slot
    border area to guarantee full coverage of the white corner region.
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
                top  = min(yi, yj) - r
                bot  = max(yi + hi, yj + hj) + r
                page.draw_rect(fitz.Rect(xi + wi, top, xj, bot),
                               color=None, fill=(0, 0, 0))
            # Row gap: j is below i with overlapping x ranges
            x_overlap = (xi < xj + wj) and (xj < xi + wi)
            if x_overlap and yj > yi + hi:
                left  = min(xi, xj) - r
                right = max(xi + wi, xj + wj) + r
                page.draw_rect(fitz.Rect(left, yi + hi, right, yj),
                               color=None, fill=(0, 0, 0))

def paint_card_slot_backgrounds(page, masks, border_inset_pt):
    """
    Paint solid black rectangles covering the full cut area of each card slot.

    The PDF template's black region is often slightly smaller than the Cricut's
    actual cut dimensions, leaving a thin white strip at the outer card edges
    after cutting.  Drawing black here — from the mask position back out by the
    border inset — ensures the black border extends all the way to the cut edge.

    Called AFTER show_pdf_page (so it draws on top of the template) and BEFORE
    the card images are placed.
    """
    for m in masks:
        x = float(m['x']) - border_inset_pt
        y = float(m['y']) - border_inset_pt
        w = float(m['w']) + 2 * border_inset_pt
        h = float(m['h']) + 2 * border_inset_pt
        page.draw_rect(fitz.Rect(x, y, x + w, y + h),
                       color=None, fill=(0, 0, 0))

def rounded_rect_mask(size_px, radius_px):
    w, h = size_px
    r = max(0, min(radius_px, min(w,h)//2))
    m = Image.new("L", (w,h), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([(0,0),(w-1,h-1)], radius=r, fill=255)
    return m

def paste_cover_with_rounding(src_img, target_px_size, radius_px):
    # Resize to cover & center-crop to target size, then apply rounded corner alpha
    tw, th = target_px_size
    iw, ih = src_img.size
    rw, rh = cover_size(iw, ih, tw, th)

    im = src_img.resize((rw, rh), Image.LANCZOS)
    # center crop to target
    left = (rw - tw)//2
    top  = (rh - th)//2
    im = im.crop((left, top, left+tw, top+th)).convert("RGBA")

    mask = rounded_rect_mask((tw, th), radius_px)
    im.putalpha(mask)
    return im

def prepare_stream_for_slot(img_rgba, dpi=DEFAULT_DPI):
    bio = BytesIO()
    img_rgba.save(bio, format="PNG")
    bio.seek(0)
    return bio.getvalue()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True, help="Template PDF path (used as page background)")
    ap.add_argument("--images", help="Folder of images to place (row-major order)")
    ap.add_argument("--single-image", help="Use one image for every mask on every page")
    ap.add_argument("--output", required=True, help="Output PDF path")
    ap.add_argument("--mask", required=True, help="JSON with masks [{x,y,w,h,corner_radius_pt}]")
    ap.add_argument("--auto-rotate", action="store_true", help="Rotate 90° when it better matches slot")
    ap.add_argument("--rotate", type=int, choices=[0,90,180,270], default=0, help="Force rotation for all images")
    ap.add_argument("--trim-bleed", action="store_true", help="Auto-detect and crop oversized bleed borders down to a normal black border")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="Rasterization DPI for masked images")
    args = ap.parse_args()

    if not (args.images or args.single_image):
        raise SystemExit("Provide either --images DIR or --single-image FILE.")

    tpl_doc = fitz.open(args.template)
    page0 = tpl_doc[0]
    pw, ph = page0.rect.width, page0.rect.height

    masks, border_inset_pt = load_masks(args.mask)

    # Prepare image list
    if args.single_image:
        src_img = Image.open(args.single_image).convert("RGBA")
        images = [src_img]
    else:
        paths = list_images(args.images)
        if not paths:
            raise SystemExit(f"No images found in {args.images}")
        for p in paths:
            print(f"Found image: {p}")
            Image.open(p).convert("RGBA")
        images = [Image.open(p).convert("RGBA") for p in paths]

    out = fitz.open()
    i_idx = 0  # index into images (only used for --images mode)

    while True:
        page = out.new_page(width=pw, height=ph)
        page.show_pdf_page(page.rect, tpl_doc, 0)
        # Paint solid black at the full card slot area (mask + border) so the
        # black border extends all the way to the Cricut cut edge.  Must happen
        # after show_pdf_page (overlay on top) and before card images.
        paint_card_slot_backgrounds(page, masks, border_inset_pt)
        fill_card_gutters(page, masks)

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

            if args.trim_bleed:
                im = trim_bleed_border(im)

            if args.rotate in (90,180,270):
                im = im.rotate(-args.rotate, expand=True)  # PIL is CCW; negative for CW
            elif args.auto_rotate:
                if should_rotate_auto(im.width, im.height, w, h):
                    im = im.rotate(-90, expand=True)

            # compute pixel size for slot at DPI
            tw_px = int(round(w * args.dpi / PT_PER_IN))
            th_px = int(round(h * args.dpi / PT_PER_IN))
            radius_px = int(round(r_pt * args.dpi / PT_PER_IN))

            im_masked = paste_cover_with_rounding(im, (tw_px, th_px), radius_px)

            img_bytes = prepare_stream_for_slot(im_masked, dpi=args.dpi)
            rect = fitz.Rect(x, y, x+w, y+h)
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

if __name__ == "__main__":
    main()
