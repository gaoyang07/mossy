const chatWindow = document.getElementById("chat-window");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const muteBtn = document.getElementById("mute-btn");
const latencyEl = document.getElementById("latency");
const expressionsBtn = document.getElementById("expressions-btn");
const expressionStatus = document.getElementById("expression-status");
const cameraBtn = document.getElementById("camera-btn");
const cameraStatus = document.getElementById("camera-status");
const cameraPanel = document.getElementById("camera-panel");
const cameraPreview = document.getElementById("camera-preview");
const settingsPanel = document.getElementById("settings");
const settingsGroups = document.getElementById("settings-groups");
const settingsNote = document.getElementById("settings-note");
const EXPRESSION_LABELS = {
    happy: "开心轻摆",
    agree: "赞同点头",
    thinking: "思考",
    listening: "倾听",
    returning: "回到自然姿态",
};

const STATE_LABELS = {
    disconnected: "未连接",
    idle: "待命",
    listening: "正在听…",
    processing: "思考中…",
    speaking: "说话中…",
};

const POSTURE_LABELS = { neutral: "自然", attentive: "专注", relaxed: "放松", curious: "好奇" };
const GESTURES = { happy: "开心", agree: "赞同", thinking: "思考", listening: "倾听" };
// The two voices the MOSS-Live reference names; an account's own voices are fetched.
const PRESET_VOICES = [
    { id: "6c3152b1-1d4f-4e49-ade3-09f447712fec", name: "暖暖（女声）" },
    { id: "6cc93520-3207-4f7a-bca9-2d560b9c306b", name: "沐沐（男声）" },
];
const SCOPE_NOTES = { reconnect: "会重新连接", restart: "下次启动生效" };
const OPEN_GROUPS = ["voice", "robot"];

let transcriptVersion = -1;
let previewFrame = null;
let polling = false;
let settingsLoaded = false;
let accountVoices = null;
let voiceRow = null;

function renderCamera(data) {
    const camera = data.camera;
    cameraBtn.disabled = !camera || !data.local_access;
    cameraBtn.textContent = camera?.enabled ? "关闭视觉" : "开启视觉";
    cameraStatus.textContent = !camera || !camera.enabled ? "视觉：关闭"
        : camera.error ? "Reachy 摄像头暂无画面，请检查连接及摄像头授权"
        : !camera.available ? "正在等待 Reachy 摄像头画面…"
        : !data.connected ? "摄像头已就绪，等待 MOSS 连接"
        : camera.last_sent_age_s == null || camera.last_sent_age_s > 3
            ? "摄像头已就绪，等待发送画面"
            : `视觉：画面发送中 · ${camera.fps} FPS · 已发送 ${camera.sent_frames} 帧`;
    const visible = camera?.enabled && camera.available && data.local_access;
    cameraPanel.hidden = !visible;
    if (visible && previewFrame !== camera.captured_frames) {
        previewFrame = camera.captured_frames;
        cameraPreview.src = `/camera/frame?frame=${previewFrame}`;
    } else if (!visible) {
        previewFrame = null;
        cameraPreview.removeAttribute("src");
    }
}

function renderTranscript(transcript) {
    const atBottom =
        chatWindow.scrollHeight - chatWindow.scrollTop - chatWindow.clientHeight < 40;

    chatWindow.querySelector(".system").hidden = transcript.length > 0;
    // Reuse the lines already shown: while a reply streams only the last one changes.
    const shown = chatWindow.querySelectorAll(".message:not(.system)");
    transcript.forEach((line, index) => {
        let div = shown[index];
        if (!div) {
            div = document.createElement("div");
            div.appendChild(document.createElement("span")).className = "content";
            chatWindow.appendChild(div);
        }
        const className = `message ${line.role}${line.final ? "" : " partial"}`;
        if (div.className !== className) div.className = className;
        if (div.firstChild.textContent !== line.text) div.firstChild.textContent = line.text;
    });
    for (let index = transcript.length; index < shown.length; index++) shown[index].remove();
    if (atBottom) chatWindow.scrollTop = chatWindow.scrollHeight;
}

