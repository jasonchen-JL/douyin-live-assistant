async page => await page.evaluate(async () => {
  const resource = performance.getEntriesByType('resource').find(item => {
    const url = new URL(item.name);
    return url.origin === location.origin && url.pathname === '/webcast/user/me/';
  });
  if (!resource) return {verified: false, reason: 'No current-user request observed'};
  const response = await fetch(resource.name, {credentials: 'same-origin'});
  const body = await response.json();
  const user = body.data?.user || body.data;
  return {http_status: response.status, status_code: body.status_code,
    nickname: user?.nickname,
    user_id: user?.id_str || (typeof user?.id === 'string' ? user.id : null),
    display_id: user?.display_id};
})
