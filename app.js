const LANGUAGE = "ZH";
const CONTINUOUS_WINDOW_SIZE = 100;
const API_TIMEOUT_MS = 120000;

const keyCodeMap = {
  Backquote: 192,
  Digit1: 49,
  Digit2: 50,
  Digit3: 51,
  Digit4: 52,
  Digit5: 53,
  Digit6: 54,
  Digit7: 55,
  Digit8: 56,
  Digit9: 57,
  Digit0: 48,
  Minus: 189,
  Equal: 187,
  Backspace: 8,
  Tab: 9,
  KeyQ: 81,
  KeyW: 87,
  KeyE: 69,
  KeyR: 82,
  KeyT: 84,
  KeyY: 89,
  KeyU: 85,
  KeyI: 73,
  KeyO: 79,
  KeyP: 80,
  BracketLeft: 219,
  BracketRight: 221,
  Backslash: 220,
  CapsLock: 20,
  KeyA: 65,
  KeyS: 83,
  KeyD: 68,
  KeyF: 70,
  KeyG: 71,
  KeyH: 72,
  KeyJ: 74,
  KeyK: 75,
  KeyL: 76,
  Semicolon: 186,
  Quote: 222,
  Enter: 13,
  ShiftLeft: 16,
  ShiftRight: 16,
  KeyZ: 90,
  KeyX: 88,
  KeyC: 67,
  KeyV: 86,
  KeyB: 66,
  KeyN: 78,
  KeyM: 77,
  Comma: 188,
  Period: 190,
  Slash: 191,
  ControlLeft: 17,
  ControlRight: 17,
  AltLeft: 18,
  AltRight: 18,
  Space: 32,
  ArrowLeft: 37,
  ArrowUp: 38,
  ArrowRight: 39,
  ArrowDown: 40,
  Delete: 46,
};

const state = {
  participantId: "",
  sessionId: "",
  baselineInfo: null,
  keyCounter: 0,
  globalRecords: [],
  unresolvedKeys: new Map(),
  continuousBuffer: [],
  continuousResults: [],
  continuousPromises: [],
  continuousChunkIndex: 0,
  composeSeq: 0,
  compositionStage: "direct",
  compositionData: "",
  isComposing: false,
  submitStarted: false,
  finalResult: null,
  sessionResultPath: "",
  sessionKeystrokePath: "",
};

const el = {
  loginScreen: document.getElementById("login-screen"),
  writingScreen: document.getElementById("writing-screen"),
  resultScreen: document.getElementById("result-screen"),
  participantId: document.getElementById("participant-id"),
  btnLogin: document.getElementById("btn-login"),
  loginMessage: document.getElementById("login-message"),
  articleInput: document.getElementById("article-input"),
  writerName: document.getElementById("writer-name"),
  btnSubmit: document.getElementById("btn-submit"),
  writingStatus: document.getElementById("writing-status"),
  metricWords: document.getElementById("metric-words"),
  metricVerifications: document.getElementById("metric-verifications"),
  metricKeystrokes: document.getElementById("metric-keystrokes"),
  continuousTotal: document.getElementById("continuous-total"),
  continuousPass: document.getElementById("continuous-pass"),
  continuousFail: document.getElementById("continuous-fail"),
  continuousList: document.getElementById("continuous-list"),
  finalGenuine: document.getElementById("final-genuine"),
  finalScore: document.getElementById("final-score"),
  finalThreshold: document.getElementById("final-threshold"),
  finalSource: document.getElementById("final-source"),
  btnDownloadKeystrokes: document.getElementById("btn-download-keystrokes"),
  btnDownloadResults: document.getElementById("btn-download-results"),
};

function setScreen(screenName) {
  const screens = {
    login: el.loginScreen,
    writing: el.writingScreen,
    result: el.resultScreen,
    register: regEl.screen,
  };

  Object.values(screens).forEach((screen) => {
    if (screen) screen.classList.remove("active");
  });
  if (screens[screenName]) screens[screenName].classList.add("active");
}

function setMessage(node, message, type = "") {
  node.textContent = message;
  node.className = `message ${type}`.trim();
}

function formatNumber(value, digits = 6) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toFixed(digits);
}

function countReadableCharacters(text) {
  return [...text.replace(/\s/g, "")].length;
}

function currentArticleText() {
  return el.articleInput.value;
}

function currentTimestamp() {
  return new Date().toISOString().replace(/[:.]/g, "-");
}

function updateDashboard() {
  el.metricWords.textContent = String(countReadableCharacters(currentArticleText()));
  el.metricVerifications.textContent = String(state.continuousResults.length);
  el.metricKeystrokes.textContent = String(state.globalRecords.length);

  if (state.submitStarted) {
    return;
  }

  const pending = state.continuousPromises.length - state.continuousResults.length;
  if (pending > 0) {
    setMessage(el.writingStatus, `分析處理中：尚有 ${pending} 個區段等待處理。`);
  } else if (state.globalRecords.length >= CONTINUOUS_WINDOW_SIZE) {
    setMessage(el.writingStatus, "打字行為動態分析引擎已就緒。", "ok");
  } else {
    setMessage(el.writingStatus, "請持續輸入，系統正持續建立打字行為樣本。");
  }
}

async function apiRequest(path, payload) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);

  try {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const message = data.error || `${path} failed with HTTP ${response.status}`;
      throw new Error(message);
    }
    return data;
  } finally {
    window.clearTimeout(timeout);
  }
}

function getZhuyinStage(event) {
  if (event.code === "Space" && state.isComposing) {
    return "candidate";
  }
  if (state.isComposing) {
    return "composing";
  }
  if (event.key && event.key.length === 1) {
    return "input";
  }
  return "control";
}