function formatDuration(seconds) {
    const minutes = Math.floor(seconds / 60);
    return minutes >= 60
        ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
        : `${minutes} 分 ${seconds % 60} 秒`;
}

function renderStatus(data) {
    const label = STATE_LABELS[data.state] || data.state;
    statusText.textContent = data.error || (data.muted ? `${label}（麦克风已静音）` : label);
    statusDot.className = `status-dot ${data.connected ? data.state : "disconnected"}`;
    muteBtn.textContent = data.muted ? "取消静音" : "静音麦克风";
    // The switches answer only a browser on the machine that runs MOSS.
    const hint = data.local_access ? "" : "请在运行 MOSS 的电脑上操作";
    muteBtn.disabled = !data.local_access;
    muteBtn.title = expressionsBtn.title = cameraBtn.title = hint;
    const expression = data.expressions;
    expressionsBtn.disabled = !expression || !expression.available || !data.local_access;
    expressionsBtn.textContent = expression?.enabled ? "关闭表情动作" : "开启表情动作";
    expressionStatus.textContent = !expression ? "表情动作准备中…"
        : expression.error ? "表情动作暂不可用，语音可继续使用"
        : !expression.enabled ? "表情动作已关闭"
        : `表情动作：${EXPRESSION_LABELS[expression.active] || "待命"}`;

    // Measured by this app: end of the user's speech to the first reply audio
    // arriving, and the WebSocket round trip to MOSS-Live.
    const stats = data.stats || {};
    const parts = [];
    if (stats.reply_ms != null) {
        parts.push(`回复 ${stats.reply_ms} ms（中位 ${stats.reply_ms_p50} ms）`);
    }
    if (stats.rtt_ms != null) parts.push(`往返 ${stats.rtt_ms} ms`);
    if (stats.connected_s != null) parts.push(`已连接 ${formatDuration(stats.connected_s)}`);
    if (stats.late_chunks) parts.push(`语音断续 ${stats.late_chunks} 次`);
    if (data.connected && stats.output_rate) parts.push(`${stats.output_rate / 1000} kHz`);
    latencyEl.textContent = parts.join(" · ");
}

async function poll() {
    // A hidden tab does not need the robot's attention.
    if (polling || document.hidden) return;
    polling = true;
    try {
        const resp = await fetch(`/state?version=${transcriptVersion}`);
        const data = await resp.json();
        renderStatus(data);
        if (data.transcript) {
            renderTranscript(data.transcript);
            transcriptVersion = data.transcript_version;
        }
        renderCamera(data);
        if (data.local_access && !settingsLoaded) loadSettings();
    } catch (e) {
        statusText.textContent = "无法连接到机器人";
        statusDot.className = "status-dot disconnected";
        cameraPanel.hidden = true;
        cameraPreview.removeAttribute("src");
        previewFrame = null;
        latencyEl.textContent = "";
        settingsLoaded = false;
    } finally {
        polling = false;
    }
}

// --- settings ---------------------------------------------------------------

function note(text, kind = "") {
    settingsNote.textContent = text;
    settingsNote.className = `settings-note ${kind}`;
}

async function postJSON(url, body = {}) {
    const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const result = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(result.error || `HTTP ${resp.status}`);
    return result;
}

async function applySetting(field, value) {
    note(`正在应用：${field.label}…`);
    try {
        const result = await postJSON("/api/settings", { [field.key]: value });
        field.value = value;
        note(
            !result.saved.length ? "没有变化"
                : result.restart ? `已保存：${field.label}（下次启动生效）`
                : `已应用并保存：${field.label}`,
            "ok",
        );
    } catch (e) {
        note(`没有应用 ${field.label}：${e.message}`, "error");
        loadSettings();  // Show what is really in effect again.
    }
}

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

