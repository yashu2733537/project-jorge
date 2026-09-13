const { createAudioPlayer, createAudioResource, joinVoiceChannel, AudioPlayerStatus, StreamType, NoSubscriberBehavior } = require("@discordjs/voice");
const { spawn } = require("child_process");
const os = require("os");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const YTDLP = fs.existsSync(os.homedir() + "/.local/bin/yt-dlp") ? os.homedir() + "/.local/bin/yt-dlp" : "yt-dlp";
const YT_ARGS = ["--js-runtimes", "node", "--impersonate", "chrome", "--force-ipv4"];
const STREAM_FMT = "best[ext=m4a]/best[ext=mp4]/bestaudio/best";
const CACHE_DIR = path.join(__dirname, "music_cache");
fs.mkdirSync(CACHE_DIR, { recursive: true });

function cacheKeyFor(query) {
  return crypto.createHash("sha1").update(query).digest("hex").slice(0, 16);
}

function cachedTrack(query) {
  const key = cacheKeyFor(query);
  const files = fs.readdirSync(CACHE_DIR).filter((f) => f.startsWith(key + ".") && !f.endsWith(".txt"));
  if (files.length === 0) return null;
  let title = "", url = "";
  try {
    const t = fs.readFileSync(path.join(CACHE_DIR, key + ".txt"), "utf8");
    const sep = t.lastIndexOf("|");
    if (sep > 0) { title = t.slice(0, sep); url = t.slice(sep + 1); }
    else title = t;
  } catch (e) {}
  return { file: path.join(CACHE_DIR, files[0]), title, url };
}

function downloadTrack(query, onFresh, onSpawn, isStale) {
  const key = cacheKeyFor(query);
  const partialFiles = () => fs.readdirSync(CACHE_DIR).filter((f) => f.startsWith(key + ".") && !f.endsWith(".txt"));
  const wipePartial = () => { for (const f of partialFiles()) { try { fs.unlinkSync(path.join(CACHE_DIR, f)); } catch (e) {} } };
  return new Promise((resolve, reject) => {
    const attempt = (n) => {
      if (n > 2) return reject(new Error("download failed after retries"));
      if (isStale && isStale()) return reject(new Error("cancelled"));
      if (onFresh) onFresh();
      const p = spawn(YTDLP, [...YT_ARGS, "--no-simulate", "-f", STREAM_FMT, "--no-part", "-o", path.join(CACHE_DIR, key + ".%(ext)s"), "--print", "after_dl:%(title)s|%(id)s", query], { stdio: ["ignore", "pipe", "pipe"] });
      if (onSpawn) onSpawn(p);
      let printed = "";
      p.stdout.on("data", (d) => (printed += d));
      p.stderr.on("data", (d) => console.log("  [music] yt-dlp:", String(d).slice(0, 200)));
      let done = false;
      let lastSize = -1;
      let lastGrow = Date.now();
      const started = Date.now();
      const iv = setInterval(() => {
        if (done) return;
        if (isStale && isStale()) {
          done = true;
          clearInterval(iv);
          p.kill("SIGKILL");
          wipePartial();
          return reject(new Error("cancelled"));
        }
        if (Date.now() - started > 180000) {
          console.log("  [music] download timed out (3min), retrying #" + (n + 1));
          done = true;
          clearInterval(iv);
          p.kill("SIGKILL");
          wipePartial();
          return attempt(n + 1);
        }
        const files = partialFiles();
        if (files.length === 0) return;
        const sz = fs.statSync(path.join(CACHE_DIR, files[0])).size;
        if (sz > lastSize) { lastSize = sz; lastGrow = Date.now(); }
        else if (Date.now() - lastGrow > 20000) {
          console.log("  [music] download stalled 20s, retrying #" + (n + 1));
          done = true;
          clearInterval(iv);
          p.kill("SIGKILL");
          wipePartial();
          attempt(n + 1);
        }
      }, 3000);
      p.on("error", (e) => { if (!done) { done = true; reject(e); } });
      p.on("close", (code) => {
        if (done) return;
        done = true;
        clearInterval(iv);
        const files = partialFiles();
        if (code === 0 && files.length > 0) {
          const file = path.join(CACHE_DIR, files[0]);
          const tail = printed.trim().split("\n").pop() || "";
          const sep = tail.lastIndexOf("|");
          const title = sep >= 0 ? tail.slice(0, sep) : tail;
          const id = sep >= 0 ? tail.slice(sep + 1) : "";
          const url = id ? `https://www.youtube.com/watch?v=${id}` : "";
          try { fs.writeFileSync(path.join(CACHE_DIR, key + ".txt"), title + (url ? "|" + url : "")); } catch (e) {}
          resolve({ file, title, url });
        } else {
          wipePartial();
          if (isStale && isStale()) return reject(new Error("cancelled"));
          attempt(n + 1);
        }
      });
    };
    attempt(0);
  });
}

