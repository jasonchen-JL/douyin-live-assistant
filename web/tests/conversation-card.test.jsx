import { expect, test } from "bun:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ConversationCard, mergeMessages } from "../src/conversation-card";

test("history merges by event ID, preserving distinct repeated text and late timestamps", () => {
  const a = { event_id: "a", id: 1, sent_at: 30, content: "same" };
  const b = { event_id: "b", id: 2, sent_at: 30, content: "same" };
  const late = { event_id: "late", id: 3, sent_at: 10, content: "earlier" };
  expect(mergeMessages([a, b], [b, late]).map(m => m.event_id)).toEqual(["late", "a", "b"]);
});

test("card escapes comment HTML, includes exact time, and keeps full string UID", () => {
  const viewer = { uid: "9007199254740993999", nickname: "<script>name</script>", last_at: 1,
    comment_count: 6, awaiting_analysis: true,
    recent_comments: Array.from({ length: 6 }, (_, i) => ({ event_id: String(i), content: "<img src=x onerror=alert(1)>", sent_at: 1 })) };
  const text = renderToStaticMarkup(<ConversationCard viewer={viewer} selected expanded={false}
    onSelect={() => {}} interval={1000} time={() => "10:21:03"} explain={String} dateRange="today" />);
  expect(text).toContain('data-viewer-id="9007199254740993999"');
  expect(text).toContain('aria-expanded="false"');
  expect(text).not.toContain("<script>");
  expect(text).not.toContain("<img ");
  expect(text.match(/class="preview-message"/g).length).toBe(6);
  expect(text.match(/10:21:03/g).length).toBe(7);
  expect(text).toContain("新评论待分析");
});