function voiceControl(field) {
    const wrapper = element("div", "voice-picker");
    const select = element("select");
    const custom = element("input");
    custom.type = "text";
    custom.placeholder = "粘贴音色 ID 后回车";
    const options = [{ id: "", name: "服务端默认音色" }, ...PRESET_VOICES];
    for (const voice of accountVoices || []) {
        if (!options.some((option) => option.id === voice.id)) {
            options.push({ id: voice.id, name: voice.name || `未命名 ${voice.id.slice(0, 8)}` });
        }
    }
    if (!options.some((option) => option.id === field.value)) {
        options.push({ id: field.value, name: `当前：${field.value}` });
    }
    for (const voice of options) select.appendChild(new Option(voice.name, voice.id));
    select.appendChild(new Option("其他音色 ID…", "__custom__"));
    select.value = field.value;
    custom.hidden = true;
    select.addEventListener("change", () => {
        custom.hidden = select.value !== "__custom__";
        if (custom.hidden) applySetting(field, select.value);
        else custom.focus();
    });
    custom.addEventListener("change", () => {
        if (custom.value.trim()) applySetting(field, custom.value.trim()).then(loadSettings);
    });
    wrapper.append(select, custom);
    return wrapper;
}

function control(field) {
    if (field.key === "MOSS_LIVE_VOICE_ID") return voiceControl(field);
    if (field.kind === "bool") {
        const input = element("input");
        input.type = "checkbox";
        input.checked = field.value;
        input.addEventListener("change", () => applySetting(field, input.checked));
        return input;
    }
    if (field.choices.length && field.kind === "str") {
        const group = element("div", "segmented");
        for (const choice of field.choices) {
            const button = element("button", "", POSTURE_LABELS[choice] || choice);
            button.type = "button";
            button.classList.toggle("active", choice === field.value);
            button.addEventListener("click", async () => {
                await applySetting(field, choice);
                for (const other of group.children) {
                    other.classList.toggle("active", other === button && field.value === choice);
                }
            });
            group.appendChild(button);
        }
        return group;
    }
    if (field.choices.length) {
        const select = element("select");
        for (const choice of field.choices) select.appendChild(new Option(choice, choice));
        select.value = field.value;
        select.addEventListener("change", () => applySetting(field, Number(select.value)));
        return select;
    }
    if (field.kind === "int" || field.kind === "float") {
        const wrapper = element("div", "slider");
        const input = element("input");
        const output = element("output", "", field.value);
        // A port has a range but no step; it reads better as a number box.
        input.type = field.step == null ? "number" : "range";
        input.min = field.minimum;
        input.max = field.maximum;
        input.step = field.step ?? 1;
        input.value = field.value;
        input.addEventListener("input", () => { output.textContent = input.value; });
        input.addEventListener("change", () => applySetting(field, Number(input.value)));
        wrapper.append(input, ...(input.type === "range" ? [output] : []));
        return wrapper;
    }
    const input = element(field.kind === "text" ? "textarea" : "input");
    if (field.kind === "text") {
        input.rows = 3;
        input.maxLength = field.max_length;
    } else {
        input.type = field.kind === "secret" ? "password" : "text";
        input.autocomplete = "off";
    }
    if (field.kind === "secret") {
        input.placeholder = field.is_set ? "已设置；输入新的密钥可替换" : "尚未设置";
    } else {
        input.value = field.value;
    }
    input.addEventListener("change", () => {
        if (field.kind !== "secret" || input.value) applySetting(field, input.value);
    });
    return input;
}

function fieldRow(field, localTimeZone) {
    const row = element("label", `field ${field.kind}`);
    const head = element("div", "field-head");
    head.appendChild(element("span", "field-label", field.label));
    const scope = field.pinned ? "由环境变量固定" : SCOPE_NOTES[field.scope];
    if (scope) head.appendChild(element("span", "field-scope", scope));
    const input = control(field);
    if (field.pinned) {
        for (const node of [input, ...input.querySelectorAll("input, select, button, textarea")]) {
            node.disabled = true;
        }
    }
    row.append(head, input);
    let help = field.help;
    if (field.key === "MOSS_TIME_ZONE" && field.value === "auto") {
        help += localTimeZone ? `（本机：${localTimeZone}）` : "（未能识别本机时区）";
    }
    if (help) row.appendChild(element("div", "field-help", help));
    if (field.key === "MOSS_LIVE_VOICE_ID") voiceRow = { row, field };
    return row;
}

