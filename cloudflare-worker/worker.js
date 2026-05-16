export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerWorkflow(env));
  },

  // 方便測試：直接用 HTTP GET 觸發
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === '/trigger') {
      await triggerWorkflow(env);
      return new Response('Workflow triggered', { status: 200 });
    }
    return new Response('Macro News Trigger Worker', { status: 200 });
  }
};

async function triggerWorkflow(env) {
  const response = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/macro_cron.yml/dispatches`,
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
        'User-Agent': 'Cloudflare-Worker-MacroNews',
      },
      body: JSON.stringify({ ref: 'main' }),
    }
  );

  if (!response.ok) {
    const text = await response.text();
    console.error(`Trigger failed: ${response.status} — ${text}`);
  } else {
    console.log('Workflow dispatch sent successfully');
  }
}