function resolveLetter(event) {
  if (event.code === "Space") {
    return " ";
  }
  if (event.key === "Enter") {
    return "ENTER";
  }
  if (event.key === "Tab") {
    return "TAB";
  }
  if (event.key === "Backspace") {
    return "BACKSPACE";
  }
  if (event.key === "Delete") {
    return "DELETE";
  }
  return event.key || event.code || "";
}

function copyRecordForSection(record, sectionId) {
  return {
    ...record,
    TEST_SECTION_ID: sectionId,
    SENTENCE: "FREE_TEXT",
    USER_INPUT: "",
  };
}

function startContinuousVerification(records) {
  const chunkIndex = ++state.continuousChunkIndex;
  const sectionId = `continuous_${String(chunkIndex).padStart(4, "0")}`;
  const chunkRecords = records.map((record) => copyRecordForSection(record, sectionId));
  const firstId = chunkRecords[0]?.KEYSTROKE_ID || "";
  const lastId = chunkRecords[chunkRecords.length - 1]?.KEYSTROKE_ID || "";

  const promise = apiRequest("/api/verify", {
    user_id: state.participantId,
    language: LANGUAGE,
    records: chunkRecords,
    verification_mode: "continuous",
    matching_strategy: "mean_file",
    save_result: false,
  })
    .then((data) => {
      state.continuousResults.push({
        chunk_index: chunkIndex,
        section_id: sectionId,
        record_count: chunkRecords.length,
        record_start: firstId,
        record_end: lastId,
        result: data.result,
      });
    })
    .catch((error) => {
      state.continuousResults.push({
        chunk_index: chunkIndex,
        section_id: sectionId,
        record_count: chunkRecords.length,
        record_start: firstId,
        record_end: lastId,
        error: error.message,
      });
    })
    .finally(updateDashboard);

  state.continuousPromises.push(promise);
  updateDashboard();
}

function maybeRunContinuousVerification() {
  while (state.continuousBuffer.length >= CONTINUOUS_WINDOW_SIZE) {
    const chunk = state.continuousBuffer.splice(0, CONTINUOUS_WINDOW_SIZE);
    startContinuousVerification(chunk);
  }
}

function finishRecord(record, releaseTime = performance.now()) {
  record.RELEASE_TIME = releaseTime.toFixed(3);
  state.globalRecords.push(record);
  state.continuousBuffer.push(record);
  maybeRunContinuousVerification();
  updateDashboard();
}

function releaseDanglingKeys() {
  const releaseTime = performance.now();
  for (const record of state.unresolvedKeys.values()) {
    finishRecord(record, releaseTime);
  }
  state.unresolvedKeys.clear();
}

function handleKeyDown(event) {
  if (!state.participantId || state.submitStarted) {
    return;
  }
  if (event.repeat || !Object.prototype.hasOwnProperty.call(keyCodeMap, event.code)) {
    return;
  }

  const record = {
    PARTICIPANT_ID: state.participantId,
    TEST_SECTION_ID: "free_text",
    SENTENCE: "FREE_TEXT",
    USER_INPUT: "",
    KEYSTROKE_ID: `FT_${String(++state.keyCounter).padStart(6, "0")}`,
    PRESS_TIME: performance.now().toFixed(3),
    RELEASE_TIME: "",
    LETTER: resolveLetter(event),
    KEYCODE: keyCodeMap[event.code],
    ZHUYIN_STAGE: getZhuyinStage(event),
    IME_STAGE: state.compositionStage,
    COMPOSING_DATA: state.compositionData,
    COMPOSING_SEQ: state.composeSeq,
  };

  state.unresolvedKeys.set(event.code, record);
}

function handleKeyUp(event) {
  const record = state.unresolvedKeys.get(event.code);
  if (!record) {
    return;
  }
  state.unresolvedKeys.delete(event.code);
  finishRecord(record, performance.now());
}

function handleCompositionStart(event) {
  state.isComposing = true;
  state.compositionStage = "start";
  state.compositionData = event.data || "";
  state.composeSeq += 1;
}

function handleCompositionUpdate(event) {
  state.isComposing = true;
  state.compositionStage = "update";
  state.compositionData = event.data || "";
}

function handleCompositionEnd(event) {
  state.isComposing = false;
  state.compositionStage = "end";
  state.compositionData = event.data || "";
}

function chunkRecordsForFinalVerification() {
  if (state.globalRecords.length === 0) {
    return [];
  }

  const sections = [];
  let sectionNumber = 0;
  for (let index = 0; index < state.globalRecords.length; index += CONTINUOUS_WINDOW_SIZE) {
    sectionNumber += 1;
    const sectionId = `final_${String(sectionNumber).padStart(4, "0")}`;
    const section = state.globalRecords
      .slice(index, index + CONTINUOUS_WINDOW_SIZE)
      .map((record) => copyRecordForSection(record, sectionId));
    sections.push(...section);
  }
  return sections;
}

async function runFinalVerification() {
  const records = chunkRecordsForFinalVerification();
  if (records.length === 0) {
    throw new Error("沒有足夠的鍵盤資料可以驗證。");
  }

  const data = await apiRequest("/api/verify", {
    user_id: state.participantId,
    language: LANGUAGE,
    records,
    verification_mode: "final",
    matching_strategy: "mean_file",
    save_result: false,
  });

  state.finalResult = data.result;
  return data.result;
}

