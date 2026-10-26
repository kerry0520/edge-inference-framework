/* Offline Node tests. They exercise parser + page controller, not phone inference. */
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../../scripts/android/phone_web/app.js", import.meta.url), "utf8");
const web = await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const state = (changes = {}) => ({status: "ready", session_id: "http-session", revision: 0,
  csrf_token: "csrf-secret", history: [], ...changes});
const start = {request_id: "request-1", session_id: "http-session", revision: 0};
const event = (name, data) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`;
const successful = (text = "你好") => event("start", start) + event("delta", {text}) +
  event("done", {...start, revision: 1, text, response: {status: "normal_finished", tokens: [1]}});
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: {"Content-Type": "application/json"}});

function streamResponse(wire, perByte = false) {
  const bytes = new TextEncoder().encode(wire);
  return new Response(new ReadableStream({start(controller) {
    if (perByte) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    else controller.enqueue(bytes);
    controller.close();
  }}), {headers: {"Content-Type": "text/event-stream; charset=utf-8"}});
}

class Element {
  constructor() { this.children = []; this.listeners = {}; this.value = ""; this.disabled = false;
    this.hidden = false; this.textContent = ""; this.className = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  set innerHTML(_) { throw new Error("Unsafe HTML rendering is forbidden"); }
}

function documentMock() {
  const elements = new Map();
  return {getElementById(id) {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  }, createElement() { return new Element(); }, elements};
}

function pageFixture(handler) {
  const document = documentMock();
  document.getElementById("tokens").value = "64";
  const calls = [];
  const page = web.mountPage(document, async (url, options = {}) => {
    calls.push({url, options});
    return handler(url, options, calls);
  });
  return {page, document, calls, el: id => document.getElementById(id)};
}

test("health validation + ready/busy/faulted/closed request state", () => {
  assert.equal(web.validateHealth(state()).status, "ready");
  assert.equal(web.canSend(state()), true);
  for (const status of ["busy", "faulted", "closed"]) assert.equal(web.canSend(state({status})), false);
  assert.equal(web.canSend(state(), true), false);
  for (const changes of [{revision: -1}, {revision: true}, {csrf_token: ""},
    {status: "invented"}, {history: [{role: "system", content: "bad"}]}]) {
    assert.throws(() => web.validateHealth(state(changes)));
  }
});

test("POST body uses exact observed identity/revision; invalid prompt/token rejected", () => {
  assert.deepEqual(web.requestBody(state(), "你好", 64), {
    session_id: "http-session", revision: 0, prompt: "你好", max_new_tokens: 64, stream: true});
  for (const [prompt, tokens] of [["", 64], ["a\0b", 64], [1, 64], ["a", 0], ["a", true]]) {
    assert.throws(() => web.requestBody(state(), prompt, tokens));
  }
  assert.throws(() => web.requestBody(state({status: "busy"}), "a", 64));
});

test("SSE strict sequence works when every decoded character is fragmented", () => {
  const events = [];
  const parser = web.createSSEParser((kind, value) => events.push([kind, value]));
  for (const char of successful("第一行\n第二行")) parser.push(char);
  assert.equal(parser.finish().terminal, "done");
  assert.equal(parser.finish().text, "第一行\n第二行");
  assert.deepEqual(events.map(item => item[0]), ["start", "delta", "done"]);
});

test("SSE CRLF, blank comments and multiline JSON data are handled", () => {
  const parser = web.createSSEParser(() => {});
  parser.push(": keepalive\r\n\r\n" + successful().replaceAll("\n", "\r\n"));
  assert.equal(parser.finish().text, "你好");
  const multiline = web.createSSEParser(() => {});
  multiline.push(`event: start\ndata: {"request_id":"r",\ndata: "session_id":"s","revision":0}\n\n`);
  multiline.push(event("error", {error: {code: "context_budget"}}));
  assert.equal(multiline.finish().terminal, "error");
});

test("UTF-8 decoder preserves Chinese and emoji split across every byte", async () => {
  const events = [];
  const result = await web.readEventStream(streamResponse(successful("你好，🌱"), true),
    (name, value) => events.push([name, value]));
  assert.equal(result.text, "你好，🌱");
  assert.equal(events[1][1].text, "你好，🌱");
});

test("EOF before done, truncated frames and invalid event orders fail closed", () => {
  for (const wire of ["", event("start", start), event("start", start) + event("delta", {text: "片段"}),
    event("delta", {text: "bad"}), event("start", start) + event("start", start),
    successful() + event("delta", {text: "extra"}),
    event("start", start) + "event: done\ndata: {", "event: nonsense\ndata: {}\n\n"]) {
    const parser = web.createSSEParser(() => {});
    assert.throws(() => { parser.push(wire); parser.finish(); });
  }
});

test("done identity, revision, text and repeated terminal mismatches are rejected", () => {
  for (const change of [{request_id: "wrong"}, {session_id: "wrong"}, {revision: 0}, {text: "different"}]) {
    const parser = web.createSSEParser(() => {});
    assert.throws(() => parser.push(event("start", start) + event("delta", {text: "a"}) +
      event("done", {...start, revision: 1, text: "a", ...change})));
  }
  const parser = web.createSSEParser(() => {});
  assert.throws(() => parser.push(successful() + event("error", {error: {code: "bad"}})));
});

test("malformed UTF-8 and non-SSE responses are rejected", async () => {
  await assert.rejects(web.readEventStream(new Response(Uint8Array.of(0xff), {
    headers: {"Content-Type": "text/event-stream"}}), () => {}));
  await assert.rejects(web.readEventStream(json({}), () => {}));
});

test("oversize incomplete SSE events are bounded", () => {
  const parser = web.createSSEParser(() => {});
  assert.throws(() => parser.push("x".repeat(2097153)));
});

test("page initialization and refresh only GET; untrusted text uses safe DOM", async () => {
  const payload = '<img src=x onerror="alert(1)">';
  const fixture = pageFixture(() => json(state({history: [{role: "assistant", content: payload}]})));
  await fixture.page.ready;
  await fixture.page.refresh();
  assert.equal(fixture.calls.length, 2);
  assert.ok(fixture.calls.every(call => call.url === "/health" && !call.options.method));
  assert.equal(fixture.el("messages").children[0].children[1].textContent, payload);
});

test("model label follows server identity and remains plain text", async () => {
  const model = 'Qwen3-1.7B <img src=x>';
  const fixture = pageFixture(() => json(state({backend: {model},
    history: [{role: "assistant", content: "answer"}]})));
  await fixture.page.ready;
  assert.equal(fixture.el("model-name").textContent, model);
  assert.equal(fixture.el("messages").children[0].children[0].textContent, model + " · 手机模型");
});

test("successful stream POST includes CSRF and commits UI only after health sync", async () => {
  let releaseHealth;
  const gate = new Promise(resolve => { releaseHealth = resolve; });
  let healthCalls = 0;
  const fixture = pageFixture((url) => {
    if (url === "/v1/chat") return streamResponse(successful());
    if (++healthCalls === 1) return json(state());
    return gate.then(() => json(state({revision: 1, history: [
      {role: "user", content: "question"}, {role: "assistant", content: "你好"}]})));
  });
  await fixture.page.ready;
  fixture.el("prompt").value = "question";
  const running = fixture.page.perform(false);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.el("send").disabled, true);
  assert.equal(fixture.el("pending").hidden, false);
  assert.equal(fixture.el("pending-label").textContent, "正在生成 · 尚未确认");
  releaseHealth();
  await running;
  const post = fixture.calls.find(call => call.options.method === "POST");
  assert.equal(post.options.headers["X-Phone-CSRF"], "csrf-secret");
  assert.equal(JSON.parse(post.options.body).revision, 0);
  assert.equal(fixture.el("prompt").value, "");
  assert.equal(fixture.el("pending").hidden, true);
  assert.equal(fixture.el("messages").children.length, 2);
  assert.equal(fixture.el("send").disabled, false);
});

test("missing done retains draft, labels partial unconfirmed and never retries", async () => {
  const fixture = pageFixture(url => url === "/v1/chat" ?
    streamResponse(event("start", start) + event("delta", {text: "不完整"})) : json(state()));
  await fixture.page.ready;
  fixture.el("prompt").value = "do not resend";
  await fixture.page.perform(false);
  assert.equal(fixture.el("prompt").value, "do not resend");
  assert.equal(fixture.el("pending-text").textContent, "不完整");
  assert.equal(fixture.el("pending").className, "pending uncertain");
  assert.match(fixture.el("notice").textContent, /未确认|尚未确认/);
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("failed final health synchronization preserves draft and disables POST", async () => {
  let healthCalls = 0;
  const fixture = pageFixture(url => {
    if (url === "/v1/chat") return streamResponse(successful());
    if (++healthCalls === 1) return json(state());
    throw new Error("offline after answer");
  });
  await fixture.page.ready;
  fixture.el("prompt").value = "keep me";
  await fixture.page.perform(false);
  assert.equal(fixture.el("prompt").value, "keep me");
  assert.equal(fixture.el("send").disabled, true);
  assert.equal(fixture.el("pending").className, "pending uncertain");
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("service replacement after done does not silently clear original draft", async () => {
  let healthCalls = 0;
  const fixture = pageFixture(url => url === "/v1/chat" ? streamResponse(successful()) :
    json(state(++healthCalls === 1 ? {} : {session_id: "replacement-service"})));
  await fixture.page.ready;
  fixture.el("prompt").value = "preserve across replacement";
  await fixture.page.perform(false);
  assert.equal(fixture.el("prompt").value, "preserve across replacement");
  assert.equal(fixture.el("pending").className, "pending uncertain");
  assert.match(fixture.el("notice").textContent, /服务会话已变化/);
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("stream error preserves partial and then presents confirmed history from GET", async () => {
  let healthCalls = 0;
  const confirmed = [{role: "user", content: "confirmed earlier"},
    {role: "assistant", content: "confirmed answer"}];
  const fixture = pageFixture(url => url === "/v1/chat" ?
    streamResponse(event("start", start) + event("delta", {text: "partial"}) +
      event("error", {error: {code: "worker_unavailable"}})) :
    json(state(++healthCalls === 1 ? {} : {status: "faulted", revision: 1, history: confirmed})));
  await fixture.page.ready;
  fixture.el("prompt").value = "keep draft";
  await fixture.page.perform(false);
  assert.equal(fixture.el("pending-text").textContent, "partial");
  assert.equal(fixture.el("pending").className, "pending uncertain");
  assert.equal(fixture.el("messages").children.length, 2);
  assert.equal(fixture.el("messages").children[1].children[1].textContent, "confirmed answer");
  assert.equal(fixture.el("send").disabled, true);
  assert.equal(fixture.el("prompt").value, "keep draft");
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("busy/faulted/closed are read-only and cannot call POST", async () => {
  for (const status of ["busy", "faulted", "closed"]) {
    const fixture = pageFixture(() => json(state({status})));
    await fixture.page.ready;
    fixture.el("prompt").value = "blocked";
    await fixture.page.perform(false);
    await fixture.page.perform(true);
    assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 0);
    assert.equal(fixture.el("send").disabled, true);
  }
});

test("new uses explicit observed state + CSRF and is followed only by GET", async () => {
  let healthCalls = 0;
  const fixture = pageFixture(url => {
    if (url === "/v1/new") return json({session_id: "http-session", revision: 1, text: ""});
    return json(state({revision: healthCalls++ ? 1 : 0}));
  });
  await fixture.page.ready;
  fixture.el("prompt").value = "unsent draft";
  await fixture.page.perform(true);
  assert.deepEqual(fixture.calls.map(call => call.url), ["/health", "/v1/new", "/health"]);
  const post = fixture.calls[1];
  assert.deepEqual(JSON.parse(post.options.body), {session_id: "http-session", revision: 0});
  assert.equal(post.options.headers["X-Phone-CSRF"], "csrf-secret");
  assert.equal(fixture.el("prompt").value, "unsent draft");
});

test("new settles transient busy using only GET and enables next send", async () => {
  let healthCalls = 0;
  const fixture = pageFixture(url => {
    if (url === "/v1/new") return json({session_id: "http-session", revision: 1, text: ""});
    healthCalls += 1;
    return json(state(healthCalls === 1 ? {} : {
      revision: 1, status: healthCalls === 2 ? "busy" : "ready"}));
  });
  await fixture.page.ready;
  fixture.el("prompt").value = "keep unsent draft";
  await fixture.page.perform(true);
  assert.equal(fixture.el("send").disabled, false);
  assert.equal(fixture.el("new").disabled, false);
  assert.equal(fixture.el("prompt").value, "keep unsent draft");
  assert.deepEqual(fixture.calls.map(call => call.url), ["/health", "/v1/new", "/health", "/health"]);
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("persistent busy settles finitely without POST replay or losing draft", async () => {
  let healthCalls = 0;
  const fixture = pageFixture(url => {
    if (url === "/v1/chat") return streamResponse(successful());
    return json(state(++healthCalls === 1 ? {} : {revision: 1, status: "busy"}));
  });
  await fixture.page.ready;
  fixture.el("prompt").value = "retain while settling";
  await fixture.page.perform(false);
  assert.equal(fixture.el("prompt").value, "retain while settling");
  assert.equal(fixture.el("send").disabled, true);
  assert.match(fixture.el("notice").textContent, /仍在收尾/);
  assert.ok(healthCalls >= 3 && healthCalls <= 8);
  const completedCalls = fixture.calls.length;
  await new Promise(resolve => setTimeout(resolve, 150));
  assert.equal(fixture.calls.length, completedCalls);
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});

test("HTTP API failures keep draft, show safe text, and never resubmit", async () => {
  const fixture = pageFixture(url => url === "/v1/chat" ?
    json({error: {code: "stale_state", message: "/private/native/session_token"}}, 409) : json(state()));
  await fixture.page.ready;
  fixture.el("prompt").value = "original";
  await fixture.page.perform(false);
  assert.equal(fixture.el("prompt").value, "original");
  assert.match(fixture.el("notice").textContent, /对话状态已变化/);
  assert.doesNotMatch(fixture.el("notice").textContent, /private|session_token/);
  assert.equal(fixture.calls.filter(call => call.options.method === "POST").length, 1);
});
