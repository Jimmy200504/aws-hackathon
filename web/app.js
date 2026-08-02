const form = document.querySelector("#search-form");
const queryInput = document.querySelector("#query");
const locationSelect = document.querySelector("#location");
const graphMode = document.querySelector("#graph-mode");
const results = document.querySelector("#results");
const loading = document.querySelector("#loading");
const errorBox = document.querySelector("#error");
const resultMeta = document.querySelector("#result-meta");
const normalizationPanel = document.querySelector("#query-normalization");
const normalizationBadge = document.querySelector("#normalization-badge");
const graphPanel = document.querySelector("#intent-graph");
const graphBadge = document.querySelector("#graph-badge");
const template = document.querySelector("#result-template");
const submitButton = form.querySelector('button[type="submit"]');
let redrawGraphLines = () => {};

const escapeText = (value) => String(value ?? "");

const intentLabels = {
  intent_type: "意圖類型",
  duty_categories: "職務分類",
  locations: "地區",
  employment_types: "僱用類型",
  shifts: "班別",
  salary_type: "薪資類型",
  company: "公司",
  keep_terms: "保留詞",
  confidence: "信心分數",
};

function normalizationSource(source) {
  if (source === "amazon_bedrock_cached") {
    return { badge: "BEDROCK CACHE", label: "Amazon Bedrock · cached", active: true };
  }
  if (source === "amazon_bedrock") {
    return { badge: "BEDROCK LIVE", label: "Amazon Bedrock · live", active: true };
  }
  if (source === "deterministic_fallback") {
    return { badge: "SAFE FALLBACK", label: "Deterministic fallback", fallback: true };
  }
  return { badge: "LOCAL PARSER", label: "Deterministic parser" };
}

function intentValues(value, key) {
  if (Array.isArray(value)) return value.filter(Boolean);
  if (key === "confidence" && Number.isFinite(Number(value))) {
    return [`${Math.round(Number(value) * 100)}%`];
  }
  return value ? [String(value)] : [];
}

function setNormalizationLoading() {
  normalizationBadge.textContent = "NORMALIZING…";
  normalizationBadge.classList.remove("active", "fallback");
  normalizationPanel.replaceChildren();
  const loadingState = document.createElement("div");
  loadingState.className = "normalization-loading";
  loadingState.innerHTML = "<i></i><span>Bedrock 正在解析搜尋意圖…</span>";
  normalizationPanel.append(loadingState);
}

function renderNormalization(normalization, rawQuery) {
  normalizationPanel.replaceChildren();
  if (!normalization?.source) {
    normalizationBadge.textContent = "NO DATA";
    normalizationBadge.classList.remove("active", "fallback");
    normalizationPanel.innerHTML = '<p class="panel-empty">這次回應沒有查詢正規化資料。</p>';
    return;
  }

  const source = normalizationSource(normalization.source);
  normalizationBadge.textContent = source.badge;
  normalizationBadge.classList.toggle("active", Boolean(source.active));
  normalizationBadge.classList.toggle("fallback", Boolean(source.fallback));

  // `normalized_query` is deliberately not shown. It is the retrieval string --
  // the query with its own keywords repeated to raise their BM25 term
  // frequency -- so it reads to a viewer as the input echoed back. What the
  // model actually understood is the structured intent below.
  const input = document.createElement("div");
  input.className = "normalization-query input-query";
  const inputLabel = document.createElement("span");
  inputLabel.textContent = "ORIGINAL QUERY";
  const inputValue = document.createElement("p");
  inputValue.textContent = rawQuery;
  input.append(inputLabel, inputValue);
  normalizationPanel.append(input);

  const structuredIntent = normalization.structured_intent || {};
  const fields = Object.entries(intentLabels)
    .map(([key, label]) => ({ key, label, values: intentValues(structuredIntent[key], key) }))
    .filter(({ values }) => values.length);

  if (fields.length) {
    const intent = document.createElement("dl");
    intent.className = "structured-intent";
    fields.forEach(({ label, values }) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      values.forEach((value) => {
        const chip = document.createElement("span");
        chip.textContent = value;
        detail.append(chip);
      });
      row.append(term, detail);
      intent.append(row);
    });
    normalizationPanel.append(intent);
  }

  const provenance = document.createElement("div");
  provenance.className = "normalization-provenance";
  const sourceLabel = document.createElement("span");
  sourceLabel.textContent = source.label;
  const model = document.createElement("code");
  model.textContent = normalization.model_id || "no model invoked";
  model.title = normalization.model_id || "No Bedrock model was invoked";
  provenance.append(sourceLabel, model);
  normalizationPanel.append(provenance);
}

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
  if (label.startsWith("Occupation:") || /^Skill:(occupation|duty)\./.test(label)) return "occupation";
  return "skill";
}

