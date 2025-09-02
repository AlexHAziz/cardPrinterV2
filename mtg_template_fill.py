
#!/usr/bin/env python3

"""
mtg_template_fill.py  (v4)
- Modes: exact | contain | cover
  * exact: fill the slot bounds exactly (no aspect preservation).
  * contain: preserve aspect, no crop (letterboxing inside slot).
  * cover:   preserve aspect, fill slot (may crop).
- Auto-rotate: if --auto-rotate is set, rotate each image 90 degrees when it better matches slot orientation.

Usage:
  python mtg_template_fill.py fill \
    --template "./CARD_TEMPLATE_FOR_GPT.pdf" \
    --images "./card_art" \
    --output "./Filled_Templates.pdf" \
    --slots "./slots_2x3_exact.json" \
    --mode exact --auto-rotate --inset 0.0

  python mtg_template_fill.py preview --template ... --slots ... --png ...
"""
import os, sys, json, argparse, re, math, glob, random
import fitz  # PyMuPDF
from PIL import Image, ImageDraw

_num_re = re.compile(r'(\d+)')
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in _num_re.split(os.path.basename(s))]

def natural_image_list(images_dir):
    exts = (".png",".jpg",".jpeg",".webp",".tif",".tiff",".bmp")
    files = [os.path.join(images_dir,f) for f in os.listdir(images_dir) if f.lower().endswith(exts)]
    return sorted(files, key=natural_key)

def load_slots(path, page_w, page_h):
    with open(path, "r") as f:
        data = json.load(f)
    slots = data.get("slots", [])
    out = []
    for s in slots:
        x = float(s["x"]); y = float(s["y"]); w = float(s["w"]); h = float(s["h"])
        x = max(0, min(x, page_w))
        y = max(0, min(y, page_h))
        w = max(1, min(w, page_w - x))
        h = max(1, min(h, page_h - y))
        out.append({"x":x,"y":y,"w":w,"h":h})
    return out

def fit_rect_cover(src_w, src_h, dst_w, dst_h):
    src_ratio = src_w/src_h
    dst_ratio = dst_w/dst_h
    if src_ratio > dst_ratio:
        render_h = dst_h
        render_w = src_ratio * render_h
    else:
        render_w = dst_w
        render_h = render_w / src_ratio
    return render_w, render_h

def fit_rect_contain(src_w, src_h, dst_w, dst_h):
    src_ratio = src_w/src_h
    dst_ratio = dst_w/dst_h
    if src_ratio > dst_ratio:
        render_w = dst_w
        render_h = render_w / src_ratio
    else:
        render_h = dst_h
        render_w = src_ratio * render_h
    return render_w, render_h

def build_preview_png(template_pdf, slots_json, out_png):
    doc = fitz.open(template_pdf)
    page = doc[0]
    zoom = 2.5
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    draw = ImageDraw.Draw(img)
    scale_x = pix.width / page.rect.width
    scale_y = pix.height / page.rect.height
    slots = load_slots(slots_json, page.rect.width, page.rect.height)
    for i, s in enumerate(slots, 1):
        x0 = int(s["x"]*scale_x); y0 = int(s["y"]*scale_y)
        x1 = int((s["x"]+s["w"])*scale_x); y1 = int((s["y"]+s["h"])*scale_y)
        draw.rectangle([x0,y0,x1,y1], outline=(255,0,0), width=3)
        draw.rectangle([x0, y0-20, x0+36, y0], fill=(255,0,0))
        draw.text((x0+6, y0-18), str(i), fill=(255,255,255))
    img.save(out_png)

def should_rotate_auto(iw, ih, sw, sh):
    """Return True if a 90deg rotation would better match slot orientation/aspect."""
    # Compare aspect error vs slot's aspect
    from math import isfinite
    slot_ratio = sw/sh
    img_ratio = iw/ih
    err0 = abs(img_ratio - slot_ratio)
    err90 = abs((ih/iw) - slot_ratio)
    # Also prefer matching portrait/landscape orientation
    slot_portrait = sh >= sw
    img_portrait = ih >= iw
    if slot_portrait != img_portrait:
        # mismatch: try rotation
        return True
    # Otherwise, choose the one with smaller absolute ratio error
    return err90 < err0

