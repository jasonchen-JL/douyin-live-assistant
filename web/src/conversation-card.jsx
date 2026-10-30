import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ArrowDown, ChevronDown, ChevronUp, LoaderCircle, MessageSquare } from "lucide-react";
import { api } from "./api";

export function mergeMessages(a, b) {
  return [...new Map([...a, ...b].map((m) => [m.event_id, m])).values()].sort(
    (x, y) => x.sent_at - y.sent_at || x.id - y.id,
  );
}

const date = (n) => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date(n * 1000));

function History({ uid, interval, time, explain }) {
  const [messages, setMessages] = useState([]), [before, setBefore] = useState(null);
  const [loaded, setLoaded] = useState(false), [error, setError] = useState("");
  const [olderLoading, setOlderLoading] = useState(false), [unread, setUnread] = useState(0);
  const scroller = useRef(), follow = useRef(true), restore = useRef(null), olderBusy = useRef(false), positioned = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    let disposed = false, timer, revision = 0, initialized = false;
    const controller = new AbortController();
    async function poll() {
      try {
        const p = new URLSearchParams({ scope: "all", limit: 50, ...(initialized ? { after: revision } : {}) });
        const data = await api(`/viewers/${encodeURIComponent(uid)}/comments?${p}`, "GET", undefined,
          AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]));
        if (disposed) return;
        revision = data.revision;
        if (!initialized) setBefore(data.before);
        if (initialized && data.items.length && !follow.current) setUnread((n) => n + data.items.length);
        setMessages((m) => initialized ? mergeMessages(m, data.items) : data.items);
        initialized = true;
        setLoaded(true);
        setError("");
        timer = setTimeout(poll, data.has_more && data.before === null ? 50 : interval);
      } catch (e) {
        if (!disposed) {
          setError(explain(e.message));
          timer = setTimeout(poll, interval);
        }
      }
    }
    poll();
    return () => { disposed = true; alive.current = false; clearTimeout(timer); controller.abort(); };
  }, [uid, interval]);
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (restore.current !== null) {
      el.scrollTop += el.scrollHeight - restore.current;
      restore.current = null;
    } else if (follow.current) el.scrollTop = el.scrollHeight;
    if (messages.length && !positioned.current) {
      positioned.current = true;
      el.closest('.conversation-card')?.scrollIntoView({ block: 'nearest' });
    }
  }, [messages]);

  async function loadOlder() {
    if (!before || olderBusy.current) return;
    olderBusy.current = true;
    setOlderLoading(true);
    try {
      const p = new URLSearchParams({ scope: "all", before: JSON.stringify(before), limit: 50 });
      const data = await api(`/viewers/${encodeURIComponent(uid)}/comments?${p}`);
      if (!alive.current) return;
      restore.current = scroller.current?.scrollHeight || 0;
      setMessages((m) => mergeMessages(data.items, m));
      setBefore(data.before);
      setError("");
    } catch (e) { if (alive.current) setError(explain(e.message)); }
    finally { olderBusy.current = false; if (alive.current) setOlderLoading(false); }
  }
  return <div className="conversation-history" aria-label="全部历史评论">
    <div className="history-scroll" ref={scroller} onScroll={() => {
      const el = scroller.current;
      follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 35;
      if (follow.current) setUnread(0);
    }}>
      {before && <button className="load-older" onClick={loadOlder} disabled={olderLoading}>
        {olderLoading ? <LoaderCircle size={14} className="spin" /> : <ChevronUp size={14} />}更早评论
      </button>}
      {!loaded && !error && <p className="history-loading">正在读取历史</p>}
      {messages.map((m, i) => <React.Fragment key={m.event_id}>
        {(i === 0 || date(messages[i - 1].sent_at) !== date(m.sent_at)) && <div className="history-date">{date(m.sent_at)}</div>}
        <div className="history-message" data-event-id={m.event_id} title={"平台消息 ID " + m.platform_message_id}>
          <time>{time(m.sent_at)}</time><p>{m.content}</p>
        </div>
      </React.Fragment>)}
      {error && <p className="field-error" role="alert">{error}</p>}
    </div>
    <div className="history-footer">
      <span>已加载 {messages.length} 条{loaded && !before ? " · 全部历史" : ""}</span>
      {unread > 0 ? <button onClick={() => {
        follow.current = true;
        scroller.current.scrollTop = scroller.current.scrollHeight;
        setUnread(0);
      }}><ArrowDown size={13} />{unread}条新评论</button> : <span>北京时间</span>}
    </div>
  </div>;
}

export function ConversationCard({ viewer: v, selected, expanded, onSelect, interval, time, explain, dateRange }) {
  const state = v.running_job ? "AI 分析中" : v.manual_pending ? "重新分析排队中" : v.error ? "分析失败" : v.awaiting_analysis ? "新评论待分析" : v.analysis ? "提词已更新" : "等待分析";
  return <article className={"conversation-card" + (selected ? " selected" : "")} data-viewer-id={v.uid}>
    <button className="conversation-toggle" onClick={onSelect} aria-expanded={expanded}
      aria-label={`${v.nickname}，${expanded ? "收起历史" : "展开历史"}`}>
      <span className="conversation-heading">
        <span className="avatar" aria-hidden="true">{[...v.nickname][0] || "?"}</span>
        <strong title={v.nickname}>{v.nickname}</strong>
        <time>{time(v.last_at)}</time>
        {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </span>
      {!expanded && <span className="conversation-preview">
        {(v.recent_comments || []).map((m) => <span className="preview-message" key={m.event_id}>
          <time title={date(m.sent_at)}>{time(m.sent_at)}</time><span>{m.content}</span>
        </span>)}
      </span>}
      <span className="conversation-meta">
        <span><MessageSquare size={12} />{v.comment_count} 条</span>
        <span className={v.error ? "failed-state" : ""}>{state}</span>
        <span className="viewer-id" title={"观众 ID " + v.uid}>ID {v.uid.slice(-6)}</span>
      </span>
    </button>
    {expanded && <History key={v.uid + dateRange} uid={v.uid} interval={interval} time={time} explain={explain} />}
  </article>;
}
