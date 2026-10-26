/* Same-origin client. No request is automatically submitted or retried. */
export function validateHealth(value) {
  if (!value || typeof value !== "object" ||
      !["ready", "busy", "faulted", "closed"].includes(value.status) ||
      typeof value.session_id !== "string" || !value.session_id ||
      !Number.isSafeInteger(value.revision) || value.revision < 0 ||
      typeof value.csrf_token !== "string" || !value.csrf_token ||
      !Array.isArray(value.history) || value.history.some(message =>
        !message || !["user", "assistant"].includes(message.role) || typeof message.content !== "string")) {
    throw new Error("服务状态无法确认，请刷新状态。");
  }
  return value;
}

export function canSend(health, pending = false) {
  return !pending && health?.status === "ready";
}

export function requestBody(health, prompt, maxNewTokens) {
  validateHealth(health);
  if (!canSend(health) || typeof prompt !== "string" || !prompt.trim() || prompt.includes("\0") ||
      !Number.isInteger(maxNewTokens) || maxNewTokens < 1 || maxNewTokens > 512) {
    throw new Error("当前无法发送，请检查服务状态与输入。");
  }
  return {session_id: health.session_id, revision: health.revision,
    prompt, max_new_tokens: maxNewTokens, stream: true};
}

export function createSSEParser(onEvent) {
  let buffer = "", lines = [], lineSize = 0, started = false, terminal = null;
  let requestId = null, sessionId = null, revision = null, text = "";

  function dispatch() {
    if (!lines.length) return;
    let event = null;
    const data = [];
    for (const line of lines) {
      if (line.startsWith(":")) continue;
      const index = line.indexOf(":");
      const field = index < 0 ? line : line.slice(0, index);
      let value = index < 0 ? "" : line.slice(index + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") {
        if (event !== null) throw new Error("重复的流事件类型。");
        event = value;
      } else if (field === "data") data.push(value);
      else throw new Error("无法识别的流字段。");
    }
    lines = [];
    lineSize = 0;
    if (event === null && data.length === 0) return;
    if (!["start", "delta", "done", "error"].includes(event) || terminal) {
      throw new Error("回答流的事件顺序异常。");
    }
    const value = JSON.parse(data.join("\n"));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("回答流内容异常。");
    if (event === "start") {
      if (started || typeof value.request_id !== "string" || !value.request_id ||
          typeof value.session_id !== "string" || !value.session_id ||
          !Number.isSafeInteger(value.revision) || value.revision < 0) throw new Error("回答流起始状态异常。");
      started = true;
      requestId = value.request_id;
      sessionId = value.session_id;
      revision = value.revision;
    } else {
      if (!started) throw new Error("回答流缺少起始事件。");
      if (event === "delta") {
        if (typeof value.text !== "string" || text.length + value.text.length > 1048576) throw new Error("回答片段异常。");
        text += value.text;
      } else if (event === "done") {
        if (value.request_id !== requestId || value.session_id !== sessionId ||
            value.revision !== revision + 1 || value.text !== text) throw new Error("回答流与最终确认不一致。");
        terminal = event;
      } else {
        if (!value.error || typeof value.error.code !== "string") throw new Error("回答流错误信息异常。");
        terminal = event;
      }
    }
    onEvent(event, value);
  }

  return {
    push(chunk) {
      if (typeof chunk !== "string") throw new TypeError("SSE parser requires decoded text");
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        let line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (line.endsWith("\r")) line = line.slice(0, -1);
        if (line === "") dispatch();
        else {
          lineSize += line.length;
          if (lineSize > 2097152) throw new Error("流事件超过页面上限。");
          lines.push(line);
        }
      }
      if (buffer.length + lineSize > 2097152) throw new Error("流事件超过页面上限。");
    },
    finish() {
      if (buffer || lines.length || !terminal) throw new Error("连接已结束，但回答尚未确认。请刷新状态，勿直接重发。");
      return {terminal, text, request_id: requestId, session_id: sessionId, revision};
    }
  };
}

export async function readEventStream(response, onEvent) {
  if (!response.body || !response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream")) {
    throw new Error("没有收到有效的流式回答。");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  const parser = createSSEParser(onEvent);
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, {stream: true}));
    }
    parser.push(decoder.decode());
    return parser.finish();
  } catch (error) {
    try { await reader.cancel(); } catch { /* Preserve the original failure. */ }
    throw error;
  } finally {
    reader.releaseLock();
  }
}

export function publicError(code) {
  return ({context_budget: "当前对话超过模型的部署预算。可以缩短问题或开始新对话，历史未被截断。",
    busy: "服务正在处理另一个请求。请稍后刷新状态，不要重复发送。",
    stale_state: "对话状态已变化，请先查看刷新后的确认历史，再决定下一步。",
    csrf_rejected: "服务状态已变化，请刷新状态后再操作。",
    unavailable: "服务已停止接收请求。请重新启动本地服务。",
    worker_unavailable: "连接或生成未能完整确认。请查看确认历史，不要直接重发。"})[code] ||
    "请求未能完成。请刷新状态核对确认历史，不要直接重发。";
}

