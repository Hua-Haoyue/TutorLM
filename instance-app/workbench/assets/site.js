/* Public pages: nav shadow on scroll, cover lift/fade, demo replay. */
(function () {
  'use strict';
  const nav = document.querySelector('.nav');
  const plate = document.querySelector('.cover__plate');
  const cover = document.querySelector('.cover');
  const page = document.querySelector('.page');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Cover video: autoplay muted loop; fall back to the placeholder only when it
  // cannot play (missing file / autoplay blocked / decode error).
  const video = plate?.querySelector('video');
  if (video) {
    const ph = plate.querySelector('.cover__ph');
    const fallback = () => { video.remove(); ph.hidden = false; };
    if (reduce) {
      // Respect reduced motion: keep the poster, do not start playback.
      video.autoplay = false;
      video.removeAttribute('autoplay');
      video.pause();
    } else {
      const started = video.play();
      if (started) started.catch(fallback);
      video.addEventListener('error', fallback, { once: true });
    }
  }

  // Cover title: the self-writing SVG strokes are inlined by
  // assets/cover-writing.js. The plain text below stays as a fallback when that
  // module is unavailable (offline assets, reduced motion).
  const brandEl = document.getElementById('cover-brand');
  const taglineEl = document.getElementById('cover-tagline');
  if (brandEl && taglineEl && !brandEl.textContent.trim()) {
    brandEl.textContent = 'TutorLM';
    taglineEl.textContent = '懂 你 意 思';
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
