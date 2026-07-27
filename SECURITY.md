# Security Policy

## Supported Versions

Only the latest development branch is supported during the beta period.

## Reporting

After a public repository is created, report vulnerabilities through GitHub
private vulnerability reporting. Do not open a public issue for exposed keys,
credential leakage, command injection, cross-app authorization problems, or
unsafe write behavior.

Until then, keep reports local to the maintainers who control this candidate.

## Sensitive Artifacts

Do not commit:

- `.env` files or API keys;
- task files containing real credentials;
- message/request dumps;
- screenshots or trajectories from private systems;
- result archives;
- container exports or database snapshots.

## Operational Warning

This software can operate a browser, call application APIs, modify application
state, and execute commands inside named containers. Use disposable instances,
least-privilege credentials, network isolation, and explicit write gates.