async function saveFreeTextSession() {
  const payload = {
    user_id: state.participantId,
    language: LANGUAGE,
    session_id: state.sessionId,
    article: currentArticleText(),
    article_character_count: countReadableCharacters(currentArticleText()),
    keystroke_count: state.globalRecords.length,
    keystroke_records: state.globalRecords.map((record) => ({ ...record })),
    continuous_window_size: CONTINUOUS_WINDOW_SIZE,
    continuous_results: [...state.continuousResults].sort((a, b) => a.chunk_index - b.chunk_index),
    final_result: state.finalResult,
  };

  const data = await apiRequest("/api/free-text-session", payload);
  state.sessionResultPath = data.result_path || "";
  state.sessionKeystrokePath = data.keystroke_tsv_path || "";
  return data;
}

function renderContinuousResults() {
  const successful = state.continuousResults.filter((item) => item.result);
  const passCount = successful.filter((item) => item.result.is_genuine).length;
  const failCount = successful.filter((item) => !item.result.is_genuine).length;

  el.continuousTotal.textContent = String(successful.length);
  el.continuousPass.textContent = String(passCount);
  el.continuousFail.textContent = String(failCount);
  el.continuousList.innerHTML = "";

  if (state.continuousResults.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "本次寫作未累積足夠的打字事件，因此沒有持續性驗證紀錄。";
    el.continuousList.appendChild(empty);
    return;
  }

  const sorted = [...state.continuousResults].sort((a, b) => a.chunk_index - b.chunk_index);
  for (const item of sorted) {
    const row = document.createElement("div");
    row.className = "result-row";

    const label = document.createElement("div");
    label.textContent = `#${item.chunk_index}`;

    const status = document.createElement("div");
    if (item.result) {
      const badge = document.createElement("span");
      badge.className = `badge ${item.result.is_genuine ? "badge-ok" : "badge-fail"}`;
      badge.textContent = item.result.is_genuine ? "符合本人" : "特徵異常";
      status.appendChild(badge);
    } else {
      const badge = document.createElement("span");
      badge.className = "badge badge-warn";
      badge.textContent = "分析錯誤";
      status.appendChild(badge);
    }

    const score = document.createElement("div");
    score.textContent = item.result
      ? `score ${formatNumber(item.result.score)}`
      : item.error || "unknown error";

    const threshold = document.createElement("div");
    threshold.textContent = item.result
      ? `threshold ${formatNumber(item.result.threshold)} (${item.result.threshold_source || "-"})`
      : `records ${item.record_count}`;

    row.append(label, status, score, threshold);
    el.continuousList.appendChild(row);
  }
}

function renderFinalResult() {
  const result = state.finalResult;
  if (!result) {
    el.finalGenuine.textContent = "-";
    el.finalScore.textContent = "-";
    el.finalThreshold.textContent = "-";
    el.finalSource.textContent = "-";
    return;
  }

  el.finalGenuine.textContent = result.is_genuine ? "符合本人" : "特徵異常";
  el.finalScore.textContent = formatNumber(result.score);
  el.finalThreshold.textContent = formatNumber(result.threshold);
  el.finalSource.textContent = result.threshold_source || "-";
}

function renderResults() {
  renderContinuousResults();
  renderFinalResult();
}

function buildExportRecords() {
  const article = currentArticleText();
  return state.globalRecords.map((record) => ({
    ...record,
    USER_INPUT: article,
  }));
}

function recordsToTsv(records) {
  const headers = [
    "PARTICIPANT_ID",
    "TEST_SECTION_ID",
    "SENTENCE",
    "USER_INPUT",
    "KEYSTROKE_ID",
    "PRESS_TIME",
    "RELEASE_TIME",
    "LETTER",
    "KEYCODE",
    "ZHUYIN_STAGE",
    "IME_STAGE",
    "COMPOSING_DATA",
    "COMPOSING_SEQ",
  ];

  const escapeCell = (value) => {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value).replace(/\t/g, " ").replace(/\r?\n/g, "\\n");
  };

  const rows = records.map((record) => headers.map((header) => escapeCell(record[header])).join("\t"));
  return [headers.join("\t"), ...rows].join("\n");
}

