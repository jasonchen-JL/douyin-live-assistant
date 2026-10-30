export async function api(path, method = "GET", body, signal) {
  const response = await fetch("/api" + path, {
    method,
    signal: signal || AbortSignal.timeout(15000),
    headers: { "Content-Type": "application/json", "X-Live-Assistant": "1" },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || value.detail || "request_failed");
  return value;
}
