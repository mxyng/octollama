import os
import re
import asyncio
import argparse
import jinja2


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

    pattern = r'msg="Listening on (.+) \(version .+\)"'
    while True:
        line = await p.stderr.readline()
        if not line:
            break
        if matches := re.findall(pattern, line.decode()):
            print("Ollama server started", matches[0])
            await queue.put(matches[0])
            break

    return await p.wait()


tmpl = """
{
    debug
    admin off
    log default {
        output stdout
        format json
    }
}

:54321 {
    reverse_proxy {
        to {%- for instance in instances %} {{ instance }} {%- endfor %}
        lb_policy ip_hash
    }
}
"""


async def caddy(queue):
    instances = []
    while len(instances) < queue.maxsize:
        hostport = await queue.get()
        instances.append(hostport)

    with open("Caddyfile", "w") as f:
        f.write(jinja2.Template(tmpl).render(instances=instances))

    p = await asyncio.create_subprocess_exec("caddy", "run", "--config", "Caddyfile")
    return await p.wait()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=8)
    args = parser.parse_args()

    queue = asyncio.Queue(maxsize=args.n)
    await asyncio.gather(
        *[ollama(queue) for _ in range(args.n)],
        caddy(queue),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        ...