const players = new Map();

function getState(guildId, textChan) {
  let s = players.get(guildId);
  if (!s) {
    const player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Play } });
    player.on("stateChange", (o, n) => {
      if (o.status !== n.status) console.log("  [music] player:", o.status, "->", n.status);
    });
    s = { player, queue: [], current: null, textChan: null, procs: [], downloading: false, volume: 100, resource: null, loop: "off", autoplay: false, mix: [], skipManual: false };
    players.set(guildId, s);
    player.on(AudioPlayerStatus.Idle, () => {
      console.log("  [music] IDLE fired (current:", s.current ? s.current.title.slice(0, 30) : "null", ")");
      next(guildId);
    });
    player.on("error", (e) => {
      console.log("  [music] player error:", e.message);
      killProcs(s);
      next(guildId);
    });
  }
  if (textChan) s.textChan = textChan;
  return s;
}

function killProcs(s) {
  for (const p of s.procs) {
    try { p.kill(); } catch (e) {}
  }
  s.procs = [];
}

async function spotifyToSearch(query) {
  try {
    const url = /^https?:\/\//i.test(query)
      ? query
      : query.replace(/^spotify:/i, "").replace(/(track|playlist|album|artist):/i, "https://open.spotify.com/$1/");
    const res = await fetch("https://open.spotify.com/oembed?url=" + encodeURIComponent(url));
    if (!res.ok) throw new Error("oembed " + res.status);
    const j = await res.json();
    const parts = [j.title, j.description && j.description !== j.title ? j.description : ""].filter(Boolean);
    return parts.join(" ").trim();
  } catch (e) {
    return "";
  }
}

