// MTool 谷歌翻译代理（带 tk 签名 + 会话Cookie 反爬增强版）
// 思路参考 UniClawAI/google_translate：
// 1) 先访问 translate 主页拿会话 Cookie 并全局缓存
// 2) 对每条文本用 TKK 计算 tk 签名
// 3) 带 Cookie + tk + 浏览器头请求，显著降低被判定为机器人(429)的概率
// 4) 对 MTool 保持 /translate_a/single 原始数组返回格式，上层无感知
// 5) 仍保留多 TLD 端点轮换兜底

const TKK = [406604, 1836941114];

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";

const ENDPOINTS = [
  "translate.googleapis.com",
  "translate.google.com",
  "translate.google.com.hk",
  "translate.google.co.jp",
  "translate.google.co.kr",
  "translate.google.co.uk",
  "translate.google.com.tw",
  "translate.google.ca",
  "translate.google.com.au",
];

// ---------- tk 签名算法（Google Translate 经典 TKK 算法的 JS 实现） ----------
function rl(a, b) {
  for (let c = 0; c < b.length - 2; c += 3) {
    let d = b[c + 2];
    d = d >= "a" ? d.charCodeAt(0) - 87 : Number(d);
    d = b[c + 1] === "+" ? a >>> d : a << d;
    a = b[c] === "+" ? (a + d) & 0xffffffff : a ^ d;
  }
  return a;
}

function calcTk(text) {
  const t1 = TKK[0];
  const t2 = TKK[1];
  const bytes = [];
  for (let f = 0; f < text.length; f++) {
    let g = text.charCodeAt(f);
    if (g < 128) bytes.push(g);
    else if (g < 2048) bytes.push((g >> 6) | 192, (g & 63) | 128);
    else if ((g & 0xfc00) === 0xd800 && f + 1 < text.length) {
      const g2 = text.charCodeAt(++f);
      g = 0x10000 + ((g & 0x3ff) << 10) + (g2 & 0x3ff);
      bytes.push((g >> 18) | 240, ((g >> 12) & 63) | 128, ((g >> 6) & 63) | 128, (g & 63) | 128);
    } else bytes.push((g >> 12) | 224, ((g >> 6) & 63) | 128, (g & 63) | 128);
  }
  let a = t1;
  for (const v of bytes) {
    a += v;
    a = rl(a, "+-a^+6");
  }
  a = rl(a, "+-3^+b+-f");
  a = a ^ t2;
  if (a < 0) a = (a & 0x7fffffff) + 0x80000000;
  a %= 1e6;
  return a + "." + (a ^ t1);
}

// ---------- 全局 Cookie 缓存（每个边缘节点独立） ----------
let cookieCache = { value: "", exp: 0 };

async function getCookies() {
  const now = Date.now();
  if (cookieCache.value && now < cookieCache.exp) return cookieCache.value;
  const collected = [];
  // 从主页拿 NID/AEC 等会话 cookie
  for (const host of ["translate.google.com", "translate.googleapis.com"]) {
    try {
      const r = await fetch(`https://${host}/`, {
        headers: { "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9" },
        redirect: "follow",
      });
      const sc = r.headers.get("set-cookie");
      // Workers 中 set-cookie 可能被合并，尽力解析 name=value
      if (sc) collected.push(sc.split(";")[0]);
      await r.body?.cancel();
    } catch (e) {}
  }
  const cookie = collected.join("; ");
  cookieCache = { value: cookie, exp: now + 30 * 60 * 1000 };
  return cookie;
}

function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "") {
      return new Response("MTool Google Translate Proxy (tk+cookie anti-bot)", {
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
    if (!url.pathname.startsWith("/translate_a/")) {
      return new Response("Forbidden", { status: 403 });
    }

    const q = url.searchParams.get("q") || "";
    let cookie = "";
    try {
      cookie = await getCookies();
    } catch (e) {}

    let lastStatus = 429;
    // 最多两轮：第一轮失败则强制刷新 Cookie 后整轮重试一次
    for (let round = 0; round < 2; round++) {
      for (const host of shuffle(ENDPOINTS)) {
        try {
          const up = new URL(`https://${host}${url.pathname}`);
          for (const [k, v] of url.searchParams) up.searchParams.append(k, v);
          if (q) up.searchParams.set("tk", calcTk(q));

          const h = new Headers();
          h.set("User-Agent", UA);
          h.set("Accept", "application/json, text/plain, */*");
          h.set("Accept-Language", "en-US,en;q=0.9,zh-CN;q=0.8");
          h.set("Referer", `https://${host}/`);
          if (cookie) h.set("Cookie", cookie);

          const resp = await fetch(up.toString(), {
            method: "GET",
            headers: h,
            redirect: "follow",
          });
          if (resp.status === 429 || resp.status === 503) {
            lastStatus = resp.status;
            continue;
          }
          const rh = new Headers(resp.headers);
          rh.set("Access-Control-Allow-Origin", "*");
          rh.set("X-Upstream-Host", host);
          return new Response(resp.body, { status: resp.status, headers: rh });
        } catch (e) {
          lastStatus = 502;
          continue;
        }
      }
      // 第一轮全军覆没，刷新 cookie 再来一轮
      cookieCache.exp = 0;
      try { cookie = await getCookies(); } catch (e) {}
    }
    return new Response(
      JSON.stringify({ error: "rate-limited after tk+cookie retry" }),
      { status: lastStatus, headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" } }
    );
  },
};
