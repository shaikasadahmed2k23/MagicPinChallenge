"""
Landing page for GET /. Purely cosmetic — the judge harness only ever calls
the /v1/* JSON endpoints, never renders this. Kept in its own module so it
doesn't clutter bot.py's actual API logic.
"""

LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vera — magicpin AI Challenge</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Zilla+Slab:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --paper: #EEE8DA;
    --panel: #DDD4BD;
    --ink: #1C1B17;
    --ink-soft: #4A4436;
    --hairline: #B8AE95;
    --live: #2F6844;
    --stamp: #A23B2E;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: 'Zilla Slab', Georgia, 'Noto Serif', serif;
    font-size: 18px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  main {
    max-width: 640px;
    margin: 0 auto;
    padding: 56px 24px 80px;
  }

  .masthead {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 14px;
    margin-bottom: 36px;
  }

  .masthead h1 {
    margin: 0;
    font-size: 28px;
    font-weight: 600;
    letter-spacing: 0.01em;
  }

  .masthead .tag {
    font-size: 14px;
    color: var(--ink-soft);
    font-style: italic;
  }

  /* Hero: a real conversation snippet, not marketing copy */
  .thread {
    border: 1px solid var(--hairline);
    background: var(--panel);
    padding: 18px 20px;
    margin-bottom: 32px;
  }

  .thread .bubble {
    max-width: 86%;
    padding: 9px 13px;
    margin: 6px 0;
    border-radius: 3px;
    font-size: 15.5px;
    line-height: 1.45;
  }

  .thread .bubble.in {
    background: var(--paper);
    border: 1px solid var(--hairline);
  }

  .thread .bubble.out {
    background: var(--ink);
    color: var(--paper);
    margin-left: auto;
  }

  .thread .who {
    font-size: 11px;
    text-transform: none;
    color: var(--ink-soft);
    margin: 2px 2px 0;
  }

  .thread .who.out { text-align: right; }

  .thread .verdict {
    margin-top: 12px;
    padding-top: 10px;
    border-top: 1px dashed var(--hairline);
    font-size: 14px;
    color: var(--live);
    font-weight: 600;
  }

  p { color: var(--ink-soft); }
  p.lede { color: var(--ink); font-size: 19px; }

  section { margin: 34px 0; }

  hr.rule {
    border: none;
    border-top: 1px solid var(--hairline);
    margin: 34px 0;
  }

  h2 {
    font-size: 15px;
    font-weight: 600;
    margin: 0 0 12px;
    color: var(--ink);
  }

  /* Status ticket — the one place monospace is earned: it's literally
     rendering live system data, not decorating a label. */
  .ticket {
    font-family: 'IBM Plex Mono', 'SF Mono', Consolas, monospace;
    font-size: 13.5px;
    background: var(--ink);
    color: var(--paper);
    padding: 16px 18px;
    white-space: pre;
    overflow-x: auto;
  }

  .ticket .dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--live);
    margin-right: 8px;
    animation: pulse 2.2s ease-in-out infinite;
  }

  .ticket .dot.down { background: var(--stamp); animation: none; }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }

  @media (prefers-reduced-motion: reduce) {
    .ticket .dot { animation: none; }
  }

  ul.endpoints {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  ul.endpoints li {
    display: flex;
    gap: 14px;
    padding: 9px 0;
    border-bottom: 1px solid var(--hairline);
    font-size: 15px;
  }

  ul.endpoints li:last-child { border-bottom: none; }

  ul.endpoints code {
    font-family: 'IBM Plex Mono', Consolas, monospace;
    font-size: 13px;
    color: var(--ink);
    flex: 0 0 190px;
  }

  ul.endpoints span { color: var(--ink-soft); }

  footer {
    margin-top: 48px;
    padding-top: 18px;
    border-top: 2px solid var(--ink);
    font-size: 13.5px;
    color: var(--ink-soft);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
  }

  footer a { color: var(--ink); }

  a { color: var(--ink); }

  :focus-visible {
    outline: 2px solid var(--live);
    outline-offset: 2px;
  }
</style>
</head>
<body>
<main>

  <div class="masthead">
    <h1>Vera</h1>
    <span class="tag">merchant engagement, on WhatsApp</span>
  </div>

  <p class="lede">Vera watches for the moments a merchant actually needs to hear from
  magicpin — a compliance deadline, a milestone, a slow week — and decides whether
  to send, wait, or leave a conversation alone.</p>

  <div class="thread" aria-label="Example exchange">
    <div class="who">merchant</div>
    <div class="bubble in">Thank you for contacting us, our team will get back to you shortly.</div>
    <div class="who">merchant</div>
    <div class="bubble in">Thank you for contacting us, our team will get back to you shortly.</div>
    <div class="who">merchant</div>
    <div class="bubble in">Thank you for contacting us, our team will get back to you shortly.</div>
    <div class="verdict">Vera: recognised the third repeat — this is an
      unattended inbox, not a person. Conversation ended, no further messages sent.</div>
  </div>

  <section>
    <h2>Live status</h2>
    <div class="ticket" id="ticket">
      <span class="dot" id="dot"></span><span id="ticket-text">checking&hellip;</span>
    </div>
  </section>

  <hr class="rule">

  <section>
    <h2>Endpoints</h2>
    <ul class="endpoints">
      <li><code>POST /v1/context</code><span>receive category, merchant, customer, and trigger context</span></li>
      <li><code>POST /v1/tick</code><span>decide proactive actions for the active triggers</span></li>
      <li><code>POST /v1/reply</code><span>decide the next move on an incoming reply</span></li>
      <li><code>GET  /v1/healthz</code><span>liveness check</span></li>
      <li><code>GET  /v1/metadata</code><span>team and model info</span></li>
    </ul>
  </section>

  <footer>
    <span>Built solo by Shaik Asad Ahmed for the magicpin AI Challenge</span>
    <a href="https://github.com/shaikasadahmed2k23/MagicPinChallenge" target="_blank" rel="noopener">source</a>
  </footer>

</main>

<script>
(async () => {
  const dot = document.getElementById('dot');
  const text = document.getElementById('ticket-text');
  try {
    const [health, meta] = await Promise.all([
      fetch('/v1/healthz').then(r => r.json()),
      fetch('/v1/metadata').then(r => r.json()),
    ]);
    const uptimeMin = Math.floor((health.uptime_seconds || 0) / 60);
    const lines = [
      `status     ${health.status}`,
      `uptime     ${uptimeMin}m`,
      `team       ${meta.team_name}`,
      `model      ${meta.model}`,
    ];
    text.textContent = lines.join('\\n');
  } catch (e) {
    dot.classList.add('down');
    text.textContent = 'status     unreachable from this browser (cold start can take ~50s on the free tier — try refreshing)';
  }
})();
</script>

</body>
</html>"""
