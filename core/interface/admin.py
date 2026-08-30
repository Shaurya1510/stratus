from __future__ import annotations

import json
from pathlib import Path

from core.system.presets import PRESETS
from core.platform.session import Session


def main() -> None:
    config_path = Path.home() / ".config" / "stratus" / "active-server.json"
    if not config_path.is_file(): raise SystemExit("No configured local Stratus server. Start hosting first.")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    # Host powers are granted by connecting from the host computer itself.
    session = Session("127.0.0.1", config["port"], config["token"])
    try:
        session.call("policy")
    except Exception as exc:
        raise SystemExit(f"The Stratus host is not answering on port {config['port']}: {exc}")
    try:
        from core.interface.app import AdminTUI
    except ImportError:
        AdminTUI = None
    if AdminTUI is not None:
        try: AdminTUI(session.call, config).run()
        finally: session.close()
        return
    print("Textual is unavailable; using the basic compatibility menu.")
    try: compatibility_menu(session, config)
    finally: session.close()


def compatibility_menu(session: Session, config: dict) -> None:
    while True:
        print(f"\nHOST ADMIN — {config['name']}\n1. Server policy\n2. Apply Low preset\n3. Apply Medium preset\n4. Apply High preset\n5. Enable/disable guest hosting\n6. Connected devices\n7. All applications\n8. Deployment approvals\n9. Exit")
        choice = input("Choose: ").strip()
        if choice == "1": print(json.dumps(session.call("policy"), indent=2))
        elif choice in {"2", "3", "4"}:
            name = {"2": "low", "3": "medium", "4": "high"}[choice]
            print(json.dumps(session.call("set-policy", changes=PRESETS[name]), indent=2))
        elif choice == "5":
            policy = session.call("policy"); enabled = 0 if policy["guest_deploy_enabled"] else 1
            print(json.dumps(session.call("set-policy", changes={"guest_deploy_enabled": enabled}), indent=2))
        elif choice == "6":
            for item in session.call("devices"): print(f"- {item['display_name']} · {item['id']} · {'blocked' if item['blocked'] else 'allowed'}")
        elif choice == "7":
            for item in session.call("apps"): print(f"- {item['name']} · {item['owner']} · {item['status']} · :{item['port']}")
        elif choice == "8":
            requests = session.call("deployment_requests")
            for item in requests: print(f"{item['id']}. {item['app_name']} · {item['display_name']} · {item['runtime']} · {item['upload_bytes']} bytes")
            if requests:
                request_id = int(input("Request ID (0 to cancel): ") or "0")
                if request_id:
                    decision = input("Approve? [y/N]: ").lower()
                    action = "approve_deployment" if decision == "y" else "deny_deployment"
                    print(json.dumps(session.call(action, request_id=request_id), indent=2))
        elif choice == "9": return


if __name__ == "__main__": main()
