/* TutorLM welcome page · example projects under "开始试用".
   Each card opens a ready-made project (circled concepts, explanations, follow-ups,
   semantic links and a narration) through its share code: the visitor gets an
   independent copy, the original is never changed. Share codes expire (7 days);
   replace them here when they are re-issued. */
(function () {
  'use strict';
  const EXAMPLES = [
    { kicker: '生活 · 菜谱', title: '灶台边的日常', desc: '焯水、炒糖色、收汁、勾芡：把"火候适中"讲成为什么这样做。',
      concepts: ['焯水', '炒糖色', '收汁', '勾芡'], code: 'TLM-SHARE-v1-QF-Xb6XiQ-lLZwmlXN4DOAjwAiB6POnYNzMEfMghy-g' },
    { kicker: '计算机 · 论文', title: 'PageRank 与马尔可夫链', desc: '从平稳分布到幂迭代法，带公式推导的网页排序数学基础。',
      concepts: ['平稳分布', '悬挂节点', '阻尼因子', 'Google 矩阵', '幂迭代法'], code: 'TLM-SHARE-v1-MPQqJR64H7YI830zZr09vXpu3CojMeuvoQ0ST4NKeD0' },
    { kicker: '哲学 · 论述', title: '自由意志与道德责任', desc: '决定论之后我们还能追究谁？相容论、后果论论证与反应态度。',
      concepts: ['决定论', '他行可能性原则', '相容论', '后果论论证', '反应态度'], code: 'TLM-SHARE-v1-oeDJ61G2tRwAdmMLmvVpwgSpSh6itY-sl1dSBnrrnG4' },
    { kicker: '文化 · 历史', title: '中国互联网抽象文化', desc: '从火星文、杀马特到鬼畜与抽象话，一部"梗"的发展史。',
      concepts: ['火星文', '杀马特', '表情包', '鬼畜', '抽象话'], code: 'TLM-SHARE-v1-OMR41-VOytmd_cBNViUjqah5U5TY42amB3s4kEa-44w' },
    { kicker: '法律 · 案例', title: '自甘风险与安全保障义务', desc: '小区篮球赛中的一次摔倒：民法典条文、责任分配与赔偿计算。',
      concepts: ['自甘风险', '安全保障义务', '过错责任', '重大过失', '精神损害抚慰金'], code: 'TLM-SHARE-v1-NemO7VenSroTc_zn0o456M3TCM_-V8PbMhv5f37XiSU' },
  ];
  const host = document.querySelector('#examples');
  if (!host) return;
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  host.innerHTML = EXAMPLES.map((e, i) => `<button type="button" class="example" data-example="${i}" aria-label="打开示例：${esc(e.title)}">
      <span class="card__idx">案例 ${String(i + 1).padStart(2, '0')} · ${esc(e.kicker)}</span>
      <span class="example__title">${esc(e.title)}</span>
      <span class="example__desc">${esc(e.desc)}</span>
      <span class="example__tags">${e.concepts.slice(0, 2).map((c) => `<i>${esc(c)}</i>`).join('')}${e.concepts.length > 2 ? `<i>+${e.concepts.length - 2}</i>` : ''}</span>
      <span class="example__go">打开示例 <svg class="ic"><use href="#i-arrow-r"/></svg></span>
    </button>`).join('');
  /* A card fills the share code and starts the normal entry flow (lease, copy, open). */
  host.addEventListener('click', (event) => {
    const card = event.target.closest('[data-example]');
    const gate = document.querySelector('#gate'), input = document.querySelector('#share-code-input');
    if (!card || !gate || !input || document.querySelector('#start')?.disabled) return;
    input.value = EXAMPLES[Number(card.dataset.example)].code;
    host.querySelectorAll('.example').forEach((el) => el.classList.toggle('is-picked', el === card));
    gate.requestSubmit();
  });
})();