function downloadBlob(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function downloadKeystrokes() {
  const records = buildExportRecords();
  const tsv = recordsToTsv(records);
  const filename = `free_text_keystrokes_ZH_${state.participantId}_${currentTimestamp()}.tsv`;
  downloadBlob(filename, tsv, "text/tab-separated-values;charset=utf-8");
}

function downloadResults() {
  const payload = {
    participant_id: state.participantId,
    language: LANGUAGE,
    session_id: state.sessionId,
    generated_at: new Date().toISOString(),
    article: currentArticleText(),
    keystroke_count: state.globalRecords.length,
    continuous_window_size: CONTINUOUS_WINDOW_SIZE,
    continuous_results: [...state.continuousResults].sort((a, b) => a.chunk_index - b.chunk_index),
    final_result: state.finalResult,
    server_result_path: state.sessionResultPath,
    server_keystroke_tsv_path: state.sessionKeystrokePath,
  };
  const filename = `free_text_results_ZH_${state.participantId}_${currentTimestamp()}.json`;
  downloadBlob(filename, JSON.stringify(payload, null, 2), "application/json;charset=utf-8");
}

async function login() {
  const userId = el.participantId.value.trim();
  if (!userId) {
    setMessage(el.loginMessage, "請先輸入已註冊的帳號 ID。", "error");
    return;
  }

  el.btnLogin.disabled = true;
  setMessage(el.loginMessage, "正在讀取註冊資料與快取特徵...");

  try {
    const data = await apiRequest("/api/login", {
      user_id: userId,
      language: LANGUAGE,
    });

    state.participantId = data.user_id || userId;
    state.sessionId = `free_text_${Date.now()}`;
    state.baselineInfo = data;
    el.writerName.textContent = state.participantId;
    setMessage(el.loginMessage, "註冊資料確認完成。", "ok");
    setScreen("writing");
    updateDashboard();
    window.setTimeout(() => el.articleInput.focus(), 80);
  } catch (error) {
    if (error.message.includes("No baseline TSV") || error.message.includes("not found") || error.message.includes("404")) {
      showConfirmRegisterModal(userId);
      setMessage(el.loginMessage, "該帳號尚未建立打字動態特徵基準，請點擊提示框以進行首次錄製。", "error");
    } else {
      setMessage(el.loginMessage, error.message, "error");
    }
  } finally {
    el.btnLogin.disabled = false;
  }
}

async function submitArticle() {
  if (state.submitStarted) {
    return;
  }

  releaseDanglingKeys();
  state.submitStarted = true;
  el.btnSubmit.disabled = true;
  setMessage(el.writingStatus, "正在完成背景驗證並執行最後總結驗證...");

  try {
    await Promise.allSettled(state.continuousPromises);
    await runFinalVerification();
    await saveFreeTextSession();
    renderResults();
    setScreen("result");
  } catch (error) {
    state.submitStarted = false;
    el.btnSubmit.disabled = false;
    setMessage(el.writingStatus, error.message, "error");
  }
}

function bindEvents() {
  el.btnLogin.addEventListener("click", login);
  el.participantId.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      login();
    }
  });

  el.articleInput.addEventListener("keydown", handleKeyDown);
  el.articleInput.addEventListener("keyup", handleKeyUp);
  el.articleInput.addEventListener("input", updateDashboard);
  el.articleInput.addEventListener("blur", releaseDanglingKeys);
  el.articleInput.addEventListener("compositionstart", handleCompositionStart);
  el.articleInput.addEventListener("compositionupdate", handleCompositionUpdate);
  el.articleInput.addEventListener("compositionend", handleCompositionEnd);

  el.btnSubmit.addEventListener("click", submitArticle);
  el.btnDownloadKeystrokes.addEventListener("click", downloadKeystrokes);
  el.btnDownloadResults.addEventListener("click", downloadResults);
}



// ============================================================================
// 整合打字註冊流程相關邏輯 (完全移植自原 keystroke_logger 專案，風格融入本系統)
// ============================================================================

// 註冊用中文題庫
const REG_SENTENCES_ZH = [
    { "promptText": "西北風吹過那片寬闊的紅鋼材", "promptKeys": "vu 1o3z/ tjo eji4s84qu04dj0 dji42k7cj/6e; h96" },
    { "promptText": "原本打算買些蕎麥與肥嫩草魚", "promptKeys": "m061p3283nj04a93vu, ful6a94m3zo6sp4hl3m6" },
    { "promptText": "這群孩子在草叢找尋彩色皇冠", "promptKeys": "5k4fmp6c96y7y94hl3hj/65l3vmp6h93nk4cj;6ej04" },
    { "promptText": "瑞雪紛飛讓他兒子頭變得銀白", "promptKeys": "bjo4vm,3zp zo b;4w8 -6y7w.61u042k6up6196" },
    { "promptText": "奶奶用舊布料縫製了一件洋裝", "promptKeys": "s93s93m/4ru.41j4xul4z/654xk7u6ru04u;65j; " },
    { "promptText": "空瓶子投進深水溝發出沉悶響聲", "promptKeys": "dj/ qu/6y7w.6rup4gp gjo3e. z8 tj tp6ap vu;3g/ " },
    { "promptText": "太陽從東邊升起照亮萬物復甦", "promptKeys": "w94u;6hj/62j/ 1u0 g/ fu35l4xu;4j04j4zj4nj " },
    { "promptText": "翁先生喜歡在午後閱覽法律書籍", "promptKeys": "j/ vu0 g/ vu3cj0 y94j3c.4m,4x03z83xm4gj ru6" },
    { "promptText": "雖然環境艱辛我們更絕不退縮", "promptKeys": "njo b06ru0 vup ji3pa7e/4mr,61j4wjo4nji " },
    { "promptText": "那位旅客背著行囊跨越了邊境", "promptKeys": "s84jo4xm3dk41o 5k7vu/6s;6dj94m,4xk71u0 ru/4" },
    { "promptText": "噴水池旁邊有隻小貓在舔肉球", "promptKeys": "qp gjo3t6q;61u0 u.35 vul3al y94wu03b.4fu.6" },
    { "promptText": "清晨露珠輕輕滑落在翠綠葉片", "promptKeys": "fu/ tp6xj45j fu/ fu/ cj86xji4y94hjo4xm4u,4qu04" },
    { "promptText": "這台電腦可以快速處理複雜運算", "promptKeys": "5k4w962u04sl3dk3u3dj94nj4tj3xu3zj4y86mp4nj04" },
    { "promptText": "牆角舊鐘擺發出滴答滴答的節奏", "promptKeys": "fu/6rul3ru.45j/ 193z8 tj 2u 28 2u 28 2k7ru,6y.4" },
    { "promptText": "他勇敢去追求夢想並收穫成功", "promptKeys": "w8 m/3e03fm45jo fu.6a/4vu;31u/4g. cji4t/6ej/ " }
];

