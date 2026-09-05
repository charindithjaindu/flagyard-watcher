# flagyard-watcher

Watch a FlagYard event/lab for new challenges and auto-spawn a [T3
Code](https://github.com/charindithjaindu/ctf-autopwn) solver thread per
challenge — unattended.

See [`SKILL.md`](SKILL.md) for full usage, options, and dependencies.

## Dependencies

- [flagyard-submit](https://github.com/RusiruSadathana/flagyard-submit) — FlagYard API, auth, Telegram reporting
- `t3-manage` — spawns/monitors the T3 Code threads

## Quick start

```bash
git clone https://github.com/charindithjaindu/flagyard-watcher.git
cd flagyard-watcher
./install.sh

python3 scripts/flagyard_watcher.py --event-id <uuid> --once --dry-run
```

## License

MIT
