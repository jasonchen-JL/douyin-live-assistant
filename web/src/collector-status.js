const labels = {
  opening: '启动校验中',
  ready: '正在监听',
  waiting_live: '等待开播',
  recovering: '正在恢复',
  manual_required: '需要人工处理',
  stopped_timeout: '自动恢复已超时',
  stopping: '正在停止',
  stopped: '采集已停止',
};
const reasons = {
  live_ended: '等待主播开播',
  browser_offline: '浏览器离线',
  room_state_unknown: '正在确认直播状态',
  room_state_conflict: '直播状态尚未一致',
  comment_source_unavailable: '评论通道暂不可用',
  observer_missing: '正在恢复评论监听',
  observer_stalled: '评论监听心跳中断',
  collector_heartbeat_stale: '采集程序心跳中断',
  browser_transport_failed: '暂时无法连接采集浏览器',
  captcha_required: '直播页需要完成验证',
  login_required: '直播页需要重新登录',
  room_changed: '直播页已偏离指定直播间',
  browser_profile_mismatch: '采集浏览器登录资料不匹配',
  storage_write_failed: '评论写入失败，请检查本地存储',
  source_read_failed: '评论导入异常',
  collector_process_exited: '采集意外中断，请手动开始监听',
  confirming_source: '正在确认评论与存储通路',
};

export function collectorPresentation(collector = {}, now = Date.now()) {
  let phase = collector.source_state || (collector.process_running ? 'opening' : 'stopped');
  if (collector.status === 'stopping' || collector.status === 'draining') phase = 'stopping';
  if (phase === 'ready' && (!collector.process_running || !collector.source_ready)) {
    phase = collector.process_running ? 'opening' : 'stopped';
  }
  let detail = reasons[collector.source_reason] || '';
  if (phase === 'stopped_timeout') detail = '自动恢复已超时，请手动开始监听';
  const remaining = collector.recovery_deadline_at && ['opening', 'waiting_live', 'recovering'].includes(phase)
    ? Math.max(0, Math.ceil(collector.recovery_deadline_at - now / 1000)) : null;
  if (remaining !== null) {
    const elapsed = Math.max(0, Math.floor(now / 1000 - collector.recovery_started_at));
    detail += `${detail ? ' · ' : ''}已等待 ${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒，剩余 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒`;
  }
  if (phase === 'ready' && (collector.warnings || []).some(w => /gap|overflow/.test(w))) {
    detail = '采集曾中断，间隙期间的评论可能不完整';
  }
  return { phase, label: labels[phase] || labels.stopped, detail,
    ready: phase === 'ready', stopping: phase === 'stopping' };
}
