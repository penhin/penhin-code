import json
import subprocess
from pathlib import Path

from penhin.cli import ui
from penhin.plugins.installation import (
    OFFICIAL_PUBLISHER_TRUST_ROOTS,
    install_plugin_artifact,
    update_project_lock,
    verify_plugin_artifact,
)
from penhin.plugins.manager import PluginManager
from penhin.plugins.service import read_local_plugin_manifest


def manager() -> PluginManager:
    return PluginManager(Path.home() / ".penhin/plugins.json", Path(".penhin/plugins.json"))


def _installation_paths(service: PluginManager, scope: str) -> tuple[Path, Path]:
    config_path = service.global_file if scope == "global" else service.project_file
    return config_path.parent / "plugins", config_path.parent / "plugins.lock.json"


def _install_args(args: list[str]) -> tuple[str, str, str]:
    if len(args) not in {3, 4}:
        raise ValueError("Usage: /plugin install <name> <source> [--project|--global]")
    name, source = args[1], args[2]
    flag = args[3] if len(args) == 4 else "--project"
    if flag not in {"--project", "--global"}:
        raise ValueError("Plugin scope must be --project or --global")
    return name, source, flag.removeprefix("--")


def _materialize_plugin(service: PluginManager, args: list[str], *, update: bool, announce: bool = True):
    name, source, scope = _install_args(args)
    root, lock_file = _installation_paths(service, scope)
    receipt = install_plugin_artifact(
        name, source, root,
        trust_roots={**OFFICIAL_PUBLISHER_TRUST_ROOTS, **service.project_trust_roots()},
    )
    manifest = read_local_plugin_manifest(receipt.path, expected_name=name)
    if update:
        service.update(name, str(receipt.path), scope)
    else:
        service.install(name, str(receipt.path), scope)
    update_project_lock(name, receipt.artifact, lock_file)
    if announce:
        ui.print_info(
            f"plugin {'updated' if update else 'installed'}: {name} ({scope}); "
            "authorize it before activation"
        )
    return name, manifest


def _authorize_installed_plugin(service: PluginManager, name: str, manifest=None) -> None:
    registration = service.effective()[name]
    artifact = verify_plugin_artifact(
        Path(registration["source"]),
        {**OFFICIAL_PUBLISHER_TRUST_ROOTS, **service.project_trust_roots()},
    )
    manifest = manifest or read_local_plugin_manifest(registration["source"], expected_name=name)
    service.authorize_artifact(name, artifact.resolved, artifact.digest, sorted(manifest.capabilities))


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
        if action == "install":
            _materialize_plugin(service, args, update=False)
            return
        if action == "add":
            name, manifest = _materialize_plugin(service, args, update=False, announce=False)
            _authorize_installed_plugin(service, name, manifest)
            if runtime is None:
                ui.print_info(f"plugin authorized: {name}; activate it in an interactive session")
                return
            reloaded = runtime.reload()
            if not reloaded.ok:
                ui.print_error(reloaded.error)
                return
            outcome = runtime.activate(name)
            if outcome.ok:
                _context.policy.allow.update(runtime.catalog().names())
                ui.print_info(f"plugin ready: {name}")
            else:
                ui.print_error(outcome.error)
            return
        if action == "trust": service.add_project_trust_root(args[1], args[2]); ui.print_info(f"trusted publisher: {args[1]}"); return
        if action == "authorize":
            if len(args) == 2:
                name = args[1]
                _authorize_installed_plugin(service, name)
            else:
                service.authorize_artifact(args[1], args[2], args[3], json.loads(args[4]))
            ui.print_info(f"authorized plugin artifact: {args[1]}")
            return
        if action == "revoke":
            service.revoke_artifact(args[1])
            if runtime is not None: runtime.revoke(args[1])
            ui.print_info(f"revoked plugin artifact: {args[1]}"); return
        if action == "require":
            candidates = service.eligible_contributions(args[1])
            if len(candidates) == 1:
                service.require_contribution(candidates[0]); ui.print_info(f"required plugin contribution: {candidates[0]['id']}")
            else:
                ui.print_json({"selector": "plugin contribution", "requested": args[1], "eligible": candidates})
            return
        if action in {"enable", "disable"}: service.set_enabled(args[1], action == "enable"); ui.print_info(f"plugin {action}d: {args[1]}"); return
        if action == "remove": service.remove(args[1]); ui.print_info(f"plugin removed: {args[1]}"); return
        if action == "update":
            _materialize_plugin(service, args, update=True)
            return
        if action == "configure": service.configure(args[1], json.loads(args[2])); ui.print_info(f"plugin configured: {args[1]}"); return
        ui.print_error("Usage: /plugin [list|inspect|install|add|activate|deactivate|active|reload|trust|authorize|revoke|require|configure|enable|disable|update|remove]")
    except (IndexError, KeyError, ValueError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        ui.print_error(f"plugin: {error}")
