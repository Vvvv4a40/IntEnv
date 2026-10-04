"""Composition root and entry point: python -m envi."""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

from .assistant import AssistantService
from .cancellation import CancellationToken
from .configuration import AppSettings
from .errors import AssistantError
from .event_log import JsonEventLog
from .providers.groq import GroqClient
from .routing import RuleRouter
from .tools import ToolRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Envi — помощник Windows на Python")
    parser.add_argument("--settings", type=Path,
                        default=Path(__file__).resolve().parent.parent / "envi.settings.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--smoke-test", action="store_true", help="создать окно без показа, записи и сети")
    modes.add_argument("--doctor", action="store_true", help="показать состояние среды, не значение ключа")
    modes.add_argument("--text", help="обработать один текстовый запрос без окна")
    args = parser.parse_args(argv)
    root = None
    window = None
    try:
        if sys.version_info < (3, 11):
            raise AssistantError("Нужен Python 3.11 или новее.")
        settings = AppSettings.load(args.settings)
        key = os.environ.get("GROQ_API_KEY")
        if args.doctor:
            print(f"Python: {sys.version.split()[0]} ({sys.executable})")
            print(f"Платформа: {sys.platform}; Windows-действия и микрофон: {'да' if sys.platform == 'win32' else 'нет'}")
            print(f"Tkinter: {'найден' if importlib.util.find_spec('tkinter') else 'не найден'}")
            print(f"Настройки: {args.settings.resolve()}")
            print(f"GROQ_API_KEY: {'задан (значение скрыто)' if key and key.strip() else 'не задан'}")
            print("Проверка не обращалась к Groq, микрофону или запуску программ.")
            return 0
        tools = ToolRegistry(settings)
        groq = GroqClient(settings, key)
        log_folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Envi"
        events = JsonEventLog(log_folder / "events.jsonl")
        assistant = AssistantService(groq, tools, RuleRouter(), settings, events)
        if args.text is not None:
            token = CancellationToken()

            def confirm(call):
                print(tools.describe(call))
                return input("Разрешить действие? [yes/нет]: ").strip().casefold() in ("yes", "да")

            print(assistant.process(args.text, confirm, token))
            return 0
        try:
            import tkinter as tk
            from .ui import EnviWindow
        except ImportError as error:
            raise AssistantError("Для окна нужен Tkinter. Используй Python с Tcl/Tk; текстовый режим --text доступен отдельно.") from error
        root = tk.Tk()
        root.withdraw()
        window = EnviWindow(root, settings, groq, assistant, tools, bool(key and key.strip()))
        if args.smoke_test:
            root.update_idletasks()
            root.update()
            return 0
        root.deiconify()
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        print("Запрос отменён.", file=sys.stderr)
        return 130
    except (AssistantError, OSError, RuntimeError) as error:
        print(f"Envi: {error}", file=sys.stderr)
        return 1
    except Exception:
        # Do not expose credential-bearing exception/transport details to the terminal.
        print("Envi: не удалось инициализировать приложение. Проверь Python, Tkinter и настройки.", file=sys.stderr)
        return 1
    finally:
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
        elif root is not None:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