function gestureRow() {
    const row = element("div", "field");
    row.appendChild(element("div", "field-head")).appendChild(
        element("span", "field-label", "预览动作"),
    );
    const group = row.appendChild(element("div", "segmented"));
    for (const [kind, label] of Object.entries(GESTURES)) {
        const button = group.appendChild(element("button", "", label));
        button.type = "button";
        button.addEventListener("click", async () => {
            try {
                await postJSON(`/api/gesture/${kind}`);
                note(`正在做动作：${label}`, "ok");
            } catch (e) {
                note(`动作没有执行：${e.message}`, "error");
            }
        });
    }
    return row;
}

async function loadSettings() {
    settingsLoaded = true;
    try {
        const resp = await fetch("/api/settings");
        if (!resp.ok) return;
        const { groups, fields, local_time_zone: localTimeZone } = await resp.json();
        const open = new Set(
            settingsGroups.children.length
                ? [...settingsGroups.querySelectorAll("details[open]")].map((d) => d.dataset.group)
                : OPEN_GROUPS,
        );
        settingsGroups.replaceChildren();
        for (const [group, title] of Object.entries(groups)) {
            const details = settingsGroups.appendChild(element("details"));
            details.dataset.group = group;
            details.open = open.has(group);
            details.appendChild(element("summary", "", title));
            for (const field of fields.filter((item) => item.group === group)) {
                details.appendChild(fieldRow(field, localTimeZone));
            }
            if (group === "robot") details.appendChild(gestureRow());
        }
        settingsPanel.hidden = false;
        if (location.hash === "#settings") settingsPanel.scrollIntoView();
        if (accountVoices === null) loadVoices();
    } catch (e) {
        settingsLoaded = false;
    }
}

async function loadVoices() {
    accountVoices = [];
    try {
        const resp = await fetch("/api/voices");
        const body = await resp.json();
        if (!resp.ok) throw new Error(body.error);
        accountVoices = body.voices;
        // Only the voice menu changes; leave anything being typed elsewhere alone.
        if (voiceRow) voiceRow.row.replaceWith(fieldRow(voiceRow.field));
    } catch (e) {
        note(`没有读到账号音色列表：${e.message}`, "error");
    }
}

async function controlApp(action, question, done) {
    if (!confirm(question)) return;
    try {
        await postJSON(`/api/app/${action}`);
        note(done, "ok");
    } catch (e) {
        note(e.message, "error");
    }
}

document.getElementById("restart-btn").addEventListener("click", () =>
    controlApp("restart", "重启 MOSS？机器人会先休眠再醒来。", "正在重启，页面会自动恢复…"));
document.getElementById("stop-btn").addEventListener("click", () =>
    controlApp("stop", "停止 MOSS 并让机器人休眠？之后用 mossy start 再次启动。", "已停止。"));

muteBtn.addEventListener("click", async () => {
    await fetch("/mute", { method: "POST" });
    poll();
});

expressionsBtn.addEventListener("click", async () => {
    await fetch("/expressions/toggle", { method: "POST" });
    poll();
});

cameraBtn.addEventListener("click", async () => {
    const response = await fetch("/camera/toggle", { method: "POST" });
    if (!response.ok) {
        cameraStatus.textContent = "请在本机状态页控制视觉输入";
        return;
    }
    poll();
});

cameraPreview.addEventListener("error", () => {
    cameraPanel.hidden = true;
    previewFrame = null;
});

document.addEventListener("visibilitychange", poll);
poll();
setInterval(poll, 400);
