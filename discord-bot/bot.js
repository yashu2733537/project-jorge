const {
  Client,
  GatewayIntentBits,
  Partials,
  PermissionFlagsBits,
  SlashCommandBuilder,
  REST,
  Routes,
  EmbedBuilder,
} = require("discord.js");
const { spawn } = require("child_process");
const music = require("./music.js");
const fs = require("fs");
const path = require("path");

const BRIDGE = path.join(__dirname, "..", "bridge.py");
const ENV_FILE = path.join(__dirname, "..", ".env");
const CONFIG_FILE = path.join(__dirname, "config.json");
const TRIGGER = "@jorge";

// ---------------- config / env ----------------

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
const TOKEN = cfg.DISCORD_TOKEN;
const OWNER = cfg.DISCORD_OWNER_ID || null;

if (!TOKEN) {
  console.error("  ✗ DISCORD_TOKEN missing in ../.env — create a bot at discord.com/developers");
  process.exit(1);
}

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf-8"));
  } catch (e) {
    return {};
  }
}

function saveConfig(c) {
  try {
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(c, null, 2));
  } catch (e) {}
}

// ---------------- bridge (jorge brain) ----------------

const activeBridges = new Map();

function bridgeCall(user, text, chanId, action, query, opts) {
  return new Promise((resolve, reject) => {
    const p = spawn("python3", [BRIDGE], { stdio: ["pipe", "pipe", "inherit"], detached: true });
    const entry = { child: p, aborted: false };
    activeBridges.set(chanId, entry);
    let out = "";
    p.stdout.on("data", (d) => {
      out += d;
      const keep = [];
      for (const line of out.split("\n")) {
        if (line.trim()) {
          try {
            const obj = JSON.parse(line.trim());
            if (obj.progress) {
              sendTo(chanId, obj.progress);
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
      if (activeBridges.get(chanId) === entry) activeBridges.delete(chanId);
      if (entry.aborted) return resolve({ reply: null, aborted: true });
      const lines = out.split("\n").map((l) => l.trim()).filter(Boolean);
      let reply = null;
      for (const line of lines) {
        try {
          const obj = JSON.parse(line);
          if (obj.progress) sendTo(chanId, obj.progress);
          if (obj.reply) reply = obj.reply;
        } catch (e) {}
      }
      if (reply !== null) return resolve({ reply });
      reject(new Error("bridge reply not JSON: " + out.slice(0, 200)));
    });
    const req = { user, prefix: "dc", owner_ok: !!OWNER && user === OWNER };
    if (action) req.action = action;
    if (query) req.query = query;
    if (text) req.text = text;
    p.stdin.write(JSON.stringify(req) + "\n");
    p.stdin.end();
  });
}

function sendTo(chanId, text) {
  client.channels.fetch(chanId).then((ch) => ch && ch.send(text).catch(() => {})).catch(() => {});
}

const ABORT_WORDS = new Set(["abort", "stop", "cancel", "abort task", "stop task", "cancel task"]);

// ---------------- slash commands ----------------

const COMMANDS = [
  new SlashCommandBuilder().setName("jorge").setDescription("talk to jorge (the assistant)")
    .addStringOption((o) => o.setName("message").setDescription("what to say").setRequired(true)),
  new SlashCommandBuilder().setName("research").setDescription("deep web research with cited sources")
    .addStringOption((o) => o.setName("topic").setDescription("what to research").setRequired(true)),
  new SlashCommandBuilder().setName("brainstorm").setDescription("structured brainstorm on a topic")
    .addStringOption((o) => o.setName("topic").setDescription("what to brainstorm").setRequired(true)),
  new SlashCommandBuilder().setName("chess").setDescription("analyze a chess position with stockfish")
    .addStringOption((o) => o.setName("position").setDescription("FEN, move list (1. e4 e5), or 'start'").setRequired(true)),
  new SlashCommandBuilder().setName("chess-vs").setDescription("start a chess game vs jorge")
    .addIntegerOption((o) => o.setName("elo").setDescription("jorge's elo (500-3190, default 1200)")),
  new SlashCommandBuilder().setName("move").setDescription("make a move in your chess game vs jorge")
    .addStringOption((o) => o.setName("move").setDescription("SAN move (e4, Nf3, O-O) or 'resign'").setRequired(true)),
  new SlashCommandBuilder().setName("ping").setDescription("bot latency"),
  new SlashCommandBuilder().setName("info").setDescription("bot info"),
  new SlashCommandBuilder().setName("help").setDescription("list all commands"),
  new SlashCommandBuilder().setName("userinfo").setDescription("info about a user")
    .addUserOption((o) => o.setName("user").setDescription("who (defaults to you)")),
  new SlashCommandBuilder().setName("serverinfo").setDescription("info about this server"),
  new SlashCommandBuilder().setName("avatar").setDescription("get someone's avatar")
    .addUserOption((o) => o.setName("user").setDescription("who (defaults to you)")),
  new SlashCommandBuilder().setName("say").setDescription("make jorge say something")
    .addStringOption((o) => o.setName("text").setDescription("what to say").setRequired(true)),
  new SlashCommandBuilder().setName("roll").setDescription("roll dice (e.g. 2d6, 20)")
    .addStringOption((o) => o.setName("dice").setDescription("dice spec").setRequired(true)),
  new SlashCommandBuilder().setName("flip").setDescription("coin flip"),
  new SlashCommandBuilder().setName("8ball").setDescription("ask the magic 8-ball")
    .addStringOption((o) => o.setName("question").setDescription("your question").setRequired(true)),
  new SlashCommandBuilder().setName("clear").setDescription("delete messages")
    .addIntegerOption((o) => o.setName("count").setDescription("how many (max 100)").setRequired(true)),
  new SlashCommandBuilder().setName("kick").setDescription("kick a member")
    .addUserOption((o) => o.setName("user").setDescription("who").setRequired(true))
    .addStringOption((o) => o.setName("reason").setDescription("why")),
  new SlashCommandBuilder().setName("ban").setDescription("ban a member")
    .addUserOption((o) => o.setName("user").setDescription("who").setRequired(true))
    .addStringOption((o) => o.setName("reason").setDescription("why")),
  new SlashCommandBuilder().setName("unban").setDescription("unban a user")
    .addStringOption((o) => o.setName("userid").setDescription("their user id").setRequired(true)),
  new SlashCommandBuilder().setName("mute").setDescription("timeout a member (10 min)")
    .addUserOption((o) => o.setName("user").setDescription("who").setRequired(true)),
  new SlashCommandBuilder().setName("unmute").setDescription("remove a member's timeout")
    .addUserOption((o) => o.setName("user").setDescription("who").setRequired(true)),
  new SlashCommandBuilder().setName("warn").setDescription("warn a member (DMs them)")
    .addUserOption((o) => o.setName("user").setDescription("who").setRequired(true))
    .addStringOption((o) => o.setName("reason").setDescription("why").setRequired(true)),
  new SlashCommandBuilder().setName("nick").setDescription("change jorge's nickname here")
    .addStringOption((o) => o.setName("name").setDescription("new nickname").setRequired(true)),
  new SlashCommandBuilder().setName("setwelcome").setDescription("set the welcome channel")
    .addChannelOption((o) => o.setName("channel").setDescription("channel (or 'off')").setRequired(true)),
];

function registerCommands(client) {
  const rest = new REST({ version: "10" }).setToken(TOKEN);
  rest.put(Routes.applicationCommands(client.user.id), { body: COMMANDS.map((c) => c.toJSON()) })
    .then(() => console.log(`  ✓ ${COMMANDS.length} slash commands registered`))
    .catch((e) => console.error("  ✗ slash command registration failed:", String(e.message || e).slice(0, 160)));
}

// ---------------- permission helpers ----------------

function hasPerm(memberOrInteractionMember, perm) {
  try {
    return memberOrInteractionMember.permissions.has(perm);
  } catch (e) {
    return false;
  }
}

function botHasPerm(guild, perm) {
  try {
    return guild.members.me.permissions.has(perm);
  } catch (e) {
    return false;
  }
}

function ownerOnly(userId) {
  if (!OWNER) return false;
  return userId !== OWNER;
}

// ---------------- fun helpers ----------------

const BALL = [
  "Yes.", "No.", "Ask again later.", "Definitely.", "I wouldn't count on it.",
  "It is certain.", "Most likely.", "My sources say no.", "Outlook good.", "Can't predict now.",
  "Signs point to yes.", "Very doubtful.", "Without a doubt.", "Maybe.", "Absolutely not.",
];

function rollDice(spec) {
  spec = String(spec || "1d6").toLowerCase().trim().replace(/^d/, "1d");
  const m = spec.match(/^(\d*)d(\d+)$/);
  if (m) {
    const n = Math.min(parseInt(m[1] || "1", 10) || 1, 10);
    const s = parseInt(m[2], 10);
    if (s < 1 || s > 1000000) return "Invalid die size.";
    const rolls = Array.from({ length: n }, () => 1 + Math.floor(Math.random() * s));
    const total = rolls.reduce((a, b) => a + b, 0);
    return n === 1 ? `🎲 d${s}: **${total}**` : `🎲 ${n}d${s}: ${rolls.join(" + ")} = **${total}**`;
  }
  const flat = parseInt(spec, 10);
  if (!isNaN(flat) && flat > 0) {
    return `🎲 d${flat}: **${1 + Math.floor(Math.random() * flat)}**`;
  }
  return "Usage: `roll 2d6`, `roll 20`.";
}

function avatarURL(user) {
  if (!user.avatar) return `https://cdn.discordapp.com/embed/avatars/${user.discriminator % 5}.png`;
  const anim = user.avatar.startsWith("a_") ? "gif" : "png";
  return `https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.${anim}?size=1024`;
}

// ---------------- command runner (shared by prefix + slash) ----------------

function fmtUptime(ms) {
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${h}h ${m}m`;
}

let startedAt = Date.now();

async function runCommand(client, ctx, name, args) {
  // ctx: { channel, guild, member, author, reply(fn), send(fn) }
  const reply = ctx.reply;
  const guild = ctx.guild;
  const member = ctx.member;
  const DESTRUCTIVE = new Set(["clear", "kick", "ban", "unban", "mute", "unmute", "warn"]);
  if (DESTRUCTIVE.has(name)) {
    if (!OWNER || ctx.author.id !== OWNER)
      return reply("🔒 That command is locked — only the boss can use it (set DISCORD_OWNER_ID in .env).");
  }
  switch (name) {
    case "ping":
      return reply(`🏓 Pong! **${client.ws.ping}ms**`);
    case "info":
      return reply(
        `🤖 **jorge** — AI assistant, research & brainstorm specialist\n` +
        `🕐 up ${fmtUptime(Date.now() - startedAt)} · 📡 ping ${client.ws.ping}ms\n` +
        `🖥️ servers: ${client.guilds.cache.size} · 👥 users: ${client.users.cache.size}\n` +
        `🛠️ @jorge <msg> or /jorge — full brain (email, browser, shell, research…)`
      );
    case "help":
      return reply(
        "**jorge commands**\n" +
        "🤖 `@jorge <msg>` / `/jorge <msg>` — talk to the assistant\n" +
        "🔍 `@jorge research <t>` / `/research` — cited research\n" +
        "💡 `@jorge brainstorm <t>` / `/brainstorm` — structured ideas\n" +
        "♟ `@jorge chess <fen or moves>` / `/chess` — stockfish analysis\n" +
        "♟ `?chess-vs <elo>` / `/chess-vs` — play a game vs jorge (elo 500-3190) · `?move <san>` to play\n" +
        "⚔️ `@jorge chess vs @user` — challenge someone · they reply `?accept` / `?decline`\n" +
        "🛠️ `?ping` `?info` `?help` `?userinfo [@u]` `?serverinfo` `?avatar [@u]`\n" +
        "🎮 `?roll 2d6` `?flip` `?8ball <q>` `?say <text>` `?nick <name>`\n" +
        "🎵 `?play <song or url>` `?skip` `?stop` `?pause` `?resume` `?queue` `?np` `?leave`\n" +
        "🛡️ `?clear <n>` `?kick @u [why]` `?ban @u [why]` `?unban <id>` `?mute @u` `?unmute @u` `?warn @u <why>`\n" +
        "🏠 `?setwelcome #channel` (or off) — welcome messages\n" +
        "⚡ `?status` · `@jorge abort` — stop a running task"
      );
    case "userinfo": {
      const user = args.user || ctx.author;
      let info = `👤 **${user.tag}** \`${user.id}\`${user.bot ? " 🤖" : ""}\n`;
      if (guild) {
        const m = await guild.members.fetch(user.id).catch(() => null);
        if (m) {
          info += `📥 joined server: <t:${Math.floor(m.joinedTimestamp / 1000)}:R>\n`;
          const roles = m.roles.cache.filter((r) => r.id !== guild.id).map((r) => r.name);
          info += `🎭 roles (${roles.length}): ${roles.slice(0, 10).join(", ") || "none"}\n`;
        }
      }
      info += `📅 account created: <t:${Math.floor(user.createdTimestamp / 1000)}:R>`;
      return reply(info);
    }
    case "serverinfo":
      if (!guild) return reply("Use this in a server.");
      return reply(
        `🏰 **${guild.name}** \`${guild.id}\`\n` +
        `👑 owner: <@${guild.ownerId}>\n` +
        `👥 members: ${guild.memberCount}\n` +
        `💬 channels: ${guild.channels.cache.size} · 🎭 roles: ${guild.roles.cache.size}\n` +
        `🚀 boost level: ${guild.premiumTier || 0}\n` +
        `📅 created: <t:${Math.floor(guild.createdTimestamp / 1000)}:R>`
      );
    case "avatar": {
      const user = args.user || ctx.author;
      const e = new EmbedBuilder().setTitle(`${user.tag}'s avatar`).setImage(avatarURL(user)).setColor(0xe6b84c);
      return ctx.send({ embeds: [e] });
    }
    case "say": {
      const text = String(args.text || "").trim();
      if (!text) return reply("What should I say?");
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.ManageMessages) && !ownerOnly(ctx.author.id)) {
        return reply("You need **Manage Messages** to make me say things.");
      }
      return reply(text);
    }
    case "roll":
      return reply(rollDice(args.dice || args.text));
    case "flip":
      return reply(Math.random() < 0.5 ? "🪙 Heads!" : "🪙 Tails!");
    case "8ball": {
      const q = String(args.question || args.text || "").trim();
      if (!q) return reply("Ask me a question first.");
      return reply(`🎱 ${BALL[Math.floor(Math.random() * BALL.length)]}`);
    }
    case "clear": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.ManageMessages) && !ownerOnly(ctx.author.id))
        return reply("You need **Manage Messages**.");
      if (!botHasPerm(guild, PermissionFlagsBits.ManageMessages))
        return reply("I need **Manage Messages** to do that.");
      const n = Math.min(Math.max(parseInt(args.count, 10) || 1, 1), 100) + 1;
      const msgs = await ctx.channel.messages.fetch({ limit: n }).catch(() => null);
      if (!msgs) return reply("Couldn't fetch messages.");
      await ctx.channel.bulkDelete(msgs, true).catch(() => {});
      return reply(`🧹 Cleared ${n - 1} messages.`);
    }
    case "kick": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.KickMembers) && !ownerOnly(ctx.author.id))
        return reply("You need **Kick Members**.");
      if (!botHasPerm(guild, PermissionFlagsBits.KickMembers)) return reply("I need **Kick Members**.");
      const target = args.user;
      if (!target || target.id === client.user.id) return reply("Give me a real user.");
      if (target.id === guild.ownerId) return reply("Can't kick the server owner.");
      await guild.members.kick(target.id, args.reason || "no reason given").catch(() => {});
      return reply(`👢 Kicked **${target.tag}** — ${args.reason || "no reason given"}`);
    }
    case "ban": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.BanMembers) && !ownerOnly(ctx.author.id))
        return reply("You need **Ban Members**.");
      if (!botHasPerm(guild, PermissionFlagsBits.BanMembers)) return reply("I need **Ban Members**.");
      const target = args.user;
      if (!target || target.id === client.user.id) return reply("Give me a real user.");
      if (target.id === guild.ownerId) return reply("Can't ban the server owner.");
      await guild.members.ban(target.id, { reason: args.reason || "no reason given" }).catch(() => {});
      return reply(`🔨 Banned **${target.tag}** — ${args.reason || "no reason given"}`);
    }
    case "unban": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.BanMembers) && !ownerOnly(ctx.author.id))
        return reply("You need **Ban Members**.");
      const id = String(args.userid || args.text || "").trim();
      if (!/^\d{15,20}$/.test(id)) return reply("Give me the user id.");
      await guild.members.unban(id, "unbanned").catch(() => {});
      return reply(`⛓️‍💥 Unbanned \`${id}\`.`);
    }
    case "mute":
    case "unmute": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.ModerateMembers) && !ownerOnly(ctx.author.id))
        return reply("You need **Moderate Members**.");
      if (!botHasPerm(guild, PermissionFlagsBits.ModerateMembers))
        return reply("I need **Moderate Members**.");
      const target = args.user;
      if (!target) return reply("Give me a real user.");
      const m = await guild.members.fetch(target.id).catch(() => null);
      if (!m) return reply("Can't find that member.");
      if (name === "mute") {
        await m.timeout(10 * 60 * 1000, "muted by moderator").catch(() => {});
        return reply(`🔇 Muted **${target.tag}** for 10 minutes.`);
      }
      await m.timeout(null, "unmuted").catch(() => {});
      return reply(`🔊 Unmuted **${target.tag}**.`);
    }
    case "warn": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.ModerateMembers) && !ownerOnly(ctx.author.id))
        return reply("You need **Moderate Members**.");
      const target = args.user;
      const reason = String(args.reason || "").trim() || "no reason given";
      if (!target || target.bot) return reply("Give me a real user.");
      await target.send(`⚠️ You've been **warned** in **${guild.name}**: ${reason}`).catch(() => {});
      return reply(`⚠️ Warned **${target.tag}** — ${reason}`);
    }
    case "nick": {
      if (!guild) return reply("Use this in a server.");
      if (!hasPerm(member, PermissionFlagsBits.ManageNicknames) && !ownerOnly(ctx.author.id))
        return reply("You need **Manage Nicknames**.");
      const name = String(args.name || args.text || "").trim().slice(0, 32);
      await guild.members.me.setNickname(name || null).catch(() => {});
      return reply(`✏️ Nickname set to **${name || "default"}**.`);
    }
    case "setwelcome": {
      if (!guild) return reply("Use this in a server.");
      if (!ownerOnly(ctx.author.id) && !hasPerm(member, PermissionFlagsBits.ManageGuild))
        return reply("You need **Manage Server**.");
      const cfgNow = loadConfig();
      const ch = args.channel;
      if (ch === "off" || (args.channel && args.channel === "off")) {
        delete cfgNow[guild.id];
        saveConfig(cfgNow);
        return reply("Welcome messages off.");
      }
      const target = typeof ch === "object" ? ch : ctx.channel;
      cfgNow[guild.id] = target.id;
      saveConfig(cfgNow);
      return reply(`🏠 Welcome messages → <#${target.id}>`);
    }
    default:
      return reply("Unknown command. Try `?help`.");
  }
}

