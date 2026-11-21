/** A-Brain uses the existing persistent conversation and routing service. */
export function initializeHub({ json, resizeCharts }) {
  const $ = (id) => document.getElementById(id);
  const agents = {
    leader: { name: "Leader Agent", role: "任务规划与协作", color: "#f8fafc" },
    product: {
      name: "商品运营 Agent",
      role: "商品、定价与库存",
      color: "#34d399",
    },
    ads: { name: "广告分析 Agent", role: "广告查询与分析", color: "#60a5fa" },
    research: {
      name: "选品研究 Agent",
      role: "市场与商品研究",
      color: "#f472b6",
    },
    content: {
      name: "内容策划 Agent",
      role: "文案与内容分析",
      color: "#818cf8",
    },
  };
  let selected = "leader",
    generation = 0,
    toastTimer;
  const sessions = new Map(),
    pending = new Set(),
    loading = new Set(),
    reads = new Map(),
    unreadable = new Set(),
    drafts = new Map(),
    workspace = localStorage.getItem("shop") || "default";
  const storageKey = (key) => `abrain:session:${workspace}:${key}`;
  function toast(value) {
    $("toastBox").textContent = value;
    $("toastBox").classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toastBox").classList.remove("show"), 4000);
  }
  function switchView(value) {
    $("hubView").classList.toggle("hidden", value !== "hub");
    $("dashboardView").classList.toggle("hidden", value !== "dashboard");
    for (const [id, name] of [
      ["navHubBtn", "hub"],
      ["navDashBtn", "dashboard"],
    ]) {
      $(id).classList.toggle("neu-pill-active", value === name);
      $(id).setAttribute("aria-pressed", String(value === name));
    }
    document
      .querySelector(".command-footer")
      .classList.toggle("hidden", value !== "hub");
    document
      .querySelector(".command-hint")
      .classList.toggle("hidden", value !== "hub");
    const url = new URL(location.href);
    url.searchParams.set("view", value);
    history.replaceState(null, "", url);
    if (value === "dashboard") requestAnimationFrame(resizeCharts);
  }
  function append(role, value) {
    const row = document.createElement("div");
    row.className = "msg-row " + role;
    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.textContent = value.replace(/^【当前协作角色：[^\n]+】\n/, "");
    row.append(bubble);
    $("modalChatLog").append(row);
    return bubble;
  }
  function render() {
    const session = sessions.get(selected);
    $("modalChatLog").replaceChildren();
    if (session?.messages?.length)
      for (const m of session.messages)
        append(m.role === "user" ? "user" : "bot", m.content);
    else
      append(
        "bot",
        `你好，我是${agents[selected].name}。请告诉我需要处理的任务；查询结果与执行状态以实际接口返回为准。`,
      );
    $("modalChatStatus").textContent = pending.has(selected)
      ? "正在处理，请稍候…"
      : "";
    $("modalChatForm").querySelector("button[type=submit]").disabled =
      pending.has(selected) || loading.has(selected);
    $("modalChatLog").scrollTop = $("modalChatLog").scrollHeight;
  }
  async function open(key) {
    drafts.set(selected, $("modalChatInput").value);
    selected = key;
    $("modalChatInput").value = drafts.get(key) || "";
    const version = ++generation,
      cfg = agents[key];
    document.querySelectorAll("[data-agent]").forEach((el) => {
      el.classList.toggle("is-selected", el.dataset.agent === key);
      el.setAttribute("aria-pressed", String(el.dataset.agent === key));
    });
    $("modalAgentTitle").textContent = cfg.name;
    $("modalAgentRole").textContent = cfg.role;
    $("modalAgentDot").style.background = cfg.color;
    if (!$("chatModal").open) $("chatModal").showModal();
    render();
    $("modalChatInput").focus();
    const id = localStorage.getItem(storageKey(key));
    if (!sessions.has(key) && id) {
      loading.add(key);
      $("modalChatForm").querySelector("button[type=submit]").disabled = true;
      $("modalChatStatus").textContent = "正在读取历史会话…";
      try {
        let read = reads.get(key);
        if (!read) {
          read = json("/api/sessions/" + encodeURIComponent(id));
          reads.set(key, read);
        }
        const session = await read;
        if (localStorage.getItem(storageKey(key)) === id)
          sessions.set(key, session);
        unreadable.delete(key);
        if (version === generation && selected === key) render();
      } catch (e) {
        unreadable.add(key);
        if (version === generation)
          $("modalChatStatus").textContent = e.message;
      } finally {
        loading.delete(key);
        reads.delete(key);
        if (version === generation)
          $("modalChatForm").querySelector("button[type=submit]").disabled =
            pending.has(key);
      }
    }
  }
  async function send(value) {
    const question = value.trim(),
      key = selected;
    if (!question || pending.has(key) || loading.has(key)) return;
    if (unreadable.has(key)) {
      toast("历史会话尚未读到，请重新打开卡片重试，或新建会话。");
      return;
    }
    if (question.length > 3500) {
      toast("请将问题控制在3500字以内。");
      return;
    }
    pending.add(key);
    render();
    append("user", question);
    $("modalChatLog").scrollTop = $("modalChatLog").scrollHeight;
    $("modalChatInput").value = "";
    drafts.set(key, "");
    let succeeded = false;
    try {
      let session = sessions.get(key);
      if (!session) {
        session = await json("/api/sessions", {
          method: "POST",
          body: JSON.stringify({
            title: agents[key].name + " · " + question.slice(0, 50),
          }),
        });
        sessions.set(key, session);
        localStorage.setItem(storageKey(key), session.id);
      }
      const response = await json(
        "/api/sessions/" + encodeURIComponent(session.id) + "/messages",
        {
          method: "POST",
          body: JSON.stringify({
            content: `【当前协作角色：${agents[key].name}】\n${question}`,
          }),
        },
      );
      sessions.set(key, response);
      succeeded = true;
    } catch (e) {
      drafts.set(key, question);
      if (selected === key && $("chatModal").open) {
        append("error", e.message);
        $("modalChatStatus").textContent = "发送失败；问题已保留，可重试。";
        $("modalChatInput").value = question;
      } else toast(agents[key].name + "：" + e.message);
    } finally {
      pending.delete(key);
      if (selected === key)
        $("modalChatForm").querySelector("button[type=submit]").disabled =
          false;
    }
    if (succeeded) {
      if (selected === key && $("chatModal").open) render();
      else toast(agents[key].name + " 已返回结果，打开卡片查看。");
    }
  }
  const hint = document.createElement("div");
  hint.className = "command-hint";
  hint.textContent = "选择一位 Agent，或直接向 Leader 描述你的任务";
  document.querySelector(".command-footer").before(hint);
  $("navHubBtn").onclick = () => switchView("hub");
  $("navDashBtn").onclick = () => switchView("dashboard");
  document.querySelector(".brand-btn").onclick = () => switchView("hub");
  document.querySelectorAll("[data-agent]").forEach((card) => {
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.setAttribute("aria-label", agents[card.dataset.agent].name);
    card.onclick = () => open(card.dataset.agent);
    card.onkeydown = (e) => {
      if (["Enter", " "].includes(e.key)) {
        e.preventDefault();
        open(card.dataset.agent);
      }
    };
  });
  $("closeHubChatBtn").onclick = () => $("chatModal").close();
  $("chatModal").addEventListener("close", () => {
    generation++;
    drafts.set(selected, $("modalChatInput").value);
  });
  $("modalChatForm").onsubmit = (e) => {
    e.preventDefault();
    send($("modalChatInput").value);
  };
  $("commandForm").onsubmit = async (e) => {
    e.preventDefault();
    const value = $("commandInput").value;
    if (!value.trim()) return;
    if (pending.has(selected) || loading.has(selected)) {
      toast("当前 Agent 正在处理，请稍候。");
      return;
    }
    await open(selected);
    if (unreadable.has(selected)) return;
    $("commandInput").value = "";
    send(value);
  };
  $("settingsBtn").onclick = () => location.assign("/?view=settings");
  $("clearAccessBtn").onclick = () => {
    sessionStorage.removeItem("token");
    location.assign("/?view=settings");
  };
  $("historyBtn").onclick = async () => {
    $("historyDialog").showModal();
    $("historyList").textContent = "正在读取…";
    try {
      const rows = await json("/api/sessions");
      $("historyList").replaceChildren();
      for (const session of rows
        .sort(
          (a, b) =>
            Number(
              Object.keys(agents).some(
                (k) => localStorage.getItem(storageKey(k)) === b.id,
              ),
            ) -
            Number(
              Object.keys(agents).some(
                (k) => localStorage.getItem(storageKey(k)) === a.id,
              ),
            ),
        )
        .slice(0, 40)) {
        const button = document.createElement("button");
        button.className = "history-item";
        button.textContent = session.title || "未命名会话";
        button.onclick = () => {
          const key =
            Object.keys(agents).find((k) =>
              session.title?.startsWith(agents[k].name),
            ) || "leader";
          if (pending.has(key) || loading.has(key)) {
            toast("当前 Agent 正在处理，请等待完成后切换历史会话。");
            return;
          }
          localStorage.setItem(storageKey(key), session.id);
          sessions.delete(key);
          $("historyDialog").close();
          open(key);
        };
        $("historyList").append(button);
      }
      if (!rows.length)
        $("historyList").textContent =
          "还没有历史会话。发送第一个问题后会自动保存。";
    } catch (e) {
      $("historyList").textContent = e.message;
    }
  };
  $("closeHistoryBtn").onclick = () => $("historyDialog").close();
  $("newHubChatBtn").onclick = () => {
    if (pending.has(selected)) {
      toast("当前会话仍在处理，请等待结果后再新建。");
      return;
    }
    sessions.delete(selected);
    unreadable.delete(selected);
    drafts.delete(selected);
    $("modalChatInput").value = "";
    localStorage.removeItem(storageKey(selected));
    $("historyDialog").close();
    open(selected);
  };
  for (const [button, panel, label] of [
    ["toggleExtendedBtn", "extendedDimensionsPanel", "维度明细"],
    ["toggleAiPanelBtn", "aiQaPanel", "AI 问答"],
  ]) {
    $(button).setAttribute("aria-expanded", "false");
    $(button).onclick = () => {
      const hidden = $(panel).classList.toggle("hidden");
      $(button).textContent = (hidden ? "展开" : "收起") + label;
      $(button).setAttribute("aria-expanded", String(!hidden));
      if (!hidden) requestAnimationFrame(resizeCharts);
    };
  }
  $("densityComfortable").classList.add("active");
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) {
    document.querySelectorAll(".magnetic").forEach((element) => {
      element.addEventListener("pointermove", (event) => {
        if (event.pointerType !== "mouse") return;
        const rect = element.getBoundingClientRect();
        const strength = Number(element.dataset.strength) || 0.2;
        element.style.transform = `translate3d(${(event.clientX - rect.left - rect.width / 2) * strength}px,${(event.clientY - rect.top - rect.height / 2) * strength}px,0)`;
      });
      element.addEventListener("pointerleave", () => {
        element.style.transform = "";
      });
    });
    $("commandInput").addEventListener("focus", () =>
      $("gooDropletStage").classList.remove("hidden"),
    );
    $("commandInput").addEventListener("blur", () =>
      $("gooDropletStage").classList.add("hidden"),
    );
    let frame = 0;
    document.addEventListener(
      "pointermove",
      (e) => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
          frame = 0;
          document.documentElement.style.setProperty(
            "--mouse-x",
            `${e.clientX}px`,
          );
          document.documentElement.style.setProperty(
            "--mouse-y",
            `${e.clientY}px`,
          );
          const card = e.target.closest(".spotlight-card");
          if (card) {
            const r = card.getBoundingClientRect();
            card.style.setProperty("--card-x", `${e.clientX - r.left}px`);
            card.style.setProperty("--card-y", `${e.clientY - r.top}px`);
          }
        });
      },
      { passive: true },
    );
  }
  switchView(
    new URLSearchParams(location.search).get("view") === "hub"
      ? "hub"
      : "dashboard",
  );
}
