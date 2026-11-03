/**
 * NV BOT - Minecraft bridge
 * بيدخل سيرفر Forge 1.20.1، وبيتحكم فيه بايثون عن طريق HTTP محلي.
 * أي حاجة مهمة بتتطبع على stdout كسطر JSON عشان بايثون يقراها.
 */
const http = require('http')
const fs = require('fs')
const path = require('path')
const mineflayer = require('mineflayer')
const { pathfinder, Movements, goals } = require('mineflayer-pathfinder')

let mcViewer = null
try { mcViewer = require('prismarine-viewer').mineflayer } catch (e) { /* العرض الحي اختياري */ }

const env = process.env
const CFG = {
  host: env.MC_HOST,
  port: parseInt(env.MC_PORT || '25565'),
  username: env.MC_USERNAME || 'NV_BOT',
  auth: env.MC_AUTH || 'offline',
  version: env.MC_VERSION || '1.20.1',
  api: parseInt(env.BRIDGE_PORT || '3100'),
  secret: env.BRIDGE_SECRET || '',
  viewerPort: parseInt(env.VIEWER_PORT || '3007'),
  debug: env.FORGE_DEBUG === '1',
  tp: env.TP_TO_TARGET === '1'
}
const REC_DIR = path.join(__dirname, '..', 'recordings')
fs.mkdirSync(REC_DIR, { recursive: true })

const emit = (ev, data = {}) => console.log(JSON.stringify({ ev, ...data }))
process.on('uncaughtException', (e) => emit('crash', { error: String(e && e.stack || e) }))
process.on('unhandledRejection', (e) => emit('crash', { error: String(e && e.stack || e) }))

/* ------------------------------------------------------------------ */
/* Forge 1.20.1 login handshake (fml:loginwrapper)                     */
/* ------------------------------------------------------------------ */
const readVarInt = (b, o) => { let n = 0, s = 0, x; do { x = b[o.i++]; n |= (x & 0x7f) << s; s += 7 } while (x & 0x80); return n }
const readStr = (b, o) => { const l = readVarInt(b, o); const s = b.toString('utf8', o.i, o.i + l); o.i += l; return s }
const varInt = (n) => { const out = []; do { let t = n & 0x7f; n >>>= 7; if (n) t |= 0x80; out.push(t) } while (n); return Buffer.from(out) }
const str = (s) => { const b = Buffer.from(s, 'utf8'); return Buffer.concat([varInt(b.length), b]) }

function forgeHandshake (client) {
  client.on('login_plugin_request', (p) => {
    const reply = (data) => client.write('login_plugin_response', { messageId: p.messageId, data })
    if (p.channel !== 'fml:loginwrapper') return reply(undefined)
    try {
      if (CFG.debug) emit('forge_raw', { hex: p.data.toString('hex') })
      const o = { i: 0 }
      const inner = readStr(p.data, o)
      const len = readVarInt(p.data, o)
      const payload = p.data.subarray(o.i, o.i + len)
      const po = { i: 0 }
      const disc = readVarInt(payload, po)
      let out
      if (disc === 1) { // S2CModList  ->  C2SModListReply
        const mods = []; let n = readVarInt(payload, po)
        while (n--) mods.push(readStr(payload, po))
        const chans = []; n = readVarInt(payload, po)
        while (n--) { const k = readStr(payload, po); const v = readStr(payload, po); chans.push([k, v]) }
        out = Buffer.concat([
          varInt(2),
          varInt(mods.length), ...mods.map(str),
          varInt(chans.length), ...chans.flatMap(([k, v]) => [str(k), str(v)]),
          varInt(0) // registries
        ])
        emit('forge', { mods: mods.length, channels: chans.length })
      } else {
        out = varInt(99) // acknowledge (registry / config data)
      }
      reply(Buffer.concat([str(inner), varInt(out.length), out]))
    } catch (e) {
      emit('forge_error', { error: String(e) })
      reply(undefined)
    }
  })
}

