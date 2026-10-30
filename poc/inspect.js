async page => {
  return await page.evaluate(() => ({
    url: location.origin + location.pathname,
    title: document.title,
    text: document.body.innerText.slice(-7000),
    rooms: Array.from(document.querySelectorAll('a'))
      .filter(a => /^https:\/\/live\.douyin\.com\/\d+/.test(a.href))
      .slice(0, 10).map(a => ({url: a.href.split('?')[0], text: a.innerText.slice(0, 80)})),
    candidates: Array.from(document.querySelectorAll('[class]'))
      .filter(e => /chat|comment|message/.test(e.className))
      .slice(-30).map(e => ({tag: e.tagName, cls: e.className, html: e.outerHTML.slice(0, 1800)})),
    selectors: Array.from(document.querySelectorAll('[data-e2e]'))
      .map(e => e.getAttribute('data-e2e')).filter((v, i, a) => a.indexOf(v) === i)
      .filter(v => /chat|message|comment|room/.test(v))
  }));
}
