// Pulls the trainer's material and plan settings out of ../../index.html and writes bot/data.json,
// so the bot reuses exactly the same content and schedule rules as the web trainer.
// Usage: node bot/tools/extract_data.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");

// constants are taken one by one: `const NAME = …;` up to the first ";" that ends a line
const NAMES = ["PHASES", "MODS", "TOPICS", "QUESTIONS_GENERIC", "RESCUE", "CONNECT", "PHOTO_STEPS", "PHOTO_WORDS",
  "SCENES", "VOCAB", "GRAMS", "QUIZ", "EXAMDAY", "RTL_LESSONS", "RTL_BASICS", "RTL_EXAM", "RTL_OTHER", "RTL_PLAN",
  "LEVELS", "LEVEL_SKIP", "EXPRESS_MID", "MINS", "ROUTINE", "MODE_NAME", "REVIEW", "LEGACY_CFG", "FB", "CODE_WORDS"];
let src = "";
for (const n of NAMES) {
  const m = html.match(new RegExp("const " + n + "\\s*=\\s*[\\s\\S]*?;\\n"));
  if (!m) throw new Error(n + " not found in index.html");
  src += m[0];
}
// transcription helpers live in their own section
const t0 = html.indexOf("/* ============ TRANSCRIPTION ============ */");
const t1 = html.indexOf("/* ============ STATE ============ */");
if (t0 < 0 || t1 < 0) throw new Error("TRANSCRIPTION/STATE markers not found in index.html");
src += html.slice(t0, t1);
src += "\nglobalThis.__out = {" + NAMES.join(",") + ", tr};";
const ctx = { Date, console };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const d = ctx.__out, tr = d.tr;
const pair = p => ({ lb: p[0], ru: p[1], tr: tr(p[0]) });
const out = {
  phases: d.PHASES.map(p => ({ id: p.id, name: p.name, weeks: p.weeks, goal: p.goal })),
  modules: d.MODS.map(w => ({ n: w.n, title: w.t, grammar: w.g, topic: w.topic, listening: w.l, tasks: w.tasks })),
  plan: {
    levels: d.LEVELS.map(([key, label, about]) => ({ key, label, about })),
    level_skip: d.LEVEL_SKIP,
    express_mid: d.EXPRESS_MID,
    mins: d.MINS,
    routine: d.ROUTINE,
    mode_name: d.MODE_NAME,
    review: { title: d.REVIEW.t, grammar: d.REVIEW.g, topic_text: d.REVIEW.tt, listening: d.REVIEW.l, tasks: d.REVIEW.tasks },
    legacy_cfg: d.LEGACY_CFG,
  },
  sync: { key: d.FB.key, base: d.FB.base, words: d.CODE_WORDS },
  topics: d.TOPICS.map(t => ({ key: t.k, lb: t.lb, ru: t.ru, questions: t.q.map(pair), phrases: t.p.map(pair) })),
  questions_generic: d.QUESTIONS_GENERIC.map(pair),
  rescue: d.RESCUE.map(pair),
  connect: d.CONNECT.map(p => ({ lb: p[0], ru: p[1] })),
  photo_steps: d.PHOTO_STEPS.map(p => ({ step: p[0], lb: p[1], ru: p[2] })),
  photo_words: d.PHOTO_WORDS.map(pair),
  scenes: d.SCENES,
  vocab: d.VOCAB.map(v => ({ title: v.t, words: v.w.map(pair) })),
  grammar: d.GRAMS.map(g => ({ key: g.k, lb: g.lb, ru: g.ru, why: g.why, rules: g.rules, weeks: g.wk, examples: (g.ex || []).map(pair) })),
  quiz: d.QUIZ.map(q => ({ q: q.q, options: q.o, answer: q.a, explain: q.x })),
  exam_day: d.EXAMDAY,
  rtl: [
    ...d.RTL_LESSONS.map(([n, u, en, ru, dt, a]) => ({ id: "l" + n, kind: "lesson", n, url: u, en, ru, date: dt, audio: a })),
    ...d.RTL_BASICS.map(([n, u, en, ru, dt, a]) => ({ id: "b" + n, kind: "basics", n, url: u, en, ru, date: dt, audio: a })),
    { id: "exam", kind: "exam", n: null, url: d.RTL_EXAM[0], en: d.RTL_EXAM[1], ru: d.RTL_EXAM[2], date: d.RTL_EXAM[3], audio: false },
    ...d.RTL_OTHER.map(([u, en, ru, dt]) => ({ id: "o" + u.match(/(\d+)$/)[1], kind: "other", n: null, url: u, en, ru, date: dt, audio: false })),
  ].map(x => ({ ...x, module: d.RTL_PLAN[x.id] ?? null })),
};
fs.writeFileSync(path.join(__dirname, "..", "data.json"), JSON.stringify(out, null, 1) + "\n");
console.log("modules", out.modules.length, "topics", out.topics.length, "quiz", out.quiz.length, "rtl", out.rtl.length);
