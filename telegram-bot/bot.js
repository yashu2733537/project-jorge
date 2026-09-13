"use strict";

const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");
const TelegramBot = require("node-telegram-bot-api");

const BRIDGE = path.join(__dirname, "..", "bridge.py");
const ENV_FILE = path.join(__dirname, "..", ".env");
const TRIGGER = "/jorge";

// ---------------- env ----------------

function loadEnv() {
  const out = {};
  try {
    for (const raw of fs.readFileSync(ENV_FILE, "utf-8").split("\n")) {
      const line = raw.trim();
      if (!line || line.startsWith("#") || !line.includes("=")) continue;
      const i = line.indexOf("=");
      out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, "");
    }
  } catch (e) {
    console.error("  ✗ could not read .env:", e.message);
  }
  return out;
}

const cfg = loadEnv();
const TOKEN = process.env.TELEGRAM_TOKEN || cfg.TELEGRAM_TOKEN;
const OWNER = process.env.TELEGRAM_OWNER_ID || cfg.TELEGRAM_OWNER_ID || null;

if (!TOKEN) {
  console.error("  ✗ TELEGRAM_TOKEN missing in ../.env — create a bot with @BotFather (https://t.me/BotFather)");
  process.exit(1);
}

const bot = new TelegramBot(TOKEN, { polling: true });

// ---------------- bridge (jorge brain) ----------------

const activeBridges = new Map();

function bridgeCall(chatId, user, text, action, query) {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", [BRIDGE], { stdio: ["pipe", "pipe", "inherit"], detached: true });
    const entry = { child: p, aborted: false };
    activeBridges.set(chatId, entry);
    let out = "";
    p.stdout.on("data", (d) => {
      out += d;
      const keep = [];
      for (const line of out.split("\n")) {
        if (line.trim()) {
          try {
            const obj = JSON.parse(line.trim());
            if (obj.progress) {
              forwardProgress(chatId, obj.progress);
              continue;
            }
          } catch (e) {}
        }
        keep.push(line);
      }
      out = keep.join("\n");
    });
    p.on("error", reject);
    p.on("close", (code) => {
      if (activeBridges.get(chatId) === entry) activeBridges.delete(chatId);
      if (entry.aborted) return resolve({ reply: null, aborted: true });
      const lines = out.split("\n").map((l) => l.trim()).filter(Boolean);
      let reply = null;
      for (const line of lines) {
        try {
          const obj = JSON.parse(line);
          if (obj.progress) forwardProgress(chatId, obj.progress);
          if (obj.reply) reply = obj.reply;
        } catch (e) {}
      }
      if (reply !== null) return resolve({ reply });
      reject(new Error("bridge reply not JSON: " + out.slice(0, 200)));
    });
    const req = { user, prefix: "tg", owner_ok: !!OWNER && String(user) === OWNER };
    if (text) req.text = text;
    if (action) req.action = action;
    if (query) req.query = query;
    p.stdin.write(JSON.stringify(req) + "\n");
    p.stdin.end();
  });
}

const ABORT_WORDS = new Set(["abort", "stop", "cancel", "abort task", "stop task", "cancel task"]);

// ---------------- message helpers ----------------

const lastProgress = new Map();
let lastProgressTs = 0;

function forwardProgress(chatId, text) {
  const now = Date.now();
  if (lastProgressTs && now - lastProgressTs < 4000) return;
  lastProgressTs = now;
  const prev = lastProgress.get(chatId);
  if (prev && now - prev < 4000) return;
  lastProgress.set(chatId, now);
  sendSafe(chatId, "⏳ " + String(text).slice(0, 300));
}

function chunkText(text, limit) {
  const parts = [];
  let rest = String(text || "");
  while (rest.length > limit) {
    let cut = rest.lastIndexOf("\n", limit);
    if (cut <= 0) cut = rest.lastIndexOf(" ", limit);
    if (cut <= 0) cut = limit;
    parts.push(rest.slice(0, cut));
    rest = rest.slice(cut).trimStart();
  }
  if (rest) parts.push(rest);
  return parts;
}

async function sendSafe(chatId, text) {
  try {
    for (const part of chunkText(text, 4000)) {
      await bot.sendMessage(chatId, part, { disable_web_page_preview: true });
    }
  } catch (e) {
    console.error("  ✗ send failed:", e.message?.slice(0, 120));
  }
}

const isOwner = (msg) => !OWNER || String(msg.from?.id) === OWNER;

async function brain(chatId, user, payload) {
  try {
    bot.sendChatAction(chatId, "typing").catch(() => {});
    const res = await bridgeCall(chatId, user, payload);
    if (!res.aborted && res.reply !== null) await sendSafe(chatId, res.reply);
  } catch (e) {
    await sendSafe(chatId, "⚠ " + String(e.message || e).slice(0, 500));
  }
}

function runAction(chatId, user, action, query) {
  bridgeCall(chatId, user, "", action, query)
    .then((r) => !r.aborted && r.reply !== null && sendSafe(chatId, r.reply))
    .catch((e) => sendSafe(chatId, "⚠ " + String(e.message || e).slice(0, 500)));
}

// ---------------- commands ----------------

