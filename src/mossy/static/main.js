const chatWindow = document.getElementById("chat-window");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const muteBtn = document.getElementById("mute-btn");
const latencyEl = document.getElementById("latency");
const expressionsBtn = document.getElementById("expressions-btn");
const expressionStatus = document.getElementById("expression-status");
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

let renderedTranscript = "";

function renderTranscript(transcript) {
    const key = JSON.stringify(transcript);
    if (key === renderedTranscript) return;
    renderedTranscript = key;

    const atBottom =
        chatWindow.scrollHeight - chatWindow.scrollTop - chatWindow.clientHeight < 40;

    chatWindow.innerHTML = "";
    for (const line of transcript) {
        const div = document.createElement("div");
        div.className = `message ${line.role}${line.final ? "" : " partial"}`;
        const span = document.createElement("span");
        span.className = "content";
        span.textContent = line.text;
        div.appendChild(span);
        chatWindow.appendChild(div);
    }
    if (atBottom) chatWindow.scrollTop = chatWindow.scrollHeight;
}

function renderStatus(data) {
    const label = STATE_LABELS[data.state] || data.state;
    statusText.textContent = data.error || (data.muted ? `${label}（麦克风已静音）` : label);
    statusDot.className = `status-dot ${data.connected ? data.state : "disconnected"}`;
    muteBtn.textContent = data.muted ? "取消静音" : "静音麦克风";
    const expression = data.expressions;
    expressionsBtn.disabled = !expression || !expression.available;
    expressionsBtn.textContent = expression?.enabled ? "关闭表情动作" : "开启表情动作";
    expressionStatus.textContent = !expression ? "表情动作准备中…"
        : expression.error ? "表情动作暂不可用，语音可继续使用"
        : !expression.enabled ? "表情动作已关闭"
        : `表情动作：${EXPRESSION_LABELS[expression.active] || "待命"}`;

    const l = data.latency || {};
    latencyEl.textContent = l.tts_first_chunk
        ? `ASR ${l.asr}ms · 首句 ${l.llm_sentence}ms · TTS ${l.tts_first_chunk}ms`
        : "";
}

async function poll() {
    try {
        const resp = await fetch("/state");
        const data = await resp.json();
        renderStatus(data);
        renderTranscript(data.transcript);
    } catch (e) {
        statusText.textContent = "无法连接到机器人";
        statusDot.className = "status-dot disconnected";
    }
}

muteBtn.addEventListener("click", async () => {
    await fetch("/mute", { method: "POST" });
    poll();
});

expressionsBtn.addEventListener("click", async () => {
    await fetch("/expressions/toggle", { method: "POST" });
    poll();
});

poll();
setInterval(poll, 500);
