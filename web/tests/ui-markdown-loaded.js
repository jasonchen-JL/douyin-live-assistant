async (page) => {
  const errors = [], checked = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.setViewportSize({width:1440,height:900});
  for (const port of [8765, 8767]) {
    await page.goto(`http://127.0.0.1:${port}`);
    const status = await (await page.request.get(`http://127.0.0.1:${port}/api/status`)).json();
    const settings = await (await page.request.get(`http://127.0.0.1:${port}/api/settings`)).json();
    if (settings.general.analysis_mode !== 'markdown_direct') throw new Error('Mode not loaded');
    const latest = status.days[0];
    if (latest) {
      await page.getByRole('textbox', {name:'检索观众昵称'}).waitFor();
      await page.getByRole('button', {name:'有效客户', exact:true}).waitFor();
      await page.getByLabel('开始日期', {exact:true}).fill(latest);
      await page.getByLabel('结束日期', {exact:true}).fill(latest);
      await page.locator('.viewer-row').first().click();
      await page.locator('.comment').first().waitFor();
      await page.locator('.script-section p, .analysis-markdown').first().waitFor();
    }
    await page.getByRole('button', {name:'设置', exact:true}).click();
    await page.getByRole('button', {name:'单观众 Markdown 提词', exact:true}).waitFor();
    if (!await page.getByRole('button', {name:'粗筛后精准分析（保留）', exact:true}).isDisabled()) throw new Error('Old mode still active in UI');
    await page.getByRole('button', {name:'关闭设置', exact:true}).click();
    checked.push({port, comments:status.comments, latest_day:latest, mode:settings.general.analysis_mode});
  }
  if (errors.length) throw new Error(errors.join('\n'));
  return {checked, errors};
}
