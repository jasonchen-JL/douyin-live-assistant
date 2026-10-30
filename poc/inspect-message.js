async page => await page.evaluate(() => {
  const rows = [...document.querySelectorAll('.webcast-chatroom___item')]
    .filter(row => row.querySelector('.webcast-chatroom___item-wrapper .cL385mHb'));
  return rows.slice(-6).map(row => {
    const start = row.querySelector('.cL385mHb');
    let fiber = start[Object.keys(start).find(key => key.startsWith('__reactFiber$'))];
    const ancestors = [];
    let detail = null, list = null;
    for (let depth = 0; fiber && depth < 18; depth++, fiber = fiber.return) {
      const props = fiber.memoizedProps || {};
      if (props.message && !detail) {
        const message = props.message;
        const payload = message.payload;
        const user = props.user || payload?.user;
        const describe = value => ({type: typeof value,
          value: typeof value === 'object' ? String(value) : value,
          keys: value && typeof value === 'object' ? Object.keys(value) : undefined});
        detail = {
          method: message.method, message_id: describe(message.msg_id),
          payload_keys: Object.keys(payload || {}),
          content: payload?.content, event_time: describe(payload?.event_time),
          message_extra: {keys: Object.keys(message.message_extra || {}),
            time_fields: Object.fromEntries(Object.entries(message.message_extra || {}).filter(([key, value]) => /time/i.test(key) && typeof value !== 'object'))},
          common_keys: Object.keys(payload?.common || {}),
          common: Object.fromEntries(['msg_id', 'room_id', 'create_time'].map(key => [key, describe(payload?.common?.[key])])),
          user: Object.fromEntries(['id', 'sec_uid', 'nickname', 'display_id', 'is_anonymous'].map(key => [key, describe(user?.[key])])),
          emoji: payload?.emoji_content, default_content: payload?.default_content
        };
      }
      if (Array.isArray(props.originalList) && !list) {
        list = props.originalList.map(item => ({keys: Object.keys(item || {}), method: item?.method,
          payload_keys: Object.keys(item?.payload || {})}));
      }
      const shape = {};
      for (const [key, value] of Object.entries(props)) {
        if (key === 'children') continue;
        shape[key] = value && typeof value === 'object'
          ? {type: Array.isArray(value) ? 'array' : 'object', keys: Object.keys(value).slice(0, 55)}
          : {type: typeof value, value: /^(id|msgId|userId|content|text|nickname|createTime|timestamp|type)$/.test(key) ? value : undefined};
      }
      ancestors.push({depth, component: typeof fiber.type === 'string' ? fiber.type : fiber.type?.name, shape});
    }
    return {text: row.textContent, detail, list};
  });
})
