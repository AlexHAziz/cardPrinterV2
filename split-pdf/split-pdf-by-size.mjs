import fs from "fs/promises";
import path from "path";
import { fileURLToPath } from "url";
import { PDFDocument } from "pdf-lib";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function parseArgs() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.error("Usage: node split-pdf-by-size.mjs <input.pdf> [--limitMB=150] [--outdir=./output]");
    process.exit(1);
  }
  const input = args[0];
  let limitMB = 150;
  let outdir = "./output";

  for (const a of args.slice(1)) {
    if (a.startsWith("--limitMB=")) limitMB = Number(a.split("=")[1]);
    else if (a.startsWith("--outdir=")) outdir = a.split("=")[1];
  }
  if (!Number.isFinite(limitMB) || limitMB <= 0) {
    console.error("Invalid --limitMB value.");
    process.exit(1);
  }
  return { input, limitBytes: Math.floor(limitMB * 1024 * 1024), outdir };
}

async function ensureDir(dir) {
  await fs.mkdir(dir, { recursive: true });
}

async function saveChunk(doc, outdir, baseName, index) {
  const bytes = await doc.save({ useObjectStreams: true });
  const filename = path.join(outdir, `${baseName}.part${String(index).padStart(3, "0")}.pdf`);
  await fs.writeFile(filename, bytes);
  return { bytesWritten: bytes.length, filename };
}

async function main() {
  const { input, limitBytes, outdir } = parseArgs();
  await ensureDir(outdir);

  const srcBytes = await fs.readFile(input);
  const srcPdf = await PDFDocument.load(srcBytes, { ignoreEncryption: true });

  const totalPages = srcPdf.getPageCount();
  const baseName = path.basename(input, path.extname(input));

  let partIndex = 1;
  let pagesAddedToCurrent = 0;

  // Start first output PDF
  let currentDoc = await PDFDocument.create();

  // pdf-lib requires copying from source to destination
  const copiedCache = new Map(); // optional, but we’ll copy page-by-page anyway

  // Small “safety margin” so we don’t barely exceed the limit after the next page’s objects.
  const SAFETY_BYTES = 512 * 1024; // 0.5 MB

  console.log(`Splitting "${input}" (${totalPages} pages) into chunks under ${(limitBytes/1024/1024).toFixed(1)} MB...`);

  for (let i = 0; i < totalPages; i++) {
    // Try adding page i to currentDoc, check size, back off if needed.
    const [copiedPage] = await currentDoc.copyPages(srcPdf, [i]);
    currentDoc.addPage(copiedPage);
    pagesAddedToCurrent++;

    // Measure size *after* adding this page
    let bytes = await currentDoc.save({ useObjectStreams: true });
    if (bytes.length > (limitBytes - SAFETY_BYTES)) {
      // If this single page pushes us over and it's the only page, we must allow it (or warn)
      if (pagesAddedToCurrent === 1) {
        // write it anyway; warn if it actually exceeds the hard limit
        await fs.writeFile(
          path.join(outdir, `${baseName}.part${String(partIndex).padStart(3,"0")}.pdf`),
          bytes
        );
        const mb = (bytes.length/1024/1024).toFixed(2);
        if (bytes.length > limitBytes) {
          console.warn(`⚠️ Page ${i+1} alone is ${mb} MB, which exceeds the limit. Wrote it as its own part.`);
        } else {
          console.log(`Wrote part ${partIndex} with 1 page (${mb} MB).`);
        }
        // Start a new doc for the next page
        partIndex++;
        currentDoc = await PDFDocument.create();
        pagesAddedToCurrent = 0;
        continue;
      }

      // Otherwise, we overshot after adding this page. We need to move this page to the next chunk.
      // Rebuild currentDoc without the last page.
      // 1) Create a new doc and fill it with previous pages from the *current chunk range*.
      const writeDoc = await PDFDocument.create();
      // We don’t have stored pages, so we’ll recopy the pages of this chunk.
      const startOfChunk = i - (pagesAddedToCurrent - 1);
      for (let p = startOfChunk; p < i; p++) {
        const [pg] = await writeDoc.copyPages(srcPdf, [p]);
        writeDoc.addPage(pg);
      }
      // Save the completed chunk
      const { bytesWritten, filename } = await saveChunk(writeDoc, outdir, baseName, partIndex);
      console.log(`Wrote ${filename} with ${pagesAddedToCurrent - 1} pages (${(bytesWritten/1024/1024).toFixed(2)} MB).`);
      partIndex++;

      // Start a fresh currentDoc that begins with the page that caused the overflow
      currentDoc = await PDFDocument.create();
      const [carryPage] = await currentDoc.copyPages(srcPdf, [i]);
      currentDoc.addPage(carryPage);
      pagesAddedToCurrent = 1;
    }
  }

  // Write any remaining pages in the final doc
  if (pagesAddedToCurrent > 0) {
    const { bytesWritten, filename } = await saveChunk(currentDoc, outdir, baseName, partIndex);
    console.log(`Wrote ${filename} with ${pagesAddedToCurrent} pages (${(bytesWritten/1024/1024).toFixed(2)} MB).`);
  }

  console.log("✅ Done.");
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
