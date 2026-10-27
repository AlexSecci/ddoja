# Runtime latency measurements

New worker invocations record monotonic start/end timing for tools. These measurements do not add model calls or send timing transcripts back to the model. The pipeline remains stopped until the user resumes it.

The efficiency API/dashboard exposes startup time, browser tool time, other tool time, time outside tools, and completed browser-call p50/p95 latency. Browser time includes the CUA runtime, browser transport, Chrome and website waits; it is not just rendering time. Other-tool time includes database work, document rendering and scoped bridge operations.

`model_and_transport_seconds` is the worker's active time outside tool intervals. It includes inference, provider queueing, network and local orchestration. **It is not pure inference time.** `pure_inference_seconds` stays null. The old `model_elapsed_seconds` usage field is retained for compatibility and represents worker execution time including tools; it must not be described as inference time.

Parallel tool intervals are merged within a worker. Browser and other-tool intervals can overlap; `all_tool_seconds` is their union. Aggregated worker seconds across parallel agents are not campaign wall-clock duration. Censored calls are counted when interrupted and excluded from completed-call latency statistics. Historical turns without instrumentation remain unavailable, rather than being assigned zero browser time.

Each CLI invocation saves timing in its existing private runtime report and emits a `timing_summary` event. App-server document tasks time their actual bridge handling and emit the same summary. The efficiency API deduplicates by run ID. Human Inbox waits remain measured separately through existing wait records.

For a finer model-request breakdown, Codex's optional local OpenTelemetry integration reports API request and tool durations. It would require a collector and validation of streaming/queueing semantics; it has not been enabled here. [Official monitoring documentation](https://learn.chatgpt.com/docs/agent-approvals-security).
