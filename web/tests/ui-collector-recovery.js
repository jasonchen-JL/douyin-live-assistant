async page => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const base = 'http://127.0.0.1:8767';
  const original = await (await page.request.get(base + '/api/status')).json();
  if (original.collector.status !== 'isolated_test') throw new Error('isolated demo required');
  let collector = {process_running: true, status: 'opening', source_state: 'opening', source_ready: false};
  let commands = [];
  await page.route('**/api/status', route => route.fulfill({json: {...original, collector}}));
  await page.route('**/api/collector/*', route => {
    commands.push(route.request().url().split('/').pop());
    collector = {...collector, status: 'stopping', source_state: 'stopping', source_ready: false};
    return route.fulfill({json: {status: 'stop_requested'}});
  });
  await page.setViewportSize({width:1440, height:1000});
  await page.goto(base);
  await page.locator('.connection').filter({hasText:'启动校验中'}).waitFor();
  if (await page.locator('.connection').filter({hasText:'正在监听'}).count()) throw new Error('false readiness');
  collector = {...collector, source_state: 'waiting_live', source_reason: 'live_ended',
    recovery_started_at: Date.now()/1000 - 100, recovery_deadline_at: Date.now()/1000 + 1700};
  await page.locator('.connection').filter({hasText:'等待开播'}).waitFor();
  await page.locator('.collector-notice').filter({hasText:'剩余'}).waitFor();
  await page.screenshot({path:'runtime/collector-recovery-desktop.png'});
  await page.getByRole('button', {name:'停止监听', exact:true}).click();
  const stopping = page.getByRole('button', {name:'正在停止', exact:true});
  await stopping.waitFor();
  if (!await stopping.isDisabled() || commands.join() !== 'stop') throw new Error('stop not serialized');
  collector = {process_running:false, status:'stopped_timeout', source_state:'stopped_timeout',
    source_ready:false, manual_restart_required:true, source_reason:'recovery_timeout'};
  await page.locator('.connection').filter({hasText:'自动恢复已超时'}).waitFor();
  await page.getByRole('button', {name:'开始监听', exact:true}).waitFor();
  await page.setViewportSize({width:390, height:844});
  await page.screenshot({path:'runtime/collector-recovery-mobile.png'});
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1)) throw new Error('mobile overflow');
  await page.reload();
  await page.locator('.collector-mobile-state').filter({hasText:'自动恢复已超时'}).waitFor();
  if (commands.length !== 1) throw new Error('GET triggered restart');
  collector = {process_running:true, status:'running', source_state:'ready', source_ready:true};
  await page.locator('.collector-mobile-state').filter({hasText:'正在监听'}).waitFor();
  if (errors.length) throw new Error('page errors');
  return {page_errors:0, writes_to_backend:0, states:['opening','waiting_live','stopping','stopped_timeout','ready'], mobile_overflow:false};
}