function handleCommand(msg, matchedText) {
  const chatId = msg.chat.id;
  const text = matchedText.trim();
  if (!isOwner(msg)) {
    sendSafe(chatId, "i only listen to the boss");
    return;
  }
  const cmd = text.toLowerCase().split(/\s+/)[0];
  const rest = text.slice(text.indexOf(" ") + 1).trim();

  switch (cmd) {
    case "/start":
      return sendSafe(chatId, "Hey boss! I'm **jorge** ⚡ — your AI assistant.\nType /help to see what I can do.");
    case "/help":
      return sendSafe(chatId, [
        "🤖 *jorge commands*",
        "`/jorge <msg>` — talk to the assistant",
        "🔍 `/research <topic>` — cited research",
        "💡 `/brainstorm <topic>` — structured ideas",
        "♟ `/chess <fen or moves>` — stockfish analysis",
        "♟ `/chess-vs <elo>` — play a game vs jorge (500-3190) · `/move <san>` to play",
        "⏹ `/abort` — stop a running task",
        "⚡ `/status` · `/ping`",
        "",
        "In a private chat you can just type — no `/jorge` needed.",
      ].join("\n"));
    case "/ping":
      return sendSafe(chatId, "🏓 pong — jorge is online ⚡");
    case "/status":
      return sendSafe(chatId, "jorge is online ⚡");
    case "/research":
      return runAction(chatId, msg.from.id, "research", rest || "general");
    case "/brainstorm":
      return runAction(chatId, msg.from.id, "brainstorm", rest || "general");
    case "/chess":
      return runAction(chatId, msg.from.id, "chess", rest || "start");
    case "/chess-vs":
    case "/chessvs":
      return runAction(chatId, msg.from.id, "chess_vs", rest || "1200");
    case "/move":
      return runAction(chatId, msg.from.id, "chess_move", rest || "resign");
    default:
      if (ABORT_WORDS.has(cmd.replace("/", "")) || ABORT_WORDS.has(text.toLowerCase())) {
        const entry = activeBridges.get(chatId);
        if (entry && !entry.aborted) {
          entry.aborted = true;
          try {
            process.kill(-entry.child.pid, "SIGKILL");
          } catch (e) {
            try {
              entry.child.kill("SIGKILL");
            } catch (e2) {}
          }
          activeBridges.delete(chatId);
          return sendSafe(chatId, "⏹ Task aborted.");
        }
        return sendSafe(chatId, "No task is running right now.");
      }
      if (text.toLowerCase() === "/quit" || rest.toLowerCase() === "!quit") {
        sendSafe(chatId, "bye! 🤠");
        return process.exit(0);
      }
      return brain(chatId, msg.from.id, text || "hi");
  }
}

// ---------------- message routing ----------------

bot.on("message", async (msg) => {
  try {
    if (msg.from?.is_bot) return;
    const raw = (msg.text || "").trim();
    if (!raw) return;

    const chatType = msg.chat.type;
    const matches = raw.match(/(?:^|\s)(\/jorge(?:@\w+)?(?:\s+[\s\S]*)?)$/i) || raw.match(/^\/jorge(?:@\w+)?(?:\s+[\s\S]*)?$/i);

    if (chatType === "private") {
      return handleCommand(msg, matches ? matches[1].trim() : raw);
    }

    // group / supergroup: only respond to /jorge, an @mention of us, or replies to us
    if (matches) return handleCommand(msg, matches[1].trim());
    const mention = raw.match(/^@([A-Za-z_]\w*)(?:\s+([\s\S]*))?$/);
    if (mention && botInfo && mention[1].toLowerCase() === botInfo.username.toLowerCase()) {
      return handleCommand(msg, mention[2] || "hi");
    }
    if (msg.reply_to_message && msg.reply_to_message.from?.id === botInfo?.id) {
      return handleCommand(msg, raw);
    }
  } catch (e) {
    console.error("  ✗ handler error:", String(e.message || e).slice(0, 200));
  }
});

let botInfo = null;

bot.on("polling_error", (err) => {
  console.error("  ✗ polling error:", String(err?.message || err).slice(0, 160));
});

bot.getMe()
  .then((me) => {
    botInfo = me;
    const cmds = [
      { command: "jorge", description: "talk to jorge (the assistant)" },
      { command: "research", description: "deep research with cited sources" },
      { command: "brainstorm", description: "structured brainstorm on a topic" },
      { command: "chess", description: "analyze a chess position with stockfish" },
      { command: "chess-vs", description: "play a chess game vs jorge (500-3190)" },
      { command: "move", description: "make a move in your game (SAN) or resign" },
      { command: "abort", description: "stop a running task" },
      { command: "status", description: "is jorge online?" },
      { command: "help", description: "list commands" },
    ];
    bot.setMyCommands(cmds).catch(() => {});
    console.log("\n  ✓ jorge is ONLINE on Telegram. Say /jorge hi!");
    console.log(`  ✓ bot handle: @${me.username}`);
    console.log(`  ✓ owner-lock: ${OWNER ? "enabled (TELEGRAM_OWNER_ID=" + OWNER + ")" : "OFF — anyone who finds the bot can use it"}`);
  })
  .catch((e) => {
    console.error("  ✗ getMe failed:", String(e?.message || e).slice(0, 160));
  });

process.on("unhandledRejection", (reason) => {
  console.error("  ✗ unhandled rejection:", String(reason?.message || reason).slice(0, 200));
});