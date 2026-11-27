"""A five-question Linux quiz with a shared scoreboard.

Standard library only, so it deploys with no install step. Scores are kept in
data/scores.json, the folder Stratus keeps for each application.
"""
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

QUESTIONS = [
    ("Which command lists the files in a directory?", ["ls", "cd", "pwd"], "ls"),
    ("Which command prints the current directory?", ["pwd", "whoami", "echo"], "pwd"),
    ("What does chmod 755 give the owner?", ["read, write, execute", "read only", "nothing"], "read, write, execute"),
    ("Which symbol pipes one command into another?", ["|", ">", "&"], "|"),
    ("Which command shows running processes?", ["ps", "cat", "mv"], "ps"),
]

DATA = Path(os.environ.get("STRATUS_DATA_DIR", "data"))
DATA.mkdir(parents=True, exist_ok=True)
SCORES = DATA / "scores.json"

STYLE = """body{font-family:system-ui,sans-serif;background:#0b1020;color:#e9ecf5;max-width:640px;margin:40px auto;padding:0 16px}
h1{color:#9b8cff}fieldset{border:1px solid #394267;border-radius:10px;margin:12px 0;padding:12px}
button{background:#7667ef;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:16px}
input[type=text]{padding:8px;border-radius:6px;border:1px solid #394267;background:#101526;color:#e9ecf5}
li{margin:4px 0}.ok{color:#7ee2a8}"""


def load_scores():
    try: return json.loads(SCORES.read_text())
    except (OSError, ValueError): return []


def page(body):
    return f"<!doctype html><meta charset=utf-8><title>Class Quiz</title><style>{STYLE}</style>{body}".encode()


class Quiz(BaseHTTPRequestHandler):
    def send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        questions = "".join(
            f"<fieldset><legend>{index + 1}. {html.escape(text)}</legend>"
            + "".join(f"<label><input type=radio name=q{index} value='{html.escape(option)}' required> {html.escape(option)}</label><br>"
                      for option in options) + "</fieldset>"
            for index, (text, options, _) in enumerate(QUESTIONS))
        board = "".join(f"<li>{html.escape(item['name'])}: {item['score']}/{len(QUESTIONS)}</li>"
                        for item in sorted(load_scores(), key=lambda item: -item["score"])[:10])
        self.send(page(f"<h1>Linux Quiz</h1><form method=post>"
                       f"<p>Your name <input type=text name=name maxlength=30 required></p>{questions}"
                       f"<button>Submit</button></form><h2>Scoreboard</h2><ol>{board or '<li>No scores yet</li>'}</ol>"))

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length", 0)), 10_000)
        form = parse_qs(self.rfile.read(length).decode())
        name = (form.get("name", ["anonymous"])[0] or "anonymous")[:30]
        score = sum(form.get(f"q{index}", [""])[0] == answer for index, (_, _, answer) in enumerate(QUESTIONS))
        scores = load_scores() + [{"name": name, "score": score}]
        SCORES.write_text(json.dumps(scores[-200:]))
        self.send(page(f"<h1>Thanks, {html.escape(name)}!</h1><p class=ok>You scored {score} out of {len(QUESTIONS)}.</p>"
                       "<p><a href='/' style='color:#9b8cff'>Back to the quiz</a></p>"))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Class Quiz on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Quiz).serve_forever()
