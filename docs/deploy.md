# Deploy

The server is one small container. Anything that runs a container can host it.

## Before you deploy

1. **Point the image at your project.** In `Dockerfile`, change
   `ARG PROJECT=examples/library` to your folder (e.g. `ARG PROJECT=my-assistant`),
   or pass `--build-arg PROJECT=my-assistant`. The build runs
   `./gt check --production`, so a project with broken facts, or leftover
   template examples, never becomes an image. (`./gt serve` refuses the same
   things; `--force` overrides for local testing.)
2. **Make a key** and keep it out of git:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Set it as `GT_MCP_API_KEY` on the platform. Every request must then send
   `Authorization: Bearer <key>` (or `x-api-key: <key>`). `/healthz` stays open
   for load balancers. It shows counts, a data version, one page title (the
   watchdog searches for it), and the check status; nothing that isn't already
   public knowledge in your assistant.
3. **Serving without a key?** (Needed for ChatGPT and claude.ai connectors, which
   use OAuth or no auth.) Set `GT_ALLOWED_HOSTS=your.host`; otherwise the MCP SDK
   rejects requests for your domain with a 421 to prevent DNS rebinding.

Test locally first:

```bash
docker build -t my-assistant .
docker run --rm -p 8765:8765 -e GT_MCP_API_KEY=test-key my-assistant
./gt watchdog --url http://127.0.0.1:8765/mcp --api-key test-key
```

## Fly.io

```bash
fly launch --no-deploy                  # detects the Dockerfile; keep internal_port = 8765
fly secrets set GT_MCP_API_KEY=<key>
fly deploy
```

Your endpoint: `https://<app>.fly.dev/mcp`.

## Render

New > Web Service > your repo > runtime **Docker**. Add the environment variable
`GT_MCP_API_KEY`, set the port to `8765`, and use `/healthz` as the health check
path. Endpoint: `https://<service>.onrender.com/mcp`.

## Google Cloud Run

```bash
gcloud run deploy my-assistant --source . --port 8765 \
  --allow-unauthenticated --set-env-vars GT_MCP_API_KEY=<key>
```

`--allow-unauthenticated` turns off Google's IAM check; your API key does the
protecting. For production, store the key in Secret Manager
(`--set-secrets GT_MCP_API_KEY=<secret>:latest`).

## Azure Container Apps

```bash
az containerapp up --name my-assistant --resource-group my-rg --location eastus \
  --source . --ingress external --target-port 8765 \
  --env-vars GT_MCP_API_KEY=<key>
```

Move the key into a Container Apps secret once it works. Endpoint:
`https://<app>.<region>.azurecontainerapps.io/mcp`.

## A plain server (VPS)

```bash
docker run -d --restart unless-stopped --name my-assistant \
  -p 127.0.0.1:8765:8765 -e GT_MCP_API_KEY=<key> my-assistant
```

Put HTTPS in front with [Caddy](https://caddyserver.com) (it gets the
certificate for you):

```
# /etc/caddy/Caddyfile
mcp.example.org {
    reverse_proxy 127.0.0.1:8765
}
```

No Docker? `GT_MCP_HOST=0.0.0.0 GT_MCP_API_KEY=<key> ./gt serve` under systemd,
behind the same proxy.

## What `/healthz` means

- **200, `"ok": true`**: the server is serving a version that passed the checks.
- **`"checks": {"update_rejected": true, "first": "..."}`**: someone changed the
  files on the server and that change fails `./gt check`. It was not applied;
  the last good version is still serving. The watchdog fails on this so you
  hear about it.
- **503, `"ok": false`**: the server was started with `--force` on content that
  fails the checks. Load balancers will treat it as down, on purpose.

## Update data without redeploying

Rebuilding the image works, but you don't have to:

1. Set the repo variable `GT_PUBLISH_DATA=true`. `.github/workflows/publish-data.yml`
   then runs the gates on every push to main, builds a bundle (`./gt bundle`), and
   attaches it to a `data-latest` release. `./gt bundle` refuses a project that
   fails `./gt check --production`, because servers trust a verified bundle.
2. Start the server with
   `GT_DATA_URL=https://github.com/<you>/<repo>/releases/download/data-latest/groundtruth-data.tar.gz`
   (add `GT_DATA_TOKEN` for a private repo).

The server polls every `GT_DATA_POLL_SECONDS` (default 600), verifies every
checksum, and refuses a bundle that would cut a collection or the wiki in half
(set `GT_DATA_ALLOW_SHRINK=1` if that's intended). Then it switches over
atomically. Code changes still need a deploy.

## Watch it

Set the repo variable `GT_WATCHDOG_URL` to your `/mcp` URL (and the secret
`GT_MCP_API_KEY`). `.github/workflows/watchdog.yml` then checks it daily: health,
whether the live content passes `./gt check`, data age, a real MCP session, a
fact lookup, and a wiki search for a real page. A failed run emails you.

## Privacy

`logs/tool_calls.jsonl` records the arguments of every tool call, which means
what people searched for. Obvious secrets (`password`, `token`, ...) are
redacted, but treat the log as sensitive. Set `GT_LOG=0` to turn it off, or
`GT_LOG_DIR` to put it somewhere specific.
