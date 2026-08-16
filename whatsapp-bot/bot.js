const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
} = require("@whiskeysockets/baileys");
const qrcode = require("qrcode-terminal");
const QRCode = require("qrcode");
const { spawn } = require("child_process");
const path = require("path");

if (typeof global.crypto === "undefined" || !global.crypto.createHash) {
  global.crypto = require("crypto");
}

const BRIDGE = path.join(__dirname, "..", "bridge.py");
const quietLogger = require("pino")({ level: "warn" });
const PHONE = process.env.JORGE_PHONE;
let pairingRequested = false;
const sentIds = new Set();

const activeBridges = new Map();

function bridgeCall(user, text, jid, sock) {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", [BRIDGE], { stdio: ["pipe", "pipe", "inherit"], detached: true });
    const entry = { child: p, aborted: false };
    activeBridges.set(jid, entry);
    let out = "";
    p.stdout.on("data", (d) => {
      out += d;
      const keep = [];
      for (const line of out.split("\n")) {
        if (line.trim()) {
          try {
            const obj = JSON.parse(line.trim());
            if (obj.progress) {
              send(sock, jid, obj.progress);
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
      if (activeBridges.get(jid) === entry) activeBridges.delete(jid);
      if (entry.aborted) return resolve({ reply: null, aborted: true });
      const lines = out.split("\n").map((l) => l.trim()).filter(Boolean);
      let reply = null;
      for (const line of lines) {
        try {
          const obj = JSON.parse(line);
          if (obj.progress) send(sock, jid, obj.progress);
          if (obj.reply) reply = obj.reply;
        } catch (e) {}
      }
      if (reply !== null) return resolve({ reply });
      reject(new Error("bridge reply not JSON: " + out.slice(0, 200)));
    });
    p.stdin.write(JSON.stringify({ user, text }) + "\n");
    p.stdin.end();
  });
}

async function startBot() {
  const { state, saveCreds } = await useMultiFileAuthState(path.join(__dirname, "wa_session"));
  const { version } = await fetchLatestBaileysVersion();
  const sock = makeWASocket({
    version,
    auth: state,
    logger: quietLogger,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    if (update.connection === "open") {
      console.log("\n  ✓ jorge is ONLINE. Say hi in WhatsApp!");
      try {
        const me = sock.user?.id;
        if (me) await send(sock, me, "Hey boss! Jorge is online ⚡ — talk to me here.");
        const lid = process.env.JORGE_LID;
        if (lid) await send(sock, lid, "Hey boss! Jorge is online ⚡ — talk to me here.");
      } catch (e) {
        console.log("  ✗ hello message failed:", e.message);
      }
    }
    if (update.qr) {
      QRCode.toFile(path.join(__dirname, "qr.png"), update.qr, { width: 400, margin: 2 })
        .then(() => {
          console.log("\n  QR saved to whatsapp-bot/qr.png — open it with your image viewer!");
          try {
            const { execSync } = require("child_process");
            execSync("xdg-open " + path.join(__dirname, "qr.png") + " 2>/dev/null &");
          } catch (e) {}
        })
        .catch(() => {});
      if (PHONE && !pairingRequested) {
        pairingRequested = true;
        try {
          const code = await sock.requestPairingCode(PHONE);
          console.log("\n  ┌────────────────────────────────────────────┐");
          console.log("  │  Pairing code:  " + code + "                    │");
          console.log("  │  WhatsApp > Linked Devices > Link with a    │");
          console.log("  │  phone number instead, then type this code  │");
          console.log("  └────────────────────────────────────────────┘\n");
        } catch (e) {
          console.log("  ✗ pairing code failed:", e.message);
        }
      } else if (!PHONE) {
        console.log("\n  ┌──────────────────────────────────┐");
        console.log("  │  Scan this QR with WhatsApp:     │");
        console.log("  │  WhatsApp > Settings > Linked    │");
        console.log("  │  Devices > Link a Device         │");
        console.log("  └──────────────────────────────────┘\n");
        qrcode.generate(update.qr, { small: true });
      }
    }
    if (update.lastDisconnect && update.connection === "close") {
      const code = update.lastDisconnect.error?.output?.statusCode;
      console.log("  ✗ close reason:", update.lastDisconnect.error?.message || update.lastDisconnect.error?.toString() || "(none)");
      if (code === DisconnectReason.loggedOut) {
        console.log("  ✗ logged out. Delete whatsapp-bot/wa_session and restart.");
        process.exit(1);
      }
      console.log("  connection closed, reconnecting in 3s...");
      setTimeout(() => startBot(), 3000);
    }
  });

  async function send(sock, jid, text) {
    try {
      const s = await sock.sendMessage(jid, { text, linkPreview: false });
      if (s?.key?.id) sentIds.add(s.key.id);
      return s;
    } catch (e) {
      console.error("  ✗ send failed:", e.message?.slice(0, 120));
      return null;
    }
  }

const seen = new Set();
sock.ev.on("messages.upsert", async (m) => {
    try {
      const msg = m.messages[0];
      if (!msg.message) return;
      if (!["notify", "append"].includes(m.type)) return;
      if (seen.has(msg.key.id)) return;
      seen.add(msg.key.id);
      if (seen.size > 500) seen.clear();
      const OWNER = new Set();
      const addOwner = (jid) => {
        if (!jid) return;
        const num = jid.split(":")[0];
        OWNER.add(num);
        if (num.endsWith("@lid")) OWNER.add(num.replace(/@lid$/, "@s.whatsapp.net"));
      };
      addOwner(sock.user?.id);
      addOwner(process.env.JORGE_LID);
      if (process.env.JORGE_PHONE) addOwner(process.env.JORGE_PHONE + "@s.whatsapp.net");
      const sender = (msg.key.participant || msg.key.remoteJid || "").split(":")[0];
      const isOwner = !!msg.key.fromMe || OWNER.has(sender);
      if (msg.key.fromMe && sentIds.has(msg.key.id)) return;
      const text =
        msg.message.conversation || msg.message.extendedTextMessage?.text || "";
      const raw = text.trim();
      if (!raw) return;
      const cmd = raw.toLowerCase();
      const TRIGGER = "@jorge";
      console.log(`  <raw ${m.type} fromMe=${!!msg.key.fromMe} ${msg.key.remoteJid}${msg.key.participant ? " via " + msg.key.participant : ""}: ${raw.slice(0, 50)}`);
      if (cmd.startsWith(TRIGGER) && !isOwner) {
        await send(sock, msg.key.remoteJid, "i only listen to the boss");
        return;
      }
      if (!isOwner) return;
      const ABORT_WORDS = new Set(["abort", "stop", "cancel", "abort task", "stop task", "cancel task"]);
      if (ABORT_WORDS.has(cmd)) {
        const entry = activeBridges.get(msg.key.remoteJid);
        if (entry && !entry.aborted) {
          entry.aborted = true;
          try {
            process.kill(-entry.child.pid, "SIGKILL");
          } catch (e) {
            try {
              entry.child.kill("SIGKILL");
            } catch (e2) {}
          }
          activeBridges.delete(msg.key.remoteJid);
          await send(sock, msg.key.remoteJid, "⏹ Task aborted.");
          console.log("  ✗ aborted task");
        } else {
          await send(sock, msg.key.remoteJid, "No task is running right now.");
        }
        return;
      }
      if (!cmd.startsWith(TRIGGER)) return;
      const payload = raw.slice(TRIGGER.length).trim() || "hi";
      if (payload.toLowerCase() === "!status") {
        await send(sock, msg.key.remoteJid, "jorge is online ⚡");
        return;
      }
      if (payload.toLowerCase() === "!quit") {
        await send(sock, msg.key.remoteJid, "bye! 🤠");
        process.exit(0);
      }
      console.log(`  < ${msg.key.remoteJid}: ${payload.slice(0, 80)}`);
      await sock.sendPresenceUpdate("composing", msg.key.remoteJid);
      try {
        const res = await bridgeCall(msg.key.remoteJid, payload, msg.key.remoteJid, sock);
        if (!res.aborted && res.reply !== null) {
          await send(sock, msg.key.remoteJid, res.reply);
        }
      } catch (e) {
        console.error("  ✗", e.message);
        await send(sock, msg.key.remoteJid, "⚠ " + String(e.message || e).slice(0, 500));
      }
    } catch (e) {
      console.error("  ✗ handler error:", e.message?.slice(0, 120));
    }
  });
}

process.on("unhandledRejection", (reason) => {
  console.error("  ✗ unhandled rejection:", String(reason?.message || reason).slice(0, 200));
});

startBot();