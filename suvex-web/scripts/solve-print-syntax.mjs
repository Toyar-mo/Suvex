// 重解印刷语法色板：在"白底可读(>=4.5:1)"的约束下，最大化易混对的区分度。
// 教训来源：第一版凭感觉填的注释说 number↔string "在明度上分开"，实测明度比 1.00 —— 根本没分。

const hex2rgb = (h) => {
  h = h.replace('#', '')
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255)
}
const lin = (c) => (c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4))
const L = (h) => {
  const [r, g, b] = hex2rgb(h).map(lin)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
const R = (a, b) => {
  const l1 = L(a)
  const l2 = L(b)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}
const toHex = (r, g, b) =>
  '#' +
  [r, g, b]
    .map((v) =>
      Math.round(Math.max(0, Math.min(255, v)))
        .toString(16)
        .padStart(2, '0'),
    )
    .join('')
    .toUpperCase()

/** HSL → hex，方便按色相家族搜索 */
function hsl(h, s, l) {
  h /= 360
  s /= 100
  l /= 100
  const f = (n) => {
    const k = (n + h * 12) % 12
    const a = s * Math.min(l, 1 - l)
    return l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))
  }
  return toHex(f(0) * 255, f(8) * 255, f(4) * 255)
}

const BG = '#F5F6F8'

/**
 * 搜索：给定色相/饱和度，找一个满足目标对比度区间的明度。
 * 纸面是浅底深字 —— 明度越低对比度越高，所以"拉开明度"= 拉开对比度。
 */
function findHue(hue, sat, minRatio, maxRatio) {
  let best = null
  for (let l = 0; l <= 60; l += 0.5) {
    const c = hsl(hue, sat, l)
    const r = R(c, BG)
    if (r < minRatio) continue
    if (maxRatio !== undefined && r > maxRatio) continue
    // 取区间内最亮的（最舒服的）那个
    if (!best || r < best.ratio) best = { hex: c, ratio: r, l }
  }
  return best
}

console.log('=== 搜索：从色相家族出发，在可用对比度区间内取值 ===\n')

// 关键：白底上"可用对比度"约 4.5 ~ 16:1，对应明度区间极窄，
// 所以易混对只能靠"一个贴 4.5、另一个压深"来拉开，拉开幅度是有限的。
// 下面为每个 token 指定 目标对比度区间，区间本身就是为了成对分离而选的。

const plan = [
  // token      色相  饱和  目标对比度区间
  ['plain', 220, 15, [15, 20]], // 正文，最重
  ['comment', 220, 10, [5.0, 6.0]],
  ['meta', 220, 10, [5.0, 6.0]],
  ['keyword', 250, 62, [5.3, 6.0]], // violet 蓝紫
  ['string', 150, 62, [6.6, 7.6]], // 深绿，压深以与 number 分离
  ['number', 35, 70, [4.6, 5.1]], // 赭棕，贴下限以与 string 分离
  ['function', 212, 75, [6.6, 7.6]], // 中蓝，压深以与 property 分离
  ['property', 212, 72, [4.6, 5.1]], // 浅蓝，贴下限
  ['variable', 285, 60, [6.9, 7.8]], // 紫品红，压深以与 keyword 分离
  ['operator', 220, 12, [5.4, 6.2]],
]

const solved = {}
for (const [name, hue, sat, [min, max]] of plan) {
  const got = findHue(hue, sat, min, max)
  if (!got) {
    console.log(`  ${name.padEnd(9)} ✗ 该色相/饱和下找不到落在 ${min}~${max}:1 的值`)
    continue
  }
  solved[name] = got.hex
  console.log(
    `  ${name.padEnd(9)} ${got.hex}  ${got.ratio.toFixed(2)}:1  (HSL ${hue}/${sat}/${got.l}%)`,
  )
}

console.log('\n=== 全部达标检查（对 ' + BG + '）===')
let bad = 0
for (const [k, v] of Object.entries(solved)) {
  const r = R(v, BG)
  if (r < 4.5) {
    bad++
    console.log(`  ${k.padEnd(9)} ${v}  ${r.toFixed(2)}:1 ✗`)
  }
}
console.log(bad === 0 ? '  全部 >= 4.5:1 ✓' : `  ${bad} 项不达标`)

console.log('\n=== 易混对的分离度 ===')
const pairs = [
  ['number', 'string', 1.25],
  ['function', 'property', 1.25],
  ['keyword', 'variable', 1.15],
  ['comment', 'plain', 2.0],
]
for (const [a, b, want] of pairs) {
  const la = L(solved[a])
  const lb = L(solved[b])
  const ratio = (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
  console.log(
    `  ${a} ↔ ${b}   明度比 ${ratio.toFixed(2)}  ${ratio >= want ? '✓' : '✗ 需 >=' + want}`,
  )
}

console.log('\n=== 可直接粘贴的 PRINT_SYNTAX ===')
for (const [k, v] of Object.entries(solved)) {
  console.log(`  ${k}: '${v}',  // ${R(v, BG).toFixed(2)}:1`)
}
