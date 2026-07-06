# Example mission

A realistic first mission to give eng-orc after setup:

```bash
orc new "Build a small note-taking REST API in Python with FastAPI: CRUD for
notes (title, body, tags), SQLite persistence, full pytest coverage of the
endpoints, and a README with run instructions. Keep it a single small package
with no auth." --slug notes-api

orc run notes-api --watch
```

What to expect:
- charter: should proceed with zero or one question (it has everything it
  needs — if it asks more, that's charterer-tuning feedback);
- plan: typically 3–5 items (scaffold, models+storage, endpoints, tests/docs);
- build: watch `orc status notes-api` between steps; test-first items write
  failing tests before implementations;
- artifacts: `~/.eng-orc/projects/notes-api/artifacts/` holds every attempt
  transcript, review, and the final report.

Then try the brownfield path on a real repo:

```bash
orc new "Add a --json output flag to the CLI" --repo ~/code/some-tool
```

The scout will map the repo first (`codebase-report.md`), and all context
(repo map, semantic index, grep) is built from your actual code.
