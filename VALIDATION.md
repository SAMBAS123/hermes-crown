# Validation — 2026-11-09

## What passed

| Check | Observed result |
| --- | --- |
| Standard-library unit/integration suite | 18 tests passed |
| Simultaneous acquisition | 12 OS processes raced; exactly one acquired the lease |
| Repeated failure proof | 20/20 rounds passed with three real processes each |
| Hard-kill recovery | Saved answer remained; successor published the pending question |
| Suspended old speaker | Replacement acquired a later epoch; resumed old token was rejected |
| Crash inside a SQLite transaction | Uncommitted lease/answer changes rolled back |
| Clock/reboot behavior | Lease expiry, generation change and reboot invalidation passed |
| Hermes admission validator | Accepted manifest, registration, capabilities, security scan and no core override |
| Real Hermes plugin-host integration | Three independent PluginManager processes loaded the plugin, invoked native tools/commands/hooks, survived takeover and unloaded cleanly |

Core stress runs used a 0.45-second lease: observed takeover ranged from
0.445 to 0.480 seconds, median 0.475 seconds on this machine. These are local
observations, not performance guarantees. The real Hermes plugin-host run used
the default 3-second lease and took 3.061 seconds to publish after speaker death.

The real host integration checked that the successor's `crown_context` tool and
`pre_llm_call` hook contained the saved answer, with the original source and
conversation ID. It then paused the current speaker, let a replacement acquire
the role, resumed the old process, observed a stale-ownership rejection, and
verified unload removed the tools and released the remaining lease.

## Exact environment

- Linux, CPython 3.12.14.
- Hermes source revision: `466591fe65d0257c96083df16fcd39636032304c`.
- Actual upstream `hermes_cli.plugin_validate.validate_plugin_dir` was called;
  its isolated capability probe ran `register(ctx)` successfully.
- Actual upstream PluginManager, tool registry, hook invocation, local command
  handler and unload lifecycle were used by `tests/hermes_host_probe.py`.
- Validation used an isolated, minimal environment with ruamel.yaml 0.18.16.
  Some unrelated bundled Hermes tools could not import due to missing optional
  modules (`cron`, `requests`, `httpx`). The validator returned `ok: true` and
  no plugin warnings. No full Hermes dependency environment is claimed.

## Reproduce

```bash
python3 -m unittest discover -s tests -v
python3 -m crown demo --rounds 20 --evidence proof/local-proof.json
```

To run the optional real-SDK probe, use a Python environment that can import the
chosen Hermes checkout's PluginManager and YAML/config dependencies:

```bash
python3 tests/hermes_host_probe.py --upstream /absolute/path/to/hermes-agent
```

It creates throwaway profile homes, enables only Crown there, and starts no
model or gateway. It kills/pauses only child processes it created. It does not
touch an existing user's Hermes home or credentials.

Evidence is in `proof/failure-proof.json`, `proof/failure-proof.log`,
`proof/tests.log`, `proof/hermes-hosts.json`, and `proof/hermes-validation.json`.

## Still unverified

This is a functioning coordination-core prototype with a tested Hermes plugin
registration path. It is **not yet the full hackathon demonstration**.

The next acceptance test must run three independent, configured Hermes agent
runtimes, perform real tasks through their models, and repeat the same failure
sequence through the intended local user interface. Model behavior, normal
gateway/CLI command dispatch and actual installed-host lifecycle still need
that end-to-end run. Native Hermes messages outside Crown are not gated.

Cross-machine consensus, network partitions, external messaging delivery,
automatic attention detection, hostile local processes and loss of the database
disk are outside the implemented and tested boundary.