// 註冊用英文題庫
const REG_SENTENCES_EN = [
    { "promptText": "The quick brown fox jumps over the lazy dog." },
    { "promptText": "Pack my box with five dozen liquor jugs." },
    { "promptText": "How vexingly quick daft zebras jump!" },
    { "promptText": "Sphinx of black quartz, judge my vow." },
    { "promptText": "The five boxing wizards jump quickly." },
    { "promptText": "Jackdaws love my big sphinx of quartz." },
    { "promptText": "Crazy Fredrick bought many very exquisite opal jewels." },
    { "promptText": "We promptly judged antique ivory buckles for the next prize." },
    { "promptText": "A mad boxer shot a quick, gloved jab to the jaw of his dizzy opponent." },
    { "promptText": "Jaded zombies acted quaintly but kept driving their oxen forward." },
    { "promptText": "The job requires extra pluck and zeal from every young wage earner." },
    { "promptText": "A quart jar of oil mixed with zinc oxide makes a very bright paint." },
    { "promptText": "Foxy parsons quiz and cajole the lovelorn monk." },
    { "promptText": "Sixty zippers were quickly picked from the woven jute bag." },
    { "promptText": "Big july earthquakes confound zany experimental vow." },
    { "promptText": "I quickly explained that many big jobs involve few hazards." }
];

// 註冊用鍵盤對應表 (包含 Shift 與字元解析)
const regKeyCodeMap = {
    'KeyA': { code: 65, char: 'a', shiftChar: 'A' },
    'KeyB': { code: 66, char: 'b', shiftChar: 'B' },
    'KeyC': { code: 67, char: 'c', shiftChar: 'C' },
    'KeyD': { code: 68, char: 'd', shiftChar: 'D' },
    'KeyE': { code: 69, char: 'e', shiftChar: 'E' },
    'KeyF': { code: 70, char: 'f', shiftChar: 'F' },
    'KeyG': { code: 71, char: 'g', shiftChar: 'G' },
    'KeyH': { code: 72, char: 'h', shiftChar: 'H' },
    'KeyI': { code: 73, char: 'i', shiftChar: 'I' },
    'KeyJ': { code: 74, char: 'j', shiftChar: 'J' },
    'KeyK': { code: 75, char: 'k', shiftChar: 'K' },
    'KeyL': { code: 76, char: 'l', shiftChar: 'L' },
    'KeyM': { code: 77, char: 'm', shiftChar: 'M' },
    'KeyN': { code: 78, char: 'n', shiftChar: 'N' },
    'KeyO': { code: 79, char: 'o', shiftChar: 'O' },
    'KeyP': { code: 80, char: 'p', shiftChar: 'P' },
    'KeyQ': { code: 81, char: 'q', shiftChar: 'Q' },
    'KeyR': { code: 82, char: 'r', shiftChar: 'R' },
    'KeyS': { code: 83, char: 's', shiftChar: 'S' },
    'KeyT': { code: 84, char: 't', shiftChar: 'T' },
    'KeyU': { code: 85, char: 'u', shiftChar: 'U' },
    'KeyV': { code: 86, char: 'v', shiftChar: 'V' },
    'KeyW': { code: 87, char: 'w', shiftChar: 'W' },
    'KeyX': { code: 88, char: 'x', shiftChar: 'X' },
    'KeyY': { code: 89, char: 'y', shiftChar: 'Y' },
    'KeyZ': { code: 90, char: 'z', shiftChar: 'Z' },
    'Digit1': { code: 49, char: '1', shiftChar: '!' },
    'Digit2': { code: 50, char: '2', shiftChar: '@' },
    'Digit3': { code: 51, char: '3', shiftChar: '#' },
    'Digit4': { code: 52, char: '4', shiftChar: '$' },
    'Digit5': { code: 53, char: '5', shiftChar: '%' },
    'Digit6': { code: 54, char: '6', shiftChar: '^' },
    'Digit7': { code: 55, char: '7', shiftChar: '&' },
    'Digit8': { code: 56, char: '8', shiftChar: '*' },
    'Digit9': { code: 57, char: '9', shiftChar: '(' },
    'Digit0': { code: 48, char: '0', shiftChar: ')' },
    'Minus': { code: 189, char: '-', shiftChar: '_' },
    'Equal': { code: 187, char: '=', shiftChar: '+' },
    'BracketLeft': { code: 219, char: '[', shiftChar: '{' },
    'BracketRight': { code: 221, char: ']', shiftChar: '}' },
    'Backslash': { code: 220, char: '\\', shiftChar: '|' },
    'Semicolon': { code: 186, char: ';', shiftChar: ':' },
    'Quote': { code: 222, char: "'", shiftChar: '"' },
    'Comma': { code: 188, char: ',', shiftChar: '<' },
    'Period': { code: 190, char: '.', shiftChar: '>' },
    'Slash': { code: 191, char: '/', shiftChar: '?' },
    'Backquote': { code: 192, char: '`', shiftChar: '~' },
    'Space': { code: 32, char: ' ', shiftChar: ' ' },
    'ArrowLeft': { code: 37, char: 'LEFT', shiftChar: 'LEFT' },
    'ArrowUp': { code: 38, char: 'UP', shiftChar: 'UP' },
    'ArrowRight': { code: 39, char: 'RIGHT', shiftChar: 'RIGHT' },
    'ArrowDown': { code: 40, char: 'DOWN', shiftChar: 'DOWN' },
    'Backspace': { code: 8, char: 'BKSP', shiftChar: 'BKSP' },
    'Enter': { code: 13, char: 'ENTER', shiftChar: 'ENTER' },
    'ShiftLeft': { code: 16, char: 'SHIFT', shiftChar: 'SHIFT' },
    'ShiftRight': { code: 16, char: 'SHIFT', shiftChar: 'SHIFT' },
    'CapsLock': { code: 20, char: 'CAPSLOCK', shiftChar: 'CAPSLOCK' },
    'Tab': { code: 9, char: 'TAB', shiftChar: 'TAB' }
};

