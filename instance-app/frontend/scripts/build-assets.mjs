import { cpSync, existsSync, mkdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const target = path.join(root, 'dist');
const pdfjs = path.join(root, 'node_modules/pdfjs-dist');
if (!existsSync(path.join(pdfjs, 'build/pdf.min.mjs'))) {
  throw new Error('Run npm ci before exporting frontend assets');
}
mkdirSync(target, { recursive: true });
cpSync(path.join(root, 'assets'), path.join(target, 'assets'), { recursive: true });
const engineTarget = path.join(target, 'static/pdfjs');
mkdirSync(engineTarget, { recursive: true });
cpSync(path.join(pdfjs, 'build'), engineTarget, { recursive: true });
for (const name of ['cmaps', 'standard_fonts', 'wasm', 'iccs', 'LICENSE']) {
  cpSync(path.join(pdfjs, name), path.join(engineTarget, name), { recursive: true });
}
console.log('Exported reader modules and self-hosted PDF.js assets to dist');
