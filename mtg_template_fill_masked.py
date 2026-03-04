
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
    return masks

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
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI, help="Rasterization DPI for masked images")
    args = ap.parse_args()

    if not (args.images or args.single_image):
        raise SystemExit("Provide either --images DIR or --single-image FILE.")

    tpl_doc = fitz.open(args.template)
    page0 = tpl_doc[0]
    pw, ph = page0.rect.width, page0.rect.height

    masks = load_masks(args.mask)

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