/* ------------------------------------------------------------------ */
/* Recorder                                                            */
/* ------------------------------------------------------------------ */
let rec = { stream: null, start: 0, file: null }
function recOpen () {
  const d = new Date(); const p = (n) => String(n).padStart(2, '0')
  const name = `session-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.jsonl`
  rec = { stream: fs.createWriteStream(path.join(REC_DIR, name), { flags: 'a' }), start: Date.now(), file: name }
  recWrite('meta', { server: `${CFG.host}:${CFG.port}`, bot: CFG.username, started: d.toISOString() })
}
function recWrite (type, data = {}) {
  if (!rec.stream) return
  rec.stream.write(JSON.stringify({ t: Date.now() - rec.start, type, ...data }) + '\n')
}
function recClose () { if (rec.stream) { recWrite('end'); rec.stream.end() } rec = { stream: null, start: 0, file: null } }

/* ------------------------------------------------------------------ */
/* Bot                                                                 */
/* ------------------------------------------------------------------ */
let bot = null
let wantOnline = false
let reconnectTimer = null
const S = {
  watch: { target: null, rotate: false, minutes: 5, timer: null, idx: -1, since: 0 },
  afk: true, stealth: 'off', manual: false
}
const skipPlayers = new Set()

function join () {
  if (bot) return { ok: false, error: 'البوت داخل أو بيحاول يدخل بالفعل' }
  wantOnline = true
  clearTimeout(reconnectTimer)
  bot = mineflayer.createBot({
    host: CFG.host, port: CFG.port, username: CFG.username, auth: CFG.auth, version: CFG.version,
    fakeHost: `${CFG.host}\0FML3\0`, // ده الي بيعرّف السيرفر إننا كلاينت Forge
    hideErrors: false, checkTimeoutInterval: 60 * 1000
  })
  forgeHandshake(bot._client)
  bot.loadPlugin(pathfinder)
  const me = bot

  me.once('login', () => emit('login'))
  me.once('spawn', () => {
    recOpen()
    emit('spawned', { recording: rec.file })
    const mv = new Movements(me); mv.canDig = false; mv.allow1by1towers = false
    me.pathfinder.setMovements(mv)
    if (mcViewer) {
      try { mcViewer(me, { port: CFG.viewerPort, firstPerson: true, viewDistance: 4 }) } catch (e) { emit('viewer_error', { error: String(e) }) }
    }
    if (S.stealth !== 'off') applyStealth(S.stealth)
  })
  me.on('messagestr', (m) => { if (m.trim()) recWrite('chat', { msg: m }) })
  me.on('playerJoined', (p) => { recWrite('join', { name: p.username }); emit('player_join', { name: p.username }) })
  me.on('playerLeft', (p) => { recWrite('leave', { name: p.username }); emit('player_leave', { name: p.username }) })
  me.on('death', () => recWrite('death', { name: CFG.username }))
  me.on('entityDead', (e) => { if (e.type === 'player' && e.username) recWrite('death', { name: e.username }) })
  me.on('entityHurt', (e) => { if (e.type === 'player' && e.username) recWrite('hurt', { name: e.username }) })
  me.on('entitySwingArm', (e) => { if (e.type === 'player' && e.username === S.watch.target) recWrite('swing', { name: e.username }) })
  me.on('blockUpdate', (o, n) => {
    const t = S.watch.target && me.players[S.watch.target] && me.players[S.watch.target].entity
    if (!t || !n || !o || o.name === n.name || n.position.distanceTo(t.position) > 8) return
    recWrite('block', { x: n.position.x, y: n.position.y, z: n.position.z, from: o.name, to: n.name })
  })
  me.on('kicked', (r) => { emit('kicked', { reason: typeof r === 'string' ? r : JSON.stringify(r) }); recWrite('kicked', { reason: String(r) }) })
  me.on('error', (e) => emit('mc_error', { error: String(e && e.message || e) }))
  me.once('end', (reason) => {
    emit('ended', { reason: String(reason) })
    try { me.viewer && me.viewer.close && me.viewer.close() } catch (e) { /* ignore */ }
    clearInterval(posTimer); clearInterval(tickTimer); clearInterval(afkTimer)
    recClose()
    bot = null
    if (wantOnline) { reconnectTimer = setTimeout(join, 30000); emit('reconnecting', { in: 30 }) }
  })

  posTimer = setInterval(samplePositions, 1000)
  tickTimer = setInterval(watchTick, 250)
  afkTimer = setInterval(afkTick, 15000)
  return { ok: true }
}

