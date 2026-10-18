(function (root) {
  'use strict';
  function selection(range, sheet, source) {
    const element = node => node.nodeType === 1 ? node : node.parentElement;
    const first = element(range.startContainer)?.closest('td[data-start], th[data-start]');
    const last = element(range.endContainer)?.closest('td[data-start], th[data-start]');
    const table = first?.closest('table');
    if (!table || !sheet.contains(table) || !last || last.closest('table') !== table || first === last) return null;
    const cells = [...table.querySelectorAll('td[data-start], th[data-start]')];
    const selected = cells.filter(cell => range.intersectsNode(cell));
    if (!selected.length) return null;
    const firstOffset = Math.min(...selected.map(cell => Number(cell.dataset.start)));
    const lastOffset = Math.max(...selected.map(cell => Number(cell.dataset.end)));
    const start = source.lastIndexOf('\n', firstOffset - 1) + 1;
    const lineEnd = source.indexOf('\n', lastOffset);
    const end = lineEnd < 0 ? source.length : lineEnd;
    const quote = source.slice(start, end);
    if (!quote.trim() || quote.length > 6000) return null;
    const header = table.querySelector('th[data-start]');
    let tableContext = quote;
    if (header) {
      const headerStart = source.lastIndexOf('\n', Number(header.dataset.start) - 1) + 1;
      const headerEnd = source.indexOf('\n', headerStart);
      const separatorEnd = source.indexOf('\n', headerEnd + 1);
      if (headerStart < start && headerEnd >= 0 && separatorEnd >= 0) {
        tableContext = source.slice(headerStart, separatorEnd) + '\n' + quote;
      }
    }
    return { quote, tableContext, exactStart: start, exactEnd: end,
      sourceStart: start, sourceEnd: end, prefix: source.slice(Math.max(0, start - 32), start),
      suffix: source.slice(end, end + 32) };
  }
  root.TLMTableSelection = { selection };
})(window);
