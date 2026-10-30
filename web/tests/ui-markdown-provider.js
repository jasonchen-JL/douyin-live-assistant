async (page) => {
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const check = (ok, why) => { if (!ok) throw new Error(why); };
  await page.setViewportSize({width:1440,height:900});
  await page.goto('http://127.0.0.1:8768');
  await page.getByRole('button', {name:/模拟电商负责人/}).click();
  await page.locator('.analysis-markdown').waitFor();
  const a = await page.locator('.analysis-markdown').innerText();
  check(a.startsWith('主播直接说：') && a.includes('脱敏') && a.includes('不做自动回复'), 'Latest scope change missing');
  check((await page.locator('.analysis-footnote').last().innerText()).includes('3条'), 'Coverage did not advance to 3');
  check(await page.locator('.analysis-content .unrated').innerText() === '暂无星级评估', 'Unscored fallback missing');
  await page.screenshot({path:'runtime/markdown-provider-20260909/provider-desktop.png'});
  await page.getByRole('button', {name:/模拟门店负责人/}).click();
  await page.waitForFunction(() => document.querySelector('.analysis-markdown')?.textContent.includes('12家门店'));
  check(!(await page.locator('.analysis-markdown').innerText()).includes('5名客服'), 'Cross-viewer contamination');
  await page.getByRole('button', {name:/模拟电商负责人/}).click();
  await page.waitForFunction(() => document.querySelector('.analysis-markdown')?.textContent.includes('脱敏订单报表'));
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button', {name:'提词', exact:true}).click();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile overflow');
  await page.screenshot({path:'runtime/markdown-provider-20260909/provider-mobile.png'});
  check(!errors.length, errors.join('\n'));
  return {passed:true, actual_provider_results:true, scope_change_visible:true, unscored:true, errors};
}
