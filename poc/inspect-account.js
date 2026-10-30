async page => await page.evaluate(() => {
  const editors = [...document.querySelectorAll('textarea,input,[contenteditable="true"]')]
    .filter(element => element.getBoundingClientRect().width > 0);
  return {
    url: location.origin + location.pathname,
    account_requests: performance.getEntriesByType('resource').map(item => new URL(item.name))
      .filter(url => /user|account|passport/.test(url.pathname))
      .map(url => url.origin + url.pathname).filter((url, index, all) => all.indexOf(url) === index),
    editors: editors.map(element => {
      let fiber = element[Object.keys(element).find(key => key.startsWith('__reactFiber$'))];
      const ancestors = [];
      for (let depth = 0; fiber && depth < 12; depth++, fiber = fiber.return) {
        const props = fiber.memoizedProps || {};
        ancestors.push({depth, keys: Object.keys(props),
          identity: Object.fromEntries(Object.entries(props).filter(([key, value]) =>
            /^(nickname|userId|user_id|uid|loginUid|loginSecUid|isLogin)$/.test(key) &&
            ['string', 'boolean'].includes(typeof value)))});
      }
      return {tag: element.tagName, role: element.getAttribute('role'),
        placeholder: element.getAttribute('placeholder') || element.getAttribute('data-placeholder'),
        html: element.outerHTML.slice(0, 1200), ancestors};
    })
  };
})
