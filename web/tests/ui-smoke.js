async (page) => {
  const errors = []
  page.on("pageerror", (error) => errors.push(error.message))
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text())
  })
  const check = (value, message) => {
    if (!value) throw new Error(message)
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto("http://127.0.0.1:8766")
  await page.locator(".viewer-row").first().waitFor()
  const before = await (
    await page.request.get("http://127.0.0.1:8766/api/status")
  ).json()
  check(
    before.collector.status === "isolated_test",
    "Must use isolated fixture",
  )
  const buyerList = await (await page.request.get('http://127.0.0.1:8766/api/viewers?query=模拟电商负责人')).json()
  const expectedComments = buyerList.items[0].comment_count
  await page.getByRole("button", { name: "有效客户", exact: true }).click()
  await page.getByRole("textbox", { name: "检索观众昵称" }).fill("模拟未分析")
  await page.waitForFunction(
    () =>
      document.querySelectorAll(".viewer-row").length === 1 &&
      document.querySelector(".viewer-row").textContent.includes("模拟未分析"),
  )
  await page.locator(".viewer-row").click()
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === 1,
  )
  check(
    (await page.locator(".identity-line").innerText()).includes(
      "demo-unscored",
    ),
    "Unscored viewer identity",
  )
  await page.getByRole("textbox", { name: "检索观众昵称" }).fill("模拟同名")
  await page.waitForFunction(
    () => document.querySelectorAll(".viewer-row").length === 2,
  )
  await page.locator(".viewer-row").first().click()
  const identityA = await page.locator(".identity-line").innerText()
  await page.locator(".viewer-row").nth(1).click()
  check(
    identityA !== (await page.locator(".identity-line").innerText()),
    "Same-name viewers must remain distinct",
  )
  await page.getByRole("textbox", { name: "检索观众昵称" }).fill("模拟旧昵称")
  await page.waitForFunction(
    () =>
      document.querySelectorAll(".viewer-row").length === 1 &&
      document
        .querySelector(".viewer-row")
        .textContent.includes("模拟电商负责人"),
  )
  await page.locator(".viewer-row").click()
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === 50,
  )
  await page.locator(".script-section p").waitFor()
  await page.locator(".comments").evaluate((el) => {
    el.scrollTop = 0
  })
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length >= 100,
  )
  await page.locator(".comments").evaluate((el) => {
    el.scrollTop = 0
  })
  await page.waitForFunction(
    count => document.querySelectorAll(".comment").length === count, expectedComments,
  )
  check(
    new Set(
      await page.locator(".comment").evaluateAll((els) => els.map((e) => e.id)),
    ).size === expectedComments,
    "History duplicate",
  )
  await page.getByRole("button", { name: "清除搜索" }).click()
  await page.getByRole("button", { name: "全部观众", exact: true }).click()
  await page.waitForFunction(
    () => document.querySelectorAll(".viewer-row").length === 8,
  )
  await page.screenshot({ path: "runtime/validation-ui/desktop.png" })

  await page.getByRole("button", { name: "设置", exact: true }).click()
  await page.getByRole("button", { name: "粗筛模型", exact: true }).click()
  await page
    .getByLabel("模型名称", { exact: true })
    .fill("fixture-screening-only")
  await page.getByRole("button", { name: "保存", exact: true }).click()
  await page.getByText("已保存", { exact: true }).waitFor()
  await page.getByRole("button", { name: "精准模型", exact: true }).click()
  check(
    (await page.getByLabel("模型名称", { exact: true }).inputValue()) ===
      "synthetic-fixture",
    "Independent model settings",
  )
  await page.getByRole("button", { name: "关闭设置", exact: true }).click()

  const sizes = [
    [1920, 1080],
    [1024, 768],
    [390, 844],
    [320, 700],
  ]
  for (const [width, height] of sizes) {
    await page.setViewportSize({ width, height })
    if (width < 800)
      await page
        .locator(".mobile-tabs")
        .getByRole("button", { name: "提词", exact: true })
        .click()
    const geometry = await page.evaluate(() => ({
      viewport: innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      buttons: [...document.querySelectorAll("button")]
        .filter((e) => e.getClientRects().length)
        .filter((e) => e.scrollWidth > e.clientWidth + 2)
        .map((e) => e.getAttribute("aria-label") || e.textContent),
      panes: [...document.querySelectorAll(".workspace > section")]
        .filter((e) => getComputedStyle(e).display !== "none")
        .map((e) => {
          const r = e.getBoundingClientRect()
          return { x: r.x, right: r.right, width: r.width }
        }),
    }))
    check(
      geometry.scrollWidth <= width + 1,
      "Horizontal page overflow " + JSON.stringify(geometry),
    )
    check(
      geometry.buttons.length === 0,
      "Button text overflow " + JSON.stringify(geometry),
    )
    for (let i = 1; i < geometry.panes.length; i++)
      check(
        geometry.panes[i].x >= geometry.panes[i - 1].right - 1,
        "Pane overlap",
      )
    await page.screenshot({ path: `runtime/validation-ui/view-${width}.png` })
    await page.getByRole("button", { name: "设置", exact: true }).click()
    check(
      await page
        .locator("dialog")
        .evaluate((el) => el.getBoundingClientRect().right <= innerWidth),
      "Dialog overflows",
    )
    await page.screenshot({
      path: `runtime/validation-ui/settings-${width}.png`,
    })
    await page.getByRole("button", { name: "关闭设置", exact: true }).click()
  }
  const after = await (
    await page.request.get("http://127.0.0.1:8766/api/status")
  ).json()
  check(
    JSON.stringify(before.jobs) === JSON.stringify(after.jobs),
    "UI reading unexpectedly invoked models",
  )
  check(errors.length === 0, "Browser errors: " + JSON.stringify(errors))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.locator(".comments").evaluate((el) => {
    el.scrollTop = 100
  })
  await page.evaluate(count => { window.fixtureHistoryCount = count }, expectedComments)
  return {
    passed: true,
    checks: [
      "search-unscored",
      "same-name-ids",
      "old-nickname",
      "history-pagination",
      "model-settings-independent",
      "read-only-no-model",
      "desktop-mobile-layout",
    ],
    errors,
  }
}
