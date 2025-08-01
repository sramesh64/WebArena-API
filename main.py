import os, time, random, socket, string
from datetime import datetime, timezone
from typing import Dict

import httpx
import docker
from docker.errors import NotFound
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
import threading

load_dotenv()
WORKER_PUBLIC_HOST = os.getenv("WORKER_PUBLIC_HOST", "").strip()

PORT_POOL_START = int(os.getenv("PORT_POOL_START", "30000"))
PORT_POOL_END   = int(os.getenv("PORT_POOL_END", "30100"))

ENV_IMAGES = {
    "shopping":        {"image": "shopping_final_0712",               "internal_port": 80},
    "shopping_admin":  {"image": "shopping_admin_final_0719",         "internal_port": 80},
    "reddit":          {"image": "postmill-populated-exposed-withimg","internal_port": 80},
    "gitlab":          {"image": "gitlab-populated-final-port8023",   "internal_port": 8023,
                        "start_cmd": "/opt/gitlab/embedded/bin/runsvdir-start"},
}

client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
app = FastAPI(title="WebArena Control Plane")
instances: Dict[str, Dict] = {}

class CreateEnvResponse(BaseModel):
    env_id: str
    environment_name: str
    base_url: str
    created_at: str
    status: str

class StatusResponse(BaseModel):
    env_id: str
    status: str
    started_seconds_ago: int

def rand_id(prefix="env_", n=8):
    return prefix + "".join(random.choices(string.hexdigits.lower(), k=n))

def allocate_port() -> int:
    for _ in range(400):
        p = random.randint(PORT_POOL_START, PORT_POOL_END)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError("No free port in pool")

def docker_exec(container, cmd: str):
    rc, out = container.exec_run(["bash", "-lc", cmd], user="root")
    txt = out.decode("utf-8", "ignore")
    if rc != 0:
        raise RuntimeError(f"exec failed (rc={rc}): {cmd}\n--- output ---\n{txt}\n--------------")
    return txt

def start_instance(env_name: str, env_id: str, host_port: int) -> str:
    tpl = ENV_IMAGES[env_name]
    image = tpl["image"]
    internal_port = tpl["internal_port"]
    start_cmd = tpl.get("start_cmd")
    cname = f"{env_id}-{env_name}"
    container = client.containers.run(
        image=image,
        name=cname,
        detach=True,
        ports={f"{internal_port}/tcp": host_port},
        command=start_cmd if start_cmd else None,
        labels={"webarena.managed": "true", "webarena.env_id": env_id, "webarena.env_name": env_name},
        mem_limit="8g",
    )
    return container.id

def _magento_fix_all(c, base_url: str):
    base = base_url.rstrip('/') + '/'
    php = "php "
    cmds = [
        f'{php}/var/www/magento2/bin/magento config:set web/unsecure/base_url "{base}"',
        f'{php}/var/www/magento2/bin/magento config:set web/secure/base_url "{base}"',
        f'{php}/var/www/magento2/bin/magento config:set web/url/redirect_to_base 0',
        f'{php}/var/www/magento2/bin/magento config:set web/seo/use_rewrites 0',
    ]
    for cmd in cmds:
        docker_exec(c, cmd)

    sql = f"""
mysql -u magentouser -pMyPassword magentodb -e "
UPDATE core_config_data
SET value='{base}'
WHERE path IN (
 'web/unsecure/base_url','web/secure/base_url'
);
INSERT INTO core_config_data (scope,scope_id,path,value)
SELECT 'default',0,'web/url/redirect_to_base','0' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM core_config_data WHERE path='web/url/redirect_to_base');
UPDATE core_config_data SET value='0' WHERE path='web/url/redirect_to_base';
INSERT INTO core_config_data (scope,scope_id,path,value)
SELECT 'default',0,'web/seo/use_rewrites','0' FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM core_config_data WHERE path='web/seo/use_rewrites');
UPDATE core_config_data SET value='0' WHERE path='web/seo/use_rewrites';
"
"""
    docker_exec(c, sql)

    try:
        docker_exec(c, f'{php}/var/www/magento2/bin/magento cache:flush')
    except Exception:
        pass
    docker_exec(c, 'rm -rf /var/www/magento2/var/cache/* /var/www/magento2/var/page_cache/* || true')
    docker_exec(c, 'command -v redis-cli >/dev/null 2>&1 && redis-cli FLUSHALL || true')

