import { expect, test } from 'bun:test';
import { collectorPresentation } from '../src/collector-status';

test('a running process is not proof of readiness', () => {
  expect(collectorPresentation({process_running: true}).ready).toBe(false);
  expect(collectorPresentation({process_running: true}).label).toBe('启动校验中');
  expect(collectorPresentation({source_state: 'ready', process_running: false, source_ready: true}).ready).toBe(false);
});

test('waiting counts down from the backend deadline without renewing it', () => {
  const state = {source_state: 'waiting_live', recovery_started_at: 1000, recovery_deadline_at: 2800};
  expect(collectorPresentation(state, 1010000).detail).toContain('剩余 29 分 50 秒');
  expect(collectorPresentation(state, 2799000).detail).toContain('剩余 0 分 1 秒');
  expect(state.recovery_deadline_at).toBe(2800);
});

test('stop intent wins over a stale ready observation', () => {
  const view = collectorPresentation({status: 'stopping', source_state: 'ready', source_ready: true, process_running: true});
  expect(view.stopping).toBe(true);
  expect(view.ready).toBe(false);
  expect(view.label).toBe('正在停止');
});

test('timeout and manual-required states remain explicit', () => {
  expect(collectorPresentation({source_state: 'stopped_timeout', process_running: false}).detail).toBe('自动恢复已超时，请手动开始监听');
  expect(collectorPresentation({source_state: 'manual_required', source_reason: 'captcha_required'}).detail).toBe('直播页需要完成验证');
});
