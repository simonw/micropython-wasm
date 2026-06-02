# Future Work

## Bounded Output Capture

Add stdout/stderr byte caps to prevent guest code from growing host memory
without bound.

Recommended first implementation:

- Add `max_stdout_bytes` and `max_stderr_bytes` arguments.
- Default both to `1_000_000`.
- Extend `RunResult` with `stdout_truncated` and `stderr_truncated` booleans.
- Capture only up to the configured byte cap.
- Preserve enough metadata for callers to distinguish complete output from
  clipped output.
- Add `on_output_limit`, defaulting to `"truncate"`.
- Support `on_output_limit="trap"` for stricter sandbox use, where exceeding
  the output cap interrupts guest execution instead of merely clipping output.

For high-risk untrusted code, still run executions in a separate worker process
with OS-level memory and CPU limits. Output caps protect this package's capture
buffers, but they do not replace process-level isolation.
