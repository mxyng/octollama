import argparse
import asyncio
import json
import os
import re
import tempfile

import httpx
from prometheus_client.parser import text_string_to_metric_families


def match(pattern, fn):
    async def inner(line):
        for match in re.findall(pattern, line):
            await fn(match)

    return inner


async def ollama(queue):
    p = await asyncio.create_subprocess_exec(
        "ollama",
        "serve",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "OLLAMA_NOPRUNE": "1",
            "OLLAMA_HOST": "0.0.0.0:0",
        },
    )

    async def capture(outerr, *fns):
        if not outerr:
            return

        while True:
            line = (await outerr.readline()).strip()
            if not line:
                break

            try:
                line = line.decode()
                print(line)
                for fn in fns:
                    await fn(line)
            except Exception:
                ...

    async def put(line):
        await queue.put(line)

    await asyncio.gather(
        p.wait(),
        capture(p.stdout),
        capture(
            p.stderr,
            match(
                r'msg="Listening on (.+) \(version .+\)"',
                put,
            ),
        ),
    )

    if p.returncode != 0:
        raise RuntimeError(f"Ollama exited with code {p.returncode}")


async def caddy(queue):
    print(f"Waiting for {queue.maxsize} Ollama instances to start...")
    instances = []
    while len(instances) < queue.maxsize:
        hostport = await queue.get()
        print(f"Got Ollama instance: {hostport}")
        instances.append(hostport)
        queue.task_done()

    with tempfile.NamedTemporaryFile(mode="w") as f:
        json.dump(
            {
                "admin": {"disabled": True},
                "logging": {
                    "logs": {
                        "default": {"level": "DEBUG", "writer": {"output": "stdout"}}
                    },
                },
                "apps": {
                    "http": {
                        "metrics": {},
                        "servers": {
                            "metrics": {
                                "listen": ["127.0.0.1:9090"],
                                "routes": [{"handle": [{"handler": "metrics"}]}],
                            },
                            "srv0": {
                                "listen": [":54321"],
                                "routes": [
                                    {
                                        "handle": [
                                            {
                                                "handler": "reverse_proxy",
                                                "transport": {
                                                    "protocol": "http",
                                                    "read_buffer_size": 0,
                                                    "write_buffer_size": 0,
                                                },
                                                "load_balancing": {
                                                    "selection_policy": {
                                                        "policy": "ip_hash"
                                                    },
                                                },
                                                "upstreams": [
                                                    {"dial": instance}
                                                    for instance in instances
                                                ],
                                                "health_checks": {
                                                    "active": {
                                                        "uri": "/",
                                                    },
                                                },
                                            }
                                        ]
                                    }
                                ],
                            },
                        },
                    },
                },
            },
            f,
        )

        f.flush()
        p = await asyncio.create_subprocess_exec("caddy", "run", "--config", f.name)
        await p.communicate()
        if p.returncode != 0:
            raise RuntimeError(f"Caddy exited with code {p.returncode}")


async def healthcheck():
    async with httpx.AsyncClient() as client:
        while True:
            try:
                r = await client.get("http://127.0.0.1:9090/metrics")
                r.raise_for_status()

                for metric in text_string_to_metric_families(r.text):
                    if metric.name != "caddy_reverse_proxy_upstreams_healthy":
                        continue
                    if any(sample.value != 1 for sample in metric.samples):
                        raise RuntimeError("Healthcheck failed")
                await asyncio.sleep(5)
            except httpx.RequestError:
                await asyncio.sleep(1)
                ...


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=8)
    args = parser.parse_args()

    queue = asyncio.Queue(maxsize=args.n)
    await asyncio.gather(
        *[ollama(queue) for _ in range(args.n)],
        caddy(queue),
        healthcheck(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        ...
