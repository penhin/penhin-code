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

**Attachment**:
An opaque user-provided resource whose metadata and content are owned by the core for an input submission.
_Avoid_: File path, upload

**Attachment Session**:
The core-owned lifetime boundary that determines whether an Attachment remains available for an input submission.
_Avoid_: Permanent attachment storage

**Attachment Handle**:
The brokered capability an Input Enricher uses to read an Attachment's content and inspect its metadata without receiving a machine path.
_Avoid_: Attachment file path, raw Attachment object

**Input Event**:
A typed user input and its Attachment references submitted to PluginRuntime for enrichment.
_Avoid_: Raw prompt payload

**Input Enrichment Event**:
The text and event kind that an Input Enricher may observe, excluding core-owned Attachments.
_Avoid_: Attachment-bearing input event

**Input Enricher**:
A Plugin Contribution that may read brokered Attachment Handles and append source-labelled Context Supplements.
_Avoid_: Input mutator, prompt hook

**Context Supplement**:
Source-labelled, untrusted Plugin-produced context preserved separately from user input.
_Avoid_: System prompt content

**Input Submission**:
The immutable input text, ordered Context Supplements, and Plugin generations captured for one model-bound input.
_Avoid_: Mutable prompt

**Tool Invocation**:
The governed execution of one or more model-requested tools for an agent turn, including their ordered results and controlled session effects.
_Avoid_: Tool dispatch, message-level tool handling

**Tool Outcome**:
The result produced by a tool handler together with its declared Tool Effects, before Tool Invocation records their completion.
_Avoid_: Raw handler result

**Tool Effect**:
A named, schema-validated requested state change that Tool Invocation alone may apply and observe.
_Avoid_: Handler side effect, callback