def post_config(env_name: str, container_id: str, base_url: str):
    c = client.containers.get(container_id)
    for attempt in range(8):
        try:
            if env_name in ("shopping", "shopping_admin"):
                _magento_fix_all(c, base_url)
            elif env_name == "gitlab":
                docker_exec(c, f"sed -i \"s|^external_url.*|external_url '{base_url}'|\" /etc/gitlab/gitlab.rb")
                time.sleep(20)
                docker_exec(c, "gitlab-ctl reconfigure")
            # reddit: no post-config

            head = docker_exec(c, "curl -I -sS http://127.0.0.1/ | egrep -i '^HTTP/|^Location:' || true")
            if "metis.lti.cs.cmu.edu" not in head:
                break
        except Exception as e:
            if attempt == 7:
                print(f"[post_config] final failure for {env_name}: {e}")
        time.sleep(5)

async def http_ok(url: str, timeout: float = 6.0) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            r = await http.get(url)
            return r.status_code < 500
    except Exception:
        return False

@app.get("/environments")
def list_envs():
    return {"environments": list(ENV_IMAGES.keys())}

@app.post("/environments", response_model=CreateEnvResponse)
def create_env(environment_name: str):
    name = environment_name.strip().lower()
    if name not in ENV_IMAGES:
        raise HTTPException(400, f"Unknown environment '{name}'. Options: {list(ENV_IMAGES)}")

    env_id = rand_id()
    host_port = allocate_port()
    base_url = f"http://{WORKER_PUBLIC_HOST}:{host_port}"
    created_at = datetime.now(timezone.utc)

    try:
        container_id = start_instance(name, env_id, host_port)
    except Exception as e:
        raise HTTPException(500, f"Docker run failed: {e}")

    instances[env_id] = {
        "env_id": env_id,
        "environment_name": name,
        "container_id": container_id,
        "host_port": host_port,
        "base_url": base_url,
        "created_at": created_at,
        "status": "starting",
    }

    def _bg():
        try:
            time.sleep(5)
            post_config(name, container_id, base_url)
        except Exception:
            pass

    threading.Thread(target=_bg, daemon=True).start()

    return CreateEnvResponse(
        env_id=env_id,
        environment_name=name,
        base_url=base_url,
        created_at=created_at.isoformat(),
        status="starting",
    )

@app.post("/environments/{env_id}/status", response_model=StatusResponse)
async def env_status(env_id: str):
    inst = instances.get(env_id)
    if not inst:
        raise HTTPException(404, "env_id not found")
    ok = await http_ok(inst["base_url"])
    inst["status"] = "running" if ok else "starting"
    started_secs = int((datetime.now(timezone.utc) - inst["created_at"]).total_seconds())
    return StatusResponse(env_id=env_id, status=inst["status"], started_seconds_ago=started_secs)

@app.post("/environments/{env_id}/reset")
def env_reset(env_id: str):
    inst = instances.get(env_id)
    if not inst:
        raise HTTPException(404, "env_id not found")
    try:
        c = client.containers.get(inst["container_id"])
        c.stop(timeout=60)
        c.remove(force=True)
    except NotFound:
        pass
    try:
        container_id = start_instance(inst["environment_name"], env_id, inst["host_port"])
        inst["container_id"] = container_id
        inst["status"] = "starting"
        inst["created_at"] = datetime.now(timezone.utc)
        time.sleep(5)
        post_config(inst["environment_name"], container_id, inst["base_url"])
    except Exception as e:
        raise HTTPException(500, f"reset failed: {e}")
    return {"env_id": env_id, "status": "restarting"}

@app.on_event("shutdown")
def stop_containers():
    if hasattr(app.state, "reaper_stop"):
        app.state.reaper_stop.set()
    try:
        for c in client.containers.list(all=True, filters={"label": "webarena.managed=true"}):
            try:
                c.stop(timeout=30)
                c.remove(force=True)
            except Exception:
                pass
    except Exception:
        pass

@app.on_event("startup")
def reconcile_existing():
    for c in client.containers.list(all=True, filters={"label": "webarena.managed=true"}):
        labels = c.labels or {}
        env_id = labels.get("webarena.env_id")
        env_name = labels.get("webarena.env_name")
        if not env_id or not env_name:
            continue
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {})
        host_port = None
        for k, v in ports.items():
            if v and isinstance(v, list) and "HostPort" in v[0]:
                host_port = int(v[0]["HostPort"])
                break
        if not host_port:
            continue
        base_url = f"http://{WORKER_PUBLIC_HOST}:{host_port}"
        created_at = datetime.now(timezone.utc)
        if env_id not in instances:
            instances[env_id] = {
                "env_id": env_id, "environment_name": env_name, "container_id": c.id,
                "host_port": host_port, "base_url": base_url,
                "created_at": created_at, "status": "starting",
            }
