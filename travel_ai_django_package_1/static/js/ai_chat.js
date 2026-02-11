/* ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   AI Chat Widget - TravelAI
   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */
(function () {
  "use strict";

  // ── State ──
  let isOpen = false;
  let isSending = false;
  let currentCity = window.AI_CHAT_CITY || null;

  // ── DOM refs (lazy) ──
  const $ = (sel) => document.querySelector(sel);
  const panel  = () => $("#aiChatPanel");
  const msgs   = () => $("#aiChatMessages");
  const input  = () => $("#aiChatInput");
  const btn    = () => $("#aiChatSend");

  // ── Toggle ──
  window.aiChatToggle = function () {
    const p = panel();
    if (!p) return;
    isOpen = !isOpen;
    p.classList.toggle("open", isOpen);
    if (isOpen) {
      input()?.focus();
      if (!msgs().hasChildNodes()) showWelcome();
    }
  };

  // ── Welcome message ──
  function showWelcome() {
    const city = currentCity || "여행지";
    appendMsg(
      `안녕하세요! 🌏 AI 여행 도우미입니다.\n${city}에 대해 궁금한 점이 있으면 편하게 물어보세요!`,
      "bot"
    );
  }

  // ── Append message ──
  function appendMsg(text, role) {
    const el = document.createElement("div");
    el.className = "chat-msg " + role;
    // 줄바꿈 지원
    el.innerHTML = text.replace(/\n/g, "<br>");
    msgs().appendChild(el);
    el.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  // ── Typing indicator ──
  function showTyping() {
    const el = document.createElement("div");
    el.className = "chat-typing";
    el.id = "aiTyping";
    el.innerHTML = "<span></span><span></span><span></span>";
    msgs().appendChild(el);
    el.scrollIntoView({ behavior: "smooth" });
  }
  function hideTyping() {
    const el = document.getElementById("aiTyping");
    if (el) el.remove();
  }

  // ── Send ──
  window.aiChatSend = async function () {
    const inp = input();
    if (!inp) return;
    const query = inp.value.trim();
    if (!query || isSending) return;

    // User message
    appendMsg(query, "user");
    inp.value = "";
    isSending = true;
    btn().disabled = true;
    showTyping();

    try {
      const res = await fetch("/api/ai/chat/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, city: currentCity }),
      });
      const data = await res.json();
      hideTyping();

      if (data.ok && data.answer) {
        appendMsg(data.answer, "bot");
      } else {
        const errMsg = data.error || "답변을 생성하지 못했습니다.";
        appendMsg("⚠️ " + errMsg, "system");
      }
    } catch (e) {
      hideTyping();
      appendMsg("⚠️ 서버와 연결할 수 없습니다.", "system");
    } finally {
      isSending = false;
      btn().disabled = false;
      input()?.focus();
    }
  };

  // ── Quick question ──
  window.aiQuickAsk = function (text) {
    const inp = input();
    if (inp) {
      inp.value = text;
      aiChatSend();
    }
  };

  // ── Enter key ──
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && document.activeElement === input()) {
      e.preventDefault();
      aiChatSend();
    }
  });

  // ── Update city context (called from other pages) ──
  window.aiSetCity = function (city) {
    currentCity = city;
  };

  // ── AI Tip loader (for itinerary sidebar) ──
  window.loadAiTip = async function (city, dayNumber, poiNames) {
    const el = document.getElementById("aiTipContent");
    if (!el) return;

    el.innerHTML = '<span class="ai-tip-loading">✨ AI 팁 생성 중...</span>';

    try {
      const query = `${city} ${dayNumber}일차 여행 팁. 오늘 방문: ${poiNames}`;
      const res = await fetch("/api/ai/chat/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, city }),
      });
      const data = await res.json();

      if (data.ok && data.answer) {
        el.innerHTML = data.answer.replace(/\n/g, "<br>");
      } else {
        el.innerHTML = "팁을 불러오지 못했습니다.";
      }
    } catch (e) {
      el.innerHTML = "서버 연결 오류";
    }
  };
})();
