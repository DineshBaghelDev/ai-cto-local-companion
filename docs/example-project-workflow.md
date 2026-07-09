# Example Project Workflow

This uses the sample PRD and the simulated runner fallback if AO is not on `PATH`. Simulated mode creates real worktrees and patches, but the agent output is a marker commit, not real code.

## 1. Check Decisions And Dependencies

```powershell
python scripts\cto.py doctor
.\scripts\start.ps1
```

Expected: decisions ok, runner is AO, voice is the local Pipecat stack.

## 2. Create Or Use A PRD

Existing sample:

```powershell
vault\prds\sample-greet-cli.md
```

New PRD:

```powershell
python scripts\cto.py prd --title "Greet CLI" --goal "Add a useful greeting CLI" --task "Add a --version flag (agent: codex)" --task "Add a smoke test (agent: claude)"
python scripts\cto.py approve prds\greet-cli.md
```

## 3. Dispatch

```powershell
python scripts\cto.py dispatch --prd vault\prds\sample-greet-cli.md --repo sandbox\greet-cli
```

## 4. Poll And Collect

```powershell
python scripts\cto.py poll
python scripts\cto.py collect
python scripts\cto.py preview --run <run-id> --cmd "python -m pytest -q"
```

Outputs:

- Runtime state: `~/.ai-cto/coder.db`
- Patches: `~/.ai-cto/patches/`
- Review report: `vault/coding-runs/<run>.md`
- Preview output: `vault/coding-runs/<run>-preview.md`

## 5. Give Feedback

```powershell
python scripts\cto.py feedback --task <task-id> --message "Fix the failing pytest assertion and rerun."
python scripts\cto.py poll --run <run-id>
python scripts\cto.py collect --run <run-id>
```

You can also give this feedback by voice. Ask the companion to send feedback to the task id.

## 6. Handle A Blocker

```powershell
python scripts\escalation.py detect
python scripts\escalation.py list
```

If a task is blocked, open the voice companion at `http://localhost:7860`, ask about blockers, give the decision, and the decision is saved back to Markdown before the task resumes.

## 7. Optional PR Flow

Requires `gh` installed and logged in:

```powershell
python scripts\cto.py pr --task <task-id>
python scripts\cto.py merge-pr --pr <number-or-url> --repo C:\path\to\repo --method squash
```

PR creation pushes the task worktree branch. Merge is never automatic; run the merge command only after review.
