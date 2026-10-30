import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  Check,
  Copy,
  Database,
  LoaderCircle,
  MessageSquare,
  Pause,
  Pencil,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Star,
  Users,
  X,
} from "lucide-react";
import "./style.css";
import { collectorPresentation } from "./collector-status";
import { AnalysisMarkdown, spokenLine } from "./analysis-markdown";
import { api } from "./api";
import { ConversationCard } from "./conversation-card";

const ERROR = {
  invalid_parameters: "设置或查询参数不正确",
  model_timeout: "模型请求超时",
  model_connection_failed: "模型连接失败",
  model_invalid_json: "模型返回格式不正确",
  model_empty_content: "模型没有返回正文",
  model_missing_opening: "模型未按要求先给出主播直接说",
  model_missing_or_extra_viewer: "模型返回的观众不完整",
  model_target_mismatch: "模型返回的消息范围不正确",
  model_invalid_evidence: "模型引用了无效评论",
  input_budget_exceeded: "输入超过预算，请调整模型设置",
  summary_budget_exceeded: "历史摘要超过预算",
  model_response_incomplete: "模型输出未完整结束，请查看请求日志",
  model_stream_error: "模型流式响应异常，请查看请求日志",
  model_response_too_large: "模型响应超过大小上限",
  source_read_failed: "采集数据库读取失败",
  collector_command_failed: "采集控制失败",
  collector_stop_before_room_change: "请先停止采集，再更换直播间或时长",
  pool_entry_not_found: "所选账号不存在，请重新选择",
  pool_key_required: "新增账号需要填写 Key",
  pool_account_blocked: "账号认证异常，请检查 Key",
  credential_save_failed: "系统凭据库保存失败",
  credential_not_found: "系统凭据库中找不到 Key",
  analysis_request_conflict: "重新分析命令冲突，请刷新页面",
  analysis_room_changed: "直播间已更换，请刷新页面后再分析",
  manual_analysis_requires_markdown: "当前链路不支持手动重新分析",
  analysis_paused: "分析已暂停，请在设置中启用分析",
  analysis_model_not_configured: "请先配置提词模型",
  viewer_not_found: "该观众暂无已采评论",
  analysis_worker_not_running: "此服务仅预览已保存结果，未运行分析任务",
};
const stateNames = {
  screening: "新评论待分析",
  queued: "重新分析排队中",
  updating: "分析更新中",
  failed: "分析失败",
  ready: "分析已就绪",
  no_analysis_needed: "暂无提词需求",
  no_data: "暂无评论",
};
const time = (n) =>
  n
    ? new Date(n * 1000).toLocaleTimeString("zh-CN", {
        hour12: false,
        timeZone: "Asia/Shanghai",
      })
    : "--:--";
const dateNow = () =>
  new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(
    new Date(),
  );
