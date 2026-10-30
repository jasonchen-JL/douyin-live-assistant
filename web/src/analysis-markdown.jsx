import React from "react";
import ReactMarkdown from "react-markdown";

export function AnalysisMarkdown({ text }) {
  return (
    <div className="analysis-markdown">
      <ReactMarkdown skipHtml unwrapDisallowed allowedElements={[
        "p", "strong", "em", "ul", "ol", "li", "blockquote", "br", "h1", "h2", "h3", "h4", "hr", "code",
      ]}>{text}</ReactMarkdown>
    </div>
  );
}

export function spokenLine(text) {
  return (text || "").split(/\n\s*\n/)[0].replace(/^\*\*主播直接说：\*\*\s*/, "").trim();
}
