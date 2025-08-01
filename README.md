# WebArena Control Plane

Spin up short‑lived WebArena environments (Magento storefront/admin, Reddit-like forum, GitLab for now) as Docker containers using FastAPI.

## API

Base URL: `http://3.133.133.67:8000`

### List environments
```bash
curl "http://3.133.133.67:8000/environments"
# -> {"environments":["shopping","shopping_admin","reddit","gitlab"]}
```

### Create an environment
```bash
curl -X POST "http://3.133.133.67:8000/environments?environment_name=shopping"
# -> { env_id, environment_name, base_url, created_at, status }
```

### Check status
```bash
curl -X POST "http://3.133.133.67:8000/environments/<env_id>/status"
# -> { env_id, status, started_seconds_ago }
```

### Reset (recreate container, same host port)
```bash
curl -X POST "http://3.133.133.67:8000/environments/<env_id>/reset"
# -> { env_id, status: "restarting" }
```

## Notes
Returned site URLs look like `http://3.133.133.67:<random-port>` (30000 and 30100) and can be opened directly in a browser.
Post‑config fixes Magento/GitLab so they do not redirect to default hostnames.
- Returned site URLs look like `http://3.133.133.67:<random-port>` (30000 and 30100) and can be opened directly in a browser.
- Post‑config fixes Magento/GitLab so they do not redirect to default hostnames.
