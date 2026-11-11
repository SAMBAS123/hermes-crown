# Crown

**Several Hermes instances. One speaking role. A record that survives the speaker.**

Prototype for the Hermes plugin hackathon. Working name: Crown.

Crown gives cooperating processes one renewable speaking lease and a shared,
durable record of reports, questions and human answers. A replacement reads the
same record. Every publication checks the current lease generation inside the
same database transaction that writes the message. A paused old speaker cannot
publish with an obsolete token after a successor takes over.

## Run the failure proof

Linux or WSL2, Python 3.11+. No package installs, model credentials or paid calls.

```bash
cd hermes-crown
python3 -m crown demo --slow
```

The demo starts three real OS processes, saves a human answer, kills the speaker,
and observes its successor publish the pending question with the saved answer.
It then **pauses** that successor, waits for a replacement, and resumes the old
process with its original token still in memory. The old token must be rejected.

```bash
python3 -m unittest discover -s tests -v
python3 -m crown demo --rounds 20 --evidence proof/my-failure-proof.json
```

The automated demo uses synthetic task content and real process failures. It
tests the coordination core, **not three live Hermes model sessions**. A separate
probe also passed with three real Hermes plugin loaders. See
[VALIDATION.md](VALIDATION.md) for the exact tested boundary and results.

## Use the shared outlet manually

Choose one absolute database path on **local disk**. Use the same path for all
participants. The database belongs to one trusted OS user and one audience.
Do not put it on NFS, SMB, Dropbox or another synced/network filesystem.

```bash
export CROWN_DB="$HOME/.local/state/hermes-crown/shared.db"
```

In three terminals, from the project directory, run respectively:

```bash
python3 -m crown run --name research
python3 -m crown run --name writer
python3 -m crown run --name builder
```

Each command stays running. In a fourth terminal, read the outlet:

```bash
python3 -m crown watch
```

Queue a question, then record an answer explicitly:

```bash
python3 -m crown report --id research-format-1 --source research --task report-42 --kind question 'PDF or Markdown?'
python3 -m crown answer research-format-1 'Markdown, please.' --id human-answer-1
python3 -m crown status
```

Reports include the source instance, task and optional originating conversation.
An answer is tied to its question, not inferred from a bare “yes.” Reusing an
event/answer ID with identical content is safe; reusing it with different content
fails. Already answered questions are never selected as new publications.

`watch` is a view of the durable outlet, not a delivery service. It replays
history unless given `--after SEQUENCE`. A question already published remains in
the pending-question view through a takeover without creating another message.

## Hermes plugin

This is a native directory plugin: `plugin.yaml` + root `register(ctx)`.
It uses public configuration, tool, hook, command, CLI and unload registration
surfaces. It imports no Hermes internals, overrides no built-in tools and adds
no Python dependencies.

The host integration exposes:

| Surface | Behavior |
| --- | --- |
| `crown_report` | Queue an attributed question/update; never send directly |
| `crown_context` | Read saved human answers and pending questions, optionally by task |
| `pre_llm_call` | Supply a bounded record excerpt; full task record remains available through the tool |
| `/crown here` | Explicitly enroll this runtime as speaker/standby; start its heartbeat |
| `/crown leave` | Stop enrollment and release its lease |
| `/crown answer QUESTION_ID TEXT` | Save a human answer from a local command |
| `/crown status` | Inspect the lease and pending questions |
| `hermes crown ...` | The same CLI as `python3 -m crown ...` |

`/crown here` enrolls until `/crown leave`, plugin unload, or process termination.
It does not detect attention or switch speakers when a different window is
focused. A healthy lease is never preempted. After expiry, the first successful
transaction wins. This is lease-based coordination inspired by leader election;
it is not an implementation of Spanning Tree Protocol or distributed consensus.

Before enabling on a real installation, validate the directory with that host:

```bash
hermes plugins validate /absolute/path/to/hermes-crown
```

For a source/manual installation, copy this directory to the intended profile's
`plugins/crown/`, then enable the plugin with the host's plugin manager. Configure
each participating profile using its existing `config.yaml` (merge these keys;
do not replace the rest of the file):

```yaml
plugins:
  enabled:
    - crown
  entries:
    crown:
      settings:
        database: /absolute/local/path/shared-crown.db
        instance_name: research
```

Use distinct `instance_name` labels and the same `database`. `CROWN_DB` and
`CROWN_INSTANCE` are optional process-environment fallbacks; profile settings
take precedence. Shared-gateway deployments should use profile settings, not
global environment variables. Settings are captured on first use; restart the
plugin to change them. Enable/restart through Hermes as appropriate for the host.

Loading the plugin creates no database and starts no worker. Run `/crown here`
explicitly in each participating local runtime, and use the shared CLI outlet.
Only CLI/TUI/Desktop local slash commands are accepted in v0.1. No answer-writing
tool is exposed to the model. An agent with unrestricted shell access can still
modify files or run the operator CLI: Crown is not a sandbox against a malicious
local process.

## The guarantee and its boundary

- One current lease owner per database, with monotonically increasing epochs.
- Expired/stale ownership cannot renew, release a successor or publish.
- Lease check, answer snapshot, unique message insert and audit entry commit in
  one `BEGIN IMMEDIATE` transaction. The database is the publication boundary.
- Answers, pending questions and publications survive worker death. SQLite WAL,
  foreign keys and `synchronous=FULL` are enabled.
- Linux boot-time monotonic time avoids wall-clock jumps and counts suspend.
  Reboot invalidates leases via boot ID while preserving the record.
- If the database is unavailable, workers do not publish through a fallback.

This guarantee applies **only to Crown's owned outlet**. Native Hermes chat,
approvals, cron notifications and unrelated messaging remain under Hermes's
existing behavior. A plugin must not claim to silence every Hermes instance
through a prompt. No phone, Telegram, network server or external send is included.

The record survives a worker dying; it does not survive destruction of its disk.
The database is one authority, not a highly available distributed database. No
claim is made about cross-machine partitions, exactly-once external delivery,
or semantic deduplication of differently worded reports.

## Next real-Hermes acceptance test

Use three genuinely independent Hermes runtimes, not three labels hosted in one
process that dies as a unit. Each queues one real tool result. Save a local human
answer, kill the speaking runtime, verify the next runtime sees that answer via
`crown_context`, then revive a paused former speaker and verify rejection at the
outlet. Record the uncut result before treating this as a submission-ready demo.

No repository has been published, catalog entry submitted, or prize claimed.