function leave () {
  wantOnline = false
  clearTimeout(reconnectTimer)
  stopRotate()
  if (!bot) return { ok: true, note: 'مش داخل أصلا' }
  try { bot.quit() } catch (e) { /* ignore */ }
  return { ok: true }
}

let posTimer, tickTimer, afkTimer

function samplePositions () {
  if (!bot || !bot.entity) return
  const r1 = (n) => Math.round(n * 10) / 10
  const list = [[CFG.username, r1(bot.entity.position.x), r1(bot.entity.position.y), r1(bot.entity.position.z)]]
  for (const [n, p] of Object.entries(bot.players)) {
    if (n !== CFG.username && p.entity) list.push([n, r1(p.entity.position.x), r1(p.entity.position.y), r1(p.entity.position.z)])
  }
  recWrite('pos', { p: list, w: S.watch.target })
}

/* ---------- مراقبة لاعب ---------- */
function setTarget (name) {
  S.watch.target = name
  S.watch.since = Date.now()
  S.watch.following = false
  if (name) {
    recWrite('watch_start', { target: name })
    emit('watch', { target: name })
    if (CFG.tp && bot) bot.chat(`/tp ${CFG.username} ${name}`)
  } else {
    recWrite('watch_stop', {})
    if (bot && bot.pathfinder) bot.pathfinder.setGoal(null)
  }
}

function watchTick () {
  if (!bot || !bot.entity || S.manual || !S.watch.target) return
  const p = bot.players[S.watch.target]
  const t = p && p.entity
  if (!t) { if (!S.watch.lost) { S.watch.lost = true; recWrite('target_not_visible', { target: S.watch.target }) } return }
  S.watch.lost = false
  bot.lookAt(t.position.offset(0, t.height, 0), true)
  if (!S.watch.following) { bot.pathfinder.setGoal(new goals.GoalFollow(t, 3), true); S.watch.following = true }
}

function onlineNames () {
  if (!bot) return []
  return Object.keys(bot.players).filter((n) => n !== CFG.username && !skipPlayers.has(n)).sort()
}

function rotateNext () {
  const all = onlineNames()
  if (!all.length) return setTarget(null)
  const visible = all.filter((n) => bot.players[n].entity)
  const pool = (CFG.tp ? all : (visible.length ? visible : all))
  S.watch.idx = (S.watch.idx + 1) % pool.length
  setTarget(pool[S.watch.idx])
}

function startRotate (minutes) {
  stopRotate()
  S.watch.rotate = true
  S.watch.minutes = minutes || 5
  rotateNext()
  S.watch.timer = setInterval(rotateNext, S.watch.minutes * 60 * 1000)
}
function stopRotate () {
  S.watch.rotate = false
  clearInterval(S.watch.timer); S.watch.timer = null
}

/* ---------- AFK + إخفاء ---------- */
function afkTick () {
  if (!bot || !bot.entity || !S.afk || S.watch.target || S.manual) return
  const a = Math.floor(Math.random() * 4)
  bot.look(bot.entity.yaw + (Math.random() - 0.5) * 1.5, (Math.random() - 0.5) * 0.6, false)
  if (a === 0) { bot.setControlState('jump', true); setTimeout(() => bot && bot.setControlState('jump', false), 300) }
  if (a === 1) { bot.setControlState('forward', true); setTimeout(() => bot && bot.setControlState('forward', false), 700) }
  if (a === 2) { bot.setControlState('sneak', true); setTimeout(() => bot && bot.setControlState('sneak', false), 800) }
  if (a === 3) bot.swingArm()
}

