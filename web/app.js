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
const submitButton = form.querySelector('button[type="submit"]');
let redrawGraphLines = () => {};

const escapeText = (value) => String(value ?? "");

async function loadMeta() {
  try {
    const response = await fetch("api/v1/meta");
    const body = await response.json();
    document.querySelector("#index-version").textContent =
      `INDEX ${body.metadata.index_version} · CUTOFF ${body.metadata.graph_train_cutoff.slice(0, 10)}`;
  } catch {}
}

function nodeType(label, index, length) {
  if (index === 0 || label.startsWith("Query:")) return "query";
  if (index === length - 1 || label.startsWith("Job:")) return "job";
  return "skill";
}

function nodeLabel(label, type) {
  if (type === "query") return label.replace(/^Query:/, "");
  if (type === "skill") return label.replace(/^Skill:/, "").replace(/^skill\./, "");
  return label.replace(/^Job:/, "#");
}

function renderTraceGraph(traces) {
  const visibleTraces = traces.slice(0, 4).filter((trace) => Array.isArray(trace.path) && trace.path.length > 1);
  const visual = document.createElement("div");
  visual.className = "trace-visual";
  visual.setAttribute("role", "img");
  visual.setAttribute("aria-label", "查詢、技能與職缺之間的圖譜推論連線圖");

  const canvas = document.createElement("div");
  canvas.className = "graph-canvas";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("graph-links");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = `
    <defs>
      <marker id="trace-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
        <path d="M0,0 L7,3.5 L0,7 Z"></path>
      </marker>
      <marker id="trace-arrow-related" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto">
        <path d="M0,0 L7,3.5 L0,7 Z"></path>
      </marker>
    </defs>`;
  canvas.append(svg);

  const columns = Array.from({ length: 4 }, (_, index) => {
    const column = document.createElement("div");
    column.className = `graph-column column-${index}`;
    canvas.append(column);
    return column;
  });
  const nodeElements = new Map();
  const edges = [];
  const relatedTargets = new Set();
  visibleTraces.forEach((trace) => {
    trace.edges?.forEach((relation, index) => {
      if (relation.includes("RELATED") && trace.path[index + 1]) {
        relatedTargets.add(trace.path[index + 1]);
      }
    });
  });

  visibleTraces.forEach((trace) => {
    const keys = trace.path.map((label, index) => {
      const type = nodeType(label, index, trace.path.length);
      const columnIndex = type === "query" ? 0 : type === "job" ? 3 : relatedTargets.has(label) ? 2 : 1;
      const key = `${type}:${label}`;
      if (!nodeElements.has(key)) {
        const node = document.createElement("div");
        node.className = `graph-node ${type}`;
        node.title = label;
        const kind = document.createElement("span");
        kind.textContent = type.toUpperCase();
        const name = document.createElement("strong");
        name.textContent = nodeLabel(label, type);
        node.append(kind, name);
        columns[columnIndex].append(node);
        nodeElements.set(key, node);
      }
      return key;
    });
    keys.slice(0, -1).forEach((source, index) => {
      edges.push({
        source,
        target: keys[index + 1],
        relation: trace.edges?.[index] || "LINKS_TO",
      });
    });
  });

  visual.append(canvas);
  const legend = document.createElement("div");
  legend.className = "graph-legend";
  legend.innerHTML = `
    <span><i class="solid"></i>直接命中</span>
    <span><i class="related"></i>技能關聯</span>
    <span class="graph-direction">由上至下推論 <b>↓</b></span>`;
  visual.append(legend);
  graphPanel.append(visual);

  const evidenceList = document.createElement("div");
  evidenceList.className = "trace-evidence-list";
  visibleTraces.forEach((trace, index) => {
    const item = document.createElement("div");
    item.className = "trace-evidence";
    const number = document.createElement("span");
    number.textContent = `PATH ${String(index + 1).padStart(2, "0")}`;
    const copy = document.createElement("div");
    const relation = document.createElement("strong");
    relation.textContent = trace.edges.join(" → ");
    const detail = document.createElement("small");
    detail.textContent = `weight ${trace.weight} · ${trace.evidence}`;
    copy.append(relation, detail);
    item.append(number, copy);
    evidenceList.append(item);
  });
  graphPanel.append(evidenceList);

  redrawGraphLines = () => {
    svg.querySelectorAll(".graph-edge").forEach((edge) => edge.remove());
    const canvasBounds = canvas.getBoundingClientRect();
    if (!canvasBounds.width || !canvasBounds.height) return;
    svg.setAttribute("viewBox", `0 0 ${canvasBounds.width} ${canvasBounds.height}`);
    edges.forEach(({ source, target, relation }) => {
      const sourceBounds = nodeElements.get(source).getBoundingClientRect();
      const targetBounds = nodeElements.get(target).getBoundingClientRect();
      const x1 = sourceBounds.left + sourceBounds.width / 2 - canvasBounds.left;
      const y1 = sourceBounds.bottom - canvasBounds.top;
      const x2 = targetBounds.left + targetBounds.width / 2 - canvasBounds.left;
      const y2 = targetBounds.top - canvasBounds.top;
      const bend = Math.max(12, (y2 - y1) * 0.48);
      const edge = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const sourceLevel = columns.indexOf(nodeElements.get(source).parentElement);
      const targetLevel = columns.indexOf(nodeElements.get(target).parentElement);
      const skipsSkillLayer = targetLevel - sourceLevel > 1 && !source.startsWith("query:");
      if (skipsSkillLayer) {
        const laneX = x1 < canvasBounds.width / 2 ? 20 : canvasBounds.width - 20;
        edge.setAttribute("d", `M ${x1} ${y1} C ${laneX} ${y1 + bend * .45}, ${laneX} ${y2 - bend * .45}, ${x2} ${y2}`);
      } else {
        edge.setAttribute("d", `M ${x1} ${y1} C ${x1} ${y1 + bend}, ${x2} ${y2 - bend}, ${x2} ${y2}`);
      }
      edge.setAttribute("marker-end", relation.includes("RELATED") ? "url(#trace-arrow-related)" : "url(#trace-arrow)");
      edge.classList.add("graph-edge");
      if (relation.includes("RELATED")) edge.classList.add("related");
      svg.append(edge);
    });
  };
  requestAnimationFrame(redrawGraphLines);
}

function showTrace(row, query, graphEnabled) {
  document.querySelectorAll(".job-card").forEach((card) => {
    card.classList.toggle("selected", Boolean(row) && card.dataset.jobId === String(row.job_id));
  });
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
  renderTraceGraph(traces);
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
    card.dataset.jobId = String(row.job_id);
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
  form.setAttribute("aria-busy", "true");
  submitButton.disabled = true;
  submitButton.querySelector(".button-label").textContent = "搜尋中";
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
    form.removeAttribute("aria-busy");
    submitButton.disabled = false;
    submitButton.querySelector(".button-label").textContent = "精準搜尋";
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
window.addEventListener("resize", () => requestAnimationFrame(redrawGraphLines), { passive: true });

loadMeta();
search();
