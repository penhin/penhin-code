# Govern Tool Effects Through Tool Invocation

Tool handlers return a Tool Outcome containing a Result and declared Tool Effects; Tool Invocation alone validates and applies those effects. This keeps session state, approval, ordering, failures, and observation under one interface instead of allowing handlers to mutate a session directly.

## Considered Options

We rejected direct handler mutation because it leaks state changes past approval and makes ordered execution hard to verify. Tool Effects use an open `kind` and `payload`, but Tool Invocation owns a registry with validation and implementation, rather than allowing handlers to interpret their own payloads.

## Consequences

Every ToolSpec handler adopts the Tool Outcome interface. Effects execute serially, and an effect failure fails its Tool Invocation and prevents later effects. ToolRun metadata and observation events record each applied or failed effect.
