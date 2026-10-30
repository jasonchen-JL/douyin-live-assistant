async (page) => {
  const errors = []
  page.on("pageerror", (e) => errors.push(e.message))
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto("http://127.0.0.1:8767")
  await page.locator(".viewer-row").first().waitFor()
  const before = await (
    await page.request.get("http://127.0.0.1:8767/api/status")
  ).json()
  if (before.comments !== 6 || before.collector.status !== "isolated_test")
    throw new Error("Incorrect real-model fixture")
  await page.getByRole("button", { name: "待回复", exact: true }).click()
  await page.waitForFunction(
    () => document.querySelectorAll(".viewer-row").length === 2,
  )
  await page.locator(".viewer-row").filter({ hasText: "模拟技术咨询" }).click()
  await page.locator(".script-section p").waitFor()
  if (!(await page.locator(".intent-heading").innerText()).includes("回复"))
    throw new Error("Low-intent question missing reply")
  await page.getByRole("button", { name: "有效客户", exact: true }).click()
  await page.waitForFunction(
    () => document.querySelectorAll(".viewer-row").length === 1,
  )
  await page.locator(".viewer-row").click()
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === 4,
  )
  await page.waitForFunction(() =>
    document.querySelector(".analysis-content").textContent.includes("私有"),
  )
  await page.screenshot({
    path: "runtime/validation-real-v3/provider-desktop.png",
  })
  await page.setViewportSize({ width: 390, height: 844 })
  await page
    .locator(".mobile-tabs")
    .getByRole("button", { name: "提词", exact: true })
    .click()
  await page.screenshot({
    path: "runtime/validation-real-v3/provider-mobile.png",
  })
  const after = await (
    await page.request.get("http://127.0.0.1:8767/api/status")
  ).json()
  if (
    JSON.stringify(before.jobs) !== JSON.stringify(after.jobs) ||
    errors.length
  )
    throw new Error("Read-only UI contract failed")
  return {
    passed: true,
    needsReplyIncludesTwoStarViewer: true,
    privateDeploymentUpdate: true,
    browserErrors: errors,
  }
}
