import json
from pathlib import Path

from penhin.cli import ui
from penhin.plugins.manager import PluginManager


def manager() -> PluginManager:
    return PluginManager(Path.home() / ".penhin/plugins.json", Path(".penhin/plugins.json"))


def handle_plugin_command(args, _context=None):
    service = manager()
    action = args[0] if args else "list"
    try:
        runtime = getattr(_context, "plugin_runtime", None)
        if action in {"activate", "deactivate", "active", "reload"}:
            if runtime is None:
                ui.print_error("plugin runtime is unavailable for this session")
                return
            if action == "active":
                ui.print_json({"available": runtime.available(), "active": runtime.active()})
                return
            if action == "reload":
                outcome = runtime.reload()
                if outcome.ok:
                    _context.policy.allow.update(runtime.catalog().names())
                    ui.print_json(outcome.data)
                else:
                    ui.print_error(outcome.error)
                return
            outcome = runtime.activate(args[1]) if action == "activate" else runtime.deactivate(args[1])
            if outcome.ok and action == "activate":
                _context.policy.allow.update(runtime.catalog().names())
            if outcome.ok:
                ui.print_info(f"plugin {action}d: {args[1]}")
            else:
                ui.print_error(outcome.error)
            return
        if action == "list": ui.print_json(service.effective()); return
        if action == "inspect": ui.print_json(service.effective()[args[1]]); return
        if action in {"enable", "disable"}: service.set_enabled(args[1], action == "enable"); ui.print_info(f"plugin {action}d: {args[1]}"); return
        if action == "remove": service.remove(args[1]); ui.print_info(f"plugin removed: {args[1]}"); return
        if action == "update": service.update(args[1], args[2]); ui.print_info(f"plugin updated: {args[1]}"); return
        if action == "configure": service.configure(args[1], json.loads(args[2])); ui.print_info(f"plugin configured: {args[1]}"); return
        ui.print_error("Usage: /plugin [list|inspect|activate|deactivate|active|reload|configure|enable|disable|update|remove]")
    except (IndexError, KeyError, json.JSONDecodeError) as error:
        ui.print_error(f"plugin: {error}")
