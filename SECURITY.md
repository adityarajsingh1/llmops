# Security

llmops sits in front of your provider API keys and decides who may spend money, so security bugs matter here. If you find a way around the team keys, budgets or rate limits, a way to read another team's cached answers or samples, or a way to get a provider key out of the gateway, please report it privately through [GitHub's private vulnerability reporting](https://github.com/adityarajsingh1/llmops/security/advisories/new) rather than a public issue.

Please include what you did, what happened, and the version (`llmops version`). I'll reply as soon as I can and credit you in the fix unless you'd rather not be named.

Things that are working as intended:

- `/metrics` and `/health` have no authentication. They're meant for an internal network; don't expose them publicly.
- The demo key in `gateway.example.yaml` is public on purpose. Don't use that config in production.