const resultTime = (n) => new Date(n * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
const explain = (error) =>
  ERROR[error] ||
  (String(error).startsWith("model_http_")
    ? `模型服务返回 HTTP ${String(error).slice(11)}`
    : "操作未完成，请重试");

function IconButton({ label, children, ...props }) {
  return (
    <button className="icon-button" title={label} aria-label={label} {...props}>
      {children}
    </button>
  );
}
function Stars({ score }) {
  if (score == null) return <span className="unrated">暂无星级评估</span>;
  return (
    <span className="stars" aria-label={score ? `${score}星` : "未评分"}>
      {[1, 2, 3, 4, 5].map((n) => (
        <Star
          key={n}
          size={12}
          fill={score >= n ? "currentColor" : "none"}
          className={score >= n ? "filled" : ""}
        />
      ))}
    </span>
  );
}
function Empty({ icon: Icon = MessageSquare, title }) {
  return (
    <div className="empty">
      <Icon size={32} strokeWidth={1.3} />
      <span>{title}</span>
    </div>
  );
}
function App() {
  const [settings, setSettings] = useState(null),
    [status, setStatus] = useState(null);
  const [view, setView] = useState("all"),
    [query, setQuery] = useState(""),
    [search, setSearch] = useState("");
  const [start, setStart] = useState(dateNow),
    [end, setEnd] = useState(dateNow);
  const [active, setActive] = useState(0),
    [minStars, setMinStars] = useState(3),
    [sort, setSort] = useState("time_desc");
  const [viewers, setViewers] = useState({ items: [], total: 0 }),
    [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null),
    [expandedUid, setExpandedUid] = useState(null),
    [analysisPacket, setAnalysis] = useState(null);
  const [manualBusy, setManualBusy] = useState(false), [manualPending, setManualPending] = useState(false);
  const [analysisRefresh, setAnalysisRefresh] = useState(0);
  const manualRequests = useRef(new Map()), sending = useRef(false);
  const [modal, setModal] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [mobilePane, setMobilePane] = useState("list"),
    [copied, setCopied] = useState(false);
  const room = settings?.general.room_url || "";
  const selectionKey = `${room}|${selected?.uid || ""}`,
    currentSelection = useRef(selectionKey);
  currentSelection.current = selectionKey;
  const analysis = analysisPacket?.viewerKey === selectionKey ? analysisPacket : null;
  const selectedViewer = viewers.items.find((v) => v.uid === selected?.uid) || selected;
  const interval = (settings?.general.ui_interval_seconds || 1) * 1000;

  useEffect(() => {
    api("/settings")
      .then((s) => {
        setSettings(s);
        setMinStars(s.general.display_min_stars);
      })
      .catch((e) => setError(explain(e.message)));
  }, []);
  useEffect(() => {
    const t = setTimeout(() => setSearch(query.trim()), 220);
    return () => clearTimeout(t);
  }, [query]);
  useEffect(() => {
    setOffset(0);
  }, [search, view, start, end, active, sort, minStars]);
  useEffect(() => {
    setSelected(null);
    setExpandedUid(null);
    setAnalysis(null);
  }, [room, start, end]);
  useEffect(() => {
    if (settings?.general.analysis_mode === "markdown_direct") {
      setView("all");
      setSort("time_desc");
    }
  }, [settings?.general.analysis_mode]);
  useEffect(() => {
    let disposed = false,
      timer;
    async function poll() {
      try {
        const s = await api("/status");
        if (!disposed) setStatus(s);
      } catch (e) {
        if (!disposed) setError(explain(e.message));
      }
      if (!disposed) timer = setTimeout(poll, interval);
    }
    poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [interval]);
  useEffect(() => {
    let disposed = false,
      timer;
    async function poll() {
      try {
        const p = new URLSearchParams({
          start,
          end,
          query: search,
          view,
          active: search ? 0 : active,
          stars: minStars,
          sort,
          offset,
          limit: 50,
        });
        const data = await api("/viewers?" + p);
        if (!disposed) setViewers(data);
      } catch (e) {
        if (!disposed) setError(explain(e.message));
      }
      if (!disposed) timer = setTimeout(poll, interval);
    }
    poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [room, start, end, search, view, active, minStars, sort, offset, interval]);
  useEffect(() => {
    setManualPending(false);
    setAnalysis(null);
    setCopied(false);
  }, [selectionKey]);
  useEffect(() => {
    let disposed = false, timer;
    const controller = new AbortController();
    if (!selected) return;
    const uid = encodeURIComponent(selected.uid);
    async function poll() {
      try {
        const result = await api(`/viewers/${uid}/analysis?scope=all`, "GET", undefined,
          AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]));
        if (!disposed) { setAnalysis({ ...result, viewerKey: selectionKey }); setManualPending(result.manual_pending); }
      } catch (e) {
        if (!disposed) setError(explain(e.message));
      }
      if (!disposed) timer = setTimeout(poll, interval);
    }
    poll();
    return () => {
      disposed = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [selectionKey, interval, analysisRefresh]);
  function selectViewer(viewer) {
    setSelected(viewer);
    setExpandedUid((uid) => uid === viewer.uid ? null : viewer.uid);
  }
  async function reanalyze() {
    if (!selected || sending.current || manualPending) return;
    sending.current = true;
    setManualBusy(true);
    const key = selectionKey;
    const requestId = manualRequests.current.get(key) || crypto.randomUUID();
    manualRequests.current.set(key, requestId);
    try {
      const result = await api(`/viewers/${encodeURIComponent(selected.uid)}/analysis`, "POST", {
        room_url: room, request_id: requestId,
      });
      manualRequests.current.delete(key);
      if (currentSelection.current === key) {
        setManualPending(!["done", "failed", "superseded"].includes(result.state));
        setAnalysisRefresh((n) => n + 1);
      }
    } catch (e) {
      if (currentSelection.current === key) setError(explain(e.message));
    } finally { sending.current = false; setManualBusy(false); }
  }
  function setViewMode(next) {
    setView(next);
    setActive(
      next === "leads" && start === dateNow()
        ? settings?.general.display_active_minutes || 0
        : 0,
    );
    setSort(next === "leads" ? "stars_desc" : "time_desc");
  }
  async function control() {
    setBusy(true);
    try {
      await api(
        "/collector/" + (status?.collector.process_running ? "stop" : "start"),
        "POST",
        {},
      );
      setStatus(await api("/status"));
    } catch (e) {
      setError(explain(e.message));
    } finally {
      setBusy(false);
    }
  }
  const body = analysis?.result?.body;
  const markdownMode = settings?.general.analysis_mode === "markdown_direct";
  const draft = analysis?.preview;
  const draftText = draft?.analysis_markdown?.startsWith("**主播直接说：**") ? draft.analysis_markdown : null;
  const pending =
    (status?.screening.pending || 0) + (status?.screening.processing || 0);
  const running = status?.collector.process_running;
  const collection = collectorPresentation(status?.collector);
  const isolated = status?.collector.status === "isolated_test";

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark">
            <Radio size={22} />
          </span>
          <h1>直播销售助手</h1>
          <span className="local-label">
            {isolated ? "模拟测试库" : "本地工作台"}
          </span>
        </div>
        <div className="header-actions">
          <span className={"connection " + (collection.ready ? "on" : "")} role="status">
            <i />
            {collection.label}
          </span>
          <button
            className={running ? "button" : "button primary"}
            disabled={busy || isolated || collection.stopping}
            onClick={control}
          >
            {busy ? (
              <LoaderCircle size={15} className="spin" />
            ) : running ? (
              <Pause size={15} />
            ) : (
              <Play size={15} />
            )}
            <span>
              {isolated ? "模拟测试" : collection.stopping ? "正在停止" : running ? "停止监听" : "开始监听"}
            </span>
          </button>
          <IconButton label="设置" onClick={() => setModal(true)}>
            <Settings size={19} />
          </IconButton>
        </div>
      </header>
      <div className="status-strip">
        <span className={"collector-mobile-state " + (collection.ready ? "healthy" : "")} role="status">
          <i className="status-dot" />{collection.label}
        </span>
        <span>
          <Database size={14} />
          已入库 <b>{status?.comments ?? 0}</b>
        </span>
        <span>
          <Activity size={14} />
          {markdownMode ? "待处理评论" : "待粗筛"} <b>{pending}</b>
        </span>
        <span className={status?.models.screening ? "healthy" : ""}>
          <i className="status-dot" />
          粗筛 {markdownMode ? "已停用" : status?.models.screening ? "已配置" : "未配置"}
        </span>
        <span className={status?.models.analysis ? "healthy" : ""}>
          <i className="status-dot" />
          {markdownMode ? "单观众提词" : "精准分析"} {status?.models.analysis ? "已配置" : "未配置"}
        </span>
        <span className="room-label">
          直播间 {settings?.general.room_url.split("/").pop() || "--"}
        </span>
      </div>
      {collection.detail && (
        <div className="collector-notice" role="status">{collection.detail}</div>
      )}
      {error && (
        <div className="error-bar" role="alert">
          {error}
          <IconButton label="关闭提示" onClick={() => setError("")}>
            <X size={15} />
          </IconButton>
        </div>
      )}
      <div className="toolbar">
        <div className="search-box">
          <Search size={17} />
          <input
            aria-label="检索观众昵称"
            placeholder="检索观众昵称"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <IconButton label="清除搜索" onClick={() => setQuery("")}>
              <X size={14} />
            </IconButton>
          )}
        </div>
        <div className="date-controls">
          <input
            type="date"
            aria-label="开始日期"
            value={start}
            onChange={(e) => {
              setStart(e.target.value);
              if (end < e.target.value) setEnd(e.target.value);
              setActive(0);
            }}
          />
          <span>至</span>
          <input
            type="date"
            aria-label="结束日期"
            value={end}
            min={start}
            onChange={(e) => {
              setEnd(e.target.value);
              setActive(0);
            }}
          />
        </div>
        <span className="toolbar-topic" title={settings?.general.topic}>
          {settings?.general.topic || "直播主题未设置"}
        </span>
      </div>
      <div className="mobile-tabs">
        {[
          ["list", "观众会话"],
          ["analysis", "提词"],
        ].map(([key, label]) => (
          <button
            key={key}
            className={mobilePane === key ? "active" : ""}
            onClick={() => setMobilePane(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <main className="workspace">
        <section
          className={
            "viewer-pane " + (mobilePane === "list" ? "mobile-active" : "")
          }
          aria-label="观众列表"
        >
          <div className="panel-heading">
            <div>
              <Users size={17} />
              <h2>{search ? "检索结果" : "观众会话"}</h2>
            </div>
            <span className="count">{viewers.total}</span>
          </div>
          {!markdownMode && <div className="view-tabs">
            {[
              ["all", "全部观众"],
              ["leads", "有效客户"],
              ["reply", "待回复"],
            ].map(([key, label]) => (
              <button
                disabled={!!search || (markdownMode && key !== "all")}
                title={markdownMode && key !== "all" ? "星级与待回复评估暂未启用，历史逻辑保留" : undefined}
                className={view === key && !search ? "active" : ""}
                key={key}
                onClick={() => setViewMode(key)}
              >
                {label}
              </button>
            ))}
          </div>}
          <div className="list-filters">
            <select
              aria-label="观众排序"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              <option value="time_desc">时间从新到旧</option>
              <option value="time_asc">时间从旧到新</option>
              <option value="stars_desc" disabled={markdownMode}>星级从高到低（历史）</option>
              <option value="stars_asc" disabled={markdownMode}>星级从低到高（历史）</option>
            </select>
            <select
              aria-label="最近活动范围"
              value={search ? 0 : active}
              disabled={!!search}
              onChange={(e) => setActive(Number(e.target.value))}
            >
              <option value="0">全部活动</option>
              <option value="20">最近20分钟</option>
              <option value="60">最近1小时</option>
              <option value="120">最近2小时</option>
              {![0, 20, 60, 120].includes(active) && (
                <option value={active}>最近{active}分钟</option>
              )}
            </select>
            {view === "leads" && !search && (
              <select
                aria-label="最低星级"
                value={minStars}
                onChange={(e) => setMinStars(Number(e.target.value))}
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n}>
                    {n}星以上
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="viewer-list">
            {!viewers.items.length && (
              <Empty
                icon={Users}
                title={search ? "没有匹配的已采观众" : "暂无符合条件的观众"}
              />
            )}
            {viewers.items.map((v) => <ConversationCard key={room + v.uid} viewer={v}
              selected={selected?.uid === v.uid} expanded={expandedUid === v.uid}
              onSelect={() => selectViewer(v)} interval={interval} time={time} explain={explain} dateRange={start + end} />)}
          </div>
          <div className="list-footer">
            <span>
              {viewers.total
                ? `${offset + 1}–${Math.min(offset + 50, viewers.total)}`
                : "0"}{" "}
              / {viewers.total}
            </span>
            <div>
              <IconButton
                label="上一页观众"
                disabled={!offset}
                onClick={() => setOffset((n) => Math.max(0, n - 50))}
              >
                <ArrowLeft size={15} />
              </IconButton>
              <IconButton
                label="下一页观众"
                disabled={offset + 50 >= viewers.total}
                onClick={() => setOffset((n) => n + 50)}
              >
                <ArrowRight size={15} />
              </IconButton>
            </div>
          </div>
        </section>
        <section
          className={
            "analysis-pane " +
            (mobilePane === "analysis" ? "mobile-active" : "")
          }
          aria-label="需求分析与提词"
        >
          <div className="panel-heading">
            <div>
              <Sparkles size={17} />
              <h2>需求分析与提词</h2>
            </div>
            <span role="status"
              className={
                "analysis-state " +
                (analysis?.state === "ready" ? "healthy" : "")
              }
            >
              {selected ? stateNames[analysis?.state] || "读取中" : "待选择"}
            </span>
          </div>
          {selected && <div className="analysis-actions">
            <div className="analysis-viewer">
              <strong>{selectedViewer.nickname}</strong>
              <span title={"观众 ID " + selected.uid}>ID {selected.uid}</span>
            </div>
            <button className="button" onClick={reanalyze}
              disabled={!analysis || manualBusy || manualPending || !markdownMode || status?.analysis_commands_enabled === false || !settings?.general.analysis_enabled || !status?.models.analysis}
              title={status?.analysis_commands_enabled === false ? "此服务仅预览已保存结果" : !settings?.general.analysis_enabled ? "请在设置中启用分析" : !status?.models.analysis ? "请先配置提词模型" : undefined}>
              {manualBusy || manualPending ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
              {manualBusy ? "提交中" : manualPending ? (analysis?.manual_state === "running" ? "重新分析中" : "重新分析已排队") : "重新分析"}
            </button>
          </div>}
          {selected && analysis?.error && <div className="analysis-error" role="alert">
            {explain(analysis.error)}{analysis.result ? "，保留上次提词" : ""}
          </div>}
          <div className="analysis-content">
            {selected && draftText && (
              <div className="markdown-preview" aria-label="生成中的提词">
                <span className="preview-label"><LoaderCircle size={14} className="spin" />生成中 · 尚未保存</span>
                <AnalysisMarkdown text={draftText} />
                {body && <p className="analysis-footnote">上次成功结果</p>}
              </div>
            )}
            {!selected ? (
              <Empty icon={Sparkles} title="未选择观众" />
            ) : !body ? (draftText ? null : (
              <Empty
                icon={Sparkles}
                title={
                  !status?.models.analysis
                    ? "精准模型未配置"
                    : stateNames[analysis?.state] || "读取中"
                }
              />
            )) : body.analysis_markdown ? (
              <>
                <AnalysisMarkdown text={body.analysis_markdown} />
                <div className="markdown-actions">
                  <Stars score={body.intent_score} />
                  <IconButton label="复制提词" onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(spokenLine(body.analysis_markdown));
                      setCopied(true);
                      setTimeout(() => setCopied(false), 1500);
                    } catch { setError("复制失败"); }
                  }}>{copied ? <Check size={15} /> : <Copy size={15} />}</IconButton>
                </div>
                <div className="analysis-footnote">
                  {resultTime(analysis.result.created_at)}生成 · 已覆盖{analysis.result.coverage.length}条评论
                </div>
              </>
            ) : (
              <>
                <div className="intent-heading">
                  <Stars score={body.intent_score} />
                  <strong>
                    {body.priority_reply
                      ? "建议优先回答"
                      : body.needs_reply
                        ? "需要回复"
                        : "持续观察"}
                  </strong>
                </div>
                <p className="intent-reason">{body.intent_reason}</p>
                <div className="profile-grid">
                  {[
                    ["行业", body.industry],
                    ["AI水平", body.ai_level],
                    ["团队", body.team],
                    ["现有系统", body.current_system],
                  ].map(([label, value]) => (
                    <div key={label}>
                      <span>{label}</span>
                      <strong>{value}</strong>
                    </div>
                  ))}
                </div>
                <div className="analysis-section">
                  <h3>当前需求</h3>
                  <p>{body.current_need}</p>
                </div>
                <div className="script-section">
                  <div className="section-heading">
                    <h3>主播直接说</h3>
                    <IconButton
                      label="复制提词"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(
                            body.suggested_reply,
                          );
                          setCopied(true);
                          setTimeout(() => setCopied(false), 1500);
                        } catch {
                          setError("复制失败");
                        }
                      }}
                    >
                      {copied ? <Check size={15} /> : <Copy size={15} />}
                    </IconButton>
                  </div>
                  <p>{body.suggested_reply}</p>
                </div>
                <div className="analysis-section">
                  <h3>下一句追问</h3>
                  <p>{body.followup_question}</p>
                </div>
                <div className="analysis-section lead-section">
                  <h3>
                    留资建议 <span>{body.lead_value}</span>
                  </h3>
                  <p>{body.lead_approach}</p>
                </div>
                <div className="analysis-section risk-section">
                  <h3>待确认 / 技术风险</h3>
                  <p>{body.risks}</p>
                </div>
                <details className="evidence">
                  <summary>画像摘要与依据</summary>
                  <p>{body.summary}</p>
                  <div>
                    {body.evidence_event_ids.map((id) => (
                      <span key={id}>
                        {selectedViewer?.recent_comments?.find((m) => m.event_id === id)?.content ||
                          `消息 ${id}`}
                      </span>
                    ))}
                  </div>
                </details>
                <div className="analysis-footnote">
                  {analysis.result.day} · {time(analysis.result.created_at)}生成
                  · 已覆盖{analysis.result.coverage.length}条 · 配置版本
                  {analysis.result.config_id}
                </div>
              </>
            )}
          </div>
        </section>
      </main>
      <footer className="app-footer">
        <span>
          <i className="status-dot" />
          {settings?.general.analysis_enabled
            ? "自动分析已启用"
            : "自动分析已暂停"}
        </span>
        <span>
          {status?.workers.import_error
            ? explain(status.workers.import_error)
            : status?.source_exists
              ? "采集库已连接"
              : "等待采集数据库"}
        </span>
        {status?.errors?.length > 0 && (
          <button onClick={() => setModal(true)}>请求异常记录</button>
        )}
        {status?.rejected > 0 && (
          <span className="field-error">异常源评论 {status.rejected} 条</span>
        )}
        <span className="footer-right">SQLite · LOCAL</span>
      </footer>
      {modal && settings && (
        <SettingsDialog
          initial={settings}
          status={status}
          onClose={() => setModal(false)}
          onSaved={setSettings}
        />
      )}
    </div>
  );
}

function SettingsDialog({ initial, status, onClose, onSaved }) {
  const [tab, setTab] = useState("general"),
    [values, setValues] = useState(structuredClone(initial));
  const [error, setError] = useState(""),
    [saved, setSaved] = useState(false),
    [busy, setBusy] = useState(false);
  const emptyPool = () => ({ label: "", base_url: "https://api.example.com/v1", api_key: "", enabled: true, max_concurrency: 1, min_interval_seconds: 2 });
  const [poolDraft, setPoolDraft] = useState(emptyPool);
  const [poolId, setPoolId] = useState(null);
  const dialog = useRef();
  useEffect(() => {
    dialog.current.showModal();
    return () => dialog.current?.close();
  }, []);
  function update(section, key, value) {
    setSaved(false);
    setValues((v) => ({ ...v, [section]: { ...v[section], [key]: value } }));
  }
  function updateModel(role, key, value) {
    setSaved(false);
    setValues((v) => ({
      ...v,
      models: { ...v.models, [role]: { ...v.models[role], [key]: value } },
    }));
  }
  async function save(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (tab === "general")
        await api("/settings/general", "PUT", values.general);
      else if (tab === "collector") {
        const { room_url, ...body } = values.collector;
        await api("/settings/collector", "PUT", body);
      } else if (tab === "key_pool") {
        const entry = await api("/settings/key-pool" + (poolId ? "/" + poolId : ""), poolId ? "PUT" : "POST", poolDraft);
        setPoolId(entry.id);
        setPoolDraft(Object.fromEntries(Object.keys(emptyPool()).map((key) => [key, key === "api_key" ? "" : entry[key]])));
      } else {
        const { configured, version, key_configured, ...body } =
          values.models[tab];
        await api("/settings/models/" + tab, "PUT", {
          ...body,
          api_key: body.api_key || "",
        });
      }
      const fresh = await api("/settings");
      setValues(fresh);
      onSaved(fresh);
      setSaved(true);
    } catch (e) {
      setError(explain(e.message));
    } finally {
      setBusy(false);
    }
  }
  const field = (section, key, label, type = "text", extra = {}) => (
    <label className="field" key={key}>
      <span>{label}</span>
      <input
        type={type}
        value={values[section][key]}
        onChange={(e) =>
          update(
            section,
            key,
            type === "number" ? Number(e.target.value) : e.target.value,
          )
        }
        {...extra}
      />
    </label>
  );
  return (
    <dialog
      className="settings-dialog"
      ref={dialog}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <form onSubmit={save}>
        <header>
          <div>
            <SlidersHorizontal size={19} />
            <h2>工作台设置</h2>
          </div>
          <IconButton label="关闭设置" type="button" onClick={onClose}>
            <X size={20} />
          </IconButton>
        </header>
        <nav className="settings-tabs">
          {[
            ["general", "业务与背景"],
            ["screening", "粗筛模型（保留）"],
            ["analysis", "提词模型"],
            ["key_pool", "账号池"],
            ["collector", "采集设置"],
          ].map(([key, label]) => (
            <button
              type="button"
              key={key}
              className={tab === key ? "active" : ""}
              onClick={() => {
                setTab(key);
                setSaved(false);
                setError("");
              }}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="settings-body">
          {tab === "general" && (
            <>
              {field("general", "room_url", "直播间链接", "url", {
                required: true,
              })}
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={values.general.analysis_enabled}
                  onChange={(e) =>
                    update("general", "analysis_enabled", e.target.checked)
                  }
                />
                自动分析
              </label>
              <div className="field">
                <span>分析链路</span>
                <div className="pipeline-modes" role="group" aria-label="分析链路">
                  {[["markdown_direct", "单观众 Markdown 提词"], ["direct", "旧结构化直达（保留）"], ["two_stage", "粗筛后精准分析（保留）"]].map(([mode, label]) => (
                    <button type="button" key={mode} aria-pressed={values.general.analysis_mode === mode}
                      disabled={mode !== "markdown_direct"}
                      onClick={() => update("general", "analysis_mode", mode)}>{label}</button>
                  ))}
                </div>
              </div>
              <p className="pool-muted">当前按未处理评论自动触发，使用提词模型与账号池，一次分析一位观众的全部已采历史；粗筛、星级和待回复评估暂不启用。</p>
              {["topic", "framework", "company_capabilities"].map((key, i) => (
                <label className="field" key={key}>
                  <span>{["直播主题", "直播框架", "公司能力"][i]}</span>
                  <textarea
                    rows={i ? 3 : 2}
                    value={values.general[key]}
                    onChange={(e) => update("general", key, e.target.value)}
                  />
                </label>
              ))}
              <div className="form-grid">
                {field(
                  "general",
                  "screening_history_comment_limit",
                  "粗筛历史条数（当前不生效）",
                  "number",
                  { min: 0, max: 200, disabled: values.general.analysis_mode === "markdown_direct" },
                )}
                {field(
                  "general",
                  "screening_batch_limit",
                  "每批新评论上限",
                  "number",
                  { min: 1, max: 100, disabled: values.general.analysis_mode === "markdown_direct" },
                )}
                {field(
                  "general",
                  "max_concurrent_requests",
                  "全局并发上限",
                  "number",
                  { min: 1, max: 20 },
                )}
                {field(
                  "general",
                  "screening_interval_seconds",
                  "粗筛检查间隔（秒）",
                  "number",
                  { min: 0.5, max: 30, step: 0.5, disabled: values.general.analysis_mode === "markdown_direct" },
                )}
                {field(
                  "general",
                  "ui_interval_seconds",
                  "页面更新间隔（秒）",
                  "number",
                  { min: 0.5, max: 10, step: 0.5 },
                )}
                {field(
                  "general",
                  "display_min_stars",
                  "有效客户最低星级",
                  "number",
                  { min: 1, max: 5, disabled: values.general.analysis_mode === "markdown_direct" },
                )}
                {field(
                  "general",
                  "display_active_minutes",
                  "有效客户活动范围（分钟，0不限）",
                  "number",
                  { min: 0, max: 1440, disabled: values.general.analysis_mode === "markdown_direct" },
                )}
              </div>
            </>
          )}
          {(tab === "screening" || tab === "analysis") && (
            <>
              {values.general.analysis_mode === "markdown_direct" && <p className="pool-muted">{tab === "screening" ? "粗筛暂不调用，原配置保留。" : "当前返回 Markdown 正文，不发送 JSON Schema，结构化输出设置暂不生效。"}</p>}
              <div className="model-status">
                <span className="status-dot" />
                {values.models[tab].configured ? "已配置" : "未配置"}
                <span>配置版本 {values.models[tab].version}</span>
              </div>
              <fieldset className="pool-selection">
                <legend>可用账号</legend>
                {(values.key_pool || []).map((entry) => (
                  <label className="check-field" key={entry.id}>
                    <input type="checkbox" checked={(values.models[tab].pool_ids || []).includes(entry.id)}
                      onChange={(e) => updateModel(tab, "pool_ids", e.target.checked
                        ? [...(values.models[tab].pool_ids || []), entry.id]
                        : values.models[tab].pool_ids.filter((id) => id !== entry.id))} />
                    <span>{entry.label}{!entry.enabled ? "（已停用）" : entry.blocked_reason ? "（认证异常）" : ""}</span>
                  </label>
                ))}
                {!values.key_pool?.length && <span className="pool-muted">暂无账号</span>}
              </fieldset>
              {["base_url", "model", "api_key"].filter((key) => key === "model" || !values.models[tab].pool_ids?.length).map((key) => (
                <label className="field" key={key}>
                  <span>{{base_url: "单账号 API Base URL", model: "模型名称", api_key: "单账号 API Key"}[key]}</span>
                  <input
                    autoComplete="off"
                    type={key === "api_key" ? "password" : "text"}
                    value={values.models[tab][key] || ""}
                    placeholder={
                      key === "api_key" && values.models[tab].key_configured
                        ? "已保存"
                        : ""
                    }
                    onChange={(e) => updateModel(tab, key, e.target.value)}
                  />
                </label>
              ))}
              {!values.models[tab].pool_ids?.length && <label className="check-field">
                <input
                  type="checkbox"
                  checked={values.models[tab].clear_key || false}
                  onChange={(e) =>
                    updateModel(tab, "clear_key", e.target.checked)
                  }
                />
                清除已保存的 Key
              </label>}
              <div className="form-grid">
                {[
                  ["timeout_seconds", "单次请求总时限（秒）", 5, 300],
                  ["input_budget", "输入预算（UTF-8 字节）", 4000, 200000],
                  ["output_limit", "输出上限（tokens）", 500, 16000],
                ].map(([key, label, min, max]) => (
                  <label className="field" key={key}>
                    <span>{label}</span>
                    <input
                      type="number"
                      min={min}
                      max={max}
                      value={values.models[tab][key]}
                      onChange={(e) =>
                        updateModel(tab, key, Number(e.target.value))
                      }
                    />
                  </label>
                ))}
                <label className="field">
                  <span>结构化输出</span>
                  <select
                    value={values.models[tab].json_mode}
                    disabled={values.general.analysis_mode === "markdown_direct" && tab === "analysis"}
                    onChange={(e) =>
                      updateModel(tab, "json_mode", e.target.value)
                    }
                  >
                    <option value="json_object">JSON mode</option>
                    <option value="json_schema">JSON Schema</option>
                    <option value="prompt">仅 JSON 提示</option>
                  </select>
                </label>
                <label className="field">
                  <span>输出长度参数</span>
                  <select
                    value={values.models[tab].token_parameter}
                    onChange={(e) =>
                      updateModel(tab, "token_parameter", e.target.value)
                    }
                  >
                    <option value="max_tokens">max_tokens</option>
                    <option value="max_completion_tokens">
                      max_completion_tokens
                    </option>
                  </select>
                </label>
              </div>
              {status?.errors
                ?.filter((e) => e.role === tab)
                .map((e, i) => (
                  <p className="field-error" key={i}>
                    {explain(e.error)}
                  </p>
                ))}
              <button
                type="button"
                className="button"
                onClick={async () => {
                  if (
                    !window.confirm(
                      "使用原任务配置重试失败任务，可能产生模型费用。继续？",
                    )
                  )
                    return;
                  try {
                    const r = await api("/jobs/" + tab + "/retry", "POST", {});
                    setError(
                      r.queued ? `已重新排队 ${r.queued} 批` : "没有可重试任务",
                    );
                  } catch (e) {
                    setError(explain(e.message));
                  }
                }}
              >
                <RefreshCw size={14} />
                重试失败任务
              </button>
            </>
          )}
          {tab === "key_pool" && (
            <>
              <div className="pool-heading">
                <h3>账号池</h3>
                <div>
                  <IconButton label="刷新账号状态" type="button" onClick={async () => {
                    try { const fresh = await api("/settings"); setValues((v) => ({ ...v, key_pool: fresh.key_pool })); onSaved(fresh); }
                    catch (e) { setError(explain(e.message)); }
                  }}><RefreshCw size={16} /></IconButton>
                  <IconButton label="新增账号" type="button" onClick={() => { setPoolId(null); setPoolDraft(emptyPool()); setSaved(false); }}>
                    <Plus size={17} />
                  </IconButton>
                </div>
              </div>
              <div className="pool-list">
                {(values.key_pool || []).map((entry) => (
                  <div className="pool-row" key={entry.id} data-selected={poolId === entry.id}>
                    <div><strong>{entry.label}</strong><span>{entry.base_url}</span></div>
                    <span className="pool-state">{!entry.enabled ? "已停用" : entry.blocked_reason ? "认证异常" : entry.cooldown_until > Date.now() / 1000 ? "冷却中" : "已启用"}</span>
                    <IconButton label={`编辑 ${entry.label}`} type="button" onClick={() => {
                      setPoolId(entry.id);
                      setPoolDraft(Object.fromEntries(Object.keys(emptyPool()).map((key) => [key, key === "api_key" ? "" : entry[key]])));
                      setSaved(false);
                    }}><Pencil size={15} /></IconButton>
                  </div>
                ))}
              </div>
              <h3>{poolId ? "编辑账号" : "新增账号"}</h3>
              <div className="form-grid">
                {[["label", "账号名称", "text"], ["base_url", "API Base URL", "url"], ["api_key", "API Key", "password"],
                  ["max_concurrency", "账号并发上限", "number"], ["min_interval_seconds", "请求最短间隔（秒）", "number"]].map(([key, label, type]) => (
                  <label className="field" key={key}><span>{label}</span><input autoComplete="off" type={type}
                    value={poolDraft[key]} required={key !== "api_key" || !poolId}
                    placeholder={key === "api_key" && poolId ? "已保存" : ""}
                    {...(type === "number" ? { min: key === "max_concurrency" ? 1 : 0, max: key === "max_concurrency" ? 10 : 3600, step: key === "max_concurrency" ? 1 : 0.5 } : {})}
                    onChange={(e) => { setSaved(false); setPoolDraft((v) => ({ ...v, [key]: type === "number" ? Number(e.target.value) : e.target.value })); }} /></label>
                ))}
              </div>
              <label className="check-field"><input type="checkbox" checked={poolDraft.enabled}
                onChange={(e) => { setSaved(false); setPoolDraft((v) => ({ ...v, enabled: e.target.checked })); }} />启用账号</label>
              {values.key_pool?.find((entry) => entry.id === poolId)?.blocked_reason && <button type="button" className="button" onClick={async () => {
                try { await api(`/settings/key-pool/${poolId}/reset`, "POST", {}); const fresh = await api("/settings"); setValues(fresh); onSaved(fresh); setSaved(true); }
                catch (e) { setError(explain(e.message)); }
              }}><RefreshCw size={14} />解除认证暂停</button>}
            </>
          )}
          {tab === "collector" && (
            <div className="form-grid">
              {field(
                "collector",
                "duration_seconds",
                "监听时长（秒，0持续）",
                "number",
                { min: 0, max: 604800 },
              )}
              {field(
                "collector",
                "scan_interval_ms",
                "消息补扫间隔（毫秒）",
                "number",
                { min: 50, max: 2000 },
              )}
              {field(
                "collector",
                "batch_interval_seconds",
                "批次读取间隔（秒）",
                "number",
                { min: 0.2, max: 10, step: 0.1 },
              )}
            </div>
          )}
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer>
          <span className="save-feedback">
            {saved && (
              <>
                <Check size={15} />
                已保存
              </>
            )}
          </span>
          <button type="submit" className="button primary" disabled={busy}>
            {busy ? (
              <LoaderCircle size={15} className="spin" />
            ) : (
              <Check size={15} />
            )}
            保存
          </button>
        </footer>
      </form>
    </dialog>
  );
}

createRoot(document.getElementById("root")).render(<App />);
