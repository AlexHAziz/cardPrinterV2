#!/usr/bin/env python3
"""
interleave_backs_from_pdf.py
Insert a *pre-made backs PDF page* after every page of a fronts PDF for duplex printing.

Usage:
  python interleave_backs_from_pdf.py     --fronts "./Fronts.pdf"     --backs "./Backs.pdf"     --output "./Fronts_WithBacks.pdf"     --duplex long     --backs-page 1     --offset-x 0 --offset-y 0

Options:
  --fronts       Path to PDF with your front pages.
  --backs        Path to a PDF containing the back layout page (single design). The same page is reused.
  --backs-page   1-based page number from the backs PDF to use (default: 1).
  --output       Output PDF path.
  --duplex       'long' (default) or 'short'. 'short' rotates each inserted BACK page 180° for short-edge flip.
  --offset-x     Nudge the back page horizontally in PDF points (+right, -left). Default 0.
  --offset-y     Nudge the back page vertically in PDF points (+down, -up). Default 0.

Notes:
- This does *not* re-render your back page; it places the chosen backs PDF page as-is onto a blank page
  the same size as your fronts and applies optional offsets / duplex rotation.
- For perfect alignment, make sure your backs page uses the same trim/page size as fronts.
"""
import fitz  # PyMuPDF
import argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fronts", required=True, help="Front pages PDF")
    ap.add_argument("--backs", required=True, help="Back layout PDF (one or more pages)")
    ap.add_argument("--backs-page", type=int, default=1, help="1-based page index to use from backs PDF")
    ap.add_argument("--output", required=True, help="Output interleaved PDF")
    ap.add_argument("--duplex", choices=["long","short"], default="long", help="Duplex flip type")
    ap.add_argument("--offset-x", type=float, default=0.0, help="Back page X offset in points (+right, -left)")
    ap.add_argument("--offset-y", type=float, default=0.0, help="Back page Y offset in points (+down, -up)")
    args = ap.parse_args()

    fronts = fitz.open(args.fronts)
    backs = fitz.open(args.backs)

    if args.backs_page < 1 or args.backs_page > len(backs):
        raise SystemExit(f"--backs-page must be between 1 and {len(backs)}")
    bi = args.backs_page - 1  # zero-based

    out = fitz.open()
    for i in range(len(fronts)):
        # Insert the original front page
        out.insert_pdf(fronts, from_page=i, to_page=i)

        # Prepare a blank page with same size as the current front
        fr = fronts[i].rect
        bpage = out.new_page(width=fr.width, height=fr.height)

        # Where to place the backs page content on the blank page
        # Start with full-page rect and then shift by offsets
        rect = fitz.Rect(0, 0, fr.width, fr.height)
        rect = rect + (args.offset_x, args.offset_y, args.offset_x, args.offset_y)

        # Draw the chosen backs page into 'bpage'
        bpage.show_pdf_page(rect, backs, pno=bi)

        # Rotate the entire back page 180° for short-edge duplex if requested
        if args.duplex == "short":
            bpage.set_rotation(180)

    out.save(args.output, deflate=True)
    out.close()

if __name__ == "__main__":
    main()
