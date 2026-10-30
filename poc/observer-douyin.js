config => {
  const key = '__liveCommentPoc';
  if (window[key]) {
    if (window[key].resume) window[key].resume();
    return window[key].status();
  }
  document.querySelector(config.root);
  const documentId = crypto.randomUUID();
  const roomUrl = location.origin + location.pathname;
  let queue = [], sequence = 0, overflow = 0, cacheEvictions = 0;
  let rootPresent = false, listFound = false, listCount = 0, messageCount = 0;
  let rootSeen = false, scans = 0, scanErrors = 0, schemaRejected = 0;
  let lastScanAt = null, lastError = null, methods = {};
  let roomIds = [];
  let paused = false;
  const seen = new Set();
  const id = value => {
    if (typeof value === 'string' && value && value !== '0') return value;
    if (typeof value === 'bigint' && value > 0n) return String(value);
    if (Number.isSafeInteger(value) && value > 0) return String(value);
    return null;
  };
  const read = (message, phase) => {
    if (message?.method !== 'WebcastChatMessage') return;
    const payload = message.payload;
    const messageId = id(message.msg_id);
    if (!messageId || typeof payload?.content !== 'string' || !payload.content ||
        typeof payload?.user?.nickname !== 'string') {
      schemaRejected++;
      return;
    }
    if (seen.has(messageId)) return;
    if (queue.length >= (config.maxQueue || 10000)) { overflow++; return; }
    seen.add(messageId);
    if (seen.size > (config.maxSeen || 50000)) {
      seen.delete(seen.values().next().value);
      cacheEvictions++;
    }
    const userId = id(payload.user.id);
    const rawTime = id(payload.event_time);
    const eventTime = rawTime && /^\d{10}$/.test(rawTime) ? rawTime : null;
    sequence++;
    queue.push({
      event_id: documentId + ':' + sequence, document_id: documentId, seq: sequence,
      room_url: roomUrl, platform_room_id: id(payload.common?.room_id),
      nickname: payload.user.nickname, content: payload.content,
      user_id: userId, user_id_kind: userId ? 'douyin_user_id' : null,
      platform_message_id: messageId, common_message_id: id(payload.common?.msg_id),
      source_time_raw: eventTime,
      source_time_kind: eventTime ? 'payload.event_time_unix_seconds' : null,
      captured_at: new Date().toISOString(), phase,
      message_type: message.method, data_source: 'douyin_react_originalList'
    });
  };
  const scan = phase => {
    if (paused || location.origin + location.pathname !== roomUrl) return;
    scans++;
    lastScanAt = new Date().toISOString();
    try {
      const roots = [...document.querySelectorAll(config.root)];
      rootPresent = roots.length > 0;
      rootSeen ||= rootPresent;
      const lists = new Set();
      for (const root of roots) {
        const elements = [root, ...root.querySelectorAll('.webcast-chatroom___item')];
        for (const element of elements) {
          let fiber = element[Object.keys(element).find(name => name.startsWith('__reactFiber$'))];
          for (let depth = 0; fiber && depth < 24; depth++, fiber = fiber.return) {
            // React can leave a host node pointing at the previous alternate.
            for (const candidate of [fiber, fiber.alternate]) {
              const list = candidate?.memoizedProps?.originalList;
              if (Array.isArray(list)) lists.add(list);
            }
          }
        }
      }
      listFound = lists.size > 0;
      listCount = lists.size;
      const messages = new Set([...lists].flat());
      roomIds = [...new Set([...messages].map(message => id(message?.payload?.common?.room_id)).filter(Boolean))];
      messageCount = messages.size;
      methods = {};
      for (const message of messages) {
        const method = typeof message?.method === 'string' ? message.method : 'unknown';
        methods[method] = (methods[method] || 0) + 1;
        read(message, phase);
      }
    } catch (error) {
      scanErrors++;
      lastError = String(error.message).slice(0, 200);
    }
  };
  const observer = new MutationObserver(() => scan('live'));
  observer.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
  // This also samples state updates that do not change rendered text.
  let timer = setInterval(() => scan('live'), config.scanIntervalMs || 250);
  const configure = settings => {
    if (!settings) return;
    if (settings.scanIntervalMs !== undefined && settings.scanIntervalMs !== config.scanIntervalMs) {
      const interval = settings.scanIntervalMs;
      if (!Number.isFinite(interval) || interval < 50 || interval > 2000) throw new Error('Invalid scan interval');
      config.scanIntervalMs = interval;
      clearInterval(timer);
      if (!paused) timer = setInterval(() => scan('live'), interval);
    }
  };
  const status = () => ({
    document_id: documentId, url: location.origin + location.pathname,
    root_present: rootPresent, root_seen: rootSeen, matched_rows: messageCount,
    list_found: listFound, list_count: listCount, methods,
    queued: queue.length, observed: sequence, overflow, cache_evictions: cacheEvictions,
    scans, scan_errors: scanErrors, schema_rejected: schemaRejected,
    last_scan_at: lastScanAt, last_error: lastError, data_source: 'douyin_react_originalList',
    paused, scan_interval_ms: config.scanIntervalMs || 250,
    online: navigator.onLine, visibility: document.visibilityState,
    page_title: document.title, platform_room_ids: roomIds,
    room_health: window.__liveRoomHealth?.() || null
  });
  window[key] = {
    status,
    poll(ack, settings) {
      configure(settings);
      if (ack?.document_id === documentId) queue = queue.filter(event => event.seq > ack.seq);
      scan('live');
      return {...status(), events: queue.slice(0, 1000)};
    },
    pause() { scan('live'); paused = true; observer.disconnect(); clearInterval(timer); return status(); },
    resume() {
      if (!paused) return;
      paused = false;
      observer.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
      timer = setInterval(() => scan('live'), config.scanIntervalMs || 250);
    },
    stop() { observer.disconnect(); clearInterval(timer); delete window[key]; }
  };
  scan('initial');
  return status();
}