function nodeLabel(label, type) {
  if (type === "query") return label.replace(/^Query:/, "");
  if (type === "skill") return label.replace(/^Skill:/, "").replace(/^skill\./, "");
  if (type === "occupation") {
    return label.replace(/^(Occupation|Skill):/, "").replace(/^(occupation|duty)\./, "");
  }
  return label.replace(/^Job:/, "#");
}

function relationDirection(relation, explicitDirection) {
  if (["forward", "reverse", "undirected"].includes(explicitDirection)) return explicitDirection;
  if (relation.includes("RELATED")) return "undirected";
  if (["REQUIRES", "INSTANCE_OF"].includes(relation)) return "reverse";
  return "forward";
}

function relationSummary(trace) {
  const nodes = trace.path.map((label, index) =>
    nodeType(label, index, trace.path.length).toUpperCase());
  return trace.edges.reduce((summary, relation, index) => {
    const direction = relationDirection(relation, trace.edge_directions?.[index]);
    const target = nodes[index + 1] || "NODE";
    if (direction === "reverse") return `${summary} ←${relation}— ${target}`;
    if (direction === "undirected") return `${summary} —${relation}— ${target}`;
    return `${summary} —${relation}→ ${target}`;
  }, nodes[0] || "QUERY");
}

function renderTraceGraph(traces) {
  const visibleTraces = traces.slice(0, 4).filter((trace) => Array.isArray(trace.path) && trace.path.length > 1);
  const visual = document.createElement("div");
  visual.className = "trace-visual";
  visual.setAttribute("role", "img");
  visual.setAttribute("aria-label", "查詢、技能、職業與職缺之間的圖譜關係圖；箭頭依關係實際方向顯示");

  const canvas = document.createElement("div");
  canvas.className = "graph-canvas";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("graph-links");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = `
    <defs>
      <marker id="trace-arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto-start-reverse">
        <path d="M0,0 L7,3.5 L0,7 Z"></path>
      </marker>
      <marker id="trace-arrow-related" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto-start-reverse">
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
    const displayPath = Array.isArray(trace.display_path) && trace.display_path.length === trace.path.length
      ? trace.display_path
      : trace.path;
    const keys = trace.path.map((label, index) => {
      const type = nodeType(label, index, trace.path.length);
      const columnIndex = type === "query" ? 0 : type === "job" ? 3 : relatedTargets.has(label) ? 2 : 1;
      const key = `${type}:${label}`;
      if (!nodeElements.has(key)) {
        const node = document.createElement("div");
        node.className = `graph-node ${type}`;
        const displayLabel = displayPath[index] || label;
        node.title = displayLabel === label
          ? label
          : `${nodeLabel(displayLabel, type)} · ${label}`;
        const kind = document.createElement("span");
        kind.textContent = type.toUpperCase();
        const name = document.createElement("strong");
        name.textContent = nodeLabel(displayLabel, type);
        node.append(kind, name);
        columns[columnIndex].append(node);
        nodeElements.set(key, node);
      }
      return key;
    });
    keys.slice(0, -1).forEach((source, index) => {
      const relation = trace.edges?.[index] || "LINKS_TO";
      edges.push({
        source,
        target: keys[index + 1],
        relation,
        direction: relationDirection(relation, trace.edge_directions?.[index]),
      });
    });
  });

  visual.append(canvas);
  const legend = document.createElement("div");
  legend.className = "graph-legend";
  legend.innerHTML = `
    <span><i class="solid"></i>直接命中</span>
    <span><i class="related"></i>關聯（無向）</span>
    <span class="graph-direction">箭頭＝關係方向 <b>↕</b></span>`;
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
    relation.textContent = relationSummary(trace);
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
    edges.forEach(({ source, target, relation, direction }) => {
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
      if (direction === "forward") edge.setAttribute("marker-end", "url(#trace-arrow)");
      if (direction === "reverse") edge.setAttribute("marker-start", "url(#trace-arrow)");
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
      ? '<p class="panel-empty">此職缺目前沒有可用的技能 evidence；系統以文字路徑安全降級排序。</p>'
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
  setNormalizationLoading();
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
    renderNormalization(body.meta?.query_normalization, query);
    renderRows(body.result, query, graphEnabled);
    resultMeta.textContent =
      `${body.result.length} RESULTS · ${body.meta.latency_ms} MS · ${graphEnabled ? "GRAPH" : "BASELINE"}`;
  } catch (error) {
    normalizationPanel.innerHTML = '<p class="panel-empty">搜尋失敗，無法取得正規化結果。</p>';
    normalizationBadge.textContent = "UNAVAILABLE";
    normalizationBadge.classList.remove("active", "fallback");
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