function createResource(s, filePath) {
  const ff = spawn("ffmpeg", ["-loglevel", "error", "-i", filePath, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"], { stdio: ["ignore", "pipe", "pipe"] });
  ff.stderr.on("data", (d) => console.log("  [music] ffmpeg:", String(d).slice(0, 200)));
  ff.on("close", (c) => console.log("  [music] ffmpeg exited", c));
  s.procs = [ff];
  const res = createAudioResource(ff.stdout, { inputType: StreamType.Raw, inlineVolume: true });
  if (res.volume) res.volume.setVolume((s.volume || 100) / 100);
  s.resource = res;
  return res;
}

function autoplayQuery(title) {
  return String(title || "")
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/\([^)]*\)/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .split(/\s+/)
    .slice(0, 4)
    .join(" ");
}

function searchMix(query) {
  return new Promise((resolve) => {
    const p = spawn(YTDLP, [...YT_ARGS, "--flat-playlist", "--playlist-end", "5", "-J", "ytsearch5:" + query], { stdio: ["ignore", "pipe", "pipe"] });
    p.stderr.on("data", (d) => console.log("  [music] mix:", String(d).slice(0, 150)));
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    const timer = setTimeout(() => { try { p.kill("SIGKILL"); } catch (e) {} }, 20000);
    p.on("close", () => {
      clearTimeout(timer);
      try {
        const j = JSON.parse(out);
        resolve((j.entries || []).map((e) => ({ title: String(e.title || "").trim(), url: e.id ? `https://www.youtube.com/watch?v=${e.id}` : "" })).filter((e) => e.url));
      } catch (e) { resolve([]); }
    });
    p.on("error", () => resolve([]));
  });
}

async function refillAutoplay(s) {
  const cur = s.current;
  const q = autoplayQuery(cur && cur.title);
  if (!q) return null;
  while (s.mix.length === 0) {
    const entries = await searchMix(q);
    s.mix = entries.filter((e) => e.title.toLowerCase() !== String(cur.title).toLowerCase());
    if (s.mix.length === 0) return null;
  }
  const e = s.mix.shift();
  return { title: e.title, query: e.url, requester: cur.requester, auto: true };
}

async function next(guildId) {
  const s = players.get(guildId);
  if (!s) return;
  const manual = s.skipManual;
  s.skipManual = false;
  killProcs(s);
  if (!manual && s.current && s.loop === "all") s.queue.push(s.current);
  let track = s.queue.shift();
  if (!track && !manual && s.current && s.loop === "one") track = s.current;
  if (!track && s.autoplay && s.current) {
    try {
      track = await refillAutoplay(s);
    } catch (e) {
      console.log("  [music] autoplay failed:", e.message);
    }
  }
  if (!track) {
    s.current = null;
    console.log("  [music] next: queue empty -> staying in VC");
    return;
  }
  s.current = track;
  s.downloading = true;
  try {
    if (!track.file) {
      const cached = cachedTrack(track.query);
      const dl = await downloadTrack(track.query, () => {
        if (s.textChan) s.textChan.send(`⏳ Downloading **${track.title}**…`).catch(() => {});
      }, (p) => { s.procs.push(p); }, () => s.current !== track);
      if (s.current !== track) return;
      track.file = dl.file;
      track.title = dl.title || track.title;
      track.url = dl.url || track.url;
      if (cached && s.textChan) s.textChan.send(`💾 **${track.title}** is already saved — playing it.`).catch(() => {});
    }
    s.player.play(createResource(s, track.file));
    if (s.textChan) s.textChan.send(`🎵 Now playing: **${track.title}** ${track.auto ? "⏭ (autoplay)" : `(by <@${track.requester}>)`}`).catch(() => {});
  } catch (e) {
    console.log("  [music] play failed:", e.message);
    if (s.current === track && s.textChan) s.textChan.send("🎵 Failed to play **" + track.title + "**: " + String(e.message || e).slice(0, 200)).catch(() => {});
  } finally {
    s.downloading = false;
  }
}

async function play(msg, query) {
  let member = msg.member;
  if (!member || !member.voice) {
    try {
      member = await msg.guild.members.fetch(msg.author.id);
    } catch (e) {}
  }
  const vc = member?.voice?.channel;
  if (!vc) return msg.channel.send("🎵 Join a voice channel first!").catch(() => {});
  const s = getState(msg.guild.id, msg.channel);
  if (!s.connection) {
    s.connection = joinVoiceChannel({
      channelId: vc.id,
      guildId: msg.guild.id,
      adapterCreator: msg.guild.voiceAdapterCreator,
      selfDeaf: true,
    });
    s.connection.on("stateChange", (o, n) => {
      console.log("  [music] conn:", o.status, "->", n.status);
    });
    s.connection.subscribe(s.player);
    if (vc.bitrate && vc.bitrate < 96000) {
      vc.setBitrate(96000).catch(() => {});
    }
  }
  try {
    let q = query;
    if (/^(https?:\/\/)?(open\.spotify\.com\/|spotify:)/i.test(query)) {
      const search = await spotifyToSearch(query);
      q = search ? "ytsearch1:" + search : query;
    } else if (!/^https?:\/\//.test(query)) {
      q = "ytsearch1:" + query;
    }
    const track = { title: query, query: q, requester: msg.author.id };
    s.queue.push(track);
    const status = s.player.state.status;
    if (!s.downloading && (!status || status === "idle" || status === "autopaused")) {
      next(msg.guild.id);
    } else {
      msg.channel.send(`➕ Queued: **${query}**`).catch(() => {});
    }
  } catch (e) {
    msg.channel.send("🎵 " + String(e.message || e).slice(0, 300)).catch(() => {});
  }
}

function skip(msg) {
  const s = players.get(msg.guild.id);
  if (!s || !s.current) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  msg.channel.send("⏭️ Skipped **" + s.current.title + "**").catch(() => {});
  s.skipManual = true;
  killProcs(s);
  s.player.stop(true);
  return null;
}

function stop(msg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 nothing to stop").catch(() => {});
  s.queue = [];
  killProcs(s);
  s.player.stop(true);
  if (s.connection) {
    s.connection.destroy();
    s.connection = null;
  }
  s.current = null;
  s.resource = null;
  return msg.channel.send("⏹️ Stopped and left the voice channel.").catch(() => {});
}

function pause(msg) {
  const s = players.get(msg.guild.id);
  if (!s || !s.current) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  s.player.pause();
  return msg.channel.send("⏸️ Paused.").catch(() => {});
}

function resume(msg) {
  const s = players.get(msg.guild.id);
  if (!s || !s.current) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  s.player.unpause();
  return msg.channel.send("▶️ Resumed.").catch(() => {});
}

function queue(msg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 queue is empty").catch(() => {});
  const now = s.current ? "🎵 Now: **" + s.current.title + "**\n" : "";
  const q = s.queue.map((t, i) => `${i + 1}. ${t.title} (by <@${t.requester}>)`).join("\n");
  return msg.channel.send((now + (q ? "Up next:\n" + q : "")).slice(0, 1900) || "🎵 queue is empty").catch(() => {});
}

function leave(msg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 I'm not in a voice channel").catch(() => {});
  s.queue = [];
  killProcs(s);
  s.player.stop(true);
  if (s.connection) {
    s.connection.destroy();
    s.connection = null;
  }
  s.current = null;
  s.resource = null;
  return msg.channel.send("👋 Left the voice channel.").catch(() => {});
}

function volume(msg, arg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  const v = parseInt(arg, 10);
  if (isNaN(v)) {
    return msg.channel.send(`🔊 Volume is **${s.volume}%** (max 200). Use \`?volume <0-200>\``).catch(() => {});
  }
  s.volume = Math.max(0, Math.min(200, v));
  if (s.resource && s.resource.volume) s.resource.volume.setVolume(s.volume / 100);
  const n = 20;
  let bar = "";
  for (let i = 0; i < n; i++) bar += i < Math.round((s.volume / 200) * n) ? "▰" : "▱";
  return msg.channel.send(`🔊 Volume set to **${s.volume}%** ${bar}`).catch(() => {});
}

function loop(msg, arg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  const now = s.loop || "off";
  let nxt;
  if (arg === undefined) nxt = now === "off" ? "one" : now === "one" ? "all" : "off";
  else if (["off", "one", "all"].includes(String(arg).toLowerCase())) nxt = String(arg).toLowerCase();
  else return msg.channel.send("🎵 loop mode must be `off`, `one` or `all`").catch(() => {});
  s.loop = nxt;
  const desc = nxt === "off" ? "loop off" : nxt === "one" ? "🔁 repeating this track" : "🔁 repeating the whole queue";
  return msg.channel.send(`🎵 Loop: **${nxt}** — ${desc}`).catch(() => {});
}

function shuffle(msg) {
  const s = players.get(msg.guild.id);
  if (!s || s.queue.length === 0) return msg.channel.send("🎵 queue is empty").catch(() => {});
  for (let i = s.queue.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [s.queue[i], s.queue[j]] = [s.queue[j], s.queue[i]];
  }
  return msg.channel.send(`🔀 Shuffled **${s.queue.length}** tracks.`).catch(() => {});
}

function autoplay(msg, arg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  if (arg !== undefined && String(arg).trim() !== "") s.autoplay = /^(on|1|true|yes|y)$/i.test(String(arg).trim());
  else s.autoplay = !s.autoplay;
  return msg.channel.send(`⏭️ Autoplay is now **${s.autoplay ? "ON" : "OFF"}** — when the queue ends, similar tracks keep playing.`).catch(() => {});
}

module.exports = { play, skip, stop, pause, resume, queue, leave, volume, loop, shuffle, autoplay };