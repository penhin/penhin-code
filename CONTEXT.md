# Penhin

Penhin is an agent runtime that coordinates tools, plugins, providers, and durable multi-agent work.

## Language

**Plugin**:
A separately installed extension that contributes governed capabilities to a Penhin run.
_Avoid_: Extension, add-on

**PluginRuntime**:
The runtime owner of Plugin discovery, activation, and cleanup for one Penhin run.
_Avoid_: Plugin manager, plugin loader

**Plugin Artifact**:
The resolved, reproducible contents of a Plugin source, identified by its digest.
_Avoid_: Package, bundle

**Plugin Contribution**:
A governed tool, command, skill, or hook supplied by a Plugin.
_Avoid_: Resource, extension point

**Plugin Activation**:
The explicit inclusion or immediate removal of a Plugin's Contributions for one Penhin run.
_Avoid_: Automatic routing, implicit loading
