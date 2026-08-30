from __future__ import annotations

import sys


def choose_menu(title: str, options: list[str], subtitle: str = "") -> int | None:
    try:
        import curses
    except ImportError:
        curses = None
    if curses is None or not sys.stdin.isatty() or not sys.stdout.isatty():
        print(f"\n{title}")
        for index, option in enumerate(options, 1): print(f"{index}. {option}")
        value = input("Choose: ").strip()
        return int(value) - 1 if value.isdigit() and 1 <= int(value) <= len(options) else None

    def run(screen):
        curses.curs_set(0); screen.keypad(True); curses.mousemask(curses.ALL_MOUSE_EVENTS)
        selected = 0
        while True:
            screen.erase(); height, width = screen.getmaxyx()
            screen.addnstr(1, 3, title, max(1, width - 6), curses.A_BOLD)
            if subtitle: screen.addnstr(2, 3, subtitle, max(1, width - 6), curses.A_DIM)
            start = 4
            for index, option in enumerate(options):
                marker = "› " if index == selected else "  "
                style = curses.A_REVERSE | curses.A_BOLD if index == selected else curses.A_NORMAL
                screen.addnstr(start + index, 3, f"{marker}{index+1}. {option}", max(1, width - 6), style)
            screen.addnstr(min(height-2, start+len(options)+2), 3, "↑/↓ move · Enter select · Esc back · mouse supported", max(1, width-6), curses.A_DIM)
            screen.refresh(); key = screen.getch()
            if key in (curses.KEY_UP, ord("k")): selected = (selected - 1) % len(options)
            elif key in (curses.KEY_DOWN, ord("j")): selected = (selected + 1) % len(options)
            elif key in (10, 13, curses.KEY_ENTER): return selected
            elif key == 27: return None
            elif ord("1") <= key <= ord("9") and key - ord("1") < len(options): return key - ord("1")
            elif key == curses.KEY_MOUSE:
                try:
                    _, _, y, _, state = curses.getmouse(); clicked = y - start
                    if 0 <= clicked < len(options) and state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED): return clicked
                except curses.error: pass
    return curses.wrapper(run)
