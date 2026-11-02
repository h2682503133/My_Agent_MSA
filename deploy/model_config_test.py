"""model_list 生成器自检（离线，不写真实数据目录）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import model_config  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


TEMPLATE = json.loads((ROOT / "config" / "model-proxy" / "config" / "model_list.json").read_text(encoding="utf-8"))


def main() -> int:
    print("远端 API")
    remote = {
        "model.source": "remote",
        "model.base_url": "https://api.example.com/v1/chat/completions",
        "model.api_key": "sk-test",
        "model.model": "remote-model",
    }
    data, note = model_config.generate(remote, TEMPLATE)
    check("返回说明", "远端" in note, note)
    check("main 用远端地址", data["models"]["main"]["api_url"] == remote["model.base_url"])
    check("main 用远端 key", data["models"]["main"]["api_key"] == "sk-test")
    check("reader 也被绑定", data["models"]["reader"]["model"] == "remote-model")
    check("aliases 指向自身", data["aliases"]["default-main"] == "main" and data["aliases"]["default-reader"] == "reader")
    check("保留 embedding", "embedding" in data["models"])

    print("本地 Ollama")
    local = {
        "model.source": "local",
        "ollama.url": "http://127.0.0.1:11434/",
        "ollama.model": "qwen3:4b",
        "ollama.profile": "local-ollama",
    }
    data2, note2 = model_config.generate(local, TEMPLATE)
    check("返回说明", "Ollama" in note2, note2)
    entry = data2["models"]["local-ollama"]
    check("provider=ollama", entry["provider"] == "ollama")
    check("api_url 拼接 chat_path", entry["api_url"] == "http://127.0.0.1:11434/api/chat", entry["api_url"])
    check("模型名写入", entry["model"] == "qwen3:4b")
    for profile in ("main", "tool", "reader", "talker"):
        check(f"default-{profile} → local-ollama", data2["aliases"][f"default-{profile}"] == "local-ollama")
    check("保留 embedding", "embedding" in data2["models"])

    print("自定义 chat_path / profile / 绑定范围")
    custom = {
        "model.source": "local",
        "ollama.url": "http://host.docker.internal:11434",
        "ollama.chat_path": "/v1/chat/completions",
        "ollama.model": "qwen3:8b",
        "ollama.profile": "my-local",
        "profiles": "main,reader",
    }
    data3, _ = model_config.generate(custom, TEMPLATE)
    check("自定义路径", data3["models"]["my-local"]["api_url"] == "http://host.docker.internal:11434/v1/chat/completions")
    check("只绑定指定 profile", data3["aliases"]["default-main"] == "my-local" and data3["aliases"]["default-reader"] == "my-local")
    check("未指定的 profile 保持原样", data3["aliases"]["default-tool"] == "tool")

    print("缺必填要报错（不能静默写坏配置）")
    for bad, label in (
        ({"model.source": "remote", "model.base_url": "x"}, "远端缺 key/model"),
        ({"model.source": "local", "ollama.url": "http://x"}, "本地缺 model"),
        ({"model.source": "whatever"}, "未知来源"),
    ):
        try:
            model_config.generate(bad, TEMPLATE)
            check(label + " → 报错", False, "没有报错")
        except ValueError as exc:
            check(label + " → 报错", True, str(exc)[:70])

    print("写盘")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config" / "model-proxy" / "config").mkdir(parents=True)
    note3 = model_config.write(tmp, local)
    target = tmp / "config" / "model-proxy" / "config" / "model_list.json"
    check("文件已生成", target.exists())
    written = json.loads(target.read_text(encoding="utf-8"))
    check("内容正确", written["models"]["local-ollama"]["model"] == "qwen3:4b")
    check("说明里有路径", "model_list.json" in note3, note3)

    print("--print 不写盘")
    tmp2 = Path(tempfile.mkdtemp())
    out = model_config.write(tmp2, remote, print_only=True)
    check("打印内容含 models", '"models"' in out, out[:60])
    check("没有落盘", not (tmp2 / "config" / "model-proxy" / "config" / "model_list.json").exists())


    print("embedding 来源")
    local_emb = dict(local, **{"embedding.source": "local", "ollama.embedding_model": "nomic-embed-text"})
    data4, note4 = model_config.generate(local_emb, TEMPLATE)
    emb = data4["models"]["local-embedding"]
    check("本地 embedding provider=ollama", emb["provider"] == "ollama", str(emb))
    check("本地 embedding 走 /api/embed", emb["api_url"] == "http://127.0.0.1:11434/api/embed", emb["api_url"])
    check("本地 embedding 模型名", emb["model"] == "nomic-embed-text")
    check("default-embedding 指向本地", data4["aliases"]["default-embedding"] == "local-embedding")
    check("说明里提到 embedding", "embedding" in note4, note4)

    remote_emb = dict(remote, **{
        "embedding.source": "remote",
        "embedding.base_url": "https://emb.example.com/v1/embeddings",
        "embedding.model": "text-embedding-3-small",
        "embedding.api_key": "sk-emb",
    })
    data5, _ = model_config.generate(remote_emb, TEMPLATE)
    emb5 = data5["models"]["embedding"]
    check("远端 embedding 覆盖地址", emb5["api_url"] == "https://emb.example.com/v1/embeddings", str(emb5))
    check("远端 embedding 覆盖模型", emb5["model"] == "text-embedding-3-small")
    check("default-embedding 指回 embedding", data5["aliases"]["default-embedding"] == "embedding")

    keep = dict(remote)
    data6, _ = model_config.generate(keep, TEMPLATE)
    check("没选远端 embedding 时保留模板 entry",
          data6["models"]["embedding"]["api_url"] == TEMPLATE["models"]["embedding"]["api_url"],
          data6["models"]["embedding"]["api_url"])

    bad_emb = dict(local, **{"embedding.source": "local"})
    bad_emb.pop("ollama.url", None)
    try:
        model_config.generate(bad_emb, TEMPLATE)
        check("本地 embedding 缺 url → 报错", False)
    except ValueError as exc:
        check("本地 embedding 缺 url → 报错", "ollama.url" in str(exc), str(exc)[:60])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
