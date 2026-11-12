// Usage: node scripts/check_qt_indicator.mjs <shadow-trades-XXXX.js> <bars_EURUSD_M1.csv>
// Runs only the generated indicator (not Quant Terminal) and checks it against Quant Terminal's documented format.
// Checks a generated shadow-trades indicator against Quant Terminal's documented format.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const [indPath, barsPath] = process.argv.slice(2);
const mod = (await import(pathToFileURL(indPath).href)).default;
const lines = fs.readFileSync(barsPath, 'utf8').trim().split('\n').slice(1);
const all = lines.map((l) => { const [t, o, h, lo, c, v] = l.split(','); return { time: Date.parse(t) / 1000, open: +o, high: +h, low: +lo, close: +c, volume: +v }; });
const problems = [];
const check = (bars, label) => {
  const out = mod.calc(bars, {}, {});
  for (const o of mod.outputs) if (!Array.isArray(out[o.key]) || out[o.key].length !== bars.length) problems.push(`${label}: output ${o.key} has ${out[o.key]?.length} values for ${bars.length} bars`);
  const times = new Set(bars.map((b) => b.time));
  for (let i = 1; i < out.markers.length; i++) if (out.markers[i].time < out.markers[i - 1].time) problems.push(`${label}: markers not in time order`);
  for (const m of out.markers) if (!times.has(m.time)) problems.push(`${label}: marker at ${m.time} is not on a bar`);
  return out;
};
if (typeof mod.name !== 'string' || mod.name.length > 80) problems.push('name');
if (typeof mod.calc !== 'function') problems.push('calc');
const m1 = check(all, '1-minute');
// 15-minute bars built from the 1-minute ones, as Quant Terminal would.
const m15 = []; for (const b of all) { const t = b.time - (b.time % 900); const last = m15.at(-1); if (last && last.time === t) { last.high = Math.max(last.high, b.high); last.low = Math.min(last.low, b.low); last.close = b.close; } else m15.push({ ...b, time: t }); }
const q15 = check(m15, '15-minute');
check([], 'no bars'); check(all.slice(0, 5), 'first 5 bars');
const entries = m1.markers.filter((m) => m.shape !== 'circle').length, exits = m1.markers.filter((m) => m.shape === 'circle').length;
console.log(`1-minute: ${all.length} bars, ${entries} entry arrows, ${exits} exit circles, stop line on ${m1.stop.filter((x) => !Number.isNaN(x)).length} bars`);
console.log(`15-minute: ${m15.length} bars, ${q15.markers.length} markers`);
console.log('example markers:', JSON.stringify(m1.markers.slice(0, 2)));
console.log(problems.length ? 'PROBLEMS:\n' + problems.join('\n') : 'All checks passed.');
process.exit(problems.length ? 1 : 0);
