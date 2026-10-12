# Deploy

The server is one small container. Anything that runs a container can host it.

## Before you deploy

1. **It builds your project.** `docker build .` uses the project `./gt` points
   at (`groundtruth.project`, set by `./gt init` and `./gt use`); pass
   `--build-arg PROJECT=folder` for another. It refuses to build the library demo
   unless you pass `--build-arg ALLOW_DEMO=1`. The build runs
   `./gt check --production`, so a project with broken or ungrounded facts, or
   leftover template examples, never becomes an image. (`./gt serve` refuses the
   same things; `--force` overrides for local testing.)
2. **Make a key** and keep it out of git:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Set it as `GT_MCP_API_KEY` on the platform. Every request must then send
   `Authorization: Bearer <key>` (or `x-api-key: <key>`). `/healthz` stays open
   for load balancers, but without the key it shows only whether it's healthy,
   the project's name, the build commit, and the data version; the details go to
   callers with the key (below).
3. **Stamp the build.** Pass `--build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD)`,
   or set `GT_BUILD_COMMIT` in the server's environment (the recipes below do).
   `/healthz` then reports which commit the code came from, so the watchdog and
   the refresh host can tell you when the running code falls behind main.
4. **Serving without a key?** (Needed for ChatGPT and claude.ai connectors, which
   use OAuth or no auth.) Set `GT_ALLOWED_HOSTS=your.host`; otherwise the MCP SDK
   rejects requests for your domain with a 421 to prevent DNS rebinding.

Once it's deployed, record where in your project's `groundtruth.yaml`, so the
watchdog, `./gt status`, and `./gt connect` know:

```yaml
url: https://your.host/mcp
```

Test locally first:

```bash
docker build -t my-assistant --build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD) .
docker run --rm -p 8765:8765 -e GT_MCP_API_KEY=test-key my-assistant
./gt watchdog --url http://127.0.0.1:8765/mcp --api-key test-key
```

## Fly.io

No prompts, so a coding agent can run it as is (app names are global on Fly,
hence the suffix):

```bash
APP=my-assistant-$(python3 -c "import secrets; print(secrets.token_hex(3))")
fly launch --name "$APP" --org personal --region iad --internal-port 8765 --no-deploy --yes
fly secrets set --app "$APP" --stage GT_MCP_API_KEY="$KEY"
fly deploy --app "$APP" --remote-only --build-arg GT_BUILD_COMMIT=$(git rev-parse HEAD)
git add fly.toml && git commit -m "Deploy to Fly.io" && git push
```

Your endpoint: `https://$APP.fly.dev/mcp`. (Flag names can shift between flyctl
versions; `fly launch --help` has the current ones.) To redeploy automatically
when the code changes, see [below](#redeploy-when-the-code-changes).

## Render

New > Web Service > your repo > runtime **Docker**. Add the environment variable
`GT_MCP_API_KEY`, set the port to `8765`, and use `/healthz` as the health check
path. Endpoint: `https://<service>.onrender.com/mcp`. Render tells the server
which commit it built (`RENDER_GIT_COMMIT`), so there's nothing to stamp.

## Google Cloud Run

```bash
gcloud run deploy my-assistant --source . --port 8765 \
  --allow-unauthenticated --set-env-vars GT_MCP_API_KEY=<key>,GT_BUILD_COMMIT=$(git rev-parse HEAD)
```

`--allow-unauthenticated` turns off Google's IAM check; your API key does the
protecting. For production, store the key in Secret Manager
(`--set-secrets GT_MCP_API_KEY=<secret>:latest`).

## Azure Container Apps

```bash
az containerapp up --name my-assistant --resource-group my-rg --location eastus \
  --source . --ingress external --target-port 8765 \
  --env-vars GT_MCP_API_KEY=<key> GT_BUILD_COMMIT=$(git rev-parse HEAD)
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

## Redeploy when the code changes

Data reaches the server as bundles, but new groundtruth code needs a new image.
`.github/workflows/deploy.yml` rebuilds and deploys whenever a push to main
touches the code, if you give it the command. For Fly.io:

```bash
gh variable set GT_DEPLOY_PLATFORM --body fly
gh variable set GT_DEPLOY_COMMAND --body 'flyctl deploy --app APP --remote-only --build-arg GT_BUILD_COMMIT=$GITHUB_SHA'
fly tokens create deploy --app APP | gh secret set FLY_API_TOKEN
```

It runs the unit tests and `./gt check --production` first. For another
platform, set `GT_DEPLOY_COMMAND` to its deploy command, and add the CLI and
its secret to the workflow.

## A chat page for your users

MCP clients need someone to connect them. To give the people the assistant is
for something to use right away, set these on the server:

```bash
GT_CHAT=1
ANTHROPIC_API_KEY=...        # the chat's model; GT_MODEL picks it (default claude-opus-5-5)
```

Then `https://your.host/chat` (and `/`) is a small web chat. Each question runs
through Claude with exactly the tools MCP clients get and the same instructions
(`./gt prompt`), so it answers the way `./gt eval --tier live` grades, and every
answer lists the sources its tools returned. It's public on purpose and never
needs the MCP key. It limits each visitor to `GT_CHAT_PER_MINUTE` (6) questions a
minute and the server to `GT_CHAT_PER_DAY` (2000) a day, so a busy day can't run
up the bill. Set a spend limit on the key at console.anthropic.com as well.

## What `/healthz` means

Without the key, `/healthz` shows only what a load balancer needs: `ok`, the
project's name, the groundtruth version, the build `commit`, and the
`data_version` and when it was built. With the key (`Authorization: Bearer ...`,
as `./gt watchdog` sends it), it adds the check details, row counts, a page title
the watchdog searches for, and the data updater's last result.

- **200, `"ok": true`**: the server is serving a version that passed the checks.
- **`"checks": {"update_rejected": true, "first": "..."}`**: someone changed the
  files on the server and that change fails `./gt check`. It was not applied;
  the last good version is still serving. The watchdog fails on this so you
  hear about it.
- **503, `"ok": false`**: the server was started with `--force` on content that
  fails the checks. Load balancers will treat it as down, on purpose.
- **`"data_update": {"last_error": "..."}`**: the server couldn't install the
  latest data bundle (bad download, checksum, a bundle older than the one it has,
  or one that would cut a collection in half). It keeps the data it has, and the
  watchdog fails.

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
fact lookup, a wiki search for a real page, and (from a stamped build) whether
the running code is more than 72 hours behind main. It also checks the repo
every day, URL or not: open alerts in `ALERTS.md`, CI on main, and, with a
refresh host, that a refresh landed recently ([operations.md](operations.md)).
A failed run emails you.

## Privacy

`logs/tool_calls.jsonl` records the arguments of every tool call, which means
what people searched for. Obvious secrets (`password`, `token`, ...) are
redacted, but treat the log as sensitive. Set `GT_LOG=0` to turn it off, or
`GT_LOG_DIR` to put it somewhere specific.
