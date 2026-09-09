# Candidate 2: PluginRuntime implementation plan

## Objective

Decision checkpoint (2026-09-08): see [grill checkpoint](candidate-2-grill-checkpoint.md) for confirmed authorization rules, the explicit bulk Plugin reload exception, and unresolved questions. That checkpoint supersedes conflicting earlier decisions below; implementation remains incomplete.

Introduce a deep `PluginRuntime` module that owns the governed Plugin lifecycle for one Penhin run: effective configuration, Artifact validation, activation, a unified tool catalog, and cleanup. Callers use its interface instead of coordinating PluginManager, source resolution, locks, hosts, routing, and catalogs themselves.

## Decisions

- A **PluginRuntime** discovers and validates every effective Plugin Artifact at run start, but does not start a Plugin host until explicit Plugin Activation.
- A Plugin's tools are always namespaced as `plugin__tool`; Plugin Contributions cannot replace built-in tools.
- Global configuration is overlaid by project configuration for the same Plugin name.
- Installation and updates are explicit CLI operations. A run uses the Artifact digest selected when it starts; ordinary updates affect later runs only. Explicit user-requested reload may switch the current run to new Artifacts. The user selected bulk reload of all Plugins; detailed semantics remain under discussion in the checkpoint.
- An active Plugin owns one isolated host for the run. Deactivation immediately removes all of its Plugin Contributions from the catalog and closes that host.
- Manifest declarations are an upper capability limit; the effective capability set is their intersection with the user permission policy.
- A failed Artifact validation or host startup disables only that Plugin, produces an auditable diagnostic, and leaves built-in tools and other Plugins available.
- Session-level Plugin Activation is explicit, audit-only, and never changes persistent `enabled` configuration.

## Public interface

`PluginRuntime` is constructed at the application composition root with adapters for configuration, Artifact resolution, Plugin loading, permission policy, and the base catalog.

```python
runtime = PluginRuntime(...)
runtime.discover()                     # validates effective configured Artifacts; no hosts start
runtime.activate("example")            # starts one host and adds its approved Contributions
runtime.deactivate("example")          # immediately removes Contributions and closes the host
runtime.catalog()                       # base catalog plus active, approved Plugin tools
runtime.diagnostics()                   # immutable activation and failure audit view
runtime.close()                         # closes every active host
```

`discover`, `activate`, and `deactivate` return structured results rather than leaking loader or host failures. `catalog()` returns a snapshot so an in-flight model turn keeps its tool surface; the next turn obtains the new catalog.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `PluginRuntime` | lifecycle state, activation set, catalog snapshots, diagnostics, cleanup |
| `PluginManager` | persistent global/project configuration and effective overlay |
| installation adapter | explicit source resolution and Artifact lock writing |
| loader adapter | manifest validation and lazy `LocalPlugin` host creation |
| `PluginCapabilityBroker` | capability requests inside one active host |
| `PluginContributions` | validated additive commands, skills, and hooks |

## Implementation slices

1. Add `PluginRuntime` with injected loader and base catalog. Write a contract test proving discovery does not start a host and activation adds only namespaced tools.
2. Add deactivation. Write a contract test proving the next catalog excludes the Plugin immediately and the host is closed.
3. Add effective configuration and Artifact lock validation. Test project-over-global precedence, digest mismatch, and failed Plugin isolation.
4. Integrate runtime creation at the application composition root and pass `runtime.catalog()` into the agent loop before each model turn.
5. Add explicit session commands for activation and deactivation. Record audit events in the transcript; do not mutate persistent configuration.
6. Route commands, skills, and hooks through the same runtime lifecycle. Test capability-policy intersection and cleanup on normal and failed runs.

## Acceptance tests

- Discovery of two enabled Plugins starts zero hosts.
- Activating one Plugin starts exactly its host and the unified catalog retains all built-in tools.
- Deactivation removes that Plugin's tools before the next model turn and closes its host.
- A malformed manifest, mismatched Artifact digest, or failed host startup records diagnostics without disabling other tools.
- A project Plugin overrides a same-named global Plugin.
- A Plugin cannot request a capability absent from either its manifest or the user policy.
- Updating a Plugin does not change the Artifact used by an active run unless the user explicitly requests reload.
- Session activation/deactivation appears in the transcript and does not alter persistent `enabled` configuration.

## Non-goals

- Automatic source download or updates during an agent run.
- Natural-language-triggered Plugin loading.
- Built-in tool overrides.
- Pi-style shared-process extension execution.
- Implicitly switching an active run onto a newly updated Artifact without explicit reload.