// 註冊流程狀態
const regState = {
    selectedLanguage: 'zh',        // 'zh' = 中文, 'en' = 英文
    participantId: '',             // 註冊學號/帳號
    currentSentenceIndex: 0,       // 目前題目索引
    currentTestSectionId: '',      // 目前測試段落 ID
    globalRecords: [],             // 所有已完成記錄的陣列
    currentSectionRecords: [],     // 目前句子的按鍵記錄
    unresolvedKeypresses: {},      // 尚未釋放的按鍵
    isComposing: false,            // 是否正在組字（IME 狀態）
    currentImeStage: 'none',       // IME 階段
    currentCompositionData: '',    // 組字中的資料
    currentComposeSeq: 0,          // 組字序號
    typingStartTime: null          // 開始打字時間
};

// 註冊用 DOM 元素
const regEl = {
    screen: document.getElementById('register-screen'),
    regLoginSec: document.getElementById('reg-login-section'),
    regTestSec: document.getElementById('reg-test-section'),
    regResultSec: document.getElementById('reg-result-section'),
    
    studentIdInput: document.getElementById('reg-student-id'),
    btnStart: document.getElementById('btn-reg-start'),
    btnBackHome: document.getElementById('btn-reg-back-home'),
    
    langToggle: document.getElementById('reg-lang-toggle'),
    btnLangZh: document.getElementById('btn-reg-lang-zh'),
    btnLangEn: document.getElementById('btn-reg-lang-en'),
    
    progressText: document.getElementById('reg-progress-text'),
    langBadge: document.getElementById('reg-lang-badge'),
    displayId: document.getElementById('reg-display-id'),
    sentenceDisplay: document.getElementById('reg-sentence-display'),
    testInput: document.getElementById('reg-test-input'),
    wpmValue: document.getElementById('reg-wpm-value'),
    warningMsg: document.getElementById('reg-warning-msg'),
    
    btnFinishDone: document.getElementById('btn-reg-finish-done'),
    btnGoRegister: document.getElementById('btn-go-register')
};

// 切換註冊流程的子區塊
function showRegSection(sectionName) {
    const sections = {
        login: regEl.regLoginSec,
        test: regEl.regTestSec,
        result: regEl.regResultSec
    };
    Object.values(sections).forEach(sec => { 
        if (sec) {
            sec.style.display = 'none'; 
            sec.classList.remove('active'); 
        }
    });
    if (sections[sectionName]) {
        sections[sectionName].style.display = 'block';
        sections[sectionName].classList.add('active');
    }
}

// 進入註冊畫面
function goToRegister(prefilledUserId = "") {
    // 重置註冊狀態
    regState.participantId = prefilledUserId;
    regState.currentSentenceIndex = 0;
    regState.currentTestSectionId = '';
    regState.globalRecords = [];
    regState.currentSectionRecords = [];
    regState.unresolvedKeypresses = {};
    regState.isComposing = false;
    regState.currentImeStage = 'none';
    regState.currentCompositionData = '';
    regState.currentComposeSeq = 0;
    regState.typingStartTime = null;

    regEl.studentIdInput.value = prefilledUserId;
    
    setScreen("register"); // 切換到註冊 screen
    showRegSection("login"); // 切換到註冊的第一個輸入階段

    setTimeout(() => { regEl.studentIdInput.focus(); }, 120);
}

