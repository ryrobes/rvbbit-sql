const { test, expect } = require("@playwright/test");

const baseURL = process.env.WAREHOUSE_TEST_URL || "http://127.0.0.1:8766";
const password = process.env.WAREHOUSE_TEST_LOGIN_PASSWORD || "";
test.use({ launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || "/usr/bin/chromium" } });

test("Action Library directly applies, verifies, and remediates a Workflow", async ({ page }) => {
  test.skip(!password, "WAREHOUSE_TEST_LOGIN_PASSWORD is required for the local authenticated UI test");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseURL}/login?next=/calliope`);
  await page.locator('input[name="email"]').fill("pilot@example.com");
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(/\/calliope/),
    page.locator('button[type="submit"]').click(),
  ]);
  await expect(page.locator("#action-library-open")).toBeVisible();
  await expect(page.locator('[data-session-tab="chats"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator("#session-tab-panel [data-session-id].active")).toBeVisible();
  await expect(page.locator("#session-tab-panel [data-session-action-id]")).toHaveCount(0);

  await page.locator("#action-library-open").click();
  await expect(page.locator("#action-library-dialog")).toBeVisible();
  await expect(page.locator('[data-library-mode="inventory"]')).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#action-library-list .inventory-card").first()).toBeVisible();
  await page.locator("#action-library-list .inventory-card").first().click();
  await expect(page.locator("#inventory-selected")).toBeVisible();
  await expect(page.locator("#inventory-detail-health")).not.toBeEmpty();
  const discoverLoaded = page.waitForResponse((response) => response.url().includes("/api/calliope/actions?"));
  await page.locator('[data-library-mode="discover"]').click();
  await discoverLoaded;
  const firstSearch = page.waitForResponse((response) => response.url().includes("/api/calliope/actions?"));
  await page.locator("#action-library-search").fill("SQL-backed document source");
  await firstSearch;
  await page.locator('[data-action-id="admin.brain_query_source"]').click();
  await expect(page.locator("#action-detail-title")).toHaveText("Add a SQL-backed document source");

  await page.locator('[name="provider"]').fill("calliope-action-smoke");
  await page.locator('[name="label"]').fill("Calliope Action Library smoke test");
  await page.locator('[name="list_sql"]').fill(
    "SELECT 'calliope-action-smoke://welcome'::text AS uri, "
      + "'Action Library verified'::text AS title, "
      + "md5('Calliope Action Library verified') AS content_hash, "
      + "now() AS occurred_at, "
      + "'A source document created by the typed Action Library smoke test.'::text AS body",
  );
  const applied = page.waitForResponse((response) => response.url().endsWith("/admin.brain_query_source/execute") && response.status() === 200);
  await page.locator("#action-create-plan").click();
  await applied;
  await expect(page.locator("#action-plan")).toBeVisible();
  await expect(page.locator("#action-plan-status")).toHaveText("Verified", { timeout: 20_000 });
  await expect(page.locator("#action-plan-steps .action-plan-step.complete")).toHaveCount(5);
  await expect(page.locator("#action-detail-note")).toContainText("Applied and verified");
  await page.screenshot({ path: "/tmp/calliope-action-library-dark.png", fullPage: true });

  await page.locator("#action-library-close").click();
  const modeButton = page.locator(".warehouse-theme-mode-button");
  await expect(modeButton).toBeVisible();
  if (await page.locator("html").getAttribute("data-warehouse-color-mode") !== "light") {
    await modeButton.click();
  }
  await expect(page.locator("html")).toHaveAttribute("data-warehouse-color-mode", "light");
  await page.locator("#action-library-open").click();
  await expect(page.locator("#action-library-dialog")).toBeVisible();
  await expect(page.locator("#action-plan-status")).toHaveText("Verified");
  await expect(page.locator("#action-library-list .action-library-loading")).toHaveCount(0);
  await page.screenshot({ path: "/tmp/calliope-action-library-light.png", fullPage: true });

  await page.locator("#action-library-close").click();
  await page.locator("#workflow-library-open").click();
  await expect(page.locator("#workflow-library-dialog")).toBeVisible();
  const projectWorkflow = page.locator("[data-workflow-id]", { hasText: "Project and ticket pulse" });
  await expect(projectWorkflow).toBeVisible();
  await projectWorkflow.click();
  const resolve = page.locator('[data-resolve-action="mcp.connect:mcp~linear"]');
  await expect(resolve).toBeVisible({ timeout: 15_000 });
  await resolve.click();
  await expect(page.locator("#workflow-library-dialog")).not.toBeVisible();
  await expect(page.locator("#action-library-dialog")).toBeVisible();
  await expect(page.locator("#action-detail-title")).toHaveText("Connect Linear");
  await expect(page.locator('[name="secret:LINEAR_API_KEY"]')).toHaveAttribute("type", "password");
  await expect(page.locator("#action-library-summary")).toHaveText("2 possible outcomes");
  await expect(page.locator("#action-library-list [data-action-id]")).toHaveCount(2);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator("#action-library-close")).toBeVisible();
  const dialogBox = await page.locator("#action-library-dialog").boundingBox();
  expect(dialogBox.width).toBeLessThanOrEqual(390);
  expect(dialogBox.height).toBeLessThanOrEqual(844);
  await page.screenshot({ path: "/tmp/calliope-action-library-mobile.png", fullPage: true });

  await page.setViewportSize({ width: 1440, height: 1000 });
  const guidedSearch = page.waitForResponse((response) => response.url().includes("/api/calliope/actions?"));
  await page.locator("#action-library-search").fill("watch an important metric");
  await guidedSearch;
  await page.locator('[data-action-id="monitor.metric_watch"]').click();
  await expect(page.locator("#action-detail-title")).toHaveText("Watch an important metric");
  const handoff = page.waitForResponse((response) => response.url().endsWith("/monitor.metric_watch/handoff") && response.status() === 201);
  await page.locator("#action-open-with-calliope").click();
  await handoff;
  await page.waitForURL(/\/calliope\?session=/);
  const actionSessionId = new URL(page.url()).searchParams.get("session");
  await expect(page.locator("#message-input")).toHaveValue(/create a governed metric watch/i);
  await expect(page.locator("#stage .surface.kind-action")).toContainText("Watch an important metric");
  const actionTab = page.locator('[data-session-tab="actions"]');
  await expect(page.locator(".session-folder")).toHaveCount(0);
  await expect(actionTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(`#session-tab-panel [data-session-id="${actionSessionId}"]`)).toHaveClass(/active/);
  const savedActionSession = await page.evaluate(() => ({
    last: localStorage.getItem("rvbbit-calliope-last-session-v1"),
    tab: localStorage.getItem("rvbbit-calliope-session-tab-v1"),
    tabs: JSON.parse(localStorage.getItem("rvbbit-calliope-tab-sessions-v1") || "{}"),
  }));
  expect(savedActionSession.last).toBe(actionSessionId);
  expect(savedActionSession.tab).toBe("actions");
  expect(savedActionSession.tabs.actions).toBe(actionSessionId);

  const chatsTab = page.locator('[data-session-tab="chats"]');
  await chatsTab.click();
  await expect(chatsTab).toHaveAttribute("aria-selected", "true");
  const activeChat = page.locator("#session-tab-panel [data-session-id].active");
  await expect(activeChat).toBeVisible();
  expect(await activeChat.getAttribute("data-session-id")).not.toBe(actionSessionId);

  await actionTab.click();
  await expect(actionTab).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(`#session-tab-panel [data-session-id="${actionSessionId}"]`)).toHaveClass(/active/);
  await page.goto(`${baseURL}/calliope`);
  await expect(page.locator('[data-session-tab="actions"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(`#session-tab-panel [data-session-id="${actionSessionId}"]`)).toHaveClass(/active/);
  await page.screenshot({ path: "/tmp/calliope-session-tabs.png", fullPage: true });
});

