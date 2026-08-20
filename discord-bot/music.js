const { createAudioPlayer, createAudioResource, joinVoiceChannel, AudioPlayerStatus, StreamType, NoSubscriberBehavior } = require("@discordjs/voice");
const { spawn } = require("child_process");
const os = require("os");
const fs = require("fs");

const YTDLP = fs.existsSync(os.homedir() + "/.local/bin/yt-dlp") ? os.homedir() + "/.local/bin/yt-dlp" : "yt-dlp";
const YT_ARGS = ["--js-runtimes", "node", "--extractor-args", "youtube:player_client=android"];
const STREAM_FMT = "best[ext=mp4]/best";

const players = new Map();

function getState(guildId, textChan) {
  let s = players.get(guildId);
  if (!s) {
    const player = createAudioPlayer({ behaviors: { noSubscriber: NoSubscriberBehavior.Play } });
    s = { player, queue: [], current: null, textChan: null, procs: [] };
    players.set(guildId, s);
    player.on(AudioPlayerStatus.Idle, () => next(guildId));
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

async function resolveTrack(query) {
  let q = query;
  let fromSpotify = false;
  if (/^(https?:\/\/)?(open\.spotify\.com\/|spotify:)/i.test(query)) {
    fromSpotify = true;
    const search = await spotifyToSearch(query);
    q = search ? "ytsearch1:" + search : query;
  } else {
    q = /^https?:\/\//.test(query) ? query : "ytsearch1:" + query;
  }
  return new Promise((resolve, reject) => {
    const p = spawn(YTDLP, [...YT_ARGS, "-f", STREAM_FMT, "--no-playlist", "--print", "%(title)s|%(url)s", q]);
    let data = "";
    p.stdout.on("data", (d) => (data += d));
    p.on("close", (code) => {
      const line = data.trim().split("\n")[0] || "";
      const i = line.indexOf("|");
      if (code === 0 && i > 0)
        resolve({ title: line.slice(0, i), url: line.slice(i + 1), fromSpotify });
      else reject(new Error("couldn't find that track"));
    });
  });
}

function createResource(s, url) {
  killProcs(s);
  const yt = spawn(YTDLP, [...YT_ARGS, "-f", STREAM_FMT, "-o", "-", url], { stdio: ["ignore", "pipe", "pipe"] });
  const ff = spawn("ffmpeg", ["-loglevel", "error", "-i", "pipe:0", "-f", "s16le", "-ar", "48000", "-ac", "2", "pipe:1"], { stdio: ["ignore", "pipe", "pipe"] });
  yt.stdout.pipe(ff.stdin);
  yt.stderr.on("data", () => {});
  ff.stderr.on("data", () => {});
  s.procs = [yt, ff];
  return createAudioResource(ff.stdout, { inputType: StreamType.Raw });
}

function next(guildId) {
  const s = players.get(guildId);
  if (!s) return;
  killProcs(s);
  const track = s.queue.shift();
  if (!track) {
    s.current = null;
    return;
  }
  s.current = track;
  try {
    s.player.play(createResource(s, track.url));
    if (s.textChan) s.textChan.send(`🎵 Now playing: **${track.title}** (by <@${track.requester}>)`).catch(() => {});
  } catch (e) {
    console.log("  [music] play failed:", e.message);
    next(guildId);
  }
}

async function play(msg, query) {
  const vc = msg.member?.voice?.channel;
  if (!vc) return msg.channel.send("🎵 Join a voice channel first!").catch(() => {});
  const s = getState(msg.guild.id, msg.channel);
  if (!s.connection) {
    s.connection = joinVoiceChannel({
      channelId: vc.id,
      guildId: msg.guild.id,
      adapterCreator: msg.guild.voiceAdapterCreator,
      selfDeaf: true,
    });
  }
  try {
    const track = await resolveTrack(query);
    s.queue.push({ ...track, requester: msg.author.id });
    const label = track.fromSpotify ? " (via YouTube — Spotify needs auth)" : "";
    if (!s.player.state.status || s.player.state.status === "idle") {
      next(msg.guild.id);
    } else {
      msg.channel.send(`➕ Queued: **${track.title}**${label}`).catch(() => {});
    }
  } catch (e) {
    msg.channel.send("🎵 " + String(e.message || e).slice(0, 300)).catch(() => {});
  }
}

function skip(msg) {
  const s = players.get(msg.guild.id);
  if (!s || !s.current) return msg.channel.send("🎵 nothing is playing").catch(() => {});
  msg.channel.send("⏭️ Skipped **" + s.current.title + "**").catch(() => {});
  s.player.stop();
  return null;
}

function stop(msg) {
  const s = players.get(msg.guild.id);
  if (!s) return msg.channel.send("🎵 nothing to stop").catch(() => {});
  s.queue = [];
  killProcs(s);
  s.player.stop();
  if (s.connection) {
    s.connection.destroy();
    s.connection = null;
  }
  s.current = null;
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
  s.player.stop();
  if (s.connection) {
    s.connection.destroy();
    s.connection = null;
  }
  s.current = null;
  return msg.channel.send("👋 Left the voice channel.").catch(() => {});
}

module.exports = { play, skip, stop, pause, resume, queue, leave };