// ---------------- bridge-style brain commands (shared) ----------------

async function jorgeBrain(channel, userId, payload) {
  await channel.sendTyping().catch(() => {});
  try {
    const res = await bridgeCall(userId, payload, channel.id);
    if (!res.aborted && res.reply !== null) await channel.send(res.reply).catch(() => {});
  } catch (e) {
    await channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {});
  }
}

// ---------------- client ----------------

function buildIntents(privileged) {
  const intents = [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.DirectMessages,
  ];
  if (privileged) {
    intents.push(GatewayIntentBits.MessageContent);
    intents.push(GatewayIntentBits.GuildMembers);
  }
  return intents;
}

function makeClient(intents) {
  const client = new Client({ intents, partials: [Partials.Channel] });

  client.on("interactionCreate", async (i) => {
    if (!i.isChatInputCommand()) return;
    const ctx = {
      channel: i.channel,
      guild: i.guild,
      member: i.member,
      author: i.user,
      reply: (t) => i.reply({ content: t, allowedMentions: { parse: [] } }).catch(() => {}),
      send: (o) => i.reply(o).catch(() => {}),
    };
    const name = i.commandName;
    if (name === "jorge") {
      const msg = i.options.getString("message") || "hi";
      await i.deferReply().catch(() => {});
      try {
        const res = await bridgeCall(i.user.id, msg, i.channel.id);
        if (!res.aborted && res.reply !== null)
          await i.editReply({ content: res.reply.slice(0, 1900) }).catch(() => {});
      } catch (e) {
        await i.editReply({ content: "⚠ " + String(e.message || e).slice(0, 500) }).catch(() => {});
      }
      return;
    }
    if (name === "research" || name === "brainstorm" || name === "chess") {
      const q = i.options.getString(name === "chess" ? "position" : "topic") || "general";
      await i.deferReply().catch(() => {});
      try {
        const res = await bridgeCall(i.user.id, "", i.channel.id, name, q);
        if (!res.aborted && res.reply !== null)
          await i.editReply({ content: res.reply.slice(0, 1900) }).catch(() => {});
      } catch (e) {
        await i.editReply({ content: "⚠ " + String(e.message || e).slice(0, 500) }).catch(() => {});
      }
      return;
    }
    if (name === "chess-vs") {
      const elo = i.options.getInteger("elo") || 1200;
      await i.deferReply().catch(() => {});
      try {
        const res = await bridgeCall(i.user.id, "", i.channel.id, "chess_vs", String(elo));
        if (!res.aborted && res.reply !== null)
          await i.editReply({ content: res.reply.slice(0, 1900) }).catch(() => {});
      } catch (e) {
        await i.editReply({ content: "⚠ " + String(e.message || e).slice(0, 500) }).catch(() => {});
      }
      return;
    }
    if (name === "move") {
      const mv = i.options.getString("move") || "resign";
      await i.deferReply().catch(() => {});
      try {
        const res = await bridgeCall(i.user.id, "", i.channel.id, "chess_move", mv);
        if (!res.aborted && res.reply !== null)
          await i.editReply({ content: res.reply.slice(0, 1900) }).catch(() => {});
      } catch (e) {
        await i.editReply({ content: "⚠ " + String(e.message || e).slice(0, 500) }).catch(() => {});
      }
      return;
    }
    const args = {};
    for (const o of COMMANDS) {
      if (o.name !== name) continue;
      for (const opt of o.options || []) {
        const v = i.options.get(opt.name)?.value;
        if (v !== undefined && v !== null) args[opt.name] = v;
      }
    }
    await runCommand(client, ctx, name, args).catch((e) =>
      ctx.reply("⚠ " + String(e.message || e).slice(0, 300))
    );
  });

  client.on("messageCreate", async (msg) => {
    try {
      if (msg.author.bot) return;
      const raw = (msg.content || "").trim();
      const cmd = raw.toLowerCase();
      let mentionsBot = false;
      try {
        mentionsBot = !!msg.mentions && msg.mentions.has(client.user.id);
      } catch (e) {}
      const literal = cmd.startsWith(TRIGGER);
      if (!mentionsBot && !literal) {
        if (raw.startsWith("?") && !raw.startsWith("??")) {
          const parts = raw.slice(1).split(/\s+/);
          const cname = parts[0].toLowerCase();
          if (cname) {
            const ctx = {
              channel: msg.channel,
              guild: msg.guild,
              member: msg.member,
              author: msg.author,
              reply: (t) => msg.channel.send({ content: t, allowedMentions: { parse: [] } }).catch(() => {}),
              send: (o) => msg.channel.send(o).catch(() => {}),
            };
            const args = { text: parts.slice(1).join(" ") };
            if (cname === "status") return msg.channel.send("jorge is online ⚡");
            if (cname === "quit") {
              if (!OWNER || msg.author.id !== OWNER) return;
              await msg.channel.send("bye! 🤠");
              return process.exit(0);
            }
            if (cname === "jorge") return jorgeBrain(msg.channel, msg.author.id, args.text || "hi");
            if (cname === "research")
              return bridgeCall(msg.author.id, "", msg.channel.id, "research", args.text || "general")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "brainstorm")
              return bridgeCall(msg.author.id, "", msg.channel.id, "brainstorm", args.text || "general")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "chess")
              return bridgeCall(msg.author.id, "", msg.channel.id, "chess", args.text || "start")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "chess-vs" || cname === "chessvs")
              return bridgeCall(msg.author.id, "", msg.channel.id, "chess_vs", args.text || "1200")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "move")
              return bridgeCall(msg.author.id, "", msg.channel.id, "chess_move", args.text || "resign")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "accept" || cname === "accept-chess")
              return bridgeCall(msg.author.id, "", msg.channel.id, "chess_accept", "")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "decline")
              return bridgeCall(msg.author.id, "", msg.channel.id, "chess_decline", "")
                .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
                .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
            if (cname === "play" || cname === "p") return music.play(msg, args.text);
            if (cname === "skip") return music.skip(msg);
            if (cname === "stop") return music.stop(msg);
            if (cname === "pause") return music.pause(msg);
            if (cname === "resume") return music.resume(msg);
            if (cname === "queue") return music.queue(msg);
            if (cname === "np" || cname === "nowplaying") return music.queue(msg);
            if (cname === "leave") return music.leave(msg);
            const userArg = msg.mentions.users?.first?.();
            if (userArg) {
              if (cname === "kick" || cname === "ban" || cname === "mute" || cname === "unmute" || cname === "warn") {
                args.user = userArg;
                const rest = args.text.replace(/<@!?\d+>/g, "").trim();
                if (cname === "warn") args.reason = rest || "no reason given";
                else args.reason = rest || undefined;
              } else if (cname === "userinfo" || cname === "avatar") {
                args.user = userArg;
              }
            }
            if (cname === "unban") args.userid = (args.text.split(/\s+/)[0] || "").trim();
            if (cname === "clear") args.count = parseInt((args.text.split(/\s+/)[0] || "5"), 10);
            if (cname === "roll") args.dice = args.text;
            if (cname === "8ball") args.question = args.text;
            if (cname === "nick") args.name = args.text;
            if (cname === "say") args.text = args.text;
            if (cname === "setwelcome") {
              const chMention = raw.match(/<#(\d+)>/);
              const c = chMention ? { id: chMention[1] } : args.text;
              args.channel = c;
            }
            return runCommand(client, ctx, cname, args).catch((e) =>
              ctx.reply("⚠ " + String(e.message || e).slice(0, 300))
            );
          }
        }
        return;
      }
      const chanId = msg.channel.id;
      if (OWNER && msg.author.id !== OWNER) {
        await msg.channel.send("i only listen to the boss");
        return;
      }
      let payload;
      if (literal) payload = raw.slice(TRIGGER.length).trim() || "hi";
      else payload = raw.replace(/<@!?\d+>/g, "").replace(/^@jorge\b/i, "").trim() || "hi";
      if (/^chess\s+vs\b/i.test(payload)) {
        const tags = raw.match(/<@!?(\d+)>/g) || [];
        const ids = tags.map((t) => t.replace(/<@!?(\d+)>/, "$1"));
        const opp = ids.find((id) => id !== client.user.id);
        if (opp) {
          const name = msg.guild?.members?.cache?.get(opp)?.displayName || "a challenger";
          return bridgeCall(msg.author.id, "", msg.channel.id, "chess_challenge", `${opp} ${name}`)
            .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
            .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
        }
        if (ids.includes(client.user.id)) {
          const elo = (raw.match(/\b(\d{3,4})\b/) || [])[1] || "1200";
          return bridgeCall(msg.author.id, "", msg.channel.id, "chess_vs", elo)
            .then((r) => !r.aborted && r.reply !== null && msg.channel.send(r.reply).catch(() => {}))
            .catch((e) => msg.channel.send("⚠ " + String(e.message || e).slice(0, 500)).catch(() => {}));
        }
        return msg.channel.send("⚔️ Who do you want to challenge? Type `@jorge chess vs @user` (mention them).").catch(() => {});
      }
      console.log(`  < ${msg.author.username} (${msg.author.id}) in ${chanId}: ${payload.slice(0, 80)}`);
      if (ABORT_WORDS.has(payload.toLowerCase())) {
        const entry = activeBridges.get(chanId);
        if (entry && !entry.aborted) {
          entry.aborted = true;
          try {
            process.kill(-entry.child.pid, "SIGKILL");
          } catch (e) {
            try {
              entry.child.kill("SIGKILL");
            } catch (e2) {}
          }
          activeBridges.delete(chanId);
          await msg.channel.send("⏹ Task aborted.");
        } else {
          await msg.channel.send("No task is running right now.");
        }
        return;
      }
      if (payload.toLowerCase() === "!status") {
        await msg.channel.send("jorge is online ⚡ — @jorge research <topic>, @jorge brainstorm <topic>, or just talk to me.");
        return;
      }
      if (payload.toLowerCase() === "!quit") {
        await msg.channel.send("bye! 🤠");
        process.exit(0);
      }
      const research = payload.match(/^research\s+([\s\S]+)$/i);
      if (research) {
        await msg.channel.sendTyping();
        try {
          const res = await bridgeCall(msg.author.id, "", chanId, "research", research[1].trim() || "general");
          if (!res.aborted && res.reply !== null) await msg.channel.send(res.reply);
        } catch (e) {
          await msg.channel.send("⚠ " + String(e.message || e).slice(0, 500));
        }
        return;
      }
      const brainstorm = payload.match(/^brainstorm\s+([\s\S]+)$/i);
      if (brainstorm) {
        await msg.channel.sendTyping();
        try {
          const res = await bridgeCall(msg.author.id, "", chanId, "brainstorm", brainstorm[1].trim() || "general");
          if (!res.aborted && res.reply !== null) await msg.channel.send(res.reply);
        } catch (e) {
          await msg.channel.send("⚠ " + String(e.message || e).slice(0, 500));
        }
        return;
      }
      await msg.channel.sendTyping();
      try {
        const res = await bridgeCall(msg.author.id, payload, chanId);
        if (!res.aborted && res.reply !== null) await msg.channel.send(res.reply);
      } catch (e) {
        await msg.channel.send("⚠ " + String(e.message || e).slice(0, 500));
      }
    } catch (e) {
      console.error("  ✗ handler error:", String(e.message || e).slice(0, 200));
    }
  });

  client.on("guildMemberAdd", async (member) => {
    try {
      if (member.user.bot) return;
      const c = loadConfig();
      const chId = c[member.guild.id];
      if (!chId) return;
      const ch = member.guild.channels.cache.get(chId) || await member.guild.channels.fetch(chId).catch(() => null);
      if (!ch) return;
      await ch.send(`👋 Welcome **${member.user.tag}** to **${member.guild.name}**! Say @jorge hi to meet the resident bot.`);
    } catch (e) {}
  });

  client.once("ready", () => {
    console.log("\n  ✓ jorge is ONLINE on Discord. Say '@jorge hi'!");
    console.log(`  ✓ slash commands: ${COMMANDS.map((c) => "/" + c.name).join(" · ")}`);
    console.log(`  ✓ prefix commands: ?ping ?info ?help ?userinfo ?serverinfo ?avatar ?say ?roll ?flip ?8ball`);
    console.log(`  ✓ moderation: !clear !kick !ban !unban !mute !unmute !warn !nick !setwelcome`);
    console.log(`  ✓ owner-lock: ${OWNER ? "enabled (DISCORD_OWNER_ID)" : "OFF — anyone can use it"}`);
    registerCommands(client);
  });

  return client;
}

// login with privileged intents; degrade gracefully if they're disabled
const client = makeClient(buildIntents(true));
client.login(TOKEN).catch(async (err) => {
  if (err && /disallowed intents/i.test(String(err.message || err))) {
    console.error("  ✗ Privileged intents (Message Content / Server Members) are DISABLED.");
    console.error("  → discord.com/developers → Applications → Jorge → Bot → Privileged Gateway Intents");
    console.error("  → enable 'Message Content' and 'Server Members' → Save → retry.");
    console.error("  → Retrying with slash commands only (they don't need the intents)…");
    try {
      const client2 = makeClient(buildIntents(false));
      await client2.login(TOKEN);
      console.error("  ⚠ Online with slash commands only — '@jorge' text + welcome messages need the intents.");
    } catch (e) {
      console.error("  ✗ login failed even without privileged intents:", String(e.message || e));
      process.exit(1);
    }
  } else {
    console.error("  ✗ login failed:", String(err.message || err));
    process.exit(1);
  }
});

process.on("unhandledRejection", (reason) => {
  console.error("  ✗ unhandled rejection:", String(reason?.message || reason).slice(0, 200));
});