function applyStealth (mode) {
  S.stealth = mode
  if (!bot) return
  if (mode === 'off') { bot.chat('/gamemode survival'); bot.chat(`/effect clear ${CFG.username} minecraft:invisibility`) }
  if (mode === 'spectator') bot.chat('/gamemode spectator')
  if (mode === 'creative') bot.chat('/gamemode creative')
  if (mode === 'invisible') bot.chat(`/effect give ${CFG.username} minecraft:invisibility 999999 0 true`)
}

/* ------------------------------------------------------------------ */
/* HTTP API (محلي بس)                                                  */
/* ------------------------------------------------------------------ */
function status () {
  const online = !!(bot && bot.entity)
  return {
    online, connecting: !!bot && !online, username: CFG.username, server: `${CFG.host}:${CFG.port}`,
    health: online ? bot.health : null, food: online ? bot.food : null,
    pos: online ? ['x', 'y', 'z'].map((k) => Math.round(bot.entity.position[k])) : null,
    players: bot ? Object.keys(bot.players).filter((n) => n !== CFG.username) : [],
    watch: { target: S.watch.target, rotate: S.watch.rotate, minutes: S.watch.minutes, lost: !!S.watch.lost },
    afk: S.afk, stealth: S.stealth, recording: rec.file, viewer: !!mcViewer
  }
}

function handle (a) {
  switch (a.action) {
    case 'status': return status()
    case 'join': return join()
    case 'leave': return leave()
    case 'players': return { players: bot ? Object.keys(bot.players).filter((n) => n !== CFG.username) : [] }
    case 'watch': if (!bot) return { ok: false, error: 'البوت مش داخل' }; stopRotate(); setTarget(a.name); return { ok: true }
    case 'unwatch': stopRotate(); setTarget(null); return { ok: true }
    case 'rotate': if (!bot) return { ok: false, error: 'البوت مش داخل' }; a.on ? startRotate(a.minutes) : (stopRotate(), setTarget(null)); return { ok: true }
    case 'skip': a.on ? skipPlayers.add(a.name) : skipPlayers.delete(a.name); return { ok: true, skipped: [...skipPlayers] }
    case 'stealth': applyStealth(a.mode); return { ok: true }
    case 'afk': S.afk = !!a.on; return { ok: true }
    case 'say': if (!bot) return { ok: false, error: 'البوت مش داخل' }; bot.chat(String(a.msg).slice(0, 250)); return { ok: true }
    case 'move':
      if (!bot) return { ok: false }
      S.manual = true; stopRotate(); setTarget(null)
      bot.setControlState(a.dir, !!a.on)
      if (!a.on && !['forward', 'back', 'left', 'right', 'jump', 'sneak', 'sprint'].some((d) => bot.controlState[d])) S.manual = false
      return { ok: true }
    case 'look':
      if (!bot || !bot.entity) return { ok: false }
      bot.look(bot.entity.yaw + (a.dyaw || 0), Math.max(-1.5, Math.min(1.5, bot.entity.pitch + (a.dpitch || 0))), true)
      return { ok: true }
    default: return { ok: false, error: 'أمر غير معروف' }
  }
}

http.createServer((req, res) => {
  if (req.method !== 'POST' || req.url !== '/api' || req.headers['x-secret'] !== CFG.secret) { res.writeHead(403); return res.end() }
  let body = ''
  req.on('data', (c) => { body += c; if (body.length > 1e5) req.destroy() })
  req.on('end', () => {
    let out
    try { out = handle(JSON.parse(body || '{}')) } catch (e) { out = { ok: false, error: String(e) } }
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(out))
  })
}).listen(CFG.api, '127.0.0.1', () => emit('bridge_ready', { port: CFG.api }))
