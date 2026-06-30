const chatWindow = document.getElementById("chat-window");
const userInput = document.getElementById("user-input");
const sendBtn = document.getElementById("send-btn");
const clearBtn = document.getElementById("clear-btn");
const micBtn = document.getElementById("mic-btn");
const voiceUpload = document.getElementById("voice-upload");
const voiceStatus = document.getElementById("voice-status");
const removeVoiceBtn = document.getElementById("remove-voice-btn");
const uploadText = document.getElementById("upload-text");

let mediaRecorder = null;
let audioChunks = [];

function addMessage(role, content) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    const span = document.createElement("span");
    span.className = "content";
    span.textContent = content;
    div.appendChild(span);
    chatWindow.appendChild(div);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

function showTyping() {
    const div = document.createElement("div");
    div.className = "typing-indicator";
    div.id = "typing";
    div.innerHTML = "<span></span><span></span><span></span>";
    chatWindow.appendChild(div);
    chatWindow.scrollTop = chatWindow.scrollHeight;
}

function hideTyping() {
    const el = document.getElementById("typing");
    if (el) el.remove();
}

function updateVoiceUI(hasVoice, filename) {
    if (hasVoice) {
        voiceStatus.textContent = filename;
        voiceStatus.classList.add("active");
        removeVoiceBtn.hidden = false;
        uploadText.textContent = "更换";
    } else {
        voiceStatus.textContent = "未上传参考音频";
        voiceStatus.classList.remove("active");
        removeVoiceBtn.hidden = true;
        uploadText.textContent = "上传音频";
    }
}

async function checkVoiceStatus() {
    try {
        const resp = await fetch("/voice-status");
        const data = await resp.json();
        updateVoiceUI(data.has_voice, data.filename || "");
    } catch (e) {}
}

voiceUpload.addEventListener("change", async () => {
    const file = voiceUpload.files[0];
    if (!file) return;

    const form = new FormData();
    form.append("file", file);

    uploadText.textContent = "上传中...";
    try {
        const resp = await fetch("/upload-voice", { method: "POST", body: form });
        if (resp.ok) {
            const data = await resp.json();
            updateVoiceUI(true, data.filename);
            addMessage("system", `已上传参考音频: ${data.filename}`);
        } else {
            const err = await resp.json();
            addMessage("system", `上传失败: ${err.error}`);
            updateVoiceUI(false);
        }
    } catch (e) {
        addMessage("system", "上传失败，请重试");
        updateVoiceUI(false);
    }
    voiceUpload.value = "";
});

removeVoiceBtn.addEventListener("click", async () => {
    try {
        await fetch("/remove-voice", { method: "POST" });
        updateVoiceUI(false);
        addMessage("system", "已移除参考音频，恢复默认音色");
    } catch (e) {
        addMessage("system", "移除失败");
    }
});

async function sendMessage() {
    const text = userInput.value.trim();
    if (!text) return;

    addMessage("user", text);
    userInput.value = "";
    sendBtn.disabled = true;
    showTyping();

    try {
        const resp = await fetch("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ message: text }),
        });

        hideTyping();

        if (resp.status === 429) {
            addMessage("system", "请稍等，上一条消息还在处理中...");
        } else if (resp.ok) {
            const data = await resp.json();
            addMessage("assistant", data.reply);
        } else {
            addMessage("system", "发送失败，请重试");
        }
    } catch (e) {
        hideTyping();
        addMessage("system", "网络错误，请检查连接");
    } finally {
        sendBtn.disabled = false;
        userInput.focus();
    }
}

async function clearChat() {
    try {
        await fetch("/clear", { method: "POST" });
        chatWindow.innerHTML = "";
        addMessage("system", "对话已清空，重新开始吧！");
    } catch (e) {
        addMessage("system", "清空失败");
    }
}

async function toggleRecording() {
    if (mediaRecorder && mediaRecorder.state === "recording") {
        mediaRecorder.stop();
        return;
    }

    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        mediaRecorder = new MediaRecorder(stream);

        mediaRecorder.addEventListener("dataavailable", (e) => {
            if (e.data.size > 0) audioChunks.push(e.data);
        });

        mediaRecorder.addEventListener("stop", async () => {
            micBtn.classList.remove("recording");
            stream.getTracks().forEach((t) => t.stop());

            const blob = new Blob(audioChunks, { type: mediaRecorder.mimeType });
            const ext = mediaRecorder.mimeType.includes("mp4") ? "m4a" : "webm";

            addMessage("system", "正在识别语音...");
            sendBtn.disabled = true;
            micBtn.disabled = true;
            showTyping();

            const form = new FormData();
            form.append("file", blob, `recording.${ext}`);

            try {
                const resp = await fetch("/transcribe", {
                    method: "POST",
                    body: form,
                });

                hideTyping();

                if (resp.status === 429) {
                    addMessage("system", "请稍等，上一条消息还在处理中...");
                } else if (resp.ok) {
                    const data = await resp.json();
                    addMessage("user", data.user_text);
                    addMessage("assistant", data.reply);
                } else {
                    const err = await resp.json().catch(() => ({}));
                    addMessage("system", err.error || "语音识别失败，请重试");
                }
            } catch (e) {
                hideTyping();
                addMessage("system", "网络错误，请检查连接");
            } finally {
                sendBtn.disabled = false;
                micBtn.disabled = false;
            }
        });

        mediaRecorder.start();
        micBtn.classList.add("recording");
    } catch (e) {
        addMessage("system", "无法访问麦克风，请检查权限");
    }
}

micBtn.addEventListener("click", toggleRecording);
sendBtn.addEventListener("click", sendMessage);
clearBtn.addEventListener("click", clearChat);
userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

checkVoiceStatus();
userInput.focus();
