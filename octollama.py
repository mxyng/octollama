import os
import re
import asyncio
import argparse
import jinja2
import tempfile


def match(pattern, fn):
    async def inner(line):
        for match in re.findall(pattern, line):
            await fn(match)

    return inner


async def ollama(queue):
    p = await asyncio.create_subprocess_exec(
        "ollama",
        "serve",
        stdout=asyncio.subprocess.DEVNULL,
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
            line = (await outerr.readline()).decode().strip()
            if not line:
                break

            print(line)
            for fn in fns:
                await fn(line)

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
    print(f"Waiting for {queue.maxsize} Ollama instances to start...")
    instances = []
    while len(instances) < queue.maxsize:
        hostport = await queue.get()
        print(f"Got Ollama instance: {hostport}")
        instances.append(hostport)

    with tempfile.NamedTemporaryFile(mode="w") as f:
        f.write(jinja2.Template(tmpl).render(instances=instances))
        f.flush()
        p = await asyncio.create_subprocess_exec("caddy", "run", "--config", f.name)
        await p.communicate()
        if p.returncode != 0:
            raise RuntimeError(f"Caddy exited with code {p.returncode}")


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