export function mountPage(document, fetch) {
  const $ = id => document.getElementById(id);
  const elements = Object.fromEntries(["prompt", "tokens", "send", "new", "refresh", "composer", "messages",
    "pending", "pending-label", "pending-text", "notice", "status", "status-dot", "model-name"].map(id => [id, $(id)]));
  let health = null, pending = false, streamText = "";

  function modelName() {
    const name = health?.backend?.model;
    return typeof name === "string" && name.length > 0 && name.length <= 80 ? name : "手机模型";
  }

  function notice(text) {
    elements.notice.textContent = text;
    elements.notice.hidden = !text;
  }

  function controls() {
    const enabled = canSend(health, pending);
    elements.send.disabled = !enabled;
    elements.new.disabled = !enabled;
    // Draft remains editable when the service is offline, but not during a send.
    elements.prompt.disabled = pending;
    elements.tokens.disabled = pending;
    elements.refresh.disabled = pending;
    const status = pending ? "busy" : health?.status;
    elements["status-dot"].className = "status-dot " + (status || "offline");
    elements.status.textContent = ({ready: "手机模型已就绪，可以开始对话", busy: "正在生成，请等待本次回答",
      faulted: "本次服务已中断，请核对确认历史后重启服务", closed: "服务已关闭"})[status] || "尚未连接到本地服务";
  }

  function historyView(messages) {
    elements.messages.replaceChildren();
    if (!messages.length) {
      const box = document.createElement("div");
      box.className = "empty";
      for (const [tag, text, className] of [["div", "✳", "empty-symbol"], ["h3", "从一个好奇开始", ""],
        ["p", "可以问一道题、请它解释一个概念，或让它帮你写一段话。对话在手机上继续。", ""]]) {
        const element = document.createElement(tag);
        element.textContent = text;
        element.className = className;
        box.append(element);
      }
      elements.messages.append(box);
    }
    for (const message of messages) {
      const article = document.createElement("article");
      article.className = "message " + message.role;
      const label = document.createElement("div");
      label.className = "message-label";
      label.textContent = message.role === "user" ? "你" : modelName() + " · 手机模型";
      const text = document.createElement("p");
      text.textContent = message.content;
      article.append(label, text);
      elements.messages.append(article);
    }
  }

  async function refresh() {
    const response = await fetch("/health", {cache: "no-store", credentials: "same-origin"});
    if (!response.ok) throw new Error("无法读取本地服务状态。");
    health = validateHealth(await response.json());
    elements["model-name"].textContent = modelName();
    historyView(health.history);
    controls();
    if (health.recording_error) notice("部分工程记录未能保存。已确认的对话显示在上方，请检查本地服务。");
    return health;
  }

  async function settleAfterReply(body) {
    // A terminal reply can arrive just before the server finishes recording
    // delivery and releases its request gate. Poll only the safe GET endpoint;
    // never repeat the POST or leave a background polling loop running.
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const confirmed = await refresh();
      if (confirmed.session_id !== body.session_id || confirmed.revision < body.revision + 1) {
        throw new Error("服务会话已变化，原回答的最终状态需要重新核对。请勿直接重发。");
      }
      if (confirmed.status !== "busy") return confirmed;
      if (attempt < 5) await new Promise(resolve => setTimeout(resolve, 120));
    }
    throw new Error("服务仍在收尾，已停止自动查询。请稍后手动刷新状态，勿直接重发。");
  }

  async function perform(reset) {
    if (!canSend(health, pending)) return;
    const draft = elements.prompt.value;
    let body;
    try {
      body = reset ? {session_id: health.session_id, revision: health.revision} :
        requestBody(health, draft, Number(elements.tokens.value));
    } catch (error) { notice(error.message); return; }
    pending = true;
    streamText = "";
    elements.pending.hidden = reset;
    elements.pending.className = "pending";
    elements["pending-label"].textContent = "正在生成 · 尚未确认";
    elements["pending-text"].textContent = "";
    notice("");
    controls();
    try {
      const response = await fetch(reset ? "/v1/new" : "/v1/chat", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: {"Content-Type": "application/json", "X-Phone-CSRF": health.csrf_token},
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        let code;
        try { code = (await response.json()).error?.code; } catch { /* No raw server text displayed. */ }
        throw new Error(publicError(code));
      }
      if (reset) {
        const result = await response.json();
        if (result.session_id !== body.session_id || result.revision !== body.revision + 1) throw new Error("新对话状态尚未确认。");
      } else {
        const result = await readEventStream(response, (event, value) => {
          if (event === "start" && (value.session_id !== body.session_id || value.revision !== body.revision)) throw new Error("回答流的会话状态不一致。");
          if (event === "delta") {
            streamText += value.text;
            elements["pending-text"].textContent = streamText;
          }
          if (event === "error") throw new Error(publicError(value.error.code));
        });
        if (result.terminal !== "done") throw new Error("回答尚未确认。");
      }
      await settleAfterReply(body);
      elements.pending.hidden = true;
      if (!reset) elements.prompt.value = "";
    } catch (error) {
      // Keep the original draft, including after a terminal result whose final
      // health sync fails. GET is safe; never resubmit an ambiguous POST.
      elements.prompt.value = draft;
      elements.pending.className = "pending uncertain";
      elements.pending.hidden = reset || !streamText;
      elements["pending-label"].textContent = "临时显示 · 未确认，请以上方确认历史为准";
      notice(error.message || publicError());
      health = null;
      try { await refresh(); } catch { /* Keep controls disabled until manual GET succeeds. */ }
    } finally {
      pending = false;
      controls();
    }
  }

  elements.composer.addEventListener("submit", event => { event.preventDefault(); perform(false); });
  elements.prompt.addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
      event.preventDefault(); perform(false);
    }
  });
  elements.new.addEventListener("click", () => perform(true));
  elements.refresh.addEventListener("click", async () => {
    notice("");
    try { await refresh(); } catch { health = null; controls(); notice("无法连接本地服务，请确认服务仍在运行。"); }
  });
  const ready = refresh().catch(() => { health = null; controls(); notice("尚未连接到服务。请启动本地服务后点击「刷新状态」。"); });
  return {ready, refresh, perform};
}

if (typeof document !== "undefined") mountPage(document, globalThis.fetch.bind(globalThis));
