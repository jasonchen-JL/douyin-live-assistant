async page => {
  const responsePromise = page.waitForResponse(response => {
    return /^https:\/\/live\.douyin\.com\/webcast\/room\/web\/enter\/(?:\?|$)/.test(response.url());
  }, {timeout: 20000});
  const reload = page.reload({waitUntil: 'domcontentloaded'});
  const [response] = await Promise.all([responsePromise, reload]);
  const body = await response.json();
  const room = body.data?.data?.[0];
  return {
    room_url: await page.evaluate(() => location.origin + location.pathname),
    http_status: response.status(), api_status: body.status_code,
    platform_room_id: room?.id_str || (typeof room?.id === 'string' ? room.id : null),
    room_status: room?.status, anchor: room?.owner?.nickname,
    title: room?.title, received_at: new Date().toISOString()
  };
}
