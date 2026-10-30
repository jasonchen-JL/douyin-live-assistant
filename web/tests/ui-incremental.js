async (page) => {
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === window.fixtureHistoryCount + 1,
  )
  await page.locator(".unread-button").waitFor()
  const position = await page.locator(".comments").evaluate((el) => ({
    top: el.scrollTop,
    height: el.scrollHeight,
    visible: el.clientHeight,
  }))
  if (position.top > position.height - position.visible - 100)
    throw new Error("New comment moved the reader to the bottom")
  const selected = await page.locator(".identity-line").innerText()
  if (!selected.includes("demo-buyer"))
    throw new Error("Polling stole selected viewer")
  await page.locator(".unread-button").click()
  await page.waitForFunction(() => {
    const el = document.querySelector(".comments")
    return el.scrollHeight - el.scrollTop - el.clientHeight < 50
  })
  await page.screenshot({ path: "runtime/validation-ui/new-comment.png" })
  return {
    passed: true,
    loaded: await page.locator('.comment').count(),
    preservesReadingPosition: true,
    preservesSelection: true,
    unreadJump: true,
  }
}
