/**
 * reservation-widget.js — Frontend AI Chat Widget
 * =================================================
 * Drop this script into any webpage to add the AI reservation chat.
 * No framework dependencies — pure vanilla JS.
 *
 * Usage:
 *   <script src="/js/reservation-widget.js"
 *           data-api-url="https://api.thegrandolive.com"
 *           data-restaurant-name="The Grand Olive">
 *   </script>
 */

(function () {
  "use strict";

  // ── Configuration ──────────────────────────────────────────────────────────
  const currentScript = document.currentScript;
  const API_URL       = currentScript?.dataset.apiUrl || "http://localhost:8000";
  const RESTAURANT    = currentScript?.dataset.restaurantName || "Our Restaurant";
  const SESSION_KEY   = "grandolive_session_token";

  // ── State ──────────────────────────────────────────────────────────────────
  let sessionToken = sessionStorage.getItem(SESSION_KEY) || null;
  let isOpen       = false;
  let isTyping     = false;

  // ── DOM Construction ───────────────────────────────────────────────────────
  function buildWidget() {
    const style = document.createElement("style");
    style.textContent = `
      :root {
        --olive-green:   #2C5F2E;
        --olive-light:   #4a7c4e;
        --cream:         #faf8f4;
        --text-dark:     #1a1a1a;
        --text-muted:    #666;
        --border:        #e8e0d0;
        --shadow:        0 8px 32px rgba(0,0,0,0.15);
      }

      #gro-launcher {
        position: fixed; bottom: 28px; right: 28px; z-index: 9999;
        width: 60px; height: 60px; border-radius: 50%;
        background: var(--olive-green); color: white; border: none;
        cursor: pointer; font-size: 26px; line-height: 60px; text-align: center;
        box-shadow: var(--shadow); transition: transform 0.2s, background 0.2s;
      }
      #gro-launcher:hover { transform: scale(1.08); background: var(--olive-light); }

      #gro-widget {
        position: fixed; bottom: 100px; right: 28px; z-index: 9998;
        width: 380px; height: 580px; border-radius: 16px;
        background: var(--cream); box-shadow: var(--shadow);
        display: flex; flex-direction: column; overflow: hidden;
        transform: scale(0.95) translateY(10px); opacity: 0;
        transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
        pointer-events: none;
      }
      #gro-widget.open {
        transform: scale(1) translateY(0); opacity: 1; pointer-events: all;
      }

      #gro-header {
        background: var(--olive-green); color: white;
        padding: 16px 20px; display: flex; align-items: center; gap: 12px;
      }
      #gro-header .avatar {
        width: 40px; height: 40px; border-radius: 50%;
        background: rgba(255,255,255,0.2);
        display: flex; align-items: center; justify-content: center; font-size: 18px;
      }
      #gro-header .info h3 { margin: 0; font-size: 15px; font-weight: 600; }
      #gro-header .info p  { margin: 2px 0 0; font-size: 12px; opacity: 0.8; }
      #gro-header .close-btn {
        margin-left: auto; background: none; border: none; color: white;
        cursor: pointer; font-size: 20px; padding: 4px; opacity: 0.7;
      }
      #gro-header .close-btn:hover { opacity: 1; }

      #gro-messages {
        flex: 1; overflow-y: auto; padding: 16px;
        display: flex; flex-direction: column; gap: 12px;
        scroll-behavior: smooth;
      }
      #gro-messages::-webkit-scrollbar { width: 4px; }
      #gro-messages::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

      .msg { max-width: 82%; display: flex; flex-direction: column; gap: 4px; }
      .msg.user  { align-self: flex-end; align-items: flex-end; }
      .msg.agent { align-self: flex-start; align-items: flex-start; }

      .msg .bubble {
        padding: 10px 14px; border-radius: 16px; font-size: 14px; line-height: 1.5;
        white-space: pre-wrap; word-break: break-word;
      }
      .msg.user  .bubble { background: var(--olive-green); color: white; border-bottom-right-radius: 4px; }
      .msg.agent .bubble { background: white; color: var(--text-dark); border: 1px solid var(--border); border-bottom-left-radius: 4px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
      .msg .time { font-size: 11px; color: var(--text-muted); }

      .typing-indicator {
        display: flex; gap: 4px; padding: 10px 14px;
        background: white; border: 1px solid var(--border);
        border-radius: 16px; border-bottom-left-radius: 4px;
        width: fit-content; align-self: flex-start;
        box-shadow: 0 1px 4px rgba(0,0,0,0.06);
      }
      .typing-dot {
        width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted);
        animation: bounce 1.2s ease-in-out infinite;
      }
      .typing-dot:nth-child(2) { animation-delay: 0.2s; }
      .typing-dot:nth-child(3) { animation-delay: 0.4s; }
      @keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-6px); } }

      .confirmation-card {
        background: linear-gradient(135deg, #f0faf0, #e8f5e9);
        border: 1px solid #a5d6a7; border-radius: 12px; padding: 14px;
        font-size: 13px; color: var(--text-dark); line-height: 1.6;
        align-self: flex-start; max-width: 92%;
      }
      .confirmation-card .code {
        font-size: 18px; font-weight: 700; color: var(--olive-green);
        letter-spacing: 2px; margin-top: 8px;
      }

      #gro-input-area {
        display: flex; gap: 8px; padding: 12px 16px;
        border-top: 1px solid var(--border); background: white;
      }
      #gro-input {
        flex: 1; border: 1px solid var(--border); border-radius: 24px;
        padding: 10px 16px; font-size: 14px; outline: none; resize: none;
        background: var(--cream); color: var(--text-dark);
        max-height: 100px; overflow-y: auto; line-height: 1.4;
        font-family: inherit;
      }
      #gro-input:focus { border-color: var(--olive-green); }
      #gro-input::placeholder { color: var(--text-muted); }
      #gro-send {
        width: 42px; height: 42px; border-radius: 50%; border: none;
        background: var(--olive-green); color: white; cursor: pointer;
        font-size: 18px; display: flex; align-items: center; justify-content: center;
        flex-shrink: 0; transition: background 0.2s;
      }
      #gro-send:hover:not(:disabled) { background: var(--olive-light); }
      #gro-send:disabled { opacity: 0.5; cursor: not-allowed; }

      @media (max-width: 420px) {
        #gro-widget { width: calc(100vw - 32px); right: 16px; }
      }
    `;
    document.head.appendChild(style);

    // Launcher button
    const launcher = document.createElement("button");
    launcher.id        = "gro-launcher";
    launcher.innerHTML = "🍽️";
    launcher.title     = `Chat with ${RESTAURANT} AI Reservation Assistant`;
    launcher.addEventListener("click", toggleWidget);
    document.body.appendChild(launcher);

    // Chat widget
    const widget = document.createElement("div");
    widget.id        = "gro-widget";
    widget.innerHTML = `
      <div id="gro-header">
        <div class="avatar">🤖</div>
        <div class="info">
          <h3>Aria · Reservations</h3>
          <p>${RESTAURANT}</p>
        </div>
        <button class="close-btn" onclick="document.getElementById('gro-launcher').click()">✕</button>
      </div>
      <div id="gro-messages"></div>
      <div id="gro-input-area">
        <textarea id="gro-input" placeholder="Type your message…" rows="1"></textarea>
        <button id="gro-send" title="Send">➤</button>
      </div>
    `;
    document.body.appendChild(widget);

    // Event listeners
    document.getElementById("gro-send").addEventListener("click", sendMessage);
    document.getElementById("gro-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });

    // Auto-resize textarea
    document.getElementById("gro-input").addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 100) + "px";
    });
  }

  // ── Widget Toggle ──────────────────────────────────────────────────────────
  function toggleWidget() {
    isOpen = !isOpen;
    const widget   = document.getElementById("gro-widget");
    const launcher = document.getElementById("gro-launcher");
    widget.classList.toggle("open", isOpen);
    launcher.innerHTML = isOpen ? "✕" : "🍽️";

    if (isOpen) {
      // Show greeting if no messages yet
      const msgs = document.getElementById("gro-messages");
      if (msgs.children.length === 0) {
        appendMessage(
          "agent",
          `Hello! Welcome to **${RESTAURANT}** 🫒\n\nI'm Aria, your personal reservation assistant. I'd be delighted to help you book a table.\n\nJust let me know:\n- What date you're thinking of?\n- How many guests will be joining you?\n- Any seating preference — outdoor terrace, cozy corner, private room?`
        );
      }
      setTimeout(() => document.getElementById("gro-input").focus(), 300);
    }
  }

  // ── Send Message ───────────────────────────────────────────────────────────
  async function sendMessage() {
    const input = document.getElementById("gro-input");
    const text  = input.value.trim();
    if (!text || isTyping) return;

    // Append user message
    appendMessage("user", text);
    input.value     = "";
    input.style.height = "auto";

    // Show typing indicator
    showTyping();

    try {
      const headers = {
        "Content-Type": "application/json",
        ...(sessionToken ? { "X-Session-Token": sessionToken } : {}),
      };

      const resp = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers,
        body: JSON.stringify({ message: text, session_token: sessionToken }),
      });

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${resp.status}`);
      }

      const data = await resp.json();

      // Persist session token across messages
      if (data.session_token) {
        sessionToken = data.session_token;
        sessionStorage.setItem(SESSION_KEY, sessionToken);
      }

      hideTyping();
      appendMessage("agent", data.reply);

      // Show booking confirmation card if complete
      if (data.booking_complete && data.confirmation_code) {
        appendConfirmationCard(data.confirmation_code);
      }

    } catch (err) {
      hideTyping();
      appendMessage(
        "agent",
        "I'm sorry, I ran into a connection issue. Please try again or call us directly at +44 20 7946 0921."
      );
      console.error("Reservation API error:", err);
    }
  }

  // ── DOM Helpers ────────────────────────────────────────────────────────────
  function appendMessage(role, text) {
    const container = document.getElementById("gro-messages");
    const div       = document.createElement("div");
    div.className   = `msg ${role}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text; // Safe: no innerHTML → XSS-free

    const time = document.createElement("span");
    time.className   = "time";
    time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

    div.appendChild(bubble);
    div.appendChild(time);
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
  }

  function appendConfirmationCard(code) {
    const container = document.getElementById("gro-messages");
    const card      = document.createElement("div");
    card.className  = "confirmation-card";
    card.innerHTML  = `
      ✅ <strong>Booking Confirmed!</strong><br>
      Your confirmation code:<br>
      <div class="code">${code}</div>
      <small>Check your email for full details.</small>
    `;
    container.appendChild(card);
    container.scrollTop = container.scrollHeight;
  }

  function showTyping() {
    isTyping = true;
    document.getElementById("gro-send").disabled = true;
    const container = document.getElementById("gro-messages");
    const indicator = document.createElement("div");
    indicator.id    = "typing-indicator";
    indicator.className = "typing-indicator";
    indicator.innerHTML = `
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
    `;
    container.appendChild(indicator);
    container.scrollTop = container.scrollHeight;
  }

  function hideTyping() {
    isTyping = false;
    document.getElementById("gro-send").disabled = false;
    const indicator = document.getElementById("typing-indicator");
    if (indicator) indicator.remove();
  }

  // ── Bootstrap ──────────────────────────────────────────────────────────────
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", buildWidget);
  } else {
    buildWidget();
  }
})();