// 註冊 Modal 詢問
function showConfirmRegisterModal(userId) {
    const overlay = document.createElement("div");
    overlay.className = "confirm-modal-overlay";
    overlay.innerHTML = `
      <div class="confirm-modal">
        <h3>未偵測到打字特徵基準</h3>
        <p>系統中找不到帳號 "<strong>${userId}</strong>" 的打字特徵資料。<br>是否現在開始為此帳號進行打字特徵錄製？</p>
        <div class="confirm-modal-actions">
          <button id="btn-modal-cancel" class="btn btn-secondary">取消</button>
          <button id="btn-modal-confirm" class="btn btn-primary">開始錄製</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    
    document.getElementById("btn-modal-cancel").onclick = () => {
      overlay.remove();
    };
    
    document.getElementById("btn-modal-confirm").onclick = () => {
      overlay.remove();
      goToRegister(userId);
    };
}

// 產生註冊的隨機 ID
function regGenerate7DigitID() {
    return Math.floor(1000000 + Math.random() * 9000000).toString();
}

// 產生註冊的鍵盤 ID
function regGenerate8DigitID() {
    return Math.floor(10000000 + Math.random() * 90000000).toString();
}

// 語言與字元解析
function regResolveLetter(e) {
    const mapping = regKeyCodeMap[e.code];
    if (!mapping) return e.key;
    const caps = e.getModifierState ? e.getModifierState('CapsLock') : false;
    const shift = e.shiftKey;
    const isLetter = mapping.code >= 65 && mapping.code <= 90;
    if (isLetter) {
        if ((caps && !shift) || (!caps && shift)) {
            return mapping.shiftChar;
        } else {
            return mapping.char;
        }
    } else {
        return shift ? mapping.shiftChar : mapping.char;
    }
}

function regGetZhuyinStage(code) {
    const consonants = ['Digit1', 'KeyQ', 'KeyA', 'KeyZ', 'Digit2', 'KeyW', 'KeyS', 'KeyX', 'KeyE', 'KeyD', 'KeyC', 'KeyR', 'KeyF', 'KeyV', 'Digit5', 'KeyT', 'KeyG', 'KeyB', 'KeyY', 'KeyH', 'KeyN'];
    const medials = ['KeyU', 'KeyJ', 'KeyM'];
    const vowels = ['Digit8', 'KeyI', 'KeyK', 'Comma', 'Digit9', 'KeyO', 'KeyL', 'Period', 'Digit0', 'KeyP', 'Semicolon', 'Slash', 'Minus'];
    const tones = ['Digit6', 'Digit3', 'Digit4', 'Digit7', 'Space'];
    
    if (consonants.includes(code)) return '聲母';
    if (medials.includes(code)) return '介音';
    if (vowels.includes(code)) return '韻母';
    if (tones.includes(code)) return '聲調/選字';
    return '其他';
}

function regGetCurrentSentences() {
    return regState.selectedLanguage === 'zh' ? REG_SENTENCES_ZH : REG_SENTENCES_EN;
}

// 載入註冊句子
function regLoadSentence(index) {
    const sentences = regGetCurrentSentences();
    
    regState.currentTestSectionId = regGenerate7DigitID();
    regState.currentSectionRecords = [];
    regState.unresolvedKeypresses = {};
    regState.isComposing = false;
    
    if (regState.selectedLanguage === 'zh') {
        regState.currentImeStage = 'none';
        regState.currentCompositionData = '';
        regState.currentComposeSeq = 0;
    }
    
    regEl.sentenceDisplay.textContent = sentences[index].promptText;
    regEl.progressText.textContent = `${index + 1} / ${sentences.length}`;
    regEl.testInput.value = '';
    regEl.warningMsg.style.display = 'none';
    
    regState.typingStartTime = null;
    regEl.wpmValue.textContent = '0';
    
    setTimeout(() => { regEl.testInput.focus(); }, 80);
}

// 提交單句
function regSubmitCurrentSentence(userInput) {
    const sentences = regGetCurrentSentences();
    const sentenceText = sentences[regState.currentSentenceIndex].promptText;
    
    const now = Date.now();
    for (let code in regState.unresolvedKeypresses) {
        const r = regState.unresolvedKeypresses[code];
        r.RELEASE_TIME = now;
        regState.currentSectionRecords.push(r);
    }
    regState.unresolvedKeypresses = {};

    regState.currentSectionRecords.forEach(rec => {
        if (regState.selectedLanguage === 'zh') {
            regState.globalRecords.push({
                PARTICIPANT_ID: regState.participantId,
                TEST_SECTION_ID: regState.currentTestSectionId,
                SENTENCE: sentenceText,
                USER_INPUT: userInput,
                KEYSTROKE_ID: rec.KEYSTROKE_ID,
                PRESS_TIME: rec.PRESS_TIME,
                RELEASE_TIME: rec.RELEASE_TIME,
                LETTER: rec.LETTER,
                KEYCODE: rec.KEYCODE,
                ZHUYIN_STAGE: rec.ZHUYIN_STAGE || '其他',
                IME_STAGE: rec.IME_STAGE || 'none',
                COMPOSING_DATA: rec.COMPOSING_DATA || '',
                COMPOSING_SEQ: rec.COMPOSING_SEQ || 0
            });
        } else {
            regState.globalRecords.push({
                PARTICIPANT_ID: regState.participantId,
                TEST_SECTION_ID: regState.currentTestSectionId,
                SENTENCE: sentenceText,
                USER_INPUT: userInput,
                KEYSTROKE_ID: rec.KEYSTROKE_ID,
                PRESS_TIME: rec.PRESS_TIME,
                RELEASE_TIME: rec.RELEASE_TIME,
                LETTER: rec.LETTER,
                KEYCODE: rec.KEYCODE
            });
        }
    });
    
    regState.currentSentenceIndex++;
    if (regState.currentSentenceIndex < sentences.length) {
        regLoadSentence(regState.currentSentenceIndex);
    } else {
        regFinishTest();
    }
}

// 提交註冊資料到後端存檔 (直接發送 JSON 數據)
async function regFinishTest() {
    try {
        regEl.testInput.disabled = true;
        // 上傳到後端註冊 API，直接傳送 keystrokes 陣列
        await apiRequest("/api/register", {
            user_id: regState.participantId,
            language: regState.selectedLanguage.toUpperCase(),
            keystrokes: regState.globalRecords
        });
        
        showRegSection('result');
    } catch (error) {
        alert("註冊基準檔案上傳失敗：" + error.message);
    } finally {
        regEl.testInput.disabled = false;
    }
}

// 綁定註冊流程所有事件
function bindRegEvents() {
    // 前往註冊按鈕
    regEl.btnGoRegister.addEventListener("click", () => {
        goToRegister("");
    });

    // 語言選擇切換 (ZH)
    regEl.btnLangZh.addEventListener('click', () => {
        regState.selectedLanguage = 'zh';
        regEl.btnLangZh.classList.add('active');
        regEl.btnLangEn.classList.remove('active');
        regEl.btnLangZh.style.background = 'var(--surface-muted)';
        regEl.btnLangZh.style.color = 'var(--ink)';
        regEl.btnLangEn.style.background = '#fff';
        regEl.btnLangEn.style.color = 'var(--muted)';
    });
    
    // 語言選擇切換 (EN)
    regEl.btnLangEn.addEventListener('click', () => {
        regState.selectedLanguage = 'en';
        regEl.btnLangEn.classList.add('active');
        regEl.btnLangZh.classList.remove('active');
        regEl.btnLangEn.style.background = 'var(--surface-muted)';
        regEl.btnLangEn.style.color = 'var(--ink)';
        regEl.btnLangZh.style.background = '#fff';
        regEl.btnLangZh.style.color = 'var(--muted)';
    });

    // 返回首頁按鈕
    regEl.btnBackHome.addEventListener("click", () => {
        setScreen("login");
    });
    
    // 註冊成功返回首頁按鈕
    regEl.btnFinishDone.addEventListener("click", () => {
        // 將註冊完的帳號填入登入框
        el.participantId.value = regState.participantId;
        setScreen("login");
        setTimeout(() => el.btnLogin.focus(), 50);
    });

    // 開始測驗
    regEl.btnStart.addEventListener('click', () => {
        const id = regEl.studentIdInput.value.trim();
        if (!id) {
            alert('請輸入帳號！');
            return;
        }
        regState.participantId = id;
        regEl.displayId.textContent = `帳號: ${regState.participantId}`;
        regEl.langBadge.textContent = regState.selectedLanguage === 'zh' ? '中文' : 'English';
        
        showRegSection('test');
        regLoadSentence(0);
    });
    
    regEl.studentIdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            regEl.btnStart.click();
        }
    });

    // IME 組字攔截
    regEl.testInput.addEventListener('compositionstart', () => {
        regState.isComposing = true;
        if (regState.selectedLanguage === 'zh') {
            regState.currentImeStage = 'start';
            regState.currentComposeSeq = 0;
            regState.currentCompositionData = '';
        }
    });
    
    regEl.testInput.addEventListener('compositionupdate', (e) => {
        if (regState.selectedLanguage === 'zh') {
            regState.currentImeStage = 'composing';
            regState.currentCompositionData = e.data;
        }
    });
    
    regEl.testInput.addEventListener('compositionend', (e) => {
        regState.isComposing = false;
        if (regState.selectedLanguage === 'zh') {
            regState.currentImeStage = 'end';
            regState.currentCompositionData = e.data;
        }
    });

    // WPM 計算
    regEl.testInput.addEventListener('input', () => {
        const currentText = regEl.testInput.value;
        if (currentText.length > 0 && regState.typingStartTime === null) {
            regState.typingStartTime = Date.now();
        }
        
        if (regState.typingStartTime !== null && currentText.length > 0) {
            const elapsedMinutes = (Date.now() - regState.typingStartTime) / 60000;
            if (elapsedMinutes > 0.01) {
                let wpm;
                if (regState.selectedLanguage === 'zh') {
                    wpm = Math.round(currentText.length / elapsedMinutes);
                } else {
                    wpm = Math.round((currentText.length / 5) / elapsedMinutes);
                }
                regEl.wpmValue.textContent = wpm;
            }
        }
        if (currentText.length > 0) {
            regEl.warningMsg.style.display = 'none';
        }
    });

    // 按鍵記錄 (keydown)
    regEl.testInput.addEventListener('keydown', (e) => {
        if (e.code === 'Enter' && !e.isComposing && !regState.isComposing) {
            e.preventDefault();
            const userInput = regEl.testInput.value.trim();
            const sentences = regGetCurrentSentences();
            const expectedText = sentences[regState.currentSentenceIndex].promptText;
            
            if (!userInput || userInput !== expectedText) {
                regEl.warningMsg.style.display = 'block';
                return;
            }
            
            const now = Date.now();
            if (regState.selectedLanguage === 'zh') {
                regState.currentSectionRecords.push({
                    KEYSTROKE_ID: regGenerate8DigitID(),
                    PRESS_TIME: now,
                    RELEASE_TIME: now + 20,
                    LETTER: 'ENTER',
                    KEYCODE: 13,
                    ZHUYIN_STAGE: '其他',
                    IME_STAGE: regState.currentImeStage,
                    COMPOSING_DATA: regState.currentCompositionData,
                    COMPOSING_SEQ: 0
                });
            } else {
                regState.currentSectionRecords.push({
                    KEYSTROKE_ID: regGenerate8DigitID(),
                    PRESS_TIME: now,
                    RELEASE_TIME: now + 20,
                    LETTER: 'ENTER',
                    KEYCODE: 13
                });
            }
            regSubmitCurrentSentence(userInput);
            return;
        }

        if (!regKeyCodeMap[e.code]) return;
        if (e.repeat) return;
        
        if (regState.selectedLanguage === 'zh') {
            regState.unresolvedKeypresses[e.code] = {
                KEYSTROKE_ID: regGenerate8DigitID(),
                PRESS_TIME: Date.now(),
                LETTER: regResolveLetter(e),
                KEYCODE: regKeyCodeMap[e.code].code,
                ZHUYIN_STAGE: regGetZhuyinStage(e.code),
                IME_STAGE: regState.currentImeStage,
                COMPOSING_DATA: regState.currentCompositionData,
                COMPOSING_SEQ: regState.isComposing ? ++regState.currentComposeSeq : 0
            };
        } else {
            regState.unresolvedKeypresses[e.code] = {
                KEYSTROKE_ID: regGenerate8DigitID(),
                PRESS_TIME: Date.now(),
                LETTER: regResolveLetter(e),
                KEYCODE: regKeyCodeMap[e.code].code
            };
        }
    });

    // 按鍵記錄 (keyup)
    regEl.testInput.addEventListener('keyup', (e) => {
        if (!regKeyCodeMap[e.code]) return;
        const record = regState.unresolvedKeypresses[e.code];
        if (record) {
            record.RELEASE_TIME = Date.now();
            regState.currentSectionRecords.push(record);
            delete regState.unresolvedKeypresses[e.code];
        }
    });

    // 失焦處理
    regEl.testInput.addEventListener('blur', () => {
        const now = Date.now();
        for (let code in regState.unresolvedKeypresses) {
            const r = regState.unresolvedKeypresses[code];
            r.RELEASE_TIME = now;
            regState.currentSectionRecords.push(r);
        }
        regState.unresolvedKeypresses = {};
    });
}

// 在所有變數與函數宣告完畢後，進行初始化呼叫
bindEvents();
bindRegEvents();
updateDashboard();