test("Custom MCP connector reveals only the selected transport contract", async ({ page }) => {
  test.skip(!password, "WAREHOUSE_TEST_LOGIN_PASSWORD is required for the local authenticated UI test");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseURL}/login?next=/calliope`);
  await page.locator('input[name="email"]').fill("pilot@example.com");
  await page.locator('input[name="password"]').fill(password);
  await Promise.all([
    page.waitForURL(/\/calliope/),
    page.locator('button[type="submit"]').click(),
  ]);

  await page.locator("#action-library-open").click();
  const discoverLoaded = page.waitForResponse((response) => response.url().includes("/api/calliope/actions?"));
  await page.locator('[data-library-mode="discover"]').click();
  await discoverLoaded;
  const searched = page.waitForResponse((response) => response.url().includes("/api/calliope/actions?"));
  await page.locator("#action-library-search").fill("custom MCP server");
  await searched;
  await page.locator('[data-action-id="mcp.connect_custom"]').click();
  await expect(page.locator("#action-detail-title")).toHaveText("Connect a custom MCP server");
  await expect(page.locator("#action-library-empty")).toBeHidden();
  await expect(page.locator("#action-library-selected")).toBeVisible();
  await expect(page.locator('[name="transport"]')).toHaveValue("http");
  await expect(page.locator('[name="url"]')).toBeVisible();
  await expect(page.locator('[name="auth_token_name"]')).toBeVisible();
  await expect(page.locator('[name="command"]')).toBeHidden();
  await expect(page.locator('[name="args"]')).toBeHidden();

  await page.locator('[name="transport"]').selectOption("stdio");
  await expect(page.locator('[name="url"]')).toBeHidden();
  await expect(page.locator('[name="auth_token_name"]')).toBeHidden();
  await expect(page.locator('[name="command"]')).toBeVisible();
  await expect(page.locator('[name="args"]')).toBeVisible();
  await expect(page.locator('[name="environment"]')).toBeVisible();
  await expect(page.locator('[name="secret_names"]')).toBeVisible();
  await expect(page.locator('[name="secret:MCP_SECRET_VALUES"]')).toHaveAttribute("type", "password");
  await expect(page.locator('[name="create_sql_functions"]')).toBeChecked();
  await expect(page.locator('[name="create_operators"]')).toBeChecked();
  await page.screenshot({ path: "/tmp/calliope-custom-mcp-action.png", fullPage: true });
});
