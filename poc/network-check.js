async page => {
  const sockets = [], failures = [], statuses = [];
  const path = raw => raw.split(/[?#]/, 1)[0];
  const onSocket = socket => {
    const record = {url: path(socket.url()), frames: 0, bytes: 0, closed: false};
    sockets.push(record);
    socket.on('framereceived', frame => { record.frames++; record.bytes += frame.payload.length; });
    socket.on('close', () => { record.closed = true; });
  };
  const onFailure = req => {
    if (/webcast|im\/|douyin/.test(req.url())) {
      failures.push({url: path(req.url()), error: req.failure()?.errorText});
    }
  };
  const onResponse = res => {
    if (['fetch', 'xhr'].includes(res.request().resourceType()) && /\/webcast\/|\/im\//.test(res.url())) {
      statuses.push({url: path(res.url()), status: res.status()});
    }
  };
  page.on('websocket', onSocket);
  page.on('requestfailed', onFailure);
  page.on('response', onResponse);
  try {
    await page.reload({waitUntil: 'domcontentloaded'});
    await page.waitForTimeout(15000);
    return {sockets, failures: failures.slice(0, 15), statuses: statuses.slice(0, 15),
      chat: await page.locator('.webcast-chatroom___list').count(),
      chatTextLength: await page.locator('.webcast-chatroom___list').innerText().then(s => s.length).catch(() => null)};
  } finally {
    page.off('websocket', onSocket);
    page.off('requestfailed', onFailure);
    page.off('response', onResponse);
  }
}
