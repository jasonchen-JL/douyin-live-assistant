() => {
  const visible = element => element.getClientRects().length > 0 &&
    getComputedStyle(element).visibility !== 'hidden' && getComputedStyle(element).display !== 'none';
  const roots = document.querySelectorAll('[data-e2e="live-room-nickname"],.webcast-chatroom___list,.douyin-player');
  const rooms = new Map();
  const stringId = value => typeof value === 'string' && /^\d+$/.test(value) ? value : null;
  for (const node of roots) {
    let fiber = node[Object.keys(node).find(key => key.startsWith('__reactFiber$'))];
    for (let depth = 0; fiber && depth < 28; depth++, fiber = fiber.return) {
      // Only current roomStore fields, never message history or recommendation cards.
      for (const candidate of [fiber, fiber.alternate]) {
        const info = candidate?.memoizedProps?.liveContainer?._store?.roomStore?.roomInfo;
        if (!info?.room) continue;
        const roomId = stringId(info.room.id_str) || stringId(info.roomId);
        const webRid = stringId(info.web_rid);
        const status = info.room.status;
        if (roomId && webRid) rooms.set(`${roomId}:${status}:${webRid}`, {
          room_id: roomId, web_rid: webRid, room_status: status
        });
      }
    }
  }
  const player = document.querySelector('.douyin-player,.LivePlayer_LivingPlayer');
  const endedVisible = !!player && [...player.querySelectorAll('div,span,p')].some(element =>
    visible(element) && element.children.length === 0 &&
    /^(直播已结束|主播已下播|主播暂未开播|暂未开播)$/.test(element.textContent.trim()));
  const captcha = [...document.querySelectorAll('#captcha_container,#captcha-verify-container,[class*="captcha_verify"]')].some(visible);
  const login = [...document.querySelectorAll('[role="dialog"],[class*="login-modal"],[class*="loginModal"]')].some(element =>
    visible(element) && /登录后.*(观看|直播)|登录.*继续观看/.test(element.innerText || ''));
  const values = [...rooms.values()];
  return {
    observed_at: new Date().toISOString(), online: navigator.onLine,
    room_signal: values.length === 1 ? values[0] : null,
    room_signal_conflict: values.length > 1, ended_visible: endedVisible,
    manual_reason: captcha ? 'captcha_required' : login ? 'login_required' : null
  };
}
