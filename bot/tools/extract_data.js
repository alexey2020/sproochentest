// Pulls the DATA and TRANSCRIPTION sections out of ../../index.html and writes bot/data.json,
// so the bot reuses exactly the same material as the web trainer.
// Usage: node bot/tools/extract_data.js
const fs = require("fs"), path = require("path"), vm = require("vm");
const root = path.join(__dirname, "..", "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const start = html.indexOf("/* ============ DATA ============ */");
const end = html.indexOf("/* ============ STATE ============ */");
if (start < 0 || end < 0) throw new Error("DATA/STATE markers not found in index.html");
const names = ["EXAM_DATE","PHASES","WEEKS","TOPICS","QUESTIONS_GENERIC","RESCUE","CONNECT","PHOTO_STEPS","PHOTO_WORDS","SCENES","VOCAB","GRAMS","QUIZ","EXAMDAY"];
let src = html.slice(start, end);
const ed = html.match(/const EXAMDAY\s*=\s*\[[\s\S]*?\];/);
if (ed) src += "\n" + ed[0];
src += "\nglobalThis.__out = {" + names.filter(n => new RegExp("const " + n + "\\b").test(src)).join(",") + ", tr};";
const ctx = { Date, console };
vm.createContext(ctx);
vm.runInContext(src, ctx);
const d = ctx.__out, tr = d.tr;
delete d.tr;
const pair = p => ({ lb: p[0], ru: p[1], tr: tr(p[0]) });
const out = {
  exam_date: [d.EXAM_DATE.getFullYear(), d.EXAM_DATE.getMonth() + 1, d.EXAM_DATE.getDate()],
  phases: d.PHASES.map(p => ({ name: p.name, range: p.range, weeks: p.weeks, goal: p.goal })),
  weeks: d.WEEKS.map(w => ({ start: [w.s[0], w.s[1] + 1, w.s[2]], title: w.t, grammar: w.g, topic: w.topic, listening: w.l, tasks: w.tasks })),
  topics: d.TOPICS.map(t => ({ key: t.k, lb: t.lb, ru: t.ru, questions: t.q.map(pair), phrases: t.p.map(pair) })),
  questions_generic: d.QUESTIONS_GENERIC.map(pair),
  rescue: d.RESCUE.map(pair),
  connect: d.CONNECT.map(p => ({ lb: p[0], ru: p[1] })),
  photo_steps: d.PHOTO_STEPS.map(p => ({ step: p[0], lb: p[1], ru: p[2] })),
  photo_words: d.PHOTO_WORDS.map(pair),
  scenes: d.SCENES,
  vocab: d.VOCAB.map(v => ({ title: v.t, words: v.w.map(pair) })),
  grammar: d.GRAMS.map(g => ({ key: g.k, lb: g.lb, ru: g.ru, why: g.why, rules: g.rules, examples: (g.ex || []).map(pair) })),
  quiz: d.QUIZ.map(q => ({ q: q.q, options: q.o, answer: q.a, explain: q.x })),
  exam_day: d.EXAMDAY || [],
};
fs.writeFileSync(path.join(__dirname, "..", "data.json"), JSON.stringify(out, null, 1) + "\n");
console.log("topics", out.topics.length, "vocab groups", out.vocab.length, "quiz", out.quiz.length, "weeks", out.weeks.length);
