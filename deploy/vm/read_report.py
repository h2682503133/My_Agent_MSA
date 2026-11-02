"""读取虚拟机回传的报告（deploy/vm/reports/*.jsonl），打印最近几条的要点。"""

import glob
import json
import os
import sys

FIELDS = {
    "onebot-mock": ("connected", "events_pushed", "sent_actions", "error"),
    "chat-sse": ("event_types", "reply_text", "reply_images"),
    "openviking-memory": (),
    "skill-state": ("skill_root", "installed_dir_exists", "skill_md_exists", "skill_list"),
}


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    reports = sorted(glob.glob("deploy/vm/reports/*.jsonl"), key=os.path.getmtime)
    if not reports:
        print("还没有报告")
        return
    lines = open(reports[-1], encoding="utf-8").read().strip().splitlines()
    for raw in lines[-limit:]:
        data = json.loads(raw)
        detail = data.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except Exception:
                pass
        stage = data.get("stage") or (detail or {}).get("stage")
        print(f"=== stage={stage} ok={data.get('ok')} at={data.get('received_at')}")
        if not isinstance(detail, dict):
            print("   detail:", str(detail)[:400])
            continue
        keys = FIELDS.get(stage) or list(detail.keys())
        for key in keys:
            if key not in detail:
                continue
            value = detail[key]
            if key == "sent_actions" and isinstance(value, list):
                if not value:
                    print("   sent_actions: （空）")
                for item in value:
                    print(f"   action={item.get('action')} params={json.dumps(item.get('params'), ensure_ascii=False)[:300]}")
                continue
            text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
            print(f"   {key}: {text[:500]}")


if __name__ == "__main__":
    main()