def fill(template_pdf, images_dir, out_pdf, slots_json, mode="exact", shuffle=False, rotate=0, auto_rotate=False, inset=0.0):
    tpl_doc = fitz.open(template_pdf)
    page0 = tpl_doc[0]
    pw, ph = page0.rect.width, page0.rect.height
    slots = load_slots(slots_json, pw, ph)
    if not slots:
        raise SystemExit("No slots found in JSON.")
    images = natural_image_list(images_dir)
    if shuffle:
        random.shuffle(images)
    if not images:
        raise SystemExit(f"No images found in {images_dir}")
    out_doc = fitz.open()
    slot_idx = 0
    while slot_idx < len(images):
        page = out_doc.new_page(width=pw, height=ph)
        page.show_pdf_page(page.rect, tpl_doc, 0)
        for s in slots:
            if slot_idx >= len(images):
                break
            img_path = images[slot_idx]
            with Image.open(img_path) as im:
                im = im.convert("RGB")
                iw, ih = im.size
                # rotation flags
                do_rotate = False
                if rotate in (90,180,270):
                    do_rotate = True
                    rot = rotate
                elif auto_rotate:
                    if should_rotate_auto(iw, ih, s["w"]-2*inset, s["h"]-2*inset):
                        do_rotate = True
                        rot = 90
                if do_rotate:
                    im = im.rotate(rot, expand=True)
                    iw, ih = im.size
                from io import BytesIO
                bio = BytesIO()
                im.save(bio, format="JPEG", quality=95)
                bio.seek(0)
                # Compute destination rect
                dst_x = s["x"] + inset
                dst_y = s["y"] + inset
                dst_w = s["w"] - 2*inset
                dst_h = s["h"] - 2*inset
                if dst_w <= 0 or dst_h <= 0:
                    dst_x, dst_y, dst_w, dst_h = s["x"], s["y"], s["w"], s["h"]
                if mode == "exact":
                    rect = fitz.Rect(dst_x, dst_y, dst_x+dst_w, dst_y+dst_h)
                elif mode == "contain":
                    rw, rh = fit_rect_contain(iw, ih, dst_w, dst_h)
                    ox = dst_x + (dst_w - rw)/2
                    oy = dst_y + (dst_h - rh)/2
                    rect = fitz.Rect(ox, oy, ox+rw, oy+rh)
                elif mode == "cover":
                    rw, rh = fit_rect_cover(iw, ih, dst_w, dst_h)
                    ox = dst_x + (dst_w - rw)/2
                    oy = dst_y + (dst_h - rh)/2
                    rect = fitz.Rect(ox, oy, ox+rw, oy+rh)
                else:
                    raise SystemExit(f"Unknown mode: {mode}")
                page.insert_image(rect, stream=bio.getvalue(), keep_proportion=False, overlay=True)
            slot_idx += 1
    out_doc.save(out_pdf, deflate=True)
    out_doc.close()

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_fill = sub.add_parser("fill", help="Place images on the template and render a final PDF")
    ap_fill.add_argument("--template", required=True)
    ap_fill.add_argument("--images", required=True)
    ap_fill.add_argument("--output", required=True)
    ap_fill.add_argument("--slots", required=True)
    ap_fill.add_argument("--mode", default="exact", choices=["exact","contain","cover"])
    ap_fill.add_argument("--shuffle", action="store_true")
    ap_fill.add_argument("--rotate", type=int, default=0, choices=[0,90,180,270])
    ap_fill.add_argument("--auto-rotate", action="store_true")
    ap_fill.add_argument("--inset", type=float, default=0.0, help="Inset in points to keep content slightly inside the slot")
    ap_prev = sub.add_parser("preview", help="Render a PNG preview with numbered slot boxes")
    ap_prev.add_argument("--template", required=True)
    ap_prev.add_argument("--slots", required=True)
    ap_prev.add_argument("--png", required=True)
    args = ap.parse_args()
    if args.cmd == "preview":
        build_preview_png(args.template, args.slots, args.png)
        print(f"Saved preview to {args.png}")
    elif args.cmd == "fill":
        fill(args.template, args.images, args.output, args.slots, mode=args.mode, shuffle=args.shuffle, rotate=args.rotate, auto_rotate=args.auto_rotate, inset=args.inset)
        print(f"Wrote {args.output}")
    else:
        print("Unknown command")
if __name__ == "__main__":
    main()
