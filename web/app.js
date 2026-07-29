const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#query");
const locationSelect = document.querySelector("#location");
const graphMode = document.querySelector("#graph-mode");
const results = document.querySelector("#results");
const loading = document.querySelector("#loading");
const errorBox = document.querySelector("#error");
const resultMeta = document.querySelector("#result-meta");
const graphPanel = document.querySelector("#intent-graph");
const graphBadge = document.querySelector("#graph-badge");
const template = document.querySelector("#result-template");

const escapeText = (value) => String(value ?? "");

async function loadMeta() {
  try {
    const response = await fetch("api/v1/meta");
    const body = await response.json();
    document.querySelector("#job-count").textContent = Number(body.job_count).toLocaleString("zh-TW");
    document.querySelector("#index-version").textContent =
      `INDEX ${body.metadata.index_version} · CUTOFF ${body.metadata.graph_train_cutoff.slice(0, 10)}`;
  } catch {
    document.querySelector("#job-count").textContent = "離線";
  }
}

function showTrace(row, query, graphEnabled) {
  graphPanel.replaceChildren();
  graphBadge.textContent = graphEnabled ? "GRAPH ON" : "BASELINE";
  graphBadge.classList.toggle("active", graphEnabled);
  const traces = row?.graph_trace || [];
  if (!graphEnabled) {
    graphPanel.innerHTML =
      '<p class="panel-empty">圖譜已關閉。此模式保留文字、條件與行為特徵，供 ablation 對照。</p>';
    return;
  }
  if (!traces.length) {
    graphPanel.innerHTML = row?.graph_eligible === false
      ? '<p class="panel-empty">此職缺晚於圖譜 cutoff，刻意不使用 JD 建邊；目前以 cold-start 文字路徑排序。</p>'
      : '<p class="panel-empty">本筆未命中已驗證的技能邊，未生成推論式說明。</p>';
    return;
  }
  traces.slice(0, 4).forEach((trace) => {
    const item = document.createElement("div");
    item.className = "trace-path";
    const rail = document.createElement("div");
    rail.className = "trace-rail";
    rail.innerHTML = '<span class="trace-dot"></span>';
    const copy = document.createElement("div");
    copy.className = "trace-copy";
    const title = document.createElement("strong");
    title.textContent = trace.path.join(" → ");
    const detail = document.createElement("small");
    detail.textContent = `${trace.edges.join(" / ")} · weight ${trace.weight} · ${trace.evidence}`;
    copy.append(title, detail);
    item.append(rail, copy);
    graphPanel.append(item);
  });
}

function renderRows(rows, query, graphEnabled) {
  results.replaceChildren();
  if (!rows.length) {
    results.innerHTML =
      '<div class="initial-state"><h3>沒有足夠相關的結果</h3><p>請改用較完整的職務名稱或技能。</p></div>';
    showTrace(null, query, graphEnabled);
    return;
  }
  rows.forEach((row, index) => {
    const card = template.content.firstElementChild.cloneNode(true);
    card.style.animationDelay = `${Math.min(index * 45, 360)}ms`;
    card.querySelector(".rank").textContent = `#${String(row.rank).padStart(2, "0")}`;
    card.querySelector("h3").textContent = escapeText(row.title);
    card.querySelector(".job-subtitle").textContent =
      [row.city, row.category, row.salary].filter(Boolean).join(" · ");
    card.querySelector(".fit-score").textContent =
      row.graph_eligible ? `SCORE ${row.score.toFixed(1)}` : "COLD START";
    const tags = card.querySelector(".job-tags");
    [row.industry, ...row.matched_skills].filter(Boolean).slice(0, 4).forEach((label, tagIndex) => {
      const tag = document.createElement("span");
      tag.textContent = label;
      if (tagIndex > 0 || row.matched_skills.includes(label)) tag.className = "skill";
      tags.append(tag);
    });
    card.querySelector(".why p").textContent = escapeText(row.why);
    card.querySelector(".trace-button").addEventListener("click", () => {
      showTrace(row, query, graphEnabled);
      if (window.innerWidth < 920) document.querySelector("#evidence").scrollIntoView();
    });
    results.append(card);
  });
  showTrace(rows[0], query, graphEnabled);
}

async function search(event) {
  event?.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;
  const graphEnabled = graphMode.value === "true";
  errorBox.classList.add("hidden");
  results.classList.add("hidden");
  loading.classList.remove("hidden");
  resultMeta.textContent = "RANKING…";
  try {
    const payload = {
      query,
      top_k: 20,
      use_graph: graphEnabled,
    };
    if (locationSelect.value) payload.location_code = [locationSelect.value];
    const response = await fetch("api/v1/jobs/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error?.message || "搜尋失敗");
    renderRows(body.result, query, graphEnabled);
    resultMeta.textContent =
      `${body.result.length} RESULTS · ${body.meta.latency_ms} MS · ${graphEnabled ? "GRAPH" : "BASELINE"}`;
  } catch (error) {
    errorBox.textContent = `無法完成搜尋：${error.message}`;
    errorBox.classList.remove("hidden");
    resultMeta.textContent = "ERROR";
  } finally {
    loading.classList.add("hidden");
    results.classList.remove("hidden");
  }
}

form.addEventListener("submit", search);
document.querySelectorAll("[data-query]").forEach((button) => {
  button.addEventListener("click", () => {
    queryInput.value = button.dataset.query;
    search();
  });
});
graphMode.addEventListener("change", () => {
  if (queryInput.value.trim()) search();
});

loadMeta();
search();
