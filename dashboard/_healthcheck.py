import subprocess, json, time


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


print("=== CONTAINERS ===")
out = run('docker ps --filter "name=litellm" --filter "name=docker-stats" --format "{{.Names}} {{.Status}}"')
print(out if out else "  (empty)")

print("\n=== PROXY ===")
print("  Health:", run("curl -s http://localhost:4000/health/liveliness"))

out = run(
    'curl -s http://localhost:4000/v1/models -H "Authorization: Bearer sk-b55922061536c9f9c19532d904871f2f9a90894e5df34b6ed73b41150c09b817"'
)
try:
    d = json.loads(out)
    models = sorted(set(m["id"] for m in d["data"]))
    print(f"  Models ({len(models)}):")
    for m in models:
        print(f"    {m}")
    emb = [m for m in models if "embed" in m.lower()]
    print(f"  Embedding: {'YES - ' + emb[0] if emb else 'NOT LOADED'}")
except Exception as e:
    print(f"  Models: ERROR - {e}")

print("\n=== DASHBOARD ===")
t0 = time.time()
out = run("curl -s -D- http://localhost:3000/stats/api/all -o NUL 2>&1")
print(f"  First: {time.time() - t0:.3f}s {'HIT' if 'X-Cache: HIT' in out else 'MISS'}")
t0 = time.time()
out = run("curl -s -D- http://localhost:3000/stats/api/all -o NUL 2>&1")
print(f"  Cached: {time.time() - t0:.3f}s {'HIT' if 'X-Cache: HIT' in out else 'MISS'}")

print("\n=== REDIS ===")
out = run("docker exec litellm-redis redis-cli -a litellm INFO stats 2>&1")
for line in out.splitlines():
    if any(k in line for k in ["keyspace_hits", "keyspace_misses", "evicted_keys"]):
        print(f"  {line.strip()}")
out = run("docker exec litellm-redis redis-cli -a litellm CONFIG GET maxmemory 2>&1")
print(f"  {out}")

print("\n=== POSTGRES ===")
print(f"  Ready: {run('docker exec litellm-postgres pg_isready -U litellm 2>&1')}")
print(
    f"  shared_buffers: {run('docker exec litellm-postgres psql -U litellm -d litellm -t -A -c "SHOW shared_buffers" 2>&1')}"
)
print(
    f"  effective_cache: {run('docker exec litellm-postgres psql -U litellm -d litellm -t -A -c "SHOW effective_cache_size" 2>&1')}"
)
print(f"  work_mem: {run('docker exec litellm-postgres psql -U litellm -d litellm -t -A -c "SHOW work_mem" 2>&1')}")

print("\n=== RESOURCES ===")
out = run('docker stats --no-stream --format "{{.Name}} {{.CPUPerc}} {{.MemUsage}}" 2>&1')
for line in out.splitlines():
    if any(
        k in line for k in ["litellm-proxy", "litellm-redis", "litellm-postgres", "litellm-dashboard", "docker-stats"]
    ):
        print(f"  {line}")

print("\n=== CACHE CONFIG ===")
out = run(
    'curl -s http://localhost:4000/cache/ping -H "Authorization: Bearer sk-b55922061536c9f9c19532d904871f2f9a90894e5df34b6ed73b41150c09b817" 2>&1'
)
try:
    d = json.loads(out)
    print(f"  TTL: {d.get('litellm_cache_params', 'N/A')}")
    print(f"  Status: {d.get('status', 'N/A')}")
except:
    print(f"  {out[:200]}")
