/* Public pages: nav shadow on scroll, cover lift/fade, demo replay. */
(function () {
  'use strict';
  const nav = document.querySelector('.nav');
  const plate = document.querySelector('.cover__plate');
  const cover = document.querySelector('.cover');
  const page = document.querySelector('.page');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Cover image: show the placeholder only when the image is missing.
  const img = plate?.querySelector('img');
  if (img) {
    const missing = () => { img.remove(); plate.querySelector('.cover__ph').hidden = false; };
    if (img.complete && !img.naturalWidth) missing();
    img.addEventListener('error', missing, { once: true });
    img.addEventListener('load', () => { plate.querySelector('.cover__ph').hidden = true; }, { once: true });
  }

  let ticking = false;
  function paint() {
    ticking = false;
    const y = window.scrollY;
    nav?.classList.toggle('scrolled', y > 4);
    if (!plate || reduce) return;
    const h = cover.offsetHeight || innerHeight;
    const p = Math.min(1, Math.max(0, y / (h * 0.85)));
    // Lift faster than the scroll, shrink a touch, fade out as the page slides over it.
    plate.style.transform = `translateY(${-p * h * 0.28}px) scale(${1 - p * 0.05})`;
    plate.style.opacity = String(1 - p * 0.95);
    page?.classList.toggle('page--lifted', p > 0.02 && p < 1);
  }
  window.addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(paint); } }, { passive: true });
  paint();

  // Replay the tree animation by re-inserting the SVG (CSS animations restart).
  document.querySelector('[data-replay]')?.addEventListener('click', () => {
    const svg = document.querySelector('.tree svg');
    if (!svg) return;
    const clone = svg.cloneNode(true);
    svg.replaceWith(clone);
  });
})();
