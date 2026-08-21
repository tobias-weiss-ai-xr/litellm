# AI Infrastructure (Legion)

## Model Server

`minimax-proxy.service` — systemd service running on port **8001**.

Proxies requests to vLLM on the AI cluster (`192.168.0.27:8000`). Exposes model `deepseek-v4-flash` as an OpenAI-compatible endpoint.

```bash
# Service definition (/etc/systemd/system/minimax-proxy.service)
[Unit]
Description=MiniMax Reasoning Merger Proxy
After=network.target

[Service]
Type=simple
User=weiss
WorkingDirectory=/home/weiss
ExecStart=/usr/bin/python3 /home/weiss/minimax_proxy.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Source: `/home/weiss/minimax_proxy.py` — FastAPI app that:
- Rewrites all model IDs to `deepseek-v4-flash`
- Forwards to `http://192.168.0.27:8000/v1`
- Handles streaming with reasoning content passthrough
- Exposes `GET /v1/models` and `GET /health`

## Port Forwarders (socat)

Docker containers (`alpine/socat`) forwarding ports to remote hosts using host networking:

| Container | Host Port | Target |
|-----------|-----------|--------|
| `fwd-ai1` | 9101 | `192.168.0.27:9100` |
| `fwd-ai2` | 9102 | `192.168.0.176:9100` |

Created via:

```bash
docker run -d --restart unless-stopped --network host --name fwd-ai1 alpine/socat \
  socat TCP-LISTEN:9101,fork,reuseaddr TCP:192.168.0.27:9100

docker run -d --restart unless-stopped --network host --name fwd-ai2 alpine/socat \
  socat TCP-LISTEN:9102,fork,reuseaddr TCP:192.168.0.176:9100
```

## Proxy Config

In `litellm_proxy_config.yaml`, the local model entry routes to the minimax proxy:

```yaml
- model_name: local/deepseek-v4-flash
  litellm_params:
    model: openai/deepseek-v4-flash
    api_base: http://192.168.1.42:8001/v1
    api_key: none
```
