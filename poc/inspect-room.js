async page => {
  return await page.evaluate(() => ({
    url: location.origin + location.pathname,
    title: document.title,
    anchor: document.querySelector('[data-e2e="live-room-nickname"]')?.textContent,
    videos: Array.from(document.querySelectorAll('video')).map(v => ({
      width: v.videoWidth, height: v.videoHeight, time: v.currentTime,
      readyState: v.readyState, paused: v.paused
    })),
    rows: Array.from(document.querySelectorAll('.webcast-chatroom___item')).slice(-15).map(row => {
      let fiber = row[Object.keys(row).find(k => k.startsWith('__reactFiber$'))];
      const propKeys = [];
      for (let depth = 0; fiber && depth < 5; depth++, fiber = fiber.return) {
        propKeys.push(Object.keys(fiber.memoizedProps || {}));
      }
      return {text: row.textContent, html: row.outerHTML.slice(0, 2400), propKeys};
    })
  }));
}
