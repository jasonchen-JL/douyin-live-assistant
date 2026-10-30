config => {
  const key = '__liveCommentPoc';
  if (window[key]) {
    if (window[key].resume) window[key].resume();
    return window[key].status();
  }
  for (const selector of [config.root, config.row, config.nickname, config.content,
    config.userLink, config.sourceTime].filter(Boolean)) document.querySelector(selector);
  const documentId = crypto.randomUUID();
  const seen = new WeakMap();
  let queue = [], sequence = 0, overflow = 0, rootSeen = false;
  let paused = false;
  const text = element => element?.textContent?.trim() || null;
  const attr = (element, name) => name ? element?.getAttribute(name) || null : null;
  const read = (row, phase) => {
    const nickname = text(row.querySelector(config.nickname));
    const content = text(row.querySelector(config.content));
    if (!nickname || !content) return;
    const link = config.userLink ? row.querySelector(config.userLink) : null;
    let userId = attr(row, config.userIdAttribute), userIdKind = userId ? 'dom_attribute' : null;
    if (!userId && link) {
      const url = new URL(link.getAttribute('href'), location.href);
      const match = url.pathname.match(/^\/user\/([^/]+)\/?$/);
      if (match && (url.hostname === 'www.douyin.com' || url.hostname === 'douyin.com')) {
        userId = match[1];
        userIdKind = 'profile_path';
      }
    }
    const sourceTimeRaw = config.sourceTime
      ? attr(row.querySelector(config.sourceTime), config.sourceTimeAttribute) : null;
    const messageId = attr(row, config.messageIdAttribute);
    const fingerprint = JSON.stringify([nickname, content, userId, messageId, sourceTimeRaw]);
    if (seen.get(row) === fingerprint) return;
    seen.set(row, fingerprint);
    sequence += 1;
    if (queue.length >= (config.maxQueue || 10000)) { overflow += 1; return; }
    queue.push({
      event_id: documentId + ':' + sequence,
      seq: sequence, document_id: documentId,
      room_url: location.origin + location.pathname,
      nickname, content, user_id: userId, user_id_kind: userIdKind,
      platform_message_id: messageId, source_time_raw: sourceTimeRaw,
      captured_at: new Date().toISOString(), phase
    });
  };
  const visit = (node, phase) => {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    if (!element) return;
    const parentRow = element.closest(config.row);
    if (parentRow && parentRow.closest(config.root)) read(parentRow, phase);
    for (const row of element.querySelectorAll(config.row)) {
      if (row.closest(config.root)) read(row, phase);
    }
  };
  // Observe inserted and reused rows, not a periodically sampled screenshot.
  const observer = new MutationObserver(records => {
    for (const record of records) {
      visit(record.target, 'live');
      for (const node of record.addedNodes) visit(node, 'live');
    }
  });
  const observeOptions = {
    childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: [config.userIdAttribute, config.messageIdAttribute,
      config.sourceTimeAttribute, 'href'].filter(Boolean)
  };
  observer.observe(document.documentElement, observeOptions);
  const status = () => {
    const root = document.querySelector(config.root);
    rootSeen ||= !!root;
    return {document_id: documentId, url: location.origin + location.pathname,
      root_present: !!root, root_seen: rootSeen,
      matched_rows: root ? root.querySelectorAll(config.row).length : 0,
      queued: queue.length, observed: sequence, overflow, paused};
  };
  window[key] = {
    status,
    poll(ack) {
      if (ack?.document_id === documentId) queue = queue.filter(event => event.seq > ack.seq);
      return {...status(), events: queue.slice(0, 1000)};
    },
    pause() { paused = true; observer.disconnect(); return status(); },
    resume() { if (paused) { paused = false; observer.observe(document.documentElement, observeOptions); } },
    stop() { observer.disconnect(); delete window[key]; }
  };
  visit(document.documentElement, 'initial');
  return status();
}
