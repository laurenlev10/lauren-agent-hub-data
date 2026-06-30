/* ===========================================================================
   VWAP View Worker — PUBLIC, READ-ONLY mirror of the trading analytics
   dashboard. The real dashboard (dashboard.themakeupblowout.com) is behind
   Cloudflare Access; this Worker serves the SAME page from the public raw
   files so an outsider can VIEW (not edit) it. No auth, no write.
   Deployed: vwap-view.laurenlev10.workers.dev
   =========================================================================== */
const RAW  = "https://raw.githubusercontent.com/laurenlev10/lauren-agent-hub-data/main/docs/trading/";
const PAGE = RAW + "analytics/index.html";

export default {
  async fetch(request) {
    const r = await fetch(PAGE + "?_=" + Date.now(), { cf: { cacheTtl: 30 } });
    if (!r.ok) return new Response("dashboard temporarily unavailable", { status: 502 });
    let html = await r.text();
    // rewrite the dashboard's relative data fetches -> absolute PUBLIC raw URLs
    html = html.split('"../journal-data.json"').join(JSON.stringify(RAW + "journal-data.json"));
    html = html.split('"../autotrade_enabled.json"').join(JSON.stringify(RAW + "autotrade_enabled.json"));
    html = html.split('"./backtest-data.json"').join(JSON.stringify(RAW + "analytics/backtest-data.json"));
    html = html.split('"../broker-ledger.json"').join(JSON.stringify(RAW + "broker-ledger.json"));
    // read-only: strip edit/save controls + a small banner
    const inject =
      "<style>.strategy-strip,#strategy-strip{display:none!important}" +
      "#__ro{position:sticky;top:0;z-index:9999;background:#0ea5e9;color:#fff;font:600 12px system-ui;" +
      "text-align:center;padding:5px;letter-spacing:.2px}</style>" +
      "<script>addEventListener('DOMContentLoaded',function(){" +
      "var b=document.createElement('div');b.id='__ro';b.textContent='\\uD83D\\uDC41\\uFE0F \\u05EA\\u05E6\\u05D5\\u05D2\\u05D4 \\u05DC\\u05E7\\u05E8\\u05D9\\u05D0\\u05D4 \\u05D1\\u05DC\\u05D1\\u05D3 (\\u05E0\\u05EA\\u05D5\\u05E0\\u05D9\\u05DD \\u05D7\\u05D9\\u05D9\\u05DD)';" +
      "document.body.insertBefore(b,document.body.firstChild);" +
      "document.querySelectorAll('.strategy-strip,#strategy-strip').forEach(function(e){e.style.display='none';});" +
      "document.querySelectorAll('button').forEach(function(x){var oc=(x.getAttribute('onclick')||'');" +
      "if(/save|Save|copyViewLink|Strategy|setStrategy|clearStrategy/.test(oc))x.style.display='none';});" +
      "});<\/script>";
    html = html.replace("</head>", inject + "</head>");
    return new Response(html, { headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Robots-Tag": "noindex, nofollow"
    }});
  }
};
