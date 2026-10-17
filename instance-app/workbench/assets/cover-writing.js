// Cover title: inline the pre-built self-writing SVGs (outline reveal).
// The SVGs are generated offline (see tools) from the real glyph outlines, so
// the character shapes are exact. Fetch + inline so SMIL animation runs.

const BRAND_SVG = new URL('./cover-brand.svg', import.meta.url).href;
const TAGLINE_SVG = new URL('./cover-tagline.svg', import.meta.url).href;

async function inline(host, url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  host.innerHTML = await res.text();
}

export async function mountCoverWriting(root = document) {
  const brandHost = root.getElementById('cover-brand');
  const taglineHost = root.getElementById('cover-tagline');
  if (!brandHost || !taglineHost) return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  try {
    await Promise.all([
      inline(brandHost, BRAND_SVG),
      inline(taglineHost, TAGLINE_SVG),
    ]);
    root.querySelector('.cover__title')?.classList.add('cover__title--written');
  } catch (err) {
    // Keep the CSS-rendered fallback text if the SVGs fail to load.
    console.warn('cover writing unavailable:', err);
  }
